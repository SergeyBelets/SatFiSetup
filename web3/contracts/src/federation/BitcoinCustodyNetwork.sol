// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

interface ICustodyRelayGuard {
    function relay() external view returns (address);
}

interface ICustodySpvAdapter {
    function state() external view returns (address);
    function testHelpersEnabled() external view returns (bool);
}

interface ICustodyZkState {
    function adapter() external view returns (address);
    function verifier() external view returns (address);
    function verifierCodeHash() external view returns (bytes32);
    function programImageId() external view returns (bytes32);
}

interface ICustodyZkVerifier {
    function CONTROL_ROOT_0() external view returns (bytes16);
    function CONTROL_ROOT_1() external view returns (bytes16);
    function BN254_CONTROL_ID() external view returns (bytes32);
    function SELECTOR() external view returns (bytes4);
}

/// @dev Classifies supported payment SPV executables without trusting a network getter.
/// Constructor provenance and enrollment of actual checkpoints are additionally reviewed by participants.
library BitcoinCustodyNetwork {
    bytes32 internal constant MAINNET = 0x000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f;
    bytes32 internal constant TESTNET4 = 0x00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043;
    bytes32 internal constant REGTEST = 0x0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206;

    error UnsupportedCustodySPV();
    error CustodyNetworkMismatch(bytes32 source, bytes32 destination);

    function requireMatch(address source, bytes32 destination) internal view {
        bytes32 actual = genesis(source);
        if (actual != destination) revert CustodyNetworkMismatch(actual, destination);
    }

    function genesis(address endpoint) internal view returns (bytes32) {
        bytes memory code = endpoint.code;
        // RegtestHeaderRelay uses one fixed constructor, including zero-valued immutables.
        if (_matches(code, 9051, 0x5aec49147a177f77e84ef1138d668d32a63b19c0a278961dc845d737eacc3aa3, hex"")) {
            if (ICustodySpvAdapter(endpoint).testHelpersEnabled()) revert UnsupportedCustodySPV();
            return REGTEST;
        }
        // MainnetSPVGuard (shared freshness boundary for both public relays)
        if (_matches(code, 1466, 0x739836d8954c634f952a46b0ee5af86533ba58f07f7a91d342d9607544b61e22, hex"04007202370420050c02019002db")) {
            code = ICustodyRelayGuard(endpoint).relay().code;
            // MainnetHeaderRelay
            if (_matches(code, 5902, 0xda754e6584e19a9fd0c69bb5ce2e8daf254e5d93ea23ccbeec7ab49f5da764b6, hex"0201161195040276056308c00d820204810c210104ee")) return MAINNET;
            // Testnet4HeaderRelay
            if (_matches(code, 6277, 0x5bc1fe9974e656a8f3d0b0db5b24ca9ab96701d8af43181b99140367a7cc6f12, hex"02012113ca040281058c08e90ddc02048c0c7a010517")) return TESTNET4;
            revert UnsupportedCustodySPV();
        }
        // ZkSpvAdapter
        if (_matches(code, 4977, 0x59f6803c7f4fa5ebbb9debdb266306ffd54bbf2d684fb1dcbd02e0e602eb0415, hex"05008801db04b106e70891")) {
            _checkZk(endpoint, false);
            return MAINNET;
        }
        // ZkTestnet4SpvAdapter
        if (_matches(code, 4979, 0x0cac4dcbcd64dff8e25ca9aa43ff1abdcb23011a989cec18fd238c2d8e8e76b9, hex"05008801db04b106e70891")) {
            _checkZk(endpoint, true);
            return TESTNET4;
        }
        revert UnsupportedCustodySPV();
    }

    function _checkZk(address endpoint, bool testnet) private view {
        address parent = ICustodySpvAdapter(endpoint).state();
        bytes memory code = parent.code;
        // ZkBitcoinTestnet4StateV2 and ZkBitcoinStateV2
        bool recognized = testnet
            ? _matches(code, 9476, 0xf5756c2fe894ff17e33529d25a6c41c0bff057ab49b52c0b1ce72dd312e5e840, hex"02016003e4030199029d1fa60101e003027503241fd70203b81c320204611bb00211411b52011cc6011d2f")
            : _matches(code, 9475, 0xbdf92a05e3c374239d334d59948c33cf81673e57541992a96cea48408944d299, hex"02016003e4030199029d1fa50101e003027503241fd60203b81c310204611baf0211411b51011cc5011d2e");
        if (!recognized || _child(parent, 2) != endpoint) revert UnsupportedCustodySPV();
        ICustodyZkState state = ICustodyZkState(parent);
        bytes32 image = testnet
            ? bytes32(0xa6d885a56488897d9b181b5d224652ad67e020d3868060a3e5aae20a4ae82ab8)
            : bytes32(0xa4c00c90dcc2600acd756df56ae4915e2180d0ebacc20c157382ec184fe0adea);
        if (state.adapter() != endpoint || state.programImageId() != image) revert UnsupportedCustodySPV();
        address verifier = state.verifier();
        if (_child(parent, 1) != verifier || verifier.codehash != state.verifierCodeHash()) revert UnsupportedCustodySPV();
        _checkVerifier(verifier);
    }

    function _checkVerifier(address verifier) private view {
        bytes memory code = verifier.code;
        // RiscZeroGroth16Verifier 3.1.0
        if (!_matches(code, 4665, 0x158a4ce09f5a5453a2f879fadfced10fdefa437bd23775150377edb169c46786, hex"0200ad0d2e0201660e200205730de80205b80dc2")) revert UnsupportedCustodySPV();
        ICustodyZkVerifier binding = ICustodyZkVerifier(verifier);
        if (binding.SELECTOR() != hex"e71e6620"
            || binding.CONTROL_ROOT_0() != hex"5c1fde52087b1c734aed7adb13effee6"
            || binding.CONTROL_ROOT_1() != hex"1d553a4d183353d025f673607237b189"
            || binding.BN254_CONTROL_ID() != hex"0ac2eaf9d5ae64ffb75c4747fde32456e9ee6db3b32350cb2a0614ba91d081de") {
            revert UnsupportedCustodySPV();
        }
    }

    function _child(address parent, uint8 nonce) private pure returns (address) {
        return address(uint160(uint256(keccak256(abi.encodePacked(hex"d694", parent, nonce)))));
    }

    /// @dev Pinned Solidity 0.8.39 executable, excluding the 53-byte CBOR footer.
    /// Layout is repeated groups of occurrence count (u8) and offsets (big-endian u16).
    /// A repeated immutable may vary between deployments but must agree inside one executable.
    function _matches(bytes memory code, uint256 length, bytes32 expected, bytes memory layout) private pure returns (bool) {
        if (code.length != length) return false;
        // Work on a copy. If classification fails, the next profile must still
        // see the original executable and all immutable values.
        bytes memory executable = new bytes(length - 53);
        assembly ("memory-safe") { mcopy(add(executable, 32), add(code, 32), mload(executable)) }
        uint256 cursor;
        while (cursor < layout.length) {
            uint256 occurrences = uint8(layout[cursor++]);
            bytes32 first;
            for (uint256 i; i < occurrences; ++i) {
                uint256 offset = (uint256(uint8(layout[cursor])) << 8) | uint8(layout[cursor + 1]);
                cursor += 2;
                bytes32 value;
                assembly ("memory-safe") {
                    let slot := add(add(executable, 32), offset)
                    value := mload(slot)
                    mstore(slot, 0)
                }
                if (i == 0) first = value;
                else if (first != value) return false;
            }
        }
        return keccak256(executable) == expected;
    }
}
