// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {ULRStaking} from "../contracts/src/staking/ULRStaking.sol";

/// @notice Deploys a common staking contract against an existing, reviewed ULR token.
contract DeployULRStaking is Script {
    function run() external returns (ULRStaking staking) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        address ulr = vm.envAddress("ULR_TOKEN");
        address usd = vm.envAddress("ULR_REWARD_TOKEN");
        require(ulr.code.length > 0 && usd.code.length > 0 && ulr != usd, "invalid tokens");
        require(ulr.codehash == vm.envBytes32("ULR_TOKEN_CODE_HASH"), "ULR code hash");
        require(usd.codehash == vm.envBytes32("ULR_REWARD_TOKEN_CODE_HASH"), "reward code hash");
        require(IERC20Metadata(ulr).decimals() == 18, "ULR decimals");
        require(IERC20Metadata(ulr).totalSupply() == 10_000_000 ether, "ULR supply");
        require(IERC20Metadata(usd).decimals() <= 18, "reward decimals");

        vm.startBroadcast(vm.envUint("PRIVATE_KEY"));
        staking = new ULRStaking(ulr, usd, vm.envOr("ULR_CANDIDATE_THRESHOLD", uint256(200_000 ether)), vm.envOr("ULR_MINIMUM_STAKE", uint256(1)));
        vm.stopBroadcast();

        console.log("ULRStaking:", address(staking));
        console.log("Existing ULR:", ulr);
        console.log("USD reward token:", usd);
        console.log("First eligible UTC day:", staking.firstEligibleDay());
    }
}
