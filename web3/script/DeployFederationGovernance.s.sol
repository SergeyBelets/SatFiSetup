// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import {Script} from "forge-std/Script.sol";
import {FederationGovernance} from "../contracts/src/federation/FederationGovernance.sol";
import {GovernanceRegistry} from "../contracts/src/federation/GovernanceRegistry.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";

/// @notice Parameterized leaf script. Use the operational bootstrap journal for resumable deployment.
contract DeployFederationGovernance is Script {
    function run(string calldata id, address authorization, address pool, uint256 expectedChainId)
        external
        returns (address governance)
    {
        require(block.chainid == expectedChainId, "chain mismatch");
        require(authorization.code.length > 0 && pool.code.length > 0, "bindings missing");
        vm.startBroadcast();
        governance = address(new FederationGovernance(id, authorization, pool));
        vm.stopBroadcast();
    }

    function registerExisting(address registry, string calldata id, address governance, uint256 finalizedSession)
        external
    {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        GovernanceRegistry r = GovernanceRegistry(registry);
        if (r.governance(id) != address(0)) {
            require(r.governance(id) == governance, "already registered differently");
            return;
        }
        FederationGovernance g = FederationGovernance(governance);
        FROSTAuthorization auth = g.frostMultisig();
        bytes32 op = r.getRegisterOperationHash(id, governance);
        vm.startBroadcast();
        if (!auth.isOperationApproved(op, registry)) auth.authorizeOperation(finalizedSession, op, registry);
        r.register(id, governance, op);
        vm.stopBroadcast();
        require(r.governance(id) == governance, "registration failed");
    }
}
