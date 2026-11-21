// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {DAOMockCoordinator, DAOMockVerifier} from "./CustodianDAO.t.sol";
import {EconomyAsset} from "./ConfederationDAOIncome.t.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {SimpleMultiPool} from "../contracts/src/demo/SimpleMultiPool.sol";
import {MultiPoolShareToken} from "../contracts/src/tokens/MultiPoolShareToken.sol";
import {PoolMpToken} from "../contracts/src/tokens/PoolMpToken.sol";
import {ConfederationDAOIncome} from "../contracts/src/federation/ConfederationDAOIncome.sol";
import {ConfederationDAOStake} from "../contracts/src/federation/ConfederationDAOStake.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {FederationFeeManager} from "../contracts/src/federation/FederationFeeManager.sol";
import {IFeeManager} from "../contracts/src/federation/IFeeManager.sol";
import {FundedFeeSale} from "../contracts/src/federation/FundedFeeSale.sol";

/// @dev The toggle lets a test exercise a failure after a bid was successfully escrowed.
contract FeeSaleTestUSD is ERC20("Fee sale test USD", "TUSD") {
    bool private taxed;

    function decimals() public pure override returns (uint8) { return 6; }
    function mint(address receiver, uint256 amount) external { _mint(receiver, amount); }
    function setTaxed(bool value) external { taxed = value; }

    function _update(address from, address to, uint256 value) internal override {
        if (taxed && from != address(0) && to != address(0)) {
            uint256 tax = value / 10;
            super._update(from, address(0), tax);
            value -= tax;
        }
        super._update(from, to, value);
    }
}

/// @dev Real authorization grants/consumption, with a fixture for finalized FROST sessions.
abstract contract FundedFeeSaleFixture is Test {
    address internal constant ALICE = address(0xA);
    address internal constant BOB = address(0xB);
    address internal constant CAROL = address(0xC);
    address internal constant PAYER = address(0xD);
    address internal constant BUYER = address(0xE);
    address internal constant TREASURY = address(0xF);

    FROSTAuthorization internal auth;
    DAOMockCoordinator internal coordinator;
    FeeSaleTestUSD internal stable;
    FundedFeeSale internal sale;
    uint256 private session;

    function setUp() public virtual {
        vm.warp(100 days);
        coordinator = new DAOMockCoordinator(address(new DAOMockVerifier()), ALICE);
        address[] memory signers = new address[](1);
        signers[0] = ALICE;
        auth = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), signers, 1);
        stable = new FeeSaleTestUSD();
    }

    function _frost(bytes32 op, address target) internal {
        coordinator.setResult(op, target);
        auth.authorizeOperation(++session, op, target);
    }

    function _fund(address bidder, uint256 asset, uint256 funds, uint256 deadline) internal returns (uint256 id) {
        stable.mint(bidder, funds);
        vm.startPrank(bidder);
        stable.approve(address(sale), funds);
        id = sale.fundBid(asset, funds, deadline);
        vm.stopPrank();
    }
}

contract FundedMPFeeSaleTest is FundedFeeSaleFixture {
    SimpleMultiPool private pool;
    MultiPoolShareToken private shares;
    PoolMpToken private mp;

    function setUp() public override {
        super.setUp();
        mp = new PoolMpToken("Pool MP", "MP", address(this), false);
        address predicted = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        shares = new MultiPoolShareToken("Pool share", "FS", predicted);
        pool = new SimpleMultiPool("fee-sale-source", address(auth), address(shares));
        IFeeManager.FeeConfig memory config = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, 1000, 0, 0);
        FederationFeeManager manager = new FederationFeeManager(address(auth), address(0), TREASURY, config, config);
        bytes32 op = manager.getSetFeeCollectorOperationHash(address(pool), true);
        _frost(op, address(manager));
        manager.setFeeCollector(address(pool), true, op);
        op = pool.getSetFeeManagerOperationHash(address(manager));
        _frost(op, address(pool));
        pool.setFeeManager(address(manager), op);
        op = pool.getAddPoolOperationHash(address(mp), "source-mp");
        _frost(op, address(pool));
        pool.addPool(address(mp), "source-mp", op);
        sale = new FundedFeeSale(address(pool), address(mp), address(stable), TREASURY, 3_000);
        _deposit(100 ether);
    }

    function _deposit(uint256 amount) private {
        mp.mint(ALICE, amount);
        vm.startPrank(ALICE);
        mp.approve(address(pool), amount);
        pool.deposit(address(mp), amount);
        vm.stopPrank();
    }

    function _claimApproval(uint256 gross, uint256 nonce) private returns (bytes32 claim) {
        claim = pool.getClaimFeesOperationHash(address(mp), true, address(sale), gross);
        assertEq(sale.getMPClaimOperationHash(true, gross, nonce), claim);
        _frost(claim, address(pool));
    }

    function _collect(uint256 gross) private {
        uint256 nonce = pool.operationNonce();
        _claimApproval(gross, nonce);
        bytes32 op = sale.getCollectMPFeesOperationHash(true, gross, nonce);
        _frost(op, address(sale));
        sale.collectMPFees(true, gross, nonce, op);
    }

    function testCollectRequiresBothApprovalsAndLeavesPrincipalInPool() public {
        uint256 gross = 10 ether;
        assertEq(pool.accruedFees(address(mp), true), gross);
        assertEq(shares.balanceOf(ALICE), 90 ether);
        uint256 nonce = pool.operationNonce();
        bytes32 op = sale.getCollectMPFeesOperationHash(true, gross, nonce);
        _frost(op, address(sale));
        vm.expectRevert();
        sale.collectMPFees(true, gross, nonce, op);
        assertFalse(auth.executedOperations(op));
        assertEq(pool.accruedFees(address(mp), true), gross);
        bytes32 claim = _claimApproval(gross, nonce);
        sale.collectMPFees(true, gross, nonce, op);
        assertTrue(auth.executedOperations(op));
        assertTrue(auth.executedOperations(claim));
        assertTrue(sale.processedMPClaims(claim));
        assertEq(pool.accruedFees(address(mp), true), 0);
        assertEq(pool.getPoolInfo(address(mp)).totalDeposited, 90 ether);
        assertEq(mp.balanceOf(address(pool)), shares.totalSupply());
        assertEq(mp.balanceOf(TREASURY), 7 ether);
        assertEq(sale.inventory(), 3 ether);
        assertEq(mp.balanceOf(address(sale)), sale.inventory());
    }

    function testExactAlreadyRelayedClaimPartitionsOnce() public {
        uint256 gross = 10 ether;
        uint256 nonce = pool.operationNonce();
        bytes32 claim = _claimApproval(gross, nonce);
        vm.prank(BOB);
        pool.claimFees(address(mp), true, address(sale), gross, claim);
        assertEq(mp.balanceOf(address(sale)), gross);
        assertEq(sale.inventory(), 0);

        bytes32 wrong = sale.getCollectMPFeesOperationHash(true, gross - 1, nonce);
        _frost(wrong, address(sale));
        vm.expectRevert();
        sale.collectMPFees(true, gross - 1, nonce, wrong);
        assertEq(mp.balanceOf(TREASURY), 0);
        assertFalse(auth.executedOperations(wrong));

        bytes32 op = sale.getCollectMPFeesOperationHash(true, gross, nonce);
        _frost(op, address(sale));
        sale.collectMPFees(true, gross, nonce, op);
        assertEq(sale.inventory(), 3 ether);
        assertEq(mp.balanceOf(TREASURY), 7 ether);

        bytes32 retry = sale.getCollectMPFeesOperationHash(true, gross, nonce);
        _frost(retry, address(sale));
        vm.expectRevert();
        sale.collectMPFees(true, gross, nonce, retry);
        assertEq(sale.inventory(), 3 ether);
        assertEq(mp.balanceOf(TREASURY), 7 ether);
    }

    function testBidsSpendOnlyTheirOwnEscrowAndRefundAfterDeadline() public {
        uint256 deadline = block.timestamp + 1 hours;
        uint256 first = _fund(BUYER, 2 ether, 400e6, deadline);
        uint256 second = _fund(BOB, 2 ether, 350e6, deadline);
        assertEq(sale.inventory(), 0);
        assertEq(stable.balanceOf(address(sale)), 750e6);
        assertEq(sale.stableEscrow(), 750e6);
        assertEq(stable.balanceOf(TREASURY), 0);
        _collect(10 ether);

        bytes32 op = sale.getFillBidOperationHash(first);
        vm.expectRevert();
        sale.fillBid(first, op);
        _frost(op, address(sale));
        sale.fillBid(first, op);
        assertEq(stable.balanceOf(TREASURY), 400e6);
        assertEq(stable.balanceOf(address(sale)), 350e6);
        assertEq(sale.stableEscrow(), 350e6);
        assertEq(mp.balanceOf(BUYER), 2 ether);
        assertEq(sale.inventory(), 1 ether);
        vm.expectRevert();
        sale.fillBid(first, op);

        bytes32 nextOp = sale.getFillBidOperationHash(second);
        _frost(nextOp, address(sale));
        vm.expectRevert();
        sale.fillBid(second, nextOp);
        assertFalse(auth.executedOperations(nextOp));
        assertEq(stable.balanceOf(address(sale)), 350e6);
        vm.prank(BOB);
        vm.expectRevert();
        sale.cancelBid(second);
        vm.warp(deadline + 1);
        vm.prank(BUYER);
        vm.expectRevert();
        sale.cancelBid(second);
        vm.prank(BOB);
        sale.cancelBid(second);
        assertEq(stable.balanceOf(BOB), 350e6);
        assertEq(stable.balanceOf(BUYER), 0);
        assertEq(stable.balanceOf(address(sale)), 0);
        assertEq(sale.stableEscrow(), 0);
        assertEq(stable.balanceOf(TREASURY), 400e6);
        assertEq(sale.inventory(), 1 ether);
        assertEq(stable.totalSupply(), 750e6);
    }

    function testBpsChangeAffectsOnlyLaterFeeCaptures() public {
        _collect(10 ether);
        bytes32 op = sale.getSetSellBpsOperationHash(0);
        vm.expectRevert();
        sale.setSellBps(0, op);
        _frost(op, address(sale));
        sale.setSellBps(0, op);
        assertEq(sale.sellBps(), 0);
        assertEq(sale.inventory(), 3 ether);
        _deposit(40 ether);
        _collect(4 ether);
        assertEq(sale.inventory(), 3 ether);
        assertEq(mp.balanceOf(TREASURY), 11 ether);
        assertEq(mp.balanceOf(address(pool)), shares.totalSupply());
    }
}

contract FundedSFeeSaleTest is FundedFeeSaleFixture {
    CustodianValidator private validator;
    EconomyAsset private governance;
    EconomyAsset private share;
    ConfederationDAOStake private staking;
    CustodianDAODecisionBoard private decisions;
    ConfederationDAOIncome private income;

    function setUp() public override {
        super.setUp();
        address[] memory custodians = new address[](3);
        custodians[0] = ALICE;
        custodians[1] = BOB;
        custodians[2] = CAROL;
        validator = new CustodianValidator(custodians, 1, address(auth));
        governance = new EconomyAsset(0);
        share = new EconomyAsset(0);
        staking = new ConfederationDAOStake(address(governance), address(validator), 200_000 ether);
        decisions = new CustodianDAODecisionBoard(address(staking), address(auth));
        // The test drives the pool's earned-fee hook directly. The DAO, staking,
        // validator and authorization execute their production accounting.
        income = new ConfederationDAOIncome(address(this), address(share), address(auth), address(staking), address(decisions));
        _enableStable(income);
        sale = new FundedFeeSale(address(income), address(share), address(stable), address(income), 4_000);
    }

    function _custodians(bytes32 op) private {
        vm.prank(ALICE);
        validator.registerCustodianSignature(op);
        vm.prank(BOB);
        validator.registerCustodianSignature(op);
    }

    function _approve(bytes32 op, address target) private {
        _custodians(op);
        _frost(op, target);
    }

    function _enableStable(ConfederationDAOIncome dao) private {
        bytes32 op = dao.getAddStablecoinOperationHash(address(stable));
        _approve(op, address(dao));
        dao.addStablecoin(address(stable), op);
    }

    function _bind() private {
        bytes32 op = income.getSetFeeSaleOperationHash(address(sale));
        _approve(op, address(income));
        income.setFeeSale(address(sale), op);
    }

    function _earn(ConfederationDAOIncome.Stream stream, uint256 received) private {
        share.mint(address(income), received);
        income.recordShareIncome(stream, received);
    }

    function _settle(uint256 day, uint256 gasDebt) private {
        ConfederationDAOIncome.DayReport memory r;
        r.day = day;
        r.evidenceHash = keccak256(abi.encode(day));
        r.tokens = income.rewardTokens();
        r.income = new uint256[4][](r.tokens.length);
        r.unitsPerEth = new uint256[](r.tokens.length);
        for (uint256 i = 0; i < r.tokens.length; ++i) {
            r.unitsPerEth[i] = 1 ether;
            for (uint256 j = 0; j < 4; ++j) {
                r.income[i][j] = income.dayIncome(day, r.tokens[i], ConfederationDAOIncome.Stream(j));
            }
        }
        r.expenses = new ConfederationDAOIncome.Expense[](gasDebt == 0 ? 0 : 1);
        if (gasDebt != 0) r.expenses[0] = ConfederationDAOIncome.Expense(PAYER, keccak256("gas receipt"), gasDebt);
        bytes32 op = income.getSettlementOperationHash(r);
        _approve(op, address(income));
        income.openDay(r, op);
        income.processDay(1);
    }

    function testSaleCapturesOnlyNewFeesAndCreditsProceedsOnce() public {
        governance.mint(ALICE, 100 ether);
        vm.startPrank(ALICE);
        governance.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        vm.stopPrank();
        _earn(ConfederationDAOIncome.Stream.Wrap, 100);
        vm.warp(101 days);
        _settle(100, 20);
        assertEq(income.reimbursement(PAYER, address(share)), 20);
        assertEq(income.dayReward(100, address(share)), 80);

        _bind();
        bytes32 passOp = income.getBindPassSaleOperationHash(address(this));
        _approve(passOp, address(income));
        income.bindPassSale(address(this), passOp);
        _earn(ConfederationDAOIncome.Stream.Wrap, 100);
        _earn(ConfederationDAOIncome.Stream.Unwrap, 50);
        _earn(ConfederationDAOIncome.Stream.Pass, 25);
        share.mint(address(income), 21);
        assertEq(income.sweep(address(share)), 21);
        assertEq(income.sweep(address(share)), 0);
        assertEq(sale.inventory(), 60);
        assertEq(share.balanceOf(address(sale)), 60);
        assertEq(income.dayIncome(101, address(share), ConfederationDAOIncome.Stream.Wrap), 60);
        assertEq(income.dayIncome(101, address(share), ConfederationDAOIncome.Stream.Unwrap), 30);
        assertEq(income.dayIncome(101, address(share), ConfederationDAOIncome.Stream.Pass), 25);
        assertEq(income.dayIncome(101, address(share), ConfederationDAOIncome.Stream.Direct), 21);
        assertEq(income.accountedBalance(address(share)), 236);

        uint256 id = _fund(BUYER, 30, 450e6, block.timestamp + 1 hours);
        assertEq(stable.balanceOf(address(income)), 0);
        stable.mint(address(income), 13e6);
        bytes32 op = sale.getFillBidOperationHash(id);
        _approve(op, address(sale));
        sale.fillBid(id, op);
        assertEq(share.balanceOf(BUYER), 30);
        assertEq(sale.inventory(), 30);
        assertEq(stable.balanceOf(address(sale)), 0);
        assertEq(stable.balanceOf(address(income)), 463e6);
        assertEq(income.dayIncome(101, address(stable), ConfederationDAOIncome.Stream.Direct), 450e6);
        assertEq(income.accountedBalance(address(stable)), 450e6);
        assertEq(income.sweep(address(stable)), 13e6);
        assertEq(income.sweep(address(stable)), 0);
        vm.expectRevert();
        sale.fillBid(id, op);

        uint256[] memory days_ = new uint256[](1);
        days_[0] = 100;
        vm.prank(ALICE);
        assertEq(income.claim(address(share), days_), 80);
        vm.prank(PAYER);
        assertEq(income.claimReimbursement(address(share)), 20);
        assertEq(share.balanceOf(address(income)), 136);
        assertEq(income.accountedBalance(address(share)), 136);

        vm.warp(102 days);
        _settle(101, 0);
        assertEq(income.dayReward(101, address(share)), 136);
        assertEq(income.dayReward(101, address(stable)), 463e6);
        days_[0] = 101;
        vm.prank(ALICE);
        assertEq(income.claim(address(stable), days_), 463e6);
        assertEq(stable.totalSupply(), 463e6);
        assertEq(stable.balanceOf(ALICE), 463e6);
    }

    function testRevertedFillPreservesApprovalInventoryAndBuyerRefund() public {
        _bind();
        _earn(ConfederationDAOIncome.Stream.Wrap, 100);
        uint256 deadline = block.timestamp + 1 hours;
        uint256 id = _fund(BUYER, 25, 200e6, deadline);
        bytes32 op = sale.getFillBidOperationHash(id);
        _frost(op, address(sale));
        vm.prank(ALICE);
        validator.registerCustodianSignature(op);
        vm.expectRevert();
        sale.fillBid(id, op);
        assertFalse(auth.executedOperations(op));

        vm.prank(BOB);
        validator.registerCustodianSignature(op);
        stable.setTaxed(true);
        vm.expectRevert();
        sale.fillBid(id, op);
        assertFalse(auth.executedOperations(op));
        assertEq(sale.getFillBidOperationHash(id), op);
        assertEq(sale.inventory(), 40);
        assertEq(share.balanceOf(BUYER), 0);
        assertEq(stable.balanceOf(address(sale)), 200e6);
        assertEq(stable.balanceOf(address(income)), 0);
        assertEq(income.accountedBalance(address(stable)), 0);
        stable.setTaxed(false);
        vm.warp(deadline + 1);
        vm.expectRevert();
        sale.fillBid(id, op);
        vm.prank(BUYER);
        sale.cancelBid(id);
        assertEq(stable.balanceOf(BUYER), 200e6);
        assertEq(stable.balanceOf(address(sale)), 0);
        assertEq(sale.inventory(), 40);
    }

    function testFundBidRejectsInexactEscrowCredit() public {
        stable.mint(BUYER, 200e6);
        stable.setTaxed(true);
        vm.startPrank(BUYER);
        stable.approve(address(sale), 200e6);
        vm.expectRevert();
        sale.fundBid(25, 200e6, block.timestamp + 1 hours);
        vm.stopPrank();
        assertEq(stable.balanceOf(BUYER), 200e6);
        assertEq(stable.balanceOf(address(sale)), 0);
        assertEq(sale.bidCount(), 0);
    }

    function testDAORejectsOtherSaleAndUnboundProceeds() public {
        ConfederationDAOIncome other = new ConfederationDAOIncome(
            address(this), address(share), address(auth), address(staking), address(decisions));
        _enableStable(other);
        FundedFeeSale wrong = new FundedFeeSale(address(other), address(share), address(stable), address(other), 4_000);
        bytes32 op = income.getSetFeeSaleOperationHash(address(wrong));
        _approve(op, address(income));
        vm.expectRevert();
        income.setFeeSale(address(wrong), op);
        assertFalse(auth.executedOperations(op));

        _bind();
        stable.mint(address(income), 100e6);
        vm.prank(BUYER);
        vm.expectRevert();
        income.recordFeeSaleProceeds(100e6);
        assertEq(income.accountedBalance(address(stable)), 0);
        assertEq(income.sweep(address(stable)), 100e6);
    }
}
