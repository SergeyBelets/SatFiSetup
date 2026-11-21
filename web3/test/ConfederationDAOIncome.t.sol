// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ConfederationDAOStake} from "../contracts/src/federation/ConfederationDAOStake.sol";
import {ConfederationDAOIncome} from "../contracts/src/federation/ConfederationDAOIncome.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";
import {ConfederationNFTPass} from "../contracts/src/federation/ConfederationNFTPass.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {ConfederationPoolV2} from "../contracts/src/federation/ConfederationPoolV2.sol";
import {ConfederationPoolV2Deployer} from "../contracts/src/federation/ConfederationPoolV2Deployer.sol";
import {ConfederationShareV2Deployer} from "../contracts/src/federation/ConfederationShareV2Deployer.sol";
import {ConfederationShareV2} from "../contracts/src/tokens/ConfederationShareV2.sol";

contract EconomyAsset is ERC20 {
    uint256 public immutable tax;
    constructor(uint256 tax_) ERC20("Test asset", "TEST") { tax = tax_; }
    function mint(address owner, uint256 amount) external { _mint(owner, amount); }
    function burn(address owner, uint256 amount) external { _burn(owner, amount); }
    function _update(address from, address to, uint256 value) internal override {
        if (from != address(0) && to != address(0) && tax != 0) {
            super._update(from, address(0), value * tax / 10_000);
            value -= value * tax / 10_000;
        }
        super._update(from, to, value);
    }
}

contract EconomyApproval {
    mapping(bytes32 => address) public targets;
    function hashOperation(address target, bytes4 method, bytes32 arguments, uint256 nonce)
        external pure returns (bytes32)
    {
        return keccak256(abi.encode(target, method, arguments, nonce));
    }
    function authorize(bytes32 op, address target) external { targets[op] = target; }
    function isOperationApproved(bytes32 op, address target) external view returns (bool) {
        return targets[op] == target;
    }
    function consumeOperation(bytes32 op) external returns (bool) {
        require(targets[op] == msg.sender, "not authorized");
        delete targets[op];
        return true;
    }
}

contract EconomyFeeManagerBinding {
    address public immutable authorization;
    address public immutable validator;
    constructor(address auth, address validator_) { authorization = auth; validator = validator_; }
    function getFeeSecurityConfig() external view returns (address, address) { return (authorization, validator); }
}

contract EconomyFeeManager is EconomyFeeManagerBinding {
    uint256 public feeBps;
    constructor(address auth, address validator_) EconomyFeeManagerBinding(auth, validator_) {}
    function setFeeBps(uint256 bps) external { feeBps = bps; }
    function authorizedFeeCollectors(address) external pure returns (bool) { return true; }
    function calculateFee(bool, uint256 amount) external view returns (uint256) {
        return amount * feeBps / 10_000;
    }
}

contract ConfederationDAOIncomeTest is Test {
    address private constant ALICE = address(0xA);
    address private constant BOB = address(0xB);
    address private constant CAROL = address(0xC);
    address private constant PAYER = address(0xD);
    address private constant SECOND_PAYER = address(0xF);

    EconomyApproval private auth;
    CustodianValidator private validator;
    EconomyAsset private ulr;
    EconomyAsset private share;
    EconomyAsset private stable;
    ConfederationDAOStake private staking;
    ConfederationDAOIncome private income;
    uint256 private day;

    function setUp() public {
        vm.warp(100 days);
        day = block.timestamp / 1 days;
        auth = new EconomyApproval();
        address[] memory members = new address[](3);
        members[0] = ALICE;
        members[1] = BOB;
        members[2] = CAROL;
        validator = new CustodianValidator(members, 1, address(auth));
        ulr = new EconomyAsset(0);
        share = new EconomyAsset(0);
        stable = new EconomyAsset(500);
        staking = new ConfederationDAOStake(address(ulr), address(validator), 200_000 ether);
        CustodianDAODecisionBoard decisions = new CustodianDAODecisionBoard(address(staking), address(auth));
        income = new ConfederationDAOIncome(address(this), address(share), address(auth), address(staking), address(decisions));
        bytes32 op = income.getAddStablecoinOperationHash(address(stable));
        _approve(op, address(income));
        income.addStablecoin(address(stable), op);
    }

    function _approve(bytes32 op, address target) private {
        vm.prank(ALICE);
        validator.registerCustodianSignature(op);
        vm.prank(BOB);
        validator.registerCustodianSignature(op);
        auth.authorize(op, target);
    }

    function _stake(address owner, uint256 amount) private {
        ulr.mint(owner, amount);
        vm.startPrank(owner);
        ulr.approve(address(staking), amount);
        staking.stake(amount);
        vm.stopPrank();
    }

    function _report(uint256 day_, uint256 shareRate, uint256 stableRate)
        private view returns (ConfederationDAOIncome.DayReport memory r)
    {
        r.day = day_;
        r.evidenceHash = keccak256(abi.encode(day_));
        r.tokens = income.rewardTokens();
        r.income = new uint256[4][](2);
        r.unitsPerEth = new uint256[](2);
        r.unitsPerEth[0] = shareRate;
        r.unitsPerEth[1] = stableRate;
        for (uint256 i; i < 2; ++i) {
            for (uint256 j; j < 4; ++j) {
                r.income[i][j] = income.dayIncome(day_, r.tokens[i], ConfederationDAOIncome.Stream(j));
            }
        }
        r.expenses = new ConfederationDAOIncome.Expense[](0);
    }

    function _open(ConfederationDAOIncome.DayReport memory report) private {
        bytes32 op = income.getSettlementOperationHash(report);
        _approve(op, address(income));
        income.openDay(report, op);
    }

    function testTransferTaxDirectIncomeAndSeparateDayClaim() public {
        _stake(ALICE, 100 ether);
        _stake(BOB, 300 ether);
        vm.warp(block.timestamp + 1);
        _stake(CAROL, 100 ether);
        share.mint(address(income), 100 ether);
        income.recordShareIncome(ConfederationDAOIncome.Stream.Wrap, 100 ether);
        stable.mint(address(this), 100);
        stable.transfer(address(income), 100);
        assertEq(income.sweep(address(stable)), 95);
        assertEq(income.sweep(address(stable)), 0);
        assertEq(income.sweep(address(ulr)), 0);

        vm.warp(101 days);
        _open(_report(day, 1 ether, 1 ether));
        income.processDay(1);
        assertEq(income.dayReward(day, address(share)), 100 ether);
        assertEq(income.dayReward(day, address(stable)), 95);

        uint256[] memory dates = new uint256[](1);
        dates[0] = day;
        vm.prank(ALICE);
        assertEq(income.claim(address(share), dates), 25 ether);
        vm.prank(ALICE);
        assertEq(income.claim(address(stable), dates), 23);
        vm.expectRevert(ConfederationDAOIncome.InvalidClaim.selector);
        vm.prank(ALICE);
        income.claim(address(share), dates);
        vm.prank(CAROL);
        assertEq(income.claim(address(share), dates), 0);
    }

    function testFIFOCarriesWeiAndUsesPaymentDayRate() public {
        _stake(ALICE, 100 ether);
        share.mint(address(income), 20);
        income.sweep(address(share));
        stable.mint(address(income), 10);
        income.sweep(address(stable));
        vm.warp(101 days);

        ConfederationDAOIncome.DayReport memory r = _report(day, 1 ether, 1 ether);
        r.expenses = new ConfederationDAOIncome.Expense[](2);
        r.expenses[0] = ConfederationDAOIncome.Expense(PAYER, bytes32(uint256(1)), 50);
        r.expenses[1] = ConfederationDAOIncome.Expense(SECOND_PAYER, bytes32(uint256(2)), 10);
        _open(r);
        income.processDay(1);
        assertEq(income.reimbursement(PAYER, address(share)), 20);
        assertEq(income.reimbursement(PAYER, address(stable)), 10);
        (, uint256 remainder) = income.debt(0);
        assertEq(remainder, 20);
        assertEq(income.reimbursement(SECOND_PAYER, address(share)), 0);
        assertEq(income.dayReward(day, address(share)), 0);

        share.mint(address(income), 80);
        income.sweep(address(share));
        vm.warp(102 days);
        _open(_report(day + 1, 2 ether, 1 ether));
        income.processDay(1);
        assertEq(income.reimbursement(PAYER, address(share)), 60);
        assertTrue(income.processing());
        income.processDay(1);
        assertEq(income.reimbursement(SECOND_PAYER, address(share)), 20);
        assertEq(income.dayReward(day + 1, address(share)), 20);
        vm.prank(PAYER);
        assertEq(income.claimReimbursement(address(share)), 60);
    }

    function testGasDebtCannotCreateIncomeOrSpendFutureRewards() public {
        _stake(ALICE, 100 ether);
        vm.warp(101 days);
        ConfederationDAOIncome.DayReport memory report = _report(day, 1 ether, 1 ether);
        report.expenses = new ConfederationDAOIncome.Expense[](1);
        report.expenses[0] = ConfederationDAOIncome.Expense(PAYER, keccak256("actual receipt"), 500);
        _open(report);
        income.processDay(1);
        assertEq(share.totalSupply(), 0);
        assertEq(income.reimbursement(PAYER, address(share)), 0);
        assertEq(income.dayReward(day, address(share)), 0);
        (, uint256 remaining) = income.debt(0);
        assertEq(remaining, 500);
        vm.prank(PAYER);
        vm.expectRevert(ConfederationDAOIncome.InvalidClaim.selector);
        income.claimReimbursement(address(share));

        share.mint(address(income), 100);
        income.sweep(address(share));
        vm.warp(102 days);
        _open(_report(day + 1, 1 ether, 1 ether));
        income.processDay(1);
        assertEq(income.reimbursement(PAYER, address(share)), 100);
        assertEq(income.dayReward(day + 1, address(share)), 0);
        (, remaining) = income.debt(0);
        assertEq(remaining, 400);
        vm.prank(PAYER);
        income.claimReimbursement(address(share));
        assertEq(share.balanceOf(PAYER), 100);
        assertEq(share.totalSupply(), 100);
        assertEq(income.accountedBalance(address(share)), 0);
        assertEq(share.balanceOf(address(income)), 0);
    }

    function testNoStakeCarryAndDuplicateExpenseRejected() public {
        share.mint(address(income), 20);
        income.sweep(address(share));
        vm.warp(101 days);
        _stake(ALICE, 100 ether);
        ConfederationDAOIncome.DayReport memory r = _report(day, 1 ether, 1 ether);
        r.expenses = new ConfederationDAOIncome.Expense[](1);
        r.expenses[0] = ConfederationDAOIncome.Expense(PAYER, bytes32(uint256(123)), 1);
        _open(r);
        income.processDay(1);
        assertEq(income.availableIncome(address(share)), 19);
        vm.warp(102 days);

        ConfederationDAOIncome.DayReport memory duplicate = _report(day + 1, 1 ether, 1 ether);
        duplicate.expenses = r.expenses;
        bytes32 op = income.getSettlementOperationHash(duplicate);
        _approve(op, address(income));
        vm.expectRevert(ConfederationDAOIncome.InvalidReport.selector);
        income.openDay(duplicate, op);
        _open(_report(day + 1, 1 ether, 1 ether));
        income.processDay(1);
        assertEq(income.dayReward(day + 1, address(share)), 19);
    }

    function testFrostPlusTwoCustodiansAndExactIncome() public {
        vm.warp(101 days);
        ConfederationDAOIncome.DayReport memory r = _report(day, 1 ether, 1 ether);
        r.income[0][0] = 1;
        bytes32 op = income.getSettlementOperationHash(r);
        vm.prank(ALICE);
        validator.registerCustodianSignature(op);
        auth.authorize(op, address(income));
        vm.expectRevert(ConfederationDAOIncome.InvalidAuthorization.selector);
        income.openDay(r, op);
        vm.prank(BOB);
        validator.registerCustodianSignature(op);
        vm.expectRevert(ConfederationDAOIncome.InvalidReport.selector);
        income.openDay(r, op);
    }

    function testStagesMoreThanOneBatchAndSealsTheSameDay() public {
        _stake(ALICE, 100 ether);
        share.mint(address(income), 135);
        income.sweep(address(share));
        vm.warp(101 days);

        ConfederationDAOIncome.DayReport memory r = _report(day, 1 ether, 1 ether);
        r.income[0][3] = 135;
        bytes32 priorOp = income.getSettlementOperationHash(r);
        _approve(priorOp, address(income));

        ConfederationDAOIncome.ExpenseBatch memory first;
        first.day = day;
        first.evidenceHash = keccak256("first expense batch");
        first.firstPosition = 1;
        first.lastPosition = 128;
        first.expenses = new ConfederationDAOIncome.Expense[](128);
        for (uint256 i; i < 128; ++i) {
            first.expenses[i] = ConfederationDAOIncome.Expense(PAYER, bytes32(i + 1), 1);
        }
        bytes32 op = income.getStageExpensesOperationHash(first);
        _approve(op, address(income));
        income.stageExpenses(first, op);
        assertEq(income.stagedExpenseCount(), 128);
        assertEq(income.nextSettlementDay(), day);
        assertFalse(income.processing());

        vm.expectRevert(ConfederationDAOIncome.InvalidAuthorization.selector);
        income.openDay(r, priorOp);
        vm.expectRevert(ConfederationDAOIncome.InvalidDay.selector);
        income.processDay(32);

        ConfederationDAOIncome.ExpenseBatch memory next;
        next.day = day;
        next.offset = 128;
        next.previous = income.stagedExpenseCommitment();
        next.evidenceHash = keccak256("second expense batch");
        next.firstPosition = 129;
        next.lastPosition = 129;
        next.expenses = new ConfederationDAOIncome.Expense[](1);
        next.expenses[0] = ConfederationDAOIncome.Expense(SECOND_PAYER, bytes32(uint256(1)), 1);
        op = income.getStageExpensesOperationHash(next);
        _approve(op, address(income));
        vm.expectRevert(ConfederationDAOIncome.InvalidReport.selector);
        income.stageExpenses(next, op);
        assertEq(income.stagedExpenseCount(), 128);

        next.expenses[0].transactionHash = bytes32(uint256(129));
        op = income.getStageExpensesOperationHash(next);
        _approve(op, address(income));
        income.stageExpenses(next, op);
        assertEq(income.stagedExpenseCount(), 129);

        _open(r);
        for (uint256 i; i < 5; ++i) {
            income.processDay(32);
        }
        assertEq(income.nextSettlementDay(), day + 1);
        assertEq(income.stagedExpenseCount(), 0);
        assertEq(income.reimbursement(PAYER, address(share)), 128);
        assertEq(income.reimbursement(SECOND_PAYER, address(share)), 1);
        assertEq(income.dayReward(day, address(share)), 6);
    }

    function testFullUtcDayAndClaimsStayWithStakeOwnerAfterDelegation() public {
        _stake(ALICE, 100 ether);
        vm.warp(100 days + 1);
        _stake(BOB, 100 ether);
        vm.prank(ALICE);
        staking.unstake(40 ether);
        _stake(ALICE, 40 ether);
        vm.prank(ALICE);
        staking.setDAOSigner(CAROL, "");
        assertEq(staking.getDAOSigner(ALICE), CAROL);
        assertEq(staking.eligibleStake(ALICE, day), 60 ether);
        assertEq(staking.eligibleStake(BOB, day), 0);
        assertEq(staking.totalEligibleStake(day), 60 ether);
        share.mint(address(income), 21 ether);
        income.sweep(address(share));

        vm.warp(101 days);
        assertEq(staking.totalEligibleStake(day + 1), 200 ether);
        ConfederationDAOIncome.DayReport memory skipped = _report(day + 1, 1 ether, 1 ether);
        vm.expectRevert(ConfederationDAOIncome.InvalidDay.selector);
        income.openDay(skipped, bytes32(0));
        _open(_report(day, 1 ether, 1 ether));
        income.processDay(1);

        uint256[] memory tooMany = new uint256[](33);
        vm.expectRevert(ConfederationDAOIncome.InvalidClaim.selector);
        income.claim(address(share), tooMany);
        uint256[] memory dates = new uint256[](1);
        dates[0] = day;
        vm.prank(CAROL);
        assertEq(income.claim(address(share), dates), 0);
        vm.prank(ALICE);
        assertEq(income.claim(address(share), dates), 21 ether);
        ConfederationDAOIncome.DayReport memory repeated = _report(day, 1 ether, 1 ether);
        vm.expectRevert(ConfederationDAOIncome.InvalidDay.selector);
        income.openDay(repeated, bytes32(0));
        vm.warp(102 days);
        _open(_report(day + 1, 1 ether, 1 ether));
        income.processDay(1);
        assertEq(income.dayReward(day + 1, address(share)), 0);
    }

    function testCustodianDelegationEndsOnRemovalAndDoesNotRevive() public {
        vm.prank(ALICE);
        staking.setDAOSigner(CAROL, "");
        assertEq(staking.getDAOSigner(ALICE), CAROL);

        bytes32 op = validator.getRemoveCustodianOperationHash(CAROL);
        _approve(op, address(validator));
        validator.removeCustodian(CAROL, op);
        assertEq(staking.getDAOSigner(ALICE), ALICE);

        op = validator.getAddCustodianOperationHash(CAROL);
        _approve(op, address(validator));
        validator.addCustodian(CAROL, op);
        assertEq(staking.getDAOSigner(ALICE), ALICE);

        vm.prank(ALICE);
        staking.setDAOSigner(CAROL, "");
        assertEq(staking.getDAOSigner(ALICE), CAROL);
    }

    function testConsentedDelegateDoesNotDependOnCustodianStatus() public {
        (address delegate, uint256 privateKey) = makeAddrAndKey("delegated DAO signer");
        bytes32 op = validator.getAddCustodianOperationHash(delegate);
        _approve(op, address(validator));
        validator.addCustodian(delegate, op);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(privateKey, staking.getSignerConsentDigest(ALICE, delegate));
        vm.prank(ALICE);
        staking.setDAOSigner(delegate, abi.encodePacked(r, s, v));

        op = validator.getRemoveCustodianOperationHash(delegate);
        _approve(op, address(validator));
        validator.removeCustodian(delegate, op);
        assertEq(staking.getDAOSigner(ALICE), delegate);
    }

    function testPassQuoteBoundToBuyerAndSingleUse() public {
        (address operator, uint256 key) = makeAddrAndKey("operator");
        EconomyFeeManagerBinding fee = new EconomyFeeManagerBinding(address(auth), address(validator));
        ConfederationNFTPass sale = new ConfederationNFTPass("Pass", "PASS", address(income), address(fee), operator);
        bytes32 op = income.getBindPassSaleOperationHash(address(sale));
        _approve(op, address(income));
        income.bindPassSale(address(sale), op);

        ConfederationNFTPass.Tariff memory tariff = ConfederationNFTPass.Tariff(1 days, 10_000, 10 ether);
        op = sale.getApproveTariffOperationHash(tariff);
        _approve(op, address(sale));
        sale.approveTariff(tariff, op);
        ConfederationNFTPass.Quote memory quote = ConfederationNFTPass.Quote(
            keccak256(abi.encode(tariff)), ALICE, 2 ether, 8, uint64(block.timestamp + 1 hours));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, sale.priceDigest(quote));
        bytes memory signature = abi.encodePacked(r, s, v);
        vm.prank(BOB);
        vm.expectRevert(ConfederationNFTPass.InvalidQuote.selector);
        sale.buy(quote, signature);

        vm.prank(ALICE);
        vm.expectRevert(ConfederationNFTPass.InvalidQuote.selector);
        sale.buy(quote, abi.encodePacked(r, bytes32(uint256(s) ^ 1), v));
        share.mint(ALICE, 2 ether);
        vm.startPrank(ALICE);
        vm.expectRevert();
        sale.buy(quote, signature);
        assertFalse(sale.usedQuote(ALICE, 8));
        share.approve(address(sale), 2 ether);
        uint256 id = sale.buy(quote, signature);
        assertEq(sale.ownerOf(id), ALICE);
        vm.expectRevert(ConfederationNFTPass.InvalidQuote.selector);
        sale.buy(quote, signature);
        vm.stopPrank();
        assertEq(income.dayIncome(day, address(share), ConfederationDAOIncome.Stream.Pass), 2 ether);

        quote.nonce++;
        (v, r, s) = vm.sign(key, sale.priceDigest(quote));
        vm.warp(quote.expiresAt);
        vm.prank(ALICE);
        vm.expectRevert(ConfederationNFTPass.InvalidQuote.selector);
        sale.buy(quote, abi.encodePacked(r, s, v));
    }
}
