// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "../libs/BTCScriptConstructor.sol";
import "../core/BitcoinTxParser.sol";

/**
 * @title BitcoinRedemptionTxBuilder
 * @notice Stateless BTC transaction construction helper for calculator-enabled pools.
 * @dev Keeps BIP143 and transaction encoding bytecode outside the pool runtime.
 */
contract BitcoinRedemptionTxBuilder {
    function constructRedemptionTx(
        BTCScriptConstructor.UTXOInput[] calldata poolUTXOs,
        BTCScriptConstructor.TxOutput[] calldata redemptionOutputs,
        bytes calldata changeScript,
        uint64 minerFeeSat
    ) external pure returns (BTCScriptConstructor.UnsignedTx memory unsignedTx, bytes memory encodedTx) {
        unsignedTx = BTCScriptConstructor.constructRedemptionTx(poolUTXOs, redemptionOutputs, changeScript, minerFeeSat);
        encodedTx = BTCScriptConstructor.encodeUnsignedTx(unsignedTx);
    }

    function txIdMatches(bytes calldata rawTx, bytes32 expectedTxid) external pure returns (bool) {
        bytes32 rawTxid = BitcoinTxParser.txId(rawTx);
        return rawTxid == expectedTxid || BitcoinTxParser.flipBytes32(rawTxid) == expectedTxid;
    }

    function sumOutputsToScript(bytes calldata rawTx, bytes calldata scriptPubKey) external pure returns (uint64) {
        return BitcoinTxParser.sumOutputsToScript(rawTx, scriptPubKey);
    }

    function parseAllInputsLE(bytes calldata rawTx)
        external
        pure
        returns (bytes32[] memory prevTxIds, uint32[] memory vouts)
    {
        (prevTxIds, vouts) = BitcoinTxParser.parseAllInputs(rawTx);
        for (uint256 i = 0; i < prevTxIds.length; i++) {
            prevTxIds[i] = BitcoinTxParser.flipBytes32(prevTxIds[i]);
        }
    }
}
