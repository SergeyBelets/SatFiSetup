// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "../core/BitcoinTxParser.sol";
import "./BitcoinBatchRegistry.sol";

interface IWithdrawalMigrationProof {
    function checkMigration(bytes32 txid) external view;
}

interface IWithdrawalInputValues {
    struct OutputValue { uint64 sats; bytes script; bool known; }
    function getValue(bytes32 key) external view returns (OutputValue memory);
    function backingSats(bytes32 key) external view returns (uint256);
    function scriptPools(bytes32 scriptHash) external view returns (address);
    function recordSpendWithChange(bytes calldata raw, uint256 burnedSats, uint32 changeOutputs) external;
}

/// @dev Created by the batch registry constructor. The standalone code keeps the proof and
/// accounting authority at the escrow while moving stateless validation off its runtime.
contract BitcoinWithdrawalFeeVerifier {
    struct Payout {
        bytes script;
        uint256 gross;
        uint64 limit;
        uint64 minimum;
    }

    error InvalidFeePlan();

    function feeForInputs(BitcoinBatchRegistry batches, IWithdrawalInputValues registry,
        bytes32 batch, bytes calldata raw, bytes32 epochScript) external view returns (uint256)
    {
        if (raw.length > 100000) revert InvalidFeePlan();
        (bytes32[] memory prev, uint32[] memory vouts) = BitcoinTxParser.parseAllInputs(raw);
        bytes32[] memory reserved = batches.getInputs(batch);
        if (prev.length != reserved.length) revert InvalidFeePlan();
        address recovery = batches.recoveryController();
        uint256 totalInputs;
        for (uint256 i; i < prev.length; ++i) {
            bytes32 key = keccak256(abi.encode(prev[i], vouts[i]));
            IWithdrawalInputValues.OutputValue memory value = registry.getValue(key);
            bytes32 script = keccak256(value.script);
            if (keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[i]), vouts[i])) != reserved[i]
                || !value.known || value.sats == 0 || registry.backingSats(key) != value.sats
                || registry.scriptPools(script) == address(0) || (epochScript != 0 && script != epochScript)) {
                revert InvalidFeePlan();
            }
            if (recovery != address(0)) IWithdrawalMigrationProof(recovery).checkMigration(prev[i]);
            totalInputs += value.sats;
        }
        uint256 outputs = BitcoinTxParser.outputCount(raw);
        if (outputs == 0 || outputs > 32) revert InvalidFeePlan();
        uint256 totalOutputs;
        for (uint256 i; i < outputs; ++i) {
            (uint64 amount,) = BitcoinTxParser.outputAt(raw, i);
            totalOutputs += amount;
        }
        if (totalOutputs > totalInputs) revert InvalidFeePlan();
        return totalInputs - totalOutputs;
    }

    function checkNetPayments(IWithdrawalInputValues registry, bytes calldata raw, Payout[] calldata payouts,
        uint256 fee, bool replacement, bytes32 epochScript) external view returns (uint32 changeOutputs)
    {
        uint256 count = payouts.length;
        uint256[] memory net = new uint256[](count);
        bytes32[] memory destinations = new bytes32[](count);
        for (uint256 i; i < count; ++i) {
            Payout calldata p = payouts[i];
            destinations[i] = keccak256(p.script);
            if (!replacement && registry.scriptPools(destinations[i]) != address(0)) revert InvalidFeePlan();
            uint256 share = fee / count + (i < fee % count ? 1 : 0);
            if (share > p.limit || share >= p.gross) revert InvalidFeePlan();
            net[i] = p.gross - share;
            if (net[i] < p.minimum) revert InvalidFeePlan();
        }
        for (uint256 i; i < count; ++i) {
            uint256 due;
            for (uint256 j; j < count; ++j) {
                if (destinations[j] == destinations[i]) due += net[j];
            }
            if (BitcoinTxParser.sumOutputsToScript(raw, payouts[i].script) != due) revert InvalidFeePlan();
        }
        uint256 outputCount = BitcoinTxParser.outputCount(raw);
        for (uint256 i; i < outputCount; ++i) {
            (uint64 amount, bytes memory script) = BitcoinTxParser.outputAt(raw, i);
            if (amount == 0) continue;
            bytes32 scriptHash = keccak256(script);
            bool payout;
            for (uint256 j; j < count; ++j) {
                if (destinations[j] == scriptHash) {
                    payout = true;
                    break;
                }
            }
            // Replacements retain original destinations even after a script registration.
            if (payout) continue;
            if (registry.scriptPools(scriptHash) == address(0) || (epochScript != 0 && scriptHash != epochScript)) {
                revert InvalidFeePlan();
            }
            changeOutputs |= uint32(1) << uint32(i);
        }
    }
}
