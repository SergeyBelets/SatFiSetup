// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {DeployFederation} from "../script/DeployFederation.s.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract StageRewardToken is ERC20 {
    constructor() ERC20("Explicit stage reward", "ESR") {}
    function mint(address recipient, uint256 amount) external { _mint(recipient, amount); }
}

abstract contract DeploymentStageFixture is ConfederationEpochFixture {
    address internal constant REGISTRY_OWNER = address(0xA001);
    address internal constant PROJECT_FIRST = address(0xA002);
    address internal constant PROJECT_SECOND = address(0xA003);
    address internal constant PROJECT_TREASURY = address(0xA004);
    address internal broadcaster;
    DeployFederation internal baseInfrastructure;
    uint256 private scriptCount;

    // vm.setEnv is process-global and not reverted with Foundry's per-test EVM snapshot.
    // Run script suites with --threads 1 and restore their explicit inputs at each test entry.
    modifier freshScriptEnvironment() { _restoreScriptEnvironment(); _; }

    function _restoreScriptEnvironment() internal virtual { _resetScriptEnvironment(); }

    function _stageSetUp() internal {
        _baseSetUp();
        broadcaster = vm.addr(1);
        vm.deal(broadcaster, 1000 ether);
        _resetScriptEnvironment();
    }

    function _resetScriptEnvironment() internal {
        vm.setEnv("EXPECTED_CHAIN_ID", "31337");
        vm.setEnv("PRIVATE_KEY", "1");
        _walletEnvironment();
        string[19] memory names = ["FROST_AUTHORIZATION_DEPLOYER", "MULTIPOOL_SHARE_TOKEN_DEPLOYER",
            "SIMPLE_MULTIPOOL_DEPLOYER", "CONFEDERATION_TOKEN_DEPLOYER", "MULTIPOOL_DEPLOYER",
            "FROST_CUSTODIAN_DEPLOYER", "FEDERATION_FEE_MANAGER_DEPLOYER", "FEE_DISTRIBUTION_CALCULATOR_DEPLOYER",
            "FEDERATION_NFT_MODIFIER_DEPLOYER", "PROJECT_WALLET_REGISTRY_DEPLOYER", "FEE_SYSTEM_DEPLOYER",
            "FEDERATION_DEPLOYER", "CONFEDERATION_DEPLOYER", "DKG_COORDINATOR", "FROST_VERIFIER",
            "FROST_COORDINATOR", "GLOBAL_PROJECT_WALLET_REGISTRY", "FEDERATION_FACTORY", "CONFEDERATION_FACTORY"];
        for (uint256 i; i < names.length; ++i) _envAddress(names[i], address(0));
        _envAddress("PROJECT_AUTHORIZATION", address(0));
        _envAddress("PROJECT_INCOME_VAULT", address(0));
        _envAddress("DEPLOYMENT_FEE_ROUTER", address(0));
    }

    function _walletEnvironment() internal {
        _envAddress("PROJECT_REGISTRY_OWNER", REGISTRY_OWNER);
        _envAddress("PROJECT_WALLET_1", PROJECT_FIRST);
        _envAddress("PROJECT_WALLET_2", PROJECT_SECOND);
        vm.setEnv("PROJECT_WALLET_SHARES", "1234,2345");
        vm.setEnv("PROJECT_WALLET_LABELS", "Operations,Community");
    }

    /// @dev Solidity scripts execute locally, outside production EIP-170/EIP-3860.
    /// Etch only the local script scaffold; all production child deployments are real CREATE calls.
    function _script() internal returns (DeployFederation script) {
        script = DeployFederation(address(uint160(0xD000 + ++scriptCount)));
        vm.etch(address(script), type(DeployFederation).runtimeCode);
    }

    function _envAddress(string memory name, address value) internal { vm.setEnv(name, vm.toString(value)); }

    function _saveBase(DeployFederation script) internal {
        _envAddress("FROST_AUTHORIZATION_DEPLOYER", address(script.frostAuthorizationDeployer()));
        _envAddress("MULTIPOOL_SHARE_TOKEN_DEPLOYER", address(script.shareTokenDeployer()));
        _envAddress("SIMPLE_MULTIPOOL_DEPLOYER", address(script.simpleMultiPoolDeployer()));
        _envAddress("CONFEDERATION_TOKEN_DEPLOYER", address(script.confederationTokenDeployer()));
        _envAddress("MULTIPOOL_DEPLOYER", address(script.multiPoolDeployer()));
        _envAddress("FROST_CUSTODIAN_DEPLOYER", address(script.frostCustodianDeployer()));
        _envAddress("FEDERATION_FEE_MANAGER_DEPLOYER", address(script.feeManagerDeployer()));
        _envAddress("FEE_DISTRIBUTION_CALCULATOR_DEPLOYER", address(script.feeDistributorDeployer()));
        _envAddress("FEDERATION_NFT_MODIFIER_DEPLOYER", address(script.nftModifierDeployer()));
        _envAddress("PROJECT_WALLET_REGISTRY_DEPLOYER", address(script.walletRegistryDeployer()));
        _envAddress("FEE_SYSTEM_DEPLOYER", address(script.feeSystemDeployer()));
        _envAddress("FEDERATION_DEPLOYER", address(script.federationDeployer()));
        _envAddress("CONFEDERATION_DEPLOYER", address(script.confederationDeployer()));
        _envAddress("DKG_COORDINATOR", address(script.dkgCoordinator()));
        _envAddress("FROST_VERIFIER", address(script.frostVerifier()));
        _envAddress("FROST_COORDINATOR", address(script.frostCoordinator()));
        _envAddress("GLOBAL_PROJECT_WALLET_REGISTRY", address(script.globalWalletRegistry()));
    }

    function _saveFactories(DeployFederation script) internal {
        _envAddress("FEDERATION_FACTORY", address(script.federationFactory()));
        _envAddress("CONFEDERATION_FACTORY", address(script.confederationFactory()));
    }

    function _baseAndFactories() internal returns (DeployFederation script) {
        DeployFederation base = _script();
        base.deployBase();
        baseInfrastructure = base;
        _saveBase(base);
        dkg = base.dkgCoordinator();
        coordinator = EpochCoordinatorFixture(address(base.frostCoordinator()));
        script = _script();
        script.deployFactories();
        _saveFactories(script);
    }
}
