// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

/// @notice Membership in the selected canonical history, not all known branches.
/// @dev New application commitment code. The future state/adapter must supply a
/// real-proof-authenticated View and enforce Fresh. This library authenticates
/// neither a caller-supplied root nor Bitcoin consensus; it verifies membership
/// only. No production contract imports it yet.
library BitcoinCanonicalHistory {
    struct View {
        bytes32 anchor;
        uint64 anchorHeight;
        bytes32 head;
        uint64 headHeight;
        bytes32 canonicalRoot;
        bool halted;
    }

    function hash(bytes memory domain, bytes[] memory parts) private pure returns (bytes32) {
        bytes memory encoded = abi.encodePacked("SATOSHIFI_FORK_V1\x00", uint32(domain.length), domain);
        for (uint256 i; i < parts.length; ++i) {
            encoded = bytes.concat(encoded, abi.encodePacked(uint64(parts[i].length), parts[i]));
        }
        return sha256(encoded);
    }

    function verify(View memory view_, uint64 height, bytes memory raw, bytes32[] memory siblings)
        internal pure returns (bool)
    {
        if (view_.halted || raw.length != 80 || siblings.length != 64
            || height < view_.anchorHeight || height > view_.headHeight) return false;
        bytes[] memory parts = new bytes[](3);
        parts[0] = bytes("bitcoin-mainnet");
        parts[1] = abi.encodePacked(view_.anchor);
        parts[2] = abi.encodePacked(view_.anchorHeight);
        bytes32 scope = hash(bytes("mainnet-anchor"), parts);

        bytes32 merkleRoot;
        assembly ("memory-safe") { merkleRoot := mload(add(raw, 68)) }
        uint32 time = uint32(uint8(raw[68])) | (uint32(uint8(raw[69])) << 8)
            | (uint32(uint8(raw[70])) << 16) | (uint32(uint8(raw[71])) << 24);
        parts = new bytes[](5);
        parts[0] = abi.encodePacked(scope);
        parts[1] = abi.encodePacked(height);
        parts[2] = abi.encodePacked(sha256(abi.encodePacked(sha256(raw))));
        parts[3] = abi.encodePacked(merkleRoot);
        parts[4] = abi.encodePacked(time);
        bytes32 node = hash(bytes("canonical-header"), parts);
        parts = new bytes[](3);
        for (uint8 bit; bit < 64; ++bit) {
            parts[0] = abi.encodePacked(bytes1(bit + 1));
            bool right = ((height >> bit) & 1) != 0;
            parts[1] = abi.encodePacked(right ? siblings[bit] : node);
            parts[2] = abi.encodePacked(right ? node : siblings[bit]);
            node = hash(bytes("history-node"), parts);
        }
        parts = new bytes[](4);
        parts[0] = abi.encodePacked(scope);
        parts[1] = abi.encodePacked(view_.head);
        parts[2] = abi.encodePacked(view_.headHeight);
        parts[3] = abi.encodePacked(node);
        return hash(bytes("canonical-root"), parts) == view_.canonicalRoot;
    }
}
