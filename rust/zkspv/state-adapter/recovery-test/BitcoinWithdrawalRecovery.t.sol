// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {BitcoinWithdrawalRecoveryScenarios} from "satoshifi-project/test/BitcoinWithdrawalRecoveryFixture.sol";
import {IPaymentSPV} from "satoshifi-project/contracts/src/federation/BitcoinWithdrawalEscrow.sol";
import {BitcoinTransitionV2} from "satoshifi-core/BitcoinTransitionV2.sol";
import {ZkSpvAdapter} from "../contracts/ZkSpvAdapter.sol";
import {ZkTestnet4SpvAdapter} from "../contracts/ZkTestnet4SpvAdapter.sol";

interface IRecoveryZkAdapter {
    function materializeHeader(uint64 height, bytes calldata raw, bytes32[] calldata siblings)
        external returns (bytes32);
}

/// @dev Synthetic proved-state boundary. The actual adapters verify canonical
/// membership and Bitcoin transaction inclusion without a mocked adapter call.
/// This fixture does not produce or verify a Groth16 seal.
contract RecoveryZkStateFixture {
    IRecoveryZkAdapter public immutable adapter;
    bytes32 private immutable scope;
    BitcoinTransitionV2.State private accepted;
    mapping(uint8 => mapping(uint64 => bytes32)) private nodes;
    bytes[] private headers;
    uint64[] private heights;

    constructor(bool testnet4) {
        accepted.anchor = keccak256("recovery test anchor");
        bytes[] memory parts = new bytes[](3);
        parts[0] = testnet4 ? bytes("bitcoin-testnet4") : bytes("bitcoin-mainnet");
        parts[1] = abi.encodePacked(accepted.anchor);
        parts[2] = abi.encodePacked(accepted.anchorHeight);
        scope = _historyHash(testnet4 ? bytes("testnet4-anchor") : bytes("mainnet-anchor"), parts);
        adapter = testnet4 ? IRecoveryZkAdapter(address(new ZkTestnet4SpvAdapter(address(this))))
            : IRecoveryZkAdapter(address(new ZkSpvAdapter(address(this))));
    }

    function getState() external view returns (BitcoinTransitionV2.State memory) { return accepted; }
    function isFresh() external pure returns (bool) { return true; }
    function stateVersion() external view returns (uint64) { return accepted.height; }

    function include(bytes32 merkleRoot, uint64 confirmations, uint64 minimumHeight)
        external returns (bytes32 hash)
    {
        uint64 height = accepted.height + 1;
        if (height < minimumHeight) height = minimumHeight;
        bytes memory raw = abi.encodePacked(hex"01000000", accepted.head, merkleRoot, hex"01000000ffff7f2000000000");
        hash = sha256(abi.encodePacked(sha256(raw)));

        bytes[] memory parts = new bytes[](5);
        parts[0] = abi.encodePacked(scope);
        parts[1] = abi.encodePacked(height);
        parts[2] = abi.encodePacked(hash);
        parts[3] = abi.encodePacked(merkleRoot);
        parts[4] = abi.encodePacked(uint32(1));
        _insert(height, _historyHash(bytes("canonical-header"), parts));
        heights.push(height);
        headers.push(raw);
        _progress(height + confirmations - 1);
    }

    function advance(uint64 count) external {
        _progress(accepted.height + count);
    }

    function exclude(bytes32 blockHash) external {
        for (uint256 index; index < headers.length; ++index) {
            if (sha256(abi.encodePacked(sha256(headers[index]))) != blockHash) continue;
            _insert(heights[index], bytes32(0));
            uint256 last = headers.length - 1;
            headers[index] = headers[last];
            heights[index] = heights[last];
            headers.pop();
            heights.pop();
            _progress(accepted.height + 1);
            return;
        }
        revert("missing fixture block");
    }

    function _insert(uint64 position, bytes32 leaf) private {
        nodes[0][position] = leaf;
        bytes32 node = leaf;
        for (uint8 level; level < 64; ++level) {
            bytes[] memory parts = new bytes[](3);
            parts[0] = abi.encodePacked(bytes1(level + 1));
            bytes32 neighbor = nodes[level][position ^ 1];
            bool right = (position & 1) == 1;
            parts[1] = abi.encodePacked(right ? neighbor : node);
            parts[2] = abi.encodePacked(right ? node : neighbor);
            position >>= 1;
            node = _historyHash(bytes("history-node"), parts);
            nodes[level + 1][position] = node;
        }
    }

    function _progress(uint64 bitcoinHeight) private {
        accepted.height = bitcoinHeight;
        accepted.head = sha256(abi.encodePacked("test head", bitcoinHeight));
        bytes[] memory parts = new bytes[](4);
        parts[0] = abi.encodePacked(scope);
        parts[1] = abi.encodePacked(accepted.head);
        parts[2] = abi.encodePacked(bitcoinHeight);
        parts[3] = abi.encodePacked(nodes[64][0]);
        accepted.canonicalRoot = _historyHash(bytes("canonical-root"), parts);
        for (uint256 index; index < headers.length; ++index) {
            bytes32[] memory siblings = new bytes32[](64);
            uint64 position = heights[index];
            for (uint8 level; level < 64; ++level) {
                siblings[level] = nodes[level][position ^ 1];
                position >>= 1;
            }
            adapter.materializeHeader(heights[index], headers[index], siblings);
        }
    }

    function _historyHash(bytes memory domain, bytes[] memory parts) private pure returns (bytes32) {
        bytes memory encoded = abi.encodePacked("SATOSHIFI_FORK_V1\x00", uint32(domain.length), domain);
        for (uint256 i; i < parts.length; ++i) {
            encoded = bytes.concat(encoded, abi.encodePacked(uint64(parts[i].length), parts[i]));
        }
        return sha256(encoded);
    }
}

abstract contract BitcoinWithdrawalRecoveryZkScenarios is BitcoinWithdrawalRecoveryScenarios {
    RecoveryZkStateFixture private state;

    function _testnet4() internal pure virtual returns (bool);

    function _initializeSpv() internal override returns (IPaymentSPV) {
        state = new RecoveryZkStateFixture(_testnet4());
        return IPaymentSPV(address(state.adapter()));
    }

    function _anchor(bytes32 merkleRoot, uint64 confirmations, uint64 minimumHeight)
        internal override returns (bytes32)
    {
        return state.include(merkleRoot, confirmations, minimumHeight);
    }

    function _advance(uint64 count) internal override {
        state.advance(count);
    }

    function _removeAnchor(bytes32 blockHash) internal override {
        state.exclude(blockHash);
    }
}

contract BitcoinWithdrawalRecoveryMainnetZkAdapterTest is BitcoinWithdrawalRecoveryZkScenarios {
    function _testnet4() internal pure override returns (bool) { return false; }
}

contract BitcoinWithdrawalRecoveryTestnet4ZkAdapterTest is BitcoinWithdrawalRecoveryZkScenarios {
    function _testnet4() internal pure override returns (bool) { return true; }
}
