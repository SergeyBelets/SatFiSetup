// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/**
 * @title BitcoinCommentParser
 * @notice Parses OP_RETURN data from Bitcoin transactions for bridge functionality
 * @dev Extracts Ethereum addresses from Bitcoin transaction comments
 */
library BitcoinCommentParser {
    // ======== CONSTANTS ========

    /// @notice OP_RETURN opcode in Bitcoin
    bytes1 public constant OP_RETURN = 0x6a;

    /// @notice Prefix for Ethereum address in OP_RETURN: "ETH:"
    bytes4 public constant ETH_PREFIX = 0x4554483a; // "ETH:"

    // ======== PARSING FUNCTIONS ========

    /**
     * @notice Parse Ethereum address from Bitcoin OP_RETURN output
     * @param opReturnData Raw OP_RETURN data from Bitcoin transaction
     * @return destinationAddress Ethereum address if found, address(0) otherwise
     * @return hasAddress True if valid Ethereum address was found
     */
    function parseEthereumAddress(bytes calldata opReturnData)
        internal
        pure
        returns (address destinationAddress, bool hasAddress)
    {
        // Minimum length: OP_RETURN (1) + push byte (1) + "ETH:" (4) + address (40 hex chars)
        if (opReturnData.length < 46) {
            return (address(0), false);
        }

        // Check if starts with OP_RETURN
        if (opReturnData[0] != OP_RETURN) {
            return (address(0), false);
        }

        // Skip OP_RETURN and push byte, look for "ETH:" prefix
        uint256 searchStart = 2;
        uint256 searchEnd = opReturnData.length - 40; // Need 40 chars for address

        for (uint256 i = searchStart; i <= searchEnd; i++) {
            // Check for "ETH:" prefix
            if (
                i + 4 <= opReturnData.length &&
                opReturnData[i] == 0x45 &&     // 'E'
                opReturnData[i + 1] == 0x54 && // 'T'
                opReturnData[i + 2] == 0x48 && // 'H'
                opReturnData[i + 3] == 0x3a    // ':'
            ) {
                // Found prefix, try to parse address
                uint256 addrStart = i + 4;
                (address addr, bool valid) = _parseHexAddress(opReturnData, addrStart);
                if (valid) {
                    return (addr, true);
                }
            }
        }

        return (address(0), false);
    }

    /**
     * @notice Parse full OP_RETURN data structure
     * @param opReturnData Raw OP_RETURN data
     * @return ethAddress Ethereum address if found
     * @return hasAddress True if address found
     * @return amount Optional amount in satoshis (0 if not specified)
     * @return rawData Full raw data
     */
    function parseFullOpReturn(bytes calldata opReturnData)
        external
        pure
        returns (
            address ethAddress,
            bool hasAddress,
            uint256 amount,
            bytes memory rawData
        )
    {
        rawData = opReturnData;
        (ethAddress, hasAddress) = parseEthereumAddress(opReturnData);
        amount = 0; // For future extension: parse amount from comment

        return (ethAddress, hasAddress, amount, rawData);
    }

    // ======== INTERNAL HELPERS ========

    /**
     * @notice Parse hexadecimal Ethereum address string
     * @param data Data containing hex address
     * @param startIndex Start position of address (after "ETH:")
     * @return addr Parsed address
     * @return valid True if valid address format
     */
    function _parseHexAddress(bytes calldata data, uint256 startIndex)
        internal
        pure
        returns (address addr, bool valid)
    {
        // Need 40 hex characters (20 bytes)
        if (startIndex + 40 > data.length) {
            return (address(0), false);
        }

        // Check if starts with "0x"
        uint256 hexStart = startIndex;
        if (data[startIndex] == 0x30 && data[startIndex + 1] == 0x78) {
            hexStart = startIndex + 2;
        }

        // Need 40 hex chars
        if (hexStart + 40 > data.length) {
            return (address(0), false);
        }

        // Parse hex string to address
        uint160 result = 0;
        for (uint256 i = 0; i < 40; i++) {
            uint8 digit = _hexCharToUint(data[hexStart + i]);
            if (digit == 255) {
                // Invalid hex character
                return (address(0), false);
            }
            result = result * 16 + digit;
        }

        return (address(result), true);
    }

    /**
     * @notice Convert hex ASCII character to uint
     * @param char ASCII hex character (0-9, a-f, A-F)
     * @return value Numeric value (0-15), or 255 if invalid
     */
    function _hexCharToUint(bytes1 char) internal pure returns (uint8) {
        uint8 c = uint8(char);

        // 0-9
        if (c >= 48 && c <= 57) {
            return c - 48;
        }
        // a-f
        if (c >= 97 && c <= 102) {
            return c - 87;
        }
        // A-F
        if (c >= 65 && c <= 70) {
            return c - 55;
        }

        // Invalid
        return 255;
    }

    /**
     * @notice Validate that data is well-formed OP_RETURN
     * @param opReturnData Raw OP_RETURN data
     * @return valid True if valid OP_RETURN structure
     */
    function isValidOpReturn(bytes calldata opReturnData) internal pure returns (bool) {
        if (opReturnData.length < 2) return false;
        if (opReturnData[0] != OP_RETURN) return false;

        // Second byte should be push operation (length)
        uint8 pushLength = uint8(opReturnData[1]);

        // Check if declared length matches actual data
        return (opReturnData.length >= pushLength + 2);
    }
}
