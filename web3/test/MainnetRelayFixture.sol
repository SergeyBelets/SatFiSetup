// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/federation/MainnetHeaderRelay.sol";

/// @dev Test-only access to arithmetic and structurally valid synthetic ancestry. Not a production artifact.
contract MainnetRelayHarness is MainnetHeaderRelay {
    uint256 private serial;
    constructor(bytes memory header, uint64 height, uint256 work, bytes[] memory prior, uint64 limit)
        MainnetHeaderRelay(header, height, work, prior, limit)
    {}

    function syntheticChild(bytes32 parent, uint256 workDelta, bytes32 root, uint32 timestamp)
        external
        returns (bytes32 hash)
    {
        require(!halted, "test relay halted");
        require(blocks[parent].height != 0, "test parent");
        hash = keccak256(abi.encode(parent, ++serial, root, timestamp));
        ParsedHeader memory h = ParsedHeader(parent, root, timestamp, blocks[parent].bits);
        _storeHeader(hash, h, blocks[parent].height + 1, blocks[parent].cumulativeWork + workDelta);
        _selectHead(hash);
    }

    function hashMeetsTarget(bytes32 hash, uint256 target) external pure returns (bool) {
        return _hashMeetsTarget(hash, target);
    }

    function medianTimePast(bytes32 parent) external view returns (uint32) {
        return _medianTimePast(parent);
    }

    function validateTime(uint32 timestamp, bytes32 parent) external view {
        _validateTime(timestamp, parent);
    }

    function expectedBits(bytes32 parent, uint64 height) external view returns (uint32) {
        return _expectedBits(parent, height);
    }

    function ancestor(bytes32 node, uint64 height) external view returns (bytes32) {
        return _ancestor(node, height);
    }

    function commonAncestor(bytes32 a, bytes32 b) external view returns (bytes32) {
        return _commonAncestor(a, b);
    }
}

abstract contract MainnetFixtureBase is Test {
    uint64 internal constant FIRST = 32246;
    uint64 internal constant CHECKPOINT = 32256;
    uint64 internal constant LAST = 36288;
    bytes internal realHeaders;
    bytes internal expectedRecords;

    function loadMainnetFixture() internal {
        realHeaders = vm.readFileBinary("test/fixtures/mainnet/headers-32246-36288.bin");
        expectedRecords = vm.readFileBinary("test/fixtures/mainnet/expected-32246-36288.bin");
        assertEq(realHeaders.length, (LAST - FIRST + 1) * 80);
        assertEq(expectedRecords.length, (LAST - FIRST + 1) * 68);
        vm.warp(1600000000);
    }

    function checkpointAncestors() internal view returns (bytes[] memory result) {
        result = new bytes[](10);
        for (uint256 i; i < 10; ++i) {
            result[i] = headerAt(FIRST + uint64(i));
        }
    }

    function newRelay(uint64 limit) internal returns (MainnetHeaderRelay) {
        return new MainnetHeaderRelay(
            headerAt(CHECKPOINT), CHECKPOINT, expectedWork(CHECKPOINT), checkpointAncestors(), limit
        );
    }

    function newHarness(uint64 limit) internal returns (MainnetRelayHarness) {
        return new MainnetRelayHarness(
            headerAt(CHECKPOINT), CHECKPOINT, expectedWork(CHECKPOINT), checkpointAncestors(), limit
        );
    }

    function headerAt(uint64 height) internal view returns (bytes memory) {
        bytes memory raw = new bytes(80);
        uint256 at = (height - FIRST) * 80;
        for (uint256 i; i < 80; ++i) {
            raw[i] = realHeaders[at + i];
        }
        return raw;
    }

    function wireHash(uint64 height) internal view returns (bytes32) {
        return digest(headerAt(height));
    }

    function expectedWork(uint64 height) internal view returns (uint256) {
        uint256 offset = (height - FIRST) * 68 + 32;
        uint256 value;
        for (uint256 i; i < 32; ++i) {
            value = (value << 8) | uint8(expectedRecords[offset + i]);
        }
        return value;
    }

    function expectedDisplayHash(uint64 height) internal view returns (bytes32 value) {
        uint256 offset = (height - FIRST) * 68;
        uint256 result;
        for (uint256 i; i < 32; ++i) {
            result = (result << 8) | uint8(expectedRecords[offset + i]);
        }
        value = bytes32(result);
    }

    function expectedBits(uint64 height) internal view returns (uint32 value) {
        uint256 offset = (height - FIRST) * 68 + 64;
        for (uint256 i; i < 4; ++i) {
            value = (value << 8) | uint8(expectedRecords[offset + i]);
        }
    }

    function timestamp(bytes memory raw) internal pure returns (uint32) {
        return uint32(uint8(raw[68])) | uint32(uint8(raw[69])) << 8 | uint32(uint8(raw[70])) << 16
            | uint32(uint8(raw[71])) << 24;
    }

    function write32(bytes memory data, uint256 at, uint32 value) internal pure {
        for (uint256 i; i < 4; ++i) {
            data[at + i] = bytes1(uint8(value >> (8 * i)));
        }
    }

    function slice(bytes memory data, uint256 at, uint256 length) internal pure returns (bytes memory result) {
        require(at + length <= data.length, "fixture bounds");
        result = new bytes(length);
        assembly ("memory-safe") { mcopy(add(result, 32), add(add(data, 32), at), length) }
    }

    function digest(bytes memory data) internal pure returns (bytes32) {
        return sha256(abi.encodePacked(sha256(data)));
    }
}
