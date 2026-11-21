// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayFixture.sol";
import "../contracts/src/federation/MainnetSPVGuard.sol";

contract MainnetSPVGuardTest is MainnetFixtureBase {
    MainnetHeaderRelay internal relay;
    MainnetSPVGuard internal guard;

    function setUp() public {
        loadMainnetFixture();
        relay = newRelay(100);
        guard = new MainnetSPVGuard(address(relay), 7200);
    }

    function testStaleProofsFailClosedIncludingValidMerkleRoot() public view {
        bytes32 hash = wireHash(CHECKPOINT);
        (, bytes32 root,,,,) = relay.blocks(hash);
        assertTrue(relay.checkTxInclusion(hash, root, new bytes32[](0), 0));
        assertFalse(guard.isFresh());
        (bool canonical, uint64 depth) = guard.getBlockStatus(hash);
        assertFalse(canonical);
        assertEq(depth, 0);
        assertFalse(guard.checkTxInclusion(hash, root, new bytes32[](0), 0));
    }

    function testExactFreshnessBoundaryAndNextSecond() public {
        bytes32 hash = wireHash(CHECKPOINT);
        (, bytes32 root,,, uint32 time,) = relay.blocks(hash);
        vm.warp(uint256(time) + 7200);
        assertTrue(guard.isFresh());
        (bool canonical, uint64 depth) = guard.getBlockStatus(hash);
        assertTrue(canonical);
        assertEq(depth, 1);
        assertTrue(guard.checkTxInclusion(hash, root, new bytes32[](0), 0));
        vm.warp(block.timestamp + 1);
        assertFalse(guard.isFresh());
        assertFalse(guard.checkTxInclusion(hash, root, new bytes32[](0), 0));
    }

    function testRealNextHeaderRestoresFreshnessWithoutAdministrator() public {
        vm.warp(uint256(timestamp(headerAt(CHECKPOINT + 1))) + 7200);
        assertFalse(guard.isFresh());
        relay.addBlockHeader(headerAt(CHECKPOINT + 1));
        assertTrue(guard.isFresh());
        (bool canonical, uint64 depth) = guard.getBlockStatus(wireHash(CHECKPOINT));
        assertTrue(canonical);
        assertEq(depth, 2);
        assertEq(address(guard.relay()), address(relay));
        assertEq(guard.maxTipAgeSeconds(), 7200);
        assertFalse(guard.testHelpersEnabled());
    }

    function testPermanentRelayHaltOverridesFreshTimestamp() public {
        MainnetRelayHarness harness = newHarness(1);
        MainnetSPVGuard guarded = new MainnetSPVGuard(address(harness), 7200);
        uint32 time = timestamp(headerAt(CHECKPOINT));
        vm.warp(uint256(time) + 10);
        bytes32 first = harness.syntheticChild(wireHash(CHECKPOINT), 100, bytes32(uint256(1)), time);
        harness.syntheticChild(first, 100, bytes32(uint256(2)), time);
        assertTrue(guarded.isFresh());
        harness.syntheticChild(wireHash(CHECKPOINT), 300, bytes32(uint256(3)), time);
        assertTrue(harness.halted());
        assertFalse(guarded.isFresh());
        (bool canonical, uint64 depth) = guarded.getBlockStatus(first);
        assertFalse(canonical);
        assertEq(depth, 0);
        assertFalse(guarded.checkTxInclusion(first, bytes32(uint256(1)), new bytes32[](0), 0));
    }

    function testRejectsMissingRelayAndUnboundedFreshnessPolicy() public {
        vm.expectRevert(MainnetSPVGuard.InvalidRelay.selector);
        new MainnetSPVGuard(address(0), 7200);
        vm.expectRevert(MainnetSPVGuard.InvalidTipAge.selector);
        new MainnetSPVGuard(address(relay), 0);
        vm.expectRevert(MainnetSPVGuard.InvalidTipAge.selector);
        new MainnetSPVGuard(address(relay), 86401);
    }
}
