// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/libs/TargetsHelper.sol";

contract TargetsArithmeticHarness {
    function decode(uint32 bits) external pure returns (uint256, bool, bool) {
        return TargetsHelper.decodeCompact(bits);
    }

    function target(uint32 bits) external pure returns (uint256) {
        return TargetsHelper.bitsToTarget(bits);
    }

    function compact(uint256 value) external pure returns (uint32) {
        return TargetsHelper.targetToBits(value);
    }

    function work(uint256 value) external pure returns (uint256) {
        return TargetsHelper.workFromTarget(value);
    }

    function retarget(uint256 value, int64 span, uint256 limit) external pure returns (uint256) {
        return TargetsHelper.retargetWithLimit(value, span, limit);
    }
}

contract TargetsHelperTest is Test {
    TargetsArithmeticHarness internal arithmetic = new TargetsArithmeticHarness();
    uint256 internal constant LIMIT = 0x00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff;

    function testCoreCompactSmallValuesAndSignCarry() public view {
        assertEq(arithmetic.compact(0), 0);
        assertEq(arithmetic.compact(1), 0x01010000);
        assertEq(arithmetic.compact(0x7f), 0x017f0000);
        assertEq(arithmetic.compact(0x80), 0x02008000);
        assertEq(arithmetic.compact(0xff), 0x0200ff00);
        assertEq(arithmetic.compact(0x100), 0x02010000);
        assertEq(arithmetic.compact(0x7fff), 0x027fff00);
        assertEq(arithmetic.compact(0x8000), 0x03008000);
        assertEq(arithmetic.compact(0x123456), 0x03123456);
        assertEq(arithmetic.compact(0x7fffff), 0x037fffff);
        assertEq(arithmetic.compact(0x800000), 0x04008000);
        assertEq(arithmetic.compact(0x12345600), 0x04123456);
        assertEq(arithmetic.compact(LIMIT), 0x1d00ffff);
        assertEq(arithmetic.compact(type(uint256).max), 0x2100ffff);
        assertEq(arithmetic.target(0x01010000), 1);
        assertEq(arithmetic.target(0x03000001), 1); // Noncanonical nonnegative SetCompact encoding.
        assertEq(arithmetic.target(0x207fffff), uint256(0x7fffff) << 232); // No mainnet ban in library.
    }

    function testCoreCompactRejectsZeroNegativeAndOverflow() public {
        uint32[10] memory invalid = [
            uint32(0),
            0x01003456,
            0x02000056,
            0x01800000,
            0x04923456,
            0x1d80ffff,
            0x23000001,
            0x22000100,
            0x21010000,
            0xff7fffff
        ];
        for (uint256 i; i < invalid.length; ++i) {
            vm.expectRevert(abi.encodeWithSelector(TargetsHelper.InvalidCompactTarget.selector, invalid[i]));
            arithmetic.target(invalid[i]);
        }
        (uint256 zero, bool negative, bool overflow) = arithmetic.decode(0x01800001);
        assertEq(zero, 0);
        assertFalse(negative);
        assertFalse(overflow);
        (uint256 largest, bool n, bool o) = arithmetic.decode(0x220000ff);
        assertEq(largest, uint256(0xff) << 248);
        assertFalse(n);
        assertFalse(o);
    }

    function testCoreExactBlockWork() public view {
        assertEq(arithmetic.work(arithmetic.target(0x1d00ffff)), 4295032833);
        assertEq(arithmetic.work(arithmetic.target(0x207fffff)), 2);
        assertEq(arithmetic.work(1), uint256(1) << 255);
        assertEq(arithmetic.work(2), 0x5555555555555555555555555555555555555555555555555555555555555555);
        assertEq(arithmetic.work(type(uint256).max), 1);
        assertEq(arithmetic.work(0), 0);
    }

    function testRetargetSignedClampAndExactFloor() public view {
        uint256 target = arithmetic.target(0x1d00ffff);
        assertEq(arithmetic.retarget(target, -1, LIMIT), target / 4);
        assertEq(arithmetic.retarget(target, type(int64).min, LIMIT), target / 4);
        assertEq(arithmetic.retarget(target, 302400, LIMIT), target / 4);
        assertEq(arithmetic.retarget(target, 1209600, LIMIT), target);
        assertEq(arithmetic.retarget(target, 4838400, LIMIT), LIMIT);
        assertEq(arithmetic.retarget(target, type(int64).max, LIMIT), LIMIT);
        assertEq(arithmetic.retarget(11, 302400, LIMIT), 2);
        assertEq(arithmetic.retarget(1, 302400, LIMIT), 0); // Core does not invent a minimum target of 1.
        assertEq(arithmetic.retarget(type(uint256).max, 4838400, type(uint256).max), type(uint256).max);
    }

    function testFuzzCoreCompactRoundtripTruncatesOnlyLowBytes(uint256 value) public view {
        if (value == 0) return;
        uint32 bits = arithmetic.compact(value);
        uint256 roundtrip = arithmetic.target(bits);
        assertLe(roundtrip, value);
        assertEq(arithmetic.compact(roundtrip), bits);
        uint256 exponent = bits >> 24;
        if (exponent > 3) assertLt(value - roundtrip, uint256(1) << (8 * (exponent - 3)));
        else assertEq(roundtrip, value);
    }

    function testFuzzRetargetMatchesFullPrecisionForMainnet(uint256 target, int64 span) public view {
        target = bound(target, 1, LIMIT);
        uint256 clamped = uint64(span < 302400 ? int64(302400) : span > 4838400 ? int64(4838400) : span);
        uint256 expected = target * clamped / 1209600; // Fits within 247 bits for every mainnet target.
        if (expected > LIMIT) expected = LIMIT;
        assertEq(arithmetic.retarget(target, span, LIMIT), expected);
    }
}
