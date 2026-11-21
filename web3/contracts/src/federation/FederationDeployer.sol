// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../FROSTAuthorization.sol";
import "./SimpleMultiPoolDeployer.sol";
import "./FeeSystemDeployer.sol";
import "./IFeeManager.sol";
import "./FeeDistributionCalculator.sol";
import "./ControlledDeployer.sol";

/**
 * @title FederationDeployer
 * @notice Orchestrates deployment of federation contracts via sub-deployer
 * @dev Uses SimpleMultiPoolDeployer to stay within bytecode limits
 */
contract FederationDeployer is ControlledDeployer {
    /// @notice Deployer for multi-pool (which creates its own FROST and ShareToken)
    SimpleMultiPoolDeployer public immutable multiPoolDeployer;

    /// @notice Deployer for fee system
    FeeSystemDeployer public immutable feeSystemDeployer;

    event FederationContractsDeployed(
        string indexed federationId,
        address frostMultisig,
        address multiPool,
        address shareToken,
        address indexed caller
    );

    constructor(address _multiPoolDeployer, address _feeSystemDeployer) {
        require(_multiPoolDeployer != address(0), "Invalid MultiPool deployer");
        require(_feeSystemDeployer != address(0), "Invalid FeeSystem deployer");
        multiPoolDeployer = SimpleMultiPoolDeployer(_multiPoolDeployer);
        feeSystemDeployer = FeeSystemDeployer(_feeSystemDeployer);
    }

    /**
     * @notice Deploy federation contracts
     * @param federationId Federation identifier
     * @param members Federation member addresses
     * @param threshold Required signature threshold
     * @param shareTokenName Name for the share token
     * @param shareTokenSymbol Symbol for the share token
     * @return frostMultisig The deployed FROST multisig address (from SimpleMultiPool)
     * @return multiPool The deployed multipool address
     * @return shareToken The deployed share token address
     */
    function deployFederationContracts(
        string calldata federationId,
        address[] calldata members,
        uint256 threshold,
        string calldata shareTokenName,
        string calldata shareTokenSymbol,
        address coordinator,
        bytes calldata groupPubkey
    ) external onlyAuthorizedDeployer returns (address frostMultisig, address multiPool, address shareToken) {
        (multiPool, frostMultisig, shareToken) = multiPoolDeployer.deploySimpleMultiPool(
            federationId, members, threshold, shareTokenName, shareTokenSymbol, coordinator, groupPubkey
        );

        emit FederationContractsDeployed(federationId, frostMultisig, multiPool, shareToken, msg.sender);
    }

    function deployFederationContractsV1(
        string calldata federationId,
        address[] calldata members,
        uint256 threshold,
        string calldata shareTokenName,
        string calldata shareTokenSymbol,
        address coordinator,
        bytes calldata groupPubkey,
        bytes32 dkgId
    ) external onlyAuthorizedDeployer returns (address frostMultisig, address multiPool, address shareToken) {
        (multiPool, frostMultisig, shareToken) = multiPoolDeployer.deploySimpleMultiPoolV1(
            federationId, members, threshold, shareTokenName, shareTokenSymbol, coordinator, groupPubkey, dkgId
        );

        emit FederationContractsDeployed(federationId, frostMultisig, multiPool, shareToken, msg.sender);
    }
}
