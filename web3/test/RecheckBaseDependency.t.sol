// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./DeploymentStageFixture.sol";
import {Vm, VmSafe} from "forge-std/Vm.sol";
import {DAOTestUSD} from "../contracts/src/demo/DAOTestUSD.sol";
import {FROSTVerifier} from "../contracts/src/FROSTVerifier.sol";
import {DKGCoordinator} from "../contracts/src/federation/DKGCoordinator.sol";
import {MultiPoolShareTokenDeployer} from "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";

contract RecheckBaseDependencyTest is DeploymentStageFixture {
    function setUp() public { _stageSetUp(); }

    function _assertNoBroadcastAccess(uint64 nonceBefore) private {
        Vm.AccountAccess[] memory accesses = vm.stopAndReturnStateDiff();
        assertEq(vm.getNonce(broadcaster), nonceBefore, "broadcaster nonce changed");
        for (uint256 i; i < accesses.length; ++i) {
            if (accesses[i].accessor == broadcaster) {
                assertTrue(accesses[i].kind != VmSafe.AccountAccessKind.Create, "broadcast CREATE attempted");
                assertTrue(accesses[i].kind != VmSafe.AccountAccessKind.Call, "broadcast call attempted");
            }
        }
    }

    function test_BaseDispatcherRejectsERC20VerifierBeforeAnyBroadcast() public freshScriptEnvironment {
        DAOTestUSD wrongVerifier = new DAOTestUSD();
        _envAddress("FROST_VERIFIER", address(wrongVerifier));
        vm.setEnv("DEPLOYMENT_STAGE", "base");
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);

        vm.startStateDiffRecording();
        vm.expectRevert("runtime mismatch: FROSTVerifier");
        script.run();
        _assertNoBroadcastAccess(nonce);
    }

    function test_DirectBaseRejectsERC20DKGRegistryBeforeAnyBroadcast() public freshScriptEnvironment {
        DAOTestUSD wrongRegistry = new DAOTestUSD();
        _envAddress("DKG_COORDINATOR", address(wrongRegistry));
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);

        vm.startStateDiffRecording();
        vm.expectRevert("runtime mismatch: DKGCoordinator");
        script.deployBase();
        _assertNoBroadcastAccess(nonce);
    }

    function test_BaseRejectsSiblingLeafDespiteMatchingOwnerBeforeAnyBroadcast() public freshScriptEnvironment {
        vm.prank(broadcaster);
        MultiPoolShareTokenDeployer wrongLeaf = new MultiPoolShareTokenDeployer();
        assertEq(wrongLeaf.deployerOwner(), broadcaster);
        _envAddress("FROST_AUTHORIZATION_DEPLOYER", address(wrongLeaf));
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);

        vm.startStateDiffRecording();
        vm.expectRevert("runtime mismatch: FROSTAuthorizationDeployer");
        script.deployBase();
        _assertNoBroadcastAccess(nonce);
    }

    function test_BaseReusesCompiledVerifierAndDKGAndResumesWithoutTransactions() public freshScriptEnvironment {
        FROSTVerifier verifier = new FROSTVerifier();
        DKGCoordinator registry = new DKGCoordinator();
        assertEq(address(verifier).codehash, keccak256(type(FROSTVerifier).runtimeCode));
        assertEq(address(registry).codehash, keccak256(type(DKGCoordinator).runtimeCode));
        _envAddress("FROST_VERIFIER", address(verifier));
        _envAddress("DKG_COORDINATOR", address(registry));

        DeployFederation original = _script();
        original.deployBase();
        assertEq(address(original.frostVerifier()), address(verifier));
        assertEq(address(original.dkgCoordinator()), address(registry));
        assertEq(address(original.frostCoordinator().defaultVerifier()), address(verifier));
        assertEq(address(original.frostCoordinator().dkgRegistry()), address(registry));
        assertFalse(original.frostCoordinator().hasRole(original.frostCoordinator().ADMIN_ROLE(), broadcaster));

        _saveBase(original);
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        vm.startStateDiffRecording();
        resumed.deployBase();
        _assertNoBroadcastAccess(nonce);
    }
}
