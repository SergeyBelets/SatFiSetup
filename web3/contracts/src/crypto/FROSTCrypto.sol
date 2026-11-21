// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "./Secp256k1.sol";
import "./Schnorr.sol";

/**
 * @title FROSTCrypto
 * @notice FROST (Flexible Round-Optimized Schnorr Threshold) signatures with real cryptography
 * @dev Implements:
 *      - DKG (Distributed Key Generation) with Feldman VSS
 *      - Threshold Schnorr signatures (t-of-n) using real secp256k1
 *      - Two-round signing protocol with proper curve operations
 *      - Group public key aggregation with elliptic curve math
 */
contract FROSTCrypto {
    using Secp256k1 for *;

    // ======== STATE ========

    struct Participant {
        address addr;
        bool isActive;
        Secp256k1.Point publicKeyShare;  // PK_i = g^{s_i}
        Secp256k1.Point[] vssCommitments; // [C_i0, C_i1, ..., C_i(t-1)] where C_ij = g^{a_ij}
        uint256 shareIndex;               // 1-based index
    }

    struct DKGRound {
        uint256 participantCount;
        uint256 threshold;
        mapping(address => Participant) participants;
        address[] participantList;
        Secp256k1.Point groupPublicKey;   // PK = Sum(PK_i) - real curve point!
        bool isComplete;
        uint256 startTime;
    }

    struct SigningSession {
        bytes32 message;
        uint256 threshold;
        address[] signers;
        mapping(address => Secp256k1.Point) nonceCommitments;  // D_i and E_i commitments
        mapping(address => uint256) signatureShares;            // z_i signature shares
        uint256 commitmentCount;
        uint256 shareCount;
        Schnorr.Signature aggregatedSignature;                  // Final (R, s) signature
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
    event VSSCommitmentsSubmitted(address indexed participant, uint256 commitmentCount);
    event PublicKeyShareSubmitted(address indexed participant, uint256 pkX, uint256 pkY);
    event DKGCompleted(uint256 groupPKX, uint256 groupPKY);
    event SigningSessionStarted(bytes32 indexed sessionId, bytes32 message, address[] signers);
    event NonceCommitmentSubmitted(bytes32 indexed sessionId, address indexed signer);
    event SignatureShareSubmitted(bytes32 indexed sessionId, address indexed signer, uint256 share);
    event SignatureAggregated(bytes32 indexed sessionId, uint256 r, uint256 s);

    // ======== ERRORS ========

    error DKGNotComplete();
    error DKGAlreadyComplete();
    error InvalidThreshold();
    error NotAParticipant();
    error InvalidCommitment();
    error InvalidPoint();
    error SessionNotFound();
    error InsufficientCommitments();

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

            Participant storage p = dkgRound.participants[participant];
            p.addr = participant;
            p.isActive = true;
            p.shareIndex = i + 1;

            dkgRound.participantList.push(participant);
            emit ParticipantJoined(participant, i + 1);
        }

        currentThreshold = threshold;
        emit DKGStarted(participantAddresses.length, threshold);
    }

    /**
     * @notice Submit VSS commitments (Feldman VSS)
     * @param commitments Array of curve points [C_i0, C_i1, ..., C_i(t-1)]
     * @dev C_ij = g^{a_ij} where a_ij are polynomial coefficients
     *      Polynomial: f_i(x) = a_i0 + a_i1*x + ... + a_i(t-1)*x^(t-1)
     */
    function submitVSSCommitments(
        Secp256k1.Point[] calldata commitments
    ) external onlyParticipant dkgActive {
        require(commitments.length == currentThreshold, "Wrong commitment count");

        Participant storage participant = dkgRound.participants[msg.sender];
        require(participant.vssCommitments.length == 0, "Already submitted");

        // Verify all commitments are on curve
        for (uint256 i = 0; i < commitments.length; i++) {
            if (!Secp256k1.isOnCurve(commitments[i])) revert InvalidPoint();
            participant.vssCommitments.push(commitments[i]);
        }

        emit VSSCommitmentsSubmitted(msg.sender, commitments.length);
    }

    /**
     * @notice Submit public key share
     * @param publicKeyShare Public key share PK_i = g^{s_i}
     * @dev s_i is the secret share (kept private)
     *      PK_i is derived from s_i and shared publicly
     */
    function submitPublicKeyShare(
        Secp256k1.Point calldata publicKeyShare
    ) external onlyParticipant dkgActive {
        if (!Secp256k1.isOnCurve(publicKeyShare)) revert InvalidPoint();

        Participant storage participant = dkgRound.participants[msg.sender];
        require(participant.vssCommitments.length > 0, "VSS commitments not submitted");
        require(participant.publicKeyShare.x == 0, "Already submitted");

        participant.publicKeyShare = publicKeyShare;
        emit PublicKeyShareSubmitted(msg.sender, publicKeyShare.x, publicKeyShare.y);
    }

    /**
     * @notice Verify VSS share using Feldman VSS verification
     * @param participant Address of participant who created the commitments
     * @param shareIndex Index j of the share recipient
     * @param share Secret share s_ij
     * @return valid True if g^{s_ij} == Product(C_ik^{j^k}) for k=0 to t-1
     */
    function verifyVSSShare(
        address participant,
        uint256 shareIndex,
        uint256 share
    ) public view returns (bool) {
        Participant storage p = dkgRound.participants[participant];
        if (!p.isActive) return false;
        if (p.vssCommitments.length == 0) return false;

        // Left side: g^{share}
        Secp256k1.Point memory left = Secp256k1.scalarBaseMul(share);

        // Right side: Product(C_ik^{j^k}) for k=0 to t-1
        Secp256k1.Point memory right = Secp256k1.Point(0, 0);

        for (uint256 k = 0; k < p.vssCommitments.length; k++) {
            // Compute j^k
            uint256 power = _powMod(shareIndex, k, Secp256k1.N);

            // Compute C_ik^{j^k}
            Secp256k1.Point memory term = Secp256k1.scalarMul(power, p.vssCommitments[k]);

            // Accumulate
            right = Secp256k1.addPoints(right, term);
        }

        return Secp256k1.equals(left, right);
    }

    /**
     * @notice Complete DKG and compute group public key
     * @dev Aggregates all public key shares: PK = Sum(PK_i) using real curve addition
     */
    function completeDKG() external dkgActive {
        // Check all participants submitted
        for (uint256 i = 0; i < dkgRound.participantList.length; i++) {
            address addr = dkgRound.participantList[i];
            Participant storage p = dkgRound.participants[addr];
            require(p.publicKeyShare.x != 0, "Not all shares submitted");
            require(p.vssCommitments.length > 0, "Not all commitments submitted");
        }

        // Aggregate public keys: PK = PK_1 + PK_2 + ... + PK_n (curve point addition!)
        Secp256k1.Point memory aggregatedKey = dkgRound.participants[dkgRound.participantList[0]].publicKeyShare;

        for (uint256 i = 1; i < dkgRound.participantList.length; i++) {
            address addr = dkgRound.participantList[i];
            Secp256k1.Point memory pkShare = dkgRound.participants[addr].publicKeyShare;
            aggregatedKey = Secp256k1.addPoints(aggregatedKey, pkShare);
        }

        dkgRound.groupPublicKey = aggregatedKey;
        dkgRound.isComplete = true;
        dkgCompleted = true;

        emit DKGCompleted(aggregatedKey.x, aggregatedKey.y);
    }

    // ======== SIGNING PHASE ========

    /**
     * @notice Start signing session
     * @param message Message to sign (32 bytes)
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
     * @param nonceCommitment Commitment point D_i + E_i
     * @dev Each signer commits to nonces: D_i = g^{d_i}, E_i = g^{e_i}
     *      Commitment is the sum D_i + E_i
     */
    function submitNonceCommitment(
        bytes32 sessionId,
        Secp256k1.Point calldata nonceCommitment
    ) external {
        SigningSession storage session = signingSessions[sessionId];
        require(session.message != bytes32(0), "Session not found");
        require(!session.isComplete, "Session complete");
        if (!Secp256k1.isOnCurve(nonceCommitment)) revert InvalidCommitment();

        // Verify sender is a signer
        bool isSigner = false;
        for (uint256 i = 0; i < session.signers.length; i++) {
            if (session.signers[i] == msg.sender) {
                isSigner = true;
                break;
            }
        }
        require(isSigner, "Not a signer for this session");
        require(session.nonceCommitments[msg.sender].x == 0, "Already submitted");

        session.nonceCommitments[msg.sender] = nonceCommitment;
        session.commitmentCount++;

        emit NonceCommitmentSubmitted(sessionId, msg.sender);
    }

    /**
     * @notice Submit signature share (Round 2)
     * @param sessionId Signing session ID
     * @param signatureShare Signature share z_i
     * @dev z_i = d_i + (e_i * ρ_i) + λ_i * s_i * c
     *      where:
     *      - d_i, e_i are secret nonces
     *      - ρ_i is binding value
     *      - λ_i is Lagrange coefficient
     *      - s_i is secret share
     *      - c is challenge
     */
    function submitSignatureShare(
        bytes32 sessionId,
        uint256 signatureShare
    ) external {
        SigningSession storage session = signingSessions[sessionId];
        require(session.message != bytes32(0), "Session not found");
        require(!session.isComplete, "Session complete");
        require(session.commitmentCount >= session.threshold, "Insufficient commitments");
        require(signatureShare > 0, "Invalid signature share");

        // Verify sender submitted commitment
        require(session.nonceCommitments[msg.sender].x != 0, "No commitment submitted");
        require(session.signatureShares[msg.sender] == 0, "Already submitted");

        session.signatureShares[msg.sender] = signatureShare;
        session.shareCount++;

        emit SignatureShareSubmitted(sessionId, msg.sender, signatureShare);

        // Try to aggregate if threshold reached
        if (session.shareCount >= session.threshold) {
            _aggregateSignature(sessionId);
        }
    }

    /**
     * @notice Aggregate signature shares into final signature
     * @param sessionId Signing session ID
     * @dev Combines shares:
     *      R = Sum(R_i) (curve point addition)
     *      s = Sum(z_i) mod N (scalar addition)
     *      Final signature: (R, s)
     */
    function _aggregateSignature(bytes32 sessionId) internal {
        SigningSession storage session = signingSessions[sessionId];

        // Aggregate nonce commitments: R = Sum(R_i)
        Secp256k1.Point memory R = Secp256k1.Point(0, 0);
        uint256 count = 0;

        for (uint256 i = 0; i < session.signers.length && count < session.threshold; i++) {
            address signer = session.signers[i];
            Secp256k1.Point memory commitment = session.nonceCommitments[signer];

            if (commitment.x != 0) {
                R = Secp256k1.addPoints(R, commitment);
                count++;
            }
        }

        // Aggregate signature shares: s = Sum(z_i) mod N
        uint256 s = 0;
        count = 0;

        for (uint256 i = 0; i < session.signers.length && count < session.threshold; i++) {
            address signer = session.signers[i];
            uint256 share = session.signatureShares[signer];

            if (share != 0) {
                s = addmod(s, share, Secp256k1.N);
                count++;
            }
        }

        // Store final signature (R.x, s)
        session.aggregatedSignature = Schnorr.Signature(R.x, s);
        session.isComplete = true;

        emit SignatureAggregated(sessionId, R.x, s);
    }

    /**
     * @notice Verify FROST signature using Schnorr verification
     * @param sessionId Signing session ID
     * @return valid True if signature is valid
     * @dev Verifies: R + c*PK == g^s (Schnorr equation with real curve operations)
     */
    function verifySignature(bytes32 sessionId) external view returns (bool) {
        SigningSession storage session = signingSessions[sessionId];
        if (!session.isComplete) return false;

        // Create public key from group public key
        Schnorr.PublicKey memory publicKey = Schnorr.publicKeyFromPoint(dkgRound.groupPublicKey);

        // Verify using Schnorr verification
        return Schnorr.verify(session.message, session.aggregatedSignature, publicKey);
    }

    // ======== VIEW FUNCTIONS ========

    function getGroupPublicKey() external view returns (uint256 x, uint256 y) {
        return (dkgRound.groupPublicKey.x, dkgRound.groupPublicKey.y);
    }

    function getParticipantInfo(address participant) external view returns (
        bool isActive,
        uint256 pkX,
        uint256 pkY,
        uint256 vssCommitmentCount,
        uint256 shareIndex
    ) {
        Participant storage p = dkgRound.participants[participant];
        return (
            p.isActive,
            p.publicKeyShare.x,
            p.publicKeyShare.y,
            p.vssCommitments.length,
            p.shareIndex
        );
    }

    function getVSSCommitment(address participant, uint256 index) external view returns (uint256 x, uint256 y) {
        Participant storage p = dkgRound.participants[participant];
        require(index < p.vssCommitments.length, "Index out of bounds");
        return (p.vssCommitments[index].x, p.vssCommitments[index].y);
    }

    function getSigningSessionInfo(bytes32 sessionId) external view returns (
        bytes32 message,
        uint256 threshold,
        uint256 commitmentCount,
        uint256 shareCount,
        uint256 sigR,
        uint256 sigS,
        bool isComplete
    ) {
        SigningSession storage session = signingSessions[sessionId];
        return (
            session.message,
            session.threshold,
            session.commitmentCount,
            session.shareCount,
            session.aggregatedSignature.r,
            session.aggregatedSignature.s,
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

    // ======== HELPER FUNCTIONS ========

    /**
     * @notice Modular exponentiation
     * @param base Base
     * @param exponent Exponent
     * @param modulus Modulus
     * @return result base^exponent mod modulus
     */
    function _powMod(uint256 base, uint256 exponent, uint256 modulus) internal pure returns (uint256 result) {
        result = 1;
        base = base % modulus;

        while (exponent > 0) {
            if (exponent & 1 == 1) {
                result = mulmod(result, base, modulus);
            }
            exponent >>= 1;
            base = mulmod(base, base, modulus);
        }
    }
}
