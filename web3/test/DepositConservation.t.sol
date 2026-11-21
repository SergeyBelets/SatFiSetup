// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./EscrowBoundary.t.sol";
import "../contracts/src/federation/BitcoinDepositRegistry.sol";

contract DepositConservationTest is Test {
    BitcoinDepositRegistry internal registry;
    DepositReservationFixture public batches;
    bytes internal script=hex"51201111111111111111111111111111111111111111111111111111111111111111";
    function setUp() public {
        batches=new DepositReservationFixture();
        registry=new BitcoinDepositRegistry(address(new EscrowProofFixture()),address(new EscrowAuthorizationFixture()),6,address(this));
        registry.register(address(this),script,registry.getRegisterOperationHash(address(this),script));
    }
    function raw(bytes32 prev,uint64 amount) internal view returns(bytes memory) {
        bytes8 little;
        for(uint256 i;i<8;i++) little|=bytes8(uint64(uint8(amount>>(i*8)))<<((7-i)*8));
        return abi.encodePacked(hex"0200000001",prev,hex"0000000000ffffffff01",little,bytes1(uint8(script.length)),script,hex"00000000");
    }
    function txid(bytes memory transaction) internal pure returns(bytes32) { return sha256(abi.encodePacked(sha256(transaction))); }
    function testFuzzPrincipalCannotPayMinerFeeOrMintChange(uint32 amount,uint32 burn) public {
        amount=uint32(bound(amount,2,1000000)); burn=uint32(bound(burn,1,amount-1));
        bytes memory deposit=raw(bytes32(uint256(9)),amount);
        registry.consume(deposit,0,bytes32(uint256(1)),new bytes32[](0),0);
        bytes32 input=keccak256(abi.encode(txid(deposit),uint32(0)));
        bytes memory shortChange=raw(txid(deposit),amount-burn-1);
        vm.expectRevert("miner fee consumes principal");registry.recordSpend(shortChange,burn);
        assertEq(registry.backingSats(input),amount);
        bytes memory paid=raw(txid(deposit),amount-burn);
        registry.recordSpend(paid,burn);
        bytes32 change=keccak256(abi.encode(txid(paid),uint32(0)));
        assertEq(registry.backingSats(input),0);assertEq(registry.backingSats(change),amount-burn);
        vm.expectRevert("outpoint consumed or change");registry.consume(paid,0,bytes32(uint256(1)),new bytes32[](0),0);
        vm.expectRevert("outpoint consumed or change");registry.consume(deposit,0,bytes32(uint256(1)),new bytes32[](0),0);
    }
    function testReservedInputCannotMintBeforeSettlement() public {
        bytes memory deposit=raw(bytes32(uint256(9)),1000);
        bytes32 wire=txid(deposit);bytes32 display;
        for(uint256 i;i<32;i++) display|=bytes32(uint256(uint8(wire[i]))<<(i*8));
        batches.reserve(keccak256(abi.encode(display,uint32(0))));
        vm.expectRevert("outpoint reserved for spend");registry.consume(deposit,0,bytes32(uint256(1)),new bytes32[](0),0);
    }
    function testFundingWithReservedWalletInputCannotBecomeNewBacking() public {
        bytes memory deposit=raw(bytes32(uint256(9)),1000);
        batches.reserve(keccak256(abi.encode(BitcoinTxParser.flipBytes32(bytes32(uint256(9))),uint32(0))));
        vm.expectRevert("wallet input reserved for spend");
        registry.consume(deposit,0,bytes32(uint256(1)),new bytes32[](0),0);
        assertFalse(registry.consumed(keccak256(abi.encode(txid(deposit),uint32(0)))));
    }
}
