// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {CustodianValidator} from "./CustodianValidator.sol";

contract CustodianDAOStaking is ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;

    struct Checkpoint {
        uint64 timestamp;
        uint192 balance;
    }

    bytes32 private constant SIGNER_CONSENT_TYPEHASH =
        keccak256("SignerConsent(address owner,address signer,uint256 nonce)");

    IERC20 public immutable stakeToken;
    IERC20 public immutable rewardToken;
    CustodianValidator public immutable custodianValidator;
    address public immutable setupAuthority;
    address public distributor;
    uint256 public immutable firstEligibleDay;
    uint256 public totalStaked;

    mapping(address => uint256) public stakedBalance;
    mapping(address => Checkpoint[]) private userCheckpoints;
    Checkpoint[] private totalCheckpoints;
    mapping(address => mapping(uint256 => uint256)) public withdrawnEligible;
    mapping(uint256 => uint256) public totalWithdrawnEligible;
    mapping(address => mapping(uint256 => uint256)) public stakedAtDayStart;
    mapping(uint256 => uint256) public totalStakedAtDayStart;
    mapping(address => mapping(uint256 => bool)) public withdrewAtDayStart;
    mapping(uint256 => bool) public dayFunded;
    mapping(uint256 => uint256) public dayReward;
    mapping(uint256 => uint256) public dayEligibleTotal;
    mapping(address => mapping(uint256 => bool)) public claimed;
    mapping(address => address) private configuredSigner;
    mapping(address => uint256) public signerConsentNonce;

    event Staked(address indexed owner, uint256 amount);
    event Unstaked(address indexed owner, uint256 amount);
    event DAOSignerChanged(address indexed owner, address indexed signer);
    event DistributorSet(address indexed distributor);
    event DayFunded(uint256 indexed day, uint256 amount, uint256 eligibleTotal);
    event RewardClaimed(address indexed owner, uint256 amount);

    error InvalidAddress();
    error InvalidAmount();
    error InvalidSignerConsent();
    error Unauthorized();
    error AlreadyConfigured();
    error InvalidDay();
    error AlreadyFunded();
    error NoEligibleStake();
    error InvalidTransfer();
    error InvalidClaimBatch();
    error DayNotFunded();

    constructor(address stakeToken_, address rewardToken_, address validator_) EIP712("CustodianDAOStaking", "1") {
        if (stakeToken_ == address(0) || rewardToken_ == address(0) || validator_ == address(0)) {
            revert InvalidAddress();
        }
        if (stakeToken_ == rewardToken_) revert InvalidAddress();
        stakeToken = IERC20(stakeToken_);
        rewardToken = IERC20(rewardToken_);
        custodianValidator = CustodianValidator(validator_);
        setupAuthority = msg.sender;
        firstEligibleDay = block.timestamp / 1 days + (block.timestamp % 1 days == 0 ? 0 : 1);
    }

    function setDistributor(address distributor_) external {
        if (msg.sender != setupAuthority) revert Unauthorized();
        if (distributor != address(0)) revert AlreadyConfigured();
        if (distributor_ == address(0) || distributor_.code.length == 0) revert InvalidAddress();
        distributor = distributor_;
        emit DistributorSet(distributor_);
    }

    function stake(uint256 amount) external nonReentrant {
        if (amount == 0) revert InvalidAmount();
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
        emit Staked(msg.sender, amount);
    }

    function unstake(uint256 amount) external nonReentrant {
        if (amount == 0 || amount > stakedBalance[msg.sender]) revert InvalidAmount();
        uint256 day = block.timestamp / 1 days;
        uint256 eligibleBefore = eligibleStake(msg.sender, day);
        uint256 newBalance = stakedBalance[msg.sender] - amount;
        uint256 eligibleAfter = newBalance < eligibleBefore ? newBalance : eligibleBefore;
        uint256 removed = eligibleBefore - eligibleAfter;
        withdrawnEligible[msg.sender][day] += removed;
        totalWithdrawnEligible[day] += removed;
        if (block.timestamp % 1 days == 0) withdrewAtDayStart[msg.sender][day] = true;
        stakedBalance[msg.sender] = newBalance;
        totalStaked -= amount;
        _writeCheckpoint(userCheckpoints[msg.sender], newBalance);
        _writeCheckpoint(totalCheckpoints, totalStaked);
        stakeToken.safeTransfer(msg.sender, amount);
        emit Unstaked(msg.sender, amount);
    }

    function setDAOSigner(address signer, bytes calldata consentSignature) external {
        if (signer == address(0)) revert InvalidAddress();
        if (signer != msg.sender && !custodianValidator.isCustodian(signer)) {
            bytes32 digest = getSignerConsentDigest(msg.sender, signer);
            if (!SignatureChecker.isValidSignatureNow(signer, digest, consentSignature)) {
                revert InvalidSignerConsent();
            }
        }
        signerConsentNonce[msg.sender]++;
        configuredSigner[msg.sender] = signer;
        emit DAOSignerChanged(msg.sender, signer);
    }

    function getDAOSigner(address owner) public view returns (address) {
        address signer = configuredSigner[owner];
        return signer == address(0) ? owner : signer;
    }

    function getSignerConsentDigest(address owner, address signer) public view returns (bytes32) {
        return
            _hashTypedDataV4(keccak256(abi.encode(SIGNER_CONSENT_TYPEHASH, owner, signer, signerConsentNonce[owner])));
    }

    function eligibleStake(address owner, uint256 day) public view returns (uint256) {
        if (day < firstEligibleDay) return 0;
        uint256 opening = _balanceBefore(userCheckpoints[owner], day * 1 days);
        return opening + stakedAtDayStart[owner][day] - withdrawnEligible[owner][day];
    }

    function totalEligibleStake(uint256 day) public view returns (uint256) {
        if (day < firstEligibleDay) return 0;
        uint256 opening = _balanceBefore(totalCheckpoints, day * 1 days);
        return opening + totalStakedAtDayStart[day] - totalWithdrawnEligible[day];
    }

    function fundDay(uint256 day, uint256 amount) external nonReentrant {
        if (msg.sender != distributor) revert Unauthorized();
        if (day < firstEligibleDay || block.timestamp < (day + 1) * 1 days) revert InvalidDay();
        if (dayFunded[day]) revert AlreadyFunded();
        uint256 eligible = totalEligibleStake(day);
        if (amount > 0 && eligible == 0) revert NoEligibleStake();
        dayFunded[day] = true;
        dayReward[day] = amount;
        dayEligibleTotal[day] = eligible;
        if (amount > 0) {
            uint256 beforeBalance = rewardToken.balanceOf(address(this));
            rewardToken.safeTransferFrom(msg.sender, address(this), amount);
            if (rewardToken.balanceOf(address(this)) - beforeBalance != amount) revert InvalidTransfer();
        }
        emit DayFunded(day, amount, eligible);
    }

    function claim(uint256[] calldata days_) external nonReentrant returns (uint256 reward) {
        if (days_.length == 0 || days_.length > 32) revert InvalidClaimBatch();
        for (uint256 i = 0; i < days_.length; i++) {
            uint256 day = days_[i];
            if (i > 0 && day <= days_[i - 1]) revert InvalidClaimBatch();
            if (!dayFunded[day]) revert DayNotFunded();
            if (claimed[msg.sender][day]) revert AlreadyFunded();
            claimed[msg.sender][day] = true;
            uint256 eligible = eligibleStake(msg.sender, day);
            if (eligible > 0 && dayEligibleTotal[day] > 0) {
                reward += dayReward[day] * eligible / dayEligibleTotal[day];
            }
        }
        if (reward > 0) rewardToken.safeTransfer(msg.sender, reward);
        emit RewardClaimed(msg.sender, reward);
    }

    function claimableReward(address owner, uint256[] calldata days_) external view returns (uint256 reward) {
        if (days_.length > 32) revert InvalidClaimBatch();
        for (uint256 i = 0; i < days_.length; i++) {
            uint256 day = days_[i];
            if (i > 0 && day <= days_[i - 1]) revert InvalidClaimBatch();
            if (!dayFunded[day] || claimed[owner][day] || dayEligibleTotal[day] == 0) continue;
            reward += dayReward[day] * eligibleStake(owner, day) / dayEligibleTotal[day];
        }
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
