// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";

interface IDAOLiquidityGuard {
    function availableLiquidity() external view returns (uint256);
}

interface IDAOSettlementBinding {
    function incomeVault() external view returns (address);
    function nextSettlementDay() external view returns (uint256);
}

contract CustodianDAOIncomeVault is ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant WRAP_FEE = keccak256("WRAP_FEE");
    bytes32 public constant UNWRAP_FEE = keccak256("UNWRAP_FEE");
    mapping(uint256=>uint256) public dayUnwrapFeeIncome;
    bytes32 public constant DEPLOY_FEE = keccak256("DEPLOY_FEE");
    bytes32 public constant CURVE = keccak256("CURVE");
    uint256 public constant MAX_YIELD_SOURCES_PER_DAY = 32;

    enum Stream {
        Yield,
        WrapFee,
        DeployFee,
        UnwrapFee
    }

    struct Source {
        address account;
        Stream stream;
        bool active;
        address liquidityGuard;
        uint256 minimumLiquidity;
    }

    IERC20 public immutable rewardToken;
    FROSTAuthorization public immutable authorization;
    address public immutable setupAuthority;
    address public settlement;
    uint256 public firstSettlementDay;
    uint256 public operationNonce;
    mapping(bytes32 => Source) public sources;
    mapping(uint256 => mapping(bytes32 => uint256)) public sourceIncome;
    mapping(uint256 => uint256) public dayYieldIncome;
    mapping(uint256 => uint256) public dayWrapFeeIncome;
    mapping(uint256 => uint256) public dayDeployFeeIncome;
    mapping(uint256 => bool) public released;
    mapping(uint256 => bytes32[]) private fundedYieldSources;

    event SettlementSet(address indexed settlement);
    event SourceSet(
        bytes32 indexed sourceId,
        address indexed account,
        Stream stream,
        bool active,
        address liquidityGuard,
        uint256 minimumLiquidity
    );
    event IncomeDeposited(uint256 indexed day, bytes32 indexed sourceId, address indexed account, uint256 amount);
    event DayReleased(uint256 indexed day, uint256 yieldIncome, uint256 wrapFeeIncome, uint256 deployFeeIncome);

    error InvalidAddress();
    error InvalidSource();
    error Unauthorized();
    error AlreadyConfigured();
    error InvalidDay();
    error InvalidAmount();
    error InvalidTransfer();
    error ReportMismatch();
    error InsufficientLiquidity();
    error TooManyYieldSources();

    constructor(address rewardToken_, address authorization_) {
        if (
            rewardToken_ == address(0) || rewardToken_.code.length == 0 || authorization_ == address(0)
                || authorization_.code.length == 0
        ) revert InvalidAddress();
        rewardToken = IERC20(rewardToken_);
        authorization = FROSTAuthorization(authorization_);
        setupAuthority = msg.sender;
    }

    function setSettlement(address settlement_) external {
        if (msg.sender != setupAuthority) revert Unauthorized();
        if (settlement != address(0)) revert AlreadyConfigured();
        if (
            settlement_ == address(0) || settlement_.code.length == 0
                || IDAOSettlementBinding(settlement_).incomeVault() != address(this)
        ) revert InvalidAddress();
        settlement = settlement_;
        firstSettlementDay = IDAOSettlementBinding(settlement_).nextSettlementDay();
        emit SettlementSet(settlement_);
    }

    function getSetSourceOperationHash(
        bytes32 sourceId,
        address account,
        Stream stream,
        bool active,
        address liquidityGuard,
        uint256 minimumLiquidity
    ) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this),
            this.setSource.selector,
            keccak256(abi.encode(sourceId, account, stream, active, liquidityGuard, minimumLiquidity)),
            operationNonce
        );
    }

    function setSource(
        bytes32 sourceId,
        address account,
        Stream stream,
        bool active,
        address liquidityGuard,
        uint256 minimumLiquidity,
        bytes32 operationHash
    ) external {
        if (sourceId == bytes32(0) || account == address(0)) revert InvalidSource();
        if (
            (sourceId == UNWRAP_FEE && stream != Stream.UnwrapFee) || (sourceId == WRAP_FEE && stream != Stream.WrapFee) || (sourceId == DEPLOY_FEE && stream != Stream.DeployFee)
        ) revert InvalidSource();
        if (sourceId == CURVE && (stream != Stream.Yield || liquidityGuard.code.length == 0 || minimumLiquidity == 0)) {
            revert InvalidSource();
        }
        if (
            (liquidityGuard == address(0) && minimumLiquidity != 0)
                || (liquidityGuard != address(0) && (liquidityGuard.code.length == 0 || minimumLiquidity == 0))
        ) revert InvalidSource();
        if (sources[sourceId].account != address(0) && sources[sourceId].stream != stream) revert InvalidSource();
        if (
            operationHash
                != getSetSourceOperationHash(sourceId, account, stream, active, liquidityGuard, minimumLiquidity)
        ) revert ReportMismatch();
        authorization.consumeOperation(operationHash);
        operationNonce++;
        sources[sourceId] = Source(account, stream, active, liquidityGuard, minimumLiquidity);
        emit SourceSet(sourceId, account, stream, active, liquidityGuard, minimumLiquidity);
    }

    function deposit(uint256 day, bytes32 sourceId, uint256 amount) external nonReentrant {
        Source memory source = sources[sourceId];
        if (!source.active || msg.sender != source.account) revert Unauthorized();
        if (day < firstSettlementDay || day > block.timestamp / 1 days || released[day]) revert InvalidDay();
        if (amount == 0) revert InvalidAmount();
        if (source.stream == Stream.Yield && sourceIncome[day][sourceId] == 0) {
            if (fundedYieldSources[day].length == MAX_YIELD_SOURCES_PER_DAY) revert TooManyYieldSources();
            fundedYieldSources[day].push(sourceId);
        }
        if (
            source.liquidityGuard != address(0)
                && IDAOLiquidityGuard(source.liquidityGuard).availableLiquidity() < source.minimumLiquidity
        ) revert InsufficientLiquidity();

        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        rewardToken.safeTransferFrom(msg.sender, address(this), amount);
        if (rewardToken.balanceOf(address(this)) - beforeBalance != amount) revert InvalidTransfer();

        sourceIncome[day][sourceId] += amount;
        if (source.stream == Stream.Yield) dayYieldIncome[day] += amount;
        else if (source.stream == Stream.WrapFee) dayWrapFeeIncome[day] += amount;
        else if(source.stream==Stream.UnwrapFee) {
            dayUnwrapFeeIncome[day]+=amount;
            // Legacy settlement field aggregates operational (wrap + unwrap) staker income.
            dayWrapFeeIncome[day]+=amount;
        } else dayDeployFeeIncome[day] += amount;
        emit IncomeDeposited(day, sourceId, msg.sender, amount);
    }

    function releaseDay(
        uint256 day,
        bytes32[] calldata sourceIds,
        uint256[] calldata amounts,
        uint256 wrapFeeIncome,
        uint256 deployFeeIncome
    ) external nonReentrant returns (uint256 funding) {
        if (msg.sender != settlement) revert Unauthorized();
        if (released[day]) revert InvalidDay();
        if (sourceIds.length != amounts.length) revert ReportMismatch();

        uint256 yieldIncome;
        uint256 positiveSources;
        for (uint256 i = 0; i < sourceIds.length; i++) {
            if (sources[sourceIds[i]].account == address(0) || sources[sourceIds[i]].stream != Stream.Yield) {
                revert ReportMismatch();
            }
            for (uint256 j = 0; j < i; j++) {
                if (sourceIds[i] == sourceIds[j]) revert ReportMismatch();
            }
            if (sourceIncome[day][sourceIds[i]] != amounts[i]) revert ReportMismatch();
            if (amounts[i] > 0) positiveSources++;
            yieldIncome += amounts[i];
        }
        if (
            positiveSources != fundedYieldSources[day].length || yieldIncome != dayYieldIncome[day]
                || wrapFeeIncome != dayWrapFeeIncome[day]
                || deployFeeIncome != dayDeployFeeIncome[day]
        ) revert ReportMismatch();

        released[day] = true;
        funding = yieldIncome + wrapFeeIncome + deployFeeIncome;
        if (funding > 0) rewardToken.safeTransfer(settlement, funding);
        emit DayReleased(day, yieldIncome, wrapFeeIncome, deployFeeIncome);
    }

    function getFundedYieldSources(uint256 day) external view returns (bytes32[] memory) {
        return fundedYieldSources[day];
    }
}
