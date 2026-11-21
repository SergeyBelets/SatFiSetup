// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IDKGCoordinator
 * @notice Interface for on-chain Distributed Key Generation coordination
 * @dev Coordinates the DKG process for FROST threshold signatures
 */
interface IDKGCoordinator {
    /// @notice DKG phases
    enum DKGPhase {
        NotStarted,
        CommitmentsPhase,    // Round 1: VSS commitments
        SharesPhase,         // Round 2: Share distribution (hashes)
        PublicKeyPhase,      // Round 3: Public key shares
        Completed,
        Failed,
        Aborted
    }

    /// @notice DKG session information
    struct DKGSessionInfo {
        bytes32 sessionId;
        string federationId;
        uint256 participantCount;
        uint256 threshold;
        DKGPhase currentPhase;
        uint256 phaseDeadline;
        uint256 commitmentCount;
        uint256 shareCount;
        uint256 pkShareCount;
        bool isCompleted;
    }

    /// @notice Emitted when a DKG session is initiated
    event DKGInitiated(bytes32 indexed sessionId, string federationId, uint256 threshold);

    /// @notice Emitted when a phase advances
    event PhaseAdvanced(bytes32 indexed sessionId, DKGPhase newPhase);

    /// @notice Emitted when a commitment is submitted
    event CommitmentSubmitted(bytes32 indexed sessionId, address indexed participant);

    /// @notice Emitted when a share hash is submitted
    event ShareSubmitted(bytes32 indexed sessionId, address indexed from, address indexed to);

    /// @notice Emitted when a public key share is submitted
    event PublicKeyShareSubmitted(bytes32 indexed sessionId, address indexed participant);

    /// @notice Emitted when a participant approves an aggregated group public key candidate
    event GroupPublicKeyApproved(bytes32 indexed sessionId, address indexed participant, bytes32 indexed groupPublicKeyHash);

    /// @notice Emitted when DKG completes successfully
    event DKGCompleted(bytes32 indexed sessionId, bytes groupPublicKey);

    /// @notice Emitted when DKG fails
    event DKGFailed(bytes32 indexed sessionId, string reason);

    /**
     * @notice Initiate a new DKG session
     * @param participants Array of participant addresses
     * @param threshold Required signature threshold
     * @param federationId Identifier for the federation
     * @return sessionId Unique session identifier
     */
    function initiateDKG(
        address[] calldata participants,
        uint256 threshold,
        string calldata federationId
    ) external returns (bytes32 sessionId);

    /**
     * @notice Submit VSS commitments for a session
     * @param sessionId The DKG session ID
     * @param commitments Array of VSS commitment hashes
     */
    function submitVSSCommitments(
        bytes32 sessionId,
        bytes32[] calldata commitments
    ) external;

    /**
     * @notice Submit a share hash for a recipient
     * @param sessionId The DKG session ID
     * @param recipient The recipient of the share
     * @param shareHash Hash of the encrypted share
     */
    function submitShareHash(
        bytes32 sessionId,
        address recipient,
        bytes32 shareHash
    ) external;

    /**
     * @notice Submit public key share
     * @param sessionId The DKG session ID
     * @param publicKeyShare The participant's public key share
     */
    function submitPublicKeyShare(
        bytes32 sessionId,
        bytes calldata publicKeyShare
    ) external;

    /**
     * @notice Approve an off-chain aggregated group public key candidate
     * @param sessionId The DKG session ID
     * @param groupPublicKey The aggregated group public key
     */
    function approveGroupPublicKey(bytes32 sessionId, bytes calldata groupPublicKey) external;

    /**
     * @notice Complete the DKG session and set group public key
     * @param sessionId The DKG session ID
     * @param groupPublicKey The aggregated group public key
     */
    function completeDKG(bytes32 sessionId, bytes calldata groupPublicKey) external;

    /**
     * @notice Manually advance to the next phase (if conditions met)
     * @param sessionId The DKG session ID
     */
    function advancePhase(bytes32 sessionId) external;

    /**
     * @notice Mark a DKG session as failed
     * @param sessionId The DKG session ID
     * @param reason Reason for failure
     */
    function failDKG(bytes32 sessionId, string calldata reason) external;

    /**
     * @notice Get DKG session status
     * @param sessionId The DKG session ID
     * @return phase Current phase
     * @return commitmentCount Number of commitments received
     * @return shareCount Number of share hashes received
     * @return pkShareCount Number of public key shares received
     * @return isCompleted Whether DKG is complete
     */
    function getDKGStatus(bytes32 sessionId) external view returns (
        DKGPhase phase,
        uint256 commitmentCount,
        uint256 shareCount,
        uint256 pkShareCount,
        bool isCompleted
    );

    /**
     * @notice Get session participants
     * @param sessionId The DKG session ID
     * @return Array of participant addresses
     */
    function getSessionParticipants(bytes32 sessionId) external view returns (address[] memory);

    /**
     * @notice Get session info
     * @param sessionId The DKG session ID
     * @return Session information struct
     */
    function getSessionInfo(bytes32 sessionId) external view returns (DKGSessionInfo memory);

    /**
     * @notice Get the group public key for a completed session
     * @param sessionId The DKG session ID
     * @return The group public key bytes
     */
    function getGroupPublicKey(bytes32 sessionId) external view returns (bytes memory);

    /**
     * @notice Check if a participant has submitted commitments
     * @param sessionId The DKG session ID
     * @param participant The participant address
     * @return True if commitments were submitted
     */
    function hasSubmittedCommitments(bytes32 sessionId, address participant) external view returns (bool);

    /**
     * @notice Check if a participant has submitted public key share
     * @param sessionId The DKG session ID
     * @param participant The participant address
     * @return True if public key share was submitted
     */
    function hasSubmittedPKShare(bytes32 sessionId, address participant) external view returns (bool);

    /**
     * @notice Get phase duration constant
     * @return Phase duration in seconds
     */
    function getPhaseDuration() external view returns (uint256);
}
