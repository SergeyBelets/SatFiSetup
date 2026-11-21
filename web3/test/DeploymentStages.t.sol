// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./DeploymentStageFixture.sol";
import {FROSTCoordinator} from "../contracts/src/FROSTCoordinator.sol";
import {FROSTAuthorizationDeployer} from "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import {GlobalProjectWalletRegistry} from "../contracts/src/federation/GlobalProjectWalletRegistry.sol";
import {FederationFactory} from "../contracts/src/federation/FederationFactory.sol";

contract DeploymentStagesTest is DeploymentStageFixture {
    function setUp() public { _stageSetUp(); }

    function testBaseStageNeedsNoFeeConfigurationAndPinsExplicitWallets() public freshScriptEnvironment {
        vm.setEnv("DEPLOYMENT_STAGE", "base");
        vm.setEnv("DEPLOYMENT_FEE_ROUTER", "not-an-address");
        vm.setEnv("DEPLOYMENT_FEE_AMOUNT", "not-a-number");
        vm.setEnv("INTEGRATION_FEE_EXEMPTION", "not-a-boolean");
        vm.setEnv("PROJECT_DKG_ID", "not-a-hash");
        DeployFederation script = _script();
        script.run();
        assertGt(address(script.dkgCoordinator()).code.length, 0);
        assertEq(address(script.frostCoordinator().dkgRegistry()), address(script.dkgCoordinator()));
        assertFalse(script.frostCoordinator().hasRole(script.frostCoordinator().ADMIN_ROLE(), broadcaster));
        assertEq(address(script.federationFactory()), address(0));
        assertEq(address(script.projectRouter()), address(0));
        GlobalProjectWalletRegistry.ProjectWallet[] memory wallets = script.globalWalletRegistry().getWallets();
        assertEq(script.globalWalletRegistry().owner(), REGISTRY_OWNER);
        assertEq(wallets.length, 2);
        assertEq(wallets[0].wallet, PROJECT_FIRST);
        assertEq(wallets[1].wallet, PROJECT_SECOND);
        assertEq(wallets[0].shareBps, 1234);
        assertEq(wallets[1].shareBps, 2345);
        assertEq(wallets[0].label, "Operations");
        assertEq(wallets[1].label, "Community");
        assertTrue(script.frostAuthorizationDeployer().authorizedDeployers(address(script.simpleMultiPoolDeployer())));
        assertTrue(script.multiPoolDeployer().authorizedDeployers(address(script.confederationDeployer())));
    }

    function testOneWalletRequiresExplicitZeroSecondAndExplicitNondefaultShareAndLabel() public freshScriptEnvironment {
        _envAddress("PROJECT_WALLET_2", address(0));
        vm.setEnv("PROJECT_WALLET_SHARES", "4321");
        vm.setEnv("PROJECT_WALLET_LABELS", "Specified owner");
        DeployFederation script = _script();
        script.deployBase();
        GlobalProjectWalletRegistry.ProjectWallet[] memory wallets = script.globalWalletRegistry().getWallets();
        assertEq(wallets.length, 1);
        assertEq(wallets[0].wallet, PROJECT_FIRST);
        assertEq(wallets[0].shareBps, 4321);
        assertEq(wallets[0].label, "Specified owner");
    }

    function testInvalidWalletInputsRejectBeforeAnyBroadcast() public freshScriptEnvironment {
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);
        _envAddress("PROJECT_WALLET_1", address(0));
        vm.expectRevert("invalid project wallets"); script.deployBase();
        _walletEnvironment();
        _envAddress("PROJECT_WALLET_2", PROJECT_FIRST);
        vm.expectRevert("invalid project wallets"); script.deployBase();
        _walletEnvironment();
        vm.setEnv("PROJECT_WALLET_SHARES", "1000");
        vm.expectRevert("wallet arrays"); script.deployBase();
        _walletEnvironment();
        vm.setEnv("PROJECT_WALLET_SHARES", "6000,5000");
        vm.expectRevert("wallet total shares"); script.deployBase();
        _walletEnvironment();
        vm.setEnv("PROJECT_WALLET_SHARES", "0,0");
        vm.expectRevert("wallet total shares"); script.deployBase();
        _walletEnvironment();
        vm.setEnv("PROJECT_WALLET_LABELS", "Operations,");
        vm.expectRevert("wallet share or label"); script.deployBase();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testExplicitChainStageAndMissingWalletRejectBeforeBroadcast() public freshScriptEnvironment {
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.expectRevert("chain mismatch"); script.deployBase();
        vm.setEnv("EXPECTED_CHAIN_ID", "31337");
        vm.setEnv("DEPLOYMENT_STAGE", "unknown");
        vm.expectRevert("unknown deployment stage"); script.run();
        vm.setEnv("PROJECT_WALLET_1", "");
        vm.expectRevert(); script.deployBase();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testFreshScriptResumesSavedBaseWithoutDeployingAnythingAgain() public freshScriptEnvironment {
        DeployFederation original = _script();
        original.deployBase();
        _saveBase(original);
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        resumed.deployBase();
        assertEq(address(resumed.dkgCoordinator()), address(original.dkgCoordinator()));
        assertEq(address(resumed.federationDeployer()), address(original.federationDeployer()));
        assertEq(address(resumed.globalWalletRegistry()), address(original.globalWalletRegistry()));
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testPartialBaseResumesFromOnePreviouslyDeployedLeaf() public freshScriptEnvironment {
        vm.prank(broadcaster);
        FROSTAuthorizationDeployer leaf = new FROSTAuthorizationDeployer();
        _envAddress("FROST_AUTHORIZATION_DEPLOYER", address(leaf));
        DeployFederation script = _script();
        script.deployBase();
        assertEq(address(script.frostAuthorizationDeployer()), address(leaf));
        assertTrue(leaf.authorizedDeployers(address(script.simpleMultiPoolDeployer())));
        assertFalse(script.frostCoordinator().hasRole(script.frostCoordinator().ADMIN_ROLE(), broadcaster));
    }

    function testSavedAddressOwnershipOrDependencyMismatchRejectsBeforeTransactions() public freshScriptEnvironment {
        FROSTAuthorizationDeployer wrongOwner = new FROSTAuthorizationDeployer();
        _envAddress("FROST_AUTHORIZATION_DEPLOYER", address(wrongOwner));
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation script = _script();
        vm.expectRevert("deployer owner mismatch"); script.deployBase();
        assertEq(vm.getNonce(broadcaster), nonce);
        _envAddress("FROST_AUTHORIZATION_DEPLOYER", address(0));
        script.deployBase();
        _saveBase(script);
        nonce = vm.getNonce(broadcaster);
        _envAddress("DKG_COORDINATOR", address(new DKGCoordinator()));
        DeployFederation resumed = _script();
        vm.expectRevert("DKG registry mismatch"); resumed.deployBase();
        assertEq(vm.getNonce(broadcaster), nonce);
        _envAddress("DKG_COORDINATOR", address(script.dkgCoordinator()));
        vm.setEnv("PROJECT_WALLET_LABELS", "Changed,Community");
        vm.expectRevert("global wallet configuration mismatch"); resumed.deployBase();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testFactoriesAreIndependentStageAndResumeFromSavedAddresses() public freshScriptEnvironment {
        DeployFederation script = _baseAndFactories();
        assertEq(script.federationFactory().feeSetupAuthority(), broadcaster);
        assertEq(script.confederationFactory().feeSetupAuthority(), broadcaster);
        assertFalse(script.federationFactory().deploymentFeeConfigured());
        assertFalse(script.confederationFactory().deploymentFeeConfigured());
        assertTrue(script.federationDeployer().authorizedDeployers(address(script.federationFactory())));
        assertTrue(script.confederationDeployer().authorizedDeployers(address(script.confederationFactory())));
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        resumed.deployFactories();
        assertEq(address(resumed.federationFactory()), address(script.federationFactory()));
        assertEq(address(resumed.confederationFactory()), address(script.confederationFactory()));
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testSavedFactoryMismatchRejectedBeforeCreatingTheOtherFactory() public freshScriptEnvironment {
        DeployFederation script = _baseAndFactories();
        FederationFactory mismatched = new FederationFactory(address(new DKGCoordinator()),
            address(script.federationDeployer()), address(script.frostCoordinator()));
        _envAddress("FEDERATION_FACTORY", address(mismatched));
        _envAddress("CONFEDERATION_FACTORY", address(0));
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        vm.expectRevert("federation factory mismatch"); resumed.deployFactories();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testRenouncedDeployerCreatesAndManagesOwnV1SessionWithoutControllingOthers() public freshScriptEnvironment {
        DeployFederation script = _baseAndFactories();
        FROSTCoordinator frost = script.frostCoordinator();
        bytes32 admin = frost.ADMIN_ROLE();
        address target = address(script.federationFactory());
        bytes32 epoch = keccak256("session after renounce");
        _completeCeremony(epoch, _founders(2), 2, keccak256("EVM_AUTH"), GX);
        assertFalse(frost.hasRole(frost.ADMIN_ROLE(), broadcaster));
        vm.prank(broadcaster); // Not a DKG participant; no new participant-only policy is introduced.
        uint256 mine = frost.createSessionFromDKG(epoch, keccak256("my operation"), target, uint64(block.timestamp + 1 hours));
        vm.prank(broadcaster);
        frost.extendDeadline(mine, uint64(block.timestamp + 2 hours));
        vm.prank(OUTSIDER);
        uint256 theirs = frost.createSessionFromDKG(epoch, keccak256("their operation"), target, uint64(block.timestamp + 1 hours));
        vm.prank(broadcaster);
        vm.expectRevert(abi.encodeWithSignature("AccessControlUnauthorizedAccount(address,bytes32)", broadcaster, admin));
        frost.extendDeadline(theirs, uint64(block.timestamp + 2 hours));
        vm.prank(broadcaster);
        vm.expectRevert(abi.encodeWithSignature("AccessControlUnauthorizedAccount(address,bytes32)", broadcaster, admin));
        frost.abortSession(theirs, "forbidden");
        vm.prank(broadcaster);
        frost.abortSession(mine, "creator cancellation");
        vm.prank(OUTSIDER);
        frost.abortSession(theirs, "other creator cancellation");
    }

    function testExemptDispatcherRequiresExplicitAllowedConfiguration() public freshScriptEnvironment {
        DeployFederation script = _script();
        vm.setEnv("DEPLOYMENT_STAGE", "exempt_all");
        vm.setEnv("INTEGRATION_FEE_EXEMPTION", "true");
        vm.setEnv("DEPLOYMENT_FEE_AMOUNT", "0");
        _envAddress("DEPLOYMENT_FEE_ROUTER", address(0));
        script.run();
        assertTrue(script.federationFactory().integrationFeeExemption());
        assertTrue(script.confederationFactory().integrationFeeExemption());
        assertTrue(script.federationFactory().deploymentFeeConfigured());
        assertEq(address(script.projectRouter()), address(0));
        vm.chainId(1);
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        uint64 nonce = vm.getNonce(broadcaster);
        vm.expectRevert("integration network only"); script.run();
        assertEq(vm.getNonce(broadcaster), nonce);
    }
}
