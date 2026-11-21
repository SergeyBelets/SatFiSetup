// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import {Test} from "forge-std/Test.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {CustodianDAOFeeRouter} from "../contracts/src/federation/CustodianDAOFeeRouter.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {ULRTreasuryVerifierFixture,ULRTreasuryCoordinatorFixture,ULRTreasuryUSD} from "./ULRTreasury.t.sol";

/// Test-only custom initcode: genuine runtime with attacker-initialized mutable storage.
contract ReviewReturnedRuntime {
    constructor(bytes memory runtime,bytes32[] memory slots,bytes32[] memory values) {
        require(slots.length==values.length,"length");
        for(uint256 i;i<slots.length;++i) {bytes32 slot=slots[i];bytes32 value=values[i];assembly{sstore(slot,value)}}
        assembly{return(add(runtime,32),mload(runtime))}
    }
}
contract ReviewCounterfeitTreasury { }
contract ReviewLogicOne {function value() external pure returns(uint256) {return 1;}}
contract ReviewLogicTwo {function value() external pure returns(uint256) {return 2;}}
contract ReviewMutableProxy {
    address public immutable owner=msg.sender;
    address public implementation;
    constructor(address next) {implementation=next;}
    function upgrade(address next) external {require(msg.sender==owner,"owner");implementation=next;}
    fallback() external payable {
        address target=implementation;
        assembly {calldatacopy(0,0,calldatasize()) let ok:=delegatecall(gas(),target,0,calldatasize(),0,0)
            returndatacopy(0,0,returndatasize()) if iszero(ok) {revert(0,returndatasize())} return(0,returndatasize())}
    }
}
contract ContractSubstitutionReview20261103Test is Test {
    function _authority() private returns(FROSTAuthorization auth,ULRTreasuryCoordinatorFixture coordinator) {
        coordinator=new ULRTreasuryCoordinatorFixture(address(new ULRTreasuryVerifierFixture()),address(this));
        address[] memory owners=new address[](1);owners[0]=address(this);
        auth=new FROSTAuthorization(address(coordinator),abi.encodePacked(bytes32(uint256(1))),owners,1);
    }
    function test_SameRuntimeHashDoesNotAuthenticateConstructorStorage() public {
        (FROSTAuthorization genuine,)=_authority();bytes32 op=keccak256("unapproved fixture operation");address target=address(0x1234);
        bytes32[] memory slots=new bytes32[](7);bytes32[] memory values=new bytes32[](7);
        // Storage layout is independently recorded from forge inspect FROSTAuthorization.
        slots[0]=bytes32(uint256(0));values[0]=bytes32(uint256(65)); // bytes length 32
        slots[1]=keccak256(abi.encode(uint256(0)));values[1]=bytes32(uint256(1));
        slots[2]=bytes32(uint256(1));values[2]=bytes32(uint256(1)); // participants.length
        slots[3]=keccak256(abi.encode(uint256(1)));values[3]=bytes32(uint256(uint160(address(this))));
        slots[4]=keccak256(abi.encode(address(this),uint256(2)));values[4]=bytes32(uint256(1));
        slots[5]=keccak256(abi.encode(op,uint256(4)));values[5]=bytes32(uint256(uint160(target)));
        slots[6]=keccak256(abi.encode(op,uint256(5)));values[6]=bytes32(uint256(1));
        FROSTAuthorization fake=FROSTAuthorization(address(new ReviewReturnedRuntime(address(genuine).code,slots,values)));
        assertEq(address(fake).codehash,address(genuine).codehash);
        assertEq(fake.groupPubkey(),genuine.groupPubkey());assertEq(fake.getParticipants(),genuine.getParticipants());
        assertEq(fake.threshold(),genuine.threshold());assertEq(address(fake.coordinator()),address(genuine.coordinator()));
        assertFalse(genuine.isOperationApproved(op,target));assertTrue(fake.isOperationApproved(op,target));
        vm.prank(target);assertTrue(fake.consumeOperation(op));
        // This creates another address. It does not replace the genuine contract.
        assertFalse(genuine.approvedOperations(op));
    }
    function test_RealRouterAcceptsApprovedCounterfeitTreasuryAndRedirectsNewFees() public {
        (FROSTAuthorization auth,ULRTreasuryCoordinatorFixture c)=_authority();
        ULRTreasuryUSD usd=new ULRTreasuryUSD(6);CustodianDAOIncomeVault vault=new CustodianDAOIncomeVault(address(usd),address(auth));
        address original=address(0xA11CE);CustodianDAOFeeRouter router=new CustodianDAOFeeRouter(address(vault),original,0,0);
        ReviewCounterfeitTreasury fake=new ReviewCounterfeitTreasury();
        bytes32 op=router.getSetTreasuryOperationHash(address(fake));
        vm.expectRevert();router.setTreasury(address(fake),op); // no quorum approval, no substitution
        c.set(op,address(router));auth.authorizeOperation(1,op,address(router));router.setTreasury(address(fake),op);
        op=router.getSetPayerOperationHash(address(this),true);c.set(op,address(router));auth.authorizeOperation(2,op,address(router));
        router.setPayer(address(this),true,op);usd.mint(address(this),100e6);usd.approve(address(router),100e6);
        router.depositFeeV1(block.timestamp/1 days,CustodianDAOFeeRouter.FeeStream.Wrap,100e6);
        assertEq(usd.balanceOf(address(fake)),100e6);assertEq(usd.balanceOf(original),0);
    }
}
