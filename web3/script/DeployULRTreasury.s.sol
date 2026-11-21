// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {ULRCustodianSnapshots} from "../contracts/src/staking/ULRCustodianSnapshots.sol";
import {ULRConfederationTreasury} from "../contracts/src/staking/ULRConfederationTreasury.sol";

contract DeployULRCustodianSnapshots is Script {
    function run() external returns (ULRCustodianSnapshots snapshots) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        address staking = vm.envAddress("ULR_STAKING");
        address authority = vm.envAddress("ULR_SNAPSHOT_AUTHORIZATION");
        require(staking.code.length > 0 && authority.code.length > 0, "missing contracts");
        require(staking.codehash == vm.envBytes32("ULR_STAKING_CODE_HASH"), "staking code hash");
        require(authority.codehash == vm.envBytes32("ULR_SNAPSHOT_AUTHORIZATION_CODE_HASH"), "authorization code hash");
        vm.startBroadcast(vm.envUint("PRIVATE_KEY"));
        snapshots = new ULRCustodianSnapshots(staking, authority);
        vm.stopBroadcast();
        console.log("ULRCustodianSnapshots:", address(snapshots));
    }
}

contract DeployULRTreasury is Script {
    function run() external returns (ULRConfederationTreasury treasury) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        address factory = vm.envAddress("CONFEDERATION_FACTORY");
        string memory id = vm.envString("CONFEDERATION_ID");
        address snapshots = vm.envAddress("ULR_SNAPSHOTS");
        require(factory.code.length > 0 && snapshots.code.length > 0, "missing contracts");
        require(factory.codehash == vm.envBytes32("CONFEDERATION_FACTORY_CODE_HASH"), "factory code hash");
        require(snapshots.codehash == vm.envBytes32("ULR_SNAPSHOTS_CODE_HASH"), "snapshots code hash");
        require(address(ULRCustodianSnapshots(snapshots).staking()) == vm.envAddress("ULR_STAKING"), "staking binding");
        bytes32 scope = ULRCustodianSnapshots(snapshots).scopeFor(factory, id);
        require(ULRCustodianSnapshots(snapshots).treasuries(scope) == address(0), "treasury already enrolled");
        vm.startBroadcast(vm.envUint("PRIVATE_KEY"));
        treasury = new ULRConfederationTreasury(factory, id, snapshots,
            vm.envOr("ULR_RESERVE_FLOOR", uint256(1000) * 10**IERC20Metadata(address(ULRCustodianSnapshots(snapshots).staking().rewardToken())).decimals()),
            vm.envOr("ULR_PAYOUT_BPS", uint256(9000)));
        vm.stopBroadcast();
        console.log("ULRConfederationTreasury:", address(treasury));
        console.log("Pending enrollment operation:");
        console.logBytes32(
            ULRCustodianSnapshots(snapshots).getEnrollTreasuryOperationHash(factory, id, address(treasury))
        );
        console.log("Enrollment calldata (after FROST and custodian approval):");
        console.logBytes(
            abi.encodeCall(
                ULRCustodianSnapshots.enrollTreasury,
                (
                    factory,
                    id,
                    address(treasury),
                    ULRCustodianSnapshots(snapshots).getEnrollTreasuryOperationHash(factory, id, address(treasury))
                )
            )
        );
    }
}
