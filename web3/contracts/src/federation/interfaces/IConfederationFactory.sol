// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IConfederationFactory
 * @notice Interface for creating and managing confederations
 * @dev A confederation is a group of federations + custodians with hybrid FROST model
 */
interface IConfederationFactory {
    /// @notice Federation member in a confederation
    struct FederationMember {
        string federationId;
        address representative;
        address shareToken;
    }

    /// @notice Custodian member in a confederation
    struct CustodianMember {
        string custodianId;
        address signerAddress;
        bytes custodianPubkey;
        bool isInstitutional;
    }

    /// @notice Confederation configuration
    struct ConfederationConfig {
        string confederationId;
        string[] federationIds;
        uint256 federationCount;
        uint256 custodianCount;
        uint256 totalThreshold;
        uint256 minCustodians;
        bytes groupPubkey;
        address frostMultisig;
        address custodianValidator;
        address multiPool;
        address shareToken;
        address confederationToken;
        address feeManager;
        address nftModifier;
        address walletRegistry;
        uint32 spvConfirmationDepth;
        uint256 createdAt;
        bool isActive;
    }

    struct CustodianDAOConfig {
        address token;
        address staking;
        address controller;
        address twap;
        address incomeVault;
        address decisions;
        address settlement;
    }

    event CustodianDAORegistered(string confederationId, address indexed settlement, address indexed staking);

    function getCustodianDAO(string calldata confederationId) external view returns (CustodianDAOConfig memory);
    function getRegisterCustodianDAOOperationHash(string calldata confederationId, CustodianDAOConfig calldata dao)
        external view returns (bytes32);
    function registerCustodianDAO(string calldata confederationId, CustodianDAOConfig calldata dao, bytes32 operationHash)
        external;

    /// @notice Emitted when confederation DKG is initiated
    event ConfederationDKGInitiated(
        bytes32 indexed sessionId,
        string confederationId,
        uint256 totalThreshold,
        uint256 minCustodians
    );

    /// @notice Emitted when a confederation is deployed
    event ConfederationDeployed(
        string indexed confederationId,
        address frostMultisig,
        address custodianValidator,
        address multiPool,
        address shareToken
    );

    /// @notice Emitted when a confederation is deactivated
    event ConfederationDeactivated(string indexed confederationId);

    /**
     * @notice Initiate confederation creation
     * @param federationIds Array of federation IDs to include
     * @param custodianMembers Array of custodian members
     * @param totalThreshold Total signature threshold
     * @param minCustodians Minimum custodian signatures required
     * @param confederationId Unique confederation identifier
     * @return sessionId The DKG session ID
     */
    function initiateConfederation(
        string[] calldata federationIds,
        CustodianMember[] calldata custodianMembers,
        uint256 totalThreshold,
        uint256 minCustodians,
        string calldata confederationId
    ) external returns (bytes32 sessionId);

    /// @notice Adopt an EVM_AUTH DKG whose founders are the ordered representatives of the federations.
    function adoptConfederationDKG(
        bytes32 sessionId,
        string[] calldata federationIds,
        CustodianMember[] calldata custodianMembers,
        uint256 totalThreshold,
        uint256 minCustodians,
        string calldata confederationId
    ) external;

    /// @notice Adopt an EVM_AUTH DKG containing every member of one active federation, then custodians.
    /// @dev Creates a new V1 confederation; the source federation and its assets remain unchanged.
    function adoptConfederationFromFederation(
        bytes32 sessionId,
        string calldata federationId,
        CustodianMember[] calldata custodians,
        uint256 totalThreshold,
        uint256 minCustodians,
        string calldata confederationId
    ) external;

    /// @notice All founders of the immutable DKG epoch, in the original roster order.
    function getFounders(string calldata confederationId) external view returns (address[] memory);

    /**
     * @notice Submit inter-federation commitment for DKG
     * @param sessionId The DKG session ID
     * @param federationId The federation submitting
     * @param commitment The commitment hash
     */
    function submitInterFederationCommitment(
        bytes32 sessionId,
        string calldata federationId,
        bytes32 commitment
    ) external;

    /**
     * @notice Deploy confederation contracts after DKG completion
     * @param sessionId The DKG session ID
     * @param shareTokenName Name for the share token
     * @param shareTokenSymbol Symbol for the share token
     * @return frostMultisig The deployed FROST multisig address
     * @return custodianValidator The deployed custodian validator address
     * @return multiPool The deployed multipool address
     * @return shareToken The deployed share token address
     */
    function deployConfederation(
        bytes32 sessionId,
        string calldata shareTokenName,
        string calldata shareTokenSymbol
    ) external returns (
        address frostMultisig,
        address custodianValidator,
        address multiPool,
        address shareToken
    );

    /**
     * @notice Deactivate a confederation (requires FROST + custodian threshold)
     * @param confederationId The confederation to deactivate
     * @param operationHash The operation hash signed by FROST and custodians
     */
    function deactivateConfederation(string calldata confederationId, bytes32 operationHash) external;

    /**
     * @notice Get confederation configuration
     * @param confederationId The confederation ID
     * @return The confederation config
     */
    function getConfederation(string calldata confederationId) external view returns (ConfederationConfig memory);

    /**
     * @notice Get federation members of a confederation
     * @param confederationId The confederation ID
     * @return Array of federation members
     */
    function getFederationMembers(string calldata confederationId) external view returns (FederationMember[] memory);

    /**
     * @notice Get custodian members of a confederation
     * @param confederationId The confederation ID
     * @return Array of custodian members
     */
    function getCustodianMembers(string calldata confederationId) external view returns (CustodianMember[] memory);

    /**
     * @notice Get all confederation IDs
     * @return Array of all confederation IDs
     */
    function getAllConfederations() external view returns (string[] memory);

    /**
     * @notice Get the number of confederations
     * @return Count of confederations
     */
    function getConfederationCount() external view returns (uint256);

    /**
     * @notice Check if a confederation exists
     * @param confederationId The confederation ID
     * @return True if confederation exists
     */
    function confederationExists(string calldata confederationId) external view returns (bool);

    /**
     * @notice Check if a confederation is active
     * @param confederationId The confederation ID
     * @return True if confederation is active
     */
    function isConfederationActive(string calldata confederationId) external view returns (bool);

    /**
     * @notice Get the federation factory address
     * @return The federation factory contract address
     */
    function getFederationFactory() external view returns (address);

    /**
     * @notice Get the DKG coordinator address
     * @return The DKG coordinator contract address
     */
    function getDKGCoordinator() external view returns (address);
}
