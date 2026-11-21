// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.t.sol";
import "../contracts/src/federation/BitcoinAddressMessageBoard.sol";

// Registry/DKG/approval/SPV setup inherits the explicit fixture boundaries.
// No ciphertext is interpreted as monetary authorization or Ready in EVM.
contract BitcoinAddressMessageBoardTest is BitcoinAddressRegistryTest {
    address constant USER = address(0x999);
    address constant SERVICE_ACCOUNT = address(0xa1);
    function _board() internal returns (BitcoinAddressMessageBoard board) {
        board = new BitcoinAddressMessageBoard(address(addresses));
        vm.prank(USER); board.registerKey(1,bytes32(uint256(11)));
        vm.prank(SERVICE_ACCOUNT); board.registerKey(1,bytes32(uint256(22)));
    }
    function _cipher() internal pure returns (bytes memory) { return new bytes(64); }
    function testTransportRetailRequesterAuthenticatedWithoutNewThresholdAuthority() public {
        BitcoinAddressMessageBoard board = _board();
        assertFalse(auth.isParticipant(USER));
        bytes32 request = keccak256("retail address request");
        vm.prank(USER); bytes32 id = board.request(request,SERVICE_ACCOUNT,1,1,uint64(block.timestamp+600),_cipher());
        BitcoinAddressMessageBoard.Packet memory packet = board.packet(id);
        assertEq(packet.sender,USER); assertEq(packet.recipient,SERVICE_ACCOUNT); assertEq(packet.kind,0);
        assertEq(board.messageCount(SERVICE_ACCOUNT),1); assertEq(board.messageAt(SERVICE_ACCOUNT,0),id);
        assertEq(pool.nonce(),0); assertEq(addresses.registrationNonce(),0);
        assertEq(uint256(addresses.derivationState()),0); // Fixture qualification, not a transport grant.
    }
    function testTransportReplyCannotImpersonateServiceOrRequester() public {
        BitcoinAddressMessageBoard board = _board(); bytes32 request = keccak256("route");
        vm.prank(USER); board.request(request,SERVICE_ACCOUNT,1,1,uint64(block.timestamp+600),_cipher());
        bytes32 route = board.routeKey(request,USER,SERVICE_ACCOUNT);
        vm.expectRevert("reply role"); vm.prank(USER);
        board.reply(route,1,0,1,1,uint64(block.timestamp+600),_cipher());
        vm.prank(SERVICE_ACCOUNT); bytes32 id = board.reply(route,1,0,1,1,uint64(block.timestamp+600),_cipher());
        assertEq(board.packet(id).recipient,USER);
        vm.expectRevert("kind/sequence"); vm.prank(SERVICE_ACCOUNT);
        board.reply(route,2,65,1,1,uint64(block.timestamp+600),_cipher());
    }
    function testTransportExactRetrySurvivesExpiryAndRotationWithoutNewInboxEntry() public {
        BitcoinAddressMessageBoard board = _board(); uint64 expiry = uint64(block.timestamp+600);
        bytes32 request = keccak256("durable retry");
        vm.prank(USER); bytes32 id = board.request(request,SERVICE_ACCOUNT,1,1,expiry,_cipher());
        vm.warp(expiry+1); vm.prank(USER); board.registerKey(2,bytes32(uint256(33)));
        vm.prank(USER); assertEq(board.request(request,SERVICE_ACCOUNT,1,1,expiry,_cipher()),id);
        assertEq(board.messageCount(SERVICE_ACCOUNT),1);
        bytes memory altered = _cipher(); altered[0]=0x01;
        vm.expectRevert("conflicting retry"); vm.prank(USER);
        board.request(request,SERVICE_ACCOUNT,1,1,expiry,altered);
    }
    function testTransportLimitsAndIndependentDomain() public {
        BitcoinAddressMessageBoard board = _board();
        assertEq(board.domain(),keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_MESSAGES_V1"),block.chainid,
            address(board),address(addresses),addresses.graphId(),address(auth))));
        vm.expectRevert("request role"); vm.prank(USER);
        board.request(keccak256("wrong service"),address(0x888),1,1,uint64(block.timestamp+600),_cipher());
        vm.expectRevert("ciphertext size"); vm.prank(USER);
        board.request(keccak256("oversize"),SERVICE_ACCOUNT,1,1,uint64(block.timestamp+600),new bytes(4097));
        vm.expectRevert("key epoch"); vm.prank(USER);
        board.request(keccak256("stale key"),SERVICE_ACCOUNT,2,1,uint64(block.timestamp+600),_cipher());
        assertEq(board.messageCount(SERVICE_ACCOUNT),0);
    }
}
