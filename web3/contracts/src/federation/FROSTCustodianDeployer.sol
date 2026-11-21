// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./CustodianValidator.sol";
import "../FROSTAuthorization.sol";
import "./ControlledDeployer.sol";
import "./DKGAuthorization.sol";

/**
 * @title FROSTCustodianDeployer
 * @notice Deploys FROST multisig and CustodianValidator contracts
 * @dev Separated from ConfederationDeployer to reduce contract size
 */
contract FROSTCustodianDeployer is ControlledDeployer {
    event FROSTCustodianDeployed(address frostMultisig, address custodianValidator, address indexed caller);

    /**
     * @notice Deploy FROST multisig and custodian validator
     * @param allSigners All signers for FROST multisig
     * @param totalThreshold Required FROST signature threshold
     * @param custodianAddresses Custodian addresses
     * @param minCustodians Minimum custodian signatures required
     * @return frostMultisig The deployed FROST multisig address
     * @return custodianValidator The deployed custodian validator address
     */
    function deployFROSTAndCustodian(
        address[] calldata allSigners,
        uint256 totalThreshold,
        address[] calldata custodianAddresses,
        uint256 minCustodians,
        address coordinator,
        bytes calldata groupPubkey
    ) external onlyAuthorizedDeployer returns (address frostMultisig, address custodianValidator) {
        FROSTAuthorization frost = new FROSTAuthorization(coordinator, groupPubkey, allSigners, totalThreshold);
        frostMultisig = address(frost);

        // Deploy custodian validator
        CustodianValidator validator = new CustodianValidator(custodianAddresses, minCustodians, frostMultisig);
        custodianValidator = address(validator);

        emit FROSTCustodianDeployed(frostMultisig, custodianValidator, msg.sender);
    }

    function deployFROSTAndCustodianV1(
        address[] calldata allSigners,
        uint256 totalThreshold,
        address[] calldata custodianAddresses,
        uint256 minCustodians,
        address coordinator,
        bytes calldata groupPubkey,
        bytes32 dkgId
    ) external onlyAuthorizedDeployer returns (address frostMultisig, address custodianValidator) {
        FROSTAuthorization frost = new DKGAuthorization(coordinator, dkgId, groupPubkey, allSigners, totalThreshold);
        frostMultisig = address(frost);

        // Deploy custodian validator
        CustodianValidator validator = new CustodianValidator(custodianAddresses, minCustodians, frostMultisig);
        custodianValidator = address(validator);

        emit FROSTCustodianDeployed(frostMultisig, custodianValidator, msg.sender);
    }
}
