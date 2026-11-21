// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "forge-std/Script.sol";

// Sub-deployers
import "../contracts/src/federation/SimpleMultiPoolDeployer.sol";
import "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";
import "../contracts/src/federation/MultiPoolDeployer.sol";
import "../contracts/src/federation/ConfederationTokenDeployer.sol";
import "../contracts/src/federation/FROSTCustodianDeployer.sol";
import "../contracts/src/federation/FeeSystemDeployer.sol";
import "../contracts/src/federation/FederationFeeManagerDeployer.sol";
import "../contracts/src/federation/FeeDistributionCalculatorDeployer.sol";
import "../contracts/src/federation/FederationNFTModifierDeployer.sol";
import "../contracts/src/federation/ProjectWalletRegistryDeployer.sol";

// Deployers
import "../contracts/src/federation/FederationDeployer.sol";
import "../contracts/src/federation/ConfederationDeployer.sol";

// Core
import "../contracts/src/federation/DKGCoordinator.sol";
import "../contracts/src/federation/FederationFactory.sol";
import "../contracts/src/federation/ConfederationFactory.sol";
import "../contracts/src/federation/GlobalProjectWalletRegistry.sol";
import {FROSTVerifier} from "../contracts/src/FROSTVerifier.sol";
import {FROSTCoordinator} from "../contracts/src/FROSTCoordinator.sol";

import {DeployDeploymentFeeRouter} from "./DeployDeploymentFeeRouter.s.sol";
import {ControlledDeployer} from "../contracts/src/federation/ControlledDeployer.sol";

/// @notice Explicit stages: base -> project DKG -> fee_router; factories -> payers -> fees.
/// @dev Reuse addresses are supplied from the confirmed public deployment manifest.
contract DeployFederation is DeployDeploymentFeeRouter {
    SimpleMultiPoolDeployer public simpleMultiPoolDeployer;
    FROSTAuthorizationDeployer public frostAuthorizationDeployer;
    MultiPoolShareTokenDeployer public shareTokenDeployer;
    MultiPoolDeployer public multiPoolDeployer;
    ConfederationTokenDeployer public confederationTokenDeployer;
    FROSTCustodianDeployer public frostCustodianDeployer;
    FeeSystemDeployer public feeSystemDeployer;
    FederationFeeManagerDeployer public feeManagerDeployer;
    FeeDistributionCalculatorDeployer public feeDistributorDeployer;
    FederationNFTModifierDeployer public nftModifierDeployer;
    ProjectWalletRegistryDeployer public walletRegistryDeployer;
    FederationDeployer public federationDeployer;
    ConfederationDeployer public confederationDeployer;
    DKGCoordinator public dkgCoordinator;
    FROSTVerifier public frostVerifier;
    FROSTCoordinator public frostCoordinator;
    FederationFactory public federationFactory;
    ConfederationFactory public confederationFactory;
    GlobalProjectWalletRegistry public globalWalletRegistry;

    function run() external override {
        // Every invocation states its stage; the base stage requires no financial dependency.
        bytes32 stage = keccak256(bytes(vm.envString("DEPLOYMENT_STAGE")));
        if (stage == keccak256("base")) deployBase();
        else if (stage == keccak256("factories")) deployFactories();
        else if (stage == keccak256("fee_router")) deployRouter();
        else if (stage == keccak256("prepare_payer")) prepareNextPayer();
        else if (stage == keccak256("execute_payer")) executeNextPayer();
        else if (stage == keccak256("fees")) configureFactories();
        else if (stage == keccak256("exempt_all")) {
            _context();
            require(vm.envBool("INTEGRATION_FEE_EXEMPTION"), "exempt_all requires exemption");
            require(block.chainid == 31337 || block.chainid == 11155111, "integration network only");
            require(vm.envAddress("DEPLOYMENT_FEE_ROUTER") == address(0) && vm.envUint("DEPLOYMENT_FEE_AMOUNT") == 0, "invalid exemption");
            deployBase();
            vm.setEnv("DKG_COORDINATOR", vm.toString(address(dkgCoordinator)));
            vm.setEnv("FROST_COORDINATOR", vm.toString(address(frostCoordinator)));
            vm.setEnv("FEDERATION_DEPLOYER", vm.toString(address(federationDeployer)));
            vm.setEnv("CONFEDERATION_DEPLOYER", vm.toString(address(confederationDeployer)));
            deployFactories();
            vm.setEnv("FEDERATION_FACTORY", vm.toString(address(federationFactory)));
            vm.setEnv("CONFEDERATION_FACTORY", vm.toString(address(confederationFactory)));
            configureFactories();
        } else revert("unknown deployment stage");
    }

    function _wallets() internal view returns (address owner, address[] memory wallets, uint256[] memory shares, string[] memory labels) {
        owner = vm.envAddress("PROJECT_REGISTRY_OWNER");
        address first = vm.envAddress("PROJECT_WALLET_1");
        address second = vm.envAddress("PROJECT_WALLET_2"); // Explicit zero means no second wallet.
        require(owner != address(0) && first != address(0) && first != second, "invalid project wallets");
        uint256 count = second == address(0) ? 1 : 2;
        shares = vm.envUint("PROJECT_WALLET_SHARES", ",");
        labels = vm.envString("PROJECT_WALLET_LABELS", ",");
        require(shares.length == count && labels.length == count, "wallet arrays");
        wallets = new address[](count); wallets[0] = first;
        if (count == 2) wallets[1] = second;
        uint256 total;
        for (uint256 i; i < count; ++i) {
            require(shares[i] <= 10_000 && bytes(labels[i]).length != 0, "wallet share or label");
            total += shares[i];
        }
        require(total > 0 && total <= 10_000, "wallet total shares");
    }

    function _loadBase() internal {
        frostAuthorizationDeployer = FROSTAuthorizationDeployer(_existing("FROST_AUTHORIZATION_DEPLOYER", "FROSTAuthorizationDeployer"));
        shareTokenDeployer = MultiPoolShareTokenDeployer(_existing("MULTIPOOL_SHARE_TOKEN_DEPLOYER", "MultiPoolShareTokenDeployer"));
        simpleMultiPoolDeployer = SimpleMultiPoolDeployer(_existing("SIMPLE_MULTIPOOL_DEPLOYER", "SimpleMultiPoolDeployer"));
        confederationTokenDeployer = ConfederationTokenDeployer(_existing("CONFEDERATION_TOKEN_DEPLOYER", "ConfederationTokenDeployer"));
        multiPoolDeployer = MultiPoolDeployer(_existing("MULTIPOOL_DEPLOYER", "MultiPoolDeployer"));
        frostCustodianDeployer = FROSTCustodianDeployer(_existing("FROST_CUSTODIAN_DEPLOYER", "FROSTCustodianDeployer"));
        feeManagerDeployer = FederationFeeManagerDeployer(_existing("FEDERATION_FEE_MANAGER_DEPLOYER", "FederationFeeManagerDeployer"));
        feeDistributorDeployer = FeeDistributionCalculatorDeployer(_existing("FEE_DISTRIBUTION_CALCULATOR_DEPLOYER", "FeeDistributionCalculatorDeployer"));
        nftModifierDeployer = FederationNFTModifierDeployer(_existing("FEDERATION_NFT_MODIFIER_DEPLOYER", "FederationNFTModifierDeployer"));
        walletRegistryDeployer = ProjectWalletRegistryDeployer(_existing("PROJECT_WALLET_REGISTRY_DEPLOYER", "ProjectWalletRegistryDeployer"));
        feeSystemDeployer = FeeSystemDeployer(_existing("FEE_SYSTEM_DEPLOYER", "FeeSystemDeployer"));
        federationDeployer = FederationDeployer(_existing("FEDERATION_DEPLOYER", "FederationDeployer"));
        confederationDeployer = ConfederationDeployer(_existing("CONFEDERATION_DEPLOYER", "ConfederationDeployer"));
        dkgCoordinator = DKGCoordinator(_existing("DKG_COORDINATOR", "DKGCoordinator"));
        frostVerifier = FROSTVerifier(_existing("FROST_VERIFIER", "FROSTVerifier"));
        frostCoordinator = FROSTCoordinator(_existing("FROST_COORDINATOR", "FROSTCoordinator"));
        globalWalletRegistry = GlobalProjectWalletRegistry(payable(_existing("GLOBAL_PROJECT_WALLET_REGISTRY", "GlobalProjectWalletRegistry")));
    }

    function _owned(address child, address sender) internal view {
        if (child != address(0)) require(ControlledDeployer(child).deployerOwner() == sender, "deployer owner mismatch");
    }

    function _checkBase(address sender, address owner, address[] memory wallets, uint256[] memory shares, string[] memory labels) internal view {
        _owned(address(frostAuthorizationDeployer), sender); _owned(address(shareTokenDeployer), sender);
        _owned(address(simpleMultiPoolDeployer), sender); _owned(address(confederationTokenDeployer), sender);
        _owned(address(multiPoolDeployer), sender); _owned(address(frostCustodianDeployer), sender);
        _owned(address(feeManagerDeployer), sender); _owned(address(feeDistributorDeployer), sender);
        _owned(address(nftModifierDeployer), sender); _owned(address(walletRegistryDeployer), sender);
        _owned(address(feeSystemDeployer), sender); _owned(address(federationDeployer), sender);
        _owned(address(confederationDeployer), sender);
        if (address(simpleMultiPoolDeployer) != address(0)) require(
            address(simpleMultiPoolDeployer.frostDeployer()) == address(frostAuthorizationDeployer)
            && address(simpleMultiPoolDeployer.shareTokenDeployer()) == address(shareTokenDeployer), "simple deployer binding");
        if (address(multiPoolDeployer) != address(0)) require(address(multiPoolDeployer.shareTokenDeployer()) == address(confederationTokenDeployer), "conf token binding");
        if (address(feeSystemDeployer) != address(0)) require(
            address(feeSystemDeployer.feeManagerDeployer()) == address(feeManagerDeployer)
            && address(feeSystemDeployer.feeDistributorDeployer()) == address(feeDistributorDeployer)
            && address(feeSystemDeployer.nftModifierDeployer()) == address(nftModifierDeployer)
            && address(feeSystemDeployer.walletRegistryDeployer()) == address(walletRegistryDeployer), "fee deployer binding");
        if (address(federationDeployer) != address(0)) require(address(federationDeployer.multiPoolDeployer()) == address(simpleMultiPoolDeployer)
            && address(federationDeployer.feeSystemDeployer()) == address(feeSystemDeployer), "federation deployer binding");
        if (address(confederationDeployer) != address(0)) require(address(confederationDeployer.frostCustodianDeployer()) == address(frostCustodianDeployer)
            && address(confederationDeployer.multiPoolDeployer()) == address(multiPoolDeployer)
            && address(confederationDeployer.feeSystemDeployer()) == address(feeSystemDeployer), "confederation deployer binding");
        if (address(frostCoordinator) != address(0)) {
            require(address(dkgCoordinator) != address(0) && address(frostCoordinator.defaultVerifier()) == address(frostVerifier), "coordinator dependencies");
            address bound = address(frostCoordinator.dkgRegistry());
            require(bound == address(0) || bound == address(dkgCoordinator), "DKG registry mismatch");
            if (bound == address(0)) require(frostCoordinator.hasRole(frostCoordinator.ADMIN_ROLE(), sender), "registry binding permission");
        }
        if (address(globalWalletRegistry) != address(0)) {
            require(globalWalletRegistry.owner() == owner, "global wallet owner mismatch");
            GlobalProjectWalletRegistry.ProjectWallet[] memory configured = globalWalletRegistry.getWallets();
            require(configured.length == wallets.length, "global wallet count mismatch");
            for (uint256 i; i < wallets.length; ++i) require(configured[i].wallet == wallets[i] && configured[i].shareBps == shares[i]
                && keccak256(bytes(configured[i].label)) == keccak256(bytes(labels[i])), "global wallet configuration mismatch");
        }
    }

    function _permit(address child, address parent) internal {
        if (!ControlledDeployer(child).authorizedDeployers(parent)) ControlledDeployer(child).setAuthorizedDeployer(parent, true);
    }

    function deployBase() public {
        (uint256 key, address sender) = _context();
        (address owner, address[] memory wallets, uint256[] memory shares, string[] memory labels) = _wallets();
        _loadBase(); _checkBase(sender, owner, wallets, shares, labels);
        vm.startBroadcast(key);
        if (address(frostAuthorizationDeployer) == address(0)) frostAuthorizationDeployer = new FROSTAuthorizationDeployer();
        if (address(shareTokenDeployer) == address(0)) shareTokenDeployer = new MultiPoolShareTokenDeployer();
        if (address(simpleMultiPoolDeployer) == address(0)) simpleMultiPoolDeployer = new SimpleMultiPoolDeployer(address(frostAuthorizationDeployer), address(shareTokenDeployer));
        _permit(address(frostAuthorizationDeployer), address(simpleMultiPoolDeployer));
        _permit(address(shareTokenDeployer), address(simpleMultiPoolDeployer));
        if (address(confederationTokenDeployer) == address(0)) confederationTokenDeployer = new ConfederationTokenDeployer();
        if (address(multiPoolDeployer) == address(0)) multiPoolDeployer = new MultiPoolDeployer(address(confederationTokenDeployer));
        _permit(address(confederationTokenDeployer), address(multiPoolDeployer));
        if (address(frostCustodianDeployer) == address(0)) frostCustodianDeployer = new FROSTCustodianDeployer();
        if (address(feeManagerDeployer) == address(0)) feeManagerDeployer = new FederationFeeManagerDeployer();
        if (address(feeDistributorDeployer) == address(0)) feeDistributorDeployer = new FeeDistributionCalculatorDeployer();
        if (address(nftModifierDeployer) == address(0)) nftModifierDeployer = new FederationNFTModifierDeployer();
        if (address(walletRegistryDeployer) == address(0)) walletRegistryDeployer = new ProjectWalletRegistryDeployer();
        if (address(feeSystemDeployer) == address(0)) feeSystemDeployer = new FeeSystemDeployer(address(feeManagerDeployer),address(feeDistributorDeployer),address(nftModifierDeployer),address(walletRegistryDeployer));
        _permit(address(feeManagerDeployer), address(feeSystemDeployer));
        _permit(address(feeDistributorDeployer), address(feeSystemDeployer));
        _permit(address(nftModifierDeployer), address(feeSystemDeployer));
        _permit(address(walletRegistryDeployer), address(feeSystemDeployer));
        if (address(federationDeployer) == address(0)) federationDeployer = new FederationDeployer(address(simpleMultiPoolDeployer), address(feeSystemDeployer));
        _permit(address(simpleMultiPoolDeployer), address(federationDeployer));
        if (address(confederationDeployer) == address(0)) confederationDeployer = new ConfederationDeployer(address(frostCustodianDeployer), address(multiPoolDeployer), address(feeSystemDeployer));
        _permit(address(frostCustodianDeployer), address(confederationDeployer));
        _permit(address(multiPoolDeployer), address(confederationDeployer));
        if (address(dkgCoordinator) == address(0)) dkgCoordinator = new DKGCoordinator();
        if (address(frostVerifier) == address(0)) frostVerifier = new FROSTVerifier();
        if (address(frostCoordinator) == address(0)) frostCoordinator = new FROSTCoordinator(address(frostVerifier));
        if (address(frostCoordinator.dkgRegistry()) == address(0)) frostCoordinator.bindDKGRegistry(address(dkgCoordinator));
        require(address(frostCoordinator.dkgRegistry()) == address(dkgCoordinator), "DKG registry mismatch");
        bytes32 admin = frostCoordinator.ADMIN_ROLE();
        if (frostCoordinator.hasRole(admin, sender)) frostCoordinator.renounceRole(admin, sender);
        require(!frostCoordinator.hasRole(admin, sender), "admin role retained");
        if (address(globalWalletRegistry) == address(0)) globalWalletRegistry = new GlobalProjectWalletRegistry(owner, wallets, shares, labels);
        vm.stopBroadcast();
        console.log("Base complete: use confirmed manifest addresses for the next stage.");
        console.log("DKGCoordinator:", address(dkgCoordinator));
        console.log("FROSTCoordinator:", address(frostCoordinator));
        console.log("FROSTAuthorizationDeployer:", address(frostAuthorizationDeployer));
        console.log("FederationDeployer:", address(federationDeployer));
        console.log("ConfederationDeployer:", address(confederationDeployer));
        console.log("GlobalProjectWalletRegistry:", address(globalWalletRegistry));
    }

    function deployFactories() public {
        (uint256 key, address sender) = _context();
        dkgCoordinator = DKGCoordinator(vm.envAddress("DKG_COORDINATOR"));
        frostCoordinator = FROSTCoordinator(vm.envAddress("FROST_COORDINATOR"));
        federationDeployer = FederationDeployer(vm.envAddress("FEDERATION_DEPLOYER"));
        confederationDeployer = ConfederationDeployer(vm.envAddress("CONFEDERATION_DEPLOYER"));
        require(address(dkgCoordinator).code.length > 0 && address(frostCoordinator).code.length > 0
            && address(federationDeployer).code.length > 0 && address(confederationDeployer).code.length > 0, "base infrastructure missing");
        _requireArtifact(address(dkgCoordinator), "DKGCoordinator");
        _checkCoordinatorArtifact(frostCoordinator);
        _requireArtifact(address(federationDeployer), "FederationDeployer");
        _requireArtifact(address(confederationDeployer), "ConfederationDeployer");
        require(address(frostCoordinator.dkgRegistry()) == address(dkgCoordinator), "DKG registry mismatch");
        require(!frostCoordinator.hasRole(frostCoordinator.ADMIN_ROLE(), sender), "admin role retained");
        _owned(address(federationDeployer), sender); _owned(address(confederationDeployer), sender);
        federationFactory = FederationFactory(_existing("FEDERATION_FACTORY", "FederationFactory"));
        confederationFactory = ConfederationFactory(_existing("CONFEDERATION_FACTORY", "ConfederationFactory"));
        if (address(federationFactory) != address(0)) require(address(federationFactory.dkgCoordinator()) == address(dkgCoordinator)
            && address(federationFactory.deployer()) == address(federationDeployer) && federationFactory.frostCoordinator() == address(frostCoordinator), "federation factory mismatch");
        if (address(confederationFactory) != address(0)) require(address(federationFactory) != address(0)
            && address(confederationFactory.federationFactory()) == address(federationFactory)
            && address(confederationFactory.dkgCoordinator()) == address(dkgCoordinator)
            && address(confederationFactory.deployer()) == address(confederationDeployer)
            && confederationFactory.frostCoordinator() == address(frostCoordinator), "confederation factory mismatch");
        vm.startBroadcast(key);
        if (address(federationFactory) == address(0)) federationFactory = new FederationFactory(address(dkgCoordinator),address(federationDeployer),address(frostCoordinator));
        _permit(address(federationDeployer), address(federationFactory));
        if (address(confederationFactory) == address(0)) confederationFactory = new ConfederationFactory(address(federationFactory),address(dkgCoordinator),address(confederationDeployer),address(frostCoordinator));
        _permit(address(confederationDeployer), address(confederationFactory));
        vm.stopBroadcast();
        console.log("FederationFactory:", address(federationFactory));
        console.log("ConfederationFactory:", address(confederationFactory));
    }
}
