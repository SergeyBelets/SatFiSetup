// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./FeeDistributionCalculator.sol";
import "./ControlledDeployer.sol";

/**
 * @title FeeDistributionCalculatorDeployer
 * @notice Deploys one FeeDistributionCalculator per call.
 */
contract FeeDistributionCalculatorDeployer is ControlledDeployer {
    event FeeDistributionCalculatorDeployed(address indexed feeDistributor);

    function deployFeeDistributionCalculator(
        address frostMultisig,
        FeeDistributionCalculator.DistributionConfig calldata config
    ) external onlyAuthorizedDeployer returns (address feeDistributor) {
        feeDistributor = address(new FeeDistributionCalculator(frostMultisig, config));
        emit FeeDistributionCalculatorDeployed(feeDistributor);
    }
}
