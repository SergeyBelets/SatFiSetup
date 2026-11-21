// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./DKGAuthorization.sol";
import "./IBitcoinAddressRegistry.sol";

interface IBitcoinMintAdmissionPool {
    function authorization() external view returns (FROSTAuthorization);
    function mintAdmission() external view returns (address);
    function nonce() external view returns (uint256);
    function mintedAllocations(bytes32 outpoint) external view returns (bytes32);
    function getMintOperationHash(bytes32 txid, uint32 vout, address[] calldata recipients, uint256[] calldata amounts)
        external view returns (bytes32);
    function mintProofVersion() external view returns (uint8);
    function deposits() external view returns (address);
    function mintedAddressIntents(bytes32 outpoint) external view returns (bytes32);
    function getAddressMintOperationHash(IBitcoinAddressRegistry.MintClaim calldata claim) external view returns (bytes32);
    function getCustodyMintOperationHash(bytes32 txid, uint32 vout, address[] calldata recipients,
        uint256[] calldata amounts, uint256 generation) external view returns (bytes32);
}

interface IAdmissionDeposits { function addressRegistry() external view returns (IBitcoinAddressRegistry); }

/// @notice Canonical single-slot admission before a standard client's mint FROST session.
/// @dev Does not change legacy mint authorization or reserve Bitcoin funds. The pool nonce
/// and deposit registry remain the final execution guards, including for legacy callers.
contract BitcoinMintAdmission {
    uint256 public constant MAX_ATTEMPTS = 8;
    uint256 public constant MAX_RECIPIENTS = 64;
    IBitcoinMintAdmissionPool public immutable pool;
    DKGAuthorization public immutable authorization;
    FROSTCoordinator public immutable coordinator;
    bytes32 public immutable dkgId;
    bytes32 public immutable resultHash;
    address public operator;
    uint256 public operatorNonce;
    enum MintMode { LEGACY, CUSTODY, ADDRESS }
    struct Fence { uint256 custody; uint256 derivation; }

    struct Claim {
        bytes32 txid;
        uint32 vout;
        address[] recipients;
        uint256[] amounts;
        uint64 deadline;
        uint256 attemptCount;
        uint256 latestSession;
    }
    struct Attempt {
        bytes32 claimId;
        bytes32 operationHash;
        uint256 operationNonce;
        uint64 deadline;
        bool retired;
    }
    struct OperatorTransfer {
        address newOperator;
        uint256 nonce;
        uint64 deadline;
        bytes32 operationHash;
    }
    mapping(bytes32 => Claim) private claims;
    mapping(bytes32 => MintMode) public claimModes;
    mapping(bytes32 => IBitcoinAddressRegistry.MintClaim) private addressClaims;
    mapping(uint256 => Fence) public attemptFences;
    /// @notice Immutable proposer provenance, distinct from transferable operator authority.
    mapping(bytes32 => address) public claimOperator;
    mapping(uint256 => Attempt) public attempts;
    mapping(uint256 => uint256) public slotSession;
    mapping(uint256 => OperatorTransfer) public operatorTransfers;
    mapping(uint256 => uint256) public operatorTransferSession;
    /// @notice Retained predecessor for distinct, replay-safe proposals after retirement.
    mapping(uint256 => uint256) public latestOperatorTransferSession;
    mapping(uint256 => bool) public operatorTransferRetired;

    event ClaimOpened(bytes32 indexed claimId, address indexed openedBy, uint64 deadline);
    event MintAdmitted(bytes32 indexed claimId, uint256 indexed session, uint256 indexed operationNonce,
        bytes32 operationHash, uint64 deadline, uint256 attemptCount);
    event MintRetired(bytes32 indexed claimId, uint256 indexed session, uint256 operationNonce);
    event OperatorTransferOpened(uint256 indexed session, address indexed newOperator,
        uint256 indexed operatorNonce, uint64 deadline, bytes32 operationHash);
    event OperatorTransferRetired(uint256 indexed session);
    event OperatorTransferred(address indexed previousOperator, address indexed newOperator, uint256 nonce);

    constructor(address pool_, address operator_) {
        require(pool_.code.length > 0, "pool");
        pool = IBitcoinMintAdmissionPool(pool_);
        authorization = DKGAuthorization(address(pool.authorization()));
        coordinator = FROSTCoordinator(address(authorization.coordinator()));
        dkgId = authorization.dkgId();
        resultHash = authorization.resultHash();
        require(address(coordinator.dkgRegistry()) == authorization.registry(), "registry");
        require(authorization.isParticipant(operator_), "operator participant");
        operator = operator_;
    }

    modifier participant() {
        require(authorization.isParticipant(msg.sender), "participant");
        require(pool.mintAdmission() == address(this), "unbound admission");
        _;
    }

    function getClaimId(bytes32 txid, uint32 vout) public view returns (bytes32) {
        return keccak256(abi.encode(address(pool), txid, vout));
    }

    function getClaim(bytes32 claimId) external view returns (
        bytes32 txid, uint32 vout, address[] memory recipients, uint256[] memory amounts,
        uint64 deadline, uint256 attemptCount, uint256 latestSession
    ) {
        Claim storage c = claims[claimId];
        return (c.txid, c.vout, c.recipients, c.amounts, c.deadline, c.attemptCount, c.latestSession);
    }

    function getAddressClaim(bytes32 claimId) external view returns (IBitcoinAddressRegistry.MintClaim memory) {
        return addressClaims[claimId]; // Original snapshot, immutable economic terms.
    }
    function getAttemptAddressClaim(uint256 session) public view returns (IBitcoinAddressRegistry.MintClaim memory c) {
        c = addressClaims[attempts[session].claimId];
        c.custodyGeneration = attemptFences[session].custody;
        c.derivationGeneration = attemptFences[session].derivation;
    }
    function _registry() private view returns (IBitcoinAddressRegistry) {
        return IAdmissionDeposits(pool.deposits()).addressRegistry();
    }
    function _economics(IBitcoinAddressRegistry.MintClaim memory c) private pure returns (bytes32) {
        return keccak256(abi.encode(c.intentId, c.bindingCommitment, c.txid, c.vout,
            c.recipient, c.satoshis, c.validationProvenance));
    }

    function openAddressMint(IBitcoinAddressRegistry.MintClaim calldata proposed, uint64 deadline,
        uint256 expectedNonce) external participant returns (uint256)
    {
        require(pool.mintProofVersion() == 2 && pool.nonce() == expectedNonce, "mint profile or nonce");
        _registry().validateMint(address(pool), proposed);
        require(proposed.satoshis > 0, "amount");
        bytes32 id = getClaimId(proposed.txid, proposed.vout);
        Claim storage c = claims[id];
        if (c.deadline != 0) {
            require(claimModes[id] == MintMode.ADDRESS && c.deadline == deadline
                && _economics(addressClaims[id]) == _economics(proposed), "claim terms");
            _requireIncomplete(c);
            require(active(c.latestSession), "explicit retry required");
            return c.latestSession;
        }
        require(msg.sender == operator && deadline > block.timestamp, "operator or deadline");
        c.txid = proposed.txid; c.vout = proposed.vout; c.deadline = deadline;
        c.recipients.push(proposed.recipient); c.amounts.push(uint256(proposed.satoshis) * 1e10);
        claimModes[id] = MintMode.ADDRESS; addressClaims[id] = proposed;
        _requireIncomplete(c); claimOperator[id] = msg.sender;
        emit ClaimOpened(id, msg.sender, deadline);
        return _admit(id, c, expectedNonce, proposed.custodyGeneration, proposed.derivationGeneration);
    }

    function openCustodyMint(bytes32 txid, uint32 vout, address[] calldata recipients, uint256[] calldata amounts,
        uint256 generation, uint64 deadline, uint256 expectedNonce) external participant returns (uint256)
    {
        require(pool.mintProofVersion() == 2 && pool.nonce() == expectedNonce, "mint profile or nonce");
        _registry().requireCustody(generation);
        bytes32 id = getClaimId(txid, vout); Claim storage c = claims[id];
        if (c.deadline != 0) {
            require(claimModes[id] == MintMode.CUSTODY && c.deadline == deadline
                && keccak256(abi.encode(c.recipients, c.amounts)) == keccak256(abi.encode(recipients, amounts)), "claim terms");
            _requireIncomplete(c); require(active(c.latestSession), "explicit retry required"); return c.latestSession;
        }
        require(msg.sender == operator && deadline > block.timestamp, "operator or deadline");
        require(recipients.length > 0 && recipients.length <= MAX_RECIPIENTS && recipients.length == amounts.length, "distribution");
        for (uint256 i; i < recipients.length; ++i) require(recipients[i] != address(0), "recipient");
        c.txid = txid; c.vout = vout; c.recipients = recipients; c.amounts = amounts; c.deadline = deadline;
        claimModes[id] = MintMode.CUSTODY; _requireIncomplete(c); claimOperator[id] = msg.sender;
        emit ClaimOpened(id, msg.sender, deadline);
        return _admit(id, c, expectedNonce, generation, 0);
    }

    /// @notice New claims require the current operator. Anyone in the immutable roster
    /// can adopt an identical active claim after a lost receipt or lost operator.
    function openMint(bytes32 txid, uint32 vout, address[] calldata recipients, uint256[] calldata amounts,
        uint64 deadline, uint256 expectedNonce) external participant returns (uint256 session)
    {
        require(pool.mintProofVersion() == 1, "generation admission required");
        require(pool.nonce() == expectedNonce, "nonce changed");
        bytes32 claimId = getClaimId(txid, vout);
        Claim storage c = claims[claimId];
        if (c.deadline != 0) {
            require(claimModes[claimId] == MintMode.LEGACY && c.deadline == deadline
                && keccak256(abi.encode(c.recipients, c.amounts)) == keccak256(abi.encode(recipients, amounts)),
                "claim terms");
            _requireIncomplete(c);
            session = c.latestSession;
            require(active(session), "explicit retry required");
            return session;
        }
        require(msg.sender == operator, "operator");
        require(deadline > block.timestamp, "deadline");
        require(recipients.length > 0 && recipients.length <= MAX_RECIPIENTS
            && recipients.length == amounts.length, "distribution");
        for (uint256 i; i < recipients.length; ++i) require(recipients[i] != address(0), "recipient");
        c.txid = txid;
        c.vout = vout;
        c.recipients = recipients;
        c.amounts = amounts;
        c.deadline = deadline;
        _requireIncomplete(c);
        claimOperator[claimId] = msg.sender;
        emit ClaimOpened(claimId, msg.sender, deadline);
        return _admit(claimId, c, expectedNonce, 0, 0);
    }

    /// @notice Explicit bounded retry of immutable economic terms. Advancing the pool
    /// nonce makes an old authorization unusable, even if its session finalized.
    function retryMint(bytes32 claimId, uint256 expectedNonce, uint256 expectedPreviousSession)
        external participant returns (uint256)
    {
        require(msg.sender == operator, "operator");
        require(pool.nonce() == expectedNonce, "nonce changed");
        Claim storage c = claims[claimId];
        require(claimModes[claimId] == MintMode.LEGACY, "generation retry required");
        require(c.deadline != 0, "unknown claim");
        _requireIncomplete(c);
        uint256 previous = c.latestSession;
        require(previous == expectedPreviousSession, "attempt changed");
        if (active(previous)) return previous;
        if (!attempts[previous].retired) _retire(previous);
        return _admit(claimId, c, expectedNonce, 0, 0);
    }

    function retryGenerationMint(bytes32 claimId, uint256 expectedNonce, uint256 expectedPreviousSession,
        uint256 cg, uint256 dg) external participant returns (uint256)
    {
        require(msg.sender == operator && pool.nonce() == expectedNonce, "operator or nonce");
        require(claimModes[claimId] != MintMode.LEGACY && pool.mintProofVersion() == 2, "generation claim required");
        Claim storage c = claims[claimId]; require(c.deadline != 0, "unknown claim"); _requireIncomplete(c);
        uint256 previous = c.latestSession; require(previous == expectedPreviousSession, "attempt changed");
        _validateFence(claimId, cg, dg);
        if (active(previous)) return previous;
        if (!attempts[previous].retired) _retire(previous);
        return _admit(claimId, c, expectedNonce, cg, dg);
    }

    function _validateFence(bytes32 id, uint256 cg, uint256 dg) private view {
        if (claimModes[id] == MintMode.ADDRESS) {
            IBitcoinAddressRegistry.MintClaim memory c = addressClaims[id];
            c.custodyGeneration = cg; c.derivationGeneration = dg;
            _registry().validateMint(address(pool), c);
        } else { require(dg == 0, "custody derivation"); _registry().requireCustody(cg); }
    }

    function _fenced(uint256 session) private view returns (bool) {
        MintMode mode = claimModes[attempts[session].claimId];
        if (mode == MintMode.LEGACY) return pool.mintProofVersion() != 1;
        IBitcoinAddressRegistry registry = _registry();
        if (registry.custodyGeneration() > attemptFences[session].custody) return true;
        if (mode == MintMode.ADDRESS) {
            return registry.derivationGeneration() > attemptFences[session].derivation;
        }
        return false;
    }

    function active(uint256 session) public view returns (bool) {
        Attempt storage a = attempts[session];
        if (session == 0 || a.claimId == 0 || a.retired || pool.mintAdmission() != address(this)
            || pool.nonce() != a.operationNonce || slotSession[a.operationNonce] != session) return false;
        MintMode mode = claimModes[a.claimId];
        if (mode == MintMode.LEGACY && pool.mintProofVersion() != 1) return false;
        if (mode == MintMode.ADDRESS) {
            try _registry().validateMint(address(pool), getAttemptAddressClaim(session)) returns (bytes32) {}
            catch { return false; }
        } else if (mode == MintMode.CUSTODY) {
            try _registry().requireCustody(attemptFences[session].custody) {} catch { return false; }
        }
        uint256 state = _state(session);
        return state == uint256(FROSTCoordinator.SessionState.OPENED)
            || state == uint256(FROSTCoordinator.SessionState.FINALIZED);
    }

    /// @notice Expiry alone cannot release an executable authorization. An OPENED
    /// expired session is aborted atomically; finalized/approved sessions are retained.
    function retire(uint256 session) external participant { _retire(session); }

    function _retire(uint256 session) private {
        Attempt storage a = attempts[session];
        require(session != 0 && a.claimId != 0, "unknown session");
        if (a.retired) return;
        require(pool.nonce() >= a.operationNonce, "nonce regression");
        if (pool.nonce() == a.operationNonce && !_fenced(session)) {
            _retireCurrent(session, a.operationHash, a.deadline);
        }
        a.retired = true;
        if (slotSession[a.operationNonce] == session) delete slotSession[a.operationNonce];
        emit MintRetired(a.claimId, session, a.operationNonce);
    }

    function _admit(bytes32 claimId, Claim storage c, uint256 expectedNonce, uint256 cg, uint256 dg) private returns (uint256 session) {
        require(c.deadline > block.timestamp, "deadline");
        require(c.attemptCount < MAX_ATTEMPTS, "attempt limit");
        require(slotSession[expectedNonce] == 0, "slot busy");
        bytes32 op;
        if (claimModes[claimId] == MintMode.ADDRESS) {
            _validateFence(claimId, cg, dg);
            IBitcoinAddressRegistry.MintClaim memory proposed = addressClaims[claimId];
            proposed.custodyGeneration = cg; proposed.derivationGeneration = dg;
            op = pool.getAddressMintOperationHash(proposed);
        } else if (claimModes[claimId] == MintMode.CUSTODY) {
            _validateFence(claimId, cg, dg);
            op = pool.getCustodyMintOperationHash(c.txid, c.vout, c.recipients, c.amounts, cg);
        } else op = pool.getMintOperationHash(c.txid, c.vout, c.recipients, c.amounts);
        // Another legacy session may have already produced the same operation.
        require(!authorization.approvedOperations(op), "operation approved");
        session = coordinator.createSessionFromDKG(dkgId, op, address(pool), c.deadline);
        attempts[session] = Attempt(claimId, op, expectedNonce, c.deadline, false);
        attemptFences[session] = Fence(cg, dg);
        slotSession[expectedNonce] = session;
        c.latestSession = session;
        ++c.attemptCount;
        emit MintAdmitted(claimId, session, expectedNonce, op, c.deadline, c.attemptCount);
    }

    function _requireIncomplete(Claim storage c) private view {
        // Any successful allocation consumes this outpoint, including a conflicting
        // legacy allocation. Client recovery distinguishes exact completion first.
        require(pool.mintedAllocations(keccak256(abi.encode(c.txid, c.vout))) == 0, "outpoint minted");
    }

    function getTransferOperatorOperationHash(address next) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.transferOperator.selector,
            keccak256(abi.encode(next)), operatorNonce);
    }

    /// @notice Typed public proposal remains available when the old operator is lost.
    function openOperatorTransfer(address next, uint64 deadline, uint256 expectedNonce, uint256 expectedPreviousSession)
        external participant returns (uint256 session)
    {
        require(operatorNonce == expectedNonce, "operator nonce changed");
        require(next != operator && authorization.isParticipant(next), "next operator");
        require(latestOperatorTransferSession[operatorNonce] == expectedPreviousSession, "attempt changed");
        session = operatorTransferSession[operatorNonce];
        if (session != 0) {
            OperatorTransfer storage current = operatorTransfers[session];
            require(current.newOperator == next && current.deadline == deadline
                && activeOperatorTransfer(session), "transfer slot busy");
            return session;
        }
        require(deadline > block.timestamp, "deadline");
        bytes32 op = getTransferOperatorOperationHash(next);
        require(!authorization.approvedOperations(op), "operation approved");
        session = coordinator.createSessionFromDKG(dkgId, op, address(this), deadline);
        operatorTransfers[session] = OperatorTransfer(next, operatorNonce, deadline, op);
        operatorTransferSession[operatorNonce] = session;
        latestOperatorTransferSession[operatorNonce] = session;
        emit OperatorTransferOpened(session, next, operatorNonce, deadline, op);
    }

    function activeOperatorTransfer(uint256 session) public view returns (bool) {
        OperatorTransfer storage t = operatorTransfers[session];
        if (session == 0 || t.newOperator == address(0) || t.nonce != operatorNonce
            || operatorTransferRetired[session] || operatorTransferSession[t.nonce] != session
            || pool.mintAdmission() != address(this)) return false;
        uint256 state = _state(session);
        return state == uint256(FROSTCoordinator.SessionState.OPENED)
            || state == uint256(FROSTCoordinator.SessionState.FINALIZED);
    }

    function retireOperatorTransfer(uint256 session) external participant {
        OperatorTransfer storage t = operatorTransfers[session];
        require(t.newOperator != address(0), "unknown transfer");
        if (operatorTransferRetired[session]) return;
        require(operatorNonce >= t.nonce, "nonce regression");
        if (operatorNonce == t.nonce) _retireCurrent(session, t.operationHash, t.deadline);
        operatorTransferRetired[session] = true;
        if (operatorTransferSession[t.nonce] == session) delete operatorTransferSession[t.nonce];
        emit OperatorTransferRetired(session);
    }

    function transferOperator(address next, bytes32 op) external {
        require(pool.mintAdmission() == address(this), "unbound admission");
        require(next != operator && authorization.isParticipant(next), "next operator");
        require(op == getTransferOperatorOperationHash(next), "hash");
        authorization.consumeOperation(op);
        address previous = operator;
        operator = next;
        uint256 previousNonce = operatorNonce++;
        emit OperatorTransferred(previous, next, previousNonce);
    }

    function _retireCurrent(uint256 session, bytes32 op, uint64 deadline) private {
        uint256 state = _state(session);
        require(state != uint256(FROSTCoordinator.SessionState.FINALIZED)
            && !authorization.approvedOperations(op), "executable authorization");
        if (state == uint256(FROSTCoordinator.SessionState.OPENED)) {
            require(block.timestamp > deadline, "session live");
            coordinator.abortSession(session, "admission original deadline expired");
        } else {
            require(state == uint256(FROSTCoordinator.SessionState.ABORTED), "unknown session state");
        }
    }

    function _state(uint256 session) private view returns (uint256 state) {
        (,,,,,,,,,, state,,,,,,,,,) = coordinator.getSession(session);
    }
}
