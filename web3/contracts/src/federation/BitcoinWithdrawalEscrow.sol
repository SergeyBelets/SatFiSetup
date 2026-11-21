// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "./BitcoinBatchRegistry.sol";
import "../core/BitcoinTxParser.sol";
import "../tokens/PoolMpToken.sol";
import "./CallbackPoolRegistry.sol";
import "./WithdrawalFeeTypes.sol";

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
    function recordFamily(bytes32 txid) external;
}

interface IWithdrawalPool {
    function settleWithdrawal(uint256 id) external;
    function withdrawalPrepared(uint256 id) external view returns (bool);
    function getFrostMultisig() external view returns (address);
    function withdrawalEscrow() external view returns (address);
}

interface IRegisteredMiningPool {
    function mpToken() external view returns (address);
    function escrow() external view returns (address);
    function deposits() external view returns (address);
    function payoutScript() external view returns (bytes memory);
    function belongsToCustody(address token, address escrow_, address registry_) external view returns (bool);
}
interface ICustodyDepositPool {
    function getPoolCount() external view returns (uint256);
    function withdrawalEscrow() external view returns (address);
    function frostMultisig() external view returns (address);
}

/// @notice MP collateral is destroyed only after a depth-checked exact Bitcoin payment.
/// @dev MP units are 10^10 per satoshi. Miner fees are paid by the wallet treasury,
/// independently of user principal. No refund after signing without a proven invalidation.
contract BitcoinWithdrawalEscrow is ReentrancyGuard {
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

    function checkDepositToken(address pool, address token) external view {
        if (!localCustodyPools[pool]) return;
        address origin = PoolMpToken(token).poolDAO();
        require(IRegisteredMiningPool(origin).belongsToCustody(token, address(this), address(depositRegistry)),
            "foreign Bitcoin custody");
    }
    IDepositSpendRecorder public depositRegistry;
    CallbackPoolRegistry public callbackPoolRegistry;
    address public withdrawalFees;
    function getSetWithdrawalFeesOperationHash(address credits) public view returns(bytes32) {
        return batches.authorization().hashOperation(address(this),this.setWithdrawalFees.selector,keccak256(abi.encode(credits)),nonce);
    }
    function setWithdrawalFees(address credits,bytes32 op) external {
        require(withdrawalFees == address(0) && batches.nonce() == 2 && batches.feeReserve() != address(0),"fee profile fixed");
        require(IWithdrawalFeeCredits(credits).escrow() == address(this),"fee profile graph");
        require(op == getSetWithdrawalFeesOperationHash(credits),"hash");batches.authorization().consumeOperation(op);++nonce;
        withdrawalFees=credits;
    }
    function createCreditRefund(bytes32 key,address owner,uint64 sats,bytes calldata script) external {
        require(msg.sender == withdrawalFees && msg.sender != address(0) && requests[key].state == State.None,"refund source");
        _validateOutput(sats,script);requests[key]=Request(address(0),0,address(0),owner,uint256(sats)*SAT,script,0,State.Pending);
    }
    function cancelCreditRefund(bytes32 key) external {
        require(msg.sender == withdrawalFees && msg.sender != address(0),"refund source");Request storage r=requests[key];
        require(r.token == address(0) && (r.state == State.Pending || r.state == State.Refunded || r.state == State.Bound
            && batches.batchState(r.batch) == BitcoinBatchRegistry.State.Cancelled),"refund signature may exist");
        r.state=State.Refunded;
    }
    mapping(bytes32 => Request) public requests;
    mapping(bytes32 => Payment) private payments;
    mapping(bytes32 => bool) public spentInputs;
    event Escrowed(bytes32 indexed id, address indexed token, uint256 amount);
    event TreasuryRequested(bytes32 indexed id, address indexed owner, uint64 satoshis, bytes script);
    event PaymentBound(bytes32 indexed batch, bytes32 txid);
    event PaymentSettled(bytes32 indexed batch, bytes32 txid, bytes32 blockHash);
    event Refunded(bytes32 indexed id);
    event CallbackPoolRegistrySet(address indexed registry);

    constructor(
        address registry,
        address relay,
        uint32 confirmations,
        uint256 minP2WPKHSats_,
        uint256 minP2TRSats_
    ) {
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
        _checkCallback(msg.sender, id, token, owner, amount, script, false);
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
        address feeReserve=batches.feeReserve();
        if(feeReserve != address(0)) IBitcoinFeeReserve(feeReserve).checkTreasuryRequest(key,sats);
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
        depositRegistry.recordFamily(txid);
        for (uint256 i; i < keys.length; i++) {
            Request storage r = requests[keys[i]];
            require(r.state == State.Pending, "request");
            if (r.pool != address(0)) _checkCallback(r.pool, r.localId, r.token, r.owner, r.amount, r.script, true);
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
        depositRegistry.recordFamily(txid);
        for (uint256 i; i < old.requests.length; ++i) {
            Request storage r = requests[old.requests[i]];
            require(r.state == State.Bound, "request state");
            if (r.pool != address(0)) _checkCallback(r.pool, r.localId, r.token, r.owner, r.amount, r.script, true);
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
        // Personal plans already committed exact aggregated outputs at both
        // preparation and admission. Their atomic policy settlement below
        // checks this exact txid and request set, including inline returns.
        // Keep the independent aggregation rule for every legacy plan.
        bool personal=withdrawalFees != address(0) && IWithdrawalFeePayments(IWithdrawalFeeCredits(withdrawalFees).payments()).hasPlan(batch);
        if(!personal)for (uint256 i; i < p.requests.length; i++) {
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
        if(withdrawalFees != address(0))burnedSats+=IWithdrawalFeePayments(IWithdrawalFeeCredits(withdrawalFees).payments()).feeBurn(batch);
        depositRegistry.recordSpend(raw, burnedSats);
        p.settled = true;
        p.blockHash = blockHash;
        address feeReserve = batches.feeReserve();
        if (feeReserve != address(0)) IBitcoinFeeReserve(feeReserve).settle(batch, raw, burnedSats, blockHash);
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
    function getRequest(bytes32 key) external view returns (Request memory) { return requests[key]; }
}
