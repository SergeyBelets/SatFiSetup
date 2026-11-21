// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/federation/ChainlinkBTCDailyTWAP.sol";

contract TWAPRemediationFeed {
    uint8 public constant decimals = 8;
    struct Round { int256 price; uint256 time; uint80 answered; }
    mapping(uint80 => Round) public rounds;
    function set(uint80 id, int256 price, uint256 time) external {
        rounds[id] = Round(price, time, id);
    }
    function setAnswered(uint80 id, uint80 answered) external { rounds[id].answered = answered; }
    function getRoundData(uint80 id) external view returns (uint80, int256, uint256, uint256, uint80) {
        Round memory r = rounds[id];
        require(r.time != 0, "missing");
        return (id, r.price, r.time, r.time, r.answered);
    }
}

contract TWAPRemediationTest is Test {
    TWAPRemediationFeed feed;
    ChainlinkBTCDailyTWAP twap;
    uint256 constant DAY = 10;
    uint256 constant START = DAY * 1 days;
    uint256 constant END = START + 1 days;
    function setUp() public {
        feed = new TWAPRemediationFeed();
        twap = new ChainlinkBTCDailyTWAP(address(feed));
        vm.warp(END + 1 days);
    }
    function ids(uint80 first, uint80 count) internal pure returns (uint80[] memory page) {
        page = new uint80[](count);
        for (uint80 i; i < count; i++) page[i] = first + i;
    }
    function four() internal {
        feed.set(1, 900, START - 100);
        feed.set(2, 100, START - 10);
        feed.set(3, 300, START + 12 hours);
        feed.set(4, 700, END);
    }
    function testExactFourRoundsExcludePreDayDuration() public {
        four();
        assertEq(twap.calculate(DAY, ids(1, 4)), 200);
        feed.set(1, 90000, START - 100);
        assertEq(twap.calculate(DAY, ids(1, 4)), 200);
        assertEq(twap.calculate(DAY, ids(2, 3)), 200);
    }
    function testPreDayPagesAndInterleavedCallers() public {
        four();
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.append(DAY, ids(1, 2));
        vm.prank(address(1)); twap.append(DAY, ids(1, 3));
        (,,,uint256 cursor,,,bool finalized) = twap.checkpoints(DAY);
        assertEq(cursor, START + 12 hours);
        assertFalse(finalized);
        vm.prank(address(3)); twap.append(DAY, ids(4, 1));
        assertEq(twap.finalizedPrice(DAY), 200);
        assertEq(twap.calculate(DAY, new uint80[](0)), 200);
    }
    function testMoreThan512RoundsPaginated() public {
        for (uint80 i = 1; i <= 601; i++) feed.set(i, 100 + int256(uint256(i)), START + (i - 1) * 144);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, ids(1, 601));
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.append(DAY, ids(1, 513));
        twap.append(DAY, ids(1, 512));
        vm.prank(address(2));
        (bool finalized, uint256 price) = twap.append(DAY, ids(513, 89));
        assertTrue(finalized);
        assertEq(price, 400);
        assertEq(twap.finalizedPrice(DAY), 400);
    }
    function testReplaySkippedAndReorderedPagesDoNotAdvance() public {
        four();
        twap.append(DAY, ids(1, 3));
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.append(DAY, ids(2, 1));
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.append(DAY, ids(5, 1));
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.append(DAY, ids(1, 1));
        (uint80 lastRound,,,,,,) = twap.checkpoints(DAY);
        assertEq(lastRound, 3);
        twap.append(DAY, ids(4, 1));
        assertEq(twap.finalizedPrice(DAY), 200);
    }
    function testFinalizedImmutabilityAndExtraEndRound() public {
        four(); feed.set(5, 800, END + 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.append(DAY, ids(1, 5));
        vm.expectRevert(ChainlinkBTCDailyTWAP.NotFinalized.selector);
        twap.finalizedPrice(DAY);
        twap.append(DAY, ids(1, 4));
        feed.set(2, 999, START - 10);
        vm.expectRevert(ChainlinkBTCDailyTWAP.AlreadyFinalized.selector);
        twap.append(DAY, ids(1, 4));
        assertEq(twap.calculate(DAY, new uint80[](0)), 200);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, ids(1, 5));
    }
    function testInvalidFeedValuesAndAtomicPage() public {
        four();
        feed.set(3, 0, START + 12 hours);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.append(DAY, ids(1, 4));
        (,,,,,bool initialized,) = twap.checkpoints(DAY);
        assertFalse(initialized);
        feed.set(3, -1, START + 12 hours);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, ids(1, 4));
        feed.set(3, 300, block.timestamp + 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, ids(1, 4));
        feed.set(3, 300, START + 12 hours); feed.setAnswered(3, 2);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, ids(1, 4));
    }
    function testStaleAndNonMonotonicRounds() public {
        four(); feed.set(1, 900, START - 1 days - 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.StalePrice.selector);
        twap.calculate(DAY, ids(1, 4));
        four(); feed.set(3, 300, START - 10);
        vm.expectRevert(ChainlinkBTCDailyTWAP.StalePrice.selector);
        twap.calculate(DAY, ids(1, 4));
        four(); feed.set(4, 700, END + 12 hours + 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.StalePrice.selector);
        twap.calculate(DAY, ids(1, 4));
    }
    function testHostilePreDayAnchorCannotPoisonCanonicalDay() public {
        for (uint256 bad; bad < 3; bad++) {
            twap = new ChainlinkBTCDailyTWAP(address(feed));
            feed.set(1, 100, START - 100);
            // Invalid value, missing round, and stale/nonmonotonic pre-day round.
            feed.set(2, bad == 0 ? int256(0) : int256(200), bad == 1 ? 0 : START - 200);
            feed.set(3, 300, START - 10);
            feed.set(4, 300, START + 12 hours);
            feed.set(5, 700, END);
            vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
            twap.append(DAY, ids(1, 1));
            vm.expectRevert();
            twap.append(DAY, ids(1, 3));
            (,,,,,bool initialized,) = twap.checkpoints(DAY);
            assertFalse(initialized);
            vm.prank(address(2)); twap.append(DAY, ids(3, 3));
            assertEq(twap.finalizedPrice(DAY), 300);
        }
    }
    function testPhaseTransitionCannotSkipExistingInvalidRound() public {
        uint80 old = uint80((uint256(1) << 64) | 9);
        uint80 next = uint80((uint256(2) << 64) | 1);
        feed.set(old, 100, START);
        feed.set(next, 200, END);
        uint80[] memory page = new uint80[](2);
        page[0] = old; page[1] = next;
        assertEq(twap.calculate(DAY, page), 100);
        feed.set(old + 1, 0, START + 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.calculate(DAY, page);
    }
    function testFuzzPreDayPriceDoesNotAffectResult(uint128 price) public {
        four(); feed.set(1, int256(uint256(price)) + 1, START - 100);
        assertEq(twap.calculate(DAY, ids(1, 4)), 200);
    }
    function testExactStartBoundaryAndIncompleteDay() public {
        feed.set(1, 100, START); feed.set(2, 999, END);
        assertEq(twap.calculate(DAY, ids(1, 2)), 100);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, ids(1, 1));
        vm.warp(END - 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidDay.selector);
        twap.append(DAY, ids(1, 2));
    }
}
