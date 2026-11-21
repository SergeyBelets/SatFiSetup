// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../libs/BlockHeader.sol";
import "../libs/TxMerkleProof.sol";
import "../libs/TargetsHelper.sol";
import "../core/BitcoinTxParser.sol";

/**
 * @title SimpleSPVContract
 * @notice Simplified SPV for demo: 6 confirmations instead of 100
 * @dev Minimal Bitcoin SPV verification without difficulty adjustment
 */
contract SimpleSPVContract {
    // DEMO: 6 confirmations instead of 100
    uint32 public constant COINBASE_MATURITY = 6;

    address public spvAdmin;
    bool public testHelpersEnabled = true;

    struct BlockData {
        bytes32 prev;
        bytes32 merkleRoot;
        uint32 version;
        uint32 time;
        uint32 nonce;
        uint32 bits;
        uint64 height;
        uint256 cumulativeWork;  // Total work up to this block
    }

    mapping(bytes32 => BlockData) public blocks;
    mapping(uint64 => bytes32) public height2hash;
    bytes32 public mainchainHead;

    // Trusted block configuration
    bytes32 public immutable trustedBlockHash;
    uint64 public immutable trustedBlockHeight;
    bytes32 public immutable prevAdjustmentBlockHash;  // Previous adjustment block for difficulty calculation
    uint32 public immutable prevAdjustmentBits;        // Bits of the previous adjustment block
    uint32 public immutable prevAdjustmentTime;        // Time of the previous adjustment block

    // Pool UTXO tracking for source verification
    mapping(bytes32 => mapping(uint32 => bool)) public poolUTXOs; // txid => vout => isPoolUTXO
    mapping(address => bool) public poolContracts; // Multiple pool contracts can register

    event BlockHeaderAdded(uint64 height, bytes32 blockHash);
    event PoolUTXORegistered(bytes32 indexed txid, uint32 vout);
    event PoolUTXOSpent(bytes32 indexed txid, uint32 vout);
    event PoolContractRegistered(address indexed poolContract);
    event PoolContractRemoved(address indexed poolContract);
    event SPVAdminTransferred(address indexed oldAdmin, address indexed newAdmin);
    event TestHelpersPermanentlyDisabled();
    event ReorgOccurred(bytes32 oldHead, bytes32 newHead, uint64 depth);
    event MainchainHeadUpdated(uint64 height, bytes32 head);

    error InvalidHeaderLength();
    error BlockAlreadyExists(bytes32);
    error PrevNotFound(bytes32);
    error OnlyPoolContract();
    error OnlySPVAdmin();
    error InvalidTxSource();
    error ReorgTooDeep();
    error InvalidProofOfWork();
    error InvalidDifficultyBits();
    error SPVTestHelpersDisabled();

    modifier onlySPVAdmin() {
        if (msg.sender != spvAdmin) revert OnlySPVAdmin();
        _;
    }

    modifier onlyTestHelpersEnabled() {
        if (!testHelpersEnabled) revert SPVTestHelpersDisabled();
        _;
    }

    /**
     * @notice Constructor with optional trusted block
     * @param _trustedBlockHash Hash of the trusted block (or bytes32(0) for genesis)
     * @param _trustedBlockHeight Height of the trusted block
     * @param _trustedMerkleRoot Merkle root of the trusted block
     * @param _prevAdjustmentBlockHash Hash of the previous adjustment block (for difficulty calculation)
     * @param _prevAdjustmentBits Difficulty bits of the previous adjustment block
     * @param _prevAdjustmentTime Timestamp of the previous adjustment block
     */
    constructor(
        bytes32 _trustedBlockHash,
        uint64 _trustedBlockHeight,
        bytes32 _trustedMerkleRoot,
        bytes32 _prevAdjustmentBlockHash,
        uint32 _prevAdjustmentBits,
        uint32 _prevAdjustmentTime
    ) {
        spvAdmin = msg.sender;

        trustedBlockHash = _trustedBlockHash;
        trustedBlockHeight = _trustedBlockHeight;
        prevAdjustmentBlockHash = _prevAdjustmentBlockHash;
        prevAdjustmentBits = _prevAdjustmentBits;
        prevAdjustmentTime = _prevAdjustmentTime;

        // If a trusted block is specified, add it to the chain
        if (_trustedBlockHash != bytes32(0)) {
            // Calculate work for trusted block
            uint256 target = TargetsHelper.bitsToTarget(0x1d00ffff);
            uint256 work = TargetsHelper.workFromTarget(target);

            blocks[_trustedBlockHash] = BlockData({
                prev: bytes32(0),
                merkleRoot: _trustedMerkleRoot,
                version: 1,
                time: uint32(block.timestamp),
                nonce: 0,
                bits: 0x1d00ffff,
                height: _trustedBlockHeight,
                cumulativeWork: work * _trustedBlockHeight  // Approximate cumulative work
            });
            height2hash[_trustedBlockHeight] = _trustedBlockHash;
            mainchainHead = _trustedBlockHash;
            emit BlockHeaderAdded(_trustedBlockHeight, _trustedBlockHash);

            // Add synthetic previous adjustment block if specified
            // This is needed for difficulty calculation when trusted block is not on adjustment boundary
            if (_prevAdjustmentBlockHash != bytes32(0)) {
                uint64 adjustmentHeight = (_trustedBlockHeight / TargetsHelper.DIFFICULTY_ADJUSTMENT_INTERVAL)
                    * TargetsHelper.DIFFICULTY_ADJUSTMENT_INTERVAL;

                blocks[_prevAdjustmentBlockHash] = BlockData({
                    prev: bytes32(0),
                    merkleRoot: bytes32(0),
                    version: 1,
                    time: _prevAdjustmentTime,
                    nonce: 0,
                    bits: _prevAdjustmentBits,
                    height: adjustmentHeight,
                    cumulativeWork: work * adjustmentHeight  // Approximate cumulative work
                });
                height2hash[adjustmentHeight] = _prevAdjustmentBlockHash;

                emit BlockHeaderAdded(adjustmentHeight, _prevAdjustmentBlockHash);
            }
        }
    }

    /**
     * @notice Set the pool contract address (multiple pools can register)
     * @param _poolContract Address of the mining pool contract
     */
    function setPoolContract(address _poolContract) external onlySPVAdmin {
        require(_poolContract != address(0), "Invalid pool address");
        require(!poolContracts[_poolContract], "Pool already registered");
        poolContracts[_poolContract] = true;
        emit PoolContractRegistered(_poolContract);
    }

    function removePoolContract(address _poolContract) external onlySPVAdmin {
        require(poolContracts[_poolContract], "Pool not registered");
        poolContracts[_poolContract] = false;
        emit PoolContractRemoved(_poolContract);
    }

    function transferSPVAdmin(address newAdmin) external onlySPVAdmin {
        require(newAdmin != address(0), "Invalid admin");
        address oldAdmin = spvAdmin;
        spvAdmin = newAdmin;
        emit SPVAdminTransferred(oldAdmin, newAdmin);
    }

    function disableTestHelpers() external onlySPVAdmin {
        testHelpersEnabled = false;
        emit TestHelpersPermanentlyDisabled();
    }

    modifier onlyPool() {
        if (!poolContracts[msg.sender]) revert OnlyPoolContract();
        _;
    }

    // ======== Views ========

    function blockExists(bytes32 bh) public view returns (bool) {
        return blocks[bh].time != 0;
    }

    function isInMainchain(bytes32 bh) public view returns (bool) {
        uint64 h = blocks[bh].height;
        return h != 0 && height2hash[h] == bh;
    }

    function getBlockStatus(bytes32 bh) external view returns (bool isIn, uint64 confirmations) {
        uint64 h = blocks[bh].height;
        if (h == 0) return (false, 0);
        isIn = (height2hash[h] == bh);
        if (!isIn) return (false, 0);

        uint64 headH = blocks[mainchainHead].height;
        confirmations = headH >= h ? headH - h + 1 : 0;
    }

    function isMature(bytes32 bh) external view returns (bool) {
        uint64 h = blocks[bh].height;
        if (h == 0 || !isInMainchain(bh)) return false;
        uint64 head = blocks[mainchainHead].height;
        return head >= h + COINBASE_MATURITY;
    }

    /**
     * @notice Check if a block has enough confirmations with custom depth
     * @param bh Block hash to check
     * @param confirmationDepth Custom confirmation depth (0 = use default COINBASE_MATURITY)
     * @return True if block has at least confirmationDepth confirmations
     */
    function isMatureWithDepth(bytes32 bh, uint32 confirmationDepth) external view returns (bool) {
        uint64 h = blocks[bh].height;
        if (h == 0 || !isInMainchain(bh)) return false;
        uint64 head = blocks[mainchainHead].height;
        uint32 depth = confirmationDepth == 0 ? COINBASE_MATURITY : confirmationDepth;
        return head >= h + depth;
    }

    function getBlockInfo(bytes32 bh) external view returns (
        BlockData memory mainBlockData,
        bool isInMainchainResult,
        uint256 cumulativeWork,
        bool exists
    ) {
        mainBlockData = blocks[bh];
        exists = mainBlockData.time != 0;
        isInMainchainResult = isInMainchain(bh);
        cumulativeWork = mainBlockData.cumulativeWork;
    }

    function getMainchainHead() external view returns (bytes32) {
        return mainchainHead;
    }

    function getMainchainHeight() external view returns (uint64) {
        return blocks[mainchainHead].height;
    }

    function getBlockMerkleRoot(bytes32 bh) external view returns (bytes32) {
        return blocks[bh].merkleRoot;
    }

    function getBlockHeight(bytes32 bh) external view returns (uint64) {
        return blocks[bh].height;
    }

    function getBlockHash(uint64 height) external view returns (bytes32) {
        return height2hash[height];
    }

    function checkTxInclusion(
        bytes32 blockHash,
        bytes32 txid,
        bytes32[] calldata siblings,
        uint256 index
    ) external view returns (bool) {
        if (!blockExists(blockHash)) return false;

        bytes32 merkleRoot = blocks[blockHash].merkleRoot;

        // Verify merkle proof
        return TxMerkleProof.verify(siblings, index, merkleRoot, txid);
    }

    /**
     * @notice Register a pool UTXO for source verification
     * @param txid Transaction ID (little-endian)
     * @param vout Output index
     */
    function registerPoolUTXO(bytes32 txid, uint32 vout) external onlyPool {
        poolUTXOs[txid][vout] = true;
        emit PoolUTXORegistered(txid, vout);
    }

    /**
     * @notice Mark a pool UTXO as spent
     * @param txid Transaction ID (little-endian)
     * @param vout Output index
     */
    function markPoolUTXOSpent(bytes32 txid, uint32 vout) external onlyPool {
        poolUTXOs[txid][vout] = false;
        emit PoolUTXOSpent(txid, vout);
    }

    /**
     * @notice Verify that a transaction spends from pool UTXOs
     * @param rawTx Raw Bitcoin transaction bytes
     * @return isValid True if transaction spends from at least one pool UTXO
     */
    function verifyTxSource(bytes calldata rawTx) external view returns (bool isValid) {
        // Parse all inputs from the transaction
        (bytes32[] memory prevTxIds, uint32[] memory vouts) = BitcoinTxParser.parseAllInputs(rawTx);

        // Check if any input spends from a registered pool UTXO
        for (uint256 i = 0; i < prevTxIds.length; i++) {
            // Flip to little-endian for lookup
            bytes32 prevTxIdLE = BitcoinTxParser.flipBytes32(prevTxIds[i]);
            if (poolUTXOs[prevTxIdLE][vouts[i]]) {
                return true;
            }
        }

        return false;
    }

    /**
     * @notice Check if a specific UTXO is registered as a pool UTXO
     * @param txid Transaction ID (little-endian)
     * @param vout Output index
     */
    function isPoolUTXO(bytes32 txid, uint32 vout) external view returns (bool) {
        return poolUTXOs[txid][vout];
    }

    // ======== Mutations (Simplified) ========

    function addBlockHeader(bytes calldata raw) public virtual {
        if (raw.length != 80) revert InvalidHeaderLength();

        (BlockHeaderData memory h, bytes32 bh) = BlockHeader.parseHeader(raw);
        if (blocks[bh].time != 0) revert BlockAlreadyExists(bh);

        if (h.prevBlockHash != bytes32(0) && blocks[h.prevBlockHash].time == 0) {
            revert PrevNotFound(h.prevBlockHash);
        }

        uint64 height = (h.prevBlockHash == bytes32(0)) ? 0 : blocks[h.prevBlockHash].height + 1;

        // Calculate work and cumulative work
        uint256 target = TargetsHelper.bitsToTarget(h.bits);
        if (target == 0) revert InvalidDifficultyBits();
        if (!_hashMeetsTarget(bh, target)) revert InvalidProofOfWork();
        if (
            h.prevBlockHash != bytes32(0) &&
            !TargetsHelper.isAdjustmentBlock(height) &&
            h.bits != blocks[h.prevBlockHash].bits
        ) {
            revert InvalidDifficultyBits();
        }
        uint256 work = TargetsHelper.workFromTarget(target);
        uint256 cumulative = (h.prevBlockHash == bytes32(0))
            ? work
            : addmod(blocks[h.prevBlockHash].cumulativeWork, work, type(uint256).max);

        blocks[bh] = BlockData({
            prev: h.prevBlockHash,
            merkleRoot: h.merkleRoot,
            version: h.version,
            time: h.time,
            nonce: h.nonce,
            bits: h.bits,
            height: height,
            cumulativeWork: cumulative
        });

        // Check if this is the new mainchain head
        if (mainchainHead == bytes32(0)) {
            mainchainHead = bh;
            height2hash[height] = bh;
            emit MainchainHeadUpdated(height, bh);
        } else {
            if (blocks[bh].cumulativeWork > blocks[mainchainHead].cumulativeWork) {
                // This fork has more work - trigger reorg
                _reorgTo(bh, height);
            } else if (h.prevBlockHash == mainchainHead) {
                // Simple extension of mainchain
                mainchainHead = bh;
                height2hash[height] = bh;
                emit MainchainHeadUpdated(height, bh);
            }
            // else: this is a fork with less work, ignore
        }

        emit BlockHeaderAdded(height, bh);
    }

    /**
     * @notice Reorganize the mainchain to a new head with more cumulative work
     * @param newHead The new mainchain head block hash
     * @param height The height of the new head
     */
    function _reorgTo(bytes32 newHead, uint64 height) internal {
        uint64 maxReorgDepth = 10;
        bytes32 cursor = newHead;
        uint64 h = height;
        bytes32 oldHead = mainchainHead;
        uint64 reorgCount = 0;

        unchecked {
            while (true) {
                // Stop if we've reached a block already on mainchain
                if (height2hash[h] == cursor) break;

                // Update height mapping
                height2hash[h] = cursor;

                // Move to parent
                bytes32 p = blocks[cursor].prev;
                if (p == bytes32(0)) break;

                cursor = p;
                --h;
                ++reorgCount;

                if (reorgCount > maxReorgDepth) revert ReorgTooDeep();
            }
        }

        mainchainHead = newHead;
        emit ReorgOccurred(oldHead, newHead, reorgCount);
        emit MainchainHeadUpdated(height, newHead);
    }

    function _hashMeetsTarget(bytes32 blockHash, uint256 target) internal pure returns (bool) {
        // Bitcoin compares the internal little-endian block hash integer to nBits target.
        return uint256(BitcoinTxParser.flipBytes32(blockHash)) <= target;
    }

    // ======== Test Helpers ========

    function addBlockWithHash(bytes32 targetHash, uint64 targetHeight) external onlySPVAdmin onlyTestHelpersEnabled {
        // Calculate work for test block
        uint256 target = TargetsHelper.bitsToTarget(0x1d00ffff);
        uint256 work = TargetsHelper.workFromTarget(target);

        blocks[targetHash] = BlockData({
            prev: bytes32(0),
            merkleRoot: bytes32(0),
            version: 1,
            time: uint32(block.timestamp),
            nonce: 0,
            bits: 0x1d00ffff,
            height: targetHeight,
            cumulativeWork: work * (targetHeight + 1)  // Approximate cumulative work
        });
        height2hash[targetHeight] = targetHash;
        mainchainHead = targetHash;
        emit BlockHeaderAdded(targetHeight, targetHash);
    }

    function setBlockMerkleRoot(bytes32 blockHash, bytes32 merkleRoot) external onlySPVAdmin onlyTestHelpersEnabled {
        require(blockExists(blockHash), "Block not found");
        blocks[blockHash].merkleRoot = merkleRoot;
    }
}
