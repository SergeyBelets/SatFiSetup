// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {CustodianValidator} from "./CustodianValidator.sol";

/// @notice One confederation's stake history using the common ULR token.
contract ConfederationDAOStake is ReentrancyGuard, EIP712 {
    using SafeERC20 for IERC20;

    struct Checkpoint {
        uint64 timestamp;
        uint192 balance;
    }

    bytes32 private constant SIGNER_CONSENT_TYPEHASH =
        keccak256("SignerConsent(address owner,address signer,uint256 nonce)");

    IERC20 public immutable stakeToken;
    uint256 public immutable CUSTODIAN_THRESHOLD;
    CustodianValidator public immutable custodianValidator;
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
    address[] private nominees;
    mapping(address => bool) private nominated;
    mapping(address => address) private configuredSigner;
    mapping(address => uint256) private configuredCustodianGeneration;
    mapping(address => uint256) public signerConsentNonce;

    event Staked(address indexed owner, uint256 amount);
    event Unstaked(address indexed owner, uint256 amount);
    event DAOSignerChanged(address indexed owner, address indexed signer);
    error InvalidAddress();
    error InvalidAmount();
    error InvalidSignerConsent();
    error InvalidTransfer();

    constructor(address stakeToken_, address validator_, uint256 threshold) EIP712("ConfederationDAOStake", "1") {
        if (stakeToken_.code.length == 0 || validator_.code.length == 0) revert InvalidAddress();
        if (threshold == 0 || threshold > 10_000_000 ether) revert InvalidAmount();
        CUSTODIAN_THRESHOLD = threshold;
        stakeToken = IERC20(stakeToken_);
        custodianValidator = CustodianValidator(validator_);
        firstEligibleDay = block.timestamp / 1 days + (block.timestamp % 1 days == 0 ? 0 : 1);
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
        if (!nominated[msg.sender]) {
            nominated[msg.sender] = true;
            nominees.push(msg.sender);
        }
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

    function isCustodianCandidate(address owner) public view returns (bool) {
        return nominated[owner] && stakeToken.balanceOf(owner) + stakedBalance[owner] >= CUSTODIAN_THRESHOLD;
    }

    function getCustodianCandidates(uint256 offset, uint256 limit)
        external view returns (address[] memory candidates, uint256 nextOffset)
    {
        if (offset > nominees.length || limit == 0 || limit > 100) revert InvalidAmount();
        nextOffset = offset + limit;
        if (nextOffset > nominees.length) nextOffset = nominees.length;
        candidates = new address[](nextOffset - offset);
        uint256 length;
        for (uint256 i = offset; i < nextOffset; ++i) {
            address owner = nominees[i];
            if (isCustodianCandidate(owner)) candidates[length++] = owner;
        }
        assembly ("memory-safe") { mstore(candidates, length) }
    }

    function setDAOSigner(address signer, bytes calldata consentSignature) external {
        if (signer == address(0)) revert InvalidAddress();
        uint256 generation;
        if (signer != msg.sender) {
            if (consentSignature.length == 0 && custodianValidator.isCustodian(signer)) {
                generation = custodianValidator.custodianGeneration(signer);
            } else {
                bytes32 digest = getSignerConsentDigest(msg.sender, signer);
                if (!SignatureChecker.isValidSignatureNow(signer, digest, consentSignature)) {
                    revert InvalidSignerConsent();
                }
            }
        }
        signerConsentNonce[msg.sender]++;
        configuredSigner[msg.sender] = signer;
        configuredCustodianGeneration[msg.sender] = generation;
        emit DAOSignerChanged(msg.sender, signer);
    }

    function getDAOSigner(address owner) public view returns (address) {
        address signer = configuredSigner[owner];
        if (signer == address(0)) return owner;
        uint256 generation = configuredCustodianGeneration[owner];
        if (generation != 0 && (!custodianValidator.isCustodian(signer)
            || custodianValidator.custodianGeneration(signer) != generation)) return owner;
        return signer;
    }

    function getSignerConsentDigest(address owner, address signer) public view returns (bytes32) {
        return
            _hashTypedDataV4(keccak256(abi.encode(SIGNER_CONSENT_TYPEHASH, owner, signer, signerConsentNonce[owner])));
    }

    function eligibleStake(address owner, uint256 day) public view returns (uint256) {
        if (day < firstEligibleDay || day > block.timestamp / 1 days) return 0;
        uint256 opening = _balanceBefore(userCheckpoints[owner], day * 1 days);
        return opening + stakedAtDayStart[owner][day] - withdrawnEligible[owner][day];
    }

    function totalEligibleStake(uint256 day) public view returns (uint256) {
        if (day < firstEligibleDay || day > block.timestamp / 1 days) return 0;
        uint256 opening = _balanceBefore(totalCheckpoints, day * 1 days);
        return opening + totalStakedAtDayStart[day] - totalWithdrawnEligible[day];
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
