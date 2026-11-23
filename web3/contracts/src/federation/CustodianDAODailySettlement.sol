// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianDAOStaking} from "./CustodianDAOStaking.sol";
import {CustodianDAORateController} from "./CustodianDAORateController.sol";
import {ChainlinkBTCDailyTWAP} from "./ChainlinkBTCDailyTWAP.sol";
import {CustodianDAOIncomeVault} from "./CustodianDAOIncomeVault.sol";

contract CustodianDAODailySettlement is ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant WRAP_FEE = keccak256("WRAP_FEE");
    bytes32 public constant DEPLOY_FEE = keccak256("DEPLOY_FEE");

    struct IncomeLine {
        bytes32 sourceId;
        uint256 amount;
    }

    struct LiquidityProvider {
        address account;
        uint256 liquidityWeight;
    }

    struct DayReport {
        uint256 day;
        uint256 averageBtcSats;
        uint256 stablecoinTvl;
        uint256 openingLiquidityReserveSats;
        uint256 stakerWrapFeeIncome;
        uint256 stakerDeployFeeIncome;
        bytes32 evidenceHash;
        IncomeLine[] income;
        LiquidityProvider[] providers;
    }

    struct Settlement {
        uint256 btcPrice8;
        uint256 tvl;
        uint256 baseTarget;
        uint256 actualIncome;
        uint256 basePaid;
        uint256 excess;
        uint256 stakerIncome;
        uint256 lpIncome;
        uint256 reserveTarget;
        uint256 reserveBalanceAfter;
        uint256 stakerWrapFeeIncome;
        uint256 stakerDeployFeeIncome;
        bytes32 evidenceHash;
        uint256 treasuryIncome;
    }

    IERC20 public immutable rewardToken;
    CustodianDAOStaking public immutable staking;
    CustodianDAORateController public immutable controller;
    FROSTAuthorization public immutable authorization;
    ChainlinkBTCDailyTWAP public immutable twap;
    CustodianDAOIncomeVault public immutable incomeVault;
    address public immutable treasury;
    uint256 public immutable rewardScale;
    uint256 public nextSettlementDay;
    uint256 public reserveBalance;
    mapping(address => uint256) public lpClaimable;
    mapping(uint256 => Settlement) public settlements;

    event DaySettled(
        uint256 indexed day, uint256 actualIncome, uint256 stakerIncome, uint256 lpIncome, uint256 reserveBalance
    );
    event ReserveFunded(address indexed funder, uint256 amount);
    event LiquidityIncomeClaimed(address indexed provider, uint256 amount);
    event TreasuryIncomeTransferred(uint256 indexed day, uint256 amount);

    error InvalidAddress();
    error InvalidRewardDecimals();
    error InvalidDay();
    error InvalidReport();
    error DuplicateSource();
    error DuplicateProvider();
    error InvalidTransfer();

    constructor(
        address rewardToken_,
        address staking_,
        address controller_,
        address authorization_,
        address twap_,
        address incomeVault_,
        address treasury_
    ) {
        if (
            rewardToken_ == address(0) || staking_ == address(0) || controller_ == address(0)
                || authorization_ == address(0) || twap_ == address(0) || incomeVault_ == address(0)
                || treasury_ == address(0) || treasury_ == address(this)
        ) revert InvalidAddress();
        uint8 decimals = IERC20Metadata(rewardToken_).decimals();
        if (decimals > 18) revert InvalidRewardDecimals();
        rewardToken = IERC20(rewardToken_);
        staking = CustodianDAOStaking(staking_);
        controller = CustodianDAORateController(controller_);
        authorization = FROSTAuthorization(authorization_);
        twap = ChainlinkBTCDailyTWAP(twap_);
        incomeVault = CustodianDAOIncomeVault(incomeVault_);
        treasury = treasury_;
        if (address(staking.rewardToken()) != rewardToken_ || address(controller.staking()) != staking_) {
            revert InvalidAddress();
        }
        if (
            address(incomeVault.rewardToken()) != rewardToken_ || address(incomeVault.authorization()) != authorization_
        ) revert InvalidAddress();
        rewardScale = 10 ** decimals;
        nextSettlementDay = staking.firstEligibleDay();
        rewardToken.forceApprove(staking_, type(uint256).max);
    }

    function getSettlementOperationHash(DayReport calldata report, uint80[] calldata roundIds)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this), this.settleDay.selector, keccak256(abi.encode(report, roundIds)), report.day
        );
    }

    function settleDay(DayReport calldata report, uint80[] calldata roundIds, bytes32 operationHash)
        external
        nonReentrant
    {
        if (report.day != nextSettlementDay || block.timestamp < (report.day + 1) * 1 days) revert InvalidDay();
        if (
            report.evidenceHash == bytes32(0) || report.income.length > incomeVault.MAX_YIELD_SOURCES_PER_DAY()
                || report.providers.length > 32
        ) {
            revert InvalidReport();
        }
        if (operationHash != getSettlementOperationHash(report, roundIds)) revert InvalidReport();

        uint256 actualIncome;
        for (uint256 i = 0; i < report.income.length; i++) {
            bytes32 sourceId = report.income[i].sourceId;
            if (sourceId == bytes32(0) || sourceId == WRAP_FEE || sourceId == DEPLOY_FEE) revert InvalidReport();
            for (uint256 j = 0; j < i; j++) {
                if (sourceId == report.income[j].sourceId) revert DuplicateSource();
            }
            actualIncome += report.income[i].amount;
        }

        uint256 totalLiquidityWeight;
        for (uint256 i = 0; i < report.providers.length; i++) {
            LiquidityProvider calldata provider = report.providers[i];
            if (provider.account == address(0) || provider.liquidityWeight == 0) revert InvalidReport();
            for (uint256 j = 0; j < i; j++) {
                if (provider.account == report.providers[j].account) revert DuplicateProvider();
            }
            totalLiquidityWeight += provider.liquidityWeight;
        }

        uint256 price8 = twap.calculate(report.day, roundIds);
        uint256 tvl = _btcUsd(report.averageBtcSats, price8) + report.stablecoinTvl;
        uint256 baseTarget = Math.mulDiv(tvl, controller.rateForDay(report.day), 1_000_000);
        uint256 reserveTarget = Math.mulDiv(_btcUsd(report.openingLiquidityReserveSats, price8), 5, 100);

        authorization.consumeOperation(operationHash);
        uint256 feeIncome = report.stakerWrapFeeIncome + report.stakerDeployFeeIncome;
        uint256 funding = actualIncome + feeIncome;
        bytes32[] memory sourceIds = new bytes32[](report.income.length);
        uint256[] memory amounts = new uint256[](report.income.length);
        for (uint256 i = 0; i < report.income.length; i++) {
            sourceIds[i] = report.income[i].sourceId;
            amounts[i] = report.income[i].amount;
        }
        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        if (
            incomeVault.releaseDay(
                report.day, sourceIds, amounts, report.stakerWrapFeeIncome, report.stakerDeployFeeIncome
            ) != funding || rewardToken.balanceOf(address(this)) - beforeBalance != funding
        ) revert InvalidTransfer();

        uint256 basePaid;
        uint256 excess;
        uint256 stakerIncome;
        uint256 lpIncome;
        if (actualIncome >= baseTarget) {
            basePaid = baseTarget;
            excess = actualIncome - baseTarget;
            stakerIncome = excess / 2 + feeIncome;
            uint256 reserveOrLp = excess - excess / 2;
            uint256 missingReserve = reserveTarget > reserveBalance ? reserveTarget - reserveBalance : 0;
            uint256 reserveAdd = reserveOrLp < missingReserve ? reserveOrLp : missingReserve;
            reserveBalance += reserveAdd;
            lpIncome = basePaid + reserveOrLp - reserveAdd;
        } else {
            basePaid = actualIncome;
            if (report.providers.length > 0 && controller.useReserveForDay(report.day)) {
                uint256 shortfall = baseTarget - actualIncome;
                uint256 coverage = shortfall < reserveBalance ? shortfall : reserveBalance;
                reserveBalance -= coverage;
                basePaid += coverage;
            }
            stakerIncome = feeIncome;
            lpIncome = basePaid;
        }

        if (staking.totalEligibleStake(report.day) == 0) {
            lpIncome += stakerIncome;
            stakerIncome = 0;
        }
        staking.fundDay(report.day, stakerIncome);
        uint256 treasuryIncome;
        if (report.providers.length == 0) {
            treasuryIncome = lpIncome;
            lpIncome = 0;
            if (treasuryIncome > 0) {
                uint256 treasuryBefore = rewardToken.balanceOf(treasury);
                uint256 balanceBeforeTransfer = rewardToken.balanceOf(address(this));
                rewardToken.safeTransfer(treasury, treasuryIncome);
                if (
                    rewardToken.balanceOf(treasury) != treasuryBefore + treasuryIncome
                        || rewardToken.balanceOf(address(this)) != balanceBeforeTransfer - treasuryIncome
                ) revert InvalidTransfer();
                emit TreasuryIncomeTransferred(report.day, treasuryIncome);
            }
        } else {
            _allocateProviders(report.providers, totalLiquidityWeight, lpIncome);
        }
        settlements[report.day] = Settlement(
            price8,
            tvl,
            baseTarget,
            actualIncome,
            basePaid,
            excess,
            stakerIncome,
            lpIncome,
            reserveTarget,
            reserveBalance,
            report.stakerWrapFeeIncome,
            report.stakerDeployFeeIncome,
            report.evidenceHash,
            treasuryIncome
        );
        nextSettlementDay++;
        emit DaySettled(report.day, actualIncome, stakerIncome, lpIncome, reserveBalance);
    }

    function fundReserve(uint256 amount) external nonReentrant {
        if (amount == 0) revert InvalidReport();
        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        rewardToken.safeTransferFrom(msg.sender, address(this), amount);
        if (rewardToken.balanceOf(address(this)) - beforeBalance != amount) revert InvalidTransfer();
        reserveBalance += amount;
        emit ReserveFunded(msg.sender, amount);
    }

    function claimLiquidityIncome() external nonReentrant returns (uint256 amount) {
        amount = lpClaimable[msg.sender];
        lpClaimable[msg.sender] = 0;
        if (amount > 0) {
            uint256 beforeBalance = rewardToken.balanceOf(address(this));
            uint256 beforeRecipient = rewardToken.balanceOf(msg.sender);
            rewardToken.safeTransfer(msg.sender, amount);
            if (
                rewardToken.balanceOf(address(this)) != beforeBalance - amount
                    || rewardToken.balanceOf(msg.sender) != beforeRecipient + amount
            ) revert InvalidTransfer();
        }
        emit LiquidityIncomeClaimed(msg.sender, amount);
    }

    function getSettlement(uint256 day) external view returns (Settlement memory) {
        return settlements[day];
    }

    function _allocateProviders(LiquidityProvider[] calldata providers, uint256 totalWeight, uint256 amount) private {
        uint256 allocated;
        uint256[] memory remainders = new uint256[](providers.length);
        for (uint256 i = 0; i < providers.length; i++) {
            uint256 share = Math.mulDiv(amount, providers[i].liquidityWeight, totalWeight);
            lpClaimable[providers[i].account] += share;
            allocated += share;
            remainders[i] = mulmod(amount, providers[i].liquidityWeight, totalWeight);
        }
        uint256 dust = amount - allocated;
        // Largest remainders receive one base unit; address ordering breaks ties independently of report order.
        for (uint256 i = 0; i < providers.length && dust > 0; i++) {
            uint256 rank;
            for (uint256 j = 0; j < providers.length; j++) {
                if (
                    remainders[j] > remainders[i]
                        || (remainders[j] == remainders[i] && uint160(providers[j].account) < uint160(providers[i].account))
                ) rank++;
            }
            if (rank < dust) lpClaimable[providers[i].account]++;
        }
    }

    function _btcUsd(uint256 satoshis, uint256 price8) private view returns (uint256) {
        return Math.mulDiv(satoshis, price8 * rewardScale, 1e16);
    }
}
