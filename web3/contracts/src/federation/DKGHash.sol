// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice ABI encoding defined in DKG_PROTOCOL_V1.md; no packed or JSON hashes.
library DKGHash {
    bytes32 internal constant PROTOCOL =
        keccak256("SATOHSI_DKG_FROST_SECP256K1_TR_2_2_0_POSTCARD_HPKE_X25519_CHACHA_V1");

    struct Context {
        uint256 chainId;
        address registry;
        bytes32 sessionId;
        uint64 epoch;
        bytes32 purpose;
        bytes32 protocol;
        bytes32 rosterHash;
        bytes32 transportKeysHash;
        uint16 count;
        uint16 threshold;
    }

    function contextHash(Context memory c) internal pure returns (bytes32) {
        return keccak256(abi.encode(keccak256("SATOHSI_DKG_CONTEXT_V1"), c));
    }

    function vss(bytes32 context, address sender, uint32 coefficient, bytes memory point)
        internal
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(keccak256("SATOHSI_DKG_VSS_COMMITMENT_V1"), context, sender, coefficient, point));
    }

    function round1(bytes32 context, address sender, bytes memory package) internal pure returns (bytes32) {
        return keccak256(abi.encode(keccak256("SATOHSI_DKG_ROUND1_PACKAGE_V1"), context, sender, package));
    }

    function share(bytes32 context, address sender, address recipient, bytes memory envelope)
        internal
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(keccak256("SATOHSI_DKG_ENCRYPTED_SHARE_V1"), context, sender, recipient, envelope));
    }

    function result(bytes32 context, bytes32 r1, bytes32 r2, bool negated, bytes32 groupX, bytes32 shares)
        internal
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(keccak256("SATOHSI_DKG_RESULT_V1"), context, r1, r2, negated, groupX, shares));
    }
}
