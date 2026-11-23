// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./MiningPoolV1.sol";
import "./ConfederationPoolV2.sol";
import "./ConfederationBitcoinReserve.sol";
import {BitcoinCustodyNetwork} from "./BitcoinCustodyNetwork.sol";

/// @notice Burns the federation MP and proves its BTC transfer before issuing the confederation S.
/// @dev Source graphs are immutable, pinned at deployment and on the same EVM chain.
/// The source may refuse the initial payment; it has no authority over issued CS.
contract BitcoinCustodyTransferV2 is ReentrancyGuard {
    using SafeERC20 for IERC20;
    uint256 private constant SAT = 1e10;
    struct Source {
        address escrow;
        address token;
        bytes32 escrowHash;
        bytes32 tokenHash;
        bytes32 miningHash;
        bytes32 depositHash;
        bytes32 batchHash;
    }
    enum State { None, Waiting, Completed, Cancelled }
    struct Transfer {
        address owner;
        address recipient;
        uint256 source;
        uint256 amount;
        uint256 minShares;
        State state;
    }
    ConfederationBitcoinReserve public immutable mining;
    ConfederationPoolV2 public immutable pool;
    bytes public destinationScript;
    bytes32 public immutable bitcoinGenesis;
    Source[] private sources;
    mapping(uint256 => Transfer) public transfers;
    mapping(uint256 => uint256) public quotedShares;
    uint256 public nextId;
    event Requested(uint256 indexed id, uint256 indexed source, address indexed owner,
        address recipient, uint256 amount, uint256 minShares, bytes32 requestKey);
    event Completed(uint256 indexed id, bytes32 indexed txid, uint32 vout, uint256 shares);
    event Cancelled(uint256 indexed id);
    event IssuanceQuoted(uint256 indexed id, uint256 protocolFee, uint256 netShares);

    constructor(address mining_, address pool_, Source[] memory sources_) {
        mining = ConfederationBitcoinReserve(mining_);
        pool = ConfederationPoolV2(pool_);
        require(address(pool.withdrawalEscrow()) == address(mining.escrow())
            && address(pool.frostMultisig()) == address(mining.authorization()), "transfer destination graph");
        destinationScript = mining.payoutScript();
        bitcoinGenesis = BitcoinCustodyNetwork.genesis(address(mining.escrow().spv()));
        require(sources_.length <= 16, "transfer source limit");
        for (uint256 i; i < sources_.length; ++i) {
            _checkSource(sources_[i]);
            BitcoinCustodyNetwork.requireMatch(address(BitcoinWithdrawalEscrow(sources_[i].escrow).spv()), bitcoinGenesis);
            for (uint256 j; j < i; ++j)
                require(sources_[i].token != sources_[j].token, "duplicate transfer source");
            sources.push(sources_[i]);
        }
    }

    function sourceCount() external view returns (uint256) { return sources.length; }
    function sourceAt(uint256 i) external view returns (Source memory) { return sources[i]; }
    function _code(address target, bytes32 expected) private view {
        require(target.code.length > 0 && expected != 0 && target.codehash == expected, "transfer source code");
    }
    function _checkSource(Source memory s) private view {
        _code(s.escrow, s.escrowHash);
        _code(s.token, s.tokenHash);
        BitcoinWithdrawalEscrow escrow = BitcoinWithdrawalEscrow(s.escrow);
        MiningPoolV1 origin = MiningPoolV1(PoolMpToken(s.token).poolDAO());
        _code(address(origin), s.miningHash);
        _code(address(origin.deposits()), s.depositHash);
        _code(address(escrow.batches()), s.batchHash);
        require(address(origin.mpToken()) == s.token && address(origin.escrow()) == s.escrow
            && address(origin.deposits()) == address(escrow.depositRegistry())
            && origin.deposits().spendRecorder() == s.escrow
            && escrow.batches().withdrawalEscrow() == s.escrow
            && origin.deposits().poolScripts(address(origin)) == keccak256(origin.payoutScript())
            && address(origin.deposits()) != address(mining.deposits())
            && keccak256(origin.payoutScript()) != keccak256(destinationScript), "transfer source graph");
    }

    /// @notice A source spend cannot bypass the handover as an ordinary fresh deposit.
    function isSourceFunding(bytes calldata raw) external view returns (bool) {
        bytes32 txid = BitcoinTxParser.txId(raw);
        (bytes32[] memory prev, uint32[] memory vouts) = BitcoinTxParser.parseAllInputs(raw);
        for (uint256 i; i < sources.length; ++i) {
            Source memory s = sources[i];
            _checkSource(s);
            BitcoinDepositRegistry registry = MiningPoolV1(PoolMpToken(s.token).poolDAO()).deposits();
            if (registry.custodyTransactions(txid)) return true;
            for (uint256 j; j < prev.length; ++j) {
                bytes32 key = keccak256(abi.encode(prev[j], vouts[j]));
                if (registry.consumed(key) || registry.walletOutput(key) || registry.backingSats(key) != 0) return true;
            }
        }
        return false;
    }

    function request(uint256 source, uint256 amount, uint256 minShares, address recipient)
        external nonReentrant returns (uint256 id)
    {
        require(source < sources.length && amount > 0 && amount % SAT == 0
            && minShares > 0 && minShares <= amount && recipient != address(0), "transfer request");
        require(mining.custodyTransfer() == address(this) && mining.escrow().localCustodyPools(address(pool)), "transfer not bound");
        Source memory s = sources[source];
        _checkSource(s);
        id = ++nextId;
        uint256 net = mining.lockCustodyQuote(id, msg.sender, recipient, amount, minShares);
        quotedShares[id] = net;
        transfers[id] = Transfer(msg.sender, recipient, source, amount, minShares, State.Waiting);
        IERC20 token = IERC20(s.token);
        uint256 beforeBalance = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), amount);
        require(token.balanceOf(address(this)) == beforeBalance + amount, "transfer amount");
        token.forceApprove(s.escrow, amount);
        BitcoinWithdrawalEscrow(s.escrow).requestDirect(id, s.token, amount, destinationScript);
        token.forceApprove(s.escrow, 0);
        emit Requested(id, source, msg.sender, recipient, amount, minShares,
            BitcoinWithdrawalEscrow(s.escrow).requestKey(address(this), id));
        emit IssuanceQuoted(id, amount - net, net);
    }

    function setMinimumShares(uint256 id, uint256 minimum) external {
        Transfer storage t = transfers[id];
        require(t.owner == msg.sender && t.state == State.Waiting && minimum > 0 && minimum <= quotedShares[id],
            "transfer owner/state");
        t.minShares = minimum;
    }

    function cancel(uint256 id) external nonReentrant {
        Transfer storage t = transfers[id];
        require(t.owner == msg.sender && t.state == State.Waiting, "transfer owner/state");
        Source memory s = sources[t.source];
        _checkSource(s);
        // Source escrow refuses refunds once signing may have begun, even on timeout.
        BitcoinWithdrawalEscrow(s.escrow).refund(BitcoinWithdrawalEscrow(s.escrow).requestKey(address(this), id));
        t.state = State.Cancelled;
        mining.cancelCustodyQuote(id);
        IERC20(s.token).safeTransfer(t.owner, t.amount);
        emit Cancelled(id);
    }

    /// @notice Permissionless completion, including a complete aggregated output.
    /// @dev All ids must belong to the same source and paid transaction. RBF
    /// winners are read by their actual batch, never by the request's original batch.
    function finalize(uint256[] calldata ids, bytes32 paidBatch, bytes calldata raw, uint32 vout,
        bytes32 blockHash, bytes32[] calldata siblings, uint256 index) external nonReentrant
    {
        require(ids.length > 0 && ids.length <= 32, "transfer ids");
        Transfer memory first = transfers[ids[0]];
        require(first.state == State.Waiting, "transfer state");
        Source memory s = sources[first.source];
        _checkSource(s);
        BitcoinWithdrawalEscrow escrow = BitcoinWithdrawalEscrow(s.escrow);
        BitcoinWithdrawalEscrow.Payment memory paid = escrow.payment(paidBatch);
        bytes32 txid = BitcoinTxParser.txId(raw);
        require(paid.settled && paid.txid == txid, "source payment not settled");
        uint256 amount;
        for (uint256 i; i < ids.length; ++i) {
            Transfer storage t = transfers[ids[i]];
            require(t.state == State.Waiting && t.source == first.source, "transfer state/source");
            bytes32 key = escrow.requestKey(address(this), ids[i]);
            BitcoinWithdrawalEscrow.Request memory r = escrow.getRequest(key);
            bool member;
            for (uint256 j; j < paid.requests.length; ++j) if (paid.requests[j] == key) member = true;
            require(member && r.state == BitcoinWithdrawalEscrow.State.Paid && r.pool == address(0)
                && r.owner == address(this) && r.localId == ids[i] && r.token == s.token && r.amount == t.amount
                && keccak256(r.script) == keccak256(destinationScript), "source receipt mismatch");
            t.state = State.Completed; // Duplicate ids fail; any later failure reverts the whole handover.
            amount += t.amount;
        }
        require(amount / SAT <= type(uint64).max, "transfer range");
        bytes32 claim = keccak256(abi.encode(address(this), ids, paidBatch, txid, vout));
        address[] memory recipients = new address[](ids.length);
        uint256[] memory amounts = new uint256[](ids.length);
        for (uint256 i; i < ids.length; ++i) {
            Transfer storage t = transfers[ids[i]];
            recipients[i] = t.recipient;
            amounts[i] = t.amount;
        }
        uint256[] memory shares = mining.mintFromCustodyTransfer(raw, vout, claim, blockHash, siblings, index,
            ids, recipients, amounts);
        for (uint256 i; i < ids.length; ++i) {
            require(shares[i] == quotedShares[ids[i]], "transfer quote mismatch");
            emit Completed(ids[i], txid, vout, shares[i]);
        }
    }
}
