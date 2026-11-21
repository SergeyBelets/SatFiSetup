// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {CustodianDAOToken} from "../contracts/src/tokens/CustodianDAOToken.sol";
import {CustodianDAOStaking} from "../contracts/src/federation/CustodianDAOStaking.sol";
import {CustodianDAORateController} from "../contracts/src/federation/CustodianDAORateController.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAODailySettlement} from "../contracts/src/federation/CustodianDAODailySettlement.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {ChainlinkBTCDailyTWAP} from "../contracts/src/federation/ChainlinkBTCDailyTWAP.sol";
import "./ValueFlowTestFixtures.sol";

contract NoLPTestUSD is ERC20 {
    address public blockedRecipient;
    constructor() ERC20("No-LP Test USD", "NLUSD") {}
    function decimals() public pure override returns (uint8) { return 6; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
    function blockRecipient(address recipient) external { blockedRecipient = recipient; }
    function _update(address from, address to, uint256 value) internal override {
        require(to != blockedRecipient || to == address(0), "blocked recipient");
        super._update(from, to, value);
    }
}

contract NoLPTestFeed {
    struct Round { int256 price; uint256 timestamp; }
    mapping(uint80 => Round) public rounds;
    uint8 public constant decimals = 8;
    function setRound(uint80 id, int256 price, uint256 timestamp) external { rounds[id] = Round(price, timestamp); }
    function getRoundData(uint80 id) external view returns (uint80, int256, uint256, uint256, uint80) {
        Round memory round = rounds[id];
        require(round.timestamp != 0, "no round");
        return (id, round.price, round.timestamp, round.timestamp, id);
    }
}

contract DAONoLPSettlementTest is Test {
    address constant TREASURY = address(0x7EA5);
    address constant LP = address(0x1F00);
    bytes32 constant YIELD = keccak256("NO_LP_YIELD");
    NoLPTestUSD usd;
    CustodianDAOToken stakeToken;
    CustodianDAOStaking staking;
    CustodianDAORateController controller;
    CustodianDAOIncomeVault vault;
    CustodianDAODailySettlement settlement;
    CustodianValidator validator;
    ValueFlowCoordinator coordinator;
    FROSTAuthorization authorization;
    NoLPTestFeed feed;
    ChainlinkBTCDailyTWAP twap;
    uint256 day;

    function setUp() public {
        vm.warp(1_000 days + 12 hours);
        coordinator = new ValueFlowCoordinator(address(this));
        address[] memory signers = new address[](1);
        signers[0] = address(this);
        authorization = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), signers, 1);
        validator = new CustodianValidator(signers, 1, address(authorization));
        usd = new NoLPTestUSD();
        stakeToken = new CustodianDAOToken("Governance", "GOV", address(this));
        staking = new CustodianDAOStaking(address(stakeToken), address(usd), address(validator));
        controller = new CustodianDAORateController(address(validator), address(staking), true);
        feed = new NoLPTestFeed();
        twap = new ChainlinkBTCDailyTWAP(address(feed));
        vault = new CustodianDAOIncomeVault(address(usd), address(authorization));
        settlement = new CustodianDAODailySettlement(address(usd), address(staking), address(controller),
            address(authorization), address(twap), address(vault), TREASURY);
        staking.setDistributor(address(settlement));
        vault.setSettlement(address(settlement));
        day = staking.firstEligibleDay();
        _source(YIELD, CustodianDAOIncomeVault.Stream.Yield);
        _source(vault.WRAP_FEE(), CustodianDAOIncomeVault.Stream.WrapFee);
        _source(vault.DEPLOY_FEE(), CustodianDAOIncomeVault.Stream.DeployFee);
    }

    function _source(bytes32 id, CustodianDAOIncomeVault.Stream stream) internal {
        bytes32 op = vault.getSetSourceOperationHash(id, address(this), stream, true, address(0), 0);
        coordinator.approve(authorization, op, address(vault));
        vault.setSource(id, address(this), stream, true, address(0), 0, op);
    }

    function _stake() internal {
        stakeToken.approve(address(staking), 100 ether);
        staking.stake(100 ether);
    }

    function _disableReserve() internal {
        uint256 proposal = controller.propose(CustodianDAORateController.Parameter.UseReserve, 0, address(this));
        controller.vote(proposal, address(this));
        controller.execute(proposal);
    }

    function _reserve(uint256 amount) internal {
        if (amount == 0) return;
        usd.mint(address(this), amount);
        usd.approve(address(settlement), amount);
        settlement.fundReserve(amount);
    }

    function _report(uint256 income, uint256 wrapFee, uint256 deployFee, bool hasLP)
        internal view returns (CustodianDAODailySettlement.DayReport memory report)
    {
        report.day = day;
        report.averageBtcSats = 1e8;
        report.openingLiquidityReserveSats = 1e8;
        report.stakerWrapFeeIncome = wrapFee;
        report.stakerDeployFeeIncome = deployFee;
        report.evidenceHash = keccak256(abi.encode("evidence", day));
        report.income = new CustodianDAODailySettlement.IncomeLine[](income > 0 ? 1 : 0);
        if (income > 0) report.income[0] = CustodianDAODailySettlement.IncomeLine(YIELD, income);
        report.providers = new CustodianDAODailySettlement.LiquidityProvider[](hasLP ? 1 : 0);
        if (hasLP) report.providers[0] = CustodianDAODailySettlement.LiquidityProvider(LP, 1);
    }

    function _rounds() internal returns (uint80[] memory ids) {
        feed.setRound(1, 100e8, day * 1 days - 1 hours);
        feed.setRound(2, 200e8, day * 1 days + 12 hours);
        feed.setRound(3, 150e8, (day + 1) * 1 days);
        ids = new uint80[](3);
        ids[0] = 1; ids[1] = 2; ids[2] = 3;
    }

    function _fund(CustodianDAODailySettlement.DayReport memory report) internal {
        uint256 income;
        for (uint256 i; i < report.income.length; i++) income += report.income[i].amount;
        usd.mint(address(this), income + report.stakerWrapFeeIncome + report.stakerDeployFeeIncome);
        usd.approve(address(vault), type(uint256).max);
        if (income > 0) vault.deposit(report.day, YIELD, income);
        if (report.stakerWrapFeeIncome > 0) vault.deposit(report.day, vault.WRAP_FEE(), report.stakerWrapFeeIncome);
        if (report.stakerDeployFeeIncome > 0) vault.deposit(report.day, vault.DEPLOY_FEE(), report.stakerDeployFeeIncome);
    }

    function _settle(CustodianDAODailySettlement.DayReport memory report) internal {
        uint80[] memory ids = _rounds();
        vm.warp((day + 1) * 1 days);
        _fund(report);
        bytes32 op = settlement.getSettlementOperationHash(report, ids);
        coordinator.approve(authorization, op, address(settlement));
        settlement.settleDay(report, ids, op);
    }

    function _assertDistribution(bool stakers, bool hasLP, uint256 expectedStakers, uint256 expectedLpOrTreasury) internal view {
        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day);
        uint256 stakingIncome = stakers ? expectedStakers : 0;
        uint256 recipients = expectedLpOrTreasury + (stakers ? 0 : expectedStakers);
        assertEq(result.stakerIncome, stakingIncome);
        assertEq(result.lpIncome, hasLP ? recipients : 0);
        assertEq(result.treasuryIncome, hasLP ? 0 : recipients);
        assertEq(staking.dayReward(day), stakingIncome);
        assertEq(usd.balanceOf(address(staking)), stakingIncome);
        assertEq(settlement.lpClaimable(LP), hasLP ? recipients : 0);
        assertEq(usd.balanceOf(TREASURY), hasLP ? 0 : recipients);
        assertTrue(staking.dayFunded(day));
        assertEq(settlement.nextSettlementDay(), day + 1);
    }

    function _matrix(bool stakers, bool hasLP) internal {
        if (stakers) _stake();
        // At $150/BTC, base target is 18,000 and reserve target 7,500,000.
        // A 10,018,000 yield has 10,000,000 excess; half funds the reserve.
        _settle(_report(10_018_000, 120, 80, hasLP));
        _assertDistribution(stakers, hasLP, 5_000_200, 18_000);
        assertEq(settlement.reserveBalance(), 5_000_000);
        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day);
        assertEq(10_018_200, result.stakerIncome + result.lpIncome + result.treasuryIncome + result.reserveBalanceAfter);
    }

    function test_StakersAndLPKeepExistingDistribution() public { _matrix(true, true); }
    function test_NoStakersAddsStakerIncomeToLP() public { _matrix(false, true); }
    function test_StakersWithoutLPTransfersOnlyLPShareToTreasury() public { _matrix(true, false); }
    function test_NoRecipientsTransfersBothDistributableShares() public { _matrix(false, false); }

    function test_RejectsZeroAndSelfTreasury() public {
        bytes memory initcode = abi.encodePacked(type(CustodianDAODailySettlement).creationCode,
            abi.encode(address(usd), address(staking), address(controller), address(authorization), address(twap), address(vault), TREASURY));
        assertEq(initcode.length, type(CustodianDAODailySettlement).creationCode.length + 224);
        assertLe(initcode.length, 49_152);
        assertLe(address(settlement).code.length, 24_576);
        vm.expectRevert(CustodianDAODailySettlement.InvalidAddress.selector);
        new CustodianDAODailySettlement(address(usd), address(staking), address(controller), address(authorization), address(twap), address(vault), address(0));
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)));
        vm.expectRevert(CustodianDAODailySettlement.InvalidAddress.selector);
        new CustodianDAODailySettlement(address(usd), address(staking), address(controller), address(authorization), address(twap), address(vault), predicted);
        assertEq(settlement.treasury(), TREASURY);
    }

    function testFuzz_NoLPDeficitNeverConsumesReserve(bool stakers, bool useReserve, uint32 incomeSeed) public {
        if (stakers) _stake();
        if (!useReserve) _disableReserve();
        _reserve(50_000);
        uint256 income = bound(uint256(incomeSeed), 0, 17_999);
        _settle(_report(income, 10, 20, false));
        _assertDistribution(stakers, false, 30, income);
        assertEq(settlement.reserveBalance(), 50_000);
        assertEq(settlement.getSettlement(day).basePaid, income);
        assertEq(controller.useReserveForDay(day), useReserve);
    }

    function testFuzz_LPDeficitPreservesReserveFlagBehavior(bool useReserve) public {
        _stake();
        if (!useReserve) _disableReserve();
        _reserve(50_000);
        _settle(_report(8_000, 10, 20, true));
        _assertDistribution(true, true, 30, useReserve ? 18_000 : 8_000);
        assertEq(settlement.reserveBalance(), useReserve ? 40_000 : 50_000);
    }

    function test_ZeroIncomeFundsZeroAdvancesAndNextDayCanSettle() public {
        _reserve(50_000);
        _settle(_report(0, 0, 0, false));
        assertTrue(staking.dayFunded(day));
        assertEq(staking.dayReward(day), 0);
        assertEq(settlement.reserveBalance(), 50_000);
        assertEq(usd.balanceOf(TREASURY), 0);
        day++;
        _settle(_report(0, 0, 23, false));
        assertTrue(staking.dayFunded(day));
        assertEq(settlement.nextSettlementDay(), day + 1);
        assertEq(usd.balanceOf(TREASURY), 23);
        assertEq(settlement.reserveBalance(), 50_000);
    }

    function test_NoLPDayPreservesPriorLPClaimsAndTheirBacking() public {
        _reserve(50_000);
        _settle(_report(8_000, 10, 20, true));
        uint256 priorClaim = settlement.lpClaimable(LP);
        assertEq(priorClaim, 18_030);
        assertEq(settlement.reserveBalance(), 40_000);
        day++;
        _settle(_report(8_000, 10, 20, false));
        assertEq(settlement.getSettlement(day).lpIncome, 0);
        assertEq(settlement.lpClaimable(LP), priorClaim);
        assertEq(settlement.reserveBalance(), 40_000);
        assertEq(usd.balanceOf(TREASURY), 8_030);
        assertEq(usd.balanceOf(address(settlement)), priorClaim + 40_000);
        vm.prank(LP);
        assertEq(settlement.claimLiquidityIncome(), priorClaim);
        assertEq(usd.balanceOf(LP), priorClaim);
        assertEq(usd.balanceOf(address(settlement)), 40_000);
    }

    function test_TreasuryTransferFailureRevertsWholeDayAndCanRetry() public {
        _stake();
        _reserve(5_000);
        CustodianDAODailySettlement.DayReport memory report = _report(10_018_000, 10, 20, false);
        uint80[] memory ids = _rounds();
        vm.warp((day + 1) * 1 days);
        _fund(report);
        bytes32 op = settlement.getSettlementOperationHash(report, ids);
        coordinator.approve(authorization, op, address(settlement));
        usd.blockRecipient(TREASURY);
        vm.expectRevert("blocked recipient");
        settlement.settleDay(report, ids, op);
        assertFalse(authorization.executedOperations(op));
        assertFalse(vault.released(day));
        assertFalse(staking.dayFunded(day));
        assertEq(settlement.nextSettlementDay(), day);
        assertEq(settlement.reserveBalance(), 5_000);
        assertEq(settlement.lpClaimable(LP), 0);
        assertEq(usd.balanceOf(address(staking)), 0);
        assertEq(usd.balanceOf(address(vault)), 10_018_030);
        assertEq(usd.balanceOf(address(settlement)), 5_000);
        usd.blockRecipient(address(0));
        settlement.settleDay(report, ids, op);
        assertTrue(vault.released(day));
        assertTrue(staking.dayFunded(day));
        assertEq(usd.balanceOf(TREASURY), 18_000);
    }

    function test_EmptyRecipientsStillRequirePriceApprovalAndCorrectDay() public {
        CustodianDAODailySettlement.DayReport memory report = _report(0, 0, 0, false);
        vm.warp((day + 1) * 1 days);
        uint80[] memory ids = new uint80[](0);
        bytes32 op = settlement.getSettlementOperationHash(report, ids);
        coordinator.approve(authorization, op, address(settlement));
        vm.expectRevert();
        settlement.settleDay(report, ids, op);
        assertFalse(authorization.executedOperations(op));
        assertFalse(staking.dayFunded(day));
        ids = _rounds();
        op = settlement.getSettlementOperationHash(report, ids);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        settlement.settleDay(report, ids, op);
        coordinator.approve(authorization, op, address(settlement));
        settlement.settleDay(report, ids, op);
        vm.expectRevert(CustodianDAODailySettlement.InvalidDay.selector);
        settlement.settleDay(report, ids, op);
        report.day += 2;
        op = settlement.getSettlementOperationHash(report, ids);
        vm.expectRevert(CustodianDAODailySettlement.InvalidDay.selector);
        settlement.settleDay(report, ids, op);
    }

    function test_NonEmptyProviderValidationStillRejectsInvalidWeightsAndDuplicates() public {
        CustodianDAODailySettlement.DayReport memory report = _report(0, 0, 0, true);
        uint80[] memory ids = _rounds();
        vm.warp((day + 1) * 1 days);
        report.providers[0].liquidityWeight = 0;
        bytes32 op = settlement.getSettlementOperationHash(report, ids);
        vm.expectRevert(CustodianDAODailySettlement.InvalidReport.selector);
        settlement.settleDay(report, ids, op);
        report.providers[0].liquidityWeight = 1;
        report.providers[0].account = address(0);
        op = settlement.getSettlementOperationHash(report, ids);
        vm.expectRevert(CustodianDAODailySettlement.InvalidReport.selector);
        settlement.settleDay(report, ids, op);
        report.providers = new CustodianDAODailySettlement.LiquidityProvider[](2);
        report.providers[0] = CustodianDAODailySettlement.LiquidityProvider(LP, 1);
        report.providers[1] = CustodianDAODailySettlement.LiquidityProvider(LP, 2);
        op = settlement.getSettlementOperationHash(report, ids);
        vm.expectRevert(CustodianDAODailySettlement.DuplicateProvider.selector);
        settlement.settleDay(report, ids, op);
        report.providers = new CustodianDAODailySettlement.LiquidityProvider[](33);
        op = settlement.getSettlementOperationHash(report, ids);
        vm.expectRevert(CustodianDAODailySettlement.InvalidReport.selector);
        settlement.settleDay(report, ids, op);
    }

    function testFuzz_FundingReserveAndAllRecipientsConserveValue(
        bool stakers, bool hasLP, bool useReserve, uint64 incomeSeed, uint64 reserveSeed, uint64 feeSeed
    ) public {
        if (stakers) _stake();
        if (!useReserve) _disableReserve();
        uint256 income = bound(uint256(incomeSeed), 0, 20_000_000);
        uint256 reserveBefore = bound(uint256(reserveSeed), 0, 10_000_000);
        uint256 feeIncome = bound(uint256(feeSeed), 0, 1_000_000);
        _reserve(reserveBefore);
        _settle(_report(income, feeIncome / 2, feeIncome - feeIncome / 2, hasLP));
        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day);
        assertEq(income + feeIncome + reserveBefore,
            result.stakerIncome + result.lpIncome + result.treasuryIncome + result.reserveBalanceAfter);
        assertEq(result.stakerIncome, staking.dayReward(day));
        assertEq(result.lpIncome, settlement.lpClaimable(LP));
        assertEq(result.treasuryIncome, usd.balanceOf(TREASURY));
        assertEq(usd.balanceOf(address(settlement)), result.lpIncome + result.reserveBalanceAfter);
        if (!hasLP) {
            assertEq(result.lpIncome, 0);
            assertGe(result.reserveBalanceAfter, reserveBefore);
        } else assertEq(result.treasuryIncome, 0);
        if (!stakers) assertEq(result.stakerIncome, 0);
    }
}
