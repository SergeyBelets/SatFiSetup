// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {DAOTestUSD} from "../contracts/src/demo/DAOTestUSD.sol";
import {DAOMockFeed} from "../contracts/src/demo/DAOMockFeed.sol";
import {ChainlinkBTCDailyTWAP} from "../contracts/src/federation/ChainlinkBTCDailyTWAP.sol";
import {SeedDAOTestFeed} from "../script/SeedDAOTestFeed.s.sol";

contract SeedDAOTestFeedTest is Test {
    DAOMockFeed feed;
    ChainlinkBTCDailyTWAP twap;
    SeedDAOTestFeed script;
    uint256 constant DAY = 1_000;

    function setUp() public {
        vm.chainId(31337);
        vm.warp(1_005 days);
        feed = new DAOMockFeed();
        twap = new ChainlinkBTCDailyTWAP(address(feed));
        script = new SeedDAOTestFeed();
    }

    function _history() internal pure returns (uint256[] memory ids, int256[] memory prices, uint256[] memory times) {
        ids = new uint256[](3);
        prices = new int256[](3);
        times = new uint256[](3);
        ids[0] = 100; ids[1] = 101; ids[2] = 102;
        prices[0] = 100e8; prices[1] = 200e8; prices[2] = 150e8;
        times[0] = DAY * 1 days - 1 hours;
        times[1] = DAY * 1 days + 12 hours;
        times[2] = (DAY + 1) * 1 days;
    }

    function _roundIds() internal pure returns (uint80[] memory ids) {
        ids = new uint80[](3);
        ids[0] = 100; ids[1] = 101; ids[2] = 102;
    }

    function _seed() internal {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        script.seed(31337, address(feed), DAY, ids, prices, times);
    }

    function test_MocksDeployOnBothAllowedChainsAndRetainTestMethods() public {
        uint256[2] memory allowed = [uint256(31337), 11155111];
        for (uint256 i; i < allowed.length; i++) {
            vm.chainId(allowed[i]);
            DAOTestUSD usd = new DAOTestUSD();
            DAOMockFeed testFeed = new DAOMockFeed();
            assertEq(usd.decimals(), 6);
            assertEq(testFeed.decimals(), 8);
            usd.mint(address(this), 1e6);
            assertEq(usd.balanceOf(address(this)), 1e6);
            testFeed.setRound(7, 42e8, 100);
            (uint80 id, int256 price,, uint256 timestamp,) = testFeed.getRoundData(7);
            assertEq(id, 7); assertEq(price, 42e8); assertEq(timestamp, 100);
        }
    }

    function testFuzz_MocksRejectAnyOtherChain(uint64 chainId) public {
        vm.assume(chainId != 31337 && chainId != 11155111);
        vm.chainId(chainId);
        vm.expectRevert("integration network only");
        new DAOTestUSD();
        vm.expectRevert("integration network only");
        new DAOMockFeed();
    }

    function test_ExplicitHistoryIsUsableAndMissedExecutionCanRunLater() public {
        _seed();
        assertEq(twap.calculate(DAY, _roundIds()), 150e8);
        // The chosen UTC day closed several days before this test's current time.
        assertGt(block.timestamp, (DAY + 2) * 1 days);
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        vm.chainId(11155111);
        DAOMockFeed sepoliaFeed = new DAOMockFeed();
        script.seed(11155111, address(sepoliaFeed), DAY, ids, prices, times);
        assertEq(new ChainlinkBTCDailyTWAP(address(sepoliaFeed)).calculate(DAY, _roundIds()), 150e8);
    }

    function test_IdempotentSeedSkipsStorageWritesAndConflictsDoNotOverwrite() public {
        _seed();
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        vm.record();
        script.seed(31337, address(feed), DAY, ids, prices, times);
        (, bytes32[] memory writes) = vm.accesses(address(feed));
        assertEq(writes.length, 0);
        prices[1]++;
        vm.expectRevert("conflicting history");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        (int256 actual, uint256 timestamp) = feed.rounds(101);
        assertEq(actual, 200e8);
        assertEq(timestamp, DAY * 1 days + 12 hours);
        prices[1]--;
        times[1]++;
        vm.expectRevert("conflicting history");
        script.seed(31337, address(feed), DAY, ids, prices, times);
    }

    function test_ConflictingFinalRoundPreflightPreventsPartialSeed() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        feed.setRound(102, 999e8, times[2]);
        vm.expectRevert("conflicting history");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        (, uint256 firstTimestamp) = feed.rounds(100);
        assertEq(firstTimestamp, 0);
        // A malformed pre-existing round is also a conflict, not an empty slot.
        feed.setRound(102, prices[2], times[2]);
        feed.setRound(101, 5e8, 0);
        vm.expectRevert("conflicting history");
        script.seed(31337, address(feed), DAY, ids, prices, times);
    }

    function test_SeedChecksChainAndExplicitArrayConfiguration() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        vm.expectRevert("chain mismatch");
        script.seed(11155111, address(feed), DAY, ids, prices, times);
        vm.chainId(1);
        vm.expectRevert("integration network only");
        script.seed(1, address(feed), DAY, ids, prices, times);
        vm.chainId(31337);
        vm.expectRevert("test feed missing");
        script.seed(31337, address(0), DAY, ids, prices, times);
        vm.expectRevert("round arrays");
        script.seed(31337, address(feed), DAY, new uint256[](0), prices, times);
        vm.expectRevert("round arrays");
        script.seed(31337, address(feed), DAY, ids, new int256[](2), times);
        ids[1] = 103;
        vm.expectRevert("round id gap");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        ids[1] = 101;
        ids[0] = 0;
        vm.expectRevert("invalid round");
        script.seed(31337, address(feed), DAY, ids, prices, times);
    }

    function test_SeedRejectsNonPositivePriceInvalidTimeAndFutureHistory() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        prices[1] = 0;
        vm.expectRevert("invalid round");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        prices[1] = -1;
        vm.expectRevert("invalid round");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        prices[1] = 200e8;
        times[1] = times[0];
        vm.expectRevert("timestamp order");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        times[1] = DAY * 1 days + 12 hours;
        times[2] = block.timestamp + 1;
        vm.expectRevert("round timestamp");
        script.seed(31337, address(feed), DAY, ids, prices, times);
    }

    function test_SeedRejectsStaleAnchorGapMissingClosingAndUnclosedDay() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        times[0] = DAY * 1 days - 1 days - 1;
        vm.expectRevert("initial round window");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        times[0] = DAY * 1 days - 1 days;
        vm.expectRevert("round time gap");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        times[0] = DAY * 1 days + 1;
        vm.expectRevert("initial round window");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        times[0] = DAY * 1 days - 1 hours;
        times[2] = (DAY + 1) * 1 days - 1;
        vm.expectRevert("closing round missing");
        script.seed(31337, address(feed), DAY, ids, prices, times);
        times[2]++;
        vm.warp(DAY * 1 days + 12 hours);
        vm.expectRevert("day not closed");
        script.seed(31337, address(feed), DAY, ids, prices, times);
    }

    function test_SeedRejectsTrailingRoundsPastFirstClosing() public {
        (uint256[] memory ids, int256[] memory prices, uint256[] memory times) = _history();
        times[1] = (DAY + 1) * 1 days;
        times[2] = (DAY + 1) * 1 days + 1 hours;
        vm.expectRevert("rounds after closing");
        script.seed(31337, address(feed), DAY, ids, prices, times);
    }

    function test_TWAPRejectsMissingGapOrderBadPriceAndFutureEvenIfMockSetDirectly() public {
        _seed();
        feed.setRound(101, 200e8, 0);
        vm.expectRevert("no round");
        twap.calculate(DAY, _roundIds());
        feed.setRound(101, 0, DAY * 1 days + 12 hours);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, _roundIds());
        feed.setRound(101, -1, DAY * 1 days + 12 hours);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, _roundIds());
        feed.setRound(101, 200e8, DAY * 1 days - 2 hours);
        vm.expectRevert(ChainlinkBTCDailyTWAP.StalePrice.selector);
        twap.calculate(DAY, _roundIds());
        feed.setRound(101, 200e8, (DAY + 1) * 1 days + 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.StalePrice.selector);
        twap.calculate(DAY, _roundIds());
        feed.setRound(101, 200e8, block.timestamp + 1);
        vm.expectRevert(ChainlinkBTCDailyTWAP.InvalidRound.selector);
        twap.calculate(DAY, _roundIds());
        feed.setRound(101, 200e8, DAY * 1 days + 12 hours);
        uint80[] memory missing = new uint80[](2);
        missing[0] = 100; missing[1] = 102;
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.calculate(DAY, missing);
    }

    function test_PaginatedAppendAndCalculateEmptyAfterSeedingMoreThan512Rounds() public {
        uint256[] memory ids = new uint256[](601);
        int256[] memory prices = new int256[](601);
        uint256[] memory times = new uint256[](601);
        for (uint256 i; i < 601; i++) {
            ids[i] = 1_000 + i;
            prices[i] = 100e8;
            times[i] = DAY * 1 days + i * 144;
        }
        script.seed(31337, address(feed), DAY, ids, prices, times);
        uint80[] memory first = new uint80[](512);
        for (uint256 i; i < 512; i++) first[i] = uint80(ids[i]);
        (bool finalized, uint256 price) = twap.append(DAY, first);
        assertFalse(finalized); assertEq(price, 0);
        vm.expectRevert(ChainlinkBTCDailyTWAP.NotFinalized.selector);
        twap.calculate(DAY, new uint80[](0));
        uint80[] memory last = new uint80[](89);
        for (uint256 i; i < 89; i++) last[i] = uint80(ids[i + 512]);
        (finalized, price) = twap.append(DAY, last);
        assertTrue(finalized); assertEq(price, 100e8);
        assertEq(twap.calculate(DAY, new uint80[](0)), 100e8);
    }

    function test_NewDayRoundsCanReuseExactClosingAsAnchor() public {
        _seed();
        uint256[] memory ids = new uint256[](3);
        int256[] memory prices = new int256[](3);
        uint256[] memory times = new uint256[](3);
        ids[0] = 102; ids[1] = 103; ids[2] = 104;
        prices[0] = 150e8; prices[1] = 250e8; prices[2] = 200e8;
        times[0] = (DAY + 1) * 1 days;
        times[1] = times[0] + 12 hours;
        times[2] = (DAY + 2) * 1 days;
        script.seed(31337, address(feed), DAY + 1, ids, prices, times);
        uint80[] memory roundIds = new uint80[](3);
        roundIds[0] = 102; roundIds[1] = 103; roundIds[2] = 104;
        assertEq(twap.calculate(DAY + 1, roundIds), 200e8);
    }

    function test_RunUsesOnlyExplicitEnvironmentInputs() public {
        vm.setEnv("EXPECTED_CHAIN_ID", "31337");
        vm.setEnv("DAO_TEST_FEED", vm.toString(address(feed)));
        vm.setEnv("DAO_TEST_DAY", vm.toString(DAY));
        vm.setEnv("ROUND_IDS", "100,101,102");
        vm.setEnv("ROUND_PRICES_8", "10000000000,20000000000,15000000000");
        vm.setEnv("ROUND_TIMESTAMPS", string.concat(
            vm.toString(DAY * 1 days - 1 hours), ",",
            vm.toString(DAY * 1 days + 12 hours), ",", vm.toString((DAY + 1) * 1 days)));
        vm.setEnv("PRIVATE_KEY", "1");
        script.run();
        assertEq(twap.calculate(DAY, _roundIds()), 150e8);
        script.run();
        assertEq(twap.calculate(DAY, _roundIds()), 150e8);
    }
}
