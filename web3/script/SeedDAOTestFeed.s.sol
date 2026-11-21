// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Script.sol";
import {DAOMockFeed} from "../contracts/src/demo/DAOMockFeed.sol";

/// @notice Explicit, idempotent test history. Include existing overlap when adding a day's rounds.
contract SeedDAOTestFeed is Script {
    function validate(
        uint256 expectedChainId, address feed, uint256 day,
        uint256[] memory ids, int256[] memory prices, uint256[] memory timestamps
    ) public view {
        require(block.chainid == expectedChainId, "chain mismatch");
        require(block.chainid == 31337 || block.chainid == 11155111, "integration network only");
        require(feed.code.length > 0 && DAOMockFeed(feed).decimals() == 8, "test feed missing");
        require(ids.length >= 2 && ids.length == prices.length && ids.length == timestamps.length, "round arrays");
        uint256 start = day * 1 days;
        uint256 end = start + 1 days;
        require(end <= block.timestamp, "day not closed");
        require(timestamps[0] <= start && start - timestamps[0] <= 1 days, "initial round window");
        require(timestamps[ids.length - 1] >= end, "closing round missing");
        for (uint256 i; i < ids.length; ++i) {
            require(ids[i] > 0 && ids[i] <= type(uint80).max && prices[i] > 0, "invalid round");
            require(timestamps[i] > 0 && timestamps[i] <= block.timestamp, "round timestamp");
            if (i + 1 < ids.length) require(timestamps[i] < end, "rounds after closing");
            if (i > 0) {
                require(ids[i] == ids[i-1] + 1, "round id gap");
                // TWAP treats the upper 16 bits as a Chainlink phase. A plain
                // increment across this boundary produces round zero of the
                // next phase, which is not a valid adjacent TWAP round.
                require((ids[i] >> 64) == (ids[i-1] >> 64), "round phase boundary");
                require(timestamps[i] > timestamps[i-1], "timestamp order");
                require(timestamps[i] - timestamps[i-1] <= 1 days, "round time gap");
            }
            (int256 priorPrice, uint256 priorTime) = DAOMockFeed(feed).rounds(uint80(ids[i]));
            require(
                (priorTime == 0 && priorPrice == 0) || (priorPrice == prices[i] && priorTime == timestamps[i]),
                "conflicting history"
            );
        }
    }

    function seed(
        uint256 expectedChainId, address feed, uint256 day,
        uint256[] memory ids, int256[] memory prices, uint256[] memory timestamps
    ) public {
        validate(expectedChainId, feed, day, ids, prices, timestamps);
        for (uint256 i; i < ids.length; ++i) {
            (int256 priorPrice, uint256 priorTime) = DAOMockFeed(feed).rounds(uint80(ids[i]));
            if (priorTime == 0 && priorPrice == 0) DAOMockFeed(feed).setRound(uint80(ids[i]), prices[i], timestamps[i]);
        }
    }

    function run() external {
        uint256 chain = vm.envUint("EXPECTED_CHAIN_ID");
        address feed = vm.envAddress("DAO_TEST_FEED");
        uint256 day = vm.envUint("DAO_TEST_DAY");
        uint256[] memory ids = vm.envUint("ROUND_IDS", ",");
        int256[] memory prices = vm.envInt("ROUND_PRICES_8", ",");
        uint256[] memory timestamps = vm.envUint("ROUND_TIMESTAMPS", ",");
        uint256 key = vm.envUint("PRIVATE_KEY");
        validate(chain, feed, day, ids, prices, timestamps);
        vm.startBroadcast(key);
        seed(chain, feed, day, ids, prices, timestamps);
        vm.stopBroadcast();
        console.log("Validated and seeded test rounds:", ids.length);
        console.log("Day:", day);
    }
}
