// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

/// @notice Mutable test feed. Production deployments must select a real feed explicitly.
contract DAOMockFeed {
    constructor() {
        require(block.chainid == 31337 || block.chainid == 11155111, "integration network only");
    }
    struct Round {
        int256 price;
        uint256 timestamp;
    }

    mapping(uint80 => Round) public rounds;

    function decimals() external pure returns (uint8) {
        return 8;
    }

    function setRound(uint80 id, int256 price, uint256 timestamp) external {
        rounds[id] = Round(price, timestamp);
    }

    function getRoundData(uint80 id) external view returns (uint80, int256, uint256, uint256, uint80) {
        Round memory round = rounds[id];
        require(round.timestamp != 0, "no round");
        return (id, round.price, round.timestamp, round.timestamp, id);
    }
}

