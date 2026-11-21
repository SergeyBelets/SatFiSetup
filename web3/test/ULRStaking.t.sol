// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CustodianDAOToken} from "../contracts/src/tokens/CustodianDAOToken.sol";
import {ULRStaking} from "../contracts/src/staking/ULRStaking.sol";
import {DeployULRStaking} from "../script/DeployULRStaking.s.sol";

contract ULRRewardTestToken is ERC20 {
    bool public taxEnabled;

    constructor() ERC20("Test USD", "USD") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address recipient, uint256 amount) external {
        _mint(recipient, amount);
    }

    function setTax(bool enabled) external {
        taxEnabled = enabled;
    }

    function _update(address from, address to, uint256 value) internal override {
        if (taxEnabled && from != address(0) && to != address(0) && value > 0) {
            super._update(from, address(0), 1);
            value--;
        }
        super._update(from, to, value);
    }
}

contract ULRStakingTest is Test {
    CustodianDAOToken private ulr;
    ULRRewardTestToken private usd;
    ULRStaking private staking;
    address private alice = address(0xA11CE);
    address private bob = address(0xB0B);
    address private sourceA = address(0xCA);
    address private sourceB = address(0xCB);
    uint256 private day = 101;

    function setUp() public {
        vm.warp(100 days + 12 hours);
        ulr = new CustodianDAOToken("ULR", "ULR", address(this));
        usd = new ULRRewardTestToken();
        staking = new ULRStaking(address(ulr), address(usd), 200_000 ether, 1);
        ulr.transfer(alice, 1_000 ether);
        ulr.transfer(bob, 1_000 ether);
        vm.prank(alice);
        ulr.approve(address(staking), type(uint256).max);
        vm.prank(bob);
        ulr.approve(address(staking), type(uint256).max);
        usd.mint(sourceA, 1_000_000e6);
        usd.mint(sourceB, 1_000_000e6);
        vm.prank(sourceA);
        usd.approve(address(staking), type(uint256).max);
        vm.prank(sourceB);
        usd.approve(address(staking), type(uint256).max);
    }

    function test_RequiresDistinctContractTokens() public {
        vm.expectRevert(ULRStaking.InvalidAddress.selector);
        new ULRStaking(address(0), address(usd), 200_000 ether, 1);
        vm.expectRevert(ULRStaking.InvalidAddress.selector);
        new ULRStaking(address(ulr), alice, 200_000 ether, 1);
        vm.expectRevert(ULRStaking.InvalidAddress.selector);
        new ULRStaking(address(ulr), address(ulr), 200_000 ether, 1);
    }

    function test_FixedSupplyULRIsReusedWithoutNewIssuance() public view {
        assertEq(address(staking.stakeToken()), address(ulr));
        assertEq(address(staking.rewardToken()), address(usd));
        assertEq(ulr.totalSupply(), 10_000_000 ether);
        assertEq(staking.firstEligibleDay(), day);
    }

    function test_DeploymentScriptReusesPinnedTokensAndDoesNotMint() public {
        _deploymentEnvironment();
        uint256 supply = ulr.totalSupply();
        ULRStaking deployed = new DeployULRStaking().run();
        assertEq(address(deployed.stakeToken()), address(ulr));
        assertEq(address(deployed.rewardToken()), address(usd));
        assertEq(deployed.firstEligibleDay(), day);
        assertEq(deployed.totalStaked(), 0);
        assertEq(ulr.totalSupply(), supply);
    }

    function test_DeploymentScriptRejectsWrongChainAndRuntime() public {
        _deploymentEnvironment();
        DeployULRStaking script = new DeployULRStaking();
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.expectRevert("chain mismatch");
        script.run();
        vm.setEnv("EXPECTED_CHAIN_ID", vm.toString(block.chainid));
        vm.setEnv("ULR_REWARD_TOKEN_CODE_HASH", vm.toString(bytes32(uint256(1))));
        vm.expectRevert("reward code hash");
        script.run();
    }

    function test_MultipleSourcesFundSameDayAndPayProportionally() public {
        _stake(alice, 100 ether);
        _stake(bob, 300 ether);
        _finishDay();
        _fund(sourceA, 400e6);
        _fund(sourceB, 800e6);
        assertEq(staking.sourceDayFunding(day, sourceA), 400e6);
        assertEq(staking.sourceDayFunding(day, sourceB), 800e6);
        assertEq(staking.dayReward(day), 1_200e6);
        assertEq(_claim(alice), 300e6);
        assertEq(_claim(bob), 900e6);
        assertEq(usd.balanceOf(alice), 300e6);
        assertEq(usd.balanceOf(bob), 900e6);
        assertEq(staking.rewardLiability(), 0);
        assertEq(ulr.balanceOf(address(staking)), staking.totalStaked());
    }

    function test_LateSourceAndRepeatedTopUpsRemainClaimableAfterUnstake() public {
        _stake(alice, 100 ether);
        _stake(bob, 300 ether);
        _finishDay();
        _fund(sourceA, 400e6);
        assertEq(_claim(alice), 100e6);
        vm.prank(alice);
        staking.unstake(100 ether);
        vm.warp(block.timestamp + 90 days);
        _fund(sourceB, 800e6);
        _fund(sourceA, 400e6);
        assertEq(staking.claimableReward(alice, _days(day)), 300e6);
        assertEq(_claim(alice), 300e6);
        assertEq(_claim(alice), 0);
        assertEq(_claim(bob), 1_200e6);
        assertEq(usd.balanceOf(alice), 400e6);
        assertEq(staking.rewardLiability(), 0);
    }

    function test_UnfundedDayClaimDoesNotLoseFutureEntitlement() public {
        _stake(alice, 100 ether);
        _finishDay();
        assertEq(_claim(alice), 0);
        _fund(sourceA, 1e6);
        assertEq(_claim(alice), 1e6);
    }

    function test_StakeAfterMidnightStartsFollowingDay() public {
        vm.warp(day * 1 days + 1);
        _stake(alice, 100 ether);
        assertEq(staking.eligibleStake(alice, day), 0);
        vm.warp((day + 1) * 1 days);
        assertEq(staking.eligibleStake(alice, day + 1), 100 ether);
    }

    function test_WithdrawNewStakeFirstAndPreserveEarlierEligibility() public {
        _stake(alice, 100 ether);
        vm.warp(day * 1 days + 1 hours);
        _stake(alice, 50 ether);
        _stake(bob, 100 ether);
        vm.prank(alice);
        staking.unstake(60 ether);
        vm.prank(bob);
        staking.unstake(30 ether);
        assertEq(staking.eligibleStake(alice, day), 90 ether);
        assertEq(staking.eligibleStake(bob, day), 0);
        assertEq(staking.totalEligibleStake(day), 90 ether);
        _finishDay();
        _fund(sourceA, 90e6);
        assertEq(_claim(alice), 90e6);
        assertEq(_claim(bob), 0);
    }

    function test_ExactMidnightStakeCountsButWithdrawalCannotBeRestored() public {
        vm.warp(day * 1 days);
        _stake(alice, 100 ether);
        vm.prank(alice);
        staking.unstake(40 ether);
        _stake(alice, 40 ether);
        assertEq(staking.eligibleStake(alice, day), 60 ether);
        assertEq(staking.totalEligibleStake(day), 60 ether);
        vm.warp((day + 1) * 1 days);
        assertEq(staking.eligibleStake(alice, day + 1), 100 ether);
    }

    function test_DeploymentAtMidnightAllowsFirstDay() public {
        vm.warp(day * 1 days);
        ULRStaking midnightStaking = new ULRStaking(address(ulr), address(usd), 200_000 ether, 1);
        assertEq(midnightStaking.firstEligibleDay(), day);
        ulr.approve(address(midnightStaking), 1 ether);
        midnightStaking.stake(1 ether);
        assertEq(midnightStaking.eligibleStake(address(this), day), 1 ether);
    }

    function test_SameDayRoundTripCannotEarnOrStrandPrincipal() public {
        _stake(alice, 100 ether);
        vm.prank(alice);
        staking.unstake(100 ether);
        _finishDay();
        vm.expectRevert(ULRStaking.NoEligibleStake.selector);
        _fund(sourceA, 10e6);
        assertEq(staking.totalStaked(), 0);
        assertEq(ulr.balanceOf(alice), 1_000 ether);
        assertEq(usd.balanceOf(sourceA), 1_000_000e6);
    }

    function test_FundingRejectsCurrentFutureAndPreDeploymentDays() public {
        _stake(alice, 100 ether);
        _finishDay();
        uint256 today = block.timestamp / 1 days;
        vm.startPrank(sourceA);
        vm.expectRevert(ULRStaking.InvalidDay.selector);
        staking.fundDay(today, 1);
        vm.expectRevert(ULRStaking.InvalidDay.selector);
        staking.fundDay(type(uint256).max, 1);
        vm.expectRevert(ULRStaking.InvalidDay.selector);
        staking.fundDay(day - 1, 1);
        vm.expectRevert(ULRStaking.InvalidAmount.selector);
        staking.fundDay(day, 0);
        vm.stopPrank();
    }

    function test_DirectTransfersDoNotChangeRewardOrStakeAccounting() public {
        _stake(alice, 100 ether);
        _finishDay();
        ulr.transfer(address(staking), 5 ether);
        vm.prank(sourceA);
        usd.transfer(address(staking), 50e6);
        assertEq(staking.totalStaked(), 100 ether);
        assertEq(staking.dayReward(day), 0);
        assertEq(_claim(alice), 0);
        _fund(sourceA, 20e6);
        assertEq(_claim(alice), 20e6);
        assertEq(usd.balanceOf(address(staking)), 50e6);
    }

    function test_ClaimBatchMustBeBoundedAscendingAndCompleted() public {
        _stake(alice, 100 ether);
        _finishDay();
        uint256[] memory batch = new uint256[](0);
        vm.expectRevert(ULRStaking.InvalidClaimBatch.selector);
        staking.claim(batch);
        batch = new uint256[](33);
        vm.expectRevert(ULRStaking.InvalidClaimBatch.selector);
        staking.claim(batch);
        batch = new uint256[](2);
        batch[0] = day;
        batch[1] = day;
        vm.expectRevert(ULRStaking.InvalidClaimBatch.selector);
        staking.claim(batch);
        vm.expectRevert(ULRStaking.InvalidDay.selector);
        staking.claim(_days(day + 1));
        vm.expectRevert(ULRStaking.InvalidDay.selector);
        staking.claimableReward(alice, _days(day - 1));
        vm.warp((day + 2) * 1 days);
        batch[0] = day + 1;
        batch[1] = day;
        vm.expectRevert(ULRStaking.InvalidClaimBatch.selector);
        staking.claim(batch);
    }

    function test_ClaimSeveralDaysAndKeepHistoricalStakeAfterFullWithdrawal() public {
        _stake(alice, 100 ether);
        _finishDay();
        _fund(sourceA, 10e6);
        vm.warp((day + 2) * 1 days);
        vm.prank(sourceB);
        staking.fundDay(day + 1, 20e6);
        vm.prank(alice);
        staking.unstake(100 ether);
        uint256[] memory batch = new uint256[](2);
        batch[0] = day;
        batch[1] = day + 1;
        vm.prank(alice);
        assertEq(staking.claim(batch), 30e6);
        assertEq(staking.totalStaked(), 0);
        assertEq(staking.totalFundedRewards(), staking.totalClaimedRewards());
    }

    function test_NoCallerCanUnstakeOrClaimAnotherOwnersBalance() public {
        _stake(alice, 100 ether);
        _finishDay();
        _fund(sourceA, 10e6);
        vm.prank(bob);
        vm.expectRevert(ULRStaking.InvalidAmount.selector);
        staking.unstake(1);
        assertEq(_claim(bob), 0);
        assertEq(staking.stakedBalance(alice), 100 ether);
        assertEq(_claim(alice), 10e6);
    }

    function test_FailedFundingCannotCreateRewardsOrPullOtherPayersAllowance() public {
        _stake(alice, 100 ether);
        _finishDay();
        vm.prank(alice);
        vm.expectRevert();
        staking.fundDay(day, 10e6);
        assertEq(staking.dayReward(day), 0);
        assertEq(staking.totalFundedRewards(), 0);
        assertEq(usd.balanceOf(sourceA), 1_000_000e6);
    }

    function test_TaxedFundingAndPayoutRevertAtomically() public {
        _stake(alice, 100 ether);
        _finishDay();
        usd.setTax(true);
        vm.expectRevert(ULRStaking.InvalidTransfer.selector);
        _fund(sourceA, 10e6);
        assertEq(staking.totalFundedRewards(), 0);
        usd.setTax(false);
        _fund(sourceA, 10e6);
        usd.setTax(true);
        vm.prank(alice);
        vm.expectRevert(ULRStaking.InvalidTransfer.selector);
        staking.claim(_days(day));
        assertEq(staking.claimedReward(alice, day), 0);
        assertEq(staking.totalClaimedRewards(), 0);
        usd.setTax(false);
        assertEq(_claim(alice), 10e6);
    }

    function test_TaxedStakeCannotCreateUnbackedPrincipal() public {
        ULRStaking taxedStaking = new ULRStaking(address(usd), address(ulr), 200_000 ether, 1);
        usd.setTax(true);
        vm.startPrank(sourceA);
        usd.approve(address(taxedStaking), 10e6);
        vm.expectRevert(ULRStaking.InvalidTransfer.selector);
        taxedStaking.stake(10e6);
        vm.stopPrank();
        assertEq(taxedStaking.totalStaked(), 0);
        assertEq(usd.balanceOf(address(taxedStaking)), 0);
    }

    function testFuzz_LateFundingRoundingNeverOverpays(uint128 first, uint128 second) public {
        uint256 a = bound(uint256(first), 1, 1_000e6);
        uint256 b = bound(uint256(second), 1, 1_000e6);
        _stake(alice, 1 ether);
        _stake(bob, 2 ether);
        _finishDay();
        _fund(sourceA, a);
        _claim(alice);
        _claim(bob);
        _fund(sourceB, b);
        _claim(bob);
        _claim(alice);
        assertEq(usd.balanceOf(alice), (a + b) / 3);
        assertEq(usd.balanceOf(bob), (a + b) * 2 / 3);
        assertLe(staking.totalClaimedRewards(), a + b);
        assertEq(staking.rewardLiability(), usd.balanceOf(address(staking)));
        assertLe(staking.rewardLiability(), 1);
        assertEq(_claim(alice), 0);
        assertEq(_claim(bob), 0);
    }

    function _stake(address owner, uint256 amount) private {
        vm.prank(owner);
        staking.stake(amount);
    }

    function _finishDay() private {
        vm.warp((day + 1) * 1 days);
    }

    function _fund(address source, uint256 amount) private {
        vm.prank(source);
        staking.fundDay(day, amount);
    }

    function _claim(address owner) private returns (uint256) {
        vm.prank(owner);
        return staking.claim(_days(day));
    }

    function _days(uint256 value) private pure returns (uint256[] memory result) {
        result = new uint256[](1);
        result[0] = value;
    }

    function _deploymentEnvironment() private {
        vm.setEnv("EXPECTED_CHAIN_ID", vm.toString(block.chainid));
        vm.setEnv("ULR_TOKEN", vm.toString(address(ulr)));
        vm.setEnv("ULR_REWARD_TOKEN", vm.toString(address(usd)));
        vm.setEnv("ULR_TOKEN_CODE_HASH", vm.toString(address(ulr).codehash));
        vm.setEnv("ULR_REWARD_TOKEN_CODE_HASH", vm.toString(address(usd).codehash));
        vm.setEnv("PRIVATE_KEY", vm.toString(uint256(0x1234)));
    }
}
