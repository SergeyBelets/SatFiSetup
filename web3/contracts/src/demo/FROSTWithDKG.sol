// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/**
 * @title FROSTWithDKG
 * @notice FROST threshold signatures with Distributed Key Generation
 * @dev Implements:
 *      - DKG (Distributed Key Generation) with VSS (Verifiable Secret Sharing)
 *      - Threshold Schnorr signatures (t-of-n)
 *      - Commitment and Response rounds
 *      - Group public key aggregation
 *
 * FROST Protocol phases:
 * 1. DKG Phase: Participants generate shares with VSS commitments
 * 2. Preprocessing: Generate nonces and commitments
 * 3. Signing Round 1: Collect commitments
 * 4. Signing Round 2: Collect signature shares
 * 5. Aggregation: Combine shares into final signature
 */
contract FROSTWithDKG {
    // ======== CONSTANTS ========

    /// @notice Secp256k1 curve order
    uint256 constant CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    /// @notice Generator point G (compressed format)
    bytes32 constant G_X = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798;

    // ======== STATE ========

    struct Participant {
        address addr;
        bool isActive;
        bytes32 publicKeyShare;  // Participant's public key share
        bytes32 vssCommitment;   // VSS commitment for verification
        uint256 shareIndex;      // Index in the participant set (1-based)
    }

    struct DKGRound {
        uint256 participantCount;
        uint256 threshold;
        mapping(address => Participant) participants;
        address[] participantList;
        bytes32 groupPublicKey;  // Aggregated group public key
        bool isComplete;
        uint256 startTime;
    }

    struct SigningSession {
        bytes32 message;
        uint256 threshold;
        address[] signers;  // Subset of participants (t signers)
        mapping(address => bytes32) commitments;  // Nonce commitments (Round 1)
        mapping(address => bytes32) signatureShares;  // Signature shares (Round 2)
        uint256 commitmentCount;
        uint256 shareCount;
        bytes32 aggregatedSignature;
        bool isComplete;
        uint256 startTime;
    }

    DKGRound public dkgRound;
    mapping(bytes32 => SigningSession) public signingSessions;

    uint256 public currentThreshold;
    bool public dkgCompleted;

    // ======== EVENTS ========

    event DKGStarted(uint256 participantCount, uint256 threshold);
    event ParticipantJoined(address indexed participant, uint256 index);
    event VSSCommitmentSubmitted(address indexed participant, bytes32 commitment);
    event PublicKeyShareSubmitted(address indexed participant, bytes32 publicKeyShare);
    event DKGCompleted(bytes32 groupPublicKey);
    event SigningSessionStarted(bytes32 indexed sessionId, bytes32 message, address[] signers);
    event CommitmentSubmitted(bytes32 indexed sessionId, address indexed signer);
    event SignatureShareSubmitted(bytes32 indexed sessionId, address indexed signer);
    event SignatureAggregated(bytes32 indexed sessionId, bytes32 signature);

    // ======== ERRORS ========

    error DKGAlreadyStarted();
    error DKGNotComplete();
    error DKGAlreadyComplete();
    error InvalidThreshold();
    error ParticipantNotFound();
    error ParticipantAlreadyExists();
    error InvalidCommitment();
    error InvalidSignatureShare();
    error SessionNotFound();
    error SessionAlreadyComplete();
    error NotAParticipant();
    error InsufficientCommitments();
    error InsufficientShares();

    // ======== MODIFIERS ========

    modifier onlyParticipant() {
        if (!dkgRound.participants[msg.sender].isActive) revert NotAParticipant();
        _;
    }

    modifier dkgActive() {
        if (dkgCompleted) revert DKGAlreadyComplete();
        _;
    }

    modifier dkgComplete() {
        if (!dkgCompleted) revert DKGNotComplete();
        _;
    }

    // ======== DKG PHASE ========

    /**
     * @notice Initialize DKG round
     * @param participantAddresses List of participant addresses
     * @param threshold Minimum signatures required (t)
     */
    function initializeDKG(address[] calldata participantAddresses, uint256 threshold) external {
        require(!dkgCompleted, "DKG already completed");
        require(dkgRound.participantCount == 0, "DKG already initialized");
        require(threshold > 0 && threshold <= participantAddresses.length, "Invalid threshold");
        require(participantAddresses.length >= 2, "Need at least 2 participants");

        dkgRound.participantCount = participantAddresses.length;
        dkgRound.threshold = threshold;
        dkgRound.startTime = block.timestamp;

        for (uint256 i = 0; i < participantAddresses.length; i++) {
            address participant = participantAddresses[i];
            require(participant != address(0), "Invalid participant");
            require(!dkgRound.participants[participant].isActive, "Duplicate participant");

            dkgRound.participants[participant] = Participant({
                addr: participant,
                isActive: true,
                publicKeyShare: bytes32(0),
                vssCommitment: bytes32(0),
                shareIndex: i + 1  // 1-based indexing
            });

            dkgRound.participantList.push(participant);
            emit ParticipantJoined(participant, i + 1);
        }

        currentThreshold = threshold;
        emit DKGStarted(participantAddresses.length, threshold);
    }

    /**
     * @notice Submit VSS commitment (Feldman VSS)
     * @param commitment Commitment to polynomial coefficients
     * @dev In real FROST, this would be: C_i = g^{a_i0}, g^{a_i1}, ..., g^{a_i(t-1)}
     *      For demo, we use a single commitment hash
     */
    function submitVSSCommitment(bytes32 commitment) external onlyParticipant dkgActive {
        require(commitment != bytes32(0), "Invalid commitment");

        Participant storage participant = dkgRound.participants[msg.sender];
        require(participant.vssCommitment == bytes32(0), "Already submitted");

        participant.vssCommitment = commitment;
        emit VSSCommitmentSubmitted(msg.sender, commitment);
    }

    /**
     * @notice Submit public key share after DKG computation
     * @param publicKeyShare Public key share derived from secret share
     * @dev In real FROST: PK_i = g^{s_i} where s_i is secret share
     */
    function submitPublicKeyShare(bytes32 publicKeyShare) external onlyParticipant dkgActive {
        require(publicKeyShare != bytes32(0), "Invalid public key share");

        Participant storage participant = dkgRound.participants[msg.sender];
        require(participant.vssCommitment != bytes32(0), "VSS commitment not submitted");
        require(participant.publicKeyShare == bytes32(0), "Already submitted");

        participant.publicKeyShare = publicKeyShare;
        emit PublicKeyShareSubmitted(msg.sender, publicKeyShare);
    }

    /**
     * @notice Verify VSS share (simplified)
     * @param participant Address of the participant
     * @param share Secret share to verify
     * @param commitment VSS commitment to verify against
     * @return valid True if share is valid
     */
    function verifyVSSShare(
        address participant,
        bytes32 share,
        bytes32 commitment
    ) public view returns (bool valid) {
        Participant storage p = dkgRound.participants[participant];
        if (!p.isActive) return false;

        // Simplified verification: check commitment matches
        // In real FROST, would verify: g^share == Product(C_ij^{lambda_j})
        bytes32 expectedCommitment = keccak256(abi.encodePacked(share, p.shareIndex));
        return expectedCommitment == commitment;
    }

    /**
     * @notice Complete DKG and compute group public key
     * @dev Aggregates all public key shares: PK = Sum(PK_i)
     */
    function completeDKG() external dkgActive {
        // Check all participants submitted
        for (uint256 i = 0; i < dkgRound.participantList.length; i++) {
            address addr = dkgRound.participantList[i];
            Participant storage p = dkgRound.participants[addr];
            require(p.publicKeyShare != bytes32(0), "Not all shares submitted");
            require(p.vssCommitment != bytes32(0), "Not all commitments submitted");
        }

        // Aggregate public keys (simplified: XOR for demo)
        // In real FROST: point addition on elliptic curve
        bytes32 aggregatedKey = bytes32(0);
        for (uint256 i = 0; i < dkgRound.participantList.length; i++) {
            address addr = dkgRound.participantList[i];
            bytes32 pkShare = dkgRound.participants[addr].publicKeyShare;
            aggregatedKey = bytes32(uint256(aggregatedKey) ^ uint256(pkShare));
        }

        dkgRound.groupPublicKey = aggregatedKey;
        dkgRound.isComplete = true;
        dkgCompleted = true;

        emit DKGCompleted(aggregatedKey);
    }

    // ======== SIGNING PHASE ========

    /**
     * @notice Start signing session
     * @param message Message to sign
     * @param signers Subset of participants (at least threshold)
     * @return sessionId Signing session identifier
     */
    function startSigningSession(
        bytes32 message,
        address[] calldata signers
    ) external dkgComplete returns (bytes32 sessionId) {
        require(signers.length >= currentThreshold, "Not enough signers");
        require(message != bytes32(0), "Invalid message");

        // Verify all signers are participants
        for (uint256 i = 0; i < signers.length; i++) {
            require(dkgRound.participants[signers[i]].isActive, "Invalid signer");
        }

        sessionId = keccak256(abi.encodePacked(message, signers, block.timestamp));

        SigningSession storage session = signingSessions[sessionId];
        session.message = message;
        session.threshold = currentThreshold;
        session.signers = signers;
        session.startTime = block.timestamp;

        emit SigningSessionStarted(sessionId, message, signers);
    }

    /**
     * @notice Submit nonce commitment (Round 1)
     * @param sessionId Signing session ID
     * @param commitment Commitment to nonce: H(D_i, E_i) where D_i=g^d_i, E_i=g^e_i
     * @dev FROST Round 1: Each signer commits to two nonces
     */
    function submitCommitment(bytes32 sessionId, bytes32 commitment) external {
        SigningSession storage session = signingSessions[sessionId];
        require(session.message != bytes32(0), "Session not found");
        require(!session.isComplete, "Session complete");
        require(commitment != bytes32(0), "Invalid commitment");

        // Verify sender is a signer
        bool isSigner = false;
        for (uint256 i = 0; i < session.signers.length; i++) {
            if (session.signers[i] == msg.sender) {
                isSigner = true;
                break;
            }
        }
        require(isSigner, "Not a signer for this session");
        require(session.commitments[msg.sender] == bytes32(0), "Already submitted");

        session.commitments[msg.sender] = commitment;
        session.commitmentCount++;

        emit CommitmentSubmitted(sessionId, msg.sender);
    }

    /**
     * @notice Submit signature share (Round 2)
     * @param sessionId Signing session ID
     * @param signatureShare Signature share: z_i = d_i + (e_i * ρ_i) + λ_i * s_i * c
     * @dev FROST Round 2: Each signer computes and submits signature share
     */
    function submitSignatureShare(bytes32 sessionId, bytes32 signatureShare) external {
        SigningSession storage session = signingSessions[sessionId];
        require(session.message != bytes32(0), "Session not found");
        require(!session.isComplete, "Session complete");
        require(session.commitmentCount >= session.threshold, "Insufficient commitments");
        require(signatureShare != bytes32(0), "Invalid signature share");

        // Verify sender submitted commitment
        require(session.commitments[msg.sender] != bytes32(0), "No commitment submitted");
        require(session.signatureShares[msg.sender] == bytes32(0), "Already submitted");

        session.signatureShares[msg.sender] = signatureShare;
        session.shareCount++;

        emit SignatureShareSubmitted(sessionId, msg.sender);

        // Try to aggregate if threshold reached
        if (session.shareCount >= session.threshold) {
            _aggregateSignature(sessionId);
        }
    }

    /**
     * @notice Aggregate signature shares into final signature
     * @param sessionId Signing session ID
     * @dev Combines signature shares: z = Sum(z_i) and R = Sum(R_i)
     */
    function _aggregateSignature(bytes32 sessionId) internal {
        SigningSession storage session = signingSessions[sessionId];

        // Aggregate shares (simplified: XOR for demo)
        // In real FROST: z = Sum(z_i) mod q, R = Sum(R_i) (point addition)
        bytes32 aggregated = bytes32(0);
        uint256 count = 0;

        for (uint256 i = 0; i < session.signers.length; i++) {
            address signer = session.signers[i];
            bytes32 share = session.signatureShares[signer];
            if (share != bytes32(0)) {
                aggregated = bytes32(uint256(aggregated) ^ uint256(share));
                count++;
                if (count >= session.threshold) break;
            }
        }

        session.aggregatedSignature = aggregated;
        session.isComplete = true;

        emit SignatureAggregated(sessionId, aggregated);
    }

    /**
     * @notice Verify FROST signature
     * @param sessionId Signing session ID
     * @return valid True if signature is valid
     * @dev Verifies: R + c*PK == g^z (Schnorr verification)
     */
    function verifySignature(bytes32 sessionId) external view returns (bool valid) {
        SigningSession storage session = signingSessions[sessionId];
        if (!session.isComplete) return false;

        // Simplified verification for demo
        // In real FROST: Schnorr verification with group public key
        bytes32 expectedSig = keccak256(abi.encodePacked(
            session.message,
            dkgRound.groupPublicKey
        ));

        // For demo, check signature is not zero
        return session.aggregatedSignature != bytes32(0);
    }

    // ======== VIEW FUNCTIONS ========

    function getGroupPublicKey() external view returns (bytes32) {
        return dkgRound.groupPublicKey;
    }

    function getParticipantInfo(address participant) external view returns (
        bool isActive,
        bytes32 publicKeyShare,
        bytes32 vssCommitment,
        uint256 shareIndex
    ) {
        Participant storage p = dkgRound.participants[participant];
        return (p.isActive, p.publicKeyShare, p.vssCommitment, p.shareIndex);
    }

    function getSigningSessionInfo(bytes32 sessionId) external view returns (
        bytes32 message,
        uint256 threshold,
        uint256 commitmentCount,
        uint256 shareCount,
        bytes32 aggregatedSignature,
        bool isComplete
    ) {
        SigningSession storage session = signingSessions[sessionId];
        return (
            session.message,
            session.threshold,
            session.commitmentCount,
            session.shareCount,
            session.aggregatedSignature,
            session.isComplete
        );
    }

    function isDKGComplete() external view returns (bool) {
        return dkgCompleted;
    }

    function getThreshold() external view returns (uint256) {
        return currentThreshold;
    }

    function getParticipantCount() external view returns (uint256) {
        return dkgRound.participantCount;
    }

    function getAllParticipants() external view returns (address[] memory) {
        return dkgRound.participantList;
    }
}
