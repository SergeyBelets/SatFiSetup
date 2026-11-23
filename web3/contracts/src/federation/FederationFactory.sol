// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./interfaces/IFederationFactory.sol";
import "./interfaces/IDKGCoordinator.sol";
import "./FederationDeployer.sol";
import "./DKGCoordinator.sol";
import "../FROSTAuthorization.sol";
import "./FederationFeeManager.sol";

interface IFederationFROSTCoordinator {
    function defaultVerifier() external view returns (address);
    function dkgRegistry() external view returns (address);
}

import "./CustodianDAOFeeRouter.sol";
/**
 * @title FederationFactory
 * @notice Factory for creating federations with FROST multisig
 * @dev A federation is a group of users/miners that share a FROST multisig.
 *      The factory manages the DKG process and deploys the required contracts.
 *      Uses FederationDeployer to reduce contract size.
 */
contract FederationFactory is IFederationFactory {
    using SafeERC20 for IERC20;
    address public immutable feeSetupAuthority=msg.sender;
    bool public deploymentFeeConfigured;
    bool public integrationFeeExemption;
    uint256 public deploymentFee;
    CustodianDAOFeeRouter public deploymentFeeRouter;
    function configureDeploymentFee(address router,uint256 amount,bool exemption) external {
        require(msg.sender==feeSetupAuthority && !deploymentFeeConfigured,"fee setup");
        if(exemption) require((block.chainid==31337 || block.chainid==11155111) && amount==0 && router==address(0),"integration exemption");
        else require(router.code.length>0 && amount>0,"fee config");
        deploymentFeeConfigured=true;integrationFeeExemption=exemption;deploymentFee=amount;deploymentFeeRouter=CustodianDAOFeeRouter(router);
    }
    function _collectDeploymentFee() private {
        require(deploymentFeeConfigured,"deployment fee not configured");
        if(integrationFeeExemption) return;
        IERC20 token=deploymentFeeRouter.rewardToken();uint256 beforeBalance=token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender,address(this),deploymentFee);
        require(token.balanceOf(address(this))==beforeBalance+deploymentFee,"fee transfer");
        token.forceApprove(address(deploymentFeeRouter),deploymentFee);
        deploymentFeeRouter.depositFeeV1(block.timestamp/1 days,CustodianDAOFeeRouter.FeeStream.Deploy,deploymentFee);
        token.forceApprove(address(deploymentFeeRouter),0);
    }
    mapping(bytes32=>bool) public v1Sessions;
    /// @notice Initiator may finish deployment using the reserved token metadata.
    mapping(bytes32 => address) public deploymentInitiator;
    // ======== STATE ========

    /// @notice DKG coordinator for managing DKG sessions
    IDKGCoordinator public immutable dkgCoordinator;

    /// @notice Deployer for federation contracts
    FederationDeployer public immutable deployer;

    /// @notice Canonical on-chain registry for all federation signing sessions.
    address public immutable frostCoordinator;

    /// @notice Federation configurations
    mapping(string => FederationConfig) private federations;

    /// @notice User to federation memberships
    mapping(address => string[]) private userFederations;

    /// @notice All federation IDs
    string[] private allFederationIds;

    /// @notice DKG session to federation ID mapping
    mapping(bytes32 => string) private sessionToFederation;

    /// @notice Federation ID to DKG session mapping
    mapping(string => bytes32) private federationToSession;

    struct ReservationTerms {
        bytes32 formationHash;
        bytes32 tokenHash;
    }

    /// @notice Admission binding for a V1 federation ID. Recovery preserves the reserved terms.
    mapping(string => bytes32) public reservedFederationSession;
    mapping(string => ReservationTerms) public reservedFederationTerms;
    mapping(bytes32 => bool) private reservedSessions;
    event FederationIdReserved(string indexed federationId, bytes32 indexed sessionId);
    event FederationReservationTransferred(string indexed federationId, bytes32 indexed previous, bytes32 indexed replacement);

    /// @notice Per-federation nonce for target-bound FROST operations
    mapping(string => uint256) private federationOperationNonces;

    // ======== ERRORS ========

    error FederationAlreadyExists();
    error FederationAlreadyDeployed();
    error FederationNotFound();
    error DKGNotComplete();
    error SessionNotFound();
    error NotAuthorized();
    error InvalidFederationId();
    error InvalidParticipants();
    error InvalidAddress();
    error LegacyNetworkDisabled();
    error InvalidFederationReservation();

    function _requireLegacyNetwork() private view {
        if (block.chainid != 31337 && block.chainid != 11155111) revert LegacyNetworkDisabled();
    }

    // ======== CONSTRUCTOR ========

    /**
     * @notice Create a federation factory
     * @param _dkgCoordinator Address of the DKG coordinator
     * @param _deployer Address of the federation deployer
     */
    constructor(address _dkgCoordinator, address _deployer, address _frostCoordinator) {
        if (_dkgCoordinator == address(0)) revert InvalidAddress();
        if (_deployer == address(0)) revert InvalidAddress();
        if (_frostCoordinator == address(0) || _frostCoordinator.code.length == 0) revert InvalidAddress();
        if (IFederationFROSTCoordinator(_frostCoordinator).defaultVerifier() == address(0)) revert InvalidAddress();
        dkgCoordinator = IDKGCoordinator(_dkgCoordinator);
        deployer = FederationDeployer(_deployer);
        frostCoordinator = _frostCoordinator;
    }

    // ======== MAIN FUNCTIONS ========

    /**
     * @notice Initiate DKG for a new federation
     * @param participants Array of participant addresses
     * @param threshold Required signature threshold
     * @param federationId Unique federation identifier
     * @return sessionId The DKG session ID
     */
    function initiateFederationDKG(address[] calldata participants, uint256 threshold, string calldata federationId)
        external
        returns (bytes32 sessionId)
    {
        _requireLegacyNetwork();
        if (msg.sender != feeSetupAuthority) revert NotAuthorized();
        if (bytes(federationId).length == 0) revert InvalidFederationId();
        if (federations[federationId].createdAt != 0 || reservedFederationSession[federationId] != 0) {
            revert FederationAlreadyExists();
        }
        if (participants.length < 2) revert InvalidParticipants();

        // Initiate DKG session
        sessionId = dkgCoordinator.initiateDKG(participants, threshold, federationId);
        if (reservedSessions[sessionId]) revert InvalidFederationReservation();

        // Store mapping
        sessionToFederation[sessionId] = federationId;
        federationToSession[federationId] = sessionId;
        deploymentInitiator[sessionId] = msg.sender;

        // Initialize federation config (not yet complete)
        federations[federationId].federationId = federationId;
        federations[federationId].members = participants;
        federations[federationId].threshold = threshold;
        federations[federationId].createdAt = block.timestamp;
        federations[federationId].isActive = false; // Will be activated after DKG

        // Track user memberships
        for (uint256 i = 0; i < participants.length; i++) {
            userFederations[participants[i]].push(federationId);
        }

        allFederationIds.push(federationId);

        emit DKGInitiated(sessionId, federationId, threshold);
    }

    /// @notice The factory deployer binds an unused ID to a DKG session, roster and token metadata.
    /// @dev May precede DKG completion. The exact retry is a no-op.
    function reserveFederationId(
        bytes32 sessionId, string calldata federationId, bytes32 formationHash, bytes32 tokenHash
    ) external {
        if (msg.sender != feeSetupAuthority) revert NotAuthorized();
        if (sessionId == 0 || bytes(federationId).length == 0 || formationHash == 0 || tokenHash == 0) {
            revert InvalidFederationReservation();
        }
        bytes32 previous = reservedFederationSession[federationId];
        if (previous == sessionId) {
            ReservationTerms storage terms = reservedFederationTerms[federationId];
            if (terms.formationHash != formationHash || terms.tokenHash != tokenHash) {
                revert InvalidFederationReservation();
            }
            return;
        }
        if (previous != 0 || federations[federationId].createdAt != 0) revert FederationAlreadyExists();
        if (reservedSessions[sessionId] || bytes(sessionToFederation[sessionId]).length != 0) {
            revert InvalidFederationReservation();
        }
        reservedFederationSession[federationId] = sessionId;
        reservedFederationTerms[federationId] = ReservationTerms(formationHash, tokenHash);
        reservedSessions[sessionId] = true;
        emit FederationIdReserved(federationId, sessionId);
    }

    /// @notice Move an unused reservation from a terminal ceremony to a replacement with the same signing terms.
    function transferFederationReservation(string calldata federationId, bytes32 previous, bytes32 replacement) external {
        if (msg.sender != feeSetupAuthority) revert NotAuthorized();
        if (previous == 0 || replacement == 0 || previous == replacement
            || reservedFederationSession[federationId] != previous || federations[federationId].createdAt != 0
            || !reservedSessions[previous] || bytes(sessionToFederation[previous]).length != 0
            || reservedSessions[replacement] || bytes(sessionToFederation[replacement]).length != 0) {
            revert InvalidFederationReservation();
        }
        _requireCanonicalDKG();
        DKGCoordinator(address(dkgCoordinator)).requireReservationTransfer(previous, replacement);

        reservedFederationSession[federationId] = replacement;
        reservedSessions[replacement] = true;
        emit FederationReservationTransferred(federationId, previous, replacement);
    }

    function _requireCanonicalDKG() private view {
        require(IFederationFROSTCoordinator(frostCoordinator).dkgRegistry() == address(dkgCoordinator), "DKG registry mismatch");
    }

    function adoptFederationDKG(bytes32 sessionId,string calldata federationId) external {
        _requireCanonicalDKG();
        if (sessionId == 0 || reservedFederationSession[federationId] != sessionId) {
            revert InvalidFederationReservation();
        }
        (, , ,bytes32 purpose, ,address[] memory participants,uint256 threshold)=DKGCoordinator(address(dkgCoordinator)).getCompletedResult(sessionId);
        require(purpose==keccak256("EVM_AUTH") && DKGCoordinator(address(dkgCoordinator)).isSessionParticipant(sessionId,msg.sender),"DKG admission");
        if (keccak256(abi.encode(participants, threshold)) != reservedFederationTerms[federationId].formationHash) {
            revert InvalidFederationReservation();
        }
        require(bytes(sessionToFederation[sessionId]).length==0,"DKG already used");
        v1Sessions[sessionId]=true;

        if (federations[federationId].createdAt != 0) revert FederationAlreadyExists();
        if (participants.length < 2) revert InvalidParticipants();

        // Store mapping
        sessionToFederation[sessionId] = federationId;
        federationToSession[federationId] = sessionId;
        deploymentInitiator[sessionId] = msg.sender;

        // Initialize federation config (not yet complete)
        federations[federationId].federationId = federationId;
        federations[federationId].members = participants;
        federations[federationId].threshold = threshold;
        federations[federationId].createdAt = block.timestamp;
        federations[federationId].isActive = false; // Will be activated after DKG

        // Track user memberships
        for (uint256 i = 0; i < participants.length; i++) {
            userFederations[participants[i]].push(federationId);
        }

        allFederationIds.push(federationId);

        emit DKGInitiated(sessionId, federationId, threshold);
    }

    /**
     * @notice Deploy federation contracts after DKG completion
     * @param sessionId The DKG session ID
     * @param shareTokenName Name for the share token
     * @param shareTokenSymbol Symbol for the share token
     * @return frostMultisig The deployed FROST multisig address
     * @return multiPool The deployed multipool address
     * @return shareToken The deployed share token address
     */
    function deployFederation(bytes32 sessionId, string calldata shareTokenName, string calldata shareTokenSymbol)
        external
        returns (address frostMultisig, address multiPool, address shareToken)
    {
        if (!v1Sessions[sessionId]) _requireLegacyNetwork();
        string storage federationId = sessionToFederation[sessionId];
        if (bytes(federationId).length == 0) revert SessionNotFound();

        // Check DKG is complete
        IDKGCoordinator.DKGSessionInfo memory sessionInfo = dkgCoordinator.getSessionInfo(sessionId);
        if (!sessionInfo.isCompleted) revert DKGNotComplete();

        FederationConfig storage config = federations[federationId];
        if (config.frostMultisig != address(0) || config.multiPool != address(0) || config.shareToken != address(0)) {
            revert FederationAlreadyDeployed();
        }
        if (msg.sender != deploymentInitiator[sessionId]) {
            bool participant;
            for (uint256 i = 0; i < config.members.length; i++) {
                if (config.members[i] == msg.sender) {
                    participant = true;
                    break;
                }
            }
            if (!participant) revert NotAuthorized();
        }
        if (v1Sessions[sessionId] && keccak256(abi.encode(shareTokenName, shareTokenSymbol))
                != reservedFederationTerms[federationId].tokenHash) {
            revert InvalidFederationReservation();
        }

        // Get group public key from DKG
        bytes memory groupPubkey = dkgCoordinator.getGroupPublicKey(sessionId);
        config.groupPubkey = groupPubkey;

        // Deploy the production authorization graph bound to the canonical coordinator.
        if(v1Sessions[sessionId]) {
        (frostMultisig, multiPool, shareToken) = deployer.deployFederationContractsV1(
            federationId,
            config.members,
            config.threshold,
            shareTokenName,
            shareTokenSymbol,
            frostCoordinator,
            groupPubkey, sessionId
        );
        } else {
        (frostMultisig, multiPool, shareToken) = deployer.deployFederationContracts(
            federationId,
            config.members,
            config.threshold,
            shareTokenName,
            shareTokenSymbol,
            frostCoordinator,
            groupPubkey
        );
        }

        if(v1Sessions[sessionId]) _collectDeploymentFee();
        config.frostMultisig = frostMultisig;
        config.multiPool = multiPool;
        config.shareToken = shareToken;
        config.feeManager = address(0);
        config.nftModifier = address(0);
        config.walletRegistry = address(0);
        config.spvConfirmationDepth = 0;

        // Activate federation
        config.isActive = true;

        emit FederationDeployed(federationId, frostMultisig, multiPool, shareToken);
    }

    /**
     * @notice Deactivate a federation (requires FROST threshold approval)
     * @param federationId The federation to deactivate
     * @param operationHash The FROST-signed operation hash for this deactivation
     */
    function deactivateFederation(string calldata federationId, bytes32 operationHash) external {
        FederationConfig storage config = federations[federationId];
        if (config.createdAt == 0) revert FederationNotFound();
        if (!config.isActive) revert FederationNotFound();

        // Verify caller is a member
        bool isMember = false;
        for (uint256 i = 0; i < config.members.length; i++) {
            if (config.members[i] == msg.sender) {
                isMember = true;
                break;
            }
        }
        if (!isMember) revert NotAuthorized();

        // Verify FROST threshold approval
        FROSTAuthorization frost = FROSTAuthorization(config.frostMultisig);
        require(operationHash == getDeactivateFederationOperationHash(federationId), "Operation hash mismatch");
        require(frost.isOperationApproved(operationHash, address(this)), "FROST threshold not met");

        // Mark as executed to prevent replay
        frost.consumeOperation(operationHash);
        federationOperationNonces[federationId]++;

        config.isActive = false;
        emit FederationDeactivated(federationId);
    }

    // ======== VIEW FUNCTIONS ========

    /**
     * @notice Get federation configuration
     */
    function getFederation(string calldata federationId) external view returns (FederationConfig memory config) {
        config = federations[federationId];
        if (config.createdAt == 0) revert FederationNotFound();
        if (config.multiPool != address(0)) {
            config.feeManager = address(SimpleMultiPool(config.multiPool).feeManager());
            if (config.feeManager != address(0)) {
                FederationFeeManager manager = FederationFeeManager(config.feeManager);
                config.nftModifier = address(manager.nftModifier());
                config.walletRegistry = address(manager.walletRegistry());
            }
        }
        return config;
    }

    /**
     * @notice Get all federations a user is member of
     */
    function getUserFederations(address user) external view returns (string[] memory) {
        return userFederations[user];
    }

    /**
     * @notice Get all federation IDs
     */
    function getAllFederations() external view returns (string[] memory) {
        return allFederationIds;
    }

    /**
     * @notice Get the number of federations
     */
    function getFederationCount() external view returns (uint256) {
        return allFederationIds.length;
    }

    /**
     * @notice Check if a federation exists
     */
    function federationExists(string calldata federationId) external view returns (bool) {
        return federations[federationId].createdAt != 0;
    }

    /**
     * @notice Check if a federation is active
     */
    function isFederationActive(string calldata federationId) external view returns (bool) {
        return federations[federationId].isActive;
    }

    function getDeactivateFederationOperationHash(string calldata federationId) public view returns (bytes32) {
        FederationConfig storage config = federations[federationId];
        if (config.createdAt == 0) revert FederationNotFound();
        return FROSTAuthorization(config.frostMultisig).hashOperation(
            address(this),
            this.deactivateFederation.selector,
            keccak256(abi.encode(federationId)),
            federationOperationNonces[federationId]
        );
    }

    /**
     * @notice Get the DKG coordinator address
     */
    function getDKGCoordinator() external view returns (address) {
        return address(dkgCoordinator);
    }

    /**
     * @notice Get DKG session ID for a federation
     * @param federationId The federation ID
     * @return The DKG session ID
     */
    function getDKGSession(string calldata federationId) external view returns (bytes32) {
        return federationToSession[federationId];
    }

    /**
     * @notice Get federation ID for a DKG session
     * @param sessionId The DKG session ID
     * @return The federation ID
     */
    function getFederationForSession(bytes32 sessionId) external view returns (string memory) {
        return sessionToFederation[sessionId];
    }
}
