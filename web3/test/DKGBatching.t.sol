// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/federation/DKGCoordinator.sol";
import "../contracts/src/federation/DKGOnchainBoard.sol";

contract DKGBatchingTest is Test {
    DKGCoordinator d;
    DKGOnchainBoard board;
    address[] roster;
    bytes32 id = keccak256("bounded DKG batching");
    bytes32 gx = 0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798;

    function setUp() public {
        d = new DKGCoordinator();
        for (uint256 i; i < 3; ++i) roster.push(address(uint160(i + 11)));
        _create(id, roster);
        vm.prank(roster[0]); board = new DKGOnchainBoard("public invitation", roster);
        vm.prank(roster[0]); board.bindRegistry(address(d));
    }

    function _create(bytes32 sid,address[] memory members) internal {
        bytes32[] memory keys = new bytes32[](members.length);
        for (uint256 i; i < keys.length; ++i) keys[i] = bytes32(i + 1);
        d.createCeremony(sid, members, 2, keys, 1, keccak256("EVM_AUTH"), d.PROTOCOL_V1());
    }

    function _round1(bytes32 sid,address[] memory members) internal {
        bytes32[] memory commitments = new bytes32[](2);
        commitments[0] = bytes32(uint256(1)); commitments[1] = bytes32(uint256(2));
        for (uint256 i; i < members.length; ++i) {
            vm.prank(members[i]); d.submitRound1(sid, bytes32(i + 1), commitments);
        }
    }

    function _others(address[] memory members,uint256 self) internal pure returns(address[] memory others,bytes32[] memory hashes) {
        others = new address[](members.length - 1); hashes = new bytes32[](members.length - 1);
        uint256 k;
        for (uint256 i; i < members.length; ++i) if (i != self) {
            others[k] = members[i]; hashes[k++] = bytes32(uint256(123));
        }
    }

    function _round2(bytes32 sid,address[] memory members) internal {
        for (uint256 i; i < members.length; ++i) {
            (address[] memory others,bytes32[] memory hashes) = _others(members,i);
            vm.prank(members[i]); d.submitShareHashes(sid,others,hashes);
        }
    }

    function _packets() internal view returns(uint8[] memory kinds,address[] memory recipients,bytes[] memory payloads) {
        kinds = new uint8[](3); recipients = new address[](3); payloads = new bytes[](3);
        kinds[0] = 0; kinds[1] = 1; kinds[2] = 2;
        recipients[2] = roster[1];
        payloads[0] = bytes("signed transport binding"); payloads[1] = bytes("public round one");
        payloads[2] = bytes("HPKE ciphertext");
    }

    function testBoardBatchSameSenderAtomicImmutableAndSessionIsolated() public {
        (uint8[] memory kinds,address[] memory recipients,bytes[] memory payloads) = _packets();
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector); board.publishBatch(id,kinds,recipients,payloads);
        vm.startPrank(roster[0]);
        board.publishBatch(id,kinds,recipients,payloads);
        board.publishBatch(id,kinds,recipients,payloads);
        payloads[2] = bytes("conflicting ciphertext");
        vm.expectRevert(DKGOnchainBoard.ConflictingRetry.selector); board.publishBatch(id,kinds,recipients,payloads);
        vm.stopPrank();
        assertEq(board.packet(id,2,roster[0],roster[1]),bytes("HPKE ciphertext"));
        assertEq(board.packet(id,2,roster[1],roster[0]).length,0);
        assertEq(board.packet(keccak256("another session"),0,roster[0],address(0)).length,0);
    }

    function testBoardBatchRollsBackEarlierValidPacketOnLaterFailure() public {
        (uint8[] memory kinds,address[] memory recipients,bytes[] memory payloads) = _packets();
        recipients[2] = roster[0];
        vm.prank(roster[0]); vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);
        board.publishBatch(id,kinds,recipients,payloads);
        assertEq(board.packet(id,0,roster[0],address(0)).length,0);
    }

    function testBoardBatchRejectsAmbiguityAndBounds() public {
        (uint8[] memory kinds,address[] memory recipients,bytes[] memory payloads) = _packets();
        vm.startPrank(roster[0]);
        kinds[1] = 0;
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector); board.publishBatch(id,kinds,recipients,payloads);
        kinds[1] = 1; payloads[0] = new bytes(16384);
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector); board.publishBatch(id,kinds,recipients,payloads);
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector); board.publishBatch(id,kinds,new address[](2),payloads);
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector); board.publishBatch(id,new uint8[](0),new address[](0),new bytes[](0));
        vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector); board.publishBatch(id,new uint8[](17),new address[](17),new bytes[](17));
        vm.stopPrank();
        assertEq(board.packet(id,0,roster[0],address(0)).length,0);
    }

    function testBoardMaximumBatchFitsEthereumTransactionGasCapWithClientMargin() public {
        address[] memory members = new address[](16);
        for (uint256 i; i < 16; ++i) members[i] = address(uint160(i + 100));
        bytes32 sid = keccak256("maximum batch"); _create(sid,members);
        vm.prank(members[0]); DKGOnchainBoard large = new DKGOnchainBoard("public invitation",members);
        vm.prank(members[0]); large.bindRegistry(address(d));
        uint8[] memory kinds = new uint8[](16); address[] memory recipients = new address[](16);
        bytes[] memory payloads = new bytes[](16);
        for (uint256 i; i < 16; ++i) {
            kinds[i] = i < 2 ? uint8(i) : 2;
            recipients[i] = i < 2 ? address(0) : members[i - 1];
            payloads[i] = new bytes(1024);
            for (uint256 j; j < 1024; ++j) payloads[i][j] = 0xff;
        }
        vm.prank(members[0]); uint256 beforeGas = gasleft(); large.publishBatch(sid,kinds,recipients,payloads);
        uint256 executionGas = beforeGas - gasleft();
        bytes memory data = abi.encodeCall(large.publishBatch,(sid,kinds,recipients,payloads));
        uint256 intrinsic = 21000;
        for (uint256 i; i < data.length; ++i) intrinsic += data[i] == 0 ? 4 : 16;
        uint256 estimate = executionGas + intrinsic;
        emit log_named_uint("maximum packet batch gas incl intrinsic",estimate);
        assertLt((estimate * 130 + 99) / 100,1 << 24);
        assertLt(estimate + 125000,1 << 24);
        assertEq(large.packet(sid,2,members[0],members[14]).length,1024);
    }

    function testHashBatchRetryPreservesCountersAndCaller() public {
        _round1(id,roster); (address[] memory others,bytes32[] memory hashes) = _others(roster,0);
        vm.expectRevert(DKGCoordinator.NotAParticipant.selector); d.submitShareHashes(id,others,hashes);
        vm.startPrank(roster[0]); d.submitShareHashes(id,others,hashes); d.submitShareHashes(id,others,hashes); vm.stopPrank();
        (,,uint256 count,,) = d.getDKGStatus(id); assertEq(count,2);
        assertEq(d.getShareHash(id,roster[0],roster[1]),hashes[0]);
        assertEq(d.getShareHash(id,roster[1],roster[0]),bytes32(0));
    }

    function testHashBatchRejectsDuplicateSelfZeroForeignAndLengthAtomically() public {
        _round1(id,roster); (address[] memory others,bytes32[] memory hashes) = _others(roster,0);
        vm.startPrank(roster[0]);
        others[1] = others[0]; vm.expectRevert(DKGCoordinator.InvalidBatch.selector); d.submitShareHashes(id,others,hashes);
        others[1] = roster[0]; vm.expectRevert(DKGCoordinator.InvalidRecipient.selector); d.submitShareHashes(id,others,hashes);
        others[1] = address(99); vm.expectRevert(DKGCoordinator.InvalidRecipient.selector); d.submitShareHashes(id,others,hashes);
        others[1] = roster[2]; hashes[1] = 0; vm.expectRevert(DKGCoordinator.InvalidHash.selector); d.submitShareHashes(id,others,hashes);
        vm.expectRevert(DKGCoordinator.InvalidBatch.selector); d.submitShareHashes(id,others,new bytes32[](1));
        vm.expectRevert(DKGCoordinator.InvalidBatch.selector); d.submitShareHashes(id,new address[](0),new bytes32[](0));
        vm.expectRevert(DKGCoordinator.InvalidBatch.selector); d.submitShareHashes(id,new address[](3),new bytes32[](3));
        vm.stopPrank(); (,,uint256 count,,) = d.getDKGStatus(id); assertEq(count,0);
    }

    function testConflictingHashBatchRollsBackAndCannotBypassPhaseDeadline() public {
        (address[] memory others,bytes32[] memory hashes) = _others(roster,0);
        vm.prank(roster[0]); vm.expectRevert(DKGCoordinator.InvalidPhase.selector); d.submitShareHashes(id,others,hashes);
        _round1(id,roster);
        vm.prank(roster[0]); d.submitShareHash(id,others[1],bytes32(uint256(1)));
        vm.prank(roster[0]); vm.expectRevert(DKGCoordinator.ShareAlreadySubmitted.selector); d.submitShareHashes(id,others,hashes);
        assertEq(d.getShareHash(id,roster[0],others[0]),bytes32(0));
        vm.warp(block.timestamp + d.PHASE_DURATION() + 1);
        vm.prank(roster[0]); vm.expectRevert(DKGCoordinator.PhaseExpired.selector); d.submitShareHashes(id,others,hashes);
    }

    function testReceiptPublicPointBatchRetryAndAllNAttestations() public {
        _round1(id,roster); _round2(id,roster);
        for (uint256 i; i < 3; ++i) {
            (address[] memory others,bytes32[] memory hashes) = _others(roster,i);
            bytes memory point = abi.encodePacked(bytes1(i == 1 ? 0x03 : 0x02),gx);
            vm.startPrank(roster[i]);
            d.submitReceiptsAndPublicKeyShare(id,others,hashes,point);
            d.submitReceiptsAndPublicKeyShare(id,others,hashes,point);
            vm.stopPrank();
        }
        (,,,,,uint16 receipts,) = d.ceremonies(id); assertEq(receipts,6);
        (,,,uint256 pkCount,) = d.getDKGStatus(id); assertEq(pkCount,3);
        bytes32 result = d.computeResultHash(id,false,gx);
        for (uint256 i; i < 3; ++i) {
            (,,,,bool done) = d.getDKGStatus(id); assertFalse(done);
            vm.prank(roster[i]); d.attestResult(id,false,abi.encodePacked(gx),result);
        }
        (,,,,bool complete) = d.getDKGStatus(id); assertTrue(complete);
        (address[] memory other,bytes32[] memory hash) = _others(roster,0);
        vm.prank(roster[0]); vm.expectRevert(DKGCoordinator.InvalidPhase.selector);
        d.submitReceiptsAndPublicKeyShare(id,other,hash,abi.encodePacked(bytes1(0x02),gx));
    }

    function testReceiptBatchInvalidPointOrHashCannotLeaveReceipts() public {
        _round1(id,roster); _round2(id,roster); (address[] memory others,bytes32[] memory hashes) = _others(roster,0);
        vm.startPrank(roster[0]);
        vm.expectRevert(); d.submitReceiptsAndPublicKeyShare(id,others,hashes,hex"02");
        hashes[1] = bytes32(uint256(999));
        vm.expectRevert(DKGCoordinator.InvalidHash.selector); d.submitReceiptsAndPublicKeyShare(id,others,hashes,abi.encodePacked(bytes1(0x02),gx));
        vm.stopPrank(); (,,,,,uint16 receipts,) = d.ceremonies(id); assertEq(receipts,0);
        assertEq(d.receiptHashes(id,others[0],roster[0]),bytes32(0));
    }

    function testConflictingPublicPointRollsBackNewReceipts() public {
        _round1(id,roster); _round2(id,roster); (address[] memory others,bytes32[] memory hashes) = _others(roster,0);
        vm.startPrank(roster[0]); d.submitPublicKeyShare(id,abi.encodePacked(bytes1(0x02),gx));
        vm.expectRevert(DKGCoordinator.AlreadySubmitted.selector);
        d.submitReceiptsAndPublicKeyShare(id,others,hashes,abi.encodePacked(bytes1(0x03),gx)); vm.stopPrank();
        (,,,,,uint16 receipts,) = d.ceremonies(id); assertEq(receipts,0);
    }

    function testMaximumRosterBatchesCompleteWithBoundedGas() public {
        address[] memory members = new address[](16); for (uint256 i; i < 16; ++i) members[i] = address(uint160(i + 100));
        bytes32 sid = keccak256("max roster"); _create(sid,members); _round1(sid,members); _round2(sid,members);
        for (uint256 i; i < 16; ++i) {
            (address[] memory others,bytes32[] memory hashes) = _others(members,i);
            vm.prank(members[i]); uint256 beforeGas = gasleft();
            d.submitReceiptsAndPublicKeyShare(sid,others,hashes,abi.encodePacked(bytes1(0x02),gx));
            assertLt(beforeGas - gasleft(),2000000);
        }
        (,,,,,uint16 receipts,) = d.ceremonies(sid); assertEq(receipts,240);
        assertTrue(d.computeResultHash(sid,false,gx) != bytes32(0));
    }
}
