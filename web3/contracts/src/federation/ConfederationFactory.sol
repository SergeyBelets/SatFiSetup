// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./interfaces/IConfederationFactory.sol";
import "./interfaces/IFederationFactory.sol";
import "./interfaces/IDKGCoordinator.sol";
import "./ConfederationDeployer.sol";
import "./DKGCoordinator.sol";
import "./CustodianValidator.sol";
import "../FROSTAuthorization.sol";
import "./FederationFeeManager.sol";
import "./ConfederationMultiPool.sol";
import {CustodianDAOConfigValidator} from "./CustodianDAOConfigValidator.sol";

interface IConfederationFROSTCoordinator {
    function defaultVerifier() external view returns (address);
    function dkgRegistry() external view returns (address);
}

import "./CustodianDAOFeeRouter.sol";
/**
 * @title ConfederationFactory
 * @notice Factory for creating confederations (groups of federations + custodians)
 * @dev A confederation combines multiple federations with institutional custodians.
 *      Withdrawals require both FROST threshold AND minimum custodian signatures.
 *      Uses ConfederationDeployer to reduce contract size.
 */
contract ConfederationFactory is IConfederationFactory {
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
    /// @notice Initiator may finish deployment; unrelated callers cannot choose token metadata.
    mapping(bytes32 => address) public deploymentInitiator;
    // ======== STATE ========

    /// @notice Reference to the federation factory
    IFederationFactory public immutable federationFactory;

    /// @notice DKG coordinator for confederation-level DKG
    IDKGCoordinator public immutable dkgCoordinator;

    /// @notice Deployer for confederation contracts
    ConfederationDeployer public immutable deployer;
    CustodianDAOConfigValidator private immutable daoConfigValidator;
    address public immutable frostCoordinator;

    /// @notice Confederation configurations
    mapping(string => ConfederationConfig) private confederations;
    mapping(string => CustodianDAOConfig) private custodianDAOs;

    /// @notice Federation members of each confederation
    mapping(string => FederationMember[]) private confederationFederations;

    /// @notice Custodian members of each confederation
    mapping(string => CustodianMember[]) private confederationCustodians;

    /// @notice Founders are fixed by the DKG epoch, independently of source federation count or validator changes.
    mapping(string => address[]) private confederationFounders;

    /// @dev Matches the participant bound of DKGCoordinator.PROTOCOL_V1.
    uint256 private constant MAX_DKG_PARTICIPANTS = 16;

    struct Formation {
        string[] federationIds;
        FederationMember[] federations;
        address[] founders;
        address[] participants;
    }

    /// @notice All confederation IDs
    string[] private allConfederationIds;

    /// @notice DKG session to confederation ID mapping
    mapping(bytes32 => string) private sessionToConfederation;

    /// @notice Confederation ID to DKG session mapping
    mapping(string => bytes32) private confederationToSession;

    /// @notice One-to-one reservation of an ID for a DKG session on this factory.
    mapping(string => bytes32) public reservedConfederationSession;
    struct ReservationTerms {
        bytes32 formationHash;
        bytes32 tokenHash;
    }
    mapping(string => ReservationTerms) public reservedConfederationTerms;
    mapping(bytes32 => bool) private reservedSessions;

    /// @notice Per-confederation nonce for target-bound FROST operations
    mapping(string => uint256) private confederationOperationNonces;

    /// @notice Exclusive UTC settlement-day limit, max uint while active and zero for an unregistered pool.
    mapping(address => uint256) public settlementDayLimit;

    // ======== ERRORS ========

    error ConfederationAlreadyExists();
    error ConfederationAlreadyDeployed();
    error ConfederationNotFound();
    error FederationNotFound();
    error FederationNotActive();
    error DKGNotComplete();
    error SessionNotFound();
    error NotAuthorized();
    error InvalidConfederationId();
    error InvalidConfederationReservation();
    error InvalidFederations();
    error InvalidCustodians();
    error InvalidThreshold();
    error InvalidAddress();
    error CustodianDAOAlreadyRegistered();
    error InvalidCustodianDAO();
    error InvalidRosterSize();
    error LegacyNetworkDisabled();

    // ======== EVENTS (additional) ========

    event InterFederationCommitmentSubmitted(bytes32 indexed sessionId, string federationId, bytes32 commitment);
    event ConfederationIdReserved(string indexed confederationId, bytes32 indexed sessionId);
    event ConfederationReservationTransferred(
        string indexed confederationId, bytes32 indexed previous, bytes32 indexed replacement
    );
    event ConfederationSettlementCutoff(address indexed pool, uint256 lastDay);

    // ======== CONSTRUCTOR ========

    /**
     * @notice Create a confederation factory
     * @param _federationFactory Address of the federation factory
     * @param _dkgCoordinator Address of the DKG coordinator
     * @param _deployer Address of the confederation deployer
     */
    constructor(address _federationFactory, address _dkgCoordinator, address _deployer, address _frostCoordinator) {
        if (_federationFactory == address(0)) revert InvalidAddress();
        if (_dkgCoordinator == address(0)) revert InvalidAddress();
        if (_deployer == address(0)) revert InvalidAddress();
        if (_frostCoordinator == address(0) || _frostCoordinator.code.length == 0) revert InvalidAddress();
        if (IConfederationFROSTCoordinator(_frostCoordinator).defaultVerifier() == address(0)) {
            revert InvalidAddress();
        }
        federationFactory = IFederationFactory(_federationFactory);
        dkgCoordinator = IDKGCoordinator(_dkgCoordinator);
        deployer = ConfederationDeployer(_deployer);
        frostCoordinator = _frostCoordinator;
        daoConfigValidator = new CustodianDAOConfigValidator();
    }

    // ======== MAIN FUNCTIONS ========

    /// @notice The factory deployer binds an unused ID to a DKG session, formation and token metadata.
    /// @dev May precede DKG completion. The exact retry is a no-op.
    function reserveConfederationId(bytes32 sessionId, string calldata id, bytes32 formationHash, bytes32 tokenHash)
        external
    {
        if (msg.sender != feeSetupAuthority) revert NotAuthorized();
        if (sessionId == 0 || bytes(id).length == 0 || formationHash == 0 || tokenHash == 0) {
            revert InvalidConfederationReservation();
        }
        bytes32 previous = reservedConfederationSession[id];
        if (previous == sessionId) {
            ReservationTerms storage terms = reservedConfederationTerms[id];
            if (terms.formationHash != formationHash || terms.tokenHash != tokenHash) {
                revert InvalidConfederationReservation();
            }
            return;
        }
        if (previous != 0 || confederations[id].createdAt != 0) revert ConfederationAlreadyExists();
        _requireUnusedReservationSession(sessionId);
        reservedConfederationSession[id] = sessionId;
        reservedConfederationTerms[id] = ReservationTerms(formationHash, tokenHash);
        reservedSessions[sessionId] = true;
        emit ConfederationIdReserved(id, sessionId);
    }

    /// @notice Move an unused reservation from a terminal ceremony to a replacement with the same DKG roster.
    function transferConfederationReservation(string calldata id, bytes32 previous, bytes32 replacement) external {
        if (msg.sender != feeSetupAuthority) revert NotAuthorized();
        _requireCanonicalRegistry();
        if (previous == 0 || reservedConfederationSession[id] != previous || confederationToSession[id] != 0
            || replacement == 0) revert InvalidConfederationReservation();
        _requireUnusedReservationSession(replacement);
        DKGCoordinator(address(dkgCoordinator)).requireReservationTransfer(previous, replacement);
        reservedConfederationSession[id] = replacement;
        reservedSessions[replacement] = true;
        emit ConfederationReservationTransferred(id, previous, replacement);
    }

    function _requireUnusedReservationSession(bytes32 sessionId) private view {
        if (reservedSessions[sessionId] || bytes(sessionToConfederation[sessionId]).length != 0) {
            revert InvalidConfederationReservation();
        }
    }

    /**
     * @notice Initiate confederation creation
     * @param federationIds Array of federation IDs to include
     * @param custodianMembers Array of custodian members
     * @param totalThreshold Total signature threshold for FROST
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
    ) external returns (bytes32 sessionId) {
        _requireLegacyNetwork();
        if (msg.sender != feeSetupAuthority) revert NotAuthorized();
        _requireNewConfederation(confederationId);
        if (reservedConfederationSession[confederationId] != 0) revert ConfederationAlreadyExists();
        Formation memory formation = _formation(federationIds, false);
        formation.participants = _validateComposition(formation.founders, custodianMembers, totalThreshold, minCustodians);
        sessionId = dkgCoordinator.initiateDKG(formation.participants, totalThreshold, confederationId);
        if (reservedSessions[sessionId]) revert InvalidConfederationReservation();
        _recordConfederation(sessionId, formation, custodianMembers, totalThreshold, minCustodians, confederationId);
    }

    function adoptConfederationDKG(
        bytes32 sessionId,
        string[] calldata federationIds,
        CustodianMember[] memory custodianMembers,
        uint256 totalThreshold,
        uint256 minCustodians,
        string calldata confederationId
    ) external {
        _requireCanonicalRegistry();
        _adopt(sessionId, 0, federationIds, custodianMembers, totalThreshold, minCustodians, confederationId);
    }

    function adoptConfederationFromFederation(
        bytes32 sessionId,
        string calldata federationId,
        CustodianMember[] memory custodians,
        uint256 totalThreshold,
        uint256 minCustodians,
        string calldata confederationId
    ) external {
        _requireCanonicalRegistry();
        string[] memory ids = new string[](1);
        ids[0] = federationId;
        _adopt(sessionId, 1, ids, custodians, totalThreshold, minCustodians, confederationId);
    }

    function _requireLegacyNetwork() private view {
        if (block.chainid != 31337 && block.chainid != 11155111) revert LegacyNetworkDisabled();
    }

    /// @notice A separate monetary epoch controlled exclusively by custodians.
    /// @dev Founders retain association metadata, never a share of this epoch's key.
    function adoptCustodianConfederationDKG(
        bytes32 sessionId, string[] calldata federationIds, CustodianMember[] memory custodians,
        uint256 threshold, uint256 minCustodians, string calldata id
    ) external {
        _requireCanonicalRegistry();
        _requireReservedConfederation(
            sessionId, id, keccak256(abi.encode(uint8(2), federationIds, custodians, threshold, minCustodians))
        );
        Formation memory formation = _formation(federationIds, false);
        uint256 n = custodians.length;
        require(n >= 2 && n <= MAX_DKG_PARTICIPANTS && threshold >= 2 && threshold <= n
            && threshold >= (7 * n + 9) / 10 && minCustodians >= threshold && minCustodians <= n,
            "custody threshold");
        formation.participants = new address[](n);
        for (uint256 i; i < n; ++i) {
            address signer = custodians[i].signerAddress;
            require(signer != address(0), "custody signer");
            for (uint256 j; j < formation.founders.length; ++j)
                require(signer != formation.founders[j], "founder is not custodian");
            for (uint256 j; j < i; ++j) require(signer != formation.participants[j], "duplicate custodian");
            formation.participants[i] = signer;
        }
        bool admitted = DKGCoordinator(address(dkgCoordinator)).isSessionParticipant(sessionId, msg.sender);
        for (uint256 i; i < formation.founders.length; ++i) if (formation.founders[i] == msg.sender) admitted = true;
        require(admitted, "DKG admission");
        _checkAdoption(sessionId, formation.participants, threshold);
        v1Sessions[sessionId] = true;
        _recordConfederation(sessionId, formation, custodians, threshold, minCustodians, id);
    }

    function _requireCanonicalRegistry() private view {
        require(
            IConfederationFROSTCoordinator(frostCoordinator).dkgRegistry() == address(dkgCoordinator),
            "DKG registry mismatch"
        );
    }

    function _requireNewConfederation(string calldata id) private view {
        if (bytes(id).length == 0) revert InvalidConfederationId();
        if (confederations[id].createdAt != 0) revert ConfederationAlreadyExists();
    }

    function _requireReservedConfederation(
        bytes32 sessionId,
        string calldata id,
        bytes32 formationHash
    ) private view {
        _requireNewConfederation(id);
        if (sessionId == 0 || reservedConfederationSession[id] != sessionId
            || reservedConfederationTerms[id].formationHash != formationHash) {
            revert InvalidConfederationReservation();
        }
    }

    /// @dev Preserve caller-provided federation order and original member order. No sorting or deduplication.
    function _formation(string[] memory ids, bool allMembers) private view returns (Formation memory formation) {
        if (ids.length == 0 || ids.length > MAX_DKG_PARTICIPANTS) revert InvalidFederations();
        formation.federationIds = ids;
        formation.federations = new FederationMember[](ids.length);
        formation.founders = new address[](ids.length);
        for (uint256 i; i < ids.length; ++i) {
            for (uint256 j; j < i; ++j) {
                if (keccak256(bytes(ids[i])) == keccak256(bytes(ids[j]))) revert InvalidFederations();
            }
            if (!federationFactory.federationExists(ids[i])) revert FederationNotFound();
            if (!federationFactory.isFederationActive(ids[i])) revert FederationNotActive();
            IFederationFactory.FederationConfig memory config = federationFactory.getFederation(ids[i]);
            if (config.members.length == 0) revert InvalidFederations();
            formation.federations[i] = FederationMember(ids[i], config.members[0], config.shareToken);
            if (allMembers) formation.founders = config.members;
            else formation.founders[i] = config.members[0];
        }
    }

    /// @dev One composition rule for every creation path. Validator membership never changes this DKG roster.
    function _validateComposition(
        address[] memory founders,
        CustodianMember[] memory custodians,
        uint256 threshold,
        uint256 minCustodians
    ) private pure returns (address[] memory participants) {
        uint256 f = founders.length;
        uint256 c = custodians.length;
        if (f == 0) revert InvalidFederations();
        if (f > MAX_DKG_PARTICIPANTS || c > MAX_DKG_PARTICIPANTS || f + c > MAX_DKG_PARTICIPANTS) {
            revert InvalidRosterSize();
        }
        if (c < (f + 1) / 2) revert InvalidCustodians();
        uint256 n = f + c;
        if (threshold == 0 || threshold > n || threshold < (7 * n + 9) / 10 || threshold <= f) {
            revert InvalidThreshold();
        }
        if (minCustodians < threshold - f || minCustodians > c) revert InvalidThreshold();
        participants = new address[](n);
        for (uint256 i; i < n; ++i) {
            address signer = i < f ? founders[i] : custodians[i - f].signerAddress;
            if (signer == address(0)) revert InvalidAddress();
            for (uint256 j; j < i; ++j) {
                if (participants[j] == signer) revert InvalidAddress();
            }
            participants[i] = signer;
        }
    }

    function _adopt(
        bytes32 sessionId,
        uint8 mode,
        string[] memory federationIds,
        CustodianMember[] memory custodians,
        uint256 threshold,
        uint256 minCustodians,
        string calldata id
    ) private {
        _requireReservedConfederation(
            sessionId, id, keccak256(abi.encode(mode, federationIds, custodians, threshold, minCustodians))
        );
        Formation memory formation = _formation(federationIds, mode == 1);
        formation.participants = _validateComposition(formation.founders, custodians, threshold, minCustodians);
        _checkAdoption(sessionId, formation.participants, threshold);
        require(DKGCoordinator(address(dkgCoordinator)).isSessionParticipant(sessionId, msg.sender), "DKG admission");
        v1Sessions[sessionId] = true;
        _recordConfederation(sessionId, formation, custodians, threshold, minCustodians, id);
    }

    function _checkAdoption(bytes32 sessionId, address[] memory participants, uint256 threshold) private view {
        (,,, bytes32 purpose,, address[] memory roster, uint256 completedThreshold) =
            DKGCoordinator(address(dkgCoordinator)).getCompletedResult(sessionId);
        require(
            purpose == keccak256("EVM_AUTH")
                && keccak256(abi.encode(roster)) == keccak256(abi.encode(participants))
                && completedThreshold == threshold,
            "DKG roster"
        );
    }

    function _recordConfederation(
        bytes32 sessionId,
        Formation memory formation,
        CustodianMember[] memory custodians,
        uint256 threshold,
        uint256 minCustodians,
        string calldata id
    ) private {
        require(bytes(sessionToConfederation[sessionId]).length == 0, "DKG already used");
        sessionToConfederation[sessionId] = id;
        confederationToSession[id] = sessionId;
        deploymentInitiator[sessionId] = msg.sender;
        ConfederationConfig storage config = confederations[id];
        config.confederationId = id;
        config.federationIds = formation.federationIds;
        config.federationCount = formation.federationIds.length;
        config.custodianCount = custodians.length;
        config.totalThreshold = threshold;
        config.minCustodians = minCustodians;
        config.createdAt = block.timestamp;
        confederationFounders[id] = formation.founders;
        for (uint256 i; i < formation.federations.length; ++i) {
            confederationFederations[id].push(formation.federations[i]);
        }
        for (uint256 i; i < custodians.length; ++i) confederationCustodians[id].push(custodians[i]);
        allConfederationIds.push(id);
        emit ConfederationDKGInitiated(sessionId, id, threshold, minCustodians);
    }

    /**
     * @notice Submit inter-federation commitment for DKG
     * @param sessionId The DKG session ID
     * @param federationId The federation submitting
     * @param commitment The commitment hash
     */
    function submitInterFederationCommitment(bytes32 sessionId, string calldata federationId, bytes32 commitment)
        external
    {
        // This transition belongs to legacy DKG; V1 ceremonies already finished before adoption.
        _requireLegacyNetwork();
        string storage confId = sessionToConfederation[sessionId];
        if (bytes(confId).length == 0) revert SessionNotFound();

        // Verify caller is authorized for this federation
        IFederationFactory.FederationConfig memory fedConfig = federationFactory.getFederation(federationId);
        bool isAuthorized = false;
        for (uint256 i = 0; i < fedConfig.members.length; i++) {
            if (fedConfig.members[i] == msg.sender) {
                isAuthorized = true;
                break;
            }
        }
        if (!isAuthorized) revert NotAuthorized();

        // Submit VSS commitments to DKG coordinator
        ConfederationConfig storage config = confederations[confId];
        bytes32[] memory commitments = new bytes32[](config.totalThreshold);
        commitments[0] = commitment;

        dkgCoordinator.submitVSSCommitments(sessionId, commitments);

        emit InterFederationCommitmentSubmitted(sessionId, federationId, commitment);
    }

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
    function deployConfederation(bytes32 sessionId, string calldata shareTokenName, string calldata shareTokenSymbol)
        external
        returns (address frostMultisig, address custodianValidator, address multiPool, address shareToken)
    {
        bool adopted = v1Sessions[sessionId];
        if (!adopted) _requireLegacyNetwork();
        string storage confederationId = sessionToConfederation[sessionId];
        if (bytes(confederationId).length == 0) revert SessionNotFound();
        if (adopted && reservedConfederationTerms[confederationId].tokenHash
            != keccak256(abi.encode(shareTokenName, shareTokenSymbol))) revert InvalidConfederationReservation();

        // Check DKG is complete
        IDKGCoordinator.DKGSessionInfo memory sessionInfo = dkgCoordinator.getSessionInfo(sessionId);
        if (!sessionInfo.isCompleted) revert DKGNotComplete();

        ConfederationConfig storage config = confederations[confederationId];
        if (
            config.frostMultisig != address(0) || config.custodianValidator != address(0)
                || config.multiPool != address(0) || config.shareToken != address(0)
        ) revert ConfederationAlreadyDeployed();

        // Get group public key from DKG
        bytes memory groupPubkey = dkgCoordinator.getGroupPublicKey(sessionId);
        config.groupPubkey = groupPubkey;

        // Get all signers from DKG
        address[] memory allSigners = dkgCoordinator.getSessionParticipants(sessionId);
        if (msg.sender != deploymentInitiator[sessionId]) {
            bool participant;
            for (uint256 i = 0; i < allSigners.length; i++) {
                if (allSigners[i] == msg.sender) {
                    participant = true;
                    break;
                }
            }
            if (!participant) revert NotAuthorized();
        }

        // Collect custodian addresses
        CustodianMember[] storage custodians = confederationCustodians[confederationId];
        address[] memory custodianAddresses = new address[](custodians.length);
        for (uint256 i = 0; i < custodians.length; i++) {
            custodianAddresses[i] = custodians[i].signerAddress;
        }

        // Deploy contracts via deployer
        if(adopted) {
        (frostMultisig, custodianValidator, multiPool, shareToken) = deployer.deployConfederationContractsV1(
            confederationId,
            allSigners,
            config.totalThreshold,
            custodianAddresses,
            config.minCustodians,
            shareTokenName,
            shareTokenSymbol,
            frostCoordinator,
            groupPubkey, sessionId
        );
        } else {
        (frostMultisig, custodianValidator, multiPool, shareToken) = deployer.deployConfederationContracts(
            confederationId,
            allSigners,
            config.totalThreshold,
            custodianAddresses,
            config.minCustodians,
            shareTokenName,
            shareTokenSymbol,
            frostCoordinator,
            groupPubkey
        );
        }

        if(adopted) _collectDeploymentFee();
        config.frostMultisig = frostMultisig;
        config.custodianValidator = custodianValidator;
        config.multiPool = multiPool;
        config.shareToken = shareToken;
        config.confederationToken = shareToken; // Same as shareToken in basic deployment
        config.feeManager = address(0);
        config.nftModifier = address(0);
        config.walletRegistry = address(0);
        config.spvConfirmationDepth = 0;

        // Activate confederation
        config.isActive = true;
        settlementDayLimit[multiPool] = type(uint256).max;

        emit ConfederationDeployed(confederationId, frostMultisig, custodianValidator, multiPool, shareToken);
    }

    /**
     * @notice Deactivate a confederation (requires FROST threshold + custodian threshold)
     * @param confederationId The confederation to deactivate
     * @param operationHash The operation hash signed by both FROST and custodians
     */
    function deactivateConfederation(string calldata confederationId, bytes32 operationHash) external {
        ConfederationConfig storage config = confederations[confederationId];
        if (config.createdAt == 0) revert ConfederationNotFound();
        if (!config.isActive) revert ConfederationNotFound();

        // Verify caller is an epoch founder or custodian.
        bool isAuthorized = false;

        address[] storage founders = confederationFounders[confederationId];
        for (uint256 i = 0; i < founders.length; i++) {
            if (founders[i] == msg.sender) {
                isAuthorized = true;
                break;
            }
        }

        // Check if caller is a custodian
        if (!isAuthorized) {
            CustodianMember[] storage custMembers = confederationCustodians[confederationId];
            for (uint256 i = 0; i < custMembers.length; i++) {
                if (custMembers[i].signerAddress == msg.sender) {
                    isAuthorized = true;
                    break;
                }
            }
        }

        if (!isAuthorized) revert NotAuthorized();

        // Verify FROST threshold approval
        FROSTAuthorization frost = FROSTAuthorization(config.frostMultisig);
        require(operationHash == getDeactivateConfederationOperationHash(confederationId), "Operation hash mismatch");
        require(frost.isOperationApproved(operationHash, address(this)), "FROST threshold not met");

        // Verify custodian threshold approval
        CustodianValidator validator = CustodianValidator(config.custodianValidator);
        require(validator.meetsMinimumCustodians(operationHash), "Custodian threshold not met");

        // Mark as executed to prevent replay
        frost.consumeOperation(operationHash);
        confederationOperationNonces[confederationId]++;

        config.isActive = false;
        uint256 lastDay = block.timestamp / 1 days;
        settlementDayLimit[config.multiPool] = lastDay + 1;
        emit ConfederationSettlementCutoff(config.multiPool, lastDay);
        emit ConfederationDeactivated(confederationId);
    }

    // ======== VIEW FUNCTIONS ========

    function registerCustodianDAO(string calldata confederationId, CustodianDAOConfig calldata dao, bytes32 operationHash)
        external
    {
        ConfederationConfig storage config = confederations[confederationId];
        if (!config.isActive) revert ConfederationNotFound();
        if (custodianDAOs[confederationId].settlement != address(0)) revert CustodianDAOAlreadyRegistered();
        require(operationHash == getRegisterCustodianDAOOperationHash(confederationId, dao), "Operation hash mismatch");
        require(
            CustodianValidator(config.custodianValidator).meetsMinimumCustodians(operationHash),
            "Custodian threshold not met"
        );
        daoConfigValidator.validate(dao, config.custodianValidator, config.frostMultisig);
        FROSTAuthorization(config.frostMultisig).consumeOperation(operationHash);
        confederationOperationNonces[confederationId]++;
        custodianDAOs[confederationId] = dao;
        emit CustodianDAORegistered(confederationId, dao.settlement, dao.staking);
    }

    function getRegisterCustodianDAOOperationHash(string calldata confederationId, CustodianDAOConfig calldata dao)
        public
        view
        returns (bytes32)
    {
        ConfederationConfig storage config = confederations[confederationId];
        if (!config.isActive) revert ConfederationNotFound();
        return FROSTAuthorization(config.frostMultisig).hashOperation(
            address(this), this.registerCustodianDAO.selector, keccak256(abi.encode(confederationId, dao)),
            confederationOperationNonces[confederationId]
        );
    }

    function getCustodianDAO(string calldata confederationId) external view returns (CustodianDAOConfig memory) {
        if (confederations[confederationId].createdAt == 0) revert ConfederationNotFound();
        return custodianDAOs[confederationId];
    }

    /**
     * @notice Get confederation configuration
     */
    function getConfederation(string calldata confederationId) external view returns (ConfederationConfig memory config) {
        config = confederations[confederationId];
        if (config.createdAt == 0) revert ConfederationNotFound();
        if (config.multiPool != address(0)) {
            config.feeManager = address(ConfederationMultiPool(config.multiPool).feeManager());
            if (config.feeManager != address(0)) {
                FederationFeeManager manager = FederationFeeManager(config.feeManager);
                config.nftModifier = address(manager.nftModifier());
                config.walletRegistry = address(manager.walletRegistry());
            }
        }
        return config;
    }

    /**
     * @notice Get federation members of a confederation
     */
    function getFederationMembers(string calldata confederationId) external view returns (FederationMember[] memory) {
        return confederationFederations[confederationId];
    }

    function getFounders(string calldata confederationId) external view returns (address[] memory) {
        return confederationFounders[confederationId];
    }

    /**
     * @notice Get custodian members of a confederation
     */
    function getCustodianMembers(string calldata confederationId) external view returns (CustodianMember[] memory) {
        return confederationCustodians[confederationId];
    }

    function getDeactivateConfederationOperationHash(string calldata confederationId) public view returns (bytes32) {
        ConfederationConfig storage config = confederations[confederationId];
        if (config.createdAt == 0) revert ConfederationNotFound();
        return FROSTAuthorization(config.frostMultisig).hashOperation(
            address(this),
            this.deactivateConfederation.selector,
            keccak256(abi.encode(confederationId)),
            confederationOperationNonces[confederationId]
        );
    }

    /**
     * @notice Get all confederation IDs
     */
    function getAllConfederations() external view returns (string[] memory) {
        return allConfederationIds;
    }

    /**
     * @notice Get the number of confederations
     */
    function getConfederationCount() external view returns (uint256) {
        return allConfederationIds.length;
    }

    /**
     * @notice Check if a confederation exists
     */
    function confederationExists(string calldata confederationId) external view returns (bool) {
        return confederations[confederationId].createdAt != 0;
    }

    /**
     * @notice Check if a confederation is active
     */
    function isConfederationActive(string calldata confederationId) external view returns (bool) {
        return confederations[confederationId].isActive;
    }

    /**
     * @notice Get the federation factory address
     */
    function getFederationFactory() external view returns (address) {
        return address(federationFactory);
    }

    /**
     * @notice Get the DKG coordinator address
     */
    function getDKGCoordinator() external view returns (address) {
        return address(dkgCoordinator);
    }

    /**
     * @notice Get DKG session ID for a confederation
     */
    function getDKGSession(string calldata confederationId) external view returns (bytes32) {
        return confederationToSession[confederationId];
    }

    /**
     * @notice Get confederation ID for a DKG session
     */
    function getConfederationForSession(bytes32 sessionId) external view returns (string memory) {
        return sessionToConfederation[sessionId];
    }
}
