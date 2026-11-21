// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetHeaderRelay.sol";

/// @notice Bitcoin testnet4 SPV relay with BIP94 consensus, never a regtest mock.
/// @dev Checkpoints are reviewed public testnet4 period boundaries, independently
/// compared with each participant's fully validating Core. The EVM chain is unrelated.
contract Testnet4HeaderRelay is MainnetHeaderRelay {
    uint32 public constant MIN_DIFFICULTY_BITS = 0x1d00ffff;
    // Cache the last non-exception difficulty on EACH branch; bounded O(1), no
    // unbounded scan over a period of minimum-difficulty blocks.
    mapping(bytes32 => uint32) internal periodDifficulty;
    error InvalidBIP94Time();

    constructor(bytes memory checkpoint, uint64 height, uint256 work, bytes[] memory prior, uint64 limit)
        MainnetHeaderRelay(checkpoint, height, work, prior, limit)
    {
        periodDifficulty[checkpointHash] = blocks[checkpointHash].bits;
    }

    function _expectedHeaderBits(bytes32 parentHash, uint64 height, uint32 timestamp)
        internal view override returns (uint32)
    {
        BlockData storage parent = blocks[parentHash];
        if (height % DIFFICULTY_ADJUSTMENT_INTERVAL != 0) {
            if (uint64(timestamp) > uint64(parent.time) + 1200) return MIN_DIFFICULTY_BITS;
            return periodDifficulty[parentHash];
        }
        bytes32 first = _ancestor(parentHash, height - DIFFICULTY_ADJUSTMENT_INTERVAL);
        int64 elapsed = int64(uint64(parent.time)) - int64(uint64(blocks[first].time));
        // BIP94 uses the FIRST block's actual difficulty, never the possibly
        // minimum-difficulty last block of the preceding period.
        return TargetsHelper.targetToBits(TargetsHelper.retargetWithLimit(
            TargetsHelper.bitsToTarget(blocks[first].bits), elapsed, POW_LIMIT));
    }

    function _validateHeaderTime(uint32 timestamp, bytes32 parentHash, uint64 height)
        internal view override
    {
        if (height % DIFFICULTY_ADJUSTMENT_INTERVAL == 0
                && uint64(timestamp) + 600 < uint64(blocks[parentHash].time)) revert InvalidBIP94Time();
        _validateTime(timestamp, parentHash);
    }

    function _storeHeader(bytes32 hash, ParsedHeader memory h, uint64 height, uint256 work)
        internal override
    {
        super._storeHeader(hash, h, height, work);
        periodDifficulty[hash] = height % DIFFICULTY_ADJUSTMENT_INTERVAL == 0 || h.bits != MIN_DIFFICULTY_BITS
            ? h.bits : periodDifficulty[h.prev];
    }
}
