// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/core/BitcoinHeaderTime.sol";

// Component fixture, not an SPV state contract or proof-verification mock.
contract HeaderTimePolicyHarness {
    uint64 public acceptedCount;
    uint32 public acceptedMaximum;

    function inspect(bytes memory timeBytes) external view returns (BitcoinHeaderTime.Summary memory) {
        return BitcoinHeaderTime.validate(timeBytes);
    }

    function recordForPolicyTest(bytes memory timeBytes) external {
        BitcoinHeaderTime.Summary memory result = BitcoinHeaderTime.validate(timeBytes);
        acceptedCount = result.headerCount;
        acceptedMaximum = result.maximumTimestamp;
    }
}

contract BitcoinHeaderTimeTest is Test {
    HeaderTimePolicyHarness private policy;
    bytes32 private constant DOMAIN = bytes32("SATOSHIFI_HEADER_TIME_V1");

    function setUp() public { policy = new HeaderTimePolicyHarness(); }

    function encoded(uint64 count, uint32 maximum, uint32 last) private pure returns (bytes memory) {
        return abi.encode(DOMAIN, count, maximum, last);
    }

    function testSharedRustAbiAndAcceptanceFixtures() public {
        string memory json = vm.readFile("test/fixtures/header-time-v1.json");
        for (uint256 i; i < 15; ++i) {
            string memory key = string.concat(".vectors[", vm.toString(i), "].");
            bytes memory raw = vm.parseJsonBytes(json, string.concat(key, "abi"));
            uint256 now_ = vm.parseUint(vm.parseJsonString(json, string.concat(key, "evm_timestamp")));
            vm.warp(now_);
            bool accepted = vm.parseJsonBool(json, string.concat(key, "accepted"));
            if (!accepted) {
                vm.expectRevert(abi.encodeWithSelector(BitcoinHeaderTime.HeaderTooFarInFuture.selector,
                    uint32(vm.parseJsonUint(json, string.concat(key, "maximum_timestamp"))), now_));
                policy.inspect(raw);
            } else {
                BitcoinHeaderTime.Summary memory result = policy.inspect(raw);
                assertEq(result.headerCount, vm.parseJsonUint(json, string.concat(key, "header_count")));
                assertEq(result.maximumTimestamp, vm.parseJsonUint(json, string.concat(key, "maximum_timestamp")));
                assertEq(result.lastTimestamp, vm.parseJsonUint(json, string.concat(key, "last_timestamp")));
            }
        }
    }

    function testEarlierMaximumCannotBeReplacedByLastTimestamp() public {
        vm.warp(10000);
        vm.expectRevert(abi.encodeWithSelector(BitcoinHeaderTime.HeaderTooFarInFuture.selector, uint32(17201), 10000));
        policy.inspect(encoded(3, 17201, 10000));
        // The same valid summary becomes eligible as EVM time advances.
        vm.warp(10001);
        assertEq(policy.inspect(encoded(3, 17201, 10000)).maximumTimestamp, 17201);
    }

    function testTimeFailureLeavesFixtureStateUnchangedAndCanRetry() public {
        vm.warp(10000);
        policy.recordForPolicyTest(encoded(1, 9000, 9000));
        vm.expectRevert(abi.encodeWithSelector(BitcoinHeaderTime.HeaderTooFarInFuture.selector, uint32(17201), 10000));
        policy.recordForPolicyTest(encoded(3, 17201, 10000));
        assertEq(policy.acceptedCount(), 1);
        assertEq(policy.acceptedMaximum(), 9000);
        vm.warp(10001);
        policy.recordForPolicyTest(encoded(3, 17201, 10000));
        assertEq(policy.acceptedCount(), 3);
    }

    function testEmptyBatchAndInconsistentSummaryRejected() public {
        vm.expectRevert(BitcoinHeaderTime.EmptyHeaderBatch.selector);
        policy.inspect(encoded(0, 100, 100));
        vm.expectRevert(BitcoinHeaderTime.InvalidTimeSummary.selector);
        policy.inspect(encoded(2, 100, 101));
    }

    function testEveryTruncationAndTrailingByteRejected() public {
        for (uint256 size; size < 128; ++size) {
            vm.expectRevert(BitcoinHeaderTime.InvalidTimeEncoding.selector);
            policy.inspect(new bytes(size));
        }
        vm.expectRevert(BitcoinHeaderTime.InvalidTimeEncoding.selector);
        policy.inspect(bytes.concat(encoded(1, 100, 100), hex"00"));
    }

    function testDomainAndNonCanonicalIntegerWidthsRejected() public {
        bytes[4] memory invalid = [
            abi.encode(bytes32("SATOSHIFI_HEADER_TIME_V2"), uint256(1), uint256(1), uint256(1)),
            abi.encode(DOMAIN, uint256(1) << 64, uint256(1), uint256(1)),
            abi.encode(DOMAIN, uint256(1), uint256(1) << 32, uint256(1)),
            abi.encode(DOMAIN, uint256(1), uint256(1), uint256(1) << 32)
        ];
        for (uint256 i; i < invalid.length; ++i) {
            vm.expectRevert(BitcoinHeaderTime.InvalidTimeEncoding.selector);
            policy.inspect(invalid[i]);
        }
    }

    function testUint256ClockDoesNotOverflow() public {
        vm.warp(type(uint256).max);
        assertEq(policy.inspect(encoded(1, type(uint32).max, 0)).maximumTimestamp, type(uint32).max);
    }

    function testFuzzFutureBoundUsesEvmClock(uint32 maximum, uint32 lastSeed, uint256 clock_) public {
        uint32 last = uint32(uint256(lastSeed) % (uint256(maximum) + 1));
        vm.warp(clock_);
        bool expected = clock_ >= type(uint32).max || uint256(maximum) <= clock_ + 7200;
        if (!expected) {
            vm.expectRevert(abi.encodeWithSelector(BitcoinHeaderTime.HeaderTooFarInFuture.selector, maximum, clock_));
        }
        policy.inspect(encoded(2, maximum, last));
    }

    function testFuzzAbiRoundTrip(uint64 count, uint32 maximum, uint32 lastSeed) public {
        count = count == 0 ? 1 : count;
        uint32 last = uint32(uint256(lastSeed) % (uint256(maximum) + 1));
        vm.warp(type(uint32).max);
        BitcoinHeaderTime.Summary memory result = policy.inspect(encoded(count, maximum, last));
        assertEq(result.headerCount, count);
        assertEq(result.maximumTimestamp, maximum);
        assertEq(result.lastTimestamp, last);
    }
}
