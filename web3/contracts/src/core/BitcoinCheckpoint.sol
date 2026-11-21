// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "../libs/TargetsHelper.sol";
import "./BitcoinTxParser.sol";

/// @notice Mainnet checkpoint validation and initial pinned-verifier context.
/// @dev Internal component, not an activated state contract. Earlier cumulative
/// work is trusted configuration. Ten predecessors supply MTP only, never SPV
/// membership. Authentication/immutability of deployment inputs is external.
library BitcoinCheckpoint {
    uint256 internal constant POW_LIMIT = 0x00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff;
    struct Context {
        bytes32 blockHash;
        uint64 height;
        uint256 totalWork;
        uint32 currentTargetBits;
        uint32 epochStartTime;
        uint32[11] timestamps;
    }
    struct ParsedHeader { bytes32 parent; uint32 time; uint32 bits; }
    error InvalidHeight();
    error InvalidHeaderLength();
    error InvalidAncestorCount();
    error InvalidAncestorLink();
    error TargetAboveMainnetLimit();
    error InvalidProofOfWork();
    error InsufficientChainwork();

    function validate(bytes memory raw, uint64 height, uint256 work, bytes[] memory prior)
        internal pure returns (Context memory c, bytes32 descriptor)
    {
        if (height < 10 || height % 2016 != 0) revert InvalidHeight();
        if (prior.length != 10) revert InvalidAncestorCount();
        (ParsedHeader memory anchor, bytes32 hash) = parse(raw);
        uint256 target = TargetsHelper.bitsToTarget(anchor.bits);
        if (target > POW_LIMIT) revert TargetAboveMainnetLimit();
        if (uint256(BitcoinTxParser.flipBytes32(hash)) > target) revert InvalidProofOfWork();
        if (work < TargetsHelper.workFromTarget(target)) revert InsufficientChainwork();
        c.blockHash = hash; c.height = height; c.totalWork = work;
        c.currentTargetBits = anchor.bits; c.epochStartTime = anchor.time;
        bytes32 previous;
        bytes memory ancestors;
        for (uint256 i; i < 10; ++i) {
            (ParsedHeader memory h, bytes32 ancestorHash) = parse(prior[i]);
            if (i != 0 && h.parent != previous) revert InvalidAncestorLink();
            previous = ancestorHash;
            c.timestamps[(uint256(height) - 10 + i) % 11] = h.time;
            ancestors = bytes.concat(ancestors, prior[i]);
        }
        if (anchor.parent != previous) revert InvalidAncestorLink();
        c.timestamps[height % 11] = anchor.time;
        bytes[] memory parts = new bytes[](5);
        parts[0] = bytes("bitcoin-mainnet"); parts[1] = raw;
        parts[2] = abi.encodePacked(height); parts[3] = abi.encodePacked(work); parts[4] = ancestors;
        descriptor = framedHash(bytes("mainnet-checkpoint-v1"), parts);
    }

    function contextCommitment(Context memory c) internal pure returns (bytes32) {
        bytes memory times;
        for (uint256 i; i < 11; ++i) times = bytes.concat(times, abi.encodePacked(c.timestamps[i]));
        bytes[] memory parts = new bytes[](6);
        parts[0] = abi.encodePacked(c.blockHash); parts[1] = abi.encodePacked(c.height);
        parts[2] = abi.encodePacked(c.totalWork); parts[3] = abi.encodePacked(c.currentTargetBits);
        parts[4] = abi.encodePacked(c.epochStartTime); parts[5] = times;
        return framedHash(bytes("citrea-LatestDaState-v1"), parts);
    }

    function framedHash(bytes memory domain, bytes[] memory parts) private pure returns (bytes32) {
        bytes memory encoded = abi.encodePacked("SATOSHIFI_FORK_V1\x00", uint32(domain.length), domain);
        for (uint256 i; i < parts.length; ++i) encoded = bytes.concat(encoded, abi.encodePacked(uint64(parts[i].length), parts[i]));
        return sha256(encoded);
    }

    // Memory parsing/PoW byte order follow the pinned repository MainnetHeaderRelay.
    function parse(bytes memory raw) private pure returns (ParsedHeader memory h, bytes32 hash) {
        if (raw.length != 80) revert InvalidHeaderLength();
        bytes32 parent;
        assembly ("memory-safe") { parent := mload(add(raw, 36)) }
        h = ParsedHeader(parent, read32LE(raw, 68), read32LE(raw, 72));
        hash = sha256(abi.encodePacked(sha256(raw)));
    }
    function read32LE(bytes memory raw, uint256 at) private pure returns (uint32) {
        return uint32(uint8(raw[at])) | (uint32(uint8(raw[at + 1])) << 8) | (uint32(uint8(raw[at + 2])) << 16)
            | (uint32(uint8(raw[at + 3])) << 24);
    }
}
