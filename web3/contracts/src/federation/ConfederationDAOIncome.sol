// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianValidator} from "./CustodianValidator.sol";
import {ConfederationDAOStake} from "./ConfederationDAOStake.sol";
import {CustodianDAODecisionBoard} from "./CustodianDAODecisionBoard.sol";

interface IConfederationFeeSale {
    function source() external view returns (address);
    function feeToken() external view returns (address);
    function stablecoin() external view returns (address);
    function treasury() external view returns (address);
    function authorization() external view returns (address);
    function validator() external view returns (address);
    function sellBps() external view returns (uint256);
    function recordFee(uint256 amount) external;
}

/// @notice Receives and accounts for all income of one new confederation DAO.
contract ConfederationDAOIncome is ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum Stream { Wrap, Unwrap, Pass, Direct }

    struct Expense {
        address payer;
        bytes32 transactionHash;
        uint256 weiCost;
    }

    struct DayReport {
        uint256 day;
        bytes32 evidenceHash;
        // S first, then stablecoins in the order approved by FROST.
        address[] tokens;
        uint256[4][] income;
        uint256[] unitsPerEth;
        Expense[] expenses;
    }

    struct ExpenseBatch {
        uint256 day;
        uint256 offset;
        bytes32 previous;
        bytes32 evidenceHash;
        uint256 firstPosition;
        uint256 lastPosition;
        Expense[] expenses;
    }

    struct Debt {
        address payer;
        uint256 outstandingWei;
    }

    uint256 public constant MAX_TOKENS = 16;
    uint256 public constant MAX_EXPENSES = 128;
    uint256 public constant MAX_PAYMENT_ROWS = 32;

    address public immutable pool;
    address public immutable shareToken;
    FROSTAuthorization public immutable authorization;
    CustodianValidator public immutable validator;
    ConfederationDAOStake public immutable staking;
    CustodianDAODecisionBoard public immutable decisions;
    address public passSale;
    IConfederationFeeSale public feeSale;
    address[] public tokens;
    mapping(address => bool) public approvedToken;
    uint256 public nextSettlementDay;
    uint256 public operationNonce;
    bool public processing;
    uint256 public debtHead;
    uint256 public debtTail;
    uint256 public stagedExpenseCount;
    uint256 public stagedExpensePosition;
    bytes32 public stagedExpenseCommitment;

    mapping(uint256 => Debt) public debt;
    mapping(bytes32 => bool) public expenseRecorded;
    mapping(uint256 => bytes32) public dayEvidence;
    mapping(uint256 => mapping(address => mapping(Stream => uint256))) public dayIncome;
    mapping(address => uint256) public accountedBalance;
    mapping(address => uint256) public availableIncome;
    mapping(address => uint256) public unitsPerEth;
    mapping(uint256 => mapping(address => uint256)) public dayReward;
    mapping(uint256 => uint256) public dayEligibleTotal;
    mapping(address => mapping(address => uint256)) public reimbursement;
    mapping(address => mapping(address => mapping(uint256 => bool))) public claimed;

    event Income(uint256 indexed day, address indexed token, Stream indexed stream, uint256 amount);
    event StablecoinApproved(address indexed token);
    event PassSaleBound(address indexed sale);
    event FeeSaleBound(address indexed sale);
    event FeeReservedForSale(Stream indexed stream, uint256 amount);
    event ExpenseRecorded(uint256 indexed id, bytes32 indexed transactionHash, address indexed payer, uint256 weiCost);
    event ExpensesStaged(uint256 indexed day, uint256 offset, uint256 count, bytes32 commitment);
    event ReimbursementAllocated(uint256 indexed id, address indexed token, uint256 amount, uint256 weiPaid);
    event ExpenseRounding(uint256 indexed id, uint256 weiAmount);
    event DayOpened(uint256 indexed day, bytes32 indexed evidenceHash);
    event DaySettled(uint256 indexed day, uint256 eligibleStake);
    event Claimed(address indexed owner, address indexed token, uint256 amount, bool expense);

    error InvalidBinding();
    error InvalidAuthorization();
    error InvalidIncome();
    error InvalidReport();
    error InvalidDay();
    error InvalidClaim();
    error InvalidTransfer();

    constructor(address pool_, address share_, address auth_, address stake_, address decisions_) {
        pool = pool_;
        shareToken = share_;
        authorization = FROSTAuthorization(auth_);
        staking = ConfederationDAOStake(stake_);
        decisions = CustodianDAODecisionBoard(decisions_);
        validator = staking.custodianValidator();
        if (pool_.code.length == 0 || share_.code.length == 0 || auth_.code.length == 0 || decisions_.code.length == 0
            || validator.frostMultisig() != auth_ || address(staking.stakeToken()) == share_
            || address(decisions.staking()) != stake_ || address(decisions.authorization()) != auth_) {
            revert InvalidBinding();
        }
        approvedToken[share_] = true;
        tokens.push(share_);
        nextSettlementDay = block.timestamp / 1 days;
    }

    function rewardTokens() external view returns (address[] memory) { return tokens; }

    function getAddStablecoinOperationHash(address token) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.addStablecoin.selector, keccak256(abi.encode(token)),
            operationNonce);
    }

    function addStablecoin(address token, bytes32 op) external {
        if (token.code.length == 0 || approvedToken[token] || token == address(staking.stakeToken())
            || tokens.length == MAX_TOKENS) revert InvalidBinding();
        _authorize(op, getAddStablecoinOperationHash(token));
        approvedToken[token] = true;
        tokens.push(token);
        emit StablecoinApproved(token);
    }

    function getBindPassSaleOperationHash(address sale) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.bindPassSale.selector, keccak256(abi.encode(sale)),
            operationNonce);
    }

    function bindPassSale(address sale, bytes32 op) external {
        if (passSale != address(0) || sale.code.length == 0) revert InvalidBinding();
        _authorize(op, getBindPassSaleOperationHash(sale));
        passSale = sale;
        emit PassSaleBound(sale);
    }

    function getSetFeeSaleOperationHash(address sale) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.setFeeSale.selector, keccak256(abi.encode(sale)),
            operationNonce);
    }

    function setFeeSale(address sale, bytes32 op) external nonReentrant {
        IConfederationFeeSale candidate = IConfederationFeeSale(sale);
        if (address(feeSale) != address(0) || sale.code.length == 0
            || candidate.source() != address(this) || candidate.treasury() != address(this)
            || candidate.feeToken() != shareToken || candidate.authorization() != address(authorization)
            || candidate.validator() != address(validator)) revert InvalidBinding();
        address stable = candidate.stablecoin();
        if (stable == shareToken || !approvedToken[stable] || candidate.sellBps() > 10_000) revert InvalidBinding();
        _authorize(op, getSetFeeSaleOperationHash(sale));
        feeSale = candidate;
        emit FeeSaleBound(sale);
    }

    function recordShareIncome(Stream stream, uint256 amount) external nonReentrant {
        IERC20 shares = IERC20(shareToken);
        uint256 balance = shares.balanceOf(address(this));
        if (stream == Stream.Direct || (stream == Stream.Pass ? msg.sender != passSale : msg.sender != pool)
            || amount == 0 || balance - accountedBalance[shareToken] < amount) {
            revert InvalidIncome();
        }
        IConfederationFeeSale sale = feeSale;
        if (stream != Stream.Pass && address(sale) != address(0)) {
            uint256 reserved = Math.mulDiv(amount, sale.sellBps(), 10_000);
            if (reserved != 0) {
                shares.safeTransfer(address(sale), reserved);
                if (shares.balanceOf(address(this)) != balance - reserved) revert InvalidTransfer();
                sale.recordFee(reserved);
                amount -= reserved;
                emit FeeReservedForSale(stream, reserved);
            }
        }
        if (amount != 0) _record(shareToken, stream, amount);
    }

    /// @notice Credits the exact transfer from the bound sale, independently of direct donations.
    function recordFeeSaleProceeds(uint256 amount) external nonReentrant {
        IConfederationFeeSale sale = feeSale;
        if (msg.sender != address(sale) || amount == 0) revert InvalidIncome();
        address token = sale.stablecoin();
        if (IERC20(token).balanceOf(address(this)) - accountedBalance[token] < amount) revert InvalidIncome();
        _record(token, Stream.Direct, amount);
    }

    /// @notice Accounts for transfers at their received value and in the day of this call.
    function sweep(address token) external nonReentrant returns (uint256 amount) {
        if (!approvedToken[token]) return 0;
        amount = IERC20(token).balanceOf(address(this)) - accountedBalance[token];
        if (amount != 0) _record(token, Stream.Direct, amount);
    }

    function _record(address token, Stream stream, uint256 amount) private {
        uint256 day = block.timestamp / 1 days;
        dayIncome[day][token][stream] += amount;
        accountedBalance[token] += amount;
        emit Income(day, token, stream, amount);
    }

    function getSettlementOperationHash(DayReport calldata report) public view returns (bytes32) {
        bytes32 reportHash = keccak256(abi.encode(report));
        if (stagedExpenseCount != 0) reportHash = keccak256(abi.encode(reportHash, stagedExpenseCommitment));
        return authorization.hashOperation(address(this), this.openDay.selector, reportHash, report.day);
    }

    function getStageExpensesOperationHash(ExpenseBatch calldata batch) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.stageExpenses.selector,
            keccak256(abi.encode(batch)), batch.offset);
    }

    /// @notice Appends chronological expenses for the next closed day without opening it.
    function stageExpenses(ExpenseBatch calldata batch, bytes32 op) external nonReentrant {
        if (processing || batch.day != nextSettlementDay || batch.day >= block.timestamp / 1 days) revert InvalidDay();
        uint256 count = batch.expenses.length;
        if (count == 0 || count > MAX_EXPENSES || batch.offset != stagedExpenseCount
            || batch.previous != stagedExpenseCommitment || batch.evidenceHash == bytes32(0)
            || (batch.offset != 0 && batch.firstPosition <= stagedExpensePosition)
            || batch.lastPosition < batch.firstPosition
            || (count > 1 && batch.lastPosition == batch.firstPosition)) revert InvalidReport();
        _consume(op, getStageExpensesOperationHash(batch));
        _recordExpenses(batch.expenses);
        stagedExpenseCount += count;
        stagedExpensePosition = batch.lastPosition;
        stagedExpenseCommitment = keccak256(abi.encode(batch));
        emit ExpensesStaged(batch.day, batch.offset, count, stagedExpenseCommitment);
    }

    function openDay(DayReport calldata report, bytes32 op) external nonReentrant {
        uint256 day = report.day;
        if (processing || day != nextSettlementDay || day >= block.timestamp / 1 days) revert InvalidDay();
        uint256 count = tokens.length;
        if (report.evidenceHash == bytes32(0) || report.expenses.length > MAX_EXPENSES
            || (stagedExpenseCount != 0 && report.expenses.length != 0)
            || report.tokens.length != count || report.income.length != count
            || report.unitsPerEth.length != count) revert InvalidReport();

        _consume(op, getSettlementOperationHash(report));
        for (uint256 i; i < count; ++i) {
            address token = tokens[i];
            if (report.tokens[i] != token || report.unitsPerEth[i] == 0
                || report.unitsPerEth[i] > type(uint128).max) revert InvalidReport();
            for (uint256 stream; stream < 4; ++stream) {
                uint256 amount = dayIncome[day][token][Stream(stream)];
                if (report.income[i][stream] != amount) revert InvalidReport();
                availableIncome[token] += amount;
            }
            unitsPerEth[token] = report.unitsPerEth[i];
        }
        _recordExpenses(report.expenses);
        processing = true;
        dayEvidence[day] = report.evidenceHash;
        emit DayOpened(day, report.evidenceHash);
    }

    function _recordExpenses(Expense[] calldata expenses) private {
        for (uint256 i; i < expenses.length; ++i) {
            Expense calldata row = expenses[i];
            if (row.transactionHash == bytes32(0) || expenseRecorded[row.transactionHash]
                || row.payer == address(0) || row.weiCost == 0 || row.weiCost > type(uint128).max) {
                revert InvalidReport();
            }
            expenseRecorded[row.transactionHash] = true;
            uint256 id = debtTail++;
            debt[id] = Debt(row.payer, row.weiCost);
            emit ExpenseRecorded(id, row.transactionHash, row.payer, row.weiCost);
        }
    }

    /// @notice Earmarks reimbursements before making any of this day's income claimable as rewards.
    function processDay(uint256 maxRows) external nonReentrant {
        if (!processing || maxRows == 0 || maxRows > MAX_PAYMENT_ROWS) revert InvalidDay();
        uint256 stop = Math.min(debtHead + maxRows, debtTail);
        while (debtHead < stop) {
            if (!_allocate(debtHead)) {
                _closeDay();
                return;
            }
            delete debt[debtHead++];
        }
        if (debtHead == debtTail) _closeDay();
    }

    function _allocate(uint256 id) private returns (bool) {
        Debt storage row = debt[id];
        uint256 outstanding = row.outstandingWei;
        uint256 count = tokens.length;
        bool representable;
        for (uint256 i; i < count; ++i) {
            address token = tokens[i];
            uint256 rate = unitsPerEth[token];
            uint256 due = Math.mulDiv(outstanding, rate, 1 ether);
            if (due == 0) continue;
            representable = true;
            uint256 amount = Math.min(due, availableIncome[token]);
            if (amount == 0) continue;
            uint256 paidWei = amount == due ? outstanding : Math.mulDiv(amount, 1 ether, rate, Math.Rounding.Ceil);
            reimbursement[row.payer][token] += amount;
            availableIncome[token] -= amount;
            outstanding -= paidWei;
            emit ReimbursementAllocated(id, token, amount, paidWei);
            if (outstanding == 0) return true;
        }
        if (!representable) {
            emit ExpenseRounding(id, outstanding);
            return true;
        }
        row.outstandingWei = outstanding;
        return false;
    }

    function _closeDay() private {
        uint256 day = nextSettlementDay;
        uint256 eligible = staking.totalEligibleStake(day);
        if (eligible != 0 && debtHead == debtTail) {
            dayEligibleTotal[day] = eligible;
            for (uint256 i; i < tokens.length; ++i) {
                address token = tokens[i];
                dayReward[day][token] = availableIncome[token];
                availableIncome[token] = 0;
            }
        }
        stagedExpenseCount = 0;
        stagedExpensePosition = 0;
        stagedExpenseCommitment = bytes32(0);
        processing = false;
        nextSettlementDay = day + 1;
        emit DaySettled(day, dayEligibleTotal[day]);
    }

    function claim(address token, uint256[] calldata days_) external nonReentrant returns (uint256 amount) {
        if (!approvedToken[token] || days_.length == 0 || days_.length > 32) revert InvalidClaim();
        for (uint256 i; i < days_.length; ++i) {
            uint256 day = days_[i];
            if (day >= nextSettlementDay || dayEvidence[day] == bytes32(0)
                || claimed[msg.sender][token][day] || (i != 0 && day <= days_[i - 1])) revert InvalidClaim();
            claimed[msg.sender][token][day] = true;
            uint256 eligible = dayEligibleTotal[day];
            if (eligible != 0) {
                amount += Math.mulDiv(dayReward[day][token], staking.eligibleStake(msg.sender, day), eligible);
            }
        }
        _pay(token, amount, false);
    }

    function claimReimbursement(address token) external nonReentrant returns (uint256 amount) {
        amount = reimbursement[msg.sender][token];
        if (amount == 0) revert InvalidClaim();
        reimbursement[msg.sender][token] = 0;
        _pay(token, amount, true);
    }

    function _pay(address token, uint256 amount, bool expense) private {
        if (amount != 0) {
            accountedBalance[token] -= amount;
            IERC20 asset = IERC20(token);
            uint256 beforeBalance = asset.balanceOf(address(this));
            asset.safeTransfer(msg.sender, amount);
            if (beforeBalance - asset.balanceOf(address(this)) != amount) revert InvalidTransfer();
        }
        emit Claimed(msg.sender, token, amount, expense);
    }

    function _authorize(bytes32 op, bytes32 expected) private {
        if (processing) revert InvalidDay();
        _consume(op, expected);
        ++operationNonce;
    }

    function _consume(bytes32 op, bytes32 expected) private {
        uint256 count = validator.getCustodianCount();
        if (count == 0 || op != expected || validator.custodianSignatureCount(op) < (2 * count + 2) / 3) {
            revert InvalidAuthorization();
        }
        authorization.consumeOperation(op);
    }
}
