// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "./Secp256k1.sol";

/**
 * @title Schnorr
 * @notice Schnorr signature scheme on secp256k1
 * @dev Implements BIP340 compatible Schnorr signatures
 */
library Schnorr {
    using Secp256k1 for *;

    // ======== STRUCTS ========

    struct Signature {
        uint256 r;  // R point x-coordinate (32 bytes)
        uint256 s;  // Signature scalar (32 bytes)
    }

    struct PublicKey {
        Secp256k1.Point point;
    }

    // ======== ERRORS ========

    error InvalidSignature();
    error InvalidPublicKey();
    error InvalidNonce();

    // ======== SIGNATURE OPERATIONS ========

    /**
     * @notice Verify Schnorr signature
     * @param message Message that was signed (32 bytes)
     * @param signature Signature (R, s)
     * @param publicKey Public key
     * @return valid True if signature is valid
     * @dev Verifies: R + hash(R || PK || m) * PK == s * G
     */
    function verify(
        bytes32 message,
        Signature memory signature,
        PublicKey memory publicKey
    ) internal view returns (bool) {
        // Verify public key is on curve
        if (!Secp256k1.isOnCurve(publicKey.point)) {
            return false;
        }

        // R point from signature
        Secp256k1.Point memory R = Secp256k1.Point(signature.r, 0);

        // Recover full R point (take even y-coordinate)
        R = recoverRPoint(signature.r);

        if (!Secp256k1.isOnCurve(R)) {
            return false;
        }

        // Compute challenge: e = H(R || PK || m)
        uint256 e = computeChallenge(R, publicKey.point, message);

        // Verify: R + e*PK == s*G
        // Rearranged: s*G - e*PK == R
        Secp256k1.Point memory sG = Secp256k1.scalarBaseMul(signature.s);
        Secp256k1.Point memory ePK = Secp256k1.scalarMul(e, publicKey.point);
        Secp256k1.Point memory left = Secp256k1.addPoints(sG, Secp256k1.negate(ePK));

        return Secp256k1.equals(left, R);
    }

    /**
     * @notice Compute Schnorr challenge
     * @param R Nonce commitment point
     * @param P Public key point
     * @param message Message
     * @return e Challenge scalar
     * @dev e = H(R.x || P.x || m) mod N
     */
    function computeChallenge(
        Secp256k1.Point memory R,
        Secp256k1.Point memory P,
        bytes32 message
    ) internal pure returns (uint256) {
        bytes memory data = abi.encodePacked(R.x, P.x, message);
        return Secp256k1.hashToScalar(data);
    }

    /**
     * @notice Recover R point from x-coordinate
     * @param x X-coordinate of R
     * @return R Full point (using even y-coordinate per BIP340)
     */
    function recoverRPoint(uint256 x) internal view returns (Secp256k1.Point memory) {
        // Compute y^2 = x^3 + 7
        uint256 ySquared = addmod(
            mulmod(mulmod(x, x, Secp256k1.P), x, Secp256k1.P),
            Secp256k1.B,
            Secp256k1.P
        );

        // Compute y = sqrt(ySquared)
        uint256 y = Secp256k1.expMod(ySquared, (Secp256k1.P + 1) / 4, Secp256k1.P);

        // Use even y-coordinate (BIP340 convention)
        if (y & 1 == 1) {
            y = Secp256k1.P - y;
        }

        return Secp256k1.Point(x, y);
    }

    /**
     * @notice Aggregate public keys
     * @param publicKeys Array of public keys
     * @return aggregated Aggregated public key
     */
    function aggregatePublicKeys(PublicKey[] memory publicKeys) internal view returns (PublicKey memory) {
        require(publicKeys.length > 0, "Empty public keys array");

        Secp256k1.Point memory result = publicKeys[0].point;

        for (uint256 i = 1; i < publicKeys.length; i++) {
            result = Secp256k1.addPoints(result, publicKeys[i].point);
        }

        return PublicKey(result);
    }

    /**
     * @notice Verify batch of signatures (more efficient than individual verification)
     * @param messages Array of messages
     * @param signatures Array of signatures
     * @param publicKeys Array of public keys
     * @return valid True if all signatures are valid
     */
    function batchVerify(
        bytes32[] memory messages,
        Signature[] memory signatures,
        PublicKey[] memory publicKeys
    ) internal view returns (bool) {
        require(
            messages.length == signatures.length && signatures.length == publicKeys.length,
            "Array length mismatch"
        );

        for (uint256 i = 0; i < messages.length; i++) {
            if (!verify(messages[i], signatures[i], publicKeys[i])) {
                return false;
            }
        }

        return true;
    }

    /**
     * @notice Create public key from point
     * @param point Elliptic curve point
     * @return pk Public key
     */
    function publicKeyFromPoint(Secp256k1.Point memory point) internal pure returns (PublicKey memory) {
        require(Secp256k1.isOnCurve(point), "Point not on curve");
        return PublicKey(point);
    }

    /**
     * @notice Encode signature to bytes
     * @param sig Signature
     * @return encoded 64 bytes: r || s
     */
    function encodeSignature(Signature memory sig) internal pure returns (bytes memory) {
        return abi.encodePacked(sig.r, sig.s);
    }

    /**
     * @notice Decode signature from bytes
     * @param encoded 64 bytes signature
     * @return sig Decoded signature
     */
    function decodeSignature(bytes memory encoded) internal pure returns (Signature memory) {
        require(encoded.length == 64, "Invalid signature length");

        uint256 r;
        uint256 s;

        assembly {
            r := mload(add(encoded, 32))
            s := mload(add(encoded, 64))
        }

        return Signature(r, s);
    }
}
