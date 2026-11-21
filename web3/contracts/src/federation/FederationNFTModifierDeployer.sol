// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../tokens/FederationNFTModifier.sol";
import "./ControlledDeployer.sol";

/**
 * @title FederationNFTModifierDeployer
 * @notice Deploys one FederationNFTModifier per call.
 */
contract FederationNFTModifierDeployer is ControlledDeployer {
    event FederationNFTModifierDeployed(address indexed nftModifier, address indexed admin);

    function deployFederationNFTModifier(string calldata name, string calldata symbol, address admin)
        external
        onlyAuthorizedDeployer
        returns (address nftModifier)
    {
        nftModifier = address(new FederationNFTModifier(name, symbol, admin));
        emit FederationNFTModifierDeployed(nftModifier, admin);
    }
}
