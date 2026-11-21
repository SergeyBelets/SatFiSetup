// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "../demo/SimpleSPVContract.sol";

/// @notice Bitcoin Core regtest header relay. Explicitly not a mainnet/testnet difficulty relay.
/// Genesis is pinned; test helper entry points are permanently disabled.
contract RegtestHeaderRelay is SimpleSPVContract {
    constructor() SimpleSPVContract(0, 0, 0, 0, 0, 0) {
        require(block.chainid == 1 || block.chainid == 31337 || block.chainid == 11155111, "integration only");
        testHelpersEnabled = false;
    }

    function addBlockHeader(bytes calldata raw) public override {
        (BlockHeaderData memory h, bytes32 hash) = BlockHeader.parseHeader(raw);
        require(h.bits == 0x207fffff, "regtest target");
        if (h.prevBlockHash == 0) {
            require(
                mainchainHead == 0 && hash == hex"06226e46111a0b59caaf126043eb5bbf28c34f3a5e332a1fc7b2b73cf188910f",
                "regtest genesis"
            );
        } else {
            uint32[] memory times = new uint32[](11);
            uint256 n;
            bytes32 cursor = h.prevBlockHash;
            while (cursor != 0 && n < 11) {
                require(blocks[cursor].time != 0, "parent");
                times[n++] = blocks[cursor].time;
                cursor = blocks[cursor].prev;
            }
            for (uint256 i = 1; i < n; i++) {
                uint32 t = times[i];
                uint256 j = i;
                while (j > 0 && times[j - 1] > t) {
                    times[j] = times[j - 1];
                    j--;
                }
                times[j] = t;
            }
            require(h.time > times[n / 2] && h.time <= block.timestamp + 2 hours, "header time");
        }
        super.addBlockHeader(raw);
    }
}
