// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetHeaderRelay.sol";

/// @notice Freshness gate for permissionless mainnet deposit and payment proofs.
/// @dev A keeper must continue submitting headers. Stale or halted relays fail closed;
/// this does not establish that the relay has received the heaviest network chain.
contract MainnetSPVGuard {
    MainnetHeaderRelay public immutable relay;
    uint64 public immutable maxTipAgeSeconds;

    error InvalidRelay();
    error InvalidTipAge();

    constructor(address relayAddress, uint64 tipAgeSeconds) {
        if (relayAddress.code.length == 0) revert InvalidRelay();
        if (tipAgeSeconds == 0 || tipAgeSeconds > 1 days) revert InvalidTipAge();
        MainnetHeaderRelay underlying = MainnetHeaderRelay(relayAddress);
        if (underlying.testHelpersEnabled()
            || underlying.POW_LIMIT() != 0x00000000ffffffffffffffffffffffffffffffffffffffffffffffffffffffff) {
            revert InvalidRelay();
        }
        relay = underlying;
        maxTipAgeSeconds = tipAgeSeconds;
    }

    function testHelpersEnabled() external pure returns (bool) {
        return false;
    }

    function isFresh() public view returns (bool) {
        if (relay.halted()) return false;
        (,,,, uint32 timestamp,) = relay.blocks(relay.getMainchainHead());
        return block.timestamp <= uint256(timestamp) + maxTipAgeSeconds;
    }

    function getBlockStatus(bytes32 blockHash) external view returns (bool, uint64) {
        if (!isFresh()) return (false, 0);
        return relay.getBlockStatus(blockHash);
    }

    function checkTxInclusion(bytes32 blockHash, bytes32 txid, bytes32[] calldata siblings, uint256 index)
        external view returns (bool)
    {
        if (!isFresh()) return false;
        return relay.checkTxInclusion(blockHash, txid, siblings, index);
    }
}
