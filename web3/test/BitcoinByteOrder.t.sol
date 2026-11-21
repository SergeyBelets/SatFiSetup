// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/core/BitcoinTxParser.sol";

contract BitcoinByteOrderTest is Test {
    function testRegtestGenesisWireHashReversesForProofOfWork() public pure {
        bytes32 wire = hex"06226e46111a0b59caaf126043eb5bbf28c34f3a5e332a1fc7b2b73cf188910f";
        bytes32 displayHash = hex"0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206";
        assertEq(BitcoinTxParser.flipBytes32(wire), displayHash);
        assertEq(BitcoinTxParser.flipBytes32(displayHash), wire);
    }

    function testFuzzReverseIsInvolution(bytes32 value) public pure {
        bytes32 reversed = BitcoinTxParser.flipBytes32(value);
        for (uint256 i; i < 32; i++) {
            assertEq(uint8(reversed[i]), uint8(value[31 - i]));
        }
        assertEq(BitcoinTxParser.flipBytes32(reversed), value);
    }
}
