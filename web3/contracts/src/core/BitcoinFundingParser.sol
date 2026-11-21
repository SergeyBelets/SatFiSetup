// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

/// @notice FUNDING_STRIPPED_100K_V1 transaction structure and outpoint extraction.
/// @dev Accepts complete canonical non-witness serialization only. Callers must
/// separately establish SPV inclusion/canonicality/depth/Fresh, exact deposit
/// binding and custody checks for EVERY returned input. Parsing proves neither
/// script execution nor that the funding output is still unspent. Legacy
/// BitcoinTxParser consumers deliberately retain their existing behavior.
library BitcoinFundingParser {
    uint256 internal constant MAX_STRIPPED_BYTES = 100_000;

    error InvalidFundingLength();
    error NonCanonicalCompactSize();
    error InvalidFundingCount();
    error FundingOutputIndex();

    struct Input {
        // Serialized prevout hash, i.e. reverse of RPC display-order txid.
        bytes32 txidLE;
        uint32 vout;
    }

    struct Parsed {
        // Raw double-SHA256 digest, matching BitcoinTxParser.txId().
        bytes32 txid;
        uint256 outputCount;
        uint64 amount;
        bytes scriptPubKey;
        Input[] inputs;
    }

    /// @notice Walk every input/output once, return all prevouts and one output.
    /// @dev Element counts are bounded by remaining bytes and minimum wire
    /// sizes, never by the old 256-item cap. No allocation depends on an
    /// unchecked CompactSize. Unselected scripts are skipped without copying.
    function parse(bytes calldata raw, uint32 targetVout) internal pure returns (Parsed memory result) {
        if (raw.length < 60 || raw.length > MAX_STRIPPED_BYTES) revert InvalidFundingLength();
        // Reserve locktime before reading any vector. version/locktime are
        // opaque uint32 fields here, not mempool policy or consensus validation.
        uint256 end = raw.length - 4;
        (uint256 inputCount, uint256 offset) = _compact(raw, 4, end);
        // A stripped transaction cannot have marker/flag or an empty vin. An
        // input needs 32+4+1+4 bytes; at least one output needs 1+8+1 bytes.
        if (inputCount == 0 || end - offset < 10 || inputCount > (end - offset - 10) / 41) {
            revert InvalidFundingCount();
        }
        result.inputs = new Input[](inputCount);
        for (uint256 i; i < inputCount; ++i) {
            _bounds(offset, 36, end);
            bytes32 previous;
            assembly ("memory-safe") { previous := calldataload(add(raw.offset, offset)) }
            result.inputs[i] = Input(previous, uint32(_le64(raw, offset + 32)));
            offset += 36;
            uint256 size;
            (size, offset) = _compact(raw, offset, end);
            _bounds(offset, size, end);
            offset += size;
            _bounds(offset, 4, end);
            offset += 4;
        }

        (result.outputCount, offset) = _compact(raw, offset, end);
        if (result.outputCount == 0 || result.outputCount > (end - offset) / 9) {
            revert InvalidFundingCount();
        }
        if (targetVout >= result.outputCount) revert FundingOutputIndex();
        for (uint256 i; i < result.outputCount; ++i) {
            _bounds(offset, 8, end);
            uint64 amount = _le64(raw, offset);
            offset += 8;
            uint256 size;
            (size, offset) = _compact(raw, offset, end);
            _bounds(offset, size, end);
            if (i == targetVout) {
                result.amount = amount;
                result.scriptPubKey = raw[offset:offset + size];
            }
            offset += size;
        }
        if (offset != end) revert InvalidFundingLength();
        result.txid = sha256(abi.encodePacked(sha256(raw)));
    }

    function _bounds(uint256 offset, uint256 size, uint256 end) private pure {
        // Subtraction first also bounds hostile uint64 length declarations.
        if (offset > end || size > end - offset) revert InvalidFundingLength();
    }

    function _compact(bytes calldata raw, uint256 offset, uint256 end)
        private pure returns (uint256 value, uint256 next)
    {
        _bounds(offset, 1, end);
        uint8 prefix = uint8(raw[offset]);
        if (prefix < 253) return (prefix, offset + 1);
        uint256 width = prefix == 253 ? 2 : prefix == 254 ? 4 : 8;
        ++offset;
        _bounds(offset, width, end);
        value = uint256(_le64(raw, offset)) & ((uint256(1) << (width * 8)) - 1);
        if ((width == 2 && value < 253) || (width == 4 && value < 65_536)
            || (width == 8 && value < 4_294_967_296)) revert NonCanonicalCompactSize();
        next = offset + width;
    }

    /// @dev Bounded callers may read fewer than 8 bytes and then mask them.
    /// calldataload zero-padding is never used in place of a bounds check.
    function _le64(bytes calldata raw, uint256 offset) private pure returns (uint64 value) {
        assembly ("memory-safe") {
            let v := shr(192, calldataload(add(raw.offset, offset)))
            v := or(shl(8, and(v, 0x00ff00ff00ff00ff)), and(shr(8, v), 0x00ff00ff00ff00ff))
            v := or(shl(16, and(v, 0x0000ffff0000ffff)), and(shr(16, v), 0x0000ffff0000ffff))
            value := or(shl(32, and(v, 0xffffffff)), shr(32, v))
        }
    }
}
