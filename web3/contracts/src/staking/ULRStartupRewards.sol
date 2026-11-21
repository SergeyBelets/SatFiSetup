// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ULRStaking} from "./ULRStaking.sol";

/// @notice Additional, prefunded introductory rewards; no minting or unfunded debt.
/// @dev APY assumes daily reinvestment at the immutable reference token price.
/// Actual USD claims do not auto-compound. Missed days expire without arrears.
contract ULRStartupRewards is ReentrancyGuard {
    using SafeERC20 for IERC20;
    uint256 private constant WAD = 1e18;
    ULRStaking public immutable staking;
    IERC20 public immutable rewardToken;
    uint256 public immutable apyBps;
    uint256 public immutable dailyRateWad;
    uint256 public immutable unitPrice;
    uint256 public immutable startDay;
    uint256 public immutable endDay;
    address public immutable refundRecipient;
    mapping(uint256 => bool) public processed;
    mapping(uint256 => uint256) public paid;
    event DayPaid(uint256 indexed day, uint256 requested, uint256 funded);
    event UnusedBudgetRefunded(uint256 amount);
    error InvalidConfiguration();
    error InvalidDay();
    error InvalidTransfer();

    constructor(address staking_, uint256 apy, uint256 price, uint256 firstDay, uint256 duration, address refund) {
        if (staking_.code.length == 0 || apy == 0 || apy > 100000 || price == 0 || price >= 2**128
            || duration == 0 || duration > 3650 || refund == address(0)
            || firstDay <= block.timestamp / 1 days || firstDay > type(uint32).max - duration) revert InvalidConfiguration();
        staking = ULRStaking(staking_);
        if (firstDay < staking.firstEligibleDay()) revert InvalidConfiguration();
        rewardToken = staking.rewardToken();apyBps = apy;unitPrice = price;
        startDay = firstDay;endDay = firstDay + duration;refundRecipient = refund;
        uint256 target = WAD + apy * WAD / 10000;
        uint256 low;uint256 high = apy * WAD / 10000 / 365 + 1;
        while (high - low > 1) {
            uint256 middle = (low + high) / 2;
            if (_annualFactor(middle) <= target) low = middle; else high = middle;
        }
        dailyRateWad = low;
    }

    function _annualFactor(uint256 rate) private pure returns (uint256 result) {
        uint256 base = WAD + rate;uint256 exponent = 365;result = WAD;
        while (exponent != 0) {
            if (exponent & 1 != 0) result = Math.mulDiv(result, base, WAD);
            exponent >>= 1;
            if (exponent != 0) base = Math.mulDiv(base, base, WAD);
        }
    }

    function previewDay(uint256 day) public view returns (uint256 requested, uint256 available) {
        uint256 today = block.timestamp / 1 days;
        if (day < startDay || day >= endDay || today == 0 || day != today - 1) revert InvalidDay();
        if (processed[day]) return (0,0);
        uint256 valued = Math.mulDiv(staking.totalEligibleStake(day), unitPrice, WAD);
        requested = Math.mulDiv(valued, dailyRateWad, WAD);
        available = Math.min(requested, rewardToken.balanceOf(address(this)));
    }

    /// @notice Funding is bounded by real budget, once for the previous UTC day.
    function payDay(uint256 day) external nonReentrant returns (uint256 amount) {
        (uint256 requested,uint256 available) = previewDay(day);
        if (processed[day]) return paid[day];
        processed[day] = true;paid[day] = available;
        if (available != 0) {
            rewardToken.forceApprove(address(staking),available);
            staking.fundDay(day,available);
            rewardToken.forceApprove(address(staking),0);
        }
        emit DayPaid(day,requested,available);
        return available;
    }

    /// @notice Unused funds return only to the immutable payer after the final payout window.
    function refundUnused() external nonReentrant returns (uint256 amount) {
        if (block.timestamp / 1 days <= endDay) revert InvalidDay();
        amount = rewardToken.balanceOf(address(this));
        if (amount != 0) {
            uint256 beforeBalance = rewardToken.balanceOf(refundRecipient);
            rewardToken.safeTransfer(refundRecipient,amount);
            if (rewardToken.balanceOf(address(this)) != 0 || rewardToken.balanceOf(refundRecipient) - beforeBalance != amount) revert InvalidTransfer();
        }
        emit UnusedBudgetRefunded(amount);
    }
}
