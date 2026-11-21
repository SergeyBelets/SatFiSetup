// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayFixture.sol";

/// @dev Independent review regressions for branch context beyond the trusted bootstrap period.
contract MainnetRelayReviewTest is MainnetFixtureBase {
    function setUp() public {
        loadMainnetFixture();
    }

    function testRetargetUsesFirstHeaderOfItsOwnBranchAfterCrossingAnEarlierBoundary() public {
        MainnetRelayHarness relay = newHarness(100);
        bytes32 shared = relay.checkpointHash();
        uint32 baseTime = timestamp(headerAt(CHECKPOINT));
        for (uint32 i = 1; i < 2016; ++i) {
            if (i % 128 == 0) vm.resetGasMetering();
            shared = relay.syntheticChild(shared, 1, bytes32(0), baseTime + i * 600);
        }

        // Split exactly at the first retarget, then inspect the following retarget.
        // The two periods have different first headers as well as different last headers.
        uint32 canonicalStartTime = baseTime + 2016 * 600;
        uint32 forkStartTime = canonicalStartTime + 600000;
        bytes32 canonicalStart = relay.syntheticChild(shared, 1000, bytes32(0), canonicalStartTime);
        bytes32 forkStart = relay.syntheticChild(shared, 1, bytes32(uint256(1)), forkStartTime);
        bytes32 canonical = canonicalStart;
        bytes32 fork = forkStart;
        for (uint32 i = 1; i < 2016; ++i) {
            if (i % 128 == 0) vm.resetGasMetering();
            canonical = relay.syntheticChild(canonical, 1000, bytes32(0), canonicalStartTime + i * 600);
            fork = relay.syntheticChild(fork, 1, bytes32(uint256(1)), forkStartTime + i * 400);
        }

        uint64 boundary = CHECKPOINT + 4032;
        uint64 firstHeight = boundary - 2016;
        assertEq(relay.ancestor(canonical, firstHeight), canonicalStart);
        assertEq(relay.ancestor(fork, firstHeight), forkStart);
        assertEq(relay.height2hash(firstHeight), canonicalStart);
        assertEq(relay.getMainchainHead(), canonical);
        assertFalse(relay.halted());

        // The harness deliberately retains the same nBits on synthetic blocks. Use
        // ordinary full-precision mainnet arithmetic as the independent timespan oracle.
        uint256 previousTarget = TargetsHelper.bitsToTarget(expectedBits(CHECKPOINT));
        uint256 canonicalTarget = previousTarget * (2015 * 600) / 1209600;
        uint256 forkTarget = previousTarget * (2015 * 400) / 1209600;
        uint256 wrongGlobalTarget = previousTarget * (600000 + 2015 * 400) / 1209600;
        uint32 forkBits = TargetsHelper.targetToBits(forkTarget);
        assertTrue(forkBits != TargetsHelper.targetToBits(wrongGlobalTarget));
        assertEq(relay.expectedBits(canonical, boundary), TargetsHelper.targetToBits(canonicalTarget));
        assertEq(relay.expectedBits(fork, boundary), forkBits);
    }

    function testMedianUsesExactlyElevenAncestorsThroughoutCheckpointBootstrap() public {
        MainnetRelayHarness relay = newHarness(100);
        for (uint64 offset; offset <= 11; ++offset) {
            uint64 parentHeight = CHECKPOINT + offset;
            uint32[11] memory times;
            for (uint64 i; i < 11; ++i) times[i] = timestamp(headerAt(parentHeight - i));
            for (uint256 i; i < 11; ++i) {
                for (uint256 j = i + 1; j < 11; ++j) {
                    if (times[i] > times[j]) (times[i], times[j]) = (times[j], times[i]);
                }
            }
            assertEq(relay.medianTimePast(wireHash(parentHeight)), times[5]);
            if (offset < 11) relay.addBlockHeader(headerAt(parentHeight + 1));
        }
    }
}
