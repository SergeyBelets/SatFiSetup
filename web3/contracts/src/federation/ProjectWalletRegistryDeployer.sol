// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./ProjectWalletRegistry.sol";
import "./ControlledDeployer.sol";

/**
 * @title ProjectWalletRegistryDeployer
 * @notice Deploys one ProjectWalletRegistry per call.
 */
contract ProjectWalletRegistryDeployer is ControlledDeployer {
    event ProjectWalletRegistryDeployed(address indexed walletRegistry);

    function deployProjectWalletRegistry(
        address frostMultisig,
        address[] calldata wallets,
        uint256[] calldata shares,
        string[] calldata labels
    ) external onlyAuthorizedDeployer returns (address walletRegistry) {
        walletRegistry = address(new ProjectWalletRegistry(frostMultisig, wallets, shares, labels));
        emit ProjectWalletRegistryDeployed(walletRegistry);
    }
}
