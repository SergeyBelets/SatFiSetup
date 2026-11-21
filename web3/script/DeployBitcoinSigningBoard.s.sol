// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Script.sol";
import {BitcoinSigningBoard} from "../contracts/src/federation/BitcoinSigningBoard.sol";

contract DeployBitcoinSigningBoard is Script {
    function run() external returns (BitcoinSigningBoard board) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        address registry = vm.envAddress("BITCOIN_BATCH_REGISTRY");
        uint256 maximum = vm.envUint("MAX_PACKET_BYTES");
        require(registry.code.length > 0 && maximum > 0, "board configuration missing");
        uint256 key = vm.envUint("PRIVATE_KEY");
        vm.startBroadcast(key);
        board = new BitcoinSigningBoard(registry, maximum);
        vm.stopBroadcast();
        console.log("BitcoinSigningBoard:", address(board));
        console.log("BitcoinBatchRegistry:", address(board.batchRegistry()));
        console.log("walletId:"); console.logBytes32(board.walletId());
        console.log("walletResult:"); console.logBytes32(board.walletResult());
        console.log("maxPacketBytes:", board.maxPacketBytes());
    }
}
