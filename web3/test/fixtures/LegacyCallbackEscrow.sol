// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
// TEST ONLY: frozen baseline 733831e, class/import paths renamed.
// Reproduces unauthenticated callback settlement denial with real MP collateral.
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "../../contracts/src/federation/BitcoinBatchRegistry.sol";
import "../../contracts/src/core/BitcoinTxParser.sol";
import "../../contracts/src/tokens/PoolMpToken.sol";

interface IPaymentSPV {
    function checkTxInclusion(bytes32 blockHash, bytes32 txid, bytes32[] calldata siblings, uint256 index)
        external
        view
        returns (bool);
    function getBlockStatus(bytes32 blockHash) external view returns (bool, uint64);
    function testHelpersEnabled() external view returns (bool);
}

interface IDepositSpendRecorder {
    function recordSpend(bytes calldata raw, uint256 burnedSats) external;
    function spendRecorder() external view returns (address);
    function poolScripts(address pool) external view returns (bytes32);
}

interface IWithdrawalPool {
    function settleWithdrawal(uint256 id) external;
    function withdrawalPrepared(uint256 id) external view returns (bool);
    function getFrostMultisig() external view returns (address);
    function withdrawalEscrow() external view returns (address);
}

interface IRegisteredMiningPool {
    function mpToken() external view returns (address);
}

/// @notice MP collateral is destroyed only after a depth-checked exact Bitcoin payment.
/// @dev MP units are 10^10 per satoshi. Miner fees are paid by the wallet treasury,
/// independently of user principal. No refund after signing without a proven invalidation.
contract LegacyCallbackEscrow is ReentrancyGuard {
    using SafeERC20 for IERC20;
    uint256 public constant SAT = 1e10;
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
    IDepositSpendRecorder public depositRegistry;
    mapping(bytes32 => Request) public requests;
    mapping(bytes32 => Payment) private payments;
    mapping(bytes32 => bool) public spentInputs;
    event Escrowed(bytes32 indexed id, address indexed token, uint256 amount);
    event TreasuryRequested(bytes32 indexed id, address indexed owner, uint64 satoshis, bytes script);
    event PaymentBound(bytes32 indexed batch, bytes32 txid);
    event PaymentSettled(bytes32 indexed batch, bytes32 txid, bytes32 blockHash);
    event Refunded(bytes32 indexed id);

    constructor(
        address registry,
        address relay,
        uint32 confirmations,
        uint256 minP2WPKHSats_,
        uint256 minP2TRSats_
    ) {
        require(registry.code.length > 0 && relay.code.length > 0 && confirmations >= 6, "config");
        require(minP2WPKHSats_ > 0 && minP2TRSats_ > 0, "withdrawal minimums");
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

    function _lock(
        bytes32 key,
        address pool,
        uint256 localId,
        address token,
        address owner,
        uint256 amount,
        bytes calldata script
    ) private {
        address miningPool = PoolMpToken(token).poolDAO();
        require(
            address(depositRegistry) != address(0) && depositRegistry.poolScripts(miningPool) != 0
                && IRegisteredMiningPool(miningPool).mpToken() == token,
            "unregistered backing token"
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
        require(IWithdrawalPool(msg.sender).getFrostMultisig() == address(batches.authorization()), "pool auth");
        require(IWithdrawalPool(msg.sender).withdrawalEscrow() == address(this), "pool binding");
        _lock(requestKey(msg.sender, id), msg.sender, id, token, owner, amount, script);
    }

    function requestDirect(uint256 id, address token, uint256 amount, bytes calldata script) external nonReentrant {
        _lock(requestKey(msg.sender, id), address(0), id, token, msg.sender, amount, script);
    }

    function _validateOutput(uint256 sats, bytes calldata script) private view {
        bool isP2WPKH = script.length == 22 && script[0] == 0x00 && script[1] == 0x14;
        require(isP2WPKH || (script.length == 34 && script[0] == 0x51 && script[1] == 0x20), "script");
        require(sats >= (isP2WPKH ? minP2WPKHSats : minP2TRSats), "below withdrawal minimum");
    }

    function getTreasuryOperationHash(uint256 id, address owner, uint64 sats, bytes calldata script)
        public view returns (bytes32)
    {
        return batches.authorization().hashOperation(address(this), this.requestTreasury.selector,
            keccak256(abi.encode(id, owner, sats, script)), nonce);
    }

    /// @notice Explicit threshold-approved use of surplus BTC, with no MP burn.
    /// @dev The normal bind/sign/settle path applies. recordSpend preserves ALL input backing.
    function requestTreasury(uint256 id, address owner, uint64 sats, bytes calldata script, bytes32 op) external {
        bytes32 key = requestKey(address(this), id);
        require(owner != address(0) && requests[key].state == State.None, "owner or duplicate");
        _validateOutput(sats, script);
        require(op == getTreasuryOperationHash(id, owner, sats, script), "hash");
        batches.authorization().consumeOperation(op);
        nonce++;
        requests[key] = Request(address(0), id, address(0), owner, uint256(sats) * SAT, script, 0, State.Pending);
        emit TreasuryRequested(key, owner, sats, script);
    }

    function getBindOperationHash(bytes32 batch, bytes32 txid, bytes32[] calldata keys) public view returns (bytes32) {
        return batches.authorization()
            .hashOperation(address(this), this.bind.selector, keccak256(abi.encode(batch, txid, keys)), nonce);
    }

    function bind(bytes32 batch, bytes32 txid, bytes32[] calldata keys, bytes32 op) external {
        require(batches.replacementOf(batch) == 0, "replacement requires inherited requests");
        require(batches.batchState(batch) == BitcoinBatchRegistry.State.Reserved && txid != 0, "batch");
        require(payments[batch].txid == 0 && keys.length > 0 && keys.length <= 32, "payment");
        require(op == getBindOperationHash(batch, txid, keys), "hash");
        batches.authorization().consumeOperation(op);
        nonce++;
        Payment storage p = payments[batch];
        p.txid = txid;
        for (uint256 i; i < keys.length; i++) {
            Request storage r = requests[keys[i]];
            require(r.state == State.Pending, "request");
            if (r.pool != address(0)) require(IWithdrawalPool(r.pool).withdrawalPrepared(r.localId), "not approved");
            r.state = State.Bound;
            r.batch = batch;
            p.requests.push(keys[i]);
        }
        emit PaymentBound(batch, txid);
    }

    function refund(bytes32 key) external nonReentrant {
        Request storage r = requests[key];
        require(msg.sender == (r.pool == address(0) ? r.owner : r.pool), "owner");
        require(
            r.state == State.Pending
                || (r.state == State.Bound && batches.batchState(r.batch) == BitcoinBatchRegistry.State.Cancelled),
            "signature may exist"
        );
        r.state = State.Refunded;
        // A treasury request locks no token; cancelling it transfers nothing.
        if (r.token != address(0)) IERC20(r.token).safeTransfer(msg.sender, r.amount);
        emit Refunded(key);
    }

    function getBindReplacementOperationHash(bytes32 previous, bytes32 batch, bytes32 txid) public view returns (bytes32) {
        return batches.authorization().hashOperation(address(this), this.bindReplacement.selector,
            keccak256(abi.encode(previous, batch, txid)), nonce);
    }

    /// @notice Keep original requests locked; any approved conflicting version may prove payment.
    function bindReplacement(bytes32 previous, bytes32 batch, bytes32 txid, bytes32 op) external {
        require(batches.replacementOf(batch) == previous && batches.batchState(batch) == BitcoinBatchRegistry.State.Reserved, "replacement");
        Payment storage old = payments[previous];
        Payment storage next = payments[batch];
        require(old.txid != 0 && !old.settled && next.txid == 0 && txid != 0 && txid != old.txid, "payment");
        require(op == getBindReplacementOperationHash(previous, batch, txid), "hash");
        batches.authorization().consumeOperation(op);
        nonce++;
        next.txid = txid;
        for (uint256 i; i < old.requests.length; ++i) {
            require(requests[old.requests[i]].state == State.Bound, "request state");
            next.requests.push(old.requests[i]);
        }
        emit PaymentBound(batch, txid);
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
        // Aggregate obligations sharing a script: a Bitcoin output can never pay two requests twice.
        for (uint256 i; i < p.requests.length; i++) {
            Request storage r = requests[p.requests[i]];
            uint256 due;
            for (uint256 j; j < p.requests.length; j++) {
                Request storage q = requests[p.requests[j]];
                if (keccak256(q.script) == keccak256(r.script)) due += q.amount / SAT;
            }
            require(BitcoinTxParser.sumOutputsToScript(raw, r.script) >= due, "underpayment");
        }
        require(address(depositRegistry) != address(0), "deposit registry missing");
        uint256 burnedSats;
        for (uint256 i; i < p.requests.length; i++) {
            Request storage r = requests[p.requests[i]];
            if (r.token != address(0)) burnedSats += r.amount / SAT;
        }
        depositRegistry.recordSpend(raw, burnedSats);
        p.settled = true;
        p.blockHash = blockHash;
        for (uint256 i; i < p.requests.length; i++) {
            Request storage r = requests[p.requests[i]];
            require(r.state == State.Bound, "request state");
            r.state = State.Paid;
            if (r.token != address(0)) PoolMpToken(r.token).burnHeld(r.amount);
            if (r.pool != address(0)) IWithdrawalPool(r.pool).settleWithdrawal(r.localId);
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
}
