// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {BitcoinSigningBoard} from "../contracts/src/federation/BitcoinSigningBoard.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {DKGAuthorization} from "../contracts/src/federation/DKGAuthorization.sol";

/// @dev State mutation is restricted to test fixtures; the production registry has no reset.
contract BoardPlanStateHarness is BitcoinBatchRegistry {
    constructor(address auth, address registry, bytes32 wallet) BitcoinBatchRegistry(auth, registry, wallet) {}
    function setStateForTest(bytes32 batch, State state) external { batches[batch].state = state; }
}

/// @dev Binding-only fixture for an independent ABI/hash vector, not a cryptographic DKG fixture.
contract BoardPlanBindingFixture {
    address public dkg;
    bytes32 public walletId;
    bytes32 public walletResult;
    constructor(address registry, bytes32 id, bytes32 result) { dkg = registry; walletId = id; walletResult = result; }
}

contract BitcoinSigningBoardPlanTest is ConfederationEpochFixture {
    bytes32 private constant AUTH_ID = keccak256("board plan authorization epoch");
    bytes32 private constant WALLET_ID = keccak256("board plan bitcoin epoch");
    bytes32 private constant BATCH = keccak256("on-chain plan batch");
    bytes private constant PAYLOAD = '{"plan":"public"}';
    address private constant FIRST_SIGNER = address(0x100);
    DKGAuthorization private auth;
    BoardPlanStateHarness private batches;
    BitcoinSigningBoard private board;

    event PlanPublished(bytes32 indexed batchId, address indexed publisher, bytes32 planHash);

    function setUp() public {
        _baseSetUp();
        _completeCeremony(AUTH_ID, _roster(2, 2), 3, keccak256("EVM_AUTH"), GX);
        _completeCeremony(WALLET_ID, _roster(2, 2), 3, keccak256("BTC_SPEND"), SECOND_GX);
        auth = new DKGAuthorization(address(coordinator), AUTH_ID, abi.encodePacked(GX), _roster(2, 2), 3);
        batches = new BoardPlanStateHarness(address(auth), address(dkg), WALLET_ID);
        board = new BitcoinSigningBoard(address(batches), 128);
    }

    function _reserve(bytes32 id, bytes32 planHash) private returns (bytes32 op) {
        bytes32[] memory outpoints = new bytes32[](2);
        outpoints[0] = keccak256(abi.encode(id, uint256(0)));
        outpoints[1] = keccak256(abi.encode(id, uint256(1)));
        uint64 deadline = uint64(block.timestamp + 1 hours);
        op = batches.operationHash(id, planHash, outpoints, deadline);
        _approve(auth, AUTH_ID, op, address(batches));
        batches.reserve(id, planHash, outpoints, deadline, op);
    }

    function _publish(bytes32 id, bytes memory payload) private {
        vm.prank(FIRST_SIGNER);
        board.publishPlan(id, payload);
    }

    function testPlanHashMatchesIndependentPythonAbiVector() public {
        bytes32 result = bytes32(uint256(0x1111111111111111111111111111111111111111111111111111111111111111));
        bytes32 id = keccak256("hash vector wallet");
        vm.mockCall(address(dkg), abi.encodeWithSelector(dkg.getCompletedResult.selector, id),
            abi.encode(bytes32(0), result, uint64(1), keccak256("BTC_SPEND"), abi.encodePacked(GX), _roster(2, 2), uint256(3)));
        BoardPlanBindingFixture registry = new BoardPlanBindingFixture(address(dkg), id, result);
        BitcoinSigningBoard vector = new BitcoinSigningBoard(address(registry), 128);
        address fixedBoard = address(0x1234567890123456789012345678901234567890);
        vm.etch(fixedBoard, address(vector).code);
        bytes32 batch = 0x2222222222222222222222222222222222222222222222222222222222222222;
        assertEq(vector.PLAN_DOMAIN(), 0xda333206662dd368afaf7956f79313fdfe1124da08030d640d2ab265e782c9a5);
        // eth_utils.keccak(eth_abi.encode([bytes32,uint256,address,bytes32,bytes32,bytes32], ...)).
        assertEq(BitcoinSigningBoard(fixedBoard).getPlanHash(batch, PAYLOAD),
            0x4810abf6d688297b1250df2905ed2968d1b53ce0c463afbab57314531e7a6eed);
    }

    function testPlanHashSeparatesChainBoardWalletBatchAndExactPayloadBytes() public {
        bytes32 original = board.getPlanHash(BATCH, PAYLOAD);
        assertEq(original, keccak256(abi.encode(keccak256("SATOHSI_BTC_ONCHAIN_PLAN_V1"),
            block.chainid, address(board), board.walletResult(), BATCH, keccak256(PAYLOAD))));
        vm.chainId(11155111);
        assertNotEq(board.getPlanHash(BATCH, PAYLOAD), original);
        vm.chainId(31337);
        BitcoinSigningBoard otherBoard = new BitcoinSigningBoard(address(batches), 128);
        assertNotEq(otherBoard.getPlanHash(BATCH, PAYLOAD), original);
        assertNotEq(board.getPlanHash(keccak256("other batch"), PAYLOAD), original);
        assertNotEq(board.getPlanHash(BATCH, '{"plan": "public"}'), original);
        assertNotEq(keccak256(abi.encode(keccak256("SATOHSI_BTC_ONCHAIN_PLAN_V0"),
            block.chainid, address(board), board.walletResult(), BATCH, keccak256(PAYLOAD))), original);

        bytes32 otherWallet = keccak256("next bitcoin wallet epoch");
        _completeCeremony(otherWallet, _roster(2, 2), 3, keccak256("BTC_SPEND"), GX);
        BitcoinBatchRegistry otherRegistry = new BitcoinBatchRegistry(address(auth), address(dkg), otherWallet);
        BitcoinSigningBoard otherWalletBoard = new BitcoinSigningBoard(address(otherRegistry), 128);
        // Same address and chain isolate the walletResult field in the commitment.
        address aliasBoard = address(0x1234);
        vm.etch(aliasBoard, address(board).code);
        bytes32 firstWalletHash = BitcoinSigningBoard(aliasBoard).getPlanHash(BATCH, PAYLOAD);
        vm.etch(aliasBoard, address(otherWalletBoard).code);
        assertNotEq(BitcoinSigningBoard(aliasBoard).getPlanHash(BATCH, PAYLOAD), firstWalletHash);
    }

    function testReservedAndSigningFirstPublicationEmitExactHashAndPreserveRegistry() public {
        assertEq(board.MAX_PLAN_BYTES(), 16384);
        assertEq(board.plan(BATCH), hex"");
        for (uint256 i; i < 2; ++i) {
            bytes32 batch = keccak256(abi.encode(BATCH, i));
            bytes32 planHash = board.getPlanHash(batch, PAYLOAD);
            bytes32 reservationOp = _reserve(batch, planHash);
            if (i == 1) {
                vm.prank(FIRST_SIGNER);
                batches.beginSigning(batch);
            }
            bytes32 beforeState = _registryDigest(batch, reservationOp);
            vm.expectEmit(true, true, false, true, address(board));
            emit PlanPublished(batch, FIRST_SIGNER, planHash);
            _publish(batch, PAYLOAD);
            assertEq(board.plan(batch), PAYLOAD);
            assertEq(_registryDigest(batch, reservationOp), beforeState);
        }
    }

    function testMaximumPlanLengthAcceptedIndependentlyOfPacketLimit() public {
        bytes memory payload = new bytes(board.MAX_PLAN_BYTES());
        for (uint256 i; i < payload.length; i += 32) {
            assembly ("memory-safe") { mstore(add(add(payload, 0x20), i), not(0)) }
        }
        _reserve(BATCH, board.getPlanHash(BATCH, payload));
        uint256 gasBefore = gasleft();
        _publish(BATCH, payload);
        uint256 publicationGas = gasBefore - gasleft();
        // Leave transaction intrinsic calldata costs below Osaka's 2^24 gas cap too.
        assertLt(publicationGas + 21_000 + 16 * (payload.length + 100), 1 << 24);
        emit log_named_uint("Maximum publishPlan call gas", publicationGas);
        assertEq(board.plan(BATCH), payload);
    }

    function testRejectsEmptyAndOversizedPlanWithoutRecording() public {
        _reserve(BATCH, board.getPlanHash(BATCH, PAYLOAD));
        bytes memory oversized = new bytes(board.MAX_PLAN_BYTES() + 1);
        vm.prank(FIRST_SIGNER);
        vm.expectRevert(BitcoinSigningBoard.InvalidPlanLength.selector);
        board.publishPlan(BATCH, hex"");
        vm.prank(FIRST_SIGNER);
        vm.expectRevert(BitcoinSigningBoard.InvalidPlanLength.selector);
        board.publishPlan(BATCH, oversized);
        assertEq(board.plan(BATCH), hex"");
    }

    function testRejectsUnknownAndCancelledBatchForFirstPublication() public {
        vm.prank(FIRST_SIGNER);
        vm.expectRevert(BitcoinSigningBoard.BatchNotPlanPublishable.selector);
        board.publishPlan(BATCH, PAYLOAD);
        _reserve(BATCH, board.getPlanHash(BATCH, PAYLOAD));
        bytes32 op = auth.hashOperation(address(batches), batches.cancel.selector,
            keccak256(abi.encode(BATCH)), batches.nonce());
        _approve(auth, AUTH_ID, op, address(batches));
        batches.cancel(BATCH, op);
        vm.prank(FIRST_SIGNER);
        vm.expectRevert(BitcoinSigningBoard.BatchNotPlanPublishable.selector);
        board.publishPlan(BATCH, PAYLOAD);
        assertEq(board.plan(BATCH), hex"");
    }

    function testOnlyWalletParticipantsMayPublishIncludingIdenticalRetry() public {
        _reserve(BATCH, board.getPlanHash(BATCH, PAYLOAD));
        vm.prank(OUTSIDER);
        vm.expectRevert(BitcoinSigningBoard.NotWalletParticipant.selector);
        board.publishPlan(BATCH, PAYLOAD);
        assertEq(board.plan(BATCH), hex"");
        _publish(BATCH, PAYLOAD);
        vm.prank(OUTSIDER);
        vm.expectRevert(BitcoinSigningBoard.NotWalletParticipant.selector);
        board.publishPlan(BATCH, PAYLOAD);
        assertEq(board.plan(BATCH), PAYLOAD);
    }

    function testReservationRejectsUnapprovedPayloadAndCrossBoardCommitment() public {
        _reserve(BATCH, board.getPlanHash(BATCH, PAYLOAD));
        vm.prank(FIRST_SIGNER);
        vm.expectRevert(BitcoinSigningBoard.PlanHashMismatch.selector);
        board.publishPlan(BATCH, '{"plan":"changed"}');
        BitcoinSigningBoard otherBoard = new BitcoinSigningBoard(address(batches), 128);
        vm.prank(FIRST_SIGNER);
        vm.expectRevert(BitcoinSigningBoard.PlanHashMismatch.selector);
        otherBoard.publishPlan(BATCH, PAYLOAD);
        assertEq(board.plan(BATCH), hex"");
        assertEq(otherBoard.plan(BATCH), hex"");
    }

    function testIdenticalRetryByAnyParticipantAfterStateChangeEmitsNothingAndNeverOverwrites() public {
        _reserve(BATCH, board.getPlanHash(BATCH, PAYLOAD));
        _publish(BATCH, PAYLOAD);
        for (uint256 i; i < 4; ++i) {
            batches.setStateForTest(BATCH, BitcoinBatchRegistry.State(i));
            vm.recordLogs();
            vm.prank(address(0x200));
            board.publishPlan(BATCH, PAYLOAD);
            assertEq(vm.getRecordedLogs().length, 0);
            vm.prank(FIRST_SIGNER);
            vm.expectRevert(BitcoinSigningBoard.ConflictingRetry.selector);
            board.publishPlan(BATCH, '{"plan":"changed"}');
            vm.prank(address(0x201));
            vm.expectRevert(BitcoinSigningBoard.ConflictingRetry.selector);
            board.publishPlan(BATCH, hex"");
            assertEq(board.plan(BATCH), PAYLOAD);
            assertEq(uint256(batches.batchState(BATCH)), i);
        }
    }

    function testPlanPublicationDoesNotStartSigningOrPublishPackets() public {
        _reserve(BATCH, board.getPlanHash(BATCH, PAYLOAD));
        _publish(BATCH, PAYLOAD);
        assertEq(uint256(batches.batchState(BATCH)), uint256(BitcoinBatchRegistry.State.Reserved));
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, FIRST_SIGNER), hex"");
        vm.prank(FIRST_SIGNER);
        vm.expectRevert(BitcoinSigningBoard.BatchNotSigning.selector);
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"11");
        vm.prank(FIRST_SIGNER);
        batches.beginSigning(BATCH);
        vm.prank(FIRST_SIGNER);
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"11");
        assertEq(board.plan(BATCH), PAYLOAD);
    }

    function _registryDigest(bytes32 batch, bytes32 reservationOp) private view returns (bytes32) {
        (bytes32 planHash, bytes32 result, uint64 deadline, BitcoinBatchRegistry.State state) = batches.batches(batch);
        bytes32[] memory inputs = batches.getInputs(batch);
        return keccak256(abi.encode(planHash, result, deadline, state, inputs, batches.reservations(inputs[0]),
            batches.reservations(inputs[1]), batches.reservationRoot(batch), batches.versionCount(batch),
            batches.replacementOf(batch), batches.nonce(), batches.withdrawalEscrow(),
            auth.executedOperations(reservationOp), auth.approvedOperations(reservationOp)));
    }
}
