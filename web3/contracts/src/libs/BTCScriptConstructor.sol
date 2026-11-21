// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "./BIP143.sol";

/**
 * @title BTCScriptConstructor
 * @notice Library for constructing Bitcoin transaction scripts for redemptions
 * @dev Returns unsigned transaction data that only needs to be signed off-chain
 * @dev Updated to use BIP143 for proper SegWit sighash calculation
 */
library BTCScriptConstructor {
    uint32 internal constant REDEMPTION_SIGHASH_TYPE = 0x81; // SIGHASH_ALL | SIGHASH_ANYONECANPAY
    uint32 internal constant MAX_RBF_SEQUENCE = 0xfffffffd;

    /**
     * @notice UTXO input for spending
     */
    struct UTXOInput {
        bytes32 txid;           // Transaction ID (reversed for Bitcoin wire format)
        uint32 vout;            // Output index
        uint64 amount;          // Amount in satoshis
        bytes scriptPubKey;     // Script from the UTXO being spent
        bytes scriptCode;       // Script code for signing (P2WPKH: OP_DUP OP_HASH160 ...)
        uint32 sequence;        // Sequence number (usually 0xfffffffe)
    }

    /**
     * @notice Output for the redemption transaction
     */
    struct TxOutput {
        uint64 amount;          // Amount in satoshis
        bytes scriptPubKey;     // Destination script (P2PKH, P2WPKH, etc.)
    }

    /**
     * @notice Unsigned transaction data
     */
    struct UnsignedTx {
        uint32 version;         // Transaction version (usually 2)
        UTXOInput[] inputs;     // Inputs to spend
        TxOutput[] outputs;     // Outputs to create
        uint32 locktime;        // Locktime (usually 0)
        bytes32[] sighashes;    // Hash to sign for each input (BIP143)
        uint32 sighashType;     // Sighash type signers must append to each Bitcoin signature
    }

    /**
     * @notice Construct an unsigned redemption transaction
     * @param poolUTXOs Array of pool UTXOs to spend
     * @param redemptionOutputs Array of redemption outputs
     * @param changeScript Change address script (if needed)
     * @param minerFeeSat Explicit miner fee to keep outside user payout outputs
     * @return Unsigned transaction data ready for signing
     */
    function constructRedemptionTx(
        UTXOInput[] memory poolUTXOs,
        TxOutput[] memory redemptionOutputs,
        bytes memory changeScript,
        uint64 minerFeeSat
    ) internal pure returns (UnsignedTx memory) {
        require(poolUTXOs.length > 0, "No inputs");
        require(redemptionOutputs.length > 0, "No outputs");
        require(minerFeeSat > 0, "Fee required");

        // Calculate total input and output amounts
        uint64 totalInput = 0;
        for (uint256 i = 0; i < poolUTXOs.length; i++) {
            require(poolUTXOs[i].sequence <= MAX_RBF_SEQUENCE, "RBF disabled");
            totalInput += poolUTXOs[i].amount;
        }

        uint64 totalOutput = 0;
        for (uint256 i = 0; i < redemptionOutputs.length; i++) {
            totalOutput += redemptionOutputs[i].amount;
        }

        // Calculate change after explicit fee. User payouts are not reduced by miner fee.
        require(totalInput >= totalOutput + minerFeeSat, "Insufficient input");
        uint64 change = totalInput - totalOutput - minerFeeSat;

        // Add change output if needed (assume 1000 sat minimum for change)
        TxOutput[] memory finalOutputs;
        if (change > 1000 && changeScript.length > 0) {
            finalOutputs = new TxOutput[](redemptionOutputs.length + 1);
            for (uint256 i = 0; i < redemptionOutputs.length; i++) {
                finalOutputs[i] = redemptionOutputs[i];
            }
            finalOutputs[redemptionOutputs.length] = TxOutput({
                amount: change,
                scriptPubKey: changeScript
            });
        } else {
            finalOutputs = redemptionOutputs;
        }

        // Calculate sighash for each input using BIP143.
        // ANYONECANPAY lets operators add an external fee input without changing these signatures.
        bytes32[] memory sighashes = calculateSighashes(poolUTXOs, finalOutputs);

        return UnsignedTx({
            version: 2,
            inputs: poolUTXOs,
            outputs: finalOutputs,
            locktime: 0,
            sighashes: sighashes,
            sighashType: REDEMPTION_SIGHASH_TYPE
        });
    }

    /**
     * @notice Calculate SIGHASH for each input using BIP143
     * @dev Uses proper BIP143 sighash calculation for SegWit transactions
     */
    function calculateSighashes(
        UTXOInput[] memory inputs,
        TxOutput[] memory outputs
    ) internal pure returns (bytes32[] memory) {
        // Convert to BIP143 format
        BIP143.TxInput[] memory bip143Inputs = new BIP143.TxInput[](inputs.length);
        for (uint256 i = 0; i < inputs.length; i++) {
            bip143Inputs[i] = BIP143.TxInput({
                prevTxid: inputs[i].txid,
                prevVout: inputs[i].vout,
                amount: inputs[i].amount,
                scriptCode: inputs[i].scriptCode,
                sequence: inputs[i].sequence
            });
        }

        BIP143.TxOutput[] memory bip143Outputs = new BIP143.TxOutput[](outputs.length);
        for (uint256 i = 0; i < outputs.length; i++) {
            bip143Outputs[i] = BIP143.TxOutput({
                value: outputs[i].amount,
                scriptPubKey: outputs[i].scriptPubKey
            });
        }

        // Calculate sighash for each input
        bytes32[] memory sighashes = new bytes32[](inputs.length);
        for (uint256 i = 0; i < inputs.length; i++) {
            sighashes[i] = BIP143.calculateSighash(
                2,              // version
                bip143Inputs,
                bip143Outputs,
                i,              // inputIndex
                0,              // locktime
                REDEMPTION_SIGHASH_TYPE
            );
        }

        return sighashes;
    }

    /**
     * @notice Encode unsigned transaction to hex string for off-chain signing
     * @dev Returns transaction in a format ready for signing with Bitcoin tools
     */
    function encodeUnsignedTx(UnsignedTx memory unsignedTx) internal pure returns (bytes memory) {
        bytes memory result = abi.encodePacked(unsignedTx.version);

        // Inputs count
        result = abi.encodePacked(result, uint8(unsignedTx.inputs.length));

        // Inputs (with placeholder for signatures)
        for (uint256 i = 0; i < unsignedTx.inputs.length; i++) {
            result = abi.encodePacked(
                result,
                unsignedTx.inputs[i].txid,
                unsignedTx.inputs[i].vout,
                uint8(0), // scriptSig length (empty, to be filled after signing)
                unsignedTx.inputs[i].sequence
            );
        }

        // Outputs count
        result = abi.encodePacked(result, uint8(unsignedTx.outputs.length));

        // Outputs
        for (uint256 i = 0; i < unsignedTx.outputs.length; i++) {
            result = abi.encodePacked(
                result,
                unsignedTx.outputs[i].amount,
                uint8(unsignedTx.outputs[i].scriptPubKey.length),
                unsignedTx.outputs[i].scriptPubKey
            );
        }

        // Locktime
        result = abi.encodePacked(result, unsignedTx.locktime);

        return result;
    }

    /**
     * @notice Create a P2PKH script (Pay-to-PubKey-Hash)
     * @param pubKeyHash 20-byte hash of the public key
     */
    function createP2PKHScript(bytes20 pubKeyHash) internal pure returns (bytes memory) {
        return abi.encodePacked(
            hex"76a914", // OP_DUP OP_HASH160
            pubKeyHash,
            hex"88ac"    // OP_EQUALVERIFY OP_CHECKSIG
        );
    }

    /**
     * @notice Create a P2WPKH script (Pay-to-Witness-PubKey-Hash)
     * @param pubKeyHash 20-byte hash of the public key
     */
    function createP2WPKHScript(bytes20 pubKeyHash) internal pure returns (bytes memory) {
        return abi.encodePacked(
            hex"0014", // OP_0 + 20 bytes
            pubKeyHash
        );
    }

    /**
     * @notice Parse Bitcoin address to script
     * @dev Simplified - supports basic address formats
     * @param btcAddress Bitcoin address as string
     */
    function parseBitcoinAddress(string memory btcAddress) internal pure returns (bytes memory) {
        bytes memory addr = bytes(btcAddress);

        // Check prefix for address type
        if (addr.length > 3 && addr[0] == 0x62 && addr[1] == 0x63 && addr[2] == 0x31) { // "bc1" - Bech32 (SegWit)
            // For demo - return placeholder
            // Real implementation would decode bech32 using Bech32Decoder
            return hex"0014"; // P2WPKH prefix
        } else if (addr.length > 0 && addr[0] == 0x31) { // "1" - P2PKH
            // Real implementation would decode base58
            return hex"76a91488ac"; // P2PKH template
        } else if (addr.length > 0 && addr[0] == 0x33) { // "3" - P2SH
            // Real implementation would decode base58
            return hex"a91487"; // P2SH template
        }

        revert("Unsupported address format");
    }
}
