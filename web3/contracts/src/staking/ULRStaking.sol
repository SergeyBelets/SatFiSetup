// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

interface IULRCustodianValidator {
    function candidateStaking() external view returns (address);
    function isCustodian(address owner) external view returns (bool);
    function custodianGeneration(address owner) external view returns (uint256);
}

/// @notice Common ULR staking with full UTC-day eligibility and one immutable payout token.
/// @dev Anyone may contribute their own reward tokens for a completed eligible day.
///      Treasury valuation, source authorization and the 90% rule belong to funding contracts.
contract ULRStaking is ReentrancyGuard {
    using SafeERC20 for IERC20;

    struct Checkpoint {
        uint64 timestamp;
        uint192 balance;
    }

    uint256 public constant MAX_CLAIM_DAYS = 32;
    uint256 public immutable CUSTODIAN_THRESHOLD;
    uint256 public immutable minimumStakeAmount;
    uint256 public constant MAX_CANDIDATE_PAGE = 100;
    IERC20 public immutable stakeToken;
    IERC20 public immutable rewardToken;
    uint256 public immutable firstEligibleDay;
    uint256 public totalStaked;
    uint256 public totalFundedRewards;
    uint256 public totalClaimedRewards;

    mapping(address => uint256) public stakedBalance;
    mapping(address => Checkpoint[]) private userCheckpoints;
    Checkpoint[] private totalCheckpoints;
    mapping(address => mapping(uint256 => uint256)) public withdrawnEligible;
    mapping(uint256 => uint256) public totalWithdrawnEligible;
    mapping(address => mapping(uint256 => uint256)) public stakedAtDayStart;
    mapping(uint256 => uint256) public totalStakedAtDayStart;
    mapping(address => mapping(uint256 => bool)) public withdrewAtDayStart;

    mapping(uint256 => uint256) public dayReward;
    mapping(uint256 => uint256) public dayEligibleTotal;
    mapping(uint256 => mapping(address => uint256)) public sourceDayFunding;
    // Cumulative entitlement already paid, rather than a once-only claim flag.
    mapping(address => mapping(uint256 => uint256)) public claimedReward;
    address[] private candidateNominees;
    mapping(address => bool) public candidateTracked;
    mapping(address => mapping(address => address)) public configuredVotingDelegate;
    mapping(address => mapping(address => uint256)) public votingDelegateGeneration;

    event Staked(address indexed owner, uint256 amount);
    event Unstaked(address indexed owner, uint256 amount);
    event DayFunded(uint256 indexed day, address indexed source, uint256 amount, uint256 eligibleTotal);
    event RewardClaimed(address indexed owner, uint256 indexed day, uint256 amount);
    event CustodianCandidateTracked(address indexed owner);
    event VotingDelegateChanged(address indexed owner, address indexed validator, address indexed delegate);

    error InvalidAddress();
    error InvalidAmount();
    error InvalidDay();
    error InvalidClaimBatch();
    error NoEligibleStake();
    error InvalidTransfer();
    error InvalidCandidatePage();
    error InvalidVotingDelegate();

    constructor(address ulr, address usdRewardToken, uint256 candidateThreshold, uint256 minimumStake) {
        if (candidateThreshold == 0 || candidateThreshold > 10_000_000 ether || minimumStake == 0 || minimumStake > 10_000_000 ether) revert InvalidAmount();
        CUSTODIAN_THRESHOLD = candidateThreshold;
        minimumStakeAmount = minimumStake;
        if (ulr.code.length == 0 || usdRewardToken.code.length == 0 || ulr == usdRewardToken) {
            revert InvalidAddress();
        }
        stakeToken = IERC20(ulr);
        rewardToken = IERC20(usdRewardToken);
        firstEligibleDay = block.timestamp / 1 days + (block.timestamp % 1 days == 0 ? 0 : 1);
    }

    function stake(uint256 amount) external nonReentrant {
        if (amount < minimumStakeAmount) revert InvalidAmount();
        uint256 beforeBalance = stakeToken.balanceOf(address(this));
        stakeToken.safeTransferFrom(msg.sender, address(this), amount);
        if (stakeToken.balanceOf(address(this)) - beforeBalance != amount) revert InvalidTransfer();
        stakedBalance[msg.sender] += amount;
        totalStaked += amount;
        if (block.timestamp % 1 days == 0) {
            uint256 day = block.timestamp / 1 days;
            if (!withdrewAtDayStart[msg.sender][day]) {
                stakedAtDayStart[msg.sender][day] += amount;
                totalStakedAtDayStart[day] += amount;
            }
        }
        _writeCheckpoint(userCheckpoints[msg.sender], stakedBalance[msg.sender]);
        _writeCheckpoint(totalCheckpoints, totalStaked);
        if (!candidateTracked[msg.sender]) {
            candidateTracked[msg.sender] = true;
            candidateNominees.push(msg.sender);
            emit CustodianCandidateTracked(msg.sender);
        }
        emit Staked(msg.sender, amount);
    }

    function unstake(uint256 amount) external nonReentrant {
        if (amount == 0 || amount > stakedBalance[msg.sender]) revert InvalidAmount();
        uint256 day = block.timestamp / 1 days;
        uint256 eligibleBefore = eligibleStake(msg.sender, day);
        uint256 newBalance = stakedBalance[msg.sender] - amount;
        uint256 removed = eligibleBefore - Math.min(newBalance, eligibleBefore);
        withdrawnEligible[msg.sender][day] += removed;
        totalWithdrawnEligible[day] += removed;
        if (block.timestamp % 1 days == 0) withdrewAtDayStart[msg.sender][day] = true;
        stakedBalance[msg.sender] = newBalance;
        totalStaked -= amount;
        _writeCheckpoint(userCheckpoints[msg.sender], newBalance);
        _writeCheckpoint(totalCheckpoints, totalStaked);
        _transferExact(stakeToken, msg.sender, amount);
        emit Unstaked(msg.sender, amount);
    }

    function eligibleStake(address owner, uint256 day) public view returns (uint256) {
        if (day < firstEligibleDay || day > block.timestamp / 1 days) return 0;
        return _balanceBefore(userCheckpoints[owner], day * 1 days) + stakedAtDayStart[owner][day]
            - withdrawnEligible[owner][day];
    }

    function totalEligibleStake(uint256 day) public view returns (uint256) {
        if (day < firstEligibleDay || day > block.timestamp / 1 days) return 0;
        return _balanceBefore(totalCheckpoints, day * 1 days) + totalStakedAtDayStart[day] - totalWithdrawnEligible[day];
    }

    function custodianHoldings(address owner) public view returns (uint256) {
        return stakeToken.balanceOf(owner) + stakedBalance[owner];
    }

    /// @notice A nomination never confers active custodian or FROST authority.
    /// @dev Wallet transfers affect eligibility immediately, without requiring a keeper callback.
    function isCustodianCandidate(address owner) public view returns (bool) {
        return candidateTracked[owner] && owner != address(this) && custodianHoldings(owner) >= CUSTODIAN_THRESHOLD;
    }

    function candidateNomineeCount() external view returns (uint256) {
        return candidateNominees.length;
    }

    /// @notice Scans at most limit historical nominees, returning only currently eligible candidates.
    /// @dev nextOffset is a scan cursor, not the number of returned candidates.
    function getCustodianCandidates(uint256 offset, uint256 limit)
        external
        view
        returns (address[] memory candidates, uint256 nextOffset)
    {
        if (limit == 0 || limit > MAX_CANDIDATE_PAGE || offset > candidateNominees.length) {
            revert InvalidCandidatePage();
        }
        nextOffset = Math.min(candidateNominees.length, offset + limit);
        address[] memory buffer = new address[](nextOffset - offset);
        uint256 count;
        for (uint256 i = offset; i < nextOffset; ++i) {
            address owner = candidateNominees[i];
            if (isCustodianCandidate(owner)) buffer[count++] = owner;
        }
        candidates = new address[](count);
        for (uint256 i; i < count; ++i) {
            candidates[i] = buffer[i];
        }
    }

    /// @notice Delegate only voting; the delegate cannot unstake or claim the owner's rewards.
    function setVotingDelegate(address validator, address delegate) external {
        if (delegate == address(0) || delegate == msg.sender) {
            delete configuredVotingDelegate[msg.sender][validator];
            delete votingDelegateGeneration[msg.sender][validator];
            emit VotingDelegateChanged(msg.sender, validator, address(0));
            return;
        }
        if (
            validator.code.length == 0 || IULRCustodianValidator(validator).candidateStaking() != address(this)
                || !IULRCustodianValidator(validator).isCustodian(delegate)
        ) revert InvalidVotingDelegate();
        configuredVotingDelegate[msg.sender][validator] = delegate;
        votingDelegateGeneration[msg.sender][validator] =
            IULRCustodianValidator(validator).custodianGeneration(delegate);
        emit VotingDelegateChanged(msg.sender, validator, delegate);
    }

    /// @notice Revoked or removed custodians lose delegation immediately; the owner regains their vote.
    function getVotingDelegate(address owner, address validator) public view returns (address) {
        address delegate = configuredVotingDelegate[owner][validator];
        if (delegate == address(0)) return owner;
        if (
            IULRCustodianValidator(validator).candidateStaking() != address(this)
                || !IULRCustodianValidator(validator).isCustodian(delegate)
                || IULRCustodianValidator(validator).custodianGeneration(delegate)
                    != votingDelegateGeneration[owner][validator]
        ) return owner;
        return delegate;
    }

    /// @notice Adds real USD-token proceeds; multiple sources and late top-ups are supported.
    /// @dev Only msg.sender's tokens are pulled. A direct ERC-20 transfer is not funding.
    function fundDay(uint256 day, uint256 amount) external nonReentrant {
        if (day < firstEligibleDay || day >= block.timestamp / 1 days) revert InvalidDay();
        if (amount == 0) revert InvalidAmount();
        uint256 eligible = totalEligibleStake(day);
        if (eligible == 0) revert NoEligibleStake();
        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        rewardToken.safeTransferFrom(msg.sender, address(this), amount);
        if (rewardToken.balanceOf(address(this)) - beforeBalance != amount) revert InvalidTransfer();
        dayEligibleTotal[day] = eligible;
        dayReward[day] += amount;
        sourceDayFunding[day][msg.sender] += amount;
        totalFundedRewards += amount;
        emit DayFunded(day, msg.sender, amount, eligible);
    }

    function claim(uint256[] calldata days_) external nonReentrant returns (uint256 reward) {
        _validateDays(days_);
        for (uint256 i = 0; i < days_.length; i++) {
            uint256 day = days_[i];
            uint256 entitlement = _entitlement(msg.sender, day);
            uint256 amount = entitlement - claimedReward[msg.sender][day];
            if (amount == 0) continue;
            claimedReward[msg.sender][day] = entitlement;
            reward += amount;
            emit RewardClaimed(msg.sender, day, amount);
        }
        if (reward > 0) {
            totalClaimedRewards += reward;
            _transferExact(rewardToken, msg.sender, reward);
        }
    }

    function claimableReward(address owner, uint256[] calldata days_) external view returns (uint256 reward) {
        _validateDays(days_);
        for (uint256 i = 0; i < days_.length; i++) {
            reward += _entitlement(owner, days_[i]) - claimedReward[owner][days_[i]];
        }
    }

    /// @notice Includes unclaimed entitlements and rounding dust; no administrator can withdraw it.
    function rewardLiability() external view returns (uint256) {
        return totalFundedRewards - totalClaimedRewards;
    }

    function _entitlement(address owner, uint256 day) private view returns (uint256) {
        uint256 eligible = dayEligibleTotal[day];
        if (eligible == 0) return 0;
        return Math.mulDiv(dayReward[day], eligibleStake(owner, day), eligible);
    }

    function _validateDays(uint256[] calldata days_) private view {
        if (days_.length == 0 || days_.length > MAX_CLAIM_DAYS) revert InvalidClaimBatch();
        uint256 today = block.timestamp / 1 days;
        for (uint256 i = 0; i < days_.length; i++) {
            if (days_[i] < firstEligibleDay || days_[i] >= today) revert InvalidDay();
            if (i > 0 && days_[i] <= days_[i - 1]) revert InvalidClaimBatch();
        }
    }

    function _transferExact(IERC20 token, address recipient, uint256 amount) private {
        uint256 beforeBalance = token.balanceOf(address(this));
        uint256 beforeRecipient = token.balanceOf(recipient);
        token.safeTransfer(recipient, amount);
        if (
            beforeBalance - token.balanceOf(address(this)) != amount
                || token.balanceOf(recipient) - beforeRecipient != amount
        ) revert InvalidTransfer();
    }

    function _writeCheckpoint(Checkpoint[] storage checkpoints, uint256 balance) private {
        if (balance > type(uint192).max) revert InvalidAmount();
        uint64 timestamp = uint64(block.timestamp);
        uint256 length = checkpoints.length;
        if (length > 0 && checkpoints[length - 1].timestamp == timestamp) {
            checkpoints[length - 1].balance = uint192(balance);
        } else {
            checkpoints.push(Checkpoint(timestamp, uint192(balance)));
        }
    }

    function _balanceBefore(Checkpoint[] storage checkpoints, uint256 timestamp) private view returns (uint256) {
        uint256 low;
        uint256 high = checkpoints.length;
        while (low < high) {
            uint256 mid = (low + high) / 2;
            if (checkpoints[mid].timestamp < timestamp) low = mid + 1;
            else high = mid;
        }
        return low == 0 ? 0 : checkpoints[low - 1].balance;
    }
}
