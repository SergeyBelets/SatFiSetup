// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayFixture.sol";
import "./MainnetRelayColdGasReview.t.sol";

/// @dev Commit ancestry in setUp; each measured extension starts with cold storage.
contract MainnetRelayStorageGasTest is MainnetFixtureBase {
    MainnetRelayHarness private relay;
    bytes32[] private branch;

    function setUp() public {
        loadMainnetFixture();
        relay = newHarness(100);
        bytes32 cursor = relay.checkpointHash();
        branch.push(cursor);
        for (uint32 i = 1; i <= 8192; ++i) {
            if (i % 128 == 0) vm.resetGasMetering();
            cursor = relay.syntheticChild(cursor, 1, bytes32(0), 1300000000 + i);
            branch.push(cursor);
        }
    }

    function testAncestorsAtEverySkipBoundaryAndRandomHeight(uint16 rawHeight) public view {
        uint64 height = uint64(rawHeight % 8193);
        assertEq(relay.ancestor(branch[8192], CHECKPOINT + height), branch[height]);
        assertEq(relay.height2hash(CHECKPOINT + height), branch[height]);
    }

    function testNewHeaderWritesOneSkipSlotAtBothYoungAndOldHeights() public {
        // Weak side branches do not select a new head, isolating header writes.
        // Four BlockData slots + one skip slot + the test harness serial.
        uint64[5] memory heights = [uint64(31), 127, 1023, 4095, 8191];
        for (uint256 i; i < heights.length; ++i) {
            bytes32 parent = branch[heights[i]];
            MainnetRelayHarness target = relay;
            VmColdRelayReview(address(vm)).cool(address(target));
            vm.record();
            uint256 beforeGas = gasleft();
            target.syntheticChild(parent, 0, bytes32(uint256(1)), 1400000000);
            uint256 used = beforeGas - gasleft();
            (, bytes32[] memory writes) = vm.accesses(address(target));
            emit log_named_uint("parent offset", heights[i]);
            emit log_named_uint("cold header storage gas", used);
            emit log_named_uint("storage write slots", writes.length);
            assertEq(writes.length, 6);
            assertLt(used, 350000);
        }
    }

    function testCommonAncestorAcrossSkipTargets(uint16 rawSplit, uint8 rawTail) public {
        uint64 split = uint64(rawSplit % 8100);
        uint64 tail = uint64(rawTail % 90) + 1;
        bytes32 fork = branch[split];
        for (uint32 i; i < tail; ++i) {
            fork = relay.syntheticChild(fork, 0, bytes32(uint256(1)), 1400000000 + i);
        }
        assertEq(relay.commonAncestor(fork, branch[8192]), branch[split]);
        assertEq(relay.commonAncestor(branch[8192], fork), branch[split]);
    }
}
