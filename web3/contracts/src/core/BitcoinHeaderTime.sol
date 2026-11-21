// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

/// @notice EVM acceptance-time policy for a proof-bound header time summary.
/// @dev Internal component only: the future state contract must first verify the
/// real proof and bind these exact bytes to its full transition journal. This
/// library authenticates neither headers nor a caller-supplied summary. No
/// production state/adapter imports it yet. A time error must precede state writes.
library BitcoinHeaderTime {
    bytes32 internal constant DOMAIN = bytes32("SATOSHIFI_HEADER_TIME_V1");
    uint256 internal constant FUTURE_ALLOWANCE = 2 hours;

    struct Summary {
        uint64 headerCount;
        uint32 maximumTimestamp;
        // Last processed header, not necessarily the canonical head after forks.
        uint32 lastTimestamp;
    }

    error InvalidTimeEncoding();
    error EmptyHeaderBatch();
    error InvalidTimeSummary();
    error HeaderTooFarInFuture(uint32 maximumTimestamp, uint256 acceptanceTimestamp);

    function decode(bytes memory authenticatedTimeBytes) internal pure returns (Summary memory result) {
        if (authenticatedTimeBytes.length != 128) revert InvalidTimeEncoding();
        bytes32 domain;
        uint256 count;
        uint256 maximum;
        uint256 last;
        (domain, count, maximum, last) = abi.decode(authenticatedTimeBytes, (bytes32, uint256, uint256, uint256));
        if (domain != DOMAIN || count > type(uint64).max || maximum > type(uint32).max
            || last > type(uint32).max) revert InvalidTimeEncoding();
        if (count == 0) revert EmptyHeaderBatch();
        if (last > maximum) revert InvalidTimeSummary();
        result = Summary(uint64(count), uint32(maximum), uint32(last));
    }

    function validate(bytes memory authenticatedTimeBytes) internal view returns (Summary memory result) {
        result = decode(authenticatedTimeBytes);
        uint256 maximum = result.maximumTimestamp;
        // Subtraction after comparison avoids overflow in block.timestamp + 7200.
        if (maximum > block.timestamp && maximum - block.timestamp > FUTURE_ALLOWANCE) {
            revert HeaderTooFarInFuture(result.maximumTimestamp, block.timestamp);
        }
    }
}
