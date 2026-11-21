// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.t.sol";
import {BitcoinIncidentMessageBoard} from "../contracts/src/federation/BitcoinIncidentMessageBoard.sol";

// Real board/status execution. Inherited DKG, FROST approval and SPV are
// explicit fixtures; ciphertext confidentiality is tested with real HPKE in Python.
contract BitcoinIncidentMessageBoardTest is BitcoinAddressRegistryTest {
    function _incidentBoard() internal returns(BitcoinIncidentMessageBoard b) {
        b=new BitcoinIncidentMessageBoard(address(addresses));
        vm.prank(REQUESTER);b.registerKey(1,bytes32(uint256(11)));
        vm.prank(address(0xa1));b.registerKey(1,bytes32(uint256(22)));
    }
    function testIncidentReportDoesNotGrantStatusAuthority() public {
        BitcoinIncidentMessageBoard b=_incidentBoard();
        vm.prank(REQUESTER);bytes32 id=b.request(keccak256("private incident commitment"),address(0xa1),1,1,uint64(block.timestamp+600),new bytes(64));
        assertEq(b.packet(id).sender,REQUESTER);assertEq(addresses.custodyGeneration(),1);
        BitcoinAddressRegistry.StatusChange memory c=_change(0,2,false);
        bytes32 op=addresses.getStatusOperationHash(c);
        vm.expectRevert();addresses.setStatus(c,op);
        assertEq(addresses.custodyGeneration(),1);
        _approve(op,address(addresses));addresses.setStatus(c,op);
        assertEq(addresses.custodyGeneration(),2);
        assertEq(addresses.registrationNonce(),0);assertEq(pool.nonce(),0);
    }
    function testIncidentDomainIsSeparateAndRepliesAreRoleBounded() public {
        BitcoinIncidentMessageBoard b=_incidentBoard();
        assertEq(b.incidentDomain(),keccak256(abi.encode(keccak256("SATOHSI_INCIDENT_MESSAGES_V1"),block.chainid,
            address(b),address(addresses),addresses.graphId(),address(auth))));
        assertTrue(b.incidentDomain()!=b.domain());
        bytes32 request=keccak256("incident report");
        vm.prank(REQUESTER);b.request(request,address(0xa1),1,1,uint64(block.timestamp+600),new bytes(64));
        bytes32 route=b.routeKey(request,REQUESTER,address(0xa1));
        vm.expectRevert("reply role");vm.prank(REQUESTER);b.reply(route,1,0,1,1,uint64(block.timestamp+600),new bytes(64));
        vm.prank(address(0xa1));b.reply(route,1,0,1,1,uint64(block.timestamp+600),new bytes(64));
        vm.expectRevert("kind/sequence");vm.prank(address(0xa1));b.reply(route,2,65,1,1,uint64(block.timestamp+600),new bytes(64));
    }
    function testIncidentExactDeliveryRetryRetainsPacketAfterExpiryAndRotation() public {
        BitcoinIncidentMessageBoard b=_incidentBoard();uint64 expiry=uint64(block.timestamp+600);
        bytes32 request=keccak256("incident");
        vm.prank(REQUESTER);bytes32 id=b.request(request,address(0xa1),1,1,expiry,new bytes(64));
        vm.warp(expiry+1);vm.prank(REQUESTER);b.registerKey(2,bytes32(uint256(44)));
        vm.prank(REQUESTER);assertEq(b.request(request,address(0xa1),1,1,expiry,new bytes(64)),id);
        assertEq(b.messageCount(address(0xa1)),1);
    }
    function testIncidentPauseAndRevokeDoNotExecuteBrokenDerivationVerifier() public {
        vm.etch(address(addresses.verifier()),hex"60006000fd");
        _status(0,2,false);_status(1,2,false);
        assertEq(uint256(addresses.custodyState()),2);assertEq(uint256(addresses.derivationState()),2);
    }
    function testIncidentFrozenPythonStatusAbiAndSelector() public view {
        string memory vector=vm.readFile("test/fixtures/incident-status-v1.json");
        BitcoinAddressRegistry.StatusChange memory c=BitcoinAddressRegistry.StatusChange(0,2,7,3,1,false,2000000000,
            BitcoinAddressRegistry.IncidentReport(bytes32(uint256(1)),bytes32(uint256(2)),bytes32(uint256(3)),
                bytes32(uint256(4)),bytes32(uint256(5)),bytes32(uint256(6))));
        assertEq(keccak256(abi.encode(c)),vm.parseJsonBytes32(vector,".abi_hash"));
        assertEq(BitcoinAddressRegistry.setStatus.selector,bytes4(vm.parseJsonBytes(vector,".selector")));
    }
}
