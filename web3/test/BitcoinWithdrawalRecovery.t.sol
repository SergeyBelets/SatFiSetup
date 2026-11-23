// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {BitcoinWithdrawalRecoveryScenarios} from "./BitcoinWithdrawalRecoveryFixture.sol";
import {IPaymentSPV} from "../contracts/src/federation/BitcoinWithdrawalEscrow.sol";
import {RegtestHeaderRelay} from "../contracts/src/federation/RegtestHeaderRelay.sol";
import {SimpleSPVContract} from "../contracts/src/demo/SimpleSPVContract.sol";
import {BitcoinTxParser} from "../contracts/src/core/BitcoinTxParser.sol";

contract BitcoinWithdrawalRecoveryRelayTest is BitcoinWithdrawalRecoveryScenarios {
    uint256 private constant REGTEST_TARGET =
        0x7fffff0000000000000000000000000000000000000000000000000000000000;
    RegtestHeaderRelay private relay;
    uint32 private timestamp;

    function _initializeSpv() internal override returns (IPaymentSPV) {
        relay = new RegtestHeaderRelay();
        relay.addBlockHeader(
            hex"0100000000000000000000000000000000000000000000000000000000000000000000003ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4adae5494dffff7f2002000000"
        );
        timestamp = 1_400_000_000 - 1 hours;
        return IPaymentSPV(address(relay));
    }

    function _anchor(bytes32 merkleRoot, uint64 confirmations, uint64 minimumHeight)
        internal override returns (bytes32 header)
    {
        uint64 height = relay.getMainchainHeight() + 1;
        if (height < minimumHeight) _advance(minimumHeight - height);
        header = _mine(merkleRoot);
        _advance(confirmations - 1);
    }

    function _advance(uint64 count) internal override {
        for (uint64 i; i < count; ++i) _mine(bytes32(uint256(7)));
    }

    function _removeAnchor(bytes32 blockHash) internal override {
        (SimpleSPVContract.BlockData memory old, bool canonical,,) = relay.getBlockInfo(blockHash);
        assertTrue(canonical);
        bytes32 parent = old.prev;
        uint64 successorHeight = relay.getMainchainHeight() + 1;
        for (uint64 height = old.height; height <= successorHeight; ++height) {
            parent = _mineOn(parent, bytes32(uint256(9)));
        }
        assertFalse(relay.isInMainchain(blockHash));
    }

    function _mine(bytes32 merkleRoot) private returns (bytes32) {
        return _mineOn(relay.getMainchainHead(), merkleRoot);
    }

    function _mineOn(bytes32 parent, bytes32 merkleRoot) private returns (bytes32) {
        bytes memory fields = abi.encodePacked(hex"01000000", parent, merkleRoot,
            _little(++timestamp, 4), hex"ffff7f20");
        for (uint32 i; i < 256; ++i) {
            bytes memory raw = abi.encodePacked(fields, _little(i, 4));
            bytes32 header = _hash(raw);
            if (uint256(BitcoinTxParser.flipBytes32(header)) <= REGTEST_TARGET) {
                relay.addBlockHeader(raw);
                return header;
            }
        }
        revert("fixture PoW search");
    }
}
