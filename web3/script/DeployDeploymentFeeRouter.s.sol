// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Script.sol";
import {ScriptArtifactIdentity} from "./ScriptArtifactIdentity.sol";
import {FROSTCoordinator} from "../contracts/src/FROSTCoordinator.sol";
import {DKGCoordinator} from "../contracts/src/federation/DKGCoordinator.sol";
import {DKGAuthorization} from "../contracts/src/federation/DKGAuthorization.sol";
import {FROSTAuthorizationDeployer} from "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAOFeeRouter} from "../contracts/src/federation/CustodianDAOFeeRouter.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";

interface IDeploymentFeeFactory {
    function feeSetupAuthority() external view returns (address);
    function deploymentFeeConfigured() external view returns (bool);
    function integrationFeeExemption() external view returns (bool);
    function deploymentFee() external view returns (uint256);
    function deploymentFeeRouter() external view returns (address);
    function configureDeploymentFee(address router, uint256 amount, bool exemption) external;
}

/// @notice Project fee infrastructure and separate, resumable FROST payer steps.
/// @dev Optional EXISTING addresses only select reuse of already deployed components.
/// Public parameters and confirmed progress are persisted by ops/deployment_stages.py.
contract DeployDeploymentFeeRouter is ScriptArtifactIdentity {
    DKGAuthorization public projectAuth;
    CustodianDAOIncomeVault public projectVault;
    CustodianDAOFeeRouter public projectRouter;

    function run() external virtual { deployRouter(); }

    function _context() internal view returns (uint256 key, address sender) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        key = vm.envUint("PRIVATE_KEY");
        sender = vm.addr(key);
    }

    function _existing(string memory name, string memory artifactName) internal view returns (address value) {
        value = vm.envOr(name, address(0));
        require(value == address(0) || value.code.length > 0, string.concat("missing code: ", name));
        if (value != address(0)) _requireArtifact(value, artifactName);
    }

    function _checkCoordinatorArtifact(FROSTCoordinator coordinator) internal view {
        _requireArtifact(address(coordinator), "FROSTCoordinator");
        _requireArtifact(address(coordinator.defaultVerifier()), "FROSTVerifier");
    }

    function _routerParameters() internal view returns (address reward, address treasury, uint16 wrap) {
        reward = vm.envAddress("PROJECT_REWARD_TOKEN");
        treasury = vm.envAddress("PROJECT_TREASURY");
        uint256 w = vm.envUint("WRAP_STAKER_BPS");
        uint256 d = vm.envUint("DEPLOY_STAKER_BPS");
        require(reward.code.length > 0 && IERC20Metadata(reward).decimals() <= 18, "reward token missing");
        // Ethereum mainnet never infers the asset from an address alone. These
        // public pins are also enforced by the confirmed deployment runner.
        if (block.chainid == 1) {
            require(w == 0 && d == 0, "mainnet project treasury only");
            require(reward.codehash == vm.envBytes32("PROJECT_REWARD_TOKEN_CODE_HASH"), "reward token code hash");
            require(IERC20Metadata(reward).decimals() == vm.envUint("PROJECT_REWARD_TOKEN_DECIMALS"), "reward token decimals");
            require(reward.codehash != keccak256(vm.getDeployedCode("DAOTestUSD.sol:DAOTestUSD")), "test reward token forbidden");
            require(!vm.envBool("INTEGRATION_FEE_EXEMPTION") && vm.envUint("DEPLOYMENT_FEE_AMOUNT") > 0, "mainnet paid deployment required");
        }
        require(treasury != address(0) && treasury != reward, "treasury missing");
        require(w <= 10_000 && d == 0, "project fee policy");
        wrap = uint16(w);
    }

    function deployRouter() public {
        (uint256 key, address sender) = _context();
        DKGCoordinator registry = DKGCoordinator(vm.envAddress("DKG_COORDINATOR"));
        FROSTCoordinator coordinator = FROSTCoordinator(vm.envAddress("FROST_COORDINATOR"));
        FROSTAuthorizationDeployer leaf = FROSTAuthorizationDeployer(vm.envAddress("FROST_AUTHORIZATION_DEPLOYER"));
        require(address(registry).code.length > 0 && address(coordinator).code.length > 0 && address(leaf).code.length > 0, "project infrastructure missing");
        _requireArtifact(address(registry), "DKGCoordinator");
        _checkCoordinatorArtifact(coordinator);
        _requireArtifact(address(leaf), "FROSTAuthorizationDeployer");
        require(address(coordinator.dkgRegistry()) == address(registry), "DKG registry mismatch");
        require(leaf.authorizedDeployers(sender), "authorization deployer permission");
        bytes32 id = vm.envBytes32("PROJECT_DKG_ID");
        (,bytes32 result,,bytes32 purpose,bytes memory group,address[] memory roster,uint256 threshold) = registry.getCompletedResult(id);
        require(purpose == keccak256("EVM_AUTH"), "project DKG purpose");
        (address reward, address treasury, uint16 wrap) = _routerParameters();
        projectAuth = DKGAuthorization(_existing("PROJECT_AUTHORIZATION", "DKGAuthorization"));
        projectVault = CustodianDAOIncomeVault(_existing("PROJECT_INCOME_VAULT", "CustodianDAOIncomeVault"));
        projectRouter = CustodianDAOFeeRouter(_existing("DEPLOYMENT_FEE_ROUTER", "CustodianDAOFeeRouter"));
        // Validate every existing dependency before scheduling any transaction.
        if (address(projectAuth) != address(0)) {
            require(address(projectAuth.coordinator()) == address(coordinator) && projectAuth.registry() == address(registry)
                && projectAuth.dkgId() == id && projectAuth.resultHash() == result
                && keccak256(projectAuth.groupPubkey()) == keccak256(group)
                && keccak256(abi.encode(projectAuth.getParticipants())) == keccak256(abi.encode(roster))
                && projectAuth.threshold() == threshold, "project authorization mismatch");
        }
        if (address(projectVault) != address(0)) {
            require(address(projectAuth) != address(0) && address(projectVault.authorization()) == address(projectAuth)
                && address(projectVault.rewardToken()) == reward && projectVault.settlement() == address(0), "project vault mismatch");
            (,,bool active,,) = projectVault.sources(projectVault.DEPLOY_FEE());
            require(!active, "project deployment source forbidden");
        }
        if (address(projectRouter) != address(0)) {
            require(address(projectVault) != address(0) && address(projectRouter.incomeVault()) == address(projectVault), "project router vault mismatch");
            _checkRouter(projectRouter, reward, treasury, wrap);
        }
        vm.startBroadcast(key);
        if (address(projectAuth) == address(0)) projectAuth = DKGAuthorization(leaf.deployFROSTAuthorizationV1(address(coordinator), group, roster, threshold, id));
        if (address(projectVault) == address(0)) projectVault = new CustodianDAOIncomeVault(reward, address(projectAuth));
        if (address(projectRouter) == address(0)) projectRouter = new CustodianDAOFeeRouter(address(projectVault), treasury, wrap, 0);
        vm.stopBroadcast();
        console.log("Project authorization:", address(projectAuth));
        console.log("Project income vault:", address(projectVault));
        console.log("Deployment fee router:", address(projectRouter));
        console.log("Project DKG result:"); console.logBytes32(result);
        console.log("Treasury:", treasury); console.log("Reward token:", reward);
    }

    function _checkRouter(CustodianDAOFeeRouter router, address reward, address treasury, uint16 wrap) internal view {
        _requireArtifact(address(router), "CustodianDAOFeeRouter");
        require(address(router).code.length > 0 && address(router.rewardToken()) == reward
            && router.treasury() == treasury && router.wrapStakerBps() == wrap
            && router.deployStakerBps() == 0, "project router policy mismatch");
        DKGAuthorization auth = DKGAuthorization(address(router.authorization()));
        _requireArtifact(address(auth), "DKGAuthorization");
        _requireArtifact(auth.registry(), "DKGCoordinator");
        _checkCoordinatorArtifact(FROSTCoordinator(address(auth.coordinator())));
        require(auth.dkgId() == vm.envBytes32("PROJECT_DKG_ID")
            && auth.registry() == vm.envAddress("DKG_COORDINATOR")
            && address(auth.coordinator()) == vm.envAddress("FROST_COORDINATOR"), "project router authorization mismatch");
        _requireArtifact(address(router.incomeVault()), "CustodianDAOIncomeVault");
        require(address(router.incomeVault().authorization()) == address(auth)
            && router.incomeVault().settlement() == address(0), "project vault binding");
        (,,bool active,,) = router.incomeVault().sources(router.incomeVault().DEPLOY_FEE());
        require(!active, "project deployment source forbidden");
    }

    function _factories() internal view returns (address first, address second) {
        first = vm.envAddress("FEDERATION_FACTORY"); second = vm.envAddress("CONFEDERATION_FACTORY");
        require(first.code.length > 0 && second.code.length > 0 && first != second, "factories missing");
        _requireArtifact(first, "FederationFactory");
        _requireArtifact(second, "ConfederationFactory");
    }

    /// @notice Only one current-nonce hash is prepared. Re-run after its confirmed execution.
    function prepareNextPayer() public view returns (address payer, uint256 nonce, bytes32 operationHash) {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        (address first, address second) = _factories();
        CustodianDAOFeeRouter router = CustodianDAOFeeRouter(vm.envAddress("DEPLOYMENT_FEE_ROUTER"));
        (address reward, address treasury, uint16 wrap) = _routerParameters();
        _checkRouter(router, reward, treasury, wrap);
        nonce = router.operationNonce();
        if (!router.authorizedPayers(first)) payer = first;
        else if (!router.authorizedPayers(second)) payer = second;
        if (payer != address(0)) operationHash = router.getSetPayerOperationHash(payer, true);
        console.log("Next payer:", payer); console.log("Current nonce:", nonce);
        console.log("Operation hash:"); console.logBytes32(operationHash);
    }

    function executeNextPayer() public {
        (uint256 key,) = _context();
        (address payer, uint256 nonce, bytes32 hash) = prepareNextPayer();
        if (payer == address(0)) return;
        require(nonce == vm.envUint("PAYER_OPERATION_NONCE") && hash == vm.envBytes32("PAYER_OPERATION_HASH"), "stale payer operation");
        uint256 session = vm.envUint("FROST_SESSION_ID");
        CustodianDAOFeeRouter router = CustodianDAOFeeRouter(vm.envAddress("DEPLOYMENT_FEE_ROUTER"));
        DKGAuthorization auth = DKGAuthorization(address(router.authorization()));
        vm.startBroadcast(key);
        if (!auth.isOperationApproved(hash, address(router))) auth.authorizeOperation(session, hash, address(router));
        router.setPayer(payer, true, hash);
        vm.stopBroadcast();
        require(router.authorizedPayers(payer) && router.operationNonce() == nonce + 1, "payer permission missing");
    }

    function _feeConfig(address target, address sender) internal view returns (address routerAddress, uint256 amount, bool exemption, bool done) {
        (address first, address second) = _factories();
        require(target == first || target == second, "unknown fee factory");
        exemption = vm.envBool("INTEGRATION_FEE_EXEMPTION");
        routerAddress = vm.envAddress("DEPLOYMENT_FEE_ROUTER");
        amount = vm.envUint("DEPLOYMENT_FEE_AMOUNT");
        if (exemption) {
            require(block.chainid == 31337 || block.chainid == 11155111, "integration network only");
            require(routerAddress == address(0) && amount == 0, "invalid exemption");
        } else {
            require(routerAddress.code.length > 0 && amount > 0, "fee config missing");
            (address reward, address treasury, uint16 wrap) = _routerParameters();
            CustodianDAOFeeRouter router = CustodianDAOFeeRouter(routerAddress);
            _checkRouter(router, reward, treasury, wrap);
            require(router.authorizedPayers(first) && router.authorizedPayers(second), "payer permissions missing");
        }
        IDeploymentFeeFactory factory = IDeploymentFeeFactory(target);
        done = factory.deploymentFeeConfigured();
        if (done) require(factory.deploymentFeeRouter() == routerAddress && factory.deploymentFee() == amount
            && factory.integrationFeeExemption() == exemption, "conflicting fee configuration");
        else require(factory.feeSetupAuthority() == sender, "fee setup authority");
    }

    function configureFactory(address target) public {
        (uint256 key, address sender) = _context();
        (address router, uint256 amount, bool exemption, bool done) = _feeConfig(target, sender);
        if (done) return;
        vm.startBroadcast(key);
        IDeploymentFeeFactory(target).configureDeploymentFee(router, amount, exemption);
        vm.stopBroadcast();
    }

    function configureFactories() public {
        (,address sender) = _context();
        (address first, address second) = _factories();
        _feeConfig(first, sender); _feeConfig(second, sender); // Full preflight before either write.
        configureFactory(first); configureFactory(second);
    }
}
