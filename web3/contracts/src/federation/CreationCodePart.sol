// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
/// @notice Immutable non-executable creation bytes. Only the canonical factory
/// constructor creates these blobs; runtime cannot replace or write them.
contract CreationCodePart {
    constructor(bytes memory part) {
        require(part.length > 0 && part.length < 24576,"creation part size");bytes memory data=bytes.concat(hex"00",part);
        assembly("memory-safe") { return(add(data,32),mload(data)) }
    }
}
