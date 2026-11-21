// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "./SimpleSPVContract.sol";
import "../tokens/PoolMpToken.sol";
import "./BitcoinCommentParser.sol";

/**
 * @title SimpleMiningPoolWithBridge
 * @notice Mining pool with bridge functionality via OP_RETURN parsing
 * @dev Extends basic pool with:
 *      - Targeted deposits via OP_RETURN Ethereum address
 *      - Bridge functionality for direct BTC -> specific user's MP tokens
 */
contract SimpleMiningPoolWithBridge is Ownable, ReentrancyGuard {
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
    bytes public payoutScript;

    // Rewards tracking
    struct RewardUTXO {
        bytes32 txid;
        uint32 vout;
        uint64 amountSat;
        bytes32 blockHash;
        bool isRegistered;
        bool isDistributed;
        address targetAddress; // If specified via OP_RETURN, send only to this address
    }

    mapping(bytes32 => RewardUTXO) public rewards;
    uint256 public totalRewardsRegistered;
    uint256 public totalRewardsDistributed;

    // Bridge deposits tracking
    struct BridgeDeposit {
        bytes32 txid;
        uint64 amountSat;
        address recipient;
        bytes32 blockHash;
        bool isRegistered;
        bool isProcessed;
        uint256 timestamp;
    }

    mapping(bytes32 => BridgeDeposit) public bridgeDeposits;
    uint256 public totalBridgeDeposits;

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
    event RewardRegistered(bytes32 indexed utxoKey, uint64 amountSat, bytes32 blockHash, address targetAddress);
    event RewardDistributed(bytes32 indexed utxoKey, address[] recipients, uint256[] amounts);
    event BridgeDepositRegistered(bytes32 indexed txid, address indexed recipient, uint64 amountSat);
    event BridgeDepositProcessed(bytes32 indexed txid, address indexed recipient, uint64 amountSat);
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

    // ======== REWARDS (Regular pool distribution) ========

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

        require(spvContract.blockExists(blockHash), "Block not found");
        require(spvContract.isMatureWithDepth(blockHash, confirmationDepth), "Block not mature");

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
            isDistributed: false,
            targetAddress: address(0)
        });

        totalRewardsRegistered += amountSat;
        spvContract.registerPoolUTXO(txid, vout);

        emit RewardRegistered(utxoKey, amountSat, blockHash, address(0));
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

    // ======== BRIDGE FUNCTIONALITY ========

    /**
     * @notice Register targeted deposit with Ethereum address from OP_RETURN
     * @param txid Bitcoin transaction ID
     * @param vout Output index
     * @param amountSat Amount in satoshis
     * @param blockHash Block containing transaction
     * @param siblings Merkle proof siblings
     * @param index Transaction index in block
     * @param opReturnData OP_RETURN data containing "ETH:<address>"
     * @return depositKey Deposit identifier
     */
    function registerBridgeDeposit(
        bytes32 txid,
        uint32 vout,
        uint64 amountSat,
        bytes32 blockHash,
        bytes32[] calldata siblings,
        uint256 index,
        bytes calldata opReturnData
    ) external onlyOwner returns (bytes32 depositKey) {
        depositKey = keccak256(abi.encodePacked(txid, vout, "bridge"));

        require(!bridgeDeposits[depositKey].isRegistered, "Already registered");
        require(amountSat > 0, "Zero amount");

        // Verify block and transaction
        require(spvContract.blockExists(blockHash), "Block not found");
        require(spvContract.isMatureWithDepth(blockHash, confirmationDepth), "Block not mature");
        require(
            spvContract.checkTxInclusion(blockHash, txid, siblings, index),
            "TX not in block"
        );

        // Parse Ethereum address from OP_RETURN
        (address recipient, bool hasAddress) = BitcoinCommentParser.parseEthereumAddress(opReturnData);
        require(hasAddress, "No valid ETH address in OP_RETURN");
        require(recipient != address(0), "Invalid recipient");

        // Register bridge deposit
        bridgeDeposits[depositKey] = BridgeDeposit({
            txid: txid,
            amountSat: amountSat,
            recipient: recipient,
            blockHash: blockHash,
            isRegistered: true,
            isProcessed: false,
            timestamp: block.timestamp
        });

        totalBridgeDeposits++;
        spvContract.registerPoolUTXO(txid, vout);

        emit BridgeDepositRegistered(txid, recipient, amountSat);
    }

    /**
     * @notice Process bridge deposit - mint MP tokens to specified recipient
     * @param depositKey Bridge deposit identifier
     */
    function processBridgeDeposit(bytes32 depositKey) external onlyOwner nonReentrant {
        require(address(mpToken) != address(0), "MP token not set");

        BridgeDeposit storage deposit = bridgeDeposits[depositKey];
        require(deposit.isRegistered, "Not registered");
        require(!deposit.isProcessed, "Already processed");
        require(deposit.amountSat > 0, "Invalid deposit");

        // Mint MP tokens directly to recipient (convert satoshis to token units)
        mpToken.mint(deposit.recipient, uint256(deposit.amountSat) * SAT_TO_TOKEN_MULTIPLIER);

        deposit.isProcessed = true;
        totalRewardsDistributed += deposit.amountSat;

        emit BridgeDepositProcessed(deposit.txid, deposit.recipient, deposit.amountSat);
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

    // ======== VIEW FUNCTIONS ========

    function getReward(bytes32 utxoKey) external view returns (RewardUTXO memory) {
        return rewards[utxoKey];
    }

    function getBridgeDeposit(bytes32 depositKey) external view returns (BridgeDeposit memory) {
        return bridgeDeposits[depositKey];
    }

    function getRedemption(uint256 redemptionId) external view returns (Redemption memory) {
        return redemptions[redemptionId];
    }

    function getMpTokenBalance(address account) external view returns (uint256) {
        if (address(mpToken) == address(0)) return 0;
        return mpToken.balanceOf(account);
    }

    /**
     * @notice Get all pending bridge deposits
     * @return depositKeys Array of deposit keys
     * @return deposits Array of deposit details
     */
    function getPendingBridgeDeposits()
        external
        pure
        returns (bytes32[] memory depositKeys, BridgeDeposit[] memory deposits)
    {
        // This is a simplified version for demo
        // In production, would need to track deposit keys in an array
        depositKeys = new bytes32[](0);
        deposits = new BridgeDeposit[](0);
    }
}
