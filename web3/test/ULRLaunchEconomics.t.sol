// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import {Test} from "forge-std/Test.sol";
import {ULRStartupRewards} from "../contracts/src/staking/ULRStartupRewards.sol";
import {ULRCustodianSnapshots} from "../contracts/src/staking/ULRCustodianSnapshots.sol";
import {ULRStaking,ULRRewardTestToken,CustodianDAOToken} from "./ULRStaking.t.sol";

contract ULRLaunchEconomicsTest is Test {
    CustodianDAOToken token;ULRRewardTestToken usd;ULRStaking staking;ULRStartupRewards program;
    address alice=address(11);address bob=address(12);address refund=address(13);
    function setUp() public {
        vm.warp(100 days+1);
        token=new CustodianDAOToken("Launch Governance","LGOV",address(this));usd=new ULRRewardTestToken();
        staking=new ULRStaking(address(token),address(usd),500_000 ether,100 ether);
        program=new ULRStartupRewards(address(staking),1000,1e6,102,30,refund);
        token.transfer(alice,500_000 ether);token.transfer(bob,1000 ether);
        vm.prank(alice);token.approve(address(staking),type(uint256).max);
        vm.prank(bob);token.approve(address(staking),type(uint256).max);
        vm.prank(alice);staking.stake(1000 ether);vm.prank(bob);staking.stake(1000 ether);
    }
    function testCustomThresholdMinimumAndFreeWithdrawal() public {
        assertTrue(staking.isCustodianCandidate(alice));assertFalse(staking.isCustodianCandidate(bob));
        vm.prank(alice);vm.expectRevert(ULRStaking.InvalidAmount.selector);staking.stake(99 ether);
        vm.prank(alice);staking.unstake(1);assertEq(staking.stakedBalance(alice),1000 ether-1);
        ULRCustodianSnapshots snapshots=new ULRCustodianSnapshots(address(staking),address(usd));
        assertEq(snapshots.MIN_CUSTODIAN_ULR(),500_000 ether);
    }
    function testPrefundedAPYFlowsIntoNormalClaimAndAddsToTreasuryIncome() public {
        usd.mint(address(program),1000e6);vm.warp(103 days);
        (uint256 requested,uint256 available)=program.previewDay(102);assertEq(requested,available);assertGt(requested,0);
        uint256 funded=program.payDay(102);assertEq(funded,requested);assertEq(program.payDay(102),funded);
        usd.mint(address(this),10e6);usd.approve(address(staking),10e6);staking.fundDay(102,10e6);
        uint256[] memory days_=new uint256[](1);days_[0]=102;
        vm.prank(alice);assertEq(staking.claim(days_),(funded+10e6)/2);
        vm.prank(bob);assertEq(staking.claim(days_),(funded+10e6)/2);
        assertLe(staking.totalClaimedRewards(),staking.totalFundedRewards());
        vm.prank(alice);assertEq(staking.claim(days_),0);
    }
    function testEmptyBudgetCreatesNoDebtAndTopupOnlyPaysNextDay() public {
        vm.warp(103 days);assertEq(program.payDay(102),0);assertTrue(program.processed(102));
        usd.mint(address(program),100e6);assertEq(program.payDay(102),0);assertEq(staking.dayReward(102),0);
        vm.warp(104 days);assertGt(program.payDay(103),0);
        vm.expectRevert(ULRStartupRewards.InvalidDay.selector);program.payDay(102);
    }
    function testInsufficientBudgetCapsFundingAndDoesNotPullOtherBalances() public {
        usd.mint(address(program),1);usd.mint(address(staking),100e6);vm.warp(103 days);
        assertEq(program.payDay(102),1);assertEq(usd.balanceOf(address(program)),0);
        assertEq(staking.totalFundedRewards(),1);assertEq(usd.balanceOf(address(staking)),100e6+1);
    }
    function testRefundIsDelayedAndRecipientImmutable() public {
        usd.mint(address(program),123e6);vm.warp(132 days);
        vm.expectRevert(ULRStartupRewards.InvalidDay.selector);program.refundUnused();
        vm.warp(133 days);vm.prank(alice);assertEq(program.refundUnused(),123e6);
        assertEq(usd.balanceOf(refund),123e6);assertEq(usd.balanceOf(alice),0);
    }
    function testAPYDailyRateMatchesIndependentDecimalReference() public view {
        // floor((1.10^(1/365)-1)*1e18), independent high-precision Decimal reference.
        assertApproxEqAbs(program.dailyRateWad(),261157876067812,2);
    }
    function testRejectsPastStartAndUnboundedEconomicParameters() public {
        vm.expectRevert(ULRStartupRewards.InvalidConfiguration.selector);
        new ULRStartupRewards(address(staking),1000,1e6,100,30,refund);
        vm.expectRevert(ULRStartupRewards.InvalidConfiguration.selector);
        new ULRStartupRewards(address(staking),100001,1e6,102,30,refund);
        vm.expectRevert(ULRStaking.InvalidAmount.selector);
        new ULRStaking(address(token),address(usd),0,1);
    }
}
