// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Secp256k1} from "../vendor/cryptography/Secp256k1.sol";
import {Secp256k1Arithmetic as EC} from "../vendor/cryptography/Secp256k1Arithmetic.sol";
import {Memory} from "../vendor/cryptography/Memory.sol";

/// @notice Candidate verifier for BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1.
/// @dev Computes Q = B + TapTweak(x(B), fixed intent leaf)G; no attestation,
/// caller-supplied tweak, tree or alternate output is accepted. B must already
/// be the post-DKG even-Y base. This component does not authenticate a DKG,
/// register an address, authorize issuance or establish Ready. The vendored EC
/// implementation still requires source/audit qualification before activation.
contract BitcoinDepositDerivation {
    bytes32 public constant PROFILE = keccak256("BASE_POST_DKG_EVEN_Y__INTENT_TAPLEAF_V1");
    bool public constant QUALIFIED = false;
    bytes32 private constant LEAF_TAG = sha256("TapLeaf");
    bytes32 private constant TWEAK_TAG = sha256("TapTweak");

    error InvalidIntent();
    error InvalidBase();
    error InvalidTweak();
    error InvalidDerivedKey();

    struct Descriptor {
        bytes32 intentId;
        bytes32 merkleRoot;
        bytes32 tweak;
        bytes32 outputKey;
        bool outputParityOdd;
        bytes32 bindingCommitment;
        bytes32 walletContext;
    }

    function derive(bytes calldata intent, bytes calldata baseGroup) public view returns (Descriptor memory d) {
        _checkIntent(intent);
        if (baseGroup.length != 33 || baseGroup[0] != 0x02) revert InvalidBase();
        bytes32 baseX = bytes32(baseGroup[1:33]);
        d.intentId = sha256(intent);
        d.walletContext = sha256(abi.encodePacked("SATOHSI_DEPOSIT_WALLET_CONTEXT_V1\x00",
            intent[30:114], intent[154:278]));
        bytes memory leaf = abi.encodePacked(hex"6a20", d.intentId);
        // Single leaf: version 0xc0, canonical CompactSize(34) = 0x22.
        d.merkleRoot = sha256(abi.encodePacked(LEAF_TAG, LEAF_TAG, hex"c022", leaf));
        d.tweak = sha256(abi.encodePacked(TWEAK_TAG, TWEAK_TAG, baseX, d.merkleRoot));
        (d.outputKey, d.outputParityOdd) = _addTweak(baseX, uint256(d.tweak));
        d.bindingCommitment = sha256(abi.encodePacked(
            "SATOHSI_DEPOSIT_BINDING_V1\x00", intent, baseGroup, leaf, d.merkleRoot,
            d.tweak, d.outputKey, uint8(d.outputParityOdd ? 1 : 0), hex"5120", d.outputKey
        ));
    }

    function verify(bytes calldata intent, bytes calldata baseGroup, bytes32 outputKey,
        bool outputParityOdd, bytes32 bindingCommitment) external view returns (bool)
    {
        Descriptor memory d = derive(intent, baseGroup);
        return d.outputKey == outputKey && d.outputParityOdd == outputParityOdd
            && d.bindingCommitment == bindingCommitment;
    }

    function _checkIntent(bytes calldata raw) internal pure {
        if (raw.length != 382 || keccak256(raw[:26]) != keccak256("SATOHSI_DEPOSIT_INTENT_V1\x00")
            || bytes2(raw[26:28]) != 0x0001 || bytes2(raw[28:30]) != 0x0001) revert InvalidIntent();
        // Shape only: the future registry must verify graph, DKG, chain,
        // recipient, authorization and current generation against EVM state.
        uint256[7] memory ids = [uint256(30), 62, 154, 206, 238, 318, 350];
        for (uint256 i; i < ids.length; ++i) {
            if (bytes32(raw[ids[i]:ids[i] + 32]) == 0) revert InvalidIntent();
        }
        uint256[6] memory addresses = [uint256(94), 114, 134, 186, 278, 298];
        for (uint256 i; i < addresses.length; ++i) {
            if (bytes20(raw[addresses[i]:addresses[i] + 20]) == 0) revert InvalidIntent();
        }
    }

    function _addTweak(bytes32 baseX, uint256 tweak) internal view returns (bytes32 outputX, bool odd) {
        if (uint256(baseX) >= Secp256k1.P) revert InvalidBase();
        uint256 baseY = Secp256k1.calculateY(uint256(baseX), false);
        // Strict BIP341 scalar interpretation, never reduce modulo n.
        if (tweak >= Secp256k1.N) revert InvalidTweak();
        (uint256 x, uint256 y, uint256 z) = EC.mulProjectivePoint(Secp256k1.GX, Secp256k1.GY, 1, tweak);
        (x, y, z) = EC.addProjectivePoint(uint256(baseX), baseY, 1, x, y, z);
        if (z == 0) revert InvalidDerivedKey();
        (x, y) = EC.convertProjectivePointToAffinePoint(Memory.allocate(0xc0), x, y, z);
        if (x >= Secp256k1.P || y >= Secp256k1.P || !Secp256k1.isOnCurve(x, y)
            || x == uint256(baseX)) revert InvalidDerivedKey();
        return (bytes32(x), y & 1 == 1);
    }
}
