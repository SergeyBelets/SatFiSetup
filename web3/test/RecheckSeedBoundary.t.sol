// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {DAOMockFeed} from "../contracts/src/demo/DAOMockFeed.sol";
import {ChainlinkBTCDailyTWAP} from "../contracts/src/federation/ChainlinkBTCDailyTWAP.sol";
import {SeedDAOTestFeed} from "../script/SeedDAOTestFeed.s.sol";

/// @dev Independent review regressions for the seed/TWAP phase boundary.
contract RecheckSeedBoundaryTest is Test {
    uint256 private constant DAY = 1_000;
    DAOMockFeed private feed;
    SeedDAOTestFeed private script;
    ChainlinkBTCDailyTWAP private twap;

    function setUp() public {
        vm.chainId(31337);
        vm.warp((DAY + 2) * 1 days);
        feed = new DAOMockFeed();
        script = new SeedDAOTestFeed();
        twap = new ChainlinkBTCDailyTWAP(address(feed));
    }

    function _history(uint256 firstId)
        private pure returns (uint256[] memory ids, int256[] memory prices, uint256[] memory times)
    {
        ids = new uint256[](2);
        prices = new int256[](2);
        times = new uint256[](2);
        ids[0] = firstId;
        ids[1] = firstId + 1;
        prices[0] = 100e8;
        prices[1] = 100e8;
        times[0] = DAY * 1 days;
        times[1] = (DAY + 1) * 1 days;
    }

    function _roundIds(uint256[] memory ids) private pure returns (uint80[] memory roundIds) {
        roundIds = new uint80[](2);
        roundIds[0] = uint80(ids[0]);
        roundIds[1] = uint80(ids[1]);
    }

    function test_SeedRejectsRoundZeroOfNextPhaseBeforeWritingAnyHistory() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history(type(uint64).max);
        vm.expectRevert("round phase boundary");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        (int256 openingPrice, uint256 openingTime) = feed.rounds(uint80(ids[0]));
        (int256 closingPrice, uint256 closingTime) = feed.rounds(uint80(ids[1]));
        assertEq(openingPrice, 0);
        assertEq(openingTime, 0);
        assertEq(closingPrice, 0);
        assertEq(closingTime, 0);
    }

    function test_TWAPStillRejectsArithmeticPhaseWrapFromDirectMockHistory() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history(type(uint64).max);
        feed.setRound(uint80(ids[0]), prices[0], times[0]);
        feed.setRound(uint80(ids[1]), prices[1], times[1]);
        uint80[] memory roundIds = _roundIds(ids);
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.calculate(DAY, roundIds);
    }

    function test_SeedAcceptsConsecutiveHighUint80IdsInsideTheSamePhase() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history(type(uint80).max - 1);
        script.seed(31337, address(feed), DAY, ids, prices, times);
        assertEq(twap.calculate(DAY, _roundIds(ids)), 100e8);
    }
}
