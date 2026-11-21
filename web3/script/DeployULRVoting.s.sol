// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {ULRConfederationTreasury} from "../contracts/src/staking/ULRConfederationTreasury.sol";
import {ULRConfederationVoting} from "../contracts/src/staking/ULRConfederationVoting.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";

contract DeployULRVoting is Script {
    function run() external returns (ULRConfederationVoting voting, CustodianDAODecisionBoard board) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        address target = vm.envAddress("ULR_TREASURY");
        require(
            target.code.length > 0 && target.codehash == vm.envBytes32("ULR_TREASURY_CODE_HASH"), "treasury code hash"
        );
        ULRConfederationTreasury treasury = ULRConfederationTreasury(target);
        require(treasury.snapshots().treasuries(treasury.scope()) == target, "treasury not enrolled");
        vm.startBroadcast(vm.envUint("PRIVATE_KEY"));
        voting = new ULRConfederationVoting(address(treasury.staking()), address(treasury.validator()));
        board = new CustodianDAODecisionBoard(address(voting), address(treasury.authorization()));
        vm.stopBroadcast();
        console.log("ULRConfederationVoting:", address(voting));
        console.log("ULR decision board:", address(board));
    }
}
