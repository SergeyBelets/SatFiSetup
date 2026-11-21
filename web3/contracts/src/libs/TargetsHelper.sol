// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Bitcoin Core compact-target and block-work arithmetic.
/// @dev Compact decoding is network-independent: valid regtest targets remain supported.
library TargetsHelper {
    uint32 internal constant DIFFICULTY_ADJUSTMENT_INTERVAL = 2016;
    uint32 internal constant TARGET_TIME_PER_BLOCK = 600;
    uint32 internal constant TARGET_TIMESPAN = TARGET_TIME_PER_BLOCK * DIFFICULTY_ADJUSTMENT_INTERVAL;

    error InvalidCompactTarget(uint32 bits);

    /// @dev Matches arith_uint256::SetCompact, including its negative/overflow flags.
    function decodeCompact(uint32 bits) internal pure returns (uint256 target, bool negative, bool overflow) {
        uint256 size = bits >> 24;
        uint256 word = bits & 0x007fffff;
        if (size <= 3) {
            word >>= 8 * (3 - size);
            target = word;
        } else {
            target = word << (8 * (size - 3));
        }
        negative = word != 0 && (bits & 0x00800000) != 0;
        overflow = word != 0 && (size > 34 || (word > 0xff && size > 33) || (word > 0xffff && size > 32));
    }

    /// @notice Decode a positive, nonzero, non-overflowing compact target; no network powLimit is applied.
    function bitsToTarget(uint32 bits) internal pure returns (uint256 target) {
        bool negative;
        bool overflow;
        (target, negative, overflow) = decodeCompact(bits);
        if (negative || overflow || target == 0) revert InvalidCompactTarget(bits);
    }

    /// @dev Matches positive arith_uint256::GetCompact, including small integers and sign-bit carry.
    function targetToBits(uint256 target) internal pure returns (uint32) {
        if (target == 0) return 0;
        uint256 size;
        for (uint256 x = target; x != 0; x >>= 8) {
            ++size;
        }
        uint256 word = size <= 3 ? target << (8 * (3 - size)) : target >> (8 * (size - 3));
        if ((word & 0x00800000) != 0) {
            word >>= 8;
            ++size;
        }
        return uint32((size << 24) | word);
    }

    /// @notice floor(2^256 / (target + 1)), using Bitcoin Core's complement identity.
    /// @dev Zero is invalid (Core GetBlockProof returns zero); handle uint256.max without wraparound.
    function workFromTarget(uint256 target) internal pure returns (uint256) {
        if (target == 0) return 0;
        if (target == type(uint256).max) return 1;
        return (~target / (target + 1)) + 1;
    }

    function isAdjustmentBlock(uint64 height) internal pure returns (bool) {
        return height != 0 && height % DIFFICULTY_ADJUSTMENT_INTERVAL == 0;
    }

    /// @notice Core timespan clamp and exact integer retarget with an explicitly supplied network limit.
    /// @dev Quotient/remainder decomposition avoids a 256-bit intermediate multiplication overflow,
    /// even for callers using regtest-sized targets. Signed subtraction must happen before this call.
    function retargetWithLimit(uint256 previousTarget, int64 actualTimespan, uint256 powLimit)
        internal
        pure
        returns (uint256)
    {
        int64 minimum = int64(uint64(TARGET_TIMESPAN / 4));
        int64 maximum = int64(uint64(TARGET_TIMESPAN * 4));
        uint256 span = uint64(actualTimespan < minimum ? minimum : actualTimespan > maximum ? maximum : actualTimespan);
        uint256 quotient = previousTarget / TARGET_TIMESPAN;
        if (quotient > powLimit / span) return powLimit;
        uint256 result = quotient * span;
        uint256 fraction = (previousTarget % TARGET_TIMESPAN) * span / TARGET_TIMESPAN;
        if (fraction > powLimit - result) return powLimit;
        return result + fraction;
    }

    /// @notice Legacy mainnet convenience overload; decoding and work remain network-independent.
    function retarget(uint256 previousTarget, uint32 actualTimespan) internal pure returns (uint256) {
        return retargetWithLimit(
            previousTarget,
            int64(uint64(actualTimespan)),
            0x00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff
        );
    }
}
