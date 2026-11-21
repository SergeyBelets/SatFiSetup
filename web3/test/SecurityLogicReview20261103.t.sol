// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {CustodianDAOToken} from "../contracts/src/tokens/CustodianDAOToken.sol";
import {ULRStaking} from "../contracts/src/staking/ULRStaking.sol";
import {ULRCustodianSnapshots} from "../contracts/src/staking/ULRCustodianSnapshots.sol";
import {ULRConfederationTreasury} from "../contracts/src/staking/ULRConfederationTreasury.sol";
import {ULRTreasuryVerifierFixture, ULRTreasuryCoordinatorFixture, ULRTreasuryFactoryFixture, ULRTreasuryUSD} from "./ULRTreasury.t.sol";

contract SecurityLogicReview20261103Test is Test {
    function test_UnprivilegedZeroAttemptAllowsSameDayFunding() public {
        vm.warp(100 days + 12 hours);vm.roll(500);
        ULRTreasuryCoordinatorFixture c=new ULRTreasuryCoordinatorFixture(address(new ULRTreasuryVerifierFixture()),address(this));
        address[] memory members=new address[](1);members[0]=address(this);
        FROSTAuthorization auth=new FROSTAuthorization(address(c),abi.encodePacked(bytes32(uint256(1))),members,1);
        CustodianValidator validator=new CustodianValidator(members,1,address(auth));
        ULRTreasuryFactoryFixture factory=new ULRTreasuryFactoryFixture();factory.configure("review",address(auth),address(validator));
        CustodianDAOToken ulr=new CustodianDAOToken("ULR","ULR",address(this));
        ULRTreasuryUSD usd=new ULRTreasuryUSD(6);ULRStaking staking=new ULRStaking(address(ulr),address(usd), 200_000 ether, 1);
        ULRCustodianSnapshots snapshots=new ULRCustodianSnapshots(address(staking),address(auth));
        ULRConfederationTreasury treasury=new ULRConfederationTreasury(address(factory),"review",address(snapshots), 1000 * 10**6, 9000);
        bytes32 op=snapshots.getEnrollTreasuryOperationHash(address(factory),"review",address(treasury));
        c.set(op,address(snapshots));auth.authorizeOperation(1,op,address(snapshots));validator.registerCustodianSignature(op);
        snapshots.enrollTreasury(address(factory),"review",address(treasury),op);
        op = snapshots.getActivateTreasuryOperationHash(address(treasury));
        c.set(op, address(snapshots));
        auth.authorizeOperation(2, op, address(snapshots));
        snapshots.activateTreasury(address(treasury), op);
        ulr.approve(address(staking),100 ether);staking.stake(100 ether);
        vm.warp(102 days);
        ULRCustodianSnapshots.Report memory report=ULRCustodianSnapshots.Report(101,1,keccak256("anchor"),keccak256("evidence"));
        uint256[] memory weights=new uint256[](1);weights[0]=10_000_000 ether;
        ULRCustodianSnapshots.Allocation[] memory allocation=new ULRCustodianSnapshots.Allocation[](1);
        allocation[0]=ULRCustodianSnapshots.Allocation(treasury.scope(),abi.encodePacked(uint16(0)),keccak256("roster"));
        op=snapshots.getPublishDayOperationHash(report,members,weights,allocation);
        c.set(op,address(snapshots));auth.authorizeOperation(3,op,address(snapshots));snapshots.publishDay(report,members,weights,allocation,op);
        address outsider=address(0xBAD);assertFalse(validator.isCustodian(outsider));
        vm.prank(outsider);assertEq(treasury.payDay(101),0);
        (bool processedBefore,,,,,,)=treasury.settlements(101);assertFalse(processedBefore);
        usd.mint(address(this),11_000e6);usd.approve(address(treasury),11_000e6);treasury.deposit(11_000e6);
        assertEq((usd.balanceOf(address(treasury))-treasury.reserveFloor())*9/10,9_000e6);
        assertEq(treasury.payDay(101),9_000e6);assertEq(staking.dayReward(101),9_000e6);
        assertEq(treasury.payDay(101),9_000e6);
        assertEq(usd.balanceOf(address(treasury)),2_000e6);
        (bool processed,,,,,,)=treasury.settlements(101);assertTrue(processed);
    }
}
