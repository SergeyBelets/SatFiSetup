// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {BitcoinBatchRegistry} from "./BitcoinBatchRegistry.sol";
import {DKGCoordinator} from "./DKGCoordinator.sol";
import "./WithdrawalFeeTypes.sol";
import "./PublicByteParts.sol";
interface IFeeBoardEscrow { function withdrawalFees() external view returns(address); }

/// @notice Immutable transport for PUBLIC materials of an existing BTC signing attempt.
/// @dev Does not create nonces, verify shares, change reservations, or assemble a transaction.
/// Never publish secret nonces, a signing key share, KeyPackage, or local signer state.
contract BitcoinSigningBoard {
    enum PacketKind { Commitment, SignatureShare }

    bytes32 public constant PLAN_DOMAIN = keccak256("SATOHSI_BTC_ONCHAIN_PLAN_V1");
    uint256 public constant MAX_PLAN_BYTES = 16_384;
    uint256 public constant MAX_FEE_PLAN_BYTES = 65_536;
    BitcoinBatchRegistry public immutable batchRegistry;
    DKGCoordinator public immutable dkg;
    bytes32 public immutable walletId;
    bytes32 public immutable walletResult;
    uint256 public immutable maxPacketBytes;
    mapping(bytes32 => bytes) private packets;
    mapping(bytes32 => bytes) private plans;
    struct FeeParts { uint256 total; uint256 size; bytes[] chunks; }
    mapping(bytes32=>mapping(address=>FeeParts)) private feeParts;
    mapping(bytes32=>address) public feePlanPublisher;

    error InvalidBinding();
    error NotWalletParticipant();
    error BatchNotSigning();
    error InputOutOfBounds();
    error InvalidPacketLength();
    error ConflictingRetry();
    error BatchNotPlanPublishable();
    error InvalidPlanLength();
    error PlanHashMismatch();
    error InvalidPacketBatch();

    event PlanPublished(bytes32 indexed batchId, address indexed publisher, bytes32 planHash);

    event PacketPublished(
        bytes32 indexed batchId,
        uint32 indexed inputIndex,
        address indexed sender,
        PacketKind kind,
        bytes32 payloadHash
    );

    constructor(address registry, uint256 maximumPacketBytes) {
        if (registry.code.length == 0) revert InvalidBinding();
        if (maximumPacketBytes == 0) revert InvalidPacketLength();
        BitcoinBatchRegistry batches_ = BitcoinBatchRegistry(registry);
        DKGCoordinator dkg_ = batches_.dkg();
        if (address(dkg_).code.length == 0) revert InvalidBinding();
        bytes32 id = batches_.walletId();
        bytes32 result = batches_.walletResult();
        (, bytes32 completedResult,, bytes32 purpose,,,) = dkg_.getCompletedResult(id);
        if (id == bytes32(0) || result == bytes32(0) || result != completedResult
            || purpose != keccak256("BTC_SPEND")) revert InvalidBinding();
        batchRegistry = batches_;
        dkg = dkg_;
        walletId = id;
        walletResult = result;
        maxPacketBytes = maximumPacketBytes;
    }

    /// @notice Domain-separated reservation commitment to the exact public signing plan bytes.
    /// @dev Client validation must verify the plan structure, transaction and immutable signer subset.
    function getPlanHash(bytes32 batchId, bytes calldata payload) public view returns (bytes32) {
        return keccak256(abi.encode(
            PLAN_DOMAIN, block.chainid, address(this), walletResult, batchId, keccak256(payload)
        ));
    }

    function plan(bytes32 batchId) public view returns (bytes memory) {
        address publisher=feePlanPublisher[batchId];return publisher == address(0) ? plans[batchId] : feePlanPartial(batchId,publisher);
    }
    function feePlanPartial(bytes32 id,address publisher) public view returns(bytes memory raw) {
        FeeParts storage p=feeParts[id][publisher];return PublicByteParts.join(p.chunks,p.size);
    }
    function _feeContext(bytes32 id) private view {
        address c=IFeeBoardEscrow(batchRegistry.withdrawalEscrow()).withdrawalFees();
        if(c == address(0) || !IWithdrawalFeePayments(IWithdrawalFeeCredits(c).payments()).hasPlan(id))revert InvalidBinding();
    }
    function appendFeePlan(bytes32 id,uint256 total,uint256 index,bytes calldata chunk) external {
        if(!dkg.isSessionParticipant(walletId,msg.sender))revert NotWalletParticipant();
        FeeParts storage p=feeParts[id][msg.sender];
        if(index < p.chunks.length) { if(p.total != total || keccak256(p.chunks[index]) != keccak256(chunk))revert ConflictingRetry();return; }
        _feeContext(id);BitcoinBatchRegistry.State state=batchRegistry.batchState(id);
        if(state != BitcoinBatchRegistry.State.Reserved && state != BitcoinBatchRegistry.State.Signing)revert BatchNotPlanPublishable();
        if(feePlanPublisher[id] != address(0) || plans[id].length != 0 || total == 0 || total > MAX_FEE_PLAN_BYTES
            || index != p.chunks.length || index >= 8 || chunk.length == 0 || chunk.length > 8192 || p.size+chunk.length > total)revert InvalidPlanLength();
        if(p.total == 0)p.total=total;else if(p.total != total)revert ConflictingRetry();
        p.chunks.push(chunk);p.size+=chunk.length;
    }
    function commitFeePlan(bytes32 id) external {
        if(!dkg.isSessionParticipant(walletId,msg.sender))revert NotWalletParticipant();
        FeeParts storage p=feeParts[id][msg.sender];if(p.total == 0 || p.size != p.total)revert InvalidPlanLength();
        bytes memory raw=feePlanPartial(id,msg.sender);bytes memory previous=plan(id);
        if(previous.length != 0) { if(keccak256(previous) != keccak256(raw))revert ConflictingRetry();return; }
        _feeContext(id);(bytes32 approved,,,BitcoinBatchRegistry.State state)=batchRegistry.batches(id);
        if(state != BitcoinBatchRegistry.State.Reserved && state != BitcoinBatchRegistry.State.Signing)revert BatchNotPlanPublishable();
        bytes32 hash=keccak256(abi.encode(PLAN_DOMAIN,block.chainid,address(this),walletResult,id,keccak256(raw)));
        if(hash != approved)revert PlanHashMismatch();feePlanPublisher[id]=msg.sender;emit PlanPublished(id,msg.sender,hash);
    }

    /// @notice Publish the public plan already approved in this batch's reservation.
    /// @dev No signature, secret nonce, share or local signer state belongs in a plan.
    function publishPlan(bytes32 batchId, bytes calldata payload) external {
        _publishPlan(batchId,payload,MAX_PLAN_BYTES);
    }
    function publishFeePlan(bytes32 batchId,bytes calldata payload) external {
        _feeContext(batchId);
        _publishPlan(batchId,payload,MAX_FEE_PLAN_BYTES);
    }
    function _publishPlan(bytes32 batchId,bytes calldata payload,uint256 maximum) private {
        if (!dkg.isSessionParticipant(walletId, msg.sender)) revert NotWalletParticipant();
        bytes memory previous = plan(batchId);
        if (previous.length != 0) {
            if (keccak256(previous) != keccak256(payload)) revert ConflictingRetry();
            return;
        }
        (bytes32 approvedPlanHash,,, BitcoinBatchRegistry.State state) = batchRegistry.batches(batchId);
        if (state != BitcoinBatchRegistry.State.Reserved && state != BitcoinBatchRegistry.State.Signing) {
            revert BatchNotPlanPublishable();
        }
        if (payload.length == 0 || payload.length > maximum) revert InvalidPlanLength();
        bytes32 planHash = getPlanHash(batchId, payload);
        if (approvedPlanHash != planHash) revert PlanHashMismatch();
        plans[batchId] = payload;
        emit PlanPublished(batchId, msg.sender, planHash);
    }

    function packetKey(bytes32 batchId, uint32 inputIndex, PacketKind kind, address sender)
        public pure returns (bytes32)
    {
        return keccak256(abi.encode(batchId, inputIndex, kind, sender));
    }

    function packet(bytes32 batchId, uint32 inputIndex, PacketKind kind, address sender)
        external view returns (bytes memory)
    {
        return packets[packetKey(batchId, inputIndex, kind, sender)];
    }

    function publish(bytes32 batchId, uint32 inputIndex, PacketKind kind, bytes calldata payload) external {
        _publish(batchId, inputIndex, kind, payload);
    }

    /// @notice Publish this sender's public packets for all inputs atomically.
    /// Each packet retains the same attempt/input/kind/sender key and retry rules.
    function publishBatch(bytes32 batchId, PacketKind kind, bytes[] calldata payloads) external {
        uint256 count = payloads.length;
        if (count == 0 || count > 16 || count != batchRegistry.getInputs(batchId).length) revert InvalidPacketBatch();
        uint256 total;
        for (uint256 i; i < count; ++i) {
            total += payloads[i].length;
            if (total > 16_384) revert InvalidPacketBatch();
            _publish(batchId, uint32(i), kind, payloads[i]);
        }
    }

    function _publish(bytes32 batchId, uint32 inputIndex, PacketKind kind, bytes calldata payload) private {
        bytes32 key = packetKey(batchId, inputIndex, kind, msg.sender);
        bytes storage previous = packets[key];
        // Delivery retries use the stored bytes even after the batch changes state.
        // msg.sender is part of the key, so a retry cannot impersonate another signer.
        if (previous.length != 0) {
            if (keccak256(previous) != keccak256(payload)) revert ConflictingRetry();
            return;
        }
        if (!dkg.isSessionParticipant(walletId, msg.sender)) revert NotWalletParticipant();
        if (batchRegistry.batchState(batchId) != BitcoinBatchRegistry.State.Signing) revert BatchNotSigning();
        if (inputIndex >= batchRegistry.getInputs(batchId).length) revert InputOutOfBounds();
        if (payload.length == 0 || payload.length > maxPacketBytes) revert InvalidPacketLength();
        packets[key] = payload;
        emit PacketPublished(batchId, inputIndex, msg.sender, kind, keccak256(payload));
    }
}
