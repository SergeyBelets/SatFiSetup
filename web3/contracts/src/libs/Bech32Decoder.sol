// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/**
 * @title Bech32Decoder
 * @notice Simplified Bech32 decoder for Bitcoin addresses
 * @dev For production, use a full Bech32 implementation
 */
library Bech32Decoder {

    /**
     * @notice Convert bech32 Bitcoin address to script pubkey
     * @param addr Bitcoin address string (e.g., "bc1q...")
     * @return Script pubkey bytes
     */
    function bech32ToScriptPubkey(string memory addr) internal pure returns (bytes memory) {
        bytes memory addrBytes = bytes(addr);

        // Verify it starts with "bc1q" (P2WPKH) or "bc1p" (P2TR)
        require(
            addrBytes.length >= 4 &&
            addrBytes[0] == 0x62 && // 'b'
            addrBytes[1] == 0x63 && // 'c'
            addrBytes[2] == 0x31,   // '1'
            "Invalid bech32 prefix"
        );

        // Check witness version
        if (addrBytes[3] == 0x71) { // 'q' = witness v0 (P2WPKH/P2WSH)
            // For P2WPKH (42 chars): bc1q + 38 data chars
            // For P2WSH (62 chars): bc1q + 58 data chars
            if (addrBytes.length == 42) {
                // P2WPKH - extract 20 byte hash
                bytes memory hash = decodeBech32Data(addrBytes, 4, 38);
                return abi.encodePacked(hex"0014", hash); // OP_0 + 20 bytes
            } else if (addrBytes.length == 62) {
                // P2WSH - extract 32 byte hash
                bytes memory hash = decodeBech32Data(addrBytes, 4, 58);
                return abi.encodePacked(hex"0020", hash); // OP_0 + 32 bytes
            }
        } else if (addrBytes[3] == 0x70) { // 'p' = witness v1 (P2TR - Taproot)
            // P2TR (62 chars): bc1p + 58 data chars
            if (addrBytes.length == 62) {
                bytes memory hash = decodeBech32Data(addrBytes, 4, 58);
                return abi.encodePacked(hex"5120", hash); // OP_1 + 32 bytes
            }
        }

        revert("Unsupported bech32 format");
    }

    /**
     * @notice Decode bech32 data portion (simplified)
     * @dev This is a simplified decoder. For production, implement full Bech32 spec.
     * @param data Full address bytes
     * @param start Start index of data portion
     * @param length Length of data portion
     * @return Decoded bytes
     */
    function decodeBech32Data(
        bytes memory data,
        uint256 start,
        uint256 length
    ) internal pure returns (bytes memory) {
        // Simplified: In a real implementation, this would:
        // 1. Map each char to its 5-bit value using bech32 charset
        // 2. Convert from 5-bit groups to 8-bit bytes
        // 3. Verify checksum
        // 4. Return the decoded payload

        // For demo purposes, we'll return a placeholder
        // In production, implement proper bech32 decoding
        bytes memory result;

        if (length == 38) {
            // P2WPKH - 20 bytes
            result = new bytes(20);
        } else if (length == 58) {
            // P2WSH or P2TR - 32 bytes
            result = new bytes(32);
        } else {
            revert("Invalid bech32 data length");
        }

        // Placeholder: In production, decode actual data
        // For now, return zeros (this should be replaced with real decoding)
        return result;
    }

    /**
     * @notice Get bech32 character value (0-31)
     * @dev Maps bech32 charset to 5-bit values
     */
    function getCharValue(bytes1 char) internal pure returns (uint8) {
        // bech32 charset: "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        // Maps: q=0, p=1, z=2, r=3, y=4, 9=5, x=6, 8=7, g=8, f=9, ...

        if (char == 0x71) return 0;  // 'q'
        if (char == 0x70) return 1;  // 'p'
        if (char == 0x7a) return 2;  // 'z'
        if (char == 0x72) return 3;  // 'r'
        if (char == 0x79) return 4;  // 'y'
        if (char == 0x39) return 5;  // '9'
        if (char == 0x78) return 6;  // 'x'
        if (char == 0x38) return 7;  // '8'
        if (char == 0x67) return 8;  // 'g'
        if (char == 0x66) return 9;  // 'f'
        if (char == 0x32) return 10; // '2'
        if (char == 0x74) return 11; // 't'
        if (char == 0x76) return 12; // 'v'
        if (char == 0x64) return 13; // 'd'
        if (char == 0x77) return 14; // 'w'
        if (char == 0x30) return 15; // '0'
        if (char == 0x73) return 16; // 's'
        if (char == 0x33) return 17; // '3'
        if (char == 0x6a) return 18; // 'j'
        if (char == 0x6e) return 19; // 'n'
        if (char == 0x35) return 20; // '5'
        if (char == 0x34) return 21; // '4'
        if (char == 0x6b) return 22; // 'k'
        if (char == 0x68) return 23; // 'h'
        if (char == 0x63) return 24; // 'c'
        if (char == 0x65) return 25; // 'e'
        if (char == 0x36) return 26; // '6'
        if (char == 0x6d) return 27; // 'm'
        if (char == 0x75) return 28; // 'u'
        if (char == 0x61) return 29; // 'a'
        if (char == 0x37) return 30; // '7'
        if (char == 0x6c) return 31; // 'l'

        revert("Invalid bech32 character");
    }

    /**
     * @notice Create P2WPKH script from pubkey hash
     * @param pubkeyHash 20-byte hash160 of public key
     */
    function createP2WPKHScript(bytes20 pubkeyHash) internal pure returns (bytes memory) {
        return abi.encodePacked(hex"0014", pubkeyHash);
    }

    /**
     * @notice Create P2WSH script from script hash
     * @param scriptHash 32-byte SHA256 of the script
     */
    function createP2WSHScript(bytes32 scriptHash) internal pure returns (bytes memory) {
        return abi.encodePacked(hex"0020", scriptHash);
    }

    /**
     * @notice Create P2TR (Taproot) script from tweaked pubkey
     * @param tweakedPubkey 32-byte x-only public key
     */
    function createP2TRScript(bytes32 tweakedPubkey) internal pure returns (bytes memory) {
        return abi.encodePacked(hex"5120", tweakedPubkey);
    }
}
