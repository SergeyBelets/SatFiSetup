// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./BitcoinBatchRegistry.sol";
import "../core/BitcoinTxParser.sol";
import "../tokens/ConfederationShareV2.sol";
import "./CallbackPoolRegistry.sol";

import {IPaymentSPV, IDepositSpendRecorder, IWithdrawalPool, ICustodyDepositPool}
    from "./BitcoinWithdrawalEscrow.sol";

interface IWithdrawalFeeLimit {
    function minerFeeLimitSats(uint256 id) external view returns (uint64);
    function shareToken() external view returns (address);
    function bitcoinReserve() external view returns (address);
}

interface IConfederationReserveBinding {
    function escrow() external view returns (address);
    function deposits() external view returns (address);
    function shareToken() external view returns (address);
}

interface IWithdrawalInputValues {
    struct OutputValue { uint64 sats; bytes script; bool known; }
    function getValue(bytes32 key) external view returns (OutputValue memory);
    function backingSats(bytes32 key) external view returns (uint256);
    function scriptPools(bytes32 scriptHash) external view returns (address);
}

/// @notice Pays ordinary S withdrawals from their gross locked backing including miner fees.
contract BitcoinWithdrawalEscrowV2 is ReentrancyGuard {
    using SafeERC20 for IERC20;
    uint256 public constant SAT = 1e10;
    uint64 public immutable defaultMinerFeeLimitSats;

    mapping(bytes32 => uint256) public minerFee;

    error InvalidFeePlan();
    enum State {
        None,
        Pending,
        Bound,
        Paid,
        Refunded
    }

    struct Request {
        address pool;
        uint256 localId;
        address token;
        address owner;
        uint256 amount;
        bytes script;
        bytes32 batch;
        State state;
    }

    struct Payment {
        bytes32 txid;
        bytes32 blockHash;
        bool settled;
        bytes32[] requests;
    }
    BitcoinBatchRegistry public immutable batches;
    IPaymentSPV public immutable spv;
    uint32 public immutable depth;
    uint256 public immutable minP2WPKHSats;
    uint256 public immutable minP2TRSats;
    uint256 public nonce;
    mapping(address => bool) public localCustodyPools;
    event LocalCustodyRequired(address indexed pool);

    function getRequireLocalCustodyOperationHash(address pool) public view returns (bytes32) {
        return batches.authorization().hashOperation(address(this), this.requireLocalCustody.selector,
            keccak256(abi.encode(pool)), nonce);
    }

    /// @notice Irreversible before the pool accepts its first asset. The monetary
    /// authorization in this graph is the custodian-only EVM epoch.
    function requireLocalCustody(address pool, bytes32 op) external {
        ICustodyDepositPool candidate = ICustodyDepositPool(pool);
        require(!localCustodyPools[pool] && candidate.withdrawalEscrow() == address(this)
            && candidate.frostMultisig() == address(batches.authorization())
            && candidate.getPoolCount() == 0, "custody policy fixed");
        require(op == getRequireLocalCustodyOperationHash(pool), "hash");
        batches.authorization().consumeOperation(op);
        ++nonce;
        localCustodyPools[pool] = true;
        emit LocalCustodyRequired(pool);
    }

    IDepositSpendRecorder public depositRegistry;
    CallbackPoolRegistry public callbackPoolRegistry;
    // BitcoinDepositRegistry also uses this accessor for legacy fee credits.
    address public constant withdrawalFees = address(0);
    mapping(bytes32 => Request) public requests;
    mapping(bytes32 => Payment) private payments;
    mapping(bytes32 => bool) public spentInputs;
    event Escrowed(bytes32 indexed id, address indexed token, uint256 amount);
    event PaymentBound(bytes32 indexed batch, bytes32 txid);
    event PaymentSettled(bytes32 indexed batch, bytes32 txid, bytes32 blockHash);
    event Refunded(bytes32 indexed id);
    event CallbackPoolRegistrySet(address indexed registry);

    constructor(
        address registry,
        address relay,
        uint32 confirmations,
        uint256 minP2WPKHSats_,
        uint256 minP2TRSats_,
        uint64 defaultMinerFeeLimitSats_
    ) {
        if (defaultMinerFeeLimitSats_ == 0) revert InvalidFeePlan();
        defaultMinerFeeLimitSats = defaultMinerFeeLimitSats_;
        require(registry.code.length > 0 && relay.code.length > 0 && confirmations >= 6, "config");
        require(minP2WPKHSats_ >= 294 && minP2TRSats_ >= 330, "withdrawal minimums");
        batches = BitcoinBatchRegistry(registry);
        spv = IPaymentSPV(relay);
        depth = confirmations;
        minP2WPKHSats = minP2WPKHSats_;
        minP2TRSats = minP2TRSats_;
        require(!spv.testHelpersEnabled(), "unsafe SPV");
    }

    function getSetDepositRegistryOperationHash(address registry) public view returns (bytes32) {
        return batches.authorization()
            .hashOperation(address(this), this.setDepositRegistry.selector, keccak256(abi.encode(registry)), nonce);
    }

    function setDepositRegistry(address registry, bytes32 op) external {
        require(
            address(depositRegistry) == address(0) && IDepositSpendRecorder(registry).spendRecorder() == address(this),
            "registry binding"
        );
        require(op == getSetDepositRegistryOperationHash(registry), "hash");
        batches.authorization().consumeOperation(op);
        nonce++;
        depositRegistry = IDepositSpendRecorder(registry);
    }

    function requestKey(address pool, uint256 id) public pure returns (bytes32) {
        return keccak256(abi.encode(pool, id));
    }

    function getSetCallbackPoolRegistryOperationHash(address registry) public view returns (bytes32) {
        return batches.authorization().hashOperation(address(this), this.setCallbackPoolRegistry.selector,
            keccak256(abi.encode(registry)), nonce);
    }

    /// @notice One-time bootstrap binding under the existing monetary authority.
    function setCallbackPoolRegistry(address registry, bytes32 op) external {
        require(address(callbackPoolRegistry) == address(0) && registry.code.length > 0, "callback registry fixed");
        CallbackPoolRegistry candidate = CallbackPoolRegistry(registry);
        require(candidate.escrow() == address(this) && candidate.authorization() == address(batches.authorization()),
            "callback registry binding");
        require(op == getSetCallbackPoolRegistryOperationHash(registry), "hash");
        batches.authorization().consumeOperation(op);
        nonce++;
        callbackPoolRegistry = candidate;
        emit CallbackPoolRegistrySet(registry);
    }

    function isCallbackPoolAdmitted(address pool) external view returns (bool) {
        return address(callbackPoolRegistry) != address(0) && callbackPoolRegistry.isAdmitted(pool);
    }

    function _checkCallback(address pool, uint256 id, address token, address owner, uint256 amount,
        bytes memory script, bool prepared) private view {
        require(address(callbackPoolRegistry) != address(0), "callback registry missing");
        callbackPoolRegistry.validateWithdrawal(pool, id, token, owner, amount, script, prepared);
    }

    function _lock(
        bytes32 key,
        address pool,
        uint256 localId,
        address token,
        address owner,
        uint256 amount,
        bytes calldata script
    ) private {
        address reserve = IWithdrawalFeeLimit(pool).bitcoinReserve();
        require(
            address(depositRegistry) != address(0) && depositRegistry.poolScripts(reserve) != 0
                && IWithdrawalFeeLimit(pool).shareToken() == token && localCustodyPools[pool]
                && IConfederationReserveBinding(reserve).escrow() == address(this)
                && IConfederationReserveBinding(reserve).deposits() == address(depositRegistry)
                && IConfederationReserveBinding(reserve).shareToken() == token,
            "unregistered S reserve"
        );
        require(requests[key].state == State.None && amount > 0 && amount % SAT == 0, "amount or duplicate");
        _validateOutput(amount / SAT, script);
        uint256 beforeBalance = IERC20(token).balanceOf(address(this));
        IERC20(token).safeTransferFrom(msg.sender, address(this), amount);
        require(IERC20(token).balanceOf(address(this)) == beforeBalance + amount, "transfer");
        requests[key] = Request(pool, localId, token, owner, amount, script, 0, State.Pending);
        emit Escrowed(key, token, amount);
    }

    function lock(uint256 id, address token, address owner, uint256 amount, bytes calldata script)
        external
        nonReentrant
    {
        _checkCallback(msg.sender, id, token, owner, amount, script, false);
        _lock(requestKey(msg.sender, id), msg.sender, id, token, owner, amount, script);
    }

    function _validateOutput(uint256 sats, bytes memory script) private view {
        bool isP2WPKH = script.length == 22 && script[0] == 0x00 && script[1] == 0x14;
        require(isP2WPKH || (script.length == 34 && script[0] == 0x51 && script[1] == 0x20), "script");
        require(sats >= (isP2WPKH ? minP2WPKHSats : minP2TRSats), "below withdrawal minimum");
    }

    function refund(bytes32 key) external nonReentrant {
        Request storage r = requests[key];
        require(msg.sender == r.pool, "owner");
        require(
            r.state == State.Pending
                || (r.state == State.Bound && batches.batchState(r.batch) == BitcoinBatchRegistry.State.Cancelled),
            "signature may exist"
        );
        r.state = State.Refunded;
        IERC20(r.token).safeTransfer(msg.sender, r.amount);
        emit Refunded(key);
    }

    function getBindUserFundedOperationHash(bytes32 batch, bytes32 txid, bytes32[] calldata keys)
        public view returns (bytes32)
    {
        return batches.authorization().hashOperation(address(this), this.bindUserFunded.selector,
            keccak256(abi.encode(batch, txid, keys)), nonce);
    }

    function bindUserFunded(bytes32 batch, bytes calldata raw, bytes32[] calldata keys, bytes32 op) external {
        bytes32 txid = BitcoinTxParser.txId(raw);
        if (batches.replacementOf(batch) != bytes32(0)
            || op != getBindUserFundedOperationHash(batch, txid, keys)) revert InvalidFeePlan();
        batches.authorization().consumeOperation(op);
        ++nonce;
        _bindUserFunded(batch, raw, txid, keys, false);
    }

    function getBindUserFundedReplacementOperationHash(bytes32 previous, bytes32 batch, bytes32 txid)
        public view returns (bytes32)
    {
        return batches.authorization().hashOperation(address(this), this.bindUserFundedReplacement.selector,
            keccak256(abi.encode(previous, batch, txid)), nonce);
    }

    function bindUserFundedReplacement(bytes32 previous, bytes32 batch, bytes calldata raw, bytes32 op) external {
        bytes32 txid = BitcoinTxParser.txId(raw);
        Payment storage old = payments[previous];
        if (old.txid == bytes32(0) || old.settled || txid == old.txid || batches.replacementOf(batch) != previous
            || op != getBindUserFundedReplacementOperationHash(previous, batch, txid)) revert InvalidFeePlan();
        batches.authorization().consumeOperation(op);
        ++nonce;
        _bindUserFunded(batch, raw, txid, old.requests, true);
        if (minerFee[batch] <= minerFee[previous]) revert InvalidFeePlan();
    }

    function _bindUserFunded(bytes32 batch, bytes calldata raw, bytes32 txid, bytes32[] memory keys, bool replacement)
        private
    {
        Payment storage p = payments[batch];
        if (txid == bytes32(0) || p.txid != bytes32(0) || keys.length == 0 || keys.length > 32
            || batches.batchState(batch) != BitcoinBatchRegistry.State.Reserved
            || batches.feeReserve() != address(0)) revert InvalidFeePlan();
        for (uint256 i; i < keys.length; ++i) {
            Request storage r = requests[keys[i]];
            if (r.state != (replacement ? State.Bound : State.Pending) || !localCustodyPools[r.pool]) {
                revert InvalidFeePlan();
            }
            _checkCallback(r.pool, r.localId, r.token, r.owner, r.amount, r.script, true);
            if (!replacement) {
                r.state = State.Bound;
                r.batch = batch;
            }
            p.requests.push(keys[i]);
        }
        uint256 fee = _feeForPrincipalInputs(batch, raw);
        _checkNetPayments(raw, p.requests, fee);
        p.txid = txid;
        minerFee[batch] = fee;
        depositRegistry.recordFamily(txid);
        emit PaymentBound(batch, txid);
    }

    function _feeForPrincipalInputs(bytes32 batch, bytes calldata raw) private view returns (uint256) {
        if (raw.length > 100000) revert InvalidFeePlan();
        (bytes32[] memory prev, uint32[] memory vouts) = BitcoinTxParser.parseAllInputs(raw);
        bytes32[] memory reserved = batches.getInputs(batch);
        IWithdrawalInputValues registry = IWithdrawalInputValues(address(depositRegistry));
        if (prev.length != reserved.length) revert InvalidFeePlan();
        uint256 totalInputs;
        for (uint256 i; i < prev.length; ++i) {
            bytes32 key = keccak256(abi.encode(prev[i], vouts[i]));
            IWithdrawalInputValues.OutputValue memory value = registry.getValue(key);
            if (keccak256(abi.encode(_display(prev[i]), vouts[i])) != reserved[i] || !value.known || value.sats == 0
                || registry.backingSats(key) != value.sats
                || registry.scriptPools(keccak256(value.script)) == address(0)) revert InvalidFeePlan();
            totalInputs += value.sats;
        }
        uint256 outputs = BitcoinTxParser.outputCount(raw);
        if (outputs == 0 || outputs > 32) revert InvalidFeePlan();
        uint256 totalOutputs;
        for (uint256 i; i < outputs; ++i) {
            (uint64 amount,) = BitcoinTxParser.outputAt(raw, i);
            totalOutputs += amount;
        }
        if (totalOutputs > totalInputs) revert InvalidFeePlan();
        return totalInputs - totalOutputs;
    }

    function _checkNetPayments(bytes calldata raw, bytes32[] storage keys, uint256 fee) private view {
        uint256 count = keys.length;
        uint256[] memory net = new uint256[](count);
        bytes32[] memory destinations = new bytes32[](count);
        IWithdrawalInputValues registry = IWithdrawalInputValues(address(depositRegistry));
        for (uint256 i; i < count; ++i) {
            Request storage r = requests[keys[i]];
            destinations[i] = keccak256(r.script);
            if (registry.scriptPools(destinations[i]) != address(0)) revert InvalidFeePlan();
            uint256 share = fee / count + (i < fee % count ? 1 : 0);
            uint256 gross = r.amount / SAT;
            uint64 limit = IWithdrawalFeeLimit(r.pool).minerFeeLimitSats(r.localId);
            if (share > (limit == type(uint64).max ? defaultMinerFeeLimitSats : limit) || share >= gross) revert InvalidFeePlan();
            net[i] = gross - share;
            _validateOutput(net[i], r.script);
        }
        for (uint256 i; i < count; ++i) {
            Request storage r = requests[keys[i]];
            uint256 due;
            for (uint256 j; j < count; ++j) {
                if (destinations[j] == destinations[i]) due += net[j];
            }
            if (BitcoinTxParser.sumOutputsToScript(raw, r.script) != due) revert InvalidFeePlan();
        }
        uint256 outputCount = BitcoinTxParser.outputCount(raw);
        for (uint256 i; i < outputCount; ++i) {
            (uint64 amount, bytes memory script) = BitcoinTxParser.outputAt(raw, i);
            if (amount == 0) continue;
            bytes32 scriptHash = keccak256(script);
            if (registry.scriptPools(scriptHash) != address(0)) continue;
            bool known;
            for (uint256 j; j < count; ++j) {
                if (destinations[j] == scriptHash) {
                    known = true;
                    break;
                }
            }
            if (!known) revert InvalidFeePlan();
        }
    }

    function settle(bytes32 batch, bytes calldata raw, bytes32 blockHash, bytes32[] calldata siblings, uint256 index)
        external
        nonReentrant
    {
        Payment storage p = payments[batch];
        require(!p.settled && p.txid != 0 && batches.batchState(batch) == BitcoinBatchRegistry.State.Signing, "state");
        require(raw.length <= 100000 && siblings.length <= 32 && index >> siblings.length == 0, "proof bounds");
        require(BitcoinTxParser.txId(raw) == p.txid, "transaction");
        (bool canonical, uint64 confirmations) = spv.getBlockStatus(blockHash);
        require(
            canonical && confirmations >= depth && spv.checkTxInclusion(blockHash, p.txid, siblings, index), "SPV proof"
        );
        (bytes32[] memory prev, uint32[] memory vout) = BitcoinTxParser.parseAllInputs(raw);
        bytes32[] memory reserved = batches.getInputs(batch);
        require(prev.length == reserved.length, "input count");
        for (uint256 i; i < prev.length; i++) {
            bytes32 key = keccak256(abi.encode(_display(prev[i]), vout[i]));
            require(key == reserved[i] && !spentInputs[key], "input mismatch or spent");
            spentInputs[key] = true;
        }
        uint256 fee = _feeForPrincipalInputs(batch, raw);
        if (fee != minerFee[batch]) revert InvalidFeePlan();
        _checkNetPayments(raw, p.requests, fee);
        require(address(depositRegistry) != address(0), "deposit registry missing");
        uint256 burnedSats;
        for (uint256 i; i < p.requests.length; i++) {
            Request storage r = requests[p.requests[i]];
            burnedSats += r.amount / SAT;
        }
        depositRegistry.recordSpend(raw, burnedSats);
        p.settled = true;
        p.blockHash = blockHash;
        for (uint256 i; i < p.requests.length; i++) {
            Request storage r = requests[p.requests[i]];
            require(r.state == State.Bound, "request state");
            r.state = State.Paid;
            ConfederationShareV2(r.token).burnHeld(r.amount);
            IWithdrawalPool(r.pool).settleWithdrawal(r.localId);
        }
        emit PaymentSettled(batch, p.txid, blockHash);
    }

    function _display(bytes32 h) private pure returns (bytes32 out) {
        for (uint256 i; i < 32; i++) {
            out |= bytes32(uint256(uint8(h[i])) << (8 * i));
        }
    }

    function isBound(bytes32 batch) external view returns (bool) {
        Payment storage p = payments[batch];
        if (p.txid == 0 || p.settled) return false;
        for (uint256 i; i < p.requests.length; ++i) {
            if (requests[p.requests[i]].state != State.Bound) return false;
        }
        return true;
    }

    function payment(bytes32 batch) external view returns (Payment memory) {
        return payments[batch];
    }
    function getRequest(bytes32 key) external view returns (Request memory) { return requests[key]; }
}
