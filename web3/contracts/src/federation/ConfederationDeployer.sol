// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./FROSTCustodianDeployer.sol";
import "./MultiPoolDeployer.sol";
import "./FeeSystemDeployer.sol";
import "./ControlledDeployer.sol";

/**
 * @title ConfederationDeployer
 * @notice Orchestrates deployment of confederation contracts via sub-deployers
 * @dev Uses FROSTCustodianDeployer and MultiPoolDeployer to stay within bytecode limits
 */
contract ConfederationDeployer is ControlledDeployer {
    /// @notice Deployer for FROST multisig and custodian validator
    FROSTCustodianDeployer public immutable frostCustodianDeployer;

    /// @notice Deployer for multi-pool
    MultiPoolDeployer public immutable multiPoolDeployer;

    /// @notice Deployer for fee system
    FeeSystemDeployer public immutable feeSystemDeployer;

    event ConfederationContractsDeployed(
        string indexed confederationId,
        address frostMultisig,
        address custodianValidator,
        address multiPool,
        address shareToken,
        address indexed caller
    );

    constructor(address _frostCustodianDeployer, address _multiPoolDeployer, address _feeSystemDeployer) {
        require(_frostCustodianDeployer != address(0), "Invalid FROST deployer");
        require(_multiPoolDeployer != address(0), "Invalid MultiPool deployer");
        require(_feeSystemDeployer != address(0), "Invalid FeeSystem deployer");
        frostCustodianDeployer = FROSTCustodianDeployer(_frostCustodianDeployer);
        multiPoolDeployer = MultiPoolDeployer(_multiPoolDeployer);
        feeSystemDeployer = FeeSystemDeployer(_feeSystemDeployer);
    }

    /**
     * @notice Deploy confederation contracts
     * @param confederationId Confederation identifier
     * @param allSigners All signers for FROST multisig
     * @param totalThreshold Required FROST signature threshold
     * @param custodianAddresses Custodian addresses
     * @param minCustodians Minimum custodian signatures required
     * @param shareTokenName Name for the share token
     * @param shareTokenSymbol Symbol for the share token
     * @return frostMultisig The deployed FROST multisig address
     * @return custodianValidator The deployed custodian validator address
     * @return multiPool The deployed multipool address
     * @return shareToken The deployed share token address
     */
    function deployConfederationContracts(
        string calldata confederationId,
        address[] calldata allSigners,
        uint256 totalThreshold,
        address[] calldata custodianAddresses,
        uint256 minCustodians,
        string calldata shareTokenName,
        string calldata shareTokenSymbol,
        address coordinator,
        bytes calldata groupPubkey
    )
        external
        onlyAuthorizedDeployer
        returns (address frostMultisig, address custodianValidator, address multiPool, address shareToken)
    {
        // Deploy FROST multisig and custodian validator via sub-deployer
        (frostMultisig, custodianValidator) = frostCustodianDeployer.deployFROSTAndCustodian(
            allSigners, totalThreshold, custodianAddresses, minCustodians, coordinator, groupPubkey
        );

        // Deploy multi-pool via sub-deployer
        (multiPool, shareToken) = multiPoolDeployer.deployMultiPool(
            confederationId, frostMultisig, custodianValidator, shareTokenName, shareTokenSymbol
        );

        emit ConfederationContractsDeployed(
            confederationId, frostMultisig, custodianValidator, multiPool, shareToken, msg.sender
        );
    }

    function deployConfederationContractsV1(
        string calldata confederationId,
        address[] calldata allSigners,
        uint256 totalThreshold,
        address[] calldata custodianAddresses,
        uint256 minCustodians,
        string calldata shareTokenName,
        string calldata shareTokenSymbol,
        address coordinator,
        bytes calldata groupPubkey,
        bytes32 dkgId
    )
        external
        onlyAuthorizedDeployer
        returns (address frostMultisig, address custodianValidator, address multiPool, address shareToken)
    {
        // Deploy FROST multisig and custodian validator via sub-deployer
        (frostMultisig, custodianValidator) = frostCustodianDeployer.deployFROSTAndCustodianV1(
            allSigners, totalThreshold, custodianAddresses, minCustodians, coordinator, groupPubkey, dkgId
        );

        // Deploy multi-pool via sub-deployer
        (multiPool, shareToken) = multiPoolDeployer.deployMultiPool(
            confederationId, frostMultisig, custodianValidator, shareTokenName, shareTokenSymbol
        );

        emit ConfederationContractsDeployed(
            confederationId, frostMultisig, custodianValidator, multiPool, shareToken, msg.sender
        );
    }
}
