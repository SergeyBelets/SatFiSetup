// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayFixture.sol";

// Foundry 1.8.5 supports this cheatcode; the vendored Vm interface predates it.
interface VmColdRelayReview {
    function cool(address target) external;
}

/// @dev Keep ancestry creation in setUp so writes are committed before the measured call.
contract MainnetRelayColdGasReviewTest is MainnetFixtureBase {
    MainnetRelayHarness private relay;
    bytes32 private oldHead;
    bytes32 private fork;

    function setUp() public {
        loadMainnetFixture();
        relay = newHarness(100);
        oldHead = relay.checkpointHash();
        for (uint32 i; i < 101; ++i) {
            oldHead = relay.syntheticChild(oldHead, 100000, bytes32(0), 1300000000 + i);
        }
        fork = relay.checkpointHash();
        for (uint32 i; i < 8192; ++i) {
            // Refill the setup driver's gas only; the final halt call is independently metered.
            if (i % 128 == 0) vm.resetGasMetering();
            fork = relay.syntheticChild(fork, 1, bytes32(uint256(1)), 1300100000 + i);
        }
        assertFalse(relay.halted());
        assertEq(relay.getMainchainHead(), oldHead);
    }

    function testColdHaltCostBoundedFor8192BlockCompetingTail() public {
        // Cache test-driver inputs before cooling. No relay read or write follows cooling
        // until the measured call; cool marks its address and every storage slot cold.
        MainnetRelayHarness target = relay;
        bytes32 parent = fork;
        bytes32 retainedHead = oldHead;
        VmColdRelayReview(address(vm)).cool(address(target));
        uint256 beforeGas = gasleft();
        target.syntheticChild(parent, 11000000, bytes32(uint256(1)), 1300200000);
        uint256 used = beforeGas - gasleft();
        emit log_named_uint("cold halt including header storage and external call gas", used);
        // A local regression guard, not a numerical requirement in spec section 1.5.
        assertLt(used, 1000000);
        assertTrue(target.halted());
        assertEq(target.getMainchainHead(), retainedHead);
    }
}
