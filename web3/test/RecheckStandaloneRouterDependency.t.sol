// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./DeploymentStageFixture.sol";
import {Vm, VmSafe} from "forge-std/Vm.sol";
import {DeployDeploymentFeeRouter} from "../script/DeployDeploymentFeeRouter.s.sol";
import {DAOTestUSD} from "../contracts/src/demo/DAOTestUSD.sol";
import {FROSTCoordinator} from "../contracts/src/FROSTCoordinator.sol";
import {MultiPoolShareTokenDeployer} from "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";

contract RecheckStandaloneRouterDependencyTest is DeploymentStageFixture {
    bytes32 private constant PROJECT_ID = keccak256("standalone artifact review project DKG");
    DeployFederation private infrastructure;
    DAOTestUSD private token;
    uint256 private standaloneCount;

    function setUp() public {
        _stageSetUp();
        infrastructure = _baseAndFactories();
        token = new DAOTestUSD();
        _completeCeremony(PROJECT_ID, _founders(2), 2, keccak256("EVM_AUTH"), SECOND_GX);
        _feeEnvironment();
    }

    function _restoreScriptEnvironment() internal override {
        _resetScriptEnvironment();
        _saveBase(baseInfrastructure);
        _saveFactories(infrastructure);
        _feeEnvironment();
    }

    function _feeEnvironment() private {
        vm.setEnv("PROJECT_DKG_ID", vm.toString(PROJECT_ID));
        _envAddress("PROJECT_REWARD_TOKEN", address(token));
        _envAddress("PROJECT_TREASURY", PROJECT_TREASURY);
        vm.setEnv("WRAP_STAKER_BPS", "6000");
        vm.setEnv("DEPLOY_STAKER_BPS", "0");
    }

    // Only the local standalone script scaffold is etched; the graph is deployed by CREATE.
    function _standalone() private returns (DeployDeploymentFeeRouter script) {
        script = DeployDeploymentFeeRouter(address(uint160(0xE000 + ++standaloneCount)));
        vm.etch(address(script), type(DeployDeploymentFeeRouter).runtimeCode);
    }

    function _saveRouter(DeployDeploymentFeeRouter script) private {
        _envAddress("PROJECT_AUTHORIZATION", address(script.projectAuth()));
        _envAddress("PROJECT_INCOME_VAULT", address(script.projectVault()));
        _envAddress("DEPLOYMENT_FEE_ROUTER", address(script.projectRouter()));
    }

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

    function test_StandaloneRejectsERC20RegistryAndCoordinatorBeforeAnyBroadcast() public freshScriptEnvironment {
        DeployDeploymentFeeRouter script = _standalone();
        uint64 nonce = vm.getNonce(broadcaster);
        _envAddress("DKG_COORDINATOR", address(token));
        vm.startStateDiffRecording();
        vm.expectRevert("runtime mismatch: DKGCoordinator");
        script.run();
        _assertNoBroadcastAccess(nonce);

        _envAddress("DKG_COORDINATOR", address(dkg));
        _envAddress("FROST_COORDINATOR", address(token));
        vm.startStateDiffRecording();
        vm.expectRevert("runtime mismatch: FROSTCoordinator");
        script.deployRouter();
        _assertNoBroadcastAccess(nonce);
    }

    function test_StandaloneRejectsCompiledCoordinatorWithERC20VerifierBeforeAnyBroadcast() public freshScriptEnvironment {
        vm.startPrank(broadcaster);
        FROSTCoordinator wrong = new FROSTCoordinator(address(token));
        wrong.bindDKGRegistry(address(dkg));
        wrong.renounceRole(wrong.ADMIN_ROLE(), broadcaster);
        vm.stopPrank();
        _envAddress("FROST_COORDINATOR", address(wrong));
        DeployDeploymentFeeRouter script = _standalone();
        uint64 nonce = vm.getNonce(broadcaster);

        vm.startStateDiffRecording();
        vm.expectRevert("runtime mismatch: FROSTVerifier");
        script.run();
        _assertNoBroadcastAccess(nonce);
    }

    function test_StandaloneResumeRejectsSiblingLeafDespiteMatchingPermissionBeforeAnyBroadcast() public freshScriptEnvironment {
        DeployDeploymentFeeRouter original = _standalone();
        original.run();
        _saveRouter(original);
        vm.prank(broadcaster);
        MultiPoolShareTokenDeployer sibling = new MultiPoolShareTokenDeployer();
        assertEq(sibling.deployerOwner(), broadcaster);
        assertTrue(sibling.authorizedDeployers(broadcaster));
        _envAddress("FROST_AUTHORIZATION_DEPLOYER", address(sibling));
        DeployDeploymentFeeRouter resumed = _standalone();
        uint64 nonce = vm.getNonce(broadcaster);

        vm.startStateDiffRecording();
        vm.expectRevert("runtime mismatch: FROSTAuthorizationDeployer");
        resumed.run();
        _assertNoBroadcastAccess(nonce);
    }

    function test_StandaloneAcceptsExistingCanonicalGraphAndResumesWithoutTransactions() public freshScriptEnvironment {
        DeployDeploymentFeeRouter original = _standalone();
        original.run();
        _saveRouter(original);
        DeployDeploymentFeeRouter resumed = _standalone();
        uint64 nonce = vm.getNonce(broadcaster);

        vm.startStateDiffRecording();
        resumed.run();
        _assertNoBroadcastAccess(nonce);
        assertEq(address(resumed.projectAuth()), address(original.projectAuth()));
        assertEq(address(resumed.projectVault()), address(original.projectVault()));
        assertEq(address(resumed.projectRouter()), address(original.projectRouter()));
        assertEq(resumed.projectAuth().registry(), address(dkg));
        assertEq(address(resumed.projectAuth().coordinator()), address(coordinator));
    }
}
