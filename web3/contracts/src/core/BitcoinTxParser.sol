// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

library BitcoinTxParser {
    error InvalidVarInt();
    error InvalidTxLength();

    struct VarInt { uint256 value; uint256 size; }
    struct OutputScan { uint256 offset; uint256 inputCount; }

    function _readVarInt(bytes calldata raw, uint256 offset) internal pure returns (VarInt memory varInt) {
        if (offset >= raw.length) revert InvalidVarInt();
        uint8 fb = uint8(raw[offset]);
        if (fb < 0xFD) return VarInt(fb, 1);
        if (fb == 0xFD) {
            if (offset + 3 > raw.length) revert InvalidVarInt();
            uint16 value16 = uint16(uint8(raw[offset+1])) | (uint16(uint8(raw[offset+2])) << 8);
            return VarInt(value16, 3);
        }
        if (fb == 0xFE) {
            if (offset + 5 > raw.length) revert InvalidVarInt();
            uint32 value32 = uint32(uint8(raw[offset+1])) |
                        (uint32(uint8(raw[offset+2])) << 8) |
                        (uint32(uint8(raw[offset+3])) << 16) |
                        (uint32(uint8(raw[offset+4])) << 24);
            return VarInt(value32, 5);
        }
        if (offset + 9 > raw.length) revert InvalidVarInt();
        uint256 value;
        unchecked {
            for (uint i = 0; i < 8; ++i) {
                value |= uint256(uint8(raw[offset + 1 + i])) << (8 * i);
            }
        }
        return VarInt(value, 9);
    }

    /**
     * @notice Parse first input from Bitcoin transaction
     * @dev Extracts prevTxId and vout from the first input
     * @param raw Raw Bitcoin transaction bytes
     * @return prevTxIdLE Previous transaction ID (little-endian)
     * @return vout Output index being spent
     */
    function parseFirstInput(bytes calldata raw) internal pure returns (bytes32 prevTxIdLE, uint32 vout) {
        require(raw.length >= 41, "InvalidTxLength");
        uint256 offset = 4;

        // Check for witness flag
        if (offset + 2 <= raw.length && raw[offset] == 0x00 && raw[offset+1] == 0x01) {
            offset += 2;
        }

        // Read input count
        VarInt memory varInt = _readVarInt(raw, offset);
        require(varInt.value >= 1, "no inputs");
        offset += varInt.size;

        // Read prevTxId (32 bytes)
        assembly {
            prevTxIdLE := calldataload(add(raw.offset, offset))
        }
        offset += 32;

        // Read vout (4 bytes, little-endian)
        vout = uint32(uint8(raw[offset])) |
               (uint32(uint8(raw[offset+1])) << 8) |
               (uint32(uint8(raw[offset+2])) << 16) |
               (uint32(uint8(raw[offset+3])) << 24);
    }

    /**
     * @notice Parse all inputs from Bitcoin transaction
     * @dev Returns arrays of all prevTxIds and vouts
     * @param raw Raw Bitcoin transaction bytes
     * @return prevTxIds Array of previous transaction IDs (little-endian)
     * @return vouts Array of output indices
     */
    function parseAllInputs(bytes calldata raw) internal pure returns (bytes32[] memory prevTxIds, uint32[] memory vouts) {
        require(raw.length >= 10, "InvalidTxLength");
        uint256 offset = 4;

        // Check for witness flag
        if (offset + 2 <= raw.length && raw[offset] == 0x00 && raw[offset+1] == 0x01) {
            offset += 2;
        }

        // Read input count
        VarInt memory vinCount = _readVarInt(raw, offset);
        offset += vinCount.size;

        require(vinCount.value>0 && vinCount.value<=256,"input count");
        prevTxIds = new bytes32[](vinCount.value);
        vouts = new uint32[](vinCount.value);

        for (uint256 i = 0; i < vinCount.value; i++) {
            // Read prevTxId (32 bytes)
            bytes32 prevTxId;
            assembly {
                prevTxId := calldataload(add(raw.offset, offset))
            }
            prevTxIds[i] = prevTxId;
            offset += 32;

            // Read vout (4 bytes, little-endian)
            uint32 vout = uint32(uint8(raw[offset])) |
                         (uint32(uint8(raw[offset+1])) << 8) |
                         (uint32(uint8(raw[offset+2])) << 16) |
                         (uint32(uint8(raw[offset+3])) << 24);
            vouts[i] = vout;
            offset += 4;

            // Skip scriptSig
            VarInt memory scriptLen = _readVarInt(raw, offset);
            offset += scriptLen.size + scriptLen.value;

            // Skip sequence
            offset += 4;
        }
    }

    function txId(bytes calldata raw) internal pure returns (bytes32) {
        require(raw.length >= 10, "InvalidTxLength");
        if (!_hasWitness(raw)) {
            bytes memory rawMemory = raw;
            return doubleSha256(rawMemory);
        }

        OutputScan memory scan = _scanOutputs(raw);
        uint256 locktimeOffset = scan.offset;

        for (uint256 i = 0; i < scan.inputCount; i++) {
            VarInt memory itemCount = _readVarInt(raw, locktimeOffset);
            locktimeOffset += itemCount.size;

            for (uint256 j = 0; j < itemCount.value; j++) {
                VarInt memory itemLength = _readVarInt(raw, locktimeOffset);
                locktimeOffset += itemLength.size;
                if (locktimeOffset + itemLength.value > raw.length) revert InvalidTxLength();
                locktimeOffset += itemLength.value;
            }
        }

        if (locktimeOffset + 4 != raw.length) revert InvalidTxLength();

        bytes memory stripped = new bytes(4 + (scan.offset - 6) + 4);
        uint256 dst = 0;
        for (uint256 i = 0; i < 4; i++) {
            stripped[dst++] = raw[i];
        }
        for (uint256 i = 6; i < scan.offset; i++) {
            stripped[dst++] = raw[i];
        }
        for (uint256 i = locktimeOffset; i < locktimeOffset + 4; i++) {
            stripped[dst++] = raw[i];
        }

        return doubleSha256(stripped);
    }

    function outputCount(bytes calldata raw) internal pure returns(uint256) {
        VarInt memory count=_readVarInt(raw,_skipInputs(raw));require(count.value<=256,"output count");return count.value;
    }
    function outputAt(bytes calldata raw,uint256 vout) internal pure returns(uint64 amount,bytes memory script) {
        uint256 offset=_skipInputs(raw); VarInt memory count=_readVarInt(raw,offset);offset+=count.size;
        require(count.value<=256 && vout<count.value,"output index");
        for(uint i;i<count.value;i++) {
            amount=_readUint64LE(raw,offset);offset+=8;
            VarInt memory len=_readVarInt(raw,offset);offset+=len.size;
            require(offset+len.value<=raw.length,"output bounds");
            if(i==vout) return(amount,raw[offset:offset+len.value]);
            offset+=len.value;
        }
        revert InvalidTxLength();
    }

    function sumOutputsToScript(bytes calldata raw, bytes memory scriptPubKey) internal pure returns (uint64 total) {
        require(scriptPubKey.length > 0, "empty script");

        uint256 offset = _skipInputs(raw);
        VarInt memory outputCount = _readVarInt(raw, offset);
        offset += outputCount.size;
        bytes32 expectedScriptHash = keccak256(scriptPubKey);

        for (uint256 i = 0; i < outputCount.value; i++) {
            if (offset + 8 > raw.length) revert InvalidTxLength();
            uint64 amount = _readUint64LE(raw, offset);
            offset += 8;

            VarInt memory scriptLength = _readVarInt(raw, offset);
            offset += scriptLength.size;
            if (offset + scriptLength.value > raw.length) revert InvalidTxLength();

            if (_scriptHashAt(raw, offset, scriptLength.value) == expectedScriptHash) {
                total += amount;
            }
            offset += scriptLength.value;
        }
    }

    function doubleSha256(bytes memory data) internal pure returns (bytes32) {
        return sha256(abi.encodePacked(sha256(data)));
    }

    function flipBytes32(bytes32 x) internal pure returns (bytes32) {
        bytes32 y;
        unchecked {
            for (uint i = 0; i < 32; ++i) {
                y |= bytes32(uint256(uint8(x[i])) << (i * 8));
            }
        }
        return y;
    }

    function _hasWitness(bytes calldata raw) private pure returns (bool) {
        return raw.length > 6 && raw[4] == 0x00 && raw[5] == 0x01;
    }

    function _skipInputs(bytes calldata raw) private pure returns (uint256 offset) {
        require(raw.length >= 10, "InvalidTxLength");
        offset = 4;

        if (_hasWitness(raw)) {
            offset += 2;
        }

        VarInt memory vinCount = _readVarInt(raw, offset);
        offset += vinCount.size;

        for (uint256 i = 0; i < vinCount.value; i++) {
            if (offset + 36 > raw.length) revert InvalidTxLength();
            offset += 36;

            VarInt memory scriptLen = _readVarInt(raw, offset);
            offset += scriptLen.size;
            if (offset + scriptLen.value + 4 > raw.length) revert InvalidTxLength();
            offset += scriptLen.value + 4;
        }
    }

    function _scanOutputs(bytes calldata raw) private pure returns (OutputScan memory scan) {
        require(raw.length >= 10, "InvalidTxLength");
        uint256 offset = 4;

        if (_hasWitness(raw)) {
            offset += 2;
        }

        VarInt memory vinCount = _readVarInt(raw, offset);
        offset += vinCount.size;
        scan.inputCount = vinCount.value;

        for (uint256 i = 0; i < vinCount.value; i++) {
            if (offset + 36 > raw.length) revert InvalidTxLength();
            offset += 36;

            VarInt memory scriptLen = _readVarInt(raw, offset);
            offset += scriptLen.size;
            if (offset + scriptLen.value + 4 > raw.length) revert InvalidTxLength();
            offset += scriptLen.value + 4;
        }

        VarInt memory outputCount = _readVarInt(raw, offset);
        offset += outputCount.size;

        for (uint256 i = 0; i < outputCount.value; i++) {
            if (offset + 8 > raw.length) revert InvalidTxLength();
            offset += 8;

            VarInt memory scriptLen = _readVarInt(raw, offset);
            offset += scriptLen.size;
            if (offset + scriptLen.value > raw.length) revert InvalidTxLength();
            offset += scriptLen.value;
        }

        scan.offset = offset;
    }

    function _readUint64LE(bytes calldata raw, uint256 offset) private pure returns (uint64 value) {
        if (offset + 8 > raw.length) revert InvalidTxLength();
        unchecked {
            for (uint256 i = 0; i < 8; i++) {
                value |= uint64(uint256(uint8(raw[offset + i])) << (8 * i));
            }
        }
    }

    function _scriptHashAt(bytes calldata raw, uint256 offset, uint256 length) private pure returns (bytes32) {
        return keccak256(raw[offset:offset + length]);
    }
}
