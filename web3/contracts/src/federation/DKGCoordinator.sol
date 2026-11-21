// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./interfaces/IDKGCoordinator.sol";
import "./DKGHash.sol";
import "../vendor/cryptography/Secp256k1.sol";

/**
 * @title DKGCoordinator
 * @notice On-chain coordinator for Distributed Key Generation (DKG)
 * @dev Manages the DKG process for FROST threshold signatures.
 *      The actual cryptographic operations happen off-chain, but commitments
 *      and hashes are stored on-chain for transparency and verification.
 */
contract DKGCoordinator is IDKGCoordinator {
    // V1 ceremonies are separate from unsupported legacy sessions. All N must attest.
    struct Ceremony {
        bytes32 contextHash;
        uint64 epoch;
        bytes32 purpose;
        bytes32 resultHash;
        bool normalizationNegated;
        uint16 receipts;
        uint16 approvals;
    }
    mapping(bytes32 => Ceremony) public ceremonies;
    mapping(bytes32 => mapping(address => bytes32)) public round1Hashes;
    mapping(bytes32 => mapping(address => bytes32)) public transportKeys;
    mapping(bytes32 => mapping(address => mapping(address => bytes32))) public receiptHashes;
    mapping(bytes32 => mapping(address => bytes32)) public resultApprovals;
    uint256 public constant MAX_PARTICIPANTS = 16;
    bytes32 public constant PROTOCOL_V1 = DKGHash.PROTOCOL;
    address public ceremonyRegistrar = msg.sender;
    uint64 private integrationPhaseDuration;
    bool private v1CeremonyStarted;
    mapping(bytes32 => address) public ceremonyCreators;
    event CeremonyCreatorAuthorized(bytes32 indexed sessionId, address indexed creator);
    event ResultAttested(bytes32 indexed sessionId,address indexed participant,bytes32 resultHash);
    event CeremonyAborted(bytes32 indexed sessionId,address indexed participant,bytes32 reason);
    error InvalidManifest();
    error InvalidHash();
    error LegacyOnly();
    error InvalidBatch();
    error UnauthorizedCeremonyCreator();

    /// @notice Set the test deployment's phase window before its first V1 ceremony.
    function configureIntegrationPhaseDuration(uint64 duration) external {
        if (msg.sender != ceremonyRegistrar) revert UnauthorizedCeremonyCreator();
        if ((block.chainid != 31337 && block.chainid != 11155111) || v1CeremonyStarted
            || integrationPhaseDuration != 0 || duration < 1 hours || duration > 1 days) revert InvalidManifest();
        integrationPhaseDuration = duration;
    }

    /// @notice Delegate creation of one unused V1 ID. A zero address revokes the delegation.
    /// @dev Creation grants no ability to submit packets or attest outside the ceremony roster.
    function authorizeCeremonyCreator(bytes32 id, address creator) external {
        if (msg.sender != ceremonyRegistrar) revert UnauthorizedCeremonyCreator();
        if (id == 0 || sessions[id].sessionId != 0) revert InvalidManifest();
        ceremonyCreators[id] = creator;
        emit CeremonyCreatorAuthorized(id, creator);
    }

    function createCeremony(bytes32 id,address[] calldata roster,uint16 threshold,bytes32[] calldata keys,uint64 epoch,bytes32 purpose,bytes32 protocol) external {
        address creator = ceremonyCreators[id];
        if (msg.sender != (creator == address(0) ? ceremonyRegistrar : creator)) {
            revert UnauthorizedCeremonyCreator();
        }
        uint256 n=roster.length;
        if (id==0 || sessions[id].sessionId!=0 || n<2 || n>MAX_PARTICIPANTS || threshold<2 || threshold>n || keys.length!=n || protocol!=PROTOCOL_V1) revert InvalidManifest();
        v1CeremonyStarted = true;
        if(purpose!=keccak256("EVM_AUTH") && purpose!=keccak256("BTC_SPEND")) revert InvalidManifest();
        DKGSession storage s=sessions[id];
        s.sessionId=id; s.participants=roster; s.threshold=threshold; s.initiator=msg.sender;
        s.currentPhase=DKGPhase.CommitmentsPhase; s.phaseDeadline=block.timestamp+PHASE_DURATION();
        for(uint256 i;i<n;++i) {
            address p=roster[i];
            if(p==address(0)||isParticipant[id][p]||keys[i]==0) revert InvalidManifest();
            for(uint256 j;j<i;++j) if(keys[j]==keys[i]) revert InvalidManifest();
            isParticipant[id][p]=true; participantIndex[id][p]=i+1; transportKeys[id][p]=keys[i];
        }
        ceremonies[id].contextHash=DKGHash.contextHash(DKGHash.Context(block.chainid,address(this),id,epoch,purpose,protocol,keccak256(abi.encode(roster)),keccak256(abi.encode(keys)),uint16(n),threshold));
        ceremonies[id].epoch=epoch; ceremonies[id].purpose=purpose;
        emit DKGInitiated(id,"V1",threshold);
    }

    function submitRound1(bytes32 id,bytes32 packageHash,bytes32[] calldata commitments) external
        sessionExists(id) onlyParticipant(id) inPhase(id,DKGPhase.CommitmentsPhase) phaseNotExpired(id) {
        if(ceremonies[id].contextHash==0) revert InvalidManifest();
        if(packageHash==0) revert InvalidHash();
        if(round1Hashes[id][msg.sender]!=0) revert AlreadySubmitted();
        if(commitments.length!=sessions[id].threshold) revert InvalidCommitmentsLength();
        for(uint256 i;i<commitments.length;++i) if(commitments[i]==0) revert InvalidHash();
        round1Hashes[id][msg.sender]=packageHash;
        vssCommitments[id][msg.sender]=commitments; _hasSubmittedCommitments[id][msg.sender]=true;
        sessions[id].commitmentCount++;
        emit CommitmentSubmitted(id,msg.sender);
        if(sessions[id].commitmentCount==sessions[id].participants.length) _advanceToPhase(id,DKGPhase.SharesPhase);
    }

    /// @notice Receipt is a recipient attestation after local library validation, not on-chain VSS.
    function submitReceipt(bytes32 id,address sender,bytes32 expectedShareHash) external
        sessionExists(id) onlyParticipant(id) inPhase(id,DKGPhase.PublicKeyPhase) phaseNotExpired(id) {
        _submitReceipt(id, sender, expectedShareHash, false);
    }

    /// @notice One recipient acknowledges all locally verified encrypted shares and its own public point.
    /// @dev Same caller/session/phase as the single-item endpoints; exact existing entries are retry-safe.
    function submitReceiptsAndPublicKeyShare(bytes32 id,address[] calldata senders,bytes32[] calldata expectedShareHashes,bytes calldata publicKeyShare) external
        sessionExists(id) onlyParticipant(id) inPhase(id,DKGPhase.PublicKeyPhase) phaseNotExpired(id) {
        if (ceremonies[id].contextHash == 0 || senders.length > sessions[id].participants.length - 1
            || senders.length != expectedShareHashes.length) revert InvalidBatch();
        for (uint256 i; i < senders.length; ++i) {
            for (uint256 j; j < i; ++j) if (senders[i] == senders[j]) revert InvalidBatch();
            _submitReceipt(id, senders[i], expectedShareHashes[i], true);
        }
        _submitPublicKeyShare(id, publicKeyShare, true);
    }

    function _submitReceipt(bytes32 id,address sender,bytes32 expectedShareHash,bool exactRetry) private {
        if(ceremonies[id].contextHash==0 || sender==msg.sender || !isParticipant[id][sender]) revert InvalidRecipient();
        if(expectedShareHash==0 || expectedShareHash!=shareHashes[id][sender][msg.sender]) revert InvalidHash();
        bytes32 hash = keccak256(abi.encode(ceremonies[id].contextHash,sender,msg.sender,expectedShareHash,true));
        if(receiptHashes[id][sender][msg.sender]!=0) {
            if (exactRetry && receiptHashes[id][sender][msg.sender] == hash) return;
            revert AlreadySubmitted();
        }
        receiptHashes[id][sender][msg.sender]=hash;
        ceremonies[id].receipts++;
    }

    function computeResultHash(bytes32 id,bool negated,bytes32 groupX) public view returns(bytes32) {
        DKGSession storage s=sessions[id]; uint256 n=s.participants.length;
        if(ceremonies[id].contextHash==0 || s.pkShareCount!=n || ceremonies[id].receipts!=n*(n-1)) revert DKGNotComplete();
        bytes32[] memory r1=new bytes32[](n); bytes32[] memory r2=new bytes32[](n*(n-1));
        bytes[] memory points=new bytes[](n); uint256 k;
        for(uint256 i;i<n;++i) {
            address p=s.participants[i]; r1[i]=round1Hashes[id][p]; points[i]=publicKeyShares[id][p];
            for(uint256 j;j<n;++j) if(i!=j) r2[k++]=shareHashes[id][p][s.participants[j]];
        }
        return DKGHash.result(ceremonies[id].contextHash,keccak256(abi.encode(r1)),keccak256(abi.encode(r2)),negated,groupX,keccak256(abi.encode(s.participants,points)));
    }

    function attestResult(bytes32 id,bool negated,bytes calldata groupKey,bytes32 expectedResult) external
        sessionExists(id) onlyParticipant(id) inPhase(id,DKGPhase.PublicKeyPhase) phaseNotExpired(id) {
        if(groupKey.length!=32) revert InvalidGroupPublicKey();
        bytes32 x=_validateGroupPublicKey(groupKey);
        bytes32 hash=computeResultHash(id,negated,x);
        if(expectedResult!=hash) revert InvalidHash();
        Ceremony storage c=ceremonies[id];
        if(c.resultHash!=0 && c.resultHash!=hash) revert InvalidHash();
        if(resultApprovals[id][msg.sender]!=0) revert AlreadySubmitted();
        resultApprovals[id][msg.sender]=hash; c.resultHash=hash; c.normalizationNegated=negated; c.approvals++;
        emit ResultAttested(id,msg.sender,hash);
        if(c.approvals==sessions[id].participants.length) {
            sessions[id].groupPublicKey=groupKey; sessions[id].isCompleted=true; sessions[id].currentPhase=DKGPhase.Completed;
            emit DKGCompleted(id,groupKey);
        }
    }

    function abortCeremony(bytes32 id,bytes32 reason) external sessionExists(id) onlyParticipant(id) {
        if(ceremonies[id].contextHash==0 || uint8(sessions[id].currentPhase)>=uint8(DKGPhase.Completed)) revert InvalidPhase();
        sessions[id].currentPhase=DKGPhase.Aborted;
        emit CeremonyAborted(id,msg.sender,reason);
    }

    function getCompletedResult(bytes32 id) external view returns(bytes32 context,bytes32 transcript,uint64 epoch,bytes32 purpose,bytes memory group,address[] memory roster,uint256 threshold) {
        if(!sessions[id].isCompleted || ceremonies[id].resultHash==0) revert DKGNotComplete();
        Ceremony storage c=ceremonies[id]; DKGSession storage s=sessions[id];
        return(c.contextHash,c.resultHash,c.epoch,c.purpose,s.groupPublicKey,s.participants,s.threshold);
    }

    // ======== CONSTANTS ========

    /// @notice Mainnet needs time for sequential finalized encrypted messages.
    /// The invitation commits this network-specific protocol duration.
    function PHASE_DURATION() public view returns (uint256) {
        if (block.chainid == 1) return 1 days;
        return integrationPhaseDuration == 0 ? 1 hours : integrationPhaseDuration;
    }

    // ======== STATE ========

    struct DKGSession {
        bytes32 sessionId;
        string federationId;
        address[] participants;
        uint256 threshold;
        DKGPhase currentPhase;
        uint256 phaseDeadline;
        uint256 commitmentCount;
        uint256 shareCount;
        uint256 pkShareCount;
        bytes groupPublicKey;
        bool isCompleted;
        address initiator;
    }

    // sessionId => session data
    mapping(bytes32 => DKGSession) private sessions;

    // sessionId => participant => VSS commitments submitted
    mapping(bytes32 => mapping(address => bool)) private _hasSubmittedCommitments;

    // sessionId => participant => commitment hashes (array of t coefficients)
    mapping(bytes32 => mapping(address => bytes32[])) private vssCommitments;

    // sessionId => from => to => share hash
    mapping(bytes32 => mapping(address => mapping(address => bytes32))) private shareHashes;

    // sessionId => from => number of shares submitted
    mapping(bytes32 => mapping(address => uint256)) private sharesSubmittedBy;

    // sessionId => participant => public key share submitted
    mapping(bytes32 => mapping(address => bool)) private _hasSubmittedPKShare;

    // sessionId => participant => public key share bytes
    mapping(bytes32 => mapping(address => bytes)) private publicKeyShares;

    // sessionId => group public key hash => approval count
    mapping(bytes32 => mapping(bytes32 => uint256)) private groupPublicKeyApprovals;

    // sessionId => group public key hash => participant => approved
    mapping(bytes32 => mapping(bytes32 => mapping(address => bool))) private _hasApprovedGroupPublicKey;

    // Track participant index for efficient lookup
    mapping(bytes32 => mapping(address => uint256)) private participantIndex;
    mapping(bytes32 => mapping(address => bool)) private isParticipant;

    // Session counter for unique IDs
    uint256 private sessionCounter;

    // ======== ERRORS ========

    error SessionNotFound();
    error NotAParticipant();
    error InvalidPhase();
    error PhaseExpired();
    error PhaseNotExpired();
    error AlreadySubmitted();
    error InvalidCommitmentsLength();
    error InvalidRecipient();
    error ShareAlreadySubmitted();
    error NotAllSharesSubmitted();
    error DKGNotComplete();
    error AlreadyCompleted();
    error InvalidThreshold();
    error InvalidParticipants();
    error NotEnoughParticipants();
    error EmptyPublicKey();
    error GroupPublicKeyNotApproved();
    error InvalidGroupPublicKey();
    error InvalidPublicKeyShare();

    // ======== MODIFIERS ========

    modifier sessionExists(bytes32 sessionId) {
        if (sessions[sessionId].sessionId == bytes32(0)) revert SessionNotFound();
        _;
    }

    modifier onlyParticipant(bytes32 sessionId) {
        if (!isParticipant[sessionId][msg.sender]) revert NotAParticipant();
        _;
    }

    modifier inPhase(bytes32 sessionId, DKGPhase expectedPhase) {
        if (sessions[sessionId].currentPhase != expectedPhase) revert InvalidPhase();
        _;
    }

    modifier phaseNotExpired(bytes32 sessionId) {
        if (block.timestamp > sessions[sessionId].phaseDeadline) revert PhaseExpired();
        _;
    }

    // ======== MAIN FUNCTIONS ========

    /**
     * @notice Initiate a new DKG session
     * @param participants Array of participant addresses
     * @param threshold Required signature threshold (t-of-n)
     * @param federationId Identifier for the federation
     * @return sessionId Unique session identifier
     */
    function initiateDKG(
        address[] calldata participants,
        uint256 threshold,
        string calldata federationId
    ) external returns (bytes32 sessionId) {
        if (participants.length < 2) revert NotEnoughParticipants();
        if (threshold == 0 || threshold > participants.length) revert InvalidThreshold();

        sessionCounter++;
        sessionId = keccak256(abi.encodePacked(
            block.timestamp,
            msg.sender,
            sessionCounter,
            federationId
        ));

        DKGSession storage session = sessions[sessionId];
        session.sessionId = sessionId;
        session.federationId = federationId;
        session.participants = participants;
        session.threshold = threshold;
        session.currentPhase = DKGPhase.CommitmentsPhase;
        session.phaseDeadline = block.timestamp + PHASE_DURATION();
        session.initiator = msg.sender;

        // Register participants
        for (uint256 i = 0; i < participants.length; i++) {
            address p = participants[i];
            if (p == address(0)) revert InvalidParticipants();
            if (isParticipant[sessionId][p]) revert InvalidParticipants(); // duplicate check

            isParticipant[sessionId][p] = true;
            participantIndex[sessionId][p] = i + 1; // 1-indexed for Lagrange
        }

        emit DKGInitiated(sessionId, federationId, threshold);
    }

    /**
     * @notice Submit VSS commitments for a session (Round 1)
     * @param sessionId The DKG session ID
     * @param commitments Array of VSS commitment hashes (one per polynomial coefficient)
     */
    function submitVSSCommitments(
        bytes32 sessionId,
        bytes32[] calldata commitments
    )
        external
        sessionExists(sessionId)
        onlyParticipant(sessionId)
        inPhase(sessionId, DKGPhase.CommitmentsPhase)
        phaseNotExpired(sessionId)
    {
        DKGSession storage session = sessions[sessionId];

        if (ceremonies[sessionId].contextHash != 0) revert LegacyOnly();
        if (_hasSubmittedCommitments[sessionId][msg.sender]) revert AlreadySubmitted();
        if (commitments.length != session.threshold) revert InvalidCommitmentsLength();

        _hasSubmittedCommitments[sessionId][msg.sender] = true;
        vssCommitments[sessionId][msg.sender] = commitments;
        session.commitmentCount++;

        emit CommitmentSubmitted(sessionId, msg.sender);

        // Auto-advance phase if all commitments received
        if (session.commitmentCount == session.participants.length) {
            _advanceToPhase(sessionId, DKGPhase.SharesPhase);
        }
    }

    /**
     * @notice Submit a share hash for a recipient (Round 2)
     * @param sessionId The DKG session ID
     * @param recipient The recipient of the share
     * @param shareHash Hash of the encrypted share
     */
    function submitShareHash(
        bytes32 sessionId,
        address recipient,
        bytes32 shareHash
    )
        external
        sessionExists(sessionId)
        onlyParticipant(sessionId)
        inPhase(sessionId, DKGPhase.SharesPhase)
        phaseNotExpired(sessionId)
    {
        _submitShareHash(sessionId, recipient, shareHash, false);
    }

    /// @notice Bounded V1 round-two hashes, all authenticated as the caller.
    function submitShareHashes(bytes32 sessionId,address[] calldata recipients,bytes32[] calldata hashes) external
        sessionExists(sessionId) onlyParticipant(sessionId) inPhase(sessionId,DKGPhase.SharesPhase) phaseNotExpired(sessionId) {
        uint256 n = recipients.length;
        if (ceremonies[sessionId].contextHash == 0 || n == 0 || n > sessions[sessionId].participants.length - 1
            || hashes.length != n) revert InvalidBatch();
        for (uint256 i; i < n; ++i) {
            for (uint256 j; j < i; ++j) if (recipients[i] == recipients[j]) revert InvalidBatch();
            _submitShareHash(sessionId, recipients[i], hashes[i], true);
        }
    }

    function _submitShareHash(bytes32 sessionId,address recipient,bytes32 shareHash,bool exactRetry) private {
        DKGSession storage session = sessions[sessionId];

        bool v1 = ceremonies[sessionId].contextHash != 0;
        if (shareHash == bytes32(0)) revert InvalidHash();
        if (v1 && recipient == msg.sender) revert InvalidRecipient();
        if (!isParticipant[sessionId][recipient]) revert InvalidRecipient();
        if (shareHashes[sessionId][msg.sender][recipient] != bytes32(0)) {
            if (exactRetry && shareHashes[sessionId][msg.sender][recipient] == shareHash) return;
            revert ShareAlreadySubmitted();
        }

        shareHashes[sessionId][msg.sender][recipient] = shareHash;
        sharesSubmittedBy[sessionId][msg.sender]++;
        session.shareCount++;

        emit ShareSubmitted(sessionId, msg.sender, recipient);

        // Auto-advance if all shares submitted (n * n shares total)
        uint256 n = session.participants.length;
        if (session.shareCount == (v1 ? n * (n - 1) : n * n)) {
            _advanceToPhase(sessionId, DKGPhase.PublicKeyPhase);
        }
    }

    /**
     * @notice Submit public key share (Round 3)
     * @param sessionId The DKG session ID
     * @param publicKeyShare SEC1 compressed (33 bytes) or uncompressed (65 bytes) point
     */
    function submitPublicKeyShare(
        bytes32 sessionId,
        bytes calldata publicKeyShare
    )
        external
        sessionExists(sessionId)
        onlyParticipant(sessionId)
        inPhase(sessionId, DKGPhase.PublicKeyPhase)
        phaseNotExpired(sessionId)
    {
        _submitPublicKeyShare(sessionId, publicKeyShare, false);
    }

    function _submitPublicKeyShare(bytes32 sessionId,bytes calldata publicKeyShare,bool exactRetry) private {
        DKGSession storage session = sessions[sessionId];

        if (_hasSubmittedPKShare[sessionId][msg.sender] && !exactRetry) revert AlreadySubmitted();
        if (publicKeyShare.length == 0) revert EmptyPublicKey();
        (uint256 x, uint256 y) = _decodePublicKeyShare(publicKeyShare);
        bytes memory point = abi.encodePacked(bytes1(uint8(2 + (y & 1))), bytes32(x));
        if (_hasSubmittedPKShare[sessionId][msg.sender]) {
            if (keccak256(publicKeyShares[sessionId][msg.sender]) != keccak256(point)) revert AlreadySubmitted();
            return;
        }

        _hasSubmittedPKShare[sessionId][msg.sender] = true;
        publicKeyShares[sessionId][msg.sender] = point;
        session.pkShareCount++;

        emit PublicKeyShareSubmitted(sessionId, msg.sender);
    }

    /**
     * @notice Approve an off-chain aggregated group public key for this DKG session.
     * @dev The contract validates each public point but not the off-chain DKG transcript, so completion
     *      requires threshold participant attestation for the exact group key hash.
     */
    function approveGroupPublicKey(
        bytes32 sessionId,
        bytes calldata groupPublicKey
    )
        external
        sessionExists(sessionId)
        onlyParticipant(sessionId)
        inPhase(sessionId, DKGPhase.PublicKeyPhase)
    {
        if (ceremonies[sessionId].contextHash != 0) revert LegacyOnly();
        if (!_hasSubmittedPKShare[sessionId][msg.sender]) revert DKGNotComplete();
        if (groupPublicKey.length == 0) revert EmptyPublicKey();
        bytes32 groupX = _validateGroupPublicKey(groupPublicKey);

        bytes32 groupPublicKeyHash = keccak256(abi.encodePacked(groupX));
        if (_hasApprovedGroupPublicKey[sessionId][groupPublicKeyHash][msg.sender]) {
            revert AlreadySubmitted();
        }

        _hasApprovedGroupPublicKey[sessionId][groupPublicKeyHash][msg.sender] = true;
        groupPublicKeyApprovals[sessionId][groupPublicKeyHash]++;

        emit GroupPublicKeyApproved(sessionId, msg.sender, groupPublicKeyHash);
    }

    /**
     * @notice Complete the DKG session and set group public key
     * @param sessionId The DKG session ID
     * @param groupPublicKey The aggregated group public key
     */
    function completeDKG(
        bytes32 sessionId,
        bytes calldata groupPublicKey
    )
        external
        sessionExists(sessionId)
        inPhase(sessionId, DKGPhase.PublicKeyPhase)
    {
        DKGSession storage session = sessions[sessionId];

        if (ceremonies[sessionId].contextHash != 0) revert LegacyOnly();
        if (session.isCompleted) revert AlreadyCompleted();
        if (session.pkShareCount < session.threshold) revert DKGNotComplete();
        if (groupPublicKey.length == 0) revert EmptyPublicKey();
        bytes32 groupX = _validateGroupPublicKey(groupPublicKey);
        if (groupPublicKeyApprovals[sessionId][keccak256(abi.encodePacked(groupX))] < session.threshold) {
            revert GroupPublicKeyNotApproved();
        }

        session.groupPublicKey = abi.encodePacked(groupX);
        session.isCompleted = true;
        session.currentPhase = DKGPhase.Completed;

        emit DKGCompleted(sessionId, session.groupPublicKey);
    }

    /**
     * @notice Manually advance to the next phase (if deadline passed)
     * @param sessionId The DKG session ID
     */
    function advancePhase(bytes32 sessionId) external sessionExists(sessionId) {
        DKGSession storage session = sessions[sessionId];

        if (block.timestamp <= session.phaseDeadline) revert PhaseNotExpired();

        if (ceremonies[sessionId].contextHash != 0) {
            if (uint8(session.currentPhase) >= uint8(DKGPhase.Completed)) revert InvalidPhase();
            _failSession(sessionId, "CEREMONY_TIMEOUT");
            return;
        }
        DKGPhase currentPhase = session.currentPhase;

        if (currentPhase == DKGPhase.CommitmentsPhase) {
            // Check if enough commitments to proceed
            if (session.commitmentCount >= session.threshold) {
                _advanceToPhase(sessionId, DKGPhase.SharesPhase);
            } else {
                _failSession(sessionId, "Not enough commitments");
            }
        } else if (currentPhase == DKGPhase.SharesPhase) {
            // Check if minimum shares submitted
            uint256 minShares = session.threshold * session.participants.length;
            if (session.shareCount >= minShares) {
                _advanceToPhase(sessionId, DKGPhase.PublicKeyPhase);
            } else {
                _failSession(sessionId, "Not enough share submissions");
            }
        } else if (currentPhase == DKGPhase.PublicKeyPhase) {
            if (session.pkShareCount >= session.threshold) {
                // Keep the session open for threshold group-key attestations and completion.
                session.phaseDeadline = block.timestamp + PHASE_DURATION();
                emit PhaseAdvanced(sessionId, DKGPhase.PublicKeyPhase);
            } else {
                _failSession(sessionId, "Not enough public key shares");
            }
        }
    }

    /**
     * @notice Mark a DKG session as failed
     * @param sessionId The DKG session ID
     * @param reason Reason for failure
     */
    function failDKG(
        bytes32 sessionId,
        string calldata reason
    ) external sessionExists(sessionId) {
        DKGSession storage session = sessions[sessionId];

        if (uint8(session.currentPhase) >= uint8(DKGPhase.Completed)) revert InvalidPhase();
        if (ceremonies[sessionId].contextHash != 0) revert LegacyOnly();
        // Only initiator can manually fail, or it auto-fails via advancePhase
        require(msg.sender == session.initiator, "Not initiator");
        require(session.currentPhase != DKGPhase.Completed, "Already completed");
        require(session.currentPhase != DKGPhase.Failed, "Already failed");

        _failSession(sessionId, reason);
    }

    // ======== VIEW FUNCTIONS ========

    /**
     * @notice Get DKG session status
     */
    function getDKGStatus(bytes32 sessionId)
        external
        view
        sessionExists(sessionId)
        returns (
            DKGPhase phase,
            uint256 commitmentCount,
            uint256 shareCount,
            uint256 pkShareCount,
            bool isCompleted
        )
    {
        DKGSession storage session = sessions[sessionId];
        return (
            session.currentPhase,
            session.commitmentCount,
            session.shareCount,
            session.pkShareCount,
            session.isCompleted
        );
    }

    /**
     * @notice Get session participants
     */
    function getSessionParticipants(bytes32 sessionId)
        external
        view
        sessionExists(sessionId)
        returns (address[] memory)
    {
        return sessions[sessionId].participants;
    }

    /**
     * @notice Get session info
     */
    function getSessionInfo(bytes32 sessionId)
        external
        view
        sessionExists(sessionId)
        returns (DKGSessionInfo memory)
    {
        DKGSession storage session = sessions[sessionId];
        return DKGSessionInfo({
            sessionId: session.sessionId,
            federationId: session.federationId,
            participantCount: session.participants.length,
            threshold: session.threshold,
            currentPhase: session.currentPhase,
            phaseDeadline: session.phaseDeadline,
            commitmentCount: session.commitmentCount,
            shareCount: session.shareCount,
            pkShareCount: session.pkShareCount,
            isCompleted: session.isCompleted
        });
    }

    /**
     * @notice Get the group public key for a completed session
     */
    function getGroupPublicKey(bytes32 sessionId)
        external
        view
        sessionExists(sessionId)
        returns (bytes memory)
    {
        return sessions[sessionId].groupPublicKey;
    }

    /**
     * @notice Check if a participant has submitted commitments
     */
    function hasSubmittedCommitments(bytes32 sessionId, address participant)
        external
        view
        returns (bool)
    {
        return _hasSubmittedCommitments[sessionId][participant];
    }

    /**
     * @notice Check if a participant has submitted public key share
     */
    function hasSubmittedPKShare(bytes32 sessionId, address participant)
        external
        view
        returns (bool)
    {
        return _hasSubmittedPKShare[sessionId][participant];
    }

    function hasApprovedGroupPublicKey(bytes32 sessionId, bytes32 groupPublicKeyHash, address participant)
        external
        view
        returns (bool)
    {
        return _hasApprovedGroupPublicKey[sessionId][groupPublicKeyHash][participant];
    }

    function getGroupPublicKeyApprovalCount(bytes32 sessionId, bytes32 groupPublicKeyHash)
        external
        view
        returns (uint256)
    {
        return groupPublicKeyApprovals[sessionId][groupPublicKeyHash];
    }

    /**
     * @notice Get phase duration constant
     */
    function getPhaseDuration() external view returns (uint256) {
        return PHASE_DURATION();
    }

    /**
     * @notice Get a participant's VSS commitments
     */
    function getVSSCommitments(bytes32 sessionId, address participant)
        external
        view
        returns (bytes32[] memory)
    {
        return vssCommitments[sessionId][participant];
    }

    /**
     * @notice Get a share hash between two participants
     */
    function getShareHash(bytes32 sessionId, address from, address to)
        external
        view
        returns (bytes32)
    {
        return shareHashes[sessionId][from][to];
    }

    /**
     * @notice Get a participant's public key share
     */
    function getPublicKeyShare(bytes32 sessionId, address participant)
        external
        view
        returns (bytes memory)
    {
        return publicKeyShares[sessionId][participant];
    }

    /**
     * @notice Check if an address is a participant in a session
     */
    function isSessionParticipant(bytes32 sessionId, address account)
        external
        view
        returns (bool)
    {
        return isParticipant[sessionId][account];
    }

    /**
     * @notice Get participant's index (1-indexed for Lagrange coefficients)
     */
    function getParticipantIndex(bytes32 sessionId, address participant)
        external
        view
        returns (uint256)
    {
        return participantIndex[sessionId][participant];
    }

    // ======== INTERNAL FUNCTIONS ========

    function _advanceToPhase(bytes32 sessionId, DKGPhase newPhase) internal {
        sessions[sessionId].currentPhase = newPhase;
        sessions[sessionId].phaseDeadline = block.timestamp + PHASE_DURATION();
        emit PhaseAdvanced(sessionId, newPhase);
    }

    function _failSession(bytes32 sessionId, string memory reason) internal {
        sessions[sessionId].currentPhase = DKGPhase.Failed;
        emit DKGFailed(sessionId, reason);
    }

    function _validateGroupPublicKey(bytes calldata groupPublicKey) internal view returns (bytes32) {
        uint256 pubX;
        uint256 pubY;

        if (groupPublicKey.length == 32) {
            pubX = _readUint256FromCalldata(groupPublicKey, 0);
            pubY = Secp256k1.deriveY(pubX);
        } else if (groupPublicKey.length == 33) {
            bytes1 prefix = groupPublicKey[0];
            if (prefix != 0x02 && prefix != 0x03) revert InvalidGroupPublicKey();
            pubX = _readUint256FromCalldata(groupPublicKey, 1);
            pubY = Secp256k1.calculateY(pubX, prefix == 0x03);
        } else if (groupPublicKey.length == 64) {
            pubX = _readUint256FromCalldata(groupPublicKey, 0);
            pubY = _readUint256FromCalldata(groupPublicKey, 32);
        } else {
            revert InvalidGroupPublicKey();
        }

        if (pubX >= Secp256k1.P || pubY >= Secp256k1.P || !Secp256k1.isOnCurve(pubX, pubY)) {
            revert InvalidGroupPublicKey();
        }
        if ((pubY & 1) != 0) revert InvalidGroupPublicKey();
        return bytes32(pubX);
    }

    function getPublicKeySharePoint(bytes32 sessionId, address participant)
        external
        view
        sessionExists(sessionId)
        returns (uint256 x, uint256 y)
    {
        if (!_hasSubmittedPKShare[sessionId][participant]) revert EmptyPublicKey();
        return _decodePublicKeyShare(publicKeyShares[sessionId][participant]);
    }

    function _decodePublicKeyShare(bytes memory key) private view returns (uint256 x, uint256 y) {
        if (key.length != 33 && key.length != 65) revert InvalidPublicKeyShare();
        assembly ("memory-safe") {
            x := mload(add(key, 33))
        }
        if (x >= Secp256k1.P) revert InvalidPublicKeyShare();
        if (key.length == 33) {
            if (key[0] != 0x02 && key[0] != 0x03) revert InvalidPublicKeyShare();
            y = Secp256k1.calculateY(x, key[0] == 0x03);
        } else {
            if (key[0] != 0x04) revert InvalidPublicKeyShare();
            assembly ("memory-safe") {
                y := mload(add(key, 65))
            }
            if (y >= Secp256k1.P || !Secp256k1.isOnCurve(x, y)) revert InvalidPublicKeyShare();
        }
    }

    function _readUint256FromCalldata(bytes calldata data, uint256 offset) private pure returns (uint256 value) {
        assembly {
            value := calldataload(add(data.offset, offset))
        }
    }
}
