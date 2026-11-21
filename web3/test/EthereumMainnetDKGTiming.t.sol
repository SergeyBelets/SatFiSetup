// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/federation/DKGCoordinator.sol";

contract EthereumMainnetDKGTimingTest is Test {
    function testMainnetSharesSurviveSequentialFinalityDelays() public {
        vm.chainId(1);
        DKGCoordinator d = new DKGCoordinator();
        assertEq(d.PHASE_DURATION(), 1 days);
        assertEq(d.getPhaseDuration(), 1 days);
        address[] memory roster = new address[](3);
        bytes32[] memory keys = new bytes32[](3);
        for (uint256 i; i < 3; ++i) { roster[i] = address(uint160(i + 1)); keys[i] = bytes32(i + 1); }
        bytes32 id = keccak256("mainnet delayed transport fixture");
        d.createCeremony(id, roster, 3, keys, 1, keccak256("EVM_AUTH"), d.PROTOCOL_V1());
        for (uint256 i; i < 3; ++i) {
            vm.warp(block.timestamp + 26 minutes); // key publication + registry attestation.
            vm.prank(roster[i]);
            d.submitRound1(id, bytes32(i + 1), keys);
        }
        // Even serialized participants (six envelopes, two transactions each)
        // remain live. The previous one-hour window expired in this schedule.
        for (uint256 i; i < 3; ++i) for (uint256 j; j < 3; ++j) if (i != j) {
            vm.warp(block.timestamp + 26 minutes);
            vm.prank(roster[i]);
            d.submitShareHash(id, roster[j], bytes32(uint256(123)));
        }
        (,, uint256 count,,) = d.getDKGStatus(id);
        assertEq(count, 6);
    }

    function testMainnetWindowStillExpires() public {
        vm.chainId(1);
        DKGCoordinator d = new DKGCoordinator();
        address[] memory roster = new address[](2);
        roster[0] = address(1); roster[1] = address(2);
        bytes32[] memory keys = new bytes32[](2); keys[0] = bytes32(uint256(1)); keys[1] = bytes32(uint256(2));
        bytes32 id = keccak256("expired mainnet fixture");
        d.createCeremony(id, roster, 2, keys, 1, keccak256("BTC_SPEND"), d.PROTOCOL_V1());
        vm.warp(block.timestamp + 1 days + 1);
        vm.prank(roster[0]);
        vm.expectRevert();
        d.submitRound1(id, bytes32(uint256(1)), keys);
    }

    function testAnvilAndSepoliaKeepOneHourProtocol() public {
        vm.chainId(31337);
        DKGCoordinator d = new DKGCoordinator();
        assertEq(d.PHASE_DURATION(), 1 hours);
        vm.chainId(11155111);
        assertEq(d.PHASE_DURATION(), 1 hours);
    }
}
