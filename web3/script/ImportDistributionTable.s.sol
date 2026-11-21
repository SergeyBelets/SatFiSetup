// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "forge-std/Script.sol";
import "../contracts/src/demo/SimpleMiningPoolWithCalculator.sol";
import "../contracts/src/libs/BTCScriptConstructor.sol";

/**
 * @title ImportDistributionTable
 * @notice Import distribution table with UUID, BTC wallet, ETH wallet, and BTC amounts
 * @dev Used to batch import user mappings for reward distribution
 */
contract ImportDistributionTable is Script {

    // Distribution entry
    struct DistributionEntry {
        string clientUuid;      // Client UUID
        string btcAddress;      // Bitcoin address (will be converted to script)
        address ethAddress;     // Ethereum address
        uint64 amountSat;       // Amount in satoshis
        string notes;           // Optional notes
    }

    /**
     * @notice Import distribution table from the provided data
     * @dev This should be called with the actual pool address
     */
    function run() external {
        require(block.chainid == 31337, "sample import is local only");
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address poolAddress = vm.envAddress("POOL_ADDRESS");

        console.log("\n=== IMPORTING DISTRIBUTION TABLE ===");
        console.log("Pool:", poolAddress);
        console.log("");

        SimpleMiningPoolWithCalculator pool = SimpleMiningPoolWithCalculator(poolAddress);

        // Sample data from user's table
        DistributionEntry[] memory entries = getSampleDistributionData();

        vm.startBroadcast(deployerPrivateKey);

        // Import in batches
        uint256 batchSize = 50; // Adjust based on gas limits
        for (uint256 i = 0; i < entries.length; i += batchSize) {
            uint256 end = i + batchSize > entries.length ? entries.length : i + batchSize;
            importBatch(pool, entries, i, end);
        }

        vm.stopBroadcast();

        console.log("\n=== IMPORT COMPLETE ===");
        console.log("Total entries imported:", entries.length);
    }

    /**
     * @notice Import a batch of distribution entries
     */
    function importBatch(
        SimpleMiningPoolWithCalculator pool,
        DistributionEntry[] memory entries,
        uint256 start,
        uint256 end
    ) internal {
        uint256 count = end - start;
        address[] memory ethAddresses = new address[](count);
        bytes[] memory btcScripts = new bytes[](count);
        string[] memory clientUuids = new string[](count);

        for (uint256 i = 0; i < count; i++) {
            uint256 idx = start + i;
            ethAddresses[i] = entries[idx].ethAddress;
            btcScripts[i] = convertBtcAddressToScript(entries[idx].btcAddress);
            clientUuids[i] = entries[idx].clientUuid;

            console.log("Importing:", entries[idx].ethAddress);
            console.log("  BTC:", entries[idx].btcAddress);
            console.log("  Amount:", entries[idx].amountSat);
            console.log("  UUID:", entries[idx].clientUuid);
        }

        pool.batchMapBtcAddresses(ethAddresses, btcScripts, clientUuids);
    }

    /**
     * @notice Convert Bitcoin address string to script bytes
     * @dev Simplified conversion - real implementation would decode base58/bech32
     */
    function convertBtcAddressToScript(string memory btcAddress) internal pure returns (bytes memory) {
        bytes memory addr = bytes(btcAddress);

        // Check for bc1 (Bech32 - SegWit)
        if (addr.length >= 3 && addr[0] == 0x62 && addr[1] == 0x63 && addr[2] == 0x31) {
            // For bc1q addresses (P2WPKH)
            // In real implementation, decode bech32 to get pubkey hash
            // For now, return a placeholder P2WPKH script
            return hex"0014000000000000000000000000000000000000000000";
        }

        // Check for 1 (P2PKH)
        if (addr[0] == 0x31) {
            // In real implementation, decode base58 to get pubkey hash
            return hex"76a914000000000000000000000000000000000000000088ac";
        }

        // Check for 3 (P2SH)
        if (addr[0] == 0x33) {
            // In real implementation, decode base58 to get script hash
            return hex"a914000000000000000000000000000000000000000087";
        }

        revert("Unsupported Bitcoin address format");
    }

    /**
     * @notice Get sample distribution data
     * @dev Replace this with actual data loading from CSV or JSON
     */
    function getSampleDistributionData() internal pure returns (DistributionEntry[] memory) {
        // Sample data from user's table
        DistributionEntry[] memory entries = new DistributionEntry[](10);

        entries[0] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qnt6c9cjmk4q5kupxrpc9xvhy9tl67mqlxn5pfk",
            ethAddress: 0x997cc4A1c9aF348C50F20d6B368D4a03d47ff9e0,
            amountSat: 106886,
            notes: ""
        });

        entries[1] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qv2x9e5nfwerxmz2lxwktml5vhyty373gjy8s58",
            ethAddress: 0xEd7e9909480cfb482BcdA00018637bB190a9576b,
            amountSat: 66352,
            notes: ""
        });

        entries[2] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1q7p52qmn6ssyd8es00jfl9qrgdlxqdszz5em3wv",
            ethAddress: 0x05DAAAD7C17E0d8742d015ef1Cf8026676f0E5eb,
            amountSat: 137555,
            notes: ""
        });

        entries[3] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qk755nwvr97zwhvdg7cjuls9y2zk0gzyzj668zh",
            ethAddress: 0x8d2aec4a64CdBE2020Ac6775ce5D77a0d0d2061C,
            amountSat: 31944,
            notes: ""
        });

        entries[4] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qzvst3zu2y6tf00j4c9g49umez3ayjcnjgal5q6",
            ethAddress: 0x1D838597BF072CA4e49D9d709647F2e5d284A14c,
            amountSat: 80976,
            notes: ""
        });

        entries[5] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qzpp7fjkf88csul9cxwtulmte7r8cj0jk76ma78",
            ethAddress: 0xe0BedDeE5CD57a159335139e140d6C7410745422,
            amountSat: 128553,
            notes: ""
        });

        entries[6] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1q503arhvu08z5fv5phm77qqhlt5xyly4324tkzg",
            ethAddress: 0xe7811744fA5A6833882d4FA58F327b41341288a3,
            amountSat: 213768,
            notes: ""
        });

        entries[7] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qrwp0z9j4cgznwje7ksaupnymnjem3s4cjt3vf3",
            ethAddress: 0xe90EE29eeaEA33415f6814FAC97c9b94c4fb6d27,
            amountSat: 78334,
            notes: ""
        });

        entries[8] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qj38g6p8tnlgedk8y26yukg0pnl2xn4uqw9j00t",
            ethAddress: 0x71EEae0894aF3266cF4995bF3c32102205978999,
            amountSat: 155485,
            notes: ""
        });

        entries[9] = DistributionEntry({
            clientUuid: "0195706e-ad3d-7f04-82cb-fb78c36ef010",
            btcAddress: "bc1qmeyl77fvr20mqmtxv6r66tvy8fghgdchuqhqnx",
            ethAddress: 0x5a6D81D7632B0986584db465092C7Dd541A269DC,
            amountSat: 147,
            notes: "change"
        });

        return entries;
    }

    /**
     * @notice Load distribution data from CSV file
     * @dev This would be implemented in a real scenario to read from external file
     * @param csvPath Path to CSV file
     */
    function loadFromCSV(string memory csvPath) internal pure returns (DistributionEntry[] memory) {
        // TODO: Implement CSV parsing
        // For now, return sample data
        csvPath; // silence unused variable warning
        return getSampleDistributionData();
    }
}
