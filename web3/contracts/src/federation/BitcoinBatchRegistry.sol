// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "../FROSTAuthorization.sol";
import "./DKGCoordinator.sol";
import "./IBitcoinFeeReserve.sol";

interface IBatchFROSTRegistry {
    function dkgRegistry() external view returns (address);
}

interface IBatchEscrowBinding {
    function batches() external view returns (address);
    function isBound(bytes32 batch) external view returns (bool);
    function depositRegistry() external view returns (address);
}

/// @notice Authorization and reservation boundary for BTC signing. This is NOT payment settlement.
/// @dev UTXOs remain locked after signing starts; no timeout refund is possible here.
contract BitcoinBatchRegistry {
    enum State {
        None,
        Reserved,
        Signing,
        Cancelled
    }

    struct Batch {
        bytes32 planHash;
        bytes32 walletResult;
        uint64 deadline;
        State state;
    }
    FROSTAuthorization public immutable authorization;
    DKGCoordinator public immutable dkg;
    bytes32 public immutable walletId;
    bytes32 public immutable walletResult;
    uint256 public nonce;
    address public withdrawalEscrow;
    address public feeReserve;
    mapping(bytes32 => Batch) public batches;
    mapping(bytes32 => bytes32) public reservationRoot;
    mapping(bytes32 => bytes32) public replacementOf;
    mapping(bytes32 => uint256) public versionCount;
    event ReplacementReserved(bytes32 indexed root, bytes32 indexed previous, bytes32 indexed replacement);
    mapping(bytes32 => bytes32) public reservations;
    mapping(bytes32 => bytes32[]) private inputs;
    event Reserved(bytes32 indexed id, bytes32 planHash);
    event SigningStarted(bytes32 indexed id);
    event Cancelled(bytes32 indexed id);

    constructor(address auth, address registry, bytes32 id) {
        authorization = FROSTAuthorization(auth);
        require(IBatchFROSTRegistry(address(authorization.coordinator())).dkgRegistry() == registry, "DKG registry mismatch");
        dkg = DKGCoordinator(registry);
        walletId = id;
        (, bytes32 result,, bytes32 purpose,, address[] memory roster, uint256 threshold) = dkg.getCompletedResult(id);
        require(purpose == keccak256("BTC_SPEND"), "wallet purpose");
        require(
            keccak256(abi.encode(roster)) == keccak256(abi.encode(authorization.getParticipants()))
                && threshold == authorization.threshold(),
            "wallet roster"
        );
        walletResult = result;
    }

    function getSetWithdrawalEscrowOperationHash(address escrow) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.setWithdrawalEscrow.selector, keccak256(abi.encode(escrow)), nonce
        );
    }

    function getSetFeeReserveOperationHash(address reserve_) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.setFeeReserve.selector, keccak256(abi.encode(reserve_)), nonce);
    }
    /// @notice New graph only, before any BTC batch/authorization reservation.
    function setFeeReserve(address reserve_, bytes32 op) external {
        require(feeReserve == address(0) && nonce == 1 && withdrawalEscrow != address(0), "fee reserve fixed");
        require(IBitcoinFeeReserve(reserve_).batches() == address(this)
            && IBitcoinFeeReserve(reserve_).deposits() == IBatchEscrowBinding(withdrawalEscrow).depositRegistry(), "fee reserve graph");
        require(op == getSetFeeReserveOperationHash(reserve_), "fee reserve hash");
        authorization.consumeOperation(op); ++nonce; feeReserve = reserve_;
    }

    function setWithdrawalEscrow(address escrow, bytes32 op) external {
        require(
            withdrawalEscrow == address(0) && IBatchEscrowBinding(escrow).batches() == address(this), "escrow binding"
        );
        require(op == getSetWithdrawalEscrowOperationHash(escrow), "hash");
        authorization.consumeOperation(op);
        nonce++;
        withdrawalEscrow = escrow;
    }

    function operationHash(bytes32 id, bytes32 planHash, bytes32[] calldata outpoints, uint64 deadline)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this),
            this.reserve.selector,
            keccak256(abi.encode(id, planHash, outpoints, deadline, walletResult)),
            nonce
        );
    }

    function reserve(bytes32 id, bytes32 planHash, bytes32[] calldata outpoints, uint64 deadline, bytes32 approvedHash)
        external
    {
        require(id != 0 && planHash != 0 && batches[id].state == State.None, "invalid batch");
        require(outpoints.length > 0 && outpoints.length <= 16 && deadline > block.timestamp, "limits");
        require(approvedHash == operationHash(id, planHash, outpoints, deadline), "operation hash");
        authorization.consumeOperation(approvedHash);
        nonce++;
        if (feeReserve != address(0)) IBitcoinFeeReserve(feeReserve).onReserve(id, planHash, outpoints);
        for (uint256 i; i < outpoints.length; ++i) {
            require(outpoints[i] != 0 && reservations[outpoints[i]] == 0, "outpoint reserved");
            reservations[outpoints[i]] = id;
            inputs[id].push(outpoints[i]);
        }
        reservationRoot[id] = id;
        versionCount[id] = 1;
        batches[id] = Batch(planHash, walletResult, deadline, State.Reserved);
        emit Reserved(id, planHash);
    }

    function getReplacementOperationHash(bytes32 previous, bytes32 id, bytes32 planHash, uint64 deadline)
        public view returns (bytes32)
    {
        return authorization.hashOperation(address(this), this.reserveReplacement.selector,
            keccak256(abi.encode(previous, id, planHash, deadline, walletResult)), nonce);
    }

    /// @notice Every version spends the same complete input set. Old signed versions remain payable.
    function reserveReplacement(bytes32 previous, bytes32 id, bytes32 planHash, uint64 deadline, bytes32 op) external {
        require(batches[previous].state == State.Signing && id != 0 && batches[id].state == State.None, "replacement state");
        require(planHash != 0 && deadline > block.timestamp, "replacement limits");
        if (withdrawalEscrow != address(0)) require(IBatchEscrowBinding(withdrawalEscrow).isBound(previous), "payment already resolved");
        bytes32 root = reservationRoot[previous];
        require(versionCount[root] < 16, "version limit");
        require(op == getReplacementOperationHash(previous, id, planHash, deadline), "operation hash");
        authorization.consumeOperation(op);
        nonce++;
        if (feeReserve != address(0)) IBitcoinFeeReserve(feeReserve).onReserve(id, planHash, inputs[previous]);
        reservationRoot[id] = root;
        replacementOf[id] = previous;
        versionCount[root]++;
        for (uint256 i; i < inputs[previous].length; ++i) {
            bytes32 input = inputs[previous][i];
            require(reservations[input] == root, "reservation lost");
            inputs[id].push(input);
        }
        batches[id] = Batch(planHash, walletResult, deadline, State.Reserved);
        emit ReplacementReserved(root, previous, id);
    }

    function getInputs(bytes32 id) external view returns (bytes32[] memory) {
        return inputs[id];
    }

    function batchState(bytes32 id) external view returns (State) {
        return batches[id].state;
    }

    function beginSigning(bytes32 id) external {
        require(dkg.isSessionParticipant(walletId, msg.sender), "wallet participant");
        if (feeReserve != address(0)) IBitcoinFeeReserve(feeReserve).checkSigning(id);
        Batch storage b = batches[id];
        if (b.state == State.Signing) return;
        require(b.state == State.Reserved && block.timestamp <= b.deadline, "batch unavailable");
        if (withdrawalEscrow != address(0)) {
            require(IBatchEscrowBinding(withdrawalEscrow).isBound(id), "payment not bound");
        }
        b.state = State.Signing;
        emit SigningStarted(id);
    }

    function cancel(bytes32 id, bytes32 approvedHash) external {
        require(batches[id].state == State.Reserved && replacementOf[id] == 0, "signature may exist");
        require(
            approvedHash
                == authorization.hashOperation(address(this), this.cancel.selector, keccak256(abi.encode(id)), nonce),
            "operation hash"
        );
        authorization.consumeOperation(approvedHash);
        nonce++;
        batches[id].state = State.Cancelled;
        for (uint256 i; i < inputs[id].length; ++i) {
            delete reservations[inputs[id][i]];
        }
        emit Cancelled(id);
    }
}
