// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "forge-std/Script.sol";
import "../contracts/src/demo/SimpleSPVContract.sol";
import "../contracts/src/demo/SimpleMiningPool.sol";
import "../contracts/src/demo/SimpleDistributionTable.sol";
import "../contracts/src/tokens/PoolMpToken.sol";

/**
 * @title DeployDemo
 * @notice Single deployment script for minimal demo configuration
 * @dev Deploy: forge script script/DeployDemo.s.sol:DeployDemo --rpc-url $RPC_URL --broadcast
 */
contract DeployDemo is Script {
    // Demo configuration
    string constant POOL_ID = "SATOHSI-DEMO-001";
    string constant MP_TOKEN_NAME = "Satohsi Mining Pool Token";
    string constant MP_TOKEN_SYMBOL = "mpBTC";

    // Deployed contracts
    SimpleSPVContract public spv;
    SimpleMiningPool public pool;
    SimpleDistributionTable public distributionTable;
    PoolMpToken public mpToken;

    function run() external {
        require(block.chainid == 31337, "local demo only; use V1 bootstrap");
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address deployerAddr = vm.addr(deployerPrivateKey);

        console.log("\n=== SATOHSI DEMO DEPLOYMENT ===");
        console.log("Deployer:", deployerAddr);
        console.log("Chain ID:", block.chainid);
        console.log("");

        vm.startBroadcast(deployerPrivateKey);

        // 1. Deploy SimpleSPVContract (6 confirmations)
        console.log("[1/4] Deploying SimpleSPVContract...");
        // Deploy without trusted block (start from genesis)
        spv = new SimpleSPVContract(bytes32(0), 0, bytes32(0), bytes32(0), 0, 0);
        console.log("  SPV:", address(spv));
        console.log("  Confirmations required:", spv.COINBASE_MATURITY());

        // 2. Deploy SimpleMiningPool
        console.log("\n[2/4] Deploying SimpleMiningPool...");
        bytes memory payoutScript = hex"76a914000000000000000000000000000000000000000188ac"; // Example P2PKH script
        pool = new SimpleMiningPool(
            address(spv),
            POOL_ID,
            payoutScript,
            0 // Use default confirmation depth
        );
        console.log("  Pool:", address(pool));
        console.log("  Pool ID:", pool.poolId());
        spv.setPoolContract(address(pool));
        console.log("  Pool registered in SPV");

        // 3. Deploy PoolMpToken
        console.log("\n[3/4] Deploying PoolMpToken...");
        mpToken = new PoolMpToken(
            MP_TOKEN_NAME,
            MP_TOKEN_SYMBOL,
            address(pool),
            false // restrictTransfer = false (open transfers)
        );
        console.log("  MP Token:", address(mpToken));
        console.log("  Name:", mpToken.name());
        console.log("  Symbol:", mpToken.symbol());

        // 4. Deploy SimpleDistributionTable
        console.log("\n[4/4] Deploying SimpleDistributionTable...");
        distributionTable = new SimpleDistributionTable();
        console.log("  Distribution Table:", address(distributionTable));

        // Configure pool with MP token
        pool.setMpToken(address(mpToken));
        console.log("\n  Pool configured with MP token");
        console.log("  NOTE: PoolMpToken automatically grants MINTER_ROLE and BURNER_ROLE to pool in constructor");

        vm.stopBroadcast();

        // Print summary
        console.log("\n=== DEPLOYMENT SUMMARY ===");
        console.log("SimpleSPVContract:        ", address(spv));
        console.log("SimpleMiningPool:         ", address(pool));
        console.log("PoolMpToken:              ", address(mpToken));
        console.log("SimpleDistributionTable:  ", address(distributionTable));
        console.log("\n=== NEXT STEPS ===");
        console.log("1. Add miners to distribution table:");
        console.log("   distributionTable.addMiner(minerAddress, shares, minerId)");
        console.log("\n2. Add Bitcoin block headers to SPV:");
        console.log("   spv.addBlockHeader(headerBytes)");
        console.log("\n3. Register rewards:");
        console.log("   pool.registerReward(txid, vout, amount, blockHash, siblings, index)");
        console.log("\n4. Distribute rewards:");
        console.log("   (recipients, amounts) = distributionTable.calculateDistribution(totalAmount)");
        console.log("   pool.distributeReward(utxoKey, recipients, amounts)");
        console.log("\n5. Redeem MP tokens to BTC:");
        console.log("   pool.requestRedemption(amount, btcScript)");
        console.log("");
    }
}
