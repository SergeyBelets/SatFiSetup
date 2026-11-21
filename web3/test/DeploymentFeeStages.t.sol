// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./DeploymentStageFixture.sol";
import {DKGAuthorization} from "../contracts/src/federation/DKGAuthorization.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAOFeeRouter} from "../contracts/src/federation/CustodianDAOFeeRouter.sol";
import {FederationFactory} from "../contracts/src/federation/FederationFactory.sol";
import {ConfederationFactory} from "../contracts/src/federation/ConfederationFactory.sol";
import {FROSTAuthorizationDeployer} from "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import {DAOTestUSD} from "../contracts/src/demo/DAOTestUSD.sol";

/// @dev These tests run real deployment scripts and real fee transfers. Finalized
/// FROST session results are explicit fixtures; actual signing is exercised by e2e.
contract DeploymentFeeStagesTest is DeploymentStageFixture {
    bytes32 private constant PROJECT_ID = keccak256("dedicated project DKG");
    uint256 private constant FEE = 50 ether;
    DeployFederation private infrastructure;
    StageRewardToken private token;
    FederationFactory private fed;
    ConfederationFactory private conf;

    function setUp() public {
        _stageSetUp();
        infrastructure = _baseAndFactories();
        fed = infrastructure.federationFactory();
        conf = infrastructure.confederationFactory();
        token = new StageRewardToken();
        _completeCeremony(PROJECT_ID, _founders(2), 2, keccak256("EVM_AUTH"), SECOND_GX);
        _feeEnvironment();
        vm.mockCall(coordinator.defaultVerifier(), abi.encodeWithSignature("verify(bytes,bytes32,bytes,string)"), abi.encode(true));
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
        vm.setEnv("INTEGRATION_FEE_EXEMPTION", "false");
        vm.setEnv("DEPLOYMENT_FEE_AMOUNT", vm.toString(FEE));
    }

    function _saveRouter(DeployFederation script) private {
        _envAddress("PROJECT_AUTHORIZATION", address(script.projectAuth()));
        _envAddress("PROJECT_INCOME_VAULT", address(script.projectVault()));
        _envAddress("DEPLOYMENT_FEE_ROUTER", address(script.projectRouter()));
    }

    function _deployRouter() private returns (DeployFederation script) {
        script = _script();
        script.deployRouter();
        _saveRouter(script);
    }

    function _operationEnvironment(uint256 nonce, bytes32 op, uint256 session) private {
        vm.setEnv("PAYER_OPERATION_NONCE", vm.toString(nonce));
        vm.setEnv("PAYER_OPERATION_HASH", vm.toString(op));
        vm.setEnv("FROST_SESSION_ID", vm.toString(session));
    }

    function _finalizedResult(DKGAuthorization auth, uint256 session, bytes32 op, bytes32 epoch) private {
        (, bytes32 result,,,,,) = dkg.getCompletedResult(epoch);
        vm.mockCall(address(coordinator), abi.encodeWithSignature("sessionDKGId(uint256)", session), abi.encode(epoch));
        vm.mockCall(address(coordinator), abi.encodeWithSignature("sessionDKGResult(uint256)", session), abi.encode(result));
        vm.mockCall(address(coordinator), abi.encodeWithSignature("getSessionParticipants(uint256)", session), abi.encode(auth.getParticipants()));
        vm.mockCall(address(coordinator), abi.encodeWithSignature("getAuthorizationResult(uint256)", session),
            abi.encode(auth.groupPubkey(), op, true, address(0), vm.envAddress("DEPLOYMENT_FEE_ROUTER"), auth.threshold(), auth.threshold(), hex"01"));
    }

    function _allowBoth(DeployFederation script) private {
        for (uint256 i; i < 2; ++i) {
            (, uint256 nonce, bytes32 op) = script.prepareNextPayer();
            DKGAuthorization auth = DKGAuthorization(address(CustodianDAOFeeRouter(vm.envAddress("DEPLOYMENT_FEE_ROUTER")).authorization()));
            _finalizedResult(auth, i + 100, op, PROJECT_ID);
            _operationEnvironment(nonce, op, i + 100);
            script.executeNextPayer();
        }
    }

    function testProjectRouterCreatesDedicatedAuthorizationVaultAndZeroDeploymentShare() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        assertEq(script.projectAuth().dkgId(), PROJECT_ID);
        assertEq(script.projectAuth().getParticipants(), _founders(2));
        assertEq(script.projectAuth().threshold(), 2);
        assertEq(script.projectAuth().groupPubkey(), abi.encodePacked(SECOND_GX));
        assertEq(script.projectRouter().treasury(), PROJECT_TREASURY);
        assertEq(address(script.projectRouter().rewardToken()), address(token));
        assertEq(script.projectRouter().deployStakerBps(), 0);
        assertEq(script.projectRouter().wrapStakerBps(), 6000);
        assertEq(script.projectVault().settlement(), address(0));
        (address source,, bool active,,) = script.projectVault().sources(script.projectVault().DEPLOY_FEE());
        assertEq(source, address(0));
        assertFalse(active);
        assertFalse(infrastructure.frostCoordinator().hasRole(infrastructure.frostCoordinator().ADMIN_ROLE(), broadcaster));
    }

    function testRouterFullResumeUsesSavedAddressesAndCreatesNoTransactions() public freshScriptEnvironment {
        DeployFederation original = _deployRouter();
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        resumed.deployRouter();
        assertEq(address(resumed.projectAuth()), address(original.projectAuth()));
        assertEq(address(resumed.projectVault()), address(original.projectVault()));
        assertEq(address(resumed.projectRouter()), address(original.projectRouter()));
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testRouterResumesAfterOnlyAuthorizationAndVaultWereConfirmed() public freshScriptEnvironment {
        FROSTAuthorizationDeployer leaf = FROSTAuthorizationDeployer(vm.envAddress("FROST_AUTHORIZATION_DEPLOYER"));
        vm.prank(broadcaster);
        address auth = leaf.deployFROSTAuthorizationV1(address(coordinator), abi.encodePacked(SECOND_GX), _founders(2), 2, PROJECT_ID);
        vm.prank(broadcaster);
        CustodianDAOIncomeVault vault = new CustodianDAOIncomeVault(address(token), auth);
        _envAddress("PROJECT_AUTHORIZATION", auth);
        _envAddress("PROJECT_INCOME_VAULT", address(vault));
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        resumed.deployRouter();
        assertEq(address(resumed.projectAuth()), auth);
        assertEq(address(resumed.projectVault()), address(vault));
        assertGt(address(resumed.projectRouter()).code.length, 0);
        assertEq(vm.getNonce(broadcaster), nonce + 1);
    }

    function testRouterPreflightRejectsRegistryPurposePermissionsAndFinancialPolicy() public freshScriptEnvironment {
        bytes32 btc = keccak256("not a project auth ceremony");
        _completeCeremony(btc, _founders(2), 2, keccak256("BTC_SPEND"), GX);
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);
        _envAddress("DKG_COORDINATOR", address(new DKGCoordinator()));
        vm.expectRevert("DKG registry mismatch"); script.deployRouter();
        _envAddress("DKG_COORDINATOR", address(dkg));
        vm.setEnv("PROJECT_DKG_ID", vm.toString(btc));
        vm.expectRevert("project DKG purpose"); script.deployRouter();
        vm.setEnv("PROJECT_DKG_ID", vm.toString(PROJECT_ID));
        vm.setEnv("PRIVATE_KEY", "2");
        vm.expectRevert("authorization deployer permission"); script.deployRouter();
        vm.setEnv("PRIVATE_KEY", "1");
        vm.setEnv("DEPLOY_STAKER_BPS", "1");
        vm.expectRevert("project fee policy"); script.deployRouter();
        vm.setEnv("DEPLOY_STAKER_BPS", "0");
        _envAddress("PROJECT_TREASURY", address(0));
        vm.expectRevert("treasury missing"); script.deployRouter();
        _envAddress("PROJECT_TREASURY", PROJECT_TREASURY);
        _envAddress("PROJECT_REWARD_TOKEN", OUTSIDER);
        vm.expectRevert("reward token missing"); script.deployRouter();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testConflictingSavedAuthorizationVaultOrRouterRejectBeforeNewTransactions() public freshScriptEnvironment {
        DeployFederation original = _deployRouter();
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        vm.setEnv("PROJECT_DKG_ID", vm.toString(bytes32(uint256(123))));
        vm.expectRevert(DKGCoordinator.DKGNotComplete.selector); resumed.deployRouter();
        vm.setEnv("PROJECT_DKG_ID", vm.toString(PROJECT_ID));
        _envAddress("PROJECT_AUTHORIZATION", address(0));
        vm.expectRevert("project vault mismatch"); resumed.deployRouter();
        _envAddress("PROJECT_AUTHORIZATION", address(original.projectAuth()));
        _envAddress("PROJECT_INCOME_VAULT", address(0));
        vm.expectRevert("project router vault mismatch"); resumed.deployRouter();
        _envAddress("PROJECT_INCOME_VAULT", address(original.projectVault()));
        _envAddress("PROJECT_TREASURY", OUTSIDER);
        vm.expectRevert("project router policy mismatch"); resumed.deployRouter();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testPayerPreparationAndExecutionUseConsecutiveCurrentNonces() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        (address first, uint256 n0, bytes32 h0) = script.prepareNextPayer();
        assertEq(first, address(fed)); assertEq(n0, 0);
        (address stillFirst, uint256 stillN0, bytes32 stillH0) = script.prepareNextPayer();
        assertEq(stillFirst, first); assertEq(stillN0, n0); assertEq(stillH0, h0);
        _finalizedResult(script.projectAuth(), 100, h0, PROJECT_ID);
        _operationEnvironment(n0, h0, 100);
        script.executeNextPayer();
        assertTrue(script.projectRouter().authorizedPayers(address(fed)));
        assertFalse(script.projectRouter().authorizedPayers(address(conf)));
        (address second, uint256 n1, bytes32 h1) = script.prepareNextPayer();
        assertEq(second, address(conf)); assertEq(n1, 1); assertTrue(h0 != h1);
        assertEq(h1, script.projectRouter().getSetPayerOperationHash(address(conf), true));
        _finalizedResult(script.projectAuth(), 101, h1, PROJECT_ID);
        _operationEnvironment(n1, h1, 101);
        script.executeNextPayer();
        (address none, uint256 n2, bytes32 h2) = script.prepareNextPayer();
        assertEq(none, address(0)); assertEq(n2, 2); assertEq(h2, bytes32(0));
        assertTrue(script.projectRouter().authorizedPayers(address(conf)));
        uint64 txNonce = vm.getNonce(broadcaster);
        script.executeNextPayer();
        assertEq(vm.getNonce(broadcaster), txNonce);
    }

    function testPayerResumeUsesAlreadyApprovedOperationWithoutReauthorizingSession() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        (, uint256 nonce, bytes32 op) = script.prepareNextPayer();
        _approve(script.projectAuth(), PROJECT_ID, op, address(script.projectRouter()));
        _operationEnvironment(nonce, op, 999999); // No such session; the operation was already approved earlier.
        script.executeNextPayer();
        assertTrue(script.projectRouter().authorizedPayers(address(fed)));
        assertTrue(script.projectAuth().executedOperations(op));
        assertEq(script.projectRouter().operationNonce(), 1);
    }

    function testPayerRejectsStaleNonceHashAndWrongAuthorization() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        (, uint256 nonce, bytes32 op) = script.prepareNextPayer();
        uint64 txNonce = vm.getNonce(broadcaster);
        _operationEnvironment(nonce + 1, op, 100);
        vm.expectRevert("stale payer operation"); script.executeNextPayer();
        assertEq(vm.getNonce(broadcaster), txNonce);
        _operationEnvironment(nonce, bytes32(uint256(123)), 100);
        vm.expectRevert("stale payer operation"); script.executeNextPayer();
        assertEq(vm.getNonce(broadcaster), txNonce);
        _operationEnvironment(nonce, op, 100);
        vm.mockCall(address(coordinator), abi.encodeWithSignature("sessionDKGId(uint256)", uint256(100)), abi.encode(bytes32(uint256(666))));
        // The authorization contract rejects this attempted transaction; payer state stays unchanged.
        vm.expectRevert("DKG result mismatch"); script.executeNextPayer();
        vm.stopBroadcast();
        assertEq(script.projectRouter().operationNonce(), 0);
        assertFalse(script.projectRouter().authorizedPayers(address(fed)));
        assertFalse(script.projectAuth().isOperationApproved(op, address(script.projectRouter())));
        assertFalse(script.projectAuth().executedOperations(op));
    }

    function testConfigurationNeedsBothPayersAndCorrectAuthorityBeforeAnyFactoryWrite() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        uint64 nonce = vm.getNonce(broadcaster);
        vm.expectRevert("payer permissions missing"); script.configureFactories();
        assertEq(vm.getNonce(broadcaster), nonce);
        assertFalse(fed.deploymentFeeConfigured()); assertFalse(conf.deploymentFeeConfigured());
        _allowBoth(script);
        vm.setEnv("PRIVATE_KEY", "2");
        vm.expectRevert("fee setup authority"); script.configureFactories();
        assertFalse(fed.deploymentFeeConfigured()); assertFalse(conf.deploymentFeeConfigured());
    }

    function testPartialFeeConfigurationResumesAndSkipsMatchingImmutableState() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        _allowBoth(script);
        script.configureFactory(address(fed));
        assertTrue(fed.deploymentFeeConfigured()); assertFalse(conf.deploymentFeeConfigured());
        uint64 nonce = vm.getNonce(broadcaster);
        DeployFederation resumed = _script();
        resumed.configureFactories();
        assertEq(vm.getNonce(broadcaster), nonce + 1);
        assertTrue(conf.deploymentFeeConfigured());
        assertEq(fed.deploymentFee(), FEE); assertEq(conf.deploymentFee(), FEE);
        assertEq(address(fed.deploymentFeeRouter()), address(script.projectRouter()));
        assertFalse(fed.integrationFeeExemption()); assertFalse(conf.integrationFeeExemption());
        nonce = vm.getNonce(broadcaster);
        resumed.configureFactories();
        assertEq(vm.getNonce(broadcaster), nonce);
        address router = address(script.projectRouter());
        vm.prank(broadcaster);
        vm.expectRevert("fee setup"); fed.configureDeploymentFee(router, FEE, false);
    }

    function testConflictingFeeSetupDoesNotModifyTheOtherFactory() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        _allowBoth(script);
        address router = address(script.projectRouter());
        vm.prank(broadcaster);
        fed.configureDeploymentFee(router, FEE + 1, false);
        uint64 nonce = vm.getNonce(broadcaster);
        vm.expectRevert("conflicting fee configuration"); script.configureFactories();
        assertEq(vm.getNonce(broadcaster), nonce);
        assertFalse(conf.deploymentFeeConfigured());
    }

    function testProjectRouterRejectsWrongEpochAndActiveDeploymentVaultSource() public freshScriptEnvironment {
        DeployFederation script = _deployRouter();
        vm.setEnv("PROJECT_DKG_ID", vm.toString(bytes32(uint256(123))));
        vm.expectRevert("project router authorization mismatch"); script.prepareNextPayer();
        vm.setEnv("PROJECT_DKG_ID", vm.toString(PROJECT_ID));
        CustodianDAOIncomeVault vault = script.projectVault();
        bytes32 sourceId = vault.DEPLOY_FEE();
        bytes32 op = vault.getSetSourceOperationHash(sourceId, address(script.projectRouter()), CustodianDAOIncomeVault.Stream.DeployFee, true, address(0), 0);
        _approve(script.projectAuth(), PROJECT_ID, op, address(vault));
        vault.setSource(sourceId, address(script.projectRouter()), CustodianDAOIncomeVault.Stream.DeployFee, true, address(0), 0, op);
        vm.expectRevert("project deployment source forbidden"); script.prepareNextPayer();
    }

    function testPaidV1FactoryDeploysAfterAllStagesMoveExactFeeOnlyToTreasury() public freshScriptEnvironment {
        _paidFactoryCycle();
    }

    function testEthereumMainnetPaidFactoryCycleAndResume() public freshScriptEnvironment {
        vm.chainId(1);
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.setEnv("PROJECT_REWARD_TOKEN_CODE_HASH", vm.toString(address(token).codehash));
        vm.setEnv("PROJECT_REWARD_TOKEN_DECIMALS", "18");
        vm.setEnv("WRAP_STAKER_BPS", "0");
        _paidFactoryCycle();
        uint64 nonce = vm.getNonce(broadcaster);
        _script().configureFactories();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testEthereumMainnetRejectsAssetSubstitutionAndKnownTestTokenBeforeBroadcast() public freshScriptEnvironment {
        vm.chainId(1);
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.setEnv("PROJECT_REWARD_TOKEN_CODE_HASH", vm.toString(bytes32(uint256(1))));
        vm.setEnv("PROJECT_REWARD_TOKEN_DECIMALS", "18");
        vm.setEnv("WRAP_STAKER_BPS", "0");
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);
        vm.expectRevert("reward token code hash"); script.deployRouter();
        vm.setEnv("PROJECT_REWARD_TOKEN_CODE_HASH", vm.toString(address(token).codehash));
        vm.setEnv("PROJECT_REWARD_TOKEN_DECIMALS", "6");
        vm.expectRevert("reward token decimals"); script.deployRouter();
        // Constructor itself forbids chain 1; etch only this adversarial fixture
        // to prove that a matching pin cannot bless already-present test code.
        address mock = address(0xFACE);
        vm.etch(mock, vm.getDeployedCode("DAOTestUSD.sol:DAOTestUSD"));
        _envAddress("PROJECT_REWARD_TOKEN", mock);
        vm.setEnv("PROJECT_REWARD_TOKEN_CODE_HASH", vm.toString(mock.codehash));
        vm.expectRevert("test reward token forbidden"); script.deployRouter();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function testEthereumMainnetRejectsZeroOrExemptFactoryFeesBeforeRouterBroadcast() public freshScriptEnvironment {
        vm.chainId(1);
        vm.setEnv("EXPECTED_CHAIN_ID", "1");
        vm.setEnv("PROJECT_REWARD_TOKEN_CODE_HASH", vm.toString(address(token).codehash));
        vm.setEnv("PROJECT_REWARD_TOKEN_DECIMALS", "18");
        vm.setEnv("WRAP_STAKER_BPS", "0");
        DeployFederation script = _script();
        uint64 nonce = vm.getNonce(broadcaster);
        vm.setEnv("DEPLOYMENT_FEE_AMOUNT", "0");
        vm.expectRevert("mainnet paid deployment required"); script.deployRouter();
        vm.setEnv("DEPLOYMENT_FEE_AMOUNT", vm.toString(FEE));
        vm.setEnv("INTEGRATION_FEE_EXEMPTION", "true");
        vm.expectRevert("mainnet paid deployment required"); script.deployRouter();
        assertEq(vm.getNonce(broadcaster), nonce);
    }

    function _paidFactoryCycle() private {
        DeployFederation script = _deployRouter();
        _allowBoth(script);
        script.configureFactories();
        token.mint(address(0x100), 3 * FEE);
        bytes32 fedEpoch = keccak256("separate user federation DKG");
        _completeCeremony(fedEpoch, _founders(2), 2, keccak256("EVM_AUTH"), GX);
        vm.prank(broadcaster);
        fed.reserveFederationId(fedEpoch, "paid-federation");
        uint256 payerBefore = token.balanceOf(address(0x100));
        vm.startPrank(address(0x100));
        fed.adoptFederationDKG(fedEpoch, "paid-federation");
        token.approve(address(fed), FEE);
        fed.deployFederation(fedEpoch, "Paid federation", "PF");
        vm.stopPrank();
        assertEq(token.balanceOf(address(0x100)), payerBefore - FEE);
        assertEq(token.balanceOf(PROJECT_TREASURY), FEE);
        bytes32 confEpoch = keccak256("separate user confederation DKG");
        _completeCeremony(confEpoch, _roster(1, 1), 2, keccak256("EVM_AUTH"), GX);
        string[] memory ids = new string[](1); ids[0] = "paid-federation";
        vm.prank(broadcaster);
        conf.reserveConfederationId(confEpoch, "paid-confederation");
        vm.startPrank(address(0x100));
        conf.adoptConfederationDKG(confEpoch, ids, _custodians(1), 2, 1, "paid-confederation");
        token.approve(address(conf), FEE);
        conf.deployConfederation(confEpoch, "Paid confederation", "PC");
        vm.stopPrank();
        assertEq(token.balanceOf(address(0x100)), payerBefore - 2 * FEE);
        assertEq(token.balanceOf(PROJECT_TREASURY), 2 * FEE);
        assertEq(token.balanceOf(address(script.projectRouter())), 0);
        assertEq(token.balanceOf(address(script.projectVault())), 0);
        assertEq(token.balanceOf(address(fed)), 0); assertEq(token.balanceOf(address(conf)), 0);
        assertEq(script.projectVault().dayDeployFeeIncome(block.timestamp / 1 days), 0);
        assertTrue(fed.isFederationActive("paid-federation"));
        assertTrue(conf.isConfederationActive("paid-confederation"));
    }
}
