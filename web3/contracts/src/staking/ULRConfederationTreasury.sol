// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ULRStaking} from "./ULRStaking.sol";
import {ULRCustodianSnapshots} from "./ULRCustodianSnapshots.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {IConfederationFactory} from "../federation/interfaces/IConfederationFactory.sol";
import {ICustodianValidator} from "../federation/interfaces/ICustodianValidator.sol";

/// @notice Free USD-token treasury; pays 90% of excess over $1000, weighted by daily custodian ULR.
contract ULRConfederationTreasury is ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum Outcome {
        Paid,
        BelowReserve,
        NoCustodianWeight,
        NoEligibleStake,
        RoundedToZero
    }

    struct Settlement {
        bool processed;
        Outcome outcome;
        uint256 openingBalance;
        uint256 custodianULR;
        uint256 systemULR;
        uint256 paid;
        bytes32 snapshotCommitment;
    }

    IConfederationFactory public immutable factory;
    string public confederationId;
    bytes32 public immutable scope;
    ULRCustodianSnapshots public immutable snapshots;
    ULRStaking public immutable staking;
    IERC20 public immutable rewardToken;
    FROSTAuthorization public immutable authorization;
    ICustodianValidator public immutable validator;
    uint256 public immutable reserveFloor;
    uint256 public immutable payoutBps;
    uint256 public operationNonce;
    mapping(uint256 => Settlement) public settlements;

    event Deposited(address indexed payer, uint256 amount);
    event DaySettled(
        uint256 indexed day,
        uint256 openingBalance,
        uint256 custodianULR,
        uint256 systemULR,
        uint256 paid,
        Outcome outcome,
        bytes32 snapshotCommitment
    );
    event OperationalSpend(address indexed recipient, uint256 amount, bytes32 operationHash);

    error InvalidBinding();
    error NotEnrolled();
    error InvalidDay();
    error SnapshotRequired();
    error InvalidAmount();
    error InvalidTransfer();
    error InvalidOperation();
    error ApprovalRequired();

    constructor(address factory_, string memory id, address snapshots_, uint256 reserve, uint256 payoutBps_) {
        if (reserve >= 2**128 || payoutBps_ > 10000) revert InvalidAmount();
        payoutBps = payoutBps_;
        if (factory_.code.length == 0 || snapshots_.code.length == 0 || bytes(id).length == 0) revert InvalidBinding();
        factory = IConfederationFactory(factory_);
        IConfederationFactory.ConfederationConfig memory config = factory.getConfederation(id);
        if (!config.isActive || config.frostMultisig.code.length == 0 || config.custodianValidator.code.length == 0) {
            revert InvalidBinding();
        }
        authorization = FROSTAuthorization(config.frostMultisig);
        validator = ICustodianValidator(config.custodianValidator);
        confederationId = id;
        snapshots = ULRCustodianSnapshots(snapshots_);
        staking = snapshots.staking();
        rewardToken = staking.rewardToken();
        uint8 decimals = IERC20Metadata(address(rewardToken)).decimals();
        if (decimals > 18) revert InvalidBinding();
        reserveFloor = reserve;
        scope = snapshots.scopeFor(factory_, id);
    }

    function deposit(uint256 amount) external nonReentrant {
        if (amount == 0) revert InvalidAmount();
        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        rewardToken.safeTransferFrom(msg.sender, address(this), amount);
        if (rewardToken.balanceOf(address(this)) - beforeBalance != amount) revert InvalidTransfer();
        emit Deposited(msg.sender, amount);
    }

    function previewDay(uint256 day) public view returns (Settlement memory result) {
        if (settlements[day].processed) return settlements[day];
        _requireEnrolled();
        // Missed days cannot repeatedly drain the same current balance as historical income.
        uint256 today = block.timestamp / 1 days;
        if (day < staking.firstEligibleDay() || today == 0 || day != today - 1) revert InvalidDay();
        (bool published, uint256 total,,,, bytes32 commitment) = snapshots.daySnapshots(day);
        (bool included, uint256 weight,) = snapshots.confederations(day, scope);
        if (!published || !included) revert SnapshotRequired();
        result = Settlement(false, Outcome.Paid, rewardToken.balanceOf(address(this)), weight, total, 0, commitment);
        if (result.openingBalance <= reserveFloor) {
            result.outcome = Outcome.BelowReserve;
        } else if (weight == 0 || total == 0) {
            result.outcome = Outcome.NoCustodianWeight;
        } else if (staking.totalEligibleStake(day) == 0) {
            result.outcome = Outcome.NoEligibleStake;
        } else {
            // One rounding step, not floor(90% of excess) followed by another floor.
            result.paid = Math.mulDiv(result.openingBalance - reserveFloor, weight * payoutBps, total * 10000);
            if (result.paid == 0) result.outcome = Outcome.RoundedToZero;
        }
    }

    /// @notice Anyone may execute the rule; balance-dependent zero quotes leave the day open.
    function payDay(uint256 day) external nonReentrant returns (uint256 amount) {
        if (settlements[day].processed) return settlements[day].paid;
        Settlement memory result = previewDay(day);
        if (result.outcome == Outcome.BelowReserve || result.outcome == Outcome.RoundedToZero) return 0;
        result.processed = true;
        settlements[day] = result;
        amount = result.paid;
        if (amount > 0) {
            rewardToken.forceApprove(address(staking), amount);
            staking.fundDay(day, amount);
            rewardToken.forceApprove(address(staking), 0);
            if (rewardToken.balanceOf(address(this)) != result.openingBalance - amount) revert InvalidTransfer();
        }
        emit DaySettled(
            day,
            result.openingBalance,
            result.custodianULR,
            result.systemULR,
            amount,
            result.outcome,
            result.snapshotCommitment
        );
    }

    function getSpendOperationHash(address recipient, uint256 amount, uint256 deadline) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.spend.selector, keccak256(abi.encode(recipient, amount, deadline)), operationNonce
        );
    }

    /// @notice Explicit operational spending; the $1000 floor limits dividends, not approved operating costs.
    function spend(address recipient, uint256 amount, uint256 deadline, bytes32 op) external nonReentrant {
        _requireEnrolled();
        if (
            recipient == address(0) || recipient == address(this) || recipient == address(staking)
                || recipient == address(rewardToken)
        ) revert InvalidBinding();
        if (amount == 0) revert InvalidAmount();
        if (block.timestamp > deadline || op != getSpendOperationHash(recipient, amount, deadline)) {
            revert InvalidOperation();
        }
        if (!validator.meetsMinimumCustodians(op)) revert ApprovalRequired();
        authorization.consumeOperation(op);
        ++operationNonce;
        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        uint256 recipientBefore = rewardToken.balanceOf(recipient);
        rewardToken.safeTransfer(recipient, amount);
        if (
            beforeBalance - rewardToken.balanceOf(address(this)) != amount
                || rewardToken.balanceOf(recipient) - recipientBefore != amount
        ) revert InvalidTransfer();
        emit OperationalSpend(recipient, amount, op);
    }

    function _requireEnrolled() private view {
        if (snapshots.treasuries(scope) != address(this)) revert NotEnrolled();
        IConfederationFactory.ConfederationConfig memory config = factory.getConfederation(confederationId);
        if (
            !config.isActive || config.frostMultisig != address(authorization)
                || config.custodianValidator != address(validator)
        ) revert InvalidBinding();
    }
}
