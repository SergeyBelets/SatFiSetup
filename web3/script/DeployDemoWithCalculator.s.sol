// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "forge-std/Script.sol";
import "../contracts/src/demo/SimpleSPVContract.sol";
import "../contracts/src/demo/SimpleMiningPoolWithCalculator.sol";
import "../contracts/src/demo/ManualPercentageCalculator.sol";
import "../contracts/src/tokens/PoolMpToken.sol";

/**
 * @title DeployDemoWithCalculator
 * @notice Deployment с Manual Percentage Calculator
 */
contract DeployDemoWithCalculator is Script {
    string constant POOL_ID = "SATOHSI-DEMO-CALC-001";
    string constant MP_TOKEN_NAME = "Satohsi Mining Pool Token";
    string constant MP_TOKEN_SYMBOL = "mpBTC";

    SimpleSPVContract public spv;
    SimpleMiningPoolWithCalculator public pool;
    PoolMpToken public mpToken;
    ManualPercentageCalculator public calculator;

    function run() external {
        require(block.chainid == 31337, "local demo only; use V1 bootstrap");
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address deployerAddr = vm.addr(deployerPrivateKey);

        console.log("\n=== SATOHSI DEMO WITH CALCULATOR DEPLOYMENT ===");
        console.log("Deployer:", deployerAddr);
        console.log("Chain ID:", block.chainid);
        console.log("");

        vm.startBroadcast(deployerPrivateKey);

        // 1. Deploy SimpleSPVContract
        console.log("[1/4] Deploying SimpleSPVContract...");
        // Deploy without trusted block (start from genesis)
        spv = new SimpleSPVContract(bytes32(0), 0, bytes32(0), bytes32(0), 0, 0);
        console.log("  SPV:", address(spv));
        console.log("  Confirmations required:", spv.COINBASE_MATURITY());

        // 2. Deploy SimpleMiningPoolWithCalculator
        console.log("\n[2/4] Deploying SimpleMiningPoolWithCalculator...");
        bytes memory payoutScript = hex"76a914000000000000000000000000000000000000000188ac";
        pool = new SimpleMiningPoolWithCalculator(
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
            false
        );
        console.log("  MP Token:", address(mpToken));
        console.log("  Name:", mpToken.name());
        console.log("  Symbol:", mpToken.symbol());

        // 4. Deploy ManualPercentageCalculator
        console.log("\n[4/4] Deploying ManualPercentageCalculator...");
        calculator = new ManualPercentageCalculator(deployerAddr); // deployer = remainder recipient
        console.log("  Calculator:", address(calculator));
        (string memory name, string memory version, string memory description) = calculator.getSchemeInfo();
        console.log("  Name:", name);
        console.log("  Version:", version);

        // Configure pool
        pool.setMpToken(address(mpToken));
        pool.setCalculator(address(calculator));
        console.log("\n  Pool configured with MP token and calculator");
        console.log("  NOTE: PoolMpToken automatically grants MINTER_ROLE and BURNER_ROLE to pool");

        vm.stopBroadcast();

        // Print summary
        console.log("\n=== DEPLOYMENT SUMMARY ===");
        console.log("SimpleSPVContract:              ", address(spv));
        console.log("SimpleMiningPoolWithCalculator: ", address(pool));
        console.log("PoolMpToken:                    ", address(mpToken));
        console.log("ManualPercentageCalculator:     ", address(calculator));
        console.log("");
        console.log("=== USAGE ===");
        console.log("1. Add miners with percentages (basis points, 10000 = 100%):");
        console.log("   Example: Alice 40%, Bob 35%, Carol 25%");
        console.log("   workers[0] = WorkerData(alice, 4000, \"alice\")");
        console.log("   workers[1] = WorkerData(bob, 3500, \"bob\")");
        console.log("   workers[2] = WorkerData(carol, 2500, \"carol\")");
        console.log("");
        console.log("2. Add Bitcoin blocks to SPV (need 6+ confirmations)");
        console.log("");
        console.log("3. Register reward and distribute:");
        console.log("   pool.registerReward(...)");
        console.log("   pool.distributeRewardWithCalculator(utxoKey, workers, params)");
        console.log("");
    }
}
