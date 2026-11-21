// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {Vm} from "forge-std/Vm.sol";
import {BitcoinSigningBoard} from "../contracts/src/federation/BitcoinSigningBoard.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {BitcoinWithdrawalEscrow} from "../contracts/src/federation/BitcoinWithdrawalEscrow.sol";
import {RegtestHeaderRelay} from "../contracts/src/federation/RegtestHeaderRelay.sol";
import {DKGAuthorization} from "../contracts/src/federation/DKGAuthorization.sol";
import {PoolMpToken} from "../contracts/src/tokens/PoolMpToken.sol";
import {DeployBitcoinSigningBoard} from "../script/DeployBitcoinSigningBoard.s.sol";

/// @dev Only this test harness can change a signing batch's state. Production gets no reset method.
contract BoardBatchStateHarness is BitcoinBatchRegistry {
    constructor(address auth, address registry, bytes32 wallet) BitcoinBatchRegistry(auth, registry, wallet) {}
    function setStateForTest(bytes32 batch, State state) external { batches[batch].state = state; }
}

contract BoardBindingFixture {
    address public dkg;
    bytes32 public walletId;
    bytes32 public walletResult;
    constructor(address registry, bytes32 id, bytes32 result) { dkg = registry; walletId = id; walletResult = result; }
}

contract BoardMiningFixture {
    PoolMpToken public mpToken = new PoolMpToken("Board escrow MP", "BMP", address(this), false);
    function mint(address recipient, uint256 amount) external { mpToken.mint(recipient, amount); }
}

contract BoardBackingFixture {
    address public immutable spendRecorder;
    mapping(address => bytes32) public poolScripts;
    constructor(address escrow, address miningPool) { spendRecorder = escrow; poolScripts[miningPool] = bytes32(uint256(1)); }
    function recordSpend(bytes calldata, uint256) external view { require(msg.sender == spendRecorder); }
    function recordFamily(bytes32) external view { require(msg.sender == spendRecorder); }
}

contract BitcoinSigningBoardTest is ConfederationEpochFixture {
    bytes32 private constant AUTH_ID = keccak256("board authorization epoch");
    bytes32 private constant WALLET_ID = keccak256("board bitcoin epoch");
    bytes32 private constant BATCH = keccak256("first plan");
    bytes32 private constant OTHER_BATCH = keccak256("second plan");
    uint256 private constant MAX_PACKET = 128;
    DKGAuthorization private auth;
    BitcoinBatchRegistry private batches;
    BitcoinSigningBoard private board;

    event PacketPublished(bytes32 indexed batchId, uint32 indexed inputIndex, address indexed sender,
        BitcoinSigningBoard.PacketKind kind, bytes32 payloadHash);

    function setUp() public {
        _baseSetUp();
        _completeCeremony(AUTH_ID, _roster(2, 2), 3, keccak256("EVM_AUTH"), GX);
        _completeCeremony(WALLET_ID, _roster(2, 2), 3, keccak256("BTC_SPEND"), SECOND_GX);
        auth = new DKGAuthorization(address(coordinator), AUTH_ID, abi.encodePacked(GX), _roster(2, 2), 3);
        batches = new BitcoinBatchRegistry(address(auth), address(dkg), WALLET_ID);
        board = new BitcoinSigningBoard(address(batches), MAX_PACKET);
        _reserve(batches, BATCH, 16);
        _reserve(batches, OTHER_BATCH, 2);
        vm.startPrank(address(0x100));
        batches.beginSigning(BATCH);
        batches.beginSigning(OTHER_BATCH);
        vm.stopPrank();
    }

    function _reserve(BitcoinBatchRegistry registry, bytes32 id, uint256 count) private returns (bytes32 op) {
        bytes32[] memory outpoints = new bytes32[](count);
        for (uint256 i; i < count; ++i) outpoints[i] = keccak256(abi.encode(id, i));
        bytes32 planHash = keccak256(abi.encode("plan", id));
        uint64 deadline = uint64(block.timestamp + 1 hours);
        op = registry.operationHash(id, planHash, outpoints, deadline);
        _approve(auth, AUTH_ID, op, address(registry));
        registry.reserve(id, planHash, outpoints, deadline, op);
    }

    function testImmutableBindingGettersAndEmptyPacket() public view {
        assertEq(address(board.batchRegistry()), address(batches));
        assertEq(address(board.dkg()), address(dkg));
        assertEq(board.walletId(), WALLET_ID);
        assertEq(board.walletResult(), batches.walletResult());
        assertEq(board.maxPacketBytes(), MAX_PACKET);
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)), hex"");
        assertEq(board.packet(bytes32(0), type(uint32).max, BitcoinSigningBoard.PacketKind.SignatureShare, OUTSIDER), hex"");
    }

    function testBatchPublishesAllInputsAndExactRetryHasNoEvents() public {
        bytes[] memory payloads = new bytes[](16);
        for (uint256 i; i < payloads.length; ++i) payloads[i] = abi.encode(i);
        vm.recordLogs();
        vm.prank(address(0x100));
        board.publishBatch(BATCH, BitcoinSigningBoard.PacketKind.Commitment, payloads);
        assertEq(vm.getRecordedLogs().length, 16);
        for (uint32 i; i < 16; ++i) {
            assertEq(board.packet(BATCH, i, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)), payloads[i]);
            assertEq(board.packet(BATCH, i, BitcoinSigningBoard.PacketKind.Commitment, address(0x101)).length, 0);
        }
        vm.recordLogs();
        vm.prank(address(0x100));
        board.publishBatch(BATCH, BitcoinSigningBoard.PacketKind.Commitment, payloads);
        assertEq(vm.getRecordedLogs().length, 0);
    }

    function testBatchLastConflictRollsBackEarlierWrites() public {
        vm.prank(address(0x100));
        board.publish(OTHER_BATCH, 1, BitcoinSigningBoard.PacketKind.Commitment, hex"aabb");
        bytes[] memory payloads = new bytes[](2);
        payloads[0] = hex"11";
        payloads[1] = hex"ccdd";
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.ConflictingRetry.selector);
        board.publishBatch(OTHER_BATCH, BitcoinSigningBoard.PacketKind.Commitment, payloads);
        assertEq(board.packet(OTHER_BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)).length, 0);
        assertEq(board.packet(OTHER_BATCH, 1, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)), hex"aabb");
    }

    function testBatchRejectsOutsiderIncompleteInputsAndOversizedAggregate() public {
        bytes[] memory payloads = new bytes[](2);
        payloads[0] = hex"11";
        payloads[1] = hex"22";
        vm.prank(OUTSIDER);
        vm.expectRevert(BitcoinSigningBoard.NotWalletParticipant.selector);
        board.publishBatch(OTHER_BATCH, BitcoinSigningBoard.PacketKind.SignatureShare, payloads);
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.InvalidPacketBatch.selector);
        board.publishBatch(BATCH, BitcoinSigningBoard.PacketKind.Commitment, payloads);
        BitcoinSigningBoard large = new BitcoinSigningBoard(address(batches), 16_384);
        payloads[0] = new bytes(16_384);
        payloads[1] = hex"22";
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.InvalidPacketBatch.selector);
        large.publishBatch(OTHER_BATCH, BitcoinSigningBoard.PacketKind.Commitment, payloads);
        assertEq(large.packet(OTHER_BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)).length, 0);
    }

    function testBothPacketKindsExactKeysPayloadHashesAndEventsAtFirstAndLastInput() public {
        for (uint8 k; k < 2; ++k) {
            BitcoinSigningBoard.PacketKind kind = BitcoinSigningBoard.PacketKind(k);
            uint32 inputIndex = k == 0 ? 0 : 15;
            bytes memory payload = abi.encodePacked(bytes1(k), bytes32(uint256(123)));
            bytes32 expectedKey = keccak256(abi.encode(BATCH, inputIndex, kind, address(0x100)));
            assertEq(board.packetKey(BATCH, inputIndex, kind, address(0x100)), expectedKey);
            vm.expectEmit(true, true, true, true, address(board));
            emit PacketPublished(BATCH, inputIndex, address(0x100), kind, keccak256(payload));
            vm.prank(address(0x100));
            board.publish(BATCH, inputIndex, kind, payload);
            assertEq(board.packet(BATCH, inputIndex, kind, address(0x100)), payload);
        }
    }

    function testBatchInputKindAndActualSenderHaveIndependentKeys() public {
        for (uint256 b; b < 2; ++b) {
            bytes32 batch = b == 0 ? BATCH : OTHER_BATCH;
            for (uint32 i; i < 2; ++i) {
                for (uint8 k; k < 2; ++k) {
                    for (uint256 s; s < 2; ++s) {
                        address sender = address(uint160(0x100 + s));
                        BitcoinSigningBoard.PacketKind kind = BitcoinSigningBoard.PacketKind(k);
                        bytes memory payload = abi.encodePacked(b, i, k, s);
                        vm.prank(sender);
                        board.publish(batch, i, kind, payload);
                        assertEq(board.packet(batch, i, kind, sender), payload);
                    }
                }
            }
        }
    }

    function testMaximumPayloadLengthAndIdempotentDeliveryProduceExactlyOneEvent() public {
        bytes memory payload = new bytes(MAX_PACKET);
        payload[0] = 0xAA;
        payload[MAX_PACKET - 1] = 0xBB;
        vm.recordLogs();
        vm.prank(address(0x200));
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, payload);
        Vm.Log[] memory firstLogs = vm.getRecordedLogs();
        assertEq(firstLogs.length, 1);
        assertEq(firstLogs[0].emitter, address(board));
        assertEq(firstLogs[0].topics[0], keccak256("PacketPublished(bytes32,uint32,address,uint8,bytes32)"));
        assertEq(firstLogs[0].topics[1], BATCH);
        assertEq(firstLogs[0].topics[2], bytes32(0));
        assertEq(firstLogs[0].topics[3], bytes32(uint256(uint160(address(0x200)))));
        assertEq(firstLogs[0].data, abi.encode(BitcoinSigningBoard.PacketKind.SignatureShare, keccak256(payload)));
        vm.recordLogs();
        vm.prank(address(0x200));
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, payload);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, address(0x200)), payload);
    }

    function testIdenticalDeliveryAfterStateChangeSucceedsWithoutEventButNewOrChangedBytesFail() public {
        BoardBatchStateHarness harness = new BoardBatchStateHarness(address(auth), address(dkg), WALLET_ID);
        BitcoinSigningBoard transport = new BitcoinSigningBoard(address(harness), MAX_PACKET);
        _reserve(harness, BATCH, 1);
        vm.startPrank(address(0x100));
        harness.beginSigning(BATCH);
        transport.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"112233");
        vm.stopPrank();
        harness.setStateForTest(BATCH, BitcoinBatchRegistry.State.Cancelled);
        vm.recordLogs();
        vm.prank(address(0x100));
        transport.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"112233");
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(transport.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)), hex"112233");
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.ConflictingRetry.selector);
        transport.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"112234");
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.BatchNotSigning.selector);
        transport.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, hex"112233");
        vm.prank(address(0x101));
        vm.expectRevert(BitcoinSigningBoard.BatchNotSigning.selector);
        transport.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"112233");
    }

    function testRejectsOutsiderCannotImpersonateParticipant() public {
        vm.prank(OUTSIDER);
        vm.expectRevert(BitcoinSigningBoard.NotWalletParticipant.selector);
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, abi.encode(address(0x100)));
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)), hex"");
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, OUTSIDER), hex"");
    }

    function testRejectsUnknownReservedAndCancelledBatchForFirstPublication() public {
        bytes32 reserved = keccak256("reserved but not signing");
        _reserve(batches, reserved, 1);
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.BatchNotSigning.selector);
        board.publish(bytes32(0), 0, BitcoinSigningBoard.PacketKind.Commitment, hex"11");
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.BatchNotSigning.selector);
        board.publish(reserved, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"11");
        bytes32 op = auth.hashOperation(address(batches), batches.cancel.selector, keccak256(abi.encode(reserved)), batches.nonce());
        _approve(auth, AUTH_ID, op, address(batches));
        batches.cancel(reserved, op);
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.BatchNotSigning.selector);
        board.publish(reserved, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"11");
    }

    function testRejectsIndexAtLengthAndMaximumUint32() public {
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.InputOutOfBounds.selector);
        board.publish(BATCH, 16, BitcoinSigningBoard.PacketKind.Commitment, hex"11");
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.InputOutOfBounds.selector);
        board.publish(BATCH, type(uint32).max, BitcoinSigningBoard.PacketKind.Commitment, hex"11");
    }

    function testRejectsInvalidKindFromRawCalldata() public {
        vm.prank(address(0x100));
        (bool success,) = address(board).call(abi.encodeWithSelector(board.publish.selector, BATCH, uint32(0), uint8(2), hex"11"));
        assertFalse(success);
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)), hex"");
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, address(0x100)), hex"");
    }

    function testRejectsEmptyAndOversizedPayloadWithoutRecording() public {
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.InvalidPacketLength.selector);
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"");
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.InvalidPacketLength.selector);
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, new bytes(MAX_PACKET + 1));
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, address(0x100)), hex"");
    }

    function testRejectsConflictingRetryAndNeverOverwrites() public {
        vm.prank(address(0x100));
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, hex"1122");
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.ConflictingRetry.selector);
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, hex"1133");
        vm.prank(address(0x100));
        vm.expectRevert(BitcoinSigningBoard.ConflictingRetry.selector);
        board.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, hex"");
        assertEq(board.packet(BATCH, 0, BitcoinSigningBoard.PacketKind.SignatureShare, address(0x100)), hex"1122");
    }

    function testRejectsInvalidConstructorBindingsAndZeroMaximum() public {
        vm.expectRevert(BitcoinSigningBoard.InvalidBinding.selector);
        new BitcoinSigningBoard(address(0), MAX_PACKET);
        vm.expectRevert(BitcoinSigningBoard.InvalidBinding.selector);
        new BitcoinSigningBoard(OUTSIDER, MAX_PACKET);
        vm.expectRevert(BitcoinSigningBoard.InvalidPacketLength.selector);
        new BitcoinSigningBoard(address(batches), 0);
        BoardBindingFixture wrongDkg = new BoardBindingFixture(OUTSIDER, WALLET_ID, batches.walletResult());
        vm.expectRevert(BitcoinSigningBoard.InvalidBinding.selector);
        new BitcoinSigningBoard(address(wrongDkg), MAX_PACKET);
        BoardBindingFixture wrongResult = new BoardBindingFixture(address(dkg), WALLET_ID, bytes32(uint256(123)));
        vm.expectRevert(BitcoinSigningBoard.InvalidBinding.selector);
        new BitcoinSigningBoard(address(wrongResult), MAX_PACKET);
        (, bytes32 authResult,,,,,) = dkg.getCompletedResult(AUTH_ID);
        BoardBindingFixture wrongPurpose = new BoardBindingFixture(address(dkg), AUTH_ID, authResult);
        vm.expectRevert(BitcoinSigningBoard.InvalidBinding.selector);
        new BitcoinSigningBoard(address(wrongPurpose), MAX_PACKET);
        BoardBindingFixture unfinished = new BoardBindingFixture(address(dkg), bytes32(uint256(777)), bytes32(uint256(1)));
        vm.expectRevert(DKGCoordinator.DKGNotComplete.selector);
        new BitcoinSigningBoard(address(unfinished), MAX_PACKET);
    }

    function testPublicationLeavesRealEscrowReservationsApprovalsAndRefundBoundaryUnchanged() public {
        BitcoinBatchRegistry registry = new BitcoinBatchRegistry(address(auth), address(dkg), WALLET_ID);
        RegtestHeaderRelay relay = new RegtestHeaderRelay();
        BitcoinWithdrawalEscrow escrow = new BitcoinWithdrawalEscrow(address(registry), address(relay), 6, 294, 330);
        bytes32 op = registry.getSetWithdrawalEscrowOperationHash(address(escrow));
        _approve(auth, AUTH_ID, op, address(registry));
        registry.setWithdrawalEscrow(address(escrow), op);
        BoardMiningFixture mining = new BoardMiningFixture();
        BoardBackingFixture backing = new BoardBackingFixture(address(escrow), address(mining));
        op = escrow.getSetDepositRegistryOperationHash(address(backing));
        _approve(auth, AUTH_ID, op, address(escrow));
        escrow.setDepositRegistry(address(backing), op);
        PoolMpToken token = mining.mpToken();
        mining.mint(address(this), 294 * 1e10);
        token.approve(address(escrow), 294 * 1e10);
        escrow.requestDirect(1, address(token), 294 * 1e10, hex"00141111111111111111111111111111111111111111");
        bytes32 request = escrow.requestKey(address(this), 1);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = request;
        bytes32 reservationOp = _reserve(registry, BATCH, 2);
        op = escrow.getBindOperationHash(BATCH, keccak256("transaction id"), keys);
        _approve(auth, AUTH_ID, op, address(escrow));
        escrow.bind(BATCH, keccak256("transaction id"), keys, op);
        vm.prank(address(0x100));
        registry.beginSigning(BATCH);
        BitcoinSigningBoard transport = new BitcoinSigningBoard(address(registry), MAX_PACKET);
        bytes32 sentinel = auth.hashOperation(address(escrow), bytes4(keccak256("sentinel()")), bytes32(0), 999);
        _approve(auth, AUTH_ID, sentinel, address(escrow));
        bytes32 beforeDigest = _escrowDigest(registry, escrow, token, request, sentinel, reservationOp);
        vm.startPrank(address(0x100));
        transport.publish(BATCH, 0, BitcoinSigningBoard.PacketKind.Commitment, hex"1122");
        transport.publish(BATCH, 1, BitcoinSigningBoard.PacketKind.SignatureShare, hex"3344");
        transport.publish(BATCH, 1, BitcoinSigningBoard.PacketKind.SignatureShare, hex"3344");
        vm.stopPrank();
        assertEq(_escrowDigest(registry, escrow, token, request, sentinel, reservationOp), beforeDigest);
        vm.expectRevert("signature may exist");
        escrow.refund(request);
        vm.expectRevert("signature may exist");
        registry.cancel(BATCH, bytes32(0));
        assertEq(uint256(registry.batchState(BATCH)), uint256(BitcoinBatchRegistry.State.Signing));
        assertEq(token.balanceOf(address(escrow)), 294 * 1e10);
    }

    function _escrowDigest(BitcoinBatchRegistry registry, BitcoinWithdrawalEscrow escrow, PoolMpToken token,
        bytes32 request, bytes32 sentinel, bytes32 reservationOp) private view returns (bytes32)
    {
        bytes32[] memory inputs = registry.getInputs(BATCH);
        (bool ok, bytes memory requestBytes) = address(escrow).staticcall(abi.encodeWithSelector(escrow.requests.selector, request));
        require(ok);
        (bool batchOk, bytes memory batchBytes) = address(registry).staticcall(abi.encodeWithSelector(registry.batches.selector, BATCH));
        require(batchOk);
        return keccak256(abi.encode(batchBytes, inputs, registry.reservations(inputs[0]), registry.reservations(inputs[1]),
            registry.nonce(), registry.withdrawalEscrow(), requestBytes, escrow.payment(BATCH), escrow.nonce(),
            address(escrow.depositRegistry()), token.totalSupply(), token.balanceOf(address(escrow)),
            auth.approvedOperations(sentinel), auth.executedOperations(sentinel), auth.executedOperations(reservationOp)));
    }

    function testScriptDeploysWithExplicitBindingAndMaximum() public {
        DeployBitcoinSigningBoard deployment = new DeployBitcoinSigningBoard();
        _scriptEnvironment();
        BitcoinSigningBoard deployed = deployment.run();
        assertEq(address(deployed.batchRegistry()), address(batches));
        assertEq(deployed.walletId(), WALLET_ID);
        assertEq(deployed.maxPacketBytes(), MAX_PACKET);
        assertLe(address(deployed).code.length, 24_576);
        assertLe(bytes.concat(type(BitcoinSigningBoard).creationCode, abi.encode(address(batches), MAX_PACKET)).length, 49_152);
    }

    function testScriptRejectsWrongChainAndInvalidConfigurationBeforeDeployment() public {
        DeployBitcoinSigningBoard deployment = new DeployBitcoinSigningBoard();
        _scriptEnvironment();
        uint64 nonceBefore = vm.getNonce(vm.addr(1));
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.expectRevert("chain mismatch");
        deployment.run();
        vm.setEnv("EXPECTED_CHAIN_ID", "31337");
        vm.setEnv("MAX_PACKET_BYTES", "0");
        vm.expectRevert("board configuration missing");
        deployment.run();
        vm.setEnv("MAX_PACKET_BYTES", "128");
        vm.setEnv("BITCOIN_BATCH_REGISTRY", vm.toString(OUTSIDER));
        vm.expectRevert("board configuration missing");
        deployment.run();
        assertEq(vm.getNonce(vm.addr(1)), nonceBefore);
    }

    function _scriptEnvironment() private {
        vm.setEnv("EXPECTED_CHAIN_ID", "31337");
        vm.setEnv("BITCOIN_BATCH_REGISTRY", vm.toString(address(batches)));
        vm.setEnv("MAX_PACKET_BYTES", "128");
        vm.setEnv("PRIVATE_KEY", "1");
        vm.deal(vm.addr(1), 10 ether);
    }
}
