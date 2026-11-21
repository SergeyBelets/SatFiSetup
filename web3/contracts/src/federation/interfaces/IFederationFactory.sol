// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IFederationFactory
 * @notice Interface for creating and managing federations
 * @dev A federation is a group of users/miners with shared FROST multisig
 */
interface IFederationFactory {
    /// @notice Federation configuration
    struct FederationConfig {
        string federationId;
        address[] members;
        uint256 threshold;
        bytes groupPubkey;
        address frostMultisig;
        address multiPool;
        address shareToken;
        address feeManager;
        address nftModifier;
        address walletRegistry;
        uint32 spvConfirmationDepth;
        uint256 createdAt;
        bool isActive;
    }

    /// @notice Emitted when DKG is initiated for a new federation
    event DKGInitiated(bytes32 indexed sessionId, string federationId, uint256 threshold);

    /// @notice Emitted when a federation is deployed
    event FederationDeployed(
        string indexed federationId,
        address frostMultisig,
        address multiPool,
        address shareToken
    );

    /// @notice Emitted when a federation is deactivated
    event FederationDeactivated(string indexed federationId);

    /**
     * @notice Initiate DKG for a new federation
     * @param participants Array of participant addresses
     * @param threshold Required signature threshold
     * @param federationId Unique federation identifier
     * @return sessionId The DKG session ID
     */
    function initiateFederationDKG(
        address[] calldata participants,
        uint256 threshold,
        string calldata federationId
    ) external returns (bytes32 sessionId);

    /**
     * @notice Deploy federation contracts after DKG completion
     * @param sessionId The DKG session ID
     * @param shareTokenName Name for the share token
     * @param shareTokenSymbol Symbol for the share token
     * @return frostMultisig The deployed FROST multisig address
     * @return multiPool The deployed multipool address
     * @return shareToken The deployed share token address
     */
    function deployFederation(
        bytes32 sessionId,
        string calldata shareTokenName,
        string calldata shareTokenSymbol
    ) external returns (
        address frostMultisig,
        address multiPool,
        address shareToken
    );

    /**
     * @notice Deactivate a federation (requires FROST threshold approval)
     * @param federationId The federation to deactivate
     * @param operationHash The FROST-signed operation hash
     */
    function deactivateFederation(string calldata federationId, bytes32 operationHash) external;

    /**
     * @notice Get federation configuration
     * @param federationId The federation ID
     * @return The federation config
     */
    function getFederation(string calldata federationId) external view returns (FederationConfig memory);

    /**
     * @notice Get all federations a user is member of
     * @param user The user address
     * @return Array of federation IDs
     */
    function getUserFederations(address user) external view returns (string[] memory);

    /**
     * @notice Get all federation IDs
     * @return Array of all federation IDs
     */
    function getAllFederations() external view returns (string[] memory);

    /**
     * @notice Get the number of federations
     * @return Count of federations
     */
    function getFederationCount() external view returns (uint256);

    /**
     * @notice Check if a federation exists
     * @param federationId The federation ID
     * @return True if federation exists
     */
    function federationExists(string calldata federationId) external view returns (bool);

    /**
     * @notice Check if a federation is active
     * @param federationId The federation ID
     * @return True if federation is active
     */
    function isFederationActive(string calldata federationId) external view returns (bool);

    /**
     * @notice Get the DKG coordinator address
     * @return The DKG coordinator contract address
     */
    function getDKGCoordinator() external view returns (address);
}
