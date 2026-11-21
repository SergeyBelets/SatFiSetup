// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAOClaimAdapter} from "../contracts/src/federation/CustodianDAOClaimAdapter.sol";
import {CustodianDAOSilo4626Adapter} from "../contracts/src/federation/CustodianDAOSilo4626Adapter.sol";
import {CustodianDAOConversionAdapter} from "../contracts/src/federation/CustodianDAOConversionAdapter.sol";

contract DeployCustodianDAOIncomeAdapter is Script {
    function run() external returns (address adapter) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        CustodianDAOIncomeVault vault = CustodianDAOIncomeVault(vm.envAddress("DAO_INCOME_VAULT"));
        require(address(vault).code.length > 0, "income vault missing");

        string memory kind = vm.envString("DAO_ADAPTER_KIND");
        bytes32 kindHash = keccak256(bytes(kind));
        bytes memory sourceName = bytes(vm.envString("DAO_SOURCE_NAME"));
        require(sourceName.length > 0, "source name missing");
        bytes32 sourceId = keccak256(sourceName);
        uint256 streamValue = vm.envUint("DAO_SOURCE_STREAM");
        require(streamValue <= uint256(CustodianDAOIncomeVault.Stream.UnwrapFee), "invalid source stream");
        CustodianDAOIncomeVault.Stream stream = CustodianDAOIncomeVault.Stream(streamValue);
        address guard = vm.envOr("DAO_LIQUIDITY_GUARD", address(0));
        uint256 minimumLiquidity = vm.envOr("DAO_MINIMUM_LIQUIDITY", uint256(0));
        require(
            (guard == address(0) && minimumLiquidity == 0) || (guard.code.length > 0 && minimumLiquidity > 0),
            "invalid liquidity guard"
        );
        if (sourceId == vault.CURVE()) {
            require(stream == CustodianDAOIncomeVault.Stream.Yield && guard != address(0), "invalid Curve source");
        }
        if (sourceId == vault.WRAP_FEE()) {
            require(stream == CustodianDAOIncomeVault.Stream.WrapFee, "invalid wrap stream");
        }
        if (sourceId == vault.UNWRAP_FEE()) {
            require(stream == CustodianDAOIncomeVault.Stream.UnwrapFee, "invalid unwrap stream");
        }
        if (sourceId == vault.DEPLOY_FEE()) {
            require(stream == CustodianDAOIncomeVault.Stream.DeployFee, "invalid deploy stream");
        }

        vm.startBroadcast(deployerKey);
        if (kindHash == keccak256("CLAIM")) {
            adapter =
                address(new CustodianDAOClaimAdapter(address(vault), vm.envAddress("DAO_CLAIM_PROTOCOL"), sourceId));
        } else if (kindHash == keccak256("SILO_ERC4626")) {
            require(
                sourceId == keccak256("SILO") && stream == CustodianDAOIncomeVault.Stream.Yield, "invalid Silo source"
            );
            adapter = address(new CustodianDAOSilo4626Adapter(address(vault), vm.envAddress("DAO_SILO_VAULT")));
        } else if (kindHash == keccak256("CONVERSION")) {
            adapter = address(
                new CustodianDAOConversionAdapter(
                    address(vault), vm.envAddress("DAO_INPUT_TOKEN"), vm.envAddress("DAO_CONVERTER"), sourceId
                )
            );
        } else {
            revert("invalid adapter kind");
        }
        vm.stopBroadcast();

        bytes32 sourceOperationHash =
            vault.getSetSourceOperationHash(sourceId, adapter, stream, true, guard, minimumLiquidity);
        console.log("Income adapter:", adapter);
        console.log("FROST setSource operation hash:");
        console.logBytes32(sourceOperationHash);
    }
}
