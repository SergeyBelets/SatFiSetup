// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./BitcoinDepositRegistry.sol";
import "./DKGAuthorization.sol";
import "../core/BitcoinDepositDerivation.sol";
import {RecipientPolicy} from "./RecipientPolicy.sol";
import {BitcoinKeyFamilyRegistry} from "./BitcoinKeyFamilyRegistry.sol";

interface IAddressPoolContext {
    function deposits() external view returns (address);
    function escrow() external view returns (address);
    function authorization() external view returns (address);
    function mpToken() external view returns (address);
    function payoutScript() external view returns (bytes memory);
    function mintAdmission() external view returns (address);
}
interface IQualifiedDepositDerivation {
    function PROFILE() external view returns (bytes32);
    function QUALIFIED() external view returns (bool);
    function derive(bytes calldata intent, bytes calldata baseGroup)
        external view returns (BitcoinDepositDerivation.Descriptor memory);
}

/// @notice Immutable intent bindings and current issuance fences for one custody graph.
/// @dev Install in a NEW common registry before its first base registration.
/// Registration is not Ready: watch, recovery and client qualification remain external gates.
/// No verifier replacement, beneficiary reassignment, fund transfer or emergency key.
contract BitcoinAddressRegistry is IBitcoinAddressRegistry {
    uint256 public constant MAX_BATCH = 8;
    bytes32 public constant PROFILE = keccak256("BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1");
    bytes32 public constant MINT_SCHEMA = keccak256("SATOHSI_ADDRESS_MINT_V1");
    enum CustodyState { ACTIVE, DRAINING, SUSPENDED, COMPROMISED }
    enum DerivationState { QUALIFIED, SUSPENDED, REVOKED }

    address public immutable deposits;
    address public immutable authorization;
    address public immutable escrow;
    BitcoinBatchRegistry public immutable batches;
    DKGCoordinator public immutable dkg;
    bytes32 public immutable bitcoinGenesis;
    bytes32 public immutable walletId;
    bytes32 public immutable walletResult;
    uint64 public immutable keyEpoch;
    bytes32 public immutable baseX;
    bytes32 public immutable graphId;
    bytes32 public immutable custodyScope;
    bytes32 public immutable derivationScope;
    IQualifiedDepositDerivation public immutable verifier;
    bytes32 public immutable verifierRuntimeHash;
    bytes32 public immutable verifierSourceHash;
    bytes32 public immutable qualificationCommitment;
    bytes32 public immutable servicePolicyHash;
    uint256 public immutable chainId;
    RecipientPolicy public immutable recipientPolicy;
    BitcoinKeyFamilyRegistry public familyRegistry;
    bytes32 public familyBindReceipt;
    event FamilyRegistryBound(address indexed directory,bytes32 indexed family);

    CustodyState public custodyState;
    DerivationState public derivationState = DerivationState.SUSPENDED;
    bool public issuanceClosed;
    uint256 public custodyGeneration = 1;
    uint256 public derivationGeneration = 1;
    uint256 public custodyStatusNonce;
    uint256 public derivationStatusNonce;
    uint256 public registrationNonce;

    struct Binding {
        bytes intent;
        address pool;
        address token;
        address requester;
        address recipient;
        bytes32 scriptHash;
        bytes32 requestNonce;
        BitcoinDepositDerivation.Descriptor descriptor;
        bytes32 validationProvenance;
        uint256 registeredBlock;
        bytes32 parentBlockHash;
    }
    struct IncidentReport {
        bytes32 incidentId;
        bytes32 reason;
        bytes32 evidenceHash;
        bytes32 bitcoinBlock;
        bytes32 bitcoinTx;
        bytes32 evmAnchor;
    }
    struct StatusChange {
        uint8 scope; // 0 CUSTODY, 1 DERIVATION. No caller-assigned scope aliases.
        uint8 expectedState;
        uint256 expectedGeneration;
        uint256 sequence;
        uint8 nextState;
        bool closeIssuance;
        uint64 deadline;
        IncidentReport report;
    }
    mapping(bytes32 => Binding) private bindings;
    mapping(bytes32 => bytes32) public scriptIntents;
    mapping(bytes32 => bytes32) public requestIntents;
    mapping(bytes32 => bool) public registrationReceipts;
    mapping(bytes32 => uint256) public statusReceipts;

    event AddressRegistered(bytes32 indexed intentId, bytes32 indexed scriptHash, address indexed pool,
        bytes32 bindingCommitment, bytes32 validationProvenance);
    event StatusChanged(bytes32 indexed scope, uint8 nextState, uint256 generation, uint256 sequence,
        bool issuanceClosed, bytes32 operationHash, IncidentReport report);

    constructor(address deposits_, bytes32 genesis, address verifier_, bytes32 expectedRuntimeHash,
        bytes32 sourceHash, bytes32 qualification, bytes32 servicePolicy)
    {
        recipientPolicy=new RecipientPolicy();
        require(deposits_.code.length > 0 && genesis != 0 && verifier_.code.length > 0, "address config");
        require(expectedRuntimeHash != 0 && verifier_.codehash == expectedRuntimeHash
            && sourceHash != 0 && qualification != 0 && servicePolicy != 0, "verifier provenance");
        deposits = deposits_;
        BitcoinDepositRegistry common = BitcoinDepositRegistry(deposits_);
        authorization = address(common.authorization());
        escrow = common.spendRecorder();
        batches = BitcoinWithdrawalEscrow(escrow).batches();
        dkg = batches.dkg();
        require(address(batches.authorization()) == authorization
            && address(BitcoinWithdrawalEscrow(escrow).spv()) == address(common.spv())
            && address(BitcoinWithdrawalEscrow(escrow).depositRegistry()) == deposits_
            && batches.withdrawalEscrow() == escrow, "custody graph");
        DKGAuthorization auth = DKGAuthorization(authorization);
        require(auth.registry() == address(dkg), "EVM DKG registry");
        (, bytes32 evmResult,, bytes32 evmPurpose, bytes memory evmGroup,
            address[] memory evmRoster, uint256 evmThreshold) = dkg.getCompletedResult(auth.dkgId());
        require(evmPurpose == keccak256("EVM_AUTH") && evmResult == auth.resultHash()
            && keccak256(evmGroup) == keccak256(auth.groupPubkey())
            && evmThreshold == auth.threshold()
            && keccak256(abi.encode(evmRoster)) == keccak256(abi.encode(auth.getParticipants())), "EVM DKG result");
        walletId = batches.walletId();
        (, bytes32 result, uint64 epoch, bytes32 purpose, bytes memory group,
            address[] memory roster, uint256 threshold) = dkg.getCompletedResult(walletId);
        require(result != 0 && result == batches.walletResult() && purpose == keccak256("BTC_SPEND")
            && group.length == 32 && threshold == evmThreshold
            && keccak256(abi.encode(roster)) == keccak256(abi.encode(evmRoster)), "BTC DKG result");
        walletResult = result;
        keyEpoch = epoch;
        baseX = bytes32(group);
        bitcoinGenesis = genesis;
        chainId = block.chainid;
        verifier = IQualifiedDepositDerivation(verifier_);
        require(verifier.PROFILE() == PROFILE, "derivation profile");
        verifierRuntimeHash = expectedRuntimeHash;
        verifierSourceHash = sourceHash;
        qualificationCommitment = qualification;
        servicePolicyHash = servicePolicy;
        graphId = keccak256(abi.encode(keccak256("SATOHSI_CUSTODY_GRAPH_V1"), block.chainid,
            deposits_, escrow, address(batches), address(dkg), authorization, address(common.spv()),
            genesis, walletId, result, epoch, baseX));
        custodyScope = keccak256(abi.encode(keccak256("SATOHSI_CUSTODY_SCOPE_V1"),
            block.chainid, address(this), graphId, baseX));
        derivationScope = keccak256(abi.encode(keccak256("SATOHSI_DERIVATION_SCOPE_V1"),
            custodyScope, PROFILE, verifier_, expectedRuntimeHash, sourceHash, qualification));
    }

    function getBinding(bytes32 intentId) external view returns (Binding memory) { return bindings[intentId]; }
    bytes32[] public intentIds;
    function intentCount() external view returns (uint256) { return intentIds.length; }

    /// @notice Bind only an enrollment already authorized by the shared root quorum.
    /// The project root must also match the participant's independently pinned policy.
    function getBindFamilyRegistryOperationHash(address directory) public view returns(bytes32) {
        return FROSTAuthorization(authorization).hashOperation(address(this),this.bindFamilyRegistry.selector,
            keccak256(abi.encode(keccak256("SATOHSI_FAMILY_BIND_V1"),graphId,directory)),0);
    }
    function bindFamilyRegistry(address directory,bytes32 op) external {
        if(familyBindReceipt==op && op!=0) {require(address(familyRegistry)==directory,"family receipt");return;}
        require(address(familyRegistry)==address(0) && registrationNonce==0 && directory.code.length>0,"family fixed");
        BitcoinKeyFamilyRegistry root=BitcoinKeyFamilyRegistry(directory);
        bytes32 id=root.requireGraph(address(this));
        require(id==root.familyId(bitcoinGenesis,baseX),"family binding");
        require(op==getBindFamilyRegistryOperationHash(directory),"family bind hash");
        FROSTAuthorization(authorization).consumeOperation(op);
        familyRegistry=root;familyBindReceipt=op;emit FamilyRegistryBound(directory,id);
    }

    function _bound() private view {
        require(block.chainid == chainId
            && address(BitcoinDepositRegistry(deposits).addressRegistry()) == address(this), "address registry binding");
    }
    function requireCustody(uint256 generation) public view {
        _bound();
        require(address(familyRegistry)!=address(0),"canonical family required");
        require(generation == custodyGeneration
            && (custodyState == CustodyState.ACTIVE || custodyState == CustodyState.DRAINING), "custody fence");
        familyRegistry.fence(address(this),false,false);
    }
    function _qualified() private view {
        require(address(verifier).codehash == verifierRuntimeHash && verifier.PROFILE() == PROFILE
            && verifier.QUALIFIED(), "verifier unqualified");
    }
    function _requireAddress(uint256 cg, uint256 dg, bool issuing) private view {
        requireCustody(cg);
        require(derivationState == DerivationState.QUALIFIED && dg == derivationGeneration, "derivation fence");
        _qualified();
        if (issuing) require(custodyState == CustodyState.ACTIVE && !issuanceClosed, "issuance closed");
        familyRegistry.fence(address(this),true,issuing);
    }

    function getStatusOperationHash(StatusChange calldata c) public view returns (bytes32) {
        require(c.scope <= 1, "status scope");
        return FROSTAuthorization(authorization).hashOperation(address(this), this.setStatus.selector,
            keccak256(abi.encode(keccak256("SATOHSI_ISSUANCE_STATUS_V1"),
                c.scope == 0 ? custodyScope : derivationScope, c)), c.sequence);
    }
    function setStatus(StatusChange calldata c, bytes32 op) external returns (uint256 generation) {
        require(op == getStatusOperationHash(c), "status hash");
        if (statusReceipts[op] != 0) return statusReceipts[op]; // Historical exact receipt; not current permission.
        _bound();
        require(block.timestamp <= c.deadline && c.report.incidentId != 0
            && c.report.reason != 0 && c.report.evidenceHash != 0, "status report");
        if (c.scope == 0) {
            require(c.expectedState == uint8(custodyState) && c.expectedGeneration == custodyGeneration
                && c.sequence == custodyStatusNonce && c.nextState <= uint8(CustodyState.COMPROMISED), "custody transition");
            require(custodyState != CustodyState.COMPROMISED, "custody terminal");
            require(!(issuanceClosed || c.closeIssuance) || c.nextState != uint8(CustodyState.ACTIVE), "issuance irreversible");
        } else {
            require(c.expectedState == uint8(derivationState) && c.expectedGeneration == derivationGeneration
                && c.sequence == derivationStatusNonce && c.nextState <= uint8(DerivationState.REVOKED)
                && !c.closeIssuance, "derivation transition");
            require(derivationState != DerivationState.REVOKED, "derivation terminal");
            if (c.nextState == uint8(DerivationState.QUALIFIED)) _qualified();
        }
        FROSTAuthorization(authorization).consumeOperation(op);
        if (c.scope == 0) {
            custodyState = CustodyState(c.nextState);
            if (c.closeIssuance || custodyState == CustodyState.DRAINING || custodyState == CustodyState.COMPROMISED)
                issuanceClosed = true;
            generation = ++custodyGeneration;
            ++custodyStatusNonce;
        } else {
            derivationState = DerivationState(c.nextState);
            generation = ++derivationGeneration;
            ++derivationStatusNonce;
        }
        statusReceipts[op] = generation;
        if(address(familyRegistry)!=address(0))familyRegistry.sync();
        emit StatusChanged(c.scope == 0 ? custodyScope : derivationScope, c.nextState, generation,
            c.sequence, issuanceClosed, op, c.report);
    }

    function getRegisterOperationHash(bytes[] calldata intents, uint256 cg, uint256 dg,
        uint64 deadline, uint256 sequence) public view returns (bytes32)
    {
        return FROSTAuthorization(authorization).hashOperation(address(this), this.registerBatch.selector,
            keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_REGISTER_V1"), graphId, derivationScope,
                intents, cg, dg, deadline)), sequence);
    }
    function registerBatch(bytes[] calldata intents, uint256 cg, uint256 dg, uint64 deadline,
        uint256 sequence, bytes32 op) external
    {
        require(intents.length > 0 && intents.length <= MAX_BATCH, "registration count");
        for (uint256 i; i < intents.length; ++i) require(intents[i].length == 382, "intent bytes");
        require(op == getRegisterOperationHash(intents, cg, dg, deadline, sequence), "registration hash");
        if (registrationReceipts[op]) return;
        _requireAddress(cg, dg, true);
        require(sequence == registrationNonce && block.timestamp <= deadline, "registration window");
        FROSTAuthorization(authorization).consumeOperation(op);
        ++registrationNonce;
        for (uint256 i; i < intents.length; ++i) _register(intents[i]);
        registrationReceipts[op] = true;
    }

    function requireRecipientNotSink(address pool,address token,address recipient) public view {
        require(recipient != address(0),"recipient profile");
        require(recipient != address(this) && recipient != deposits && recipient != pool && recipient != token
            && recipient != escrow && recipient != address(batches) && recipient != address(dkg)
            && recipient != authorization && recipient != address(FROSTAuthorization(authorization).coordinator())
            && recipient != address(FROSTAuthorization(authorization).verifier())
            && recipient != address(BitcoinDepositRegistry(deposits).spv()) && recipient != address(verifier)
            && recipient != address(recipientPolicy) && recipient != batches.feeReserve()
            && recipient != IAddressPoolContext(pool).mintAdmission()
            && recipient != address(BitcoinWithdrawalEscrow(escrow).callbackPoolRegistry()), "recipient sink");
        address fees=BitcoinWithdrawalEscrow(escrow).withdrawalFees();
        require(recipient != fees && (fees == address(0) || recipient != IWithdrawalFeeCredits(fees).payments()
            && recipient != IWithdrawalFeeCredits(fees).funding()),"recipient sink");
    }
    function _recipient(address recipient,address pool,address token,address requester,bytes32 nonce) private view returns(uint8) {
        requireRecipientNotSink(pool,token,recipient);
        return recipientPolicy.check(address(this),pool,token,requester,recipient,nonce);
    }
    function validateRecipient(address pool,address token,address requester,address recipient,bytes32 nonce) external view returns(uint8) {
        return _recipient(recipient,pool,token,requester,nonce);
    }
    function _register(bytes calldata raw) private {
        require(raw.length == 382, "intent bytes");
        bytes32 intentId = sha256(raw);
        if (bindings[intentId].scriptHash != 0) {
            require(keccak256(bindings[intentId].intent) == keccak256(raw), "intent collision");
            return;
        }
        require(uint256(bytes32(raw[62:94])) == chainId && bytes32(raw[30:62]) == bitcoinGenesis
            && address(bytes20(raw[94:114])) == address(this) && bytes32(raw[154:186]) == graphId
            && address(bytes20(raw[186:206])) == address(dkg) && bytes32(raw[206:238]) == walletId
            && bytes32(raw[238:270]) == walletResult && uint64(bytes8(raw[270:278])) == keyEpoch
            && bytes32(raw[350:382]) == servicePolicyHash, "intent context");
        address pool = address(bytes20(raw[114:134]));
        address token = address(bytes20(raw[134:154]));
        address requester = address(bytes20(raw[278:298]));
        address recipient = address(bytes20(raw[298:318]));
        require(pool.code.length > 0 && IAddressPoolContext(pool).deposits() == deposits
            && IAddressPoolContext(pool).escrow() == escrow && IAddressPoolContext(pool).authorization() == authorization
            && IAddressPoolContext(pool).mpToken() == token
            && keccak256(IAddressPoolContext(pool).payoutScript()) == keccak256(abi.encodePacked(hex"5120", baseX))
            && BitcoinDepositRegistry(deposits).poolScripts(pool) == keccak256(abi.encodePacked(hex"5120", baseX)), "intent pool");
        bytes32 requestNonce = bytes32(raw[318:350]);
        _recipient(recipient,pool,token,requester,requestNonce);
        bytes32 requestKey = keccak256(abi.encode(requester, requestNonce));
        require(requestIntents[requestKey] == 0, "request nonce bound");
        BitcoinDepositDerivation.Descriptor memory d = verifier.derive(raw, abi.encodePacked(hex"02", baseX));
        require(d.intentId == intentId, "validated intent");
        familyRegistry.bindDerived(address(this),d.outputKey,intentId);
        bytes32 scriptHash = keccak256(abi.encodePacked(hex"5120", d.outputKey));
        require(scriptIntents[scriptHash] == 0, "address script bound");
        BitcoinDepositRegistry(deposits).registerAddressScript(scriptHash, pool, intentId);
        bytes32 parent = blockhash(block.number - 1);
        bytes32 provenance = keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_VALIDATION_V1"),
            derivationScope, PROFILE, verifierRuntimeHash, verifierSourceHash, qualificationCommitment,
            walletResult, d.walletContext, intentId, d.bindingCommitment, block.number, parent));
        bindings[intentId] = Binding(raw, pool, token, requester, recipient, scriptHash, requestNonce,
            d, provenance, block.number, parent);
        requestIntents[requestKey] = intentId;
        scriptIntents[scriptHash] = intentId;
        intentIds.push(intentId);
        emit AddressRegistered(intentId, scriptHash, pool, d.bindingCommitment, provenance);
    }

    function validateMint(address pool, MintClaim calldata claim) external view returns (bytes32 scriptHash) {
        _requireAddress(claim.custodyGeneration, claim.derivationGeneration, false);
        Binding storage b = bindings[claim.intentId];
        require(b.pool == pool && b.scriptHash != 0 && b.recipient == claim.recipient
            && b.descriptor.bindingCommitment == claim.bindingCommitment
            && b.validationProvenance == claim.validationProvenance, "address binding");
        _recipient(b.recipient,b.pool,b.token,b.requester,b.requestNonce);
        return b.scriptHash;
    }
}
