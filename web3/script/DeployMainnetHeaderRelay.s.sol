// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Script.sol";
import {MainnetHeaderRelay} from "../contracts/src/federation/MainnetHeaderRelay.sol";
import {Testnet4HeaderRelay} from "../contracts/src/federation/Testnet4HeaderRelay.sol";
import {MainnetSPVGuard} from "../contracts/src/federation/MainnetSPVGuard.sol";

/// @notice Mainnet Bitcoin trust anchor is independent of the selected EVM network.
contract DeployMainnetHeaderRelay is Script {
    MainnetSPVGuard public guard;

    function run() external returns (MainnetHeaderRelay relay) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        bytes memory raw = vm.envBytes("BTC_CHECKPOINT_HEADER");
        uint256 height = vm.envUint("BTC_CHECKPOINT_HEIGHT");
        uint256 chainWork = vm.envUint("BTC_CHECKPOINT_CHAINWORK");
        bytes[] memory prior = vm.envBytes("BTC_CHECKPOINT_PREDECESSORS", ",");
        uint256 reorg = vm.envUint("MAX_REORG_DEPTH");
        uint256 tipAge = vm.envUint("MAX_TIP_AGE_SECONDS");
        require(raw.length == 80 && height >= 10 && height % 2016 == 0 && height <= type(uint64).max, "checkpoint configuration");
        require(prior.length == 10 && reorg > 0 && reorg <= 144, "checkpoint ancestors or reorg limit");
        require(tipAge > 0 && tipAge <= 1 days, "tip age configuration");
        for (uint256 i; i < 10; ++i) require(prior[i].length == 80, "ancestor length");
        string memory network = vm.envOr("BITCOIN_NETWORK", string("mainnet"));
        bool testnet4 = keccak256(bytes(network)) == keccak256("testnet4");
        require(testnet4 || keccak256(bytes(network)) == keccak256("mainnet"), "public Bitcoin network required");
        uint256 key = vm.envUint("PRIVATE_KEY");
        vm.startBroadcast(key);
        relay = testnet4
            ? MainnetHeaderRelay(address(new Testnet4HeaderRelay(raw, uint64(height), chainWork, prior, uint64(reorg))))
            : new MainnetHeaderRelay(raw, uint64(height), chainWork, prior, uint64(reorg));
        guard = new MainnetSPVGuard(address(relay), uint64(tipAge));
        vm.stopBroadcast();
        console.log("Bitcoin network:", network);
        console.log("Header relay:", address(relay));
        console.log("MainnetSPVGuard (consumer SPV address):", address(guard));
        console.log("Checkpoint height:", height); console.log("Max reorg depth:", reorg);
        console.log("Checkpoint hash:"); console.logBytes32(relay.getMainchainHead());
    }
}
