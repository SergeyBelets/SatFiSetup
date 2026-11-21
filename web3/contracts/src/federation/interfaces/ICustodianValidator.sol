// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title ICustodianValidator
 * @notice Interface for validating custodian signatures in confederation operations
 * @dev Custodians are institutional entities that must approve confederation withdrawals
 */
interface ICustodianValidator {
    /// @notice Emitted when a custodian signs an operation
    event CustodianSigned(bytes32 indexed operationHash, address indexed custodian);

    /// @notice Emitted when a new custodian is added
    event CustodianAdded(address indexed custodian);

    /// @notice Emitted when a custodian is removed
    event CustodianRemoved(address indexed custodian);

    /// @notice Emitted when minimum custodians requirement is changed
    event MinCustodiansChanged(uint256 newMinimum);

    /**
     * @notice Register a custodian's signature for an operation
     * @param operationHash The hash of the operation being signed
     */
    function registerCustodianSignature(bytes32 operationHash) external;

    /**
     * @notice Count how many custodians have signed an operation
     * @param operationHash The hash of the operation
     * @return The number of custodian signatures
     */
    function countCustodianSignatures(bytes32 operationHash) external view returns (uint256);

    /**
     * @notice Check if the minimum custodian threshold is met for an operation
     * @param operationHash The hash of the operation
     * @return True if minimum custodians have signed
     */
    function meetsMinimumCustodians(bytes32 operationHash) external view returns (bool);

    /**
     * @notice Validate that custodian threshold is met (alias for meetsMinimumCustodians)
     * @param operationHash The hash of the operation
     * @return True if threshold is met
     */
    function validateCustodianThreshold(bytes32 operationHash) external view returns (bool);

    /**
     * @notice Check if an address is a registered custodian
     * @param account The address to check
     * @return True if the address is a custodian
     */
    function isCustodian(address account) external view returns (bool);

    /**
     * @notice Get all registered custodians
     * @return Array of custodian addresses
     */
    function getCustodians() external view returns (address[] memory);

    /**
     * @notice Get the minimum number of custodians required
     * @return The minimum custodian count
     */
    function getMinCustodiansRequired() external view returns (uint256);

    /**
     * @notice Get the total number of custodians
     * @return The custodian count
     */
    function getCustodianCount() external view returns (uint256);

    /**
     * @notice Check if a custodian has signed an operation
     * @param operationHash The hash of the operation
     * @param custodian The custodian address
     * @return True if the custodian has signed
     */
    function hasCustodianSigned(bytes32 operationHash, address custodian) external view returns (bool);

    /**
     * @notice Add a new custodian (requires FROST and custodian approval)
     * @param custodian The address to add as custodian
     * @param approvalHash The operation hash that was approved
     */
    function addCustodian(address custodian, bytes32 approvalHash) external;

    /**
     * @notice Remove a custodian (requires FROST and custodian approval)
     * @param custodian The address to remove
     * @param approvalHash The operation hash that was approved
     */
    function removeCustodian(address custodian, bytes32 approvalHash) external;

    /**
     * @notice Set minimum custodians required (requires FROST and custodian approval)
     * @param minRequired The new minimum
     * @param approvalHash The operation hash that was approved
     */
    function setMinCustodians(uint256 minRequired, bytes32 approvalHash) external;
}
