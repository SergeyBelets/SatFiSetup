// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./FederationFeeManager.sol";
import "./ControlledDeployer.sol";

/**
 * @title FederationFeeManagerDeployer
 * @notice Deploys one FederationFeeManager per call.
 */
contract FederationFeeManagerDeployer is ControlledDeployer {
    event FederationFeeManagerDeployed(address indexed feeManager);

    function deployFederationFeeManager(
        address frostMultisig,
        address custodianValidator,
        address treasury,
        IFeeManager.FeeConfig calldata wrapConfig,
        IFeeManager.FeeConfig calldata unwrapConfig
    ) external onlyAuthorizedDeployer returns (address feeManager) {
        feeManager =
            address(new FederationFeeManager(frostMultisig, custodianValidator, treasury, wrapConfig, unwrapConfig));
        emit FederationFeeManagerDeployed(feeManager);
    }
}
