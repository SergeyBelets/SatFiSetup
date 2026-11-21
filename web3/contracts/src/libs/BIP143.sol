// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "../core/BitcoinHash.sol";

/**
 * @title BIP143
 * @notice BIP143 (Segregated Witness) signature hash calculation
 * @dev Implements the sighash algorithm for SegWit transactions
 * Reference: https://github.com/bitcoin/bips/blob/master/bip-0143.mediawiki
 */
library BIP143 {

    uint32 constant SIGHASH_ALL = 0x01;
    uint32 constant SIGHASH_NONE = 0x02;
    uint32 constant SIGHASH_SINGLE = 0x03;
    uint32 constant SIGHASH_ANYONECANPAY = 0x80;

    /**
     * @notice Transaction input for sighash calculation
     */
    struct TxInput {
        bytes32 prevTxid;       // Previous transaction ID
        uint32 prevVout;        // Previous output index
        uint64 amount;          // Amount of the UTXO being spent (in satoshis)
        bytes scriptCode;       // Script code for this input
        uint32 sequence;        // Sequence number
    }

    /**
     * @notice Transaction output
     */
    struct TxOutput {
        uint64 value;           // Value in satoshis
        bytes scriptPubKey;     // Script public key
    }

    /**
     * @notice Calculate BIP143 sighash for a transaction input
     * @param version Transaction version (usually 2)
     * @param inputs All transaction inputs
     * @param outputs All transaction outputs
     * @param inputIndex Index of the input being signed
     * @param locktime Transaction locktime
     * @param sighashType Sighash type (default: SIGHASH_ALL)
     * @return Signature hash to be signed
     */
    function calculateSighash(
        uint32 version,
        TxInput[] memory inputs,
        TxOutput[] memory outputs,
        uint256 inputIndex,
        uint32 locktime,
        uint32 sighashType
    ) internal pure returns (bytes32) {
        require(inputIndex < inputs.length, "Invalid input index");

        TxInput memory input = inputs[inputIndex];

        // 1. nVersion (4 bytes, little-endian)
        bytes memory preimage = abi.encodePacked(uint32ToLE(version));

        // 2. hashPrevouts (32 bytes)
        bytes32 hashPrevouts = bytes32(0);
        if ((sighashType & SIGHASH_ANYONECANPAY) == 0) {
            bytes memory prevouts;
            for (uint256 i = 0; i < inputs.length; i++) {
                prevouts = abi.encodePacked(
                    prevouts,
                    inputs[i].prevTxid,
                    uint32ToLE(inputs[i].prevVout)
                );
            }
            hashPrevouts = BitcoinHash.doubleSha256(prevouts);
        }
        preimage = abi.encodePacked(preimage, hashPrevouts);

        // 3. hashSequence (32 bytes)
        bytes32 hashSequence = bytes32(0);
        if ((sighashType & SIGHASH_ANYONECANPAY) == 0 &&
            (sighashType & 0x1f) != SIGHASH_SINGLE &&
            (sighashType & 0x1f) != SIGHASH_NONE) {
            bytes memory sequences;
            for (uint256 i = 0; i < inputs.length; i++) {
                sequences = abi.encodePacked(sequences, uint32ToLE(inputs[i].sequence));
            }
            hashSequence = BitcoinHash.doubleSha256(sequences);
        }
        preimage = abi.encodePacked(preimage, hashSequence);

        // 4. outpoint being spent (36 bytes: txid + vout)
        preimage = abi.encodePacked(
            preimage,
            input.prevTxid,
            uint32ToLE(input.prevVout)
        );

        // 5. scriptCode (with varint length prefix)
        preimage = abi.encodePacked(
            preimage,
            encodeVarInt(input.scriptCode.length),
            input.scriptCode
        );

        // 6. value of the output spent by this input (8 bytes, little-endian)
        preimage = abi.encodePacked(preimage, uint64ToLE(input.amount));

        // 7. nSequence (4 bytes, little-endian)
        preimage = abi.encodePacked(preimage, uint32ToLE(input.sequence));

        // 8. hashOutputs (32 bytes)
        bytes32 hashOutputs = bytes32(0);
        if ((sighashType & 0x1f) != SIGHASH_SINGLE && (sighashType & 0x1f) != SIGHASH_NONE) {
            // SIGHASH_ALL
            bytes memory outputsData;
            for (uint256 i = 0; i < outputs.length; i++) {
                outputsData = abi.encodePacked(
                    outputsData,
                    uint64ToLE(outputs[i].value),
                    encodeVarInt(outputs[i].scriptPubKey.length),
                    outputs[i].scriptPubKey
                );
            }
            hashOutputs = BitcoinHash.doubleSha256(outputsData);
        } else if ((sighashType & 0x1f) == SIGHASH_SINGLE && inputIndex < outputs.length) {
            // SIGHASH_SINGLE
            bytes memory singleOutput = abi.encodePacked(
                uint64ToLE(outputs[inputIndex].value),
                encodeVarInt(outputs[inputIndex].scriptPubKey.length),
                outputs[inputIndex].scriptPubKey
            );
            hashOutputs = BitcoinHash.doubleSha256(singleOutput);
        }
        preimage = abi.encodePacked(preimage, hashOutputs);

        // 9. nLocktime (4 bytes, little-endian)
        preimage = abi.encodePacked(preimage, uint32ToLE(locktime));

        // 10. sighash type (4 bytes, little-endian)
        preimage = abi.encodePacked(preimage, uint32ToLE(sighashType));

        // Double SHA256 of the whole preimage
        return BitcoinHash.doubleSha256(preimage);
    }

    /**
     * @notice Calculate sighash for a single input (convenience function)
     * @dev Uses SIGHASH_ALL by default
     */
    function calculateSighashAll(
        uint32 version,
        TxInput[] memory inputs,
        TxOutput[] memory outputs,
        uint256 inputIndex,
        uint32 locktime
    ) internal pure returns (bytes32) {
        return calculateSighash(version, inputs, outputs, inputIndex, locktime, SIGHASH_ALL);
    }

    /**
     * @notice Encode a variable-length integer (CompactSize)
     * @param value Value to encode
     * @return Encoded bytes
     */
    function encodeVarInt(uint256 value) internal pure returns (bytes memory) {
        if (value < 0xfd) {
            return abi.encodePacked(uint8(value));
        } else if (value <= 0xffff) {
            return abi.encodePacked(uint8(0xfd), uint16ToLE(uint16(value)));
        } else if (value <= 0xffffffff) {
            return abi.encodePacked(uint8(0xfe), uint32ToLE(uint32(value)));
        } else {
            return abi.encodePacked(uint8(0xff), uint64ToLE(uint64(value)));
        }
    }

    /**
     * @notice Convert uint32 to little-endian bytes
     */
    function uint32ToLE(uint32 value) internal pure returns (bytes memory) {
        return abi.encodePacked(
            uint8(value),
            uint8(value >> 8),
            uint8(value >> 16),
            uint8(value >> 24)
        );
    }

    /**
     * @notice Convert uint16 to little-endian bytes
     */
    function uint16ToLE(uint16 value) internal pure returns (bytes memory) {
        return abi.encodePacked(
            uint8(value),
            uint8(value >> 8)
        );
    }

    /**
     * @notice Convert uint64 to little-endian bytes
     */
    function uint64ToLE(uint64 value) internal pure returns (bytes memory) {
        return abi.encodePacked(
            uint8(value),
            uint8(value >> 8),
            uint8(value >> 16),
            uint8(value >> 24),
            uint8(value >> 32),
            uint8(value >> 40),
            uint8(value >> 48),
            uint8(value >> 56)
        );
    }

    /**
     * @notice Create scriptCode for P2WPKH (Pay-to-Witness-PubKey-Hash)
     * @param pubkeyHash 20-byte hash160 of the public key
     * @return Script code for signing
     */
    function createP2WPKHScriptCode(bytes20 pubkeyHash) internal pure returns (bytes memory) {
        // For P2WPKH, scriptCode is: OP_DUP OP_HASH160 <pubkeyhash> OP_EQUALVERIFY OP_CHECKSIG
        return abi.encodePacked(
            hex"76a914", // OP_DUP OP_HASH160 + push 20 bytes
            pubkeyHash,
            hex"88ac"    // OP_EQUALVERIFY OP_CHECKSIG
        );
    }

    /**
     * @notice Create scriptCode for P2WSH (Pay-to-Witness-Script-Hash)
     * @param witnessScript The actual witness script being executed
     * @return Script code for signing
     */
    function createP2WSHScriptCode(bytes memory witnessScript) internal pure returns (bytes memory) {
        // For P2WSH, scriptCode is the actual witness script
        return witnessScript;
    }
}
