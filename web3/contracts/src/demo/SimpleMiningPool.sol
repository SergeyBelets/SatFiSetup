// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "./SimpleSPVContract.sol";
import "../tokens/PoolMpToken.sol";

/**
 * @title SimpleMiningPool
 * @notice Minimal mining pool for demo - no FROST, no Factory, single owner
 * @dev Simplified flow: Bitcoin rewards → MP tokens → BTC redemption
 */
contract SimpleMiningPool is Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ======== CONSTANTS ========
    /// @notice Multiplier to convert satoshis (8 decimals) to token units (18 decimals)
    uint256 public constant SAT_TO_TOKEN_MULTIPLIER = 1e10;

    // ======== STATE ========
    SimpleSPVContract public immutable spvContract;
    PoolMpToken public mpToken;

    /// @notice Custom SPV confirmation depth (0 = use default)
    uint32 public confirmationDepth;

    string public poolId;
    bytes public payoutScript; // Bitcoin payout script

    // Rewards tracking
    struct RewardUTXO {
        bytes32 txid;
        uint32 vout;
        uint64 amountSat;
        bytes32 blockHash;
        bool isRegistered;
        bool isDistributed;
    }

    mapping(bytes32 => RewardUTXO) public rewards;
    uint256 public totalRewardsRegistered;
    uint256 public totalRewardsDistributed;

    // Redemptions tracking
    struct Redemption {
        address requester;
        uint64 amountSat;
        bytes btcScript;
        bool isConfirmed;
        uint256 timestamp;
    }

    mapping(uint256 => Redemption) public redemptions;
    mapping(uint256 => bool) public redemptionApproved;
    mapping(uint256 => bool) public redemptionRefunded;
    uint256 public redemptionCounter;

    // ======== EVENTS ========
    event RewardRegistered(bytes32 indexed utxoKey, uint64 amountSat, bytes32 blockHash);
    event RewardDistributed(bytes32 indexed utxoKey, address[] recipients, uint256[] amounts);
    event RedemptionRequested(uint256 indexed redemptionId, address indexed requester, uint64 amountSat);
    event RedemptionConfirmed(uint256 indexed redemptionId, bool approved);
    event RedemptionRefunded(uint256 indexed redemptionId, address indexed requester, uint256 tokenAmount);

    // ======== CONSTRUCTOR ========
    constructor(
        address _spvContract,
        string memory _poolId,
        bytes memory _payoutScript,
        uint32 _confirmationDepth
    ) Ownable(msg.sender) {
        require(_spvContract != address(0), "Invalid SPV");
        require(bytes(_poolId).length > 0, "Empty pool ID");

        spvContract = SimpleSPVContract(_spvContract);
        poolId = _poolId;
        payoutScript = _payoutScript;
        confirmationDepth = _confirmationDepth;

        // SPV admin must register this pool after deployment.
    }

    // ======== SETUP ========

    function setMpToken(address _mpToken) external onlyOwner {
        require(address(mpToken) == address(0), "MP token already set");
        require(_mpToken != address(0), "Invalid MP token");
        mpToken = PoolMpToken(_mpToken);
    }

    function setPayoutScript(bytes calldata _script) external onlyOwner {
        payoutScript = _script;
    }

    // ======== REWARDS ========

    function registerReward(
        bytes32 txid,
        uint32 vout,
        uint64 amountSat,
        bytes32 blockHash,
        bytes32[] calldata siblings,
        uint256 index
    ) external onlyOwner returns (bytes32 utxoKey) {
        utxoKey = keccak256(abi.encodePacked(txid, vout));

        require(!rewards[utxoKey].isRegistered, "Already registered");
        require(amountSat > 0, "Zero amount");

        // Verify block exists and is mature
        require(spvContract.blockExists(blockHash), "Block not found");
        require(spvContract.isMatureWithDepth(blockHash, confirmationDepth), "Block not mature");

        // Verify transaction inclusion
        require(
            spvContract.checkTxInclusion(blockHash, txid, siblings, index),
            "TX not in block"
        );

        rewards[utxoKey] = RewardUTXO({
            txid: txid,
            vout: vout,
            amountSat: amountSat,
            blockHash: blockHash,
            isRegistered: true,
            isDistributed: false
        });

        totalRewardsRegistered += amountSat;

        // Register this UTXO in SPV for source verification
        spvContract.registerPoolUTXO(txid, vout);

        emit RewardRegistered(utxoKey, amountSat, blockHash);
    }

    function distributeReward(
        bytes32 utxoKey,
        address[] calldata recipients,
        uint256[] calldata amounts
    ) external onlyOwner nonReentrant {
        require(address(mpToken) != address(0), "MP token not set");
        require(recipients.length == amounts.length, "Length mismatch");
        require(recipients.length > 0, "No recipients");

        RewardUTXO storage reward = rewards[utxoKey];
        require(reward.isRegistered, "Not registered");
        require(!reward.isDistributed, "Already distributed");

        // Verify total amount matches
        uint256 totalAmount = 0;
        for (uint256 i = 0; i < amounts.length; i++) {
            totalAmount += amounts[i];
        }
        require(totalAmount == reward.amountSat, "Amount mismatch");

        // Mint MP tokens to recipients (convert satoshis to token units)
        for (uint256 i = 0; i < recipients.length; i++) {
            if (amounts[i] > 0) {
                mpToken.mint(recipients[i], amounts[i] * SAT_TO_TOKEN_MULTIPLIER);
            }
        }

        reward.isDistributed = true;
        totalRewardsDistributed += reward.amountSat;

        emit RewardDistributed(utxoKey, recipients, amounts);
    }

    // ======== REDEMPTIONS ========

    function requestRedemption(
        uint64 amountSat,
        bytes calldata btcScript
    ) external nonReentrant returns (uint256 redemptionId) {
        require(address(mpToken) != address(0), "MP token not set");
        require(amountSat > 0, "Zero amount");
        require(btcScript.length > 0, "Empty script");

        uint256 tokenAmount = uint256(amountSat) * SAT_TO_TOKEN_MULTIPLIER;
        IERC20(address(mpToken)).safeTransferFrom(msg.sender, address(this), tokenAmount);

        redemptionId = redemptionCounter++;
        redemptions[redemptionId] = Redemption({
            requester: msg.sender,
            amountSat: amountSat,
            btcScript: btcScript,
            isConfirmed: false,
            timestamp: block.timestamp
        });

        emit RedemptionRequested(redemptionId, msg.sender, amountSat);
    }

    function confirmRedemption(uint256 redemptionId, bool approved) external onlyOwner {
        Redemption storage redemption = redemptions[redemptionId];
        require(redemption.amountSat > 0, "Redemption not found");
        require(!redemption.isConfirmed, "Already confirmed");

        redemption.isConfirmed = true;
        redemptionApproved[redemptionId] = approved;

        uint256 tokenAmount = uint256(redemption.amountSat) * SAT_TO_TOKEN_MULTIPLIER;
        if (approved) {
            mpToken.burn(address(this), tokenAmount);
        } else {
            redemptionRefunded[redemptionId] = true;
            IERC20(address(mpToken)).safeTransfer(redemption.requester, tokenAmount);
            emit RedemptionRefunded(redemptionId, redemption.requester, tokenAmount);
        }

        emit RedemptionConfirmed(redemptionId, approved);
    }

    // ======== VIEWS ========

    function getReward(bytes32 utxoKey) external view returns (RewardUTXO memory) {
        return rewards[utxoKey];
    }

    function getRedemption(uint256 redemptionId) external view returns (Redemption memory) {
        return redemptions[redemptionId];
    }

    function getMpTokenBalance(address account) external view returns (uint256) {
        if (address(mpToken) == address(0)) return 0;
        return mpToken.balanceOf(account);
    }

    // ======== ADMIN VIEWS ========

    /**
     * @notice Get list of all pending redemptions
     * @return pendingIds Array of redemption IDs that are not yet confirmed
     * @return pendingRedemptions Array of corresponding redemption details
     */
    function getPendingRedemptions()
        external
        view
        returns (uint256[] memory pendingIds, Redemption[] memory pendingRedemptions)
    {
        // First pass: count pending redemptions
        uint256 pendingCount = 0;
        for (uint256 i = 0; i < redemptionCounter; i++) {
            if (redemptions[i].amountSat > 0 && !redemptions[i].isConfirmed) {
                pendingCount++;
            }
        }

        // Second pass: collect pending redemptions
        pendingIds = new uint256[](pendingCount);
        pendingRedemptions = new Redemption[](pendingCount);

        uint256 index = 0;
        for (uint256 i = 0; i < redemptionCounter; i++) {
            if (redemptions[i].amountSat > 0 && !redemptions[i].isConfirmed) {
                pendingIds[index] = i;
                pendingRedemptions[index] = redemptions[i];
                index++;
            }
        }
    }

    /**
     * @notice Get detailed information about a specific redemption request
     * @param redemptionId The redemption ID to query
     * @return redemption The full redemption details
     * @return requesterBalance Current MP token balance of the requester
     * @return timeSinceRequest Time elapsed since request in seconds
     */
    function getRedemptionDetails(uint256 redemptionId)
        external
        view
        returns (
            Redemption memory redemption,
            uint256 requesterBalance,
            uint256 timeSinceRequest
        )
    {
        redemption = redemptions[redemptionId];
        require(redemption.amountSat > 0, "Redemption not found");

        requesterBalance = this.getMpTokenBalance(redemption.requester);
        timeSinceRequest = block.timestamp - redemption.timestamp;
    }

    /**
     * @notice Get statistics about redemptions
     * @return totalRequests Total number of redemption requests
     * @return pendingCount Number of pending redemptions
     * @return confirmedCount Number of confirmed redemptions
     * @return totalPendingAmount Total amount in pending redemptions (satoshis)
     * @return totalConfirmedAmount Total amount in confirmed redemptions (satoshis)
     */
    function getRedemptionStats()
        external
        view
        returns (
            uint256 totalRequests,
            uint256 pendingCount,
            uint256 confirmedCount,
            uint256 totalPendingAmount,
            uint256 totalConfirmedAmount
        )
    {
        totalRequests = redemptionCounter;

        for (uint256 i = 0; i < redemptionCounter; i++) {
            if (redemptions[i].amountSat > 0) {
                if (redemptions[i].isConfirmed) {
                    confirmedCount++;
                    totalConfirmedAmount += redemptions[i].amountSat;
                } else {
                    pendingCount++;
                    totalPendingAmount += redemptions[i].amountSat;
                }
            }
        }
    }

    /**
     * @notice Get all reward addresses and their paid rewards
     * @return addresses Array of unique addresses that received rewards
     * @return totalPaid Array of total amounts paid to each address
     */
    function getRewardAddresses()
        external
        view
        returns (
            address[] memory addresses,
            uint256[] memory totalPaid
        )
    {
        require(address(mpToken) != address(0), "MP token not set");

        // Note: This is a simplified version that returns all addresses
        // that currently hold MP tokens. For full history including addresses
        // that no longer hold tokens, an event-based indexer would be needed.

        // This implementation assumes we track recipients in a mapping
        // For demo purposes, we return empty arrays as proper implementation
        // would require additional state tracking
        addresses = new address[](0);
        totalPaid = new uint256[](0);
    }

    /**
     * @notice Get all paid rewards for a specific address
     * @param recipient The address to query
     * @return currentBalance Current MP token balance
     * @return totalRedeemed Total amount redeemed by this address
     */
    function getAddressRewards(address recipient)
        external
        view
        returns (
            uint256 currentBalance,
            uint256 totalRedeemed
        )
    {
        currentBalance = this.getMpTokenBalance(recipient);

        // Calculate total redeemed by this address
        for (uint256 i = 0; i < redemptionCounter; i++) {
            if (redemptions[i].requester == recipient) {
                totalRedeemed += redemptions[i].amountSat;
            }
        }
    }

    /**
     * @notice Get paginated list of redemptions for better UI performance
     * @param offset Starting index
     * @param limit Maximum number of results
     * @return ids Array of redemption IDs
     * @return redeems Array of redemption details
     * @return total Total number of redemptions
     */
    function getRedemptionsPaginated(uint256 offset, uint256 limit)
        external
        view
        returns (
            uint256[] memory ids,
            Redemption[] memory redeems,
            uint256 total
        )
    {
        total = redemptionCounter;

        if (offset >= total) {
            return (new uint256[](0), new Redemption[](0), total);
        }

        uint256 end = offset + limit;
        if (end > total) {
            end = total;
        }

        uint256 resultCount = end - offset;
        ids = new uint256[](resultCount);
        redeems = new Redemption[](resultCount);

        for (uint256 i = 0; i < resultCount; i++) {
            uint256 redemptionId = offset + i;
            ids[i] = redemptionId;
            redeems[i] = redemptions[redemptionId];
        }
    }
}
