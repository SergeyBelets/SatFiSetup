// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "../libs/TargetsHelper.sol";
import "../core/BitcoinTxParser.sol";

/// @notice Permissionless Bitcoin mainnet relay anchored at an immutable trusted period boundary.
/// @dev Hashes/txids/Merkle nodes use raw SHA256d digests (Bitcoin wire order), as in deposits/escrow.
/// Only PoW comparison reverses the digest to its little-endian integer representation.
/// No administrator, checkpoint replacement, test entry point, or recovery from halt exists.
contract MainnetHeaderRelay {
    uint256 public constant POW_LIMIT = 0x00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff;
    uint64 public constant DIFFICULTY_ADJUSTMENT_INTERVAL = 2016;
    uint256 public constant MAX_MERKLE_DEPTH = 32;

    struct BlockData {
        bytes32 prev;
        bytes32 merkleRoot;
        uint256 cumulativeWork;
        uint64 height;
        uint32 time;
        uint32 bits;
    }

    struct ParsedHeader {
        bytes32 prev;
        bytes32 merkleRoot;
        uint32 time;
        uint32 bits;
    }

    mapping(bytes32 => BlockData) public blocks;
    // One Bitcoin Core-style skip pointer per header, relative to the checkpoint.
    // Unlike a binary-lifting table, storage writes do not grow with relay age.
    mapping(bytes32 => bytes32) internal skips;
    uint32[10] private checkpointPriorTimes;
    bytes32 public immutable checkpointHash;
    uint64 public immutable checkpointHeight;
    uint256 public immutable checkpointChainWork;
    uint64 public immutable maxReorgDepth;
    bytes32 public mainchainHead;
    bool public halted;

    event BlockHeaderAdded(uint64 indexed height, bytes32 indexed blockHash, uint256 cumulativeWork);
    event MainchainHeadUpdated(uint64 indexed height, bytes32 indexed head);
    event ReorgOccurred(bytes32 indexed oldHead, bytes32 indexed newHead, bytes32 commonAncestor, uint64 depth);
    event RelayHalted(bytes32 indexed oldHead, bytes32 indexed candidate, uint64 rollbackDepth, bytes32 reason);

    error InvalidHeaderLength();
    error InvalidCheckpoint();
    error InvalidCheckpointWork();
    error InvalidAncestors();
    error InvalidReorgLimit();
    error BlockAlreadyExists(bytes32 blockHash);
    error PrevNotFound(bytes32 blockHash);
    error TargetAboveMainnetLimit();
    error InvalidProofOfWork();
    error InvalidDifficultyBits(uint32 actual, uint32 expected);
    error InvalidMedianTime();
    error TooFarInFuture();
    error RelayIsHalted();
    error InvalidHeaderBatch();

    constructor(
        bytes memory rawCheckpoint,
        uint64 height,
        uint256 chainWork,
        bytes[] memory precedingHeaders,
        uint64 reorgLimit
    ) {
        if (height < 10 || height % DIFFICULTY_ADJUSTMENT_INTERVAL != 0) {
            revert InvalidCheckpoint();
        }
        if (precedingHeaders.length != 10) revert InvalidAncestors();
        if (reorgLimit == 0) revert InvalidReorgLimit();
        (ParsedHeader memory h, bytes32 hash) = _parse(rawCheckpoint);
        uint256 target = _target(h.bits);
        if (!_hashMeetsTarget(hash, target)) revert InvalidProofOfWork();
        if (chainWork < TargetsHelper.workFromTarget(target)) revert InvalidCheckpointWork();
        bytes32 previous;
        for (uint256 i; i < 10; ++i) {
            (ParsedHeader memory ancestor, bytes32 ancestorHash) = _parse(precedingHeaders[i]);
            if (i != 0 && ancestor.prev != previous) revert InvalidAncestors();
            checkpointPriorTimes[i] = ancestor.time;
            previous = ancestorHash;
        }
        if (h.prev != previous) revert InvalidAncestors();
        checkpointHash = hash;
        checkpointHeight = height;
        checkpointChainWork = chainWork;
        maxReorgDepth = reorgLimit;
        // These ten ancestors have no BlockData entries and cannot receive confirmations.
        blocks[hash] = BlockData(h.prev, h.merkleRoot, chainWork, height, h.time, h.bits);
        mainchainHead = hash;
        emit BlockHeaderAdded(height, hash, chainWork);
        emit MainchainHeadUpdated(height, hash);
    }

    function testHelpersEnabled() external pure returns (bool) {
        return false;
    }

    function getMainchainHead() external view returns (bytes32) {
        return mainchainHead;
    }

    function getMainchainHeight() external view returns (uint64) {
        return blocks[mainchainHead].height;
    }

    /// @notice Canonical height mapping derived from the selected head using skip ancestry.
    /// @dev Avoids stale entries after a shorter, heavier chain and unbounded reorg tail rewrites.
    /// The last selected chain remains readable for diagnostics after halt.
    function height2hash(uint64 height) public view returns (bytes32) {
        if (height < checkpointHeight || height > blocks[mainchainHead].height) return bytes32(0);
        return _ancestor(mainchainHead, height);
    }

    function getBlockStatus(bytes32 blockHash) external view returns (bool isInMainchain, uint64 confirmations) {
        if (halted) return (false, 0);
        uint64 height = blocks[blockHash].height;
        if (height == 0 || height2hash(height) != blockHash) return (false, 0);
        return (true, blocks[mainchainHead].height - height + 1);
    }

    /// @notice Merkle inclusion only; consumers separately check canonicality and confirmation depth.
    function checkTxInclusion(bytes32 blockHash, bytes32 txid, bytes32[] calldata siblings, uint256 index)
        external
        view
        returns (bool)
    {
        if (halted || blocks[blockHash].height == 0 || siblings.length > MAX_MERKLE_DEPTH) return false;
        if (index >> siblings.length != 0) return false;
        bytes32 node = txid;
        for (uint256 i; i < siblings.length; ++i) {
            node = (index & 1) == 0
                ? _sha256d(abi.encodePacked(node, siblings[i]))
                : _sha256d(abi.encodePacked(siblings[i], node));
            index >>= 1;
        }
        return node == blocks[blockHash].merkleRoot;
    }

    function addBlockHeader(bytes calldata raw) external {
        _addBlockHeader(raw);
    }

    /// @notice Headers share one atomic receipt; exact known headers are safe retries.
    function addBlockHeaders(bytes[] calldata headers) external {
        if (headers.length == 0 || headers.length > 64) revert InvalidHeaderBatch();
        if (halted) revert RelayIsHalted();
        for (uint256 i; i < headers.length; ++i) {
            // Another operator can publish part of an already signed batch first.
            // Parsing enforces 80 bytes; SHA256d binds every field to the previously
            // validated header. Skipping it must not add work or reselect a head.
            (, bytes32 hash) = _parse(headers[i]);
            if (blocks[hash].height != 0) continue;
            _addBlockHeader(headers[i]);
            // Preserve a deep-reorg halt; reverting the next iteration would undo it.
            if (halted) break;
        }
    }

    function _addBlockHeader(bytes calldata raw) internal {
        if (halted) revert RelayIsHalted();
        (ParsedHeader memory h, bytes32 hash) = _parse(raw);
        if (blocks[hash].height != 0) revert BlockAlreadyExists(hash);
        BlockData storage parent = blocks[h.prev];
        if (parent.height == 0) revert PrevNotFound(h.prev);
        uint64 height = parent.height + 1;
        uint256 target = _target(h.bits);
        uint32 expected = _expectedHeaderBits(h.prev, height, h.time);
        if (h.bits != expected) revert InvalidDifficultyBits(h.bits, expected);
        _validateHeaderTime(h.time, h.prev, height);
        if (!_hashMeetsTarget(hash, target)) revert InvalidProofOfWork();
        // Checked arithmetic: wrapping work can never change fork selection.
        uint256 chainWork = parent.cumulativeWork + TargetsHelper.workFromTarget(target);
        _storeHeader(hash, h, height, chainWork);
        emit BlockHeaderAdded(height, hash, chainWork);
        _selectHead(hash);
    }

    function _expectedHeaderBits(bytes32 parentHash, uint64 height, uint32) internal view virtual returns (uint32) {
        return _expectedBits(parentHash, height);
    }

    function _validateHeaderTime(uint32 timestamp, bytes32 parentHash, uint64) internal view virtual {
        _validateTime(timestamp, parentHash);
    }

    function _expectedBits(bytes32 parentHash, uint64 height) internal view returns (uint32) {
        BlockData storage parent = blocks[parentHash];
        if (height % DIFFICULTY_ADJUSTMENT_INTERVAL != 0) return parent.bits;
        bytes32 first = _ancestor(parentHash, height - DIFFICULTY_ADJUSTMENT_INTERVAL);
        // Exactly 2015 intervals; convert before subtracting to preserve negative spans.
        int64 elapsed = int64(uint64(parent.time)) - int64(uint64(blocks[first].time));
        uint256 target = TargetsHelper.retargetWithLimit(TargetsHelper.bitsToTarget(parent.bits), elapsed, POW_LIMIT);
        return TargetsHelper.targetToBits(target);
    }

    function _validateTime(uint32 timestamp, bytes32 parentHash) internal view {
        if (timestamp <= _medianTimePast(parentHash)) revert InvalidMedianTime();
        // Subtract instead of adding to avoid overflow under artificial EVM clocks.
        if (uint256(timestamp) > block.timestamp && uint256(timestamp) - block.timestamp > 2 hours) {
            revert TooFarInFuture();
        }
    }

    function _medianTimePast(bytes32 cursor) internal view returns (uint32) {
        uint32[11] memory times;
        uint256 count;
        while (count < 11) {
            times[count++] = blocks[cursor].time;
            if (cursor == checkpointHash) break;
            cursor = blocks[cursor].prev;
        }
        uint256 i = 10;
        while (count < 11) times[count++] = checkpointPriorTimes[--i];
        for (i = 1; i < 11; ++i) {
            uint32 value = times[i];
            uint256 j = i;
            while (j > 0 && times[j - 1] > value) {
                times[j] = times[j - 1];
                --j;
            }
            times[j] = value;
        }
        return times[5];
    }

    function _storeHeader(bytes32 hash, ParsedHeader memory h, uint64 height, uint256 chainWork) internal virtual {
        blocks[hash] = BlockData(h.prev, h.merkleRoot, chainWork, height, h.time, h.bits);
        skips[hash] = _ancestor(h.prev, checkpointHeight + _skipHeight(height - checkpointHeight));
    }

    // Adapted from Bitcoin Core (c) 2009-2022 The Bitcoin Core developers (MIT),
    // CBlockIndex::GetSkipHeight / GetAncestor, using uint64
    // checkpoint-relative heights. https://github.com/bitcoin/bitcoin/blob/v30.0/src/chain.cpp
    function _invertLowestOne(uint64 value) private pure returns (uint64) {
        return value == 0 ? 0 : value & (value - 1);
    }

    function _skipHeight(uint64 height) private pure returns (uint64) {
        if (height < 2) return 0;
        return (height & 1) != 0
            ? _invertLowestOne(_invertLowestOne(height - 1)) + 1 : _invertLowestOne(height);
    }

    function _ancestor(bytes32 cursor, uint64 height) internal view returns (bytes32) {
        uint64 walk = blocks[cursor].height - checkpointHeight;
        uint64 target = height - checkpointHeight;
        while (walk > target) {
            uint64 skip = _skipHeight(walk);
            uint64 previousSkip = _skipHeight(walk - 1);
            if (skip == target || (skip > target && !(skip > 2 && previousSkip < skip - 2 && previousSkip >= target))) {
                cursor = skips[cursor];
                walk = skip;
            } else {
                cursor = blocks[cursor].prev;
                --walk;
            }
        }
        return cursor;
    }

    function _commonAncestor(bytes32 a, bytes32 b) internal view returns (bytes32) {
        uint64 ah = blocks[a].height;
        uint64 bh = blocks[b].height;
        if (ah > bh) a = _ancestor(a, bh);
        else if (bh > ah) b = _ancestor(b, ah);
        // Both pointers always have the same height. Different skip ancestors
        // cannot cross the fork point; equal skips require one parent step.
        // This preserves the exact common ancestor without scanning a fork tail.
        while (a != b) {
            bytes32 ap = skips[a];
            bytes32 bp = skips[b];
            a = ap != bp ? ap : blocks[a].prev;
            b = ap != bp ? bp : blocks[b].prev;
        }
        return a;
    }

    function _selectHead(bytes32 candidate) internal {
        bytes32 oldHead = mainchainHead;
        if (blocks[candidate].cumulativeWork <= blocks[oldHead].cumulativeWork) return;
        bytes32 common = _commonAncestor(oldHead, candidate);
        uint64 depth = blocks[oldHead].height - blocks[common].height;
        if (depth > maxReorgDepth) {
            halted = true;
            emit RelayHalted(oldHead, candidate, depth, keccak256("MAX_REORG_DEPTH_EXCEEDED"));
            return; // Do not revert: the halt must survive the successful transaction.
        }
        mainchainHead = candidate;
        if (depth != 0) emit ReorgOccurred(oldHead, candidate, common, depth);
        emit MainchainHeadUpdated(blocks[candidate].height, candidate);
    }

    function _target(uint32 bits) internal pure returns (uint256 target) {
        target = TargetsHelper.bitsToTarget(bits);
        if (target > POW_LIMIT) revert TargetAboveMainnetLimit();
    }

    function _hashMeetsTarget(bytes32 hash, uint256 target) internal pure returns (bool) {
        return uint256(BitcoinTxParser.flipBytes32(hash)) <= target;
    }

    function _parse(bytes memory raw) internal pure returns (ParsedHeader memory h, bytes32 hash) {
        if (raw.length != 80) revert InvalidHeaderLength();
        bytes32 prev;
        bytes32 root;
        assembly ("memory-safe") {
            prev := mload(add(raw, 36))
            root := mload(add(raw, 68))
        }
        h = ParsedHeader(prev, root, _read32LE(raw, 68), _read32LE(raw, 72));
        hash = _sha256d(raw);
    }

    function _read32LE(bytes memory raw, uint256 at) private pure returns (uint32) {
        return uint32(uint8(raw[at])) | (uint32(uint8(raw[at + 1])) << 8) | (uint32(uint8(raw[at + 2])) << 16)
            | (uint32(uint8(raw[at + 3])) << 24);
    }

    function _sha256d(bytes memory raw) internal pure returns (bytes32) {
        return sha256(abi.encodePacked(sha256(raw)));
    }
}
