// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

interface IChainlinkBTCUSD {
    function decimals() external view returns (uint8);
    function getRoundData(uint80 roundId) external view returns (uint80, int256, uint256, uint256, uint80);
}

contract ChainlinkBTCDailyTWAP {
    uint256 public constant MAX_PRICE_AGE = 1 days;
    uint256 public constant MAX_ROUNDS = 512;
    uint256 private constant ROUND_MASK = type(uint64).max;

    IChainlinkBTCUSD public immutable feed;

    error InvalidFeed();
    error InvalidDay();
    error InvalidRound();
    error MissingRound();
    error StalePrice();

    constructor(address feed_) {
        if (feed_ == address(0) || feed_.code.length == 0) revert InvalidFeed();
        if (IChainlinkBTCUSD(feed_).decimals() != 8) revert InvalidFeed();
        feed = IChainlinkBTCUSD(feed_);
    }

    struct Checkpoint {
        uint80 lastRound;
        uint256 lastPrice;
        uint256 lastTime;
        uint256 cursor;
        uint256 weightedPrice;
        bool initialized;
        bool finalized;
    }

    mapping(uint256 => Checkpoint) public checkpoints;
    error AlreadyFinalized();
    error NotFinalized();

    /// @notice Immutable daily result, available after append reaches the first round at or after day end.
    function finalizedPrice(uint256 day) public view returns (uint256) {
        Checkpoint storage checkpoint = checkpoints[day];
        if (!checkpoint.finalized) revert NotFinalized();
        return checkpoint.weightedPrice / 1 days;
    }

    /// @notice Permissionless canonical stream. Each page contains only new consecutive rounds.
    /// The first round must be at or before day start; pre-day rounds contribute no duration.
    /// The initial page must also reach a round strictly after day start, proving the opening.
    /// The final page ends exactly at the first round whose timestamp is at or after day end.
    function append(uint256 day, uint80[] calldata roundIds) external returns (bool finalized, uint256 price8) {
        Checkpoint memory checkpoint = checkpoints[day];
        if (checkpoint.finalized) revert AlreadyFinalized();
        bool initializing = !checkpoint.initialized;
        checkpoint = _process(day, roundIds, checkpoint);
        // Prove the opening before committing a shared stream: an obsolete pre-day
        // anchor must not lock the day behind an invalid or missing pre-day round.
        if (initializing && checkpoint.lastTime <= day * 1 days) revert InvalidRound();
        checkpoints[day] = checkpoint;
        return (checkpoint.finalized, checkpoint.finalized ? checkpoint.weightedPrice / 1 days : 0);
    }

    function calculate(uint256 day, uint80[] calldata roundIds) external view returns (uint256 price8) {
        if (roundIds.length == 0) return finalizedPrice(day);
        Checkpoint memory checkpoint;
        checkpoint = _process(day, roundIds, checkpoint);
        if (!checkpoint.finalized) revert InvalidRound();
        return checkpoint.weightedPrice / 1 days;
    }

    function _process(uint256 day, uint80[] calldata roundIds, Checkpoint memory checkpoint)
        private view returns (Checkpoint memory)
    {
        uint256 start = day * 1 days;
        uint256 end = start + 1 days;
        if (block.timestamp < end) revert InvalidDay();
        if (roundIds.length == 0 || roundIds.length > MAX_ROUNDS) revert InvalidRound();
        uint256 i;
        if (!checkpoint.initialized) {
            (checkpoint.lastPrice, checkpoint.lastTime) = _read(roundIds[0]);
            if (checkpoint.lastTime > start) revert InvalidRound();
            if (start - checkpoint.lastTime > MAX_PRICE_AGE) revert StalePrice();
            checkpoint.lastRound = roundIds[0];
            checkpoint.cursor = start;
            checkpoint.initialized = true;
            i = 1;
        }
        for (; i < roundIds.length; i++) {
            if (!_adjacent(checkpoint.lastRound, roundIds[i])) revert MissingRound();
            (uint256 currentPrice, uint256 currentTime) = _read(roundIds[i]);
            if (currentTime <= checkpoint.lastTime || currentTime - checkpoint.lastTime > MAX_PRICE_AGE) {
                revert StalePrice();
            }
            uint256 until = currentTime < end ? currentTime : end;
            // Never move the cursor before day start, even when traversing pre-day rounds.
            if (until > checkpoint.cursor) {
                checkpoint.weightedPrice += checkpoint.lastPrice * (until - checkpoint.cursor);
                checkpoint.cursor = until;
            }
            checkpoint.lastRound = roundIds[i];
            checkpoint.lastPrice = currentPrice;
            checkpoint.lastTime = currentTime;
            if (checkpoint.cursor == end) {
                if (i != roundIds.length - 1) revert InvalidRound();
                checkpoint.finalized = true;
                break;
            }
        }
        return checkpoint;
    }

    function _read(uint80 roundId) private view returns (uint256 price, uint256 updatedAt) {
        (uint80 returnedId, int256 answer,, uint256 timestamp, uint80 answeredInRound) = feed.getRoundData(roundId);
        if (returnedId != roundId || answer <= 0 || timestamp == 0 || timestamp > block.timestamp || answeredInRound < roundId) {
            revert InvalidRound();
        }
        return (uint256(answer), timestamp);
    }

    function _adjacent(uint80 previous, uint80 current) private view returns (bool) {
        uint256 oldPhase = uint256(previous) >> 64;
        uint256 newPhase = uint256(current) >> 64;
        if (oldPhase == newPhase) return (uint256(previous) & ROUND_MASK) < ROUND_MASK && current == previous + 1;
        if (newPhase != oldPhase + 1 || (uint256(current) & ROUND_MASK) != 1) return false;

        // A new proxy phase can start only after the previous phase has no next round.
        try feed.getRoundData(previous + 1) returns (uint80, int256, uint256, uint256, uint80) {
            return false;
        } catch {
            return true;
        }
    }
}
