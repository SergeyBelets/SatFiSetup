// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/**
 * @title FROSTCoordinator
 * @notice Ончейн-координатор FROST-сессий с поддержкой DKG и подписи:
 *  - Совместимость с MiningPoolDAO (createSessionWithId / finalizeSession(id,sig,msgHash))
 *  - Привязка messageHash позже (bind или в finalize)
 *  - Учёт отказов участников (refuse) для слешинга в DAO
 *  - Гибкая проверка порога (enforceSharesCheck true/false)
 *  - Поддержка внешнего верификатора (override на сессию)
 *  - Метаданные purpose/origin (тип операции и источник)
 *  - События-алиасы (SessionOpened) для старых агентов
 *  - Мультичейн поддержка (Schnorr для BTC/BCH/LTC, ECDSA для DOGE)
 *  - DKG с ончейн-хранением зашифрованных secret shares
 *
 * Валидация подписи делегирована IFROSTVerifier (BIP340 для Schnorr, ECDSA для DOGE).
 * Оффчейн агенты слушают события и исполняют MPC-протокол.
 */
import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";
import "./interfaces/IFROSTCoordinator.sol";
import "./vendor/cryptography/Secp256k1.sol";
import "./vendor/cryptography/Secp256k1Arithmetic.sol";
import "./vendor/cryptography/Memory.sol";
import "./vendor/cryptography/ModExp.sol";

interface IDKGResultRegistry {
    function getCompletedResult(bytes32 id) external view returns(bytes32,bytes32,uint64,bytes32,bytes memory,address[] memory,uint256);
    function getPublicKeySharePoint(bytes32 id,address participant) external view returns(uint256,uint256);
}

interface IFROSTVerifier {
    function verify(
        bytes calldata groupPubkey,
        bytes32 messageHash,
        bytes calldata signature,
        string calldata signatureType
    ) external view returns (bool ok);
}

contract FROSTCoordinator is AccessControl, IFROSTCoordinator {
    using EnumerableSet for EnumerableSet.AddressSet;

    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
    bytes32 public constant SESSION_CREATOR_ROLE = keccak256("SESSION_CREATOR_ROLE");
    bytes32 public constant CONFIRMER_ROLE = keccak256("CONFIRMER_ROLE");

    enum SessionState {
        NONE,
        OPENED,
        FINALIZED,
        ABORTED
    }
    enum SessionPurpose {
        UNKNOWN,
        WITHDRAWAL,
        SLASH,
        REDEMPTION,
        BRIDGE_OUT,
        BRIDGE_IN,
        DKG
    }

    struct OriginMeta {
        address originContract;
        uint256 originId;
        uint16 networkId;
        bytes32 poolId;
    }

    struct CurvePoint {
        uint256 x;
        uint256 y;
    }

    struct NonceCommitmentPair {
        uint256 dX;
        uint256 dY;
        uint256 eX;
        uint256 eY;
        bool submitted;
    }

    struct PublicKeyShare {
        uint256 x;
        uint256 y;
        bool registered;
    }

    struct Session {
        uint256 id;
        address creator;
        bytes groupPubkey;
        bytes32 messageHash;
        bool messageBound;
        string signatureType;
        uint256 threshold;
        uint256 total;
        uint64 deadline;
        bool enforceSharesCheck;
        address verifierOverride;
        SessionState state;
        bytes aggregatedSig;
        EnumerableSet.AddressSet participants;
        mapping(address => bytes32) nonceCommitments;
        mapping(address => bytes) signatureShares;
        mapping(address => PublicKeyShare) publicKeyShares;
        mapping(address => NonceCommitmentPair) noncePointCommitments;
        mapping(address => uint256) verifiedSignatureShares;
        mapping(address => uint256) participantIndexes;
        mapping(address => bool) refused;
        uint256 commitsCount;
        uint256 sharesCount;
        uint256 verifiedSharesCount;
        uint256 refusalCount;
        bool nonceSetLocked;
        bool nonceSetNegated;
        bytes32 nonceTranscriptHash;
        uint256 aggregateNonceX;
        uint256 aggregateNonceY;
        SessionPurpose purpose;
        OriginMeta origin;
        mapping(address => mapping(address => bytes)) dkgShares;
        uint256 dkgSharesCount;
    }

    uint256 public nextSessionId;
    mapping(uint256 => Session) private sessions;
    mapping(bytes32 => bool) public usedNonceCommitments;
    IFROSTVerifier public immutable defaultVerifier;
    EnumerableSet.AddressSet private custodians;

    IDKGResultRegistry public dkgRegistry;
    mapping(uint256 => bytes32) public sessionDKGResult;
    mapping(uint256 => bytes32) public sessionDKGId;
    event DKGRegistryBound(address indexed registry);

    /// @notice One-time deployment binding; never chosen by a session caller.
    function bindDKGRegistry(address registry) external onlyRole(ADMIN_ROLE) {
        require(address(dkgRegistry)==address(0) && registry.code.length>0,"registry binding");
        dkgRegistry=IDKGResultRegistry(registry);
        emit DKGRegistryBound(registry);
    }

    function createSessionFromDKG(bytes32 dkgId,bytes32 messageHash,address target,uint64 deadline) external returns(uint256 id) {
        require(address(dkgRegistry)!=address(0) && target.code.length>0 && messageHash!=0,"invalid binding");
        (,bytes32 result,,bytes32 purpose,bytes memory group,address[] memory roster,uint256 threshold)=dkgRegistry.getCompletedResult(dkgId);
        require(purpose==keccak256("EVM_AUTH"),"key purpose");
        id=_createSessionInternal(0,group,roster,threshold,deadline,true,address(0),SessionPurpose.UNKNOWN,OriginMeta(target,0,0,0),"","Schnorr");
        Session storage s=sessions[id]; s.messageHash=messageHash; s.messageBound=true;
        sessionDKGResult[id]=result; sessionDKGId[id]=dkgId;
        for(uint256 i;i<roster.length;++i) {
            (uint256 x,uint256 y)=dkgRegistry.getPublicKeySharePoint(dkgId,roster[i]);
            s.publicKeyShares[roster[i]]=PublicKeyShare(x,y,true);
            emit ParticipantPublicKeyShareRegistered(id,roster[i],x,y);
        }
        emit MessageBound(id,messageHash);
    }

    event SessionCreated(
        uint256 indexed sessionId,
        address indexed creator,
        bytes groupPubkey,
        bytes message,
        string signatureType,
        uint256 threshold,
        uint256 total,
        uint64 deadline,
        bool enforceSharesCheck,
        address verifierOverride,
        SessionPurpose purpose,
        OriginMeta origin
    );
    event SessionOpened(
        uint256 indexed sessionId,
        address indexed creator,
        bytes groupPubkey,
        uint256 threshold,
        uint256 total,
        uint64 deadline
    );
    event MessageBound(uint256 indexed sessionId, bytes32 messageHash);
    event ParticipantRefused(uint256 indexed sessionId, address indexed participant, string reason);
    event DeadlineExtended(uint256 indexed sessionId, uint64 newDeadline);
    event SessionFinalized(uint256 indexed sessionId, bytes aggregatedSignature, bytes32 messageHash);
    event SessionAborted(uint256 indexed sessionId, string reason);
    event NonceCommitted(uint256 indexed sessionId, address indexed participant, bytes32 commitment);
    event NonceCommitmentPointsSubmitted(
        uint256 indexed sessionId, address indexed participant, uint256 dX, uint256 dY, uint256 eX, uint256 eY
    );
    event ParticipantPublicKeyShareRegistered(
        uint256 indexed sessionId, address indexed participant, uint256 x, uint256 y
    );
    event SignatureShareSubmitted(uint256 indexed sessionId, address indexed participant, bytes share);
    event SignatureShareVerified(uint256 indexed sessionId, address indexed participant, uint256 share);
    event DKGShareSubmitted(
        uint256 indexed sessionId, address indexed sender, address indexed recipient, bytes encryptedShare
    );
    event DKGCompleted(uint256 indexed sessionId, bytes groupPubkey);
    event CustodianAdded(address indexed custodian);
    event CustodianRemoved(address indexed custodian);

    error NotParticipant();
    error SessionNotOpen();
    error SessionExpired();
    error DuplicateCommit();
    error DuplicateShare();
    error DuplicateRefusal();
    error BadThreshold();
    error BadTotal();
    error DeadlineInPast();
    error AlreadyFinalized();
    error AlreadyAborted();
    error InvalidSignature();
    error MessageAlreadyBound();
    error MessageMismatch();
    error InvalidSignatureType();
    error InvalidGroupPubkey();
    error InvalidDKGShare();
    error InsufficientDKGShares();
    error InvalidParticipant();
    error DuplicateParticipant();
    error MessageNotBound();
    error VerifiableCommitmentRequired();
    error VerifiableShareRequired();
    error PublicKeyShareMissing();
    error NonceCommitmentMissing();
    error NonceCommitmentReuse();
    error NonceSetLocked();
    error InvalidSignatureShare();
    error InvalidNonceCommitment();
    error AggregateNonceMismatch();
    error InsufficientCommitments();
    error UnauthorizedSessionCreator();
    error ParticipantSessionMustBeEnforced();
    error ParticipantSessionVerifierOverride();
    error InvalidOriginContract();

    constructor(address _defaultVerifier) {
        require(_defaultVerifier != address(0), "verifier=0");
        defaultVerifier = IFROSTVerifier(_defaultVerifier);
        _grantRole(ADMIN_ROLE, msg.sender);
        _setRoleAdmin(SESSION_CREATOR_ROLE, ADMIN_ROLE);
        _setRoleAdmin(CONFIRMER_ROLE, ADMIN_ROLE);
    }

    // Добавленная функция для реализации интерфейса
    function getCustodians() external view override returns (address[] memory) {
        return custodians.values();
    }

    function createSession(
        uint256 sessionId,
        bytes calldata groupPubkey,
        address[] calldata participants,
        uint256 threshold,
        uint256 deadline
    ) external override onlyRole(SESSION_CREATOR_ROLE) {
        if (groupPubkey.length != 32 && groupPubkey.length != 64) revert InvalidGroupPubkey();
        if (participants.length == 0) revert BadTotal();
        if (threshold == 0 || threshold > participants.length) revert BadThreshold();
        if (deadline <= block.timestamp) revert DeadlineInPast();

        OriginMeta memory emptyOrigin =
            OriginMeta({originContract: address(0), originId: 0, networkId: 0, poolId: bytes32(0)});

        _createSessionInternal(
            sessionId,
            groupPubkey,
            participants,
            threshold,
            uint64(deadline),
            false,
            address(0),
            SessionPurpose.DKG,
            emptyOrigin,
            "",
            "Schnorr"
        );
    }

    function createSession(
        uint256 sessionId,
        bytes calldata groupPubkey,
        bytes calldata message,
        string calldata signatureType,
        uint256 deadline
    ) external override onlyRole(SESSION_CREATOR_ROLE) {
        if (
            keccak256(abi.encodePacked(signatureType)) != keccak256(abi.encodePacked("Schnorr"))
                && keccak256(abi.encodePacked(signatureType)) != keccak256(abi.encodePacked("ECDSA"))
        ) {
            revert InvalidSignatureType();
        }
        if (
            keccak256(abi.encodePacked(signatureType)) == keccak256(abi.encodePacked("Schnorr"))
                && groupPubkey.length != 32
        ) {
            revert InvalidGroupPubkey();
        }
        if (
            keccak256(abi.encodePacked(signatureType)) == keccak256(abi.encodePacked("ECDSA"))
                && groupPubkey.length != 64
        ) {
            revert InvalidGroupPubkey();
        }

        address[] memory participants = new address[](0);
        OriginMeta memory emptyOrigin =
            OriginMeta({originContract: address(0), originId: 0, networkId: 0, poolId: bytes32(0)});
        _createSessionInternal(
            sessionId,
            groupPubkey,
            participants,
            0,
            uint64(deadline),
            false,
            address(0),
            SessionPurpose.UNKNOWN,
            emptyOrigin,
            message,
            signatureType
        );
    }

    function submitNonceCommit(uint256 sessionId, bytes32 commitment) external override {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (s.enforceSharesCheck) revert VerifiableCommitmentRequired();
        if (!isParticipant(sessionId, msg.sender)) revert NotParticipant();
        if (s.nonceCommitments[msg.sender] != bytes32(0)) revert DuplicateCommit();

        s.nonceCommitments[msg.sender] = commitment;
        s.commitsCount += 1;
        if (!s.participants.contains(msg.sender)) {
            s.participants.add(msg.sender);
            s.total = s.participants.length();
        }

        emit NonceCommitted(sessionId, msg.sender, commitment);
    }

    function registerParticipantPublicKeyShare(uint256 sessionId, address participant, uint256 pubX, uint256 pubY)
        external
    {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (msg.sender != s.creator) {
            _checkRole(CONFIRMER_ROLE, msg.sender);
        }
        if (!isParticipant(sessionId, participant)) revert NotParticipant();
        if (!Secp256k1.isOnCurve(pubX, pubY) || (pubX == 0 && pubY == 0)) revert InvalidGroupPubkey();

        PublicKeyShare storage share = s.publicKeyShares[participant];
        if (share.registered) revert DuplicateShare();
        share.x = pubX;
        share.y = pubY;
        share.registered = true;

        emit ParticipantPublicKeyShareRegistered(sessionId, participant, pubX, pubY);
    }

    function submitNonceCommitments(uint256 sessionId, uint256 dX, uint256 dY, uint256 eX, uint256 eY) external {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (!s.messageBound) revert MessageNotBound();
        if (!isParticipant(sessionId, msg.sender)) revert NotParticipant();
        if (s.nonceSetLocked) revert NonceSetLocked();
        if (s.commitsCount >= s.threshold) revert NonceSetLocked();
        if (s.nonceCommitments[msg.sender] != bytes32(0)) revert DuplicateCommit();
        if (!_isValidNonIdentityPoint(dX, dY) || !_isValidNonIdentityPoint(eX, eY)) {
            revert InvalidNonceCommitment();
        }

        bytes32 nonceKey = keccak256(abi.encode("SATOHSI_FROST_NONCE_REUSE_V1", msg.sender, dX, dY, eX, eY));
        if (usedNonceCommitments[nonceKey]) revert NonceCommitmentReuse();
        usedNonceCommitments[nonceKey] = true;

        s.noncePointCommitments[msg.sender] = NonceCommitmentPair({dX: dX, dY: dY, eX: eX, eY: eY, submitted: true});
        s.nonceCommitments[msg.sender] = keccak256(abi.encode(dX, dY, eX, eY));
        s.commitsCount += 1;

        emit NonceCommitted(sessionId, msg.sender, s.nonceCommitments[msg.sender]);
        emit NonceCommitmentPointsSubmitted(sessionId, msg.sender, dX, dY, eX, eY);
    }

    function submitSignatureShare(uint256 sessionId, bytes calldata share) external override {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (s.enforceSharesCheck) revert VerifiableShareRequired();
        if (!isParticipant(sessionId, msg.sender)) revert NotParticipant();
        if (s.signatureShares[msg.sender].length != 0) revert DuplicateShare();

        s.signatureShares[msg.sender] = share;
        s.sharesCount += 1;

        emit SignatureShareSubmitted(sessionId, msg.sender, share);
    }

    function submitVerifiableSignatureShare(uint256 sessionId, uint256 share) external {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (!isParticipant(sessionId, msg.sender)) revert NotParticipant();
        if (!s.messageBound) revert MessageNotBound();
        if (!Secp256k1.isValidNonZeroScalar(share)) revert InvalidSignatureShare();
        if (s.signatureShares[msg.sender].length != 0) revert DuplicateShare();

        PublicKeyShare storage pkShare = s.publicKeyShares[msg.sender];
        if (!pkShare.registered) revert PublicKeyShareMissing();

        NonceCommitmentPair storage nonce = s.noncePointCommitments[msg.sender];
        if (!nonce.submitted) revert NonceCommitmentMissing();
        if (s.commitsCount < s.threshold) revert InsufficientCommitments();

        if (!s.nonceSetLocked) {
            _lockNonceSet(s);
        }

        if (!_verifySignatureShare(s, msg.sender, share)) revert InvalidSignatureShare();

        s.signatureShares[msg.sender] = abi.encode(share);
        s.verifiedSignatureShares[msg.sender] = share;
        s.sharesCount += 1;
        s.verifiedSharesCount += 1;

        emit SignatureShareSubmitted(sessionId, msg.sender, s.signatureShares[msg.sender]);
        emit SignatureShareVerified(sessionId, msg.sender, share);
    }

    function submitDKGShare(uint256 sessionId, address recipient, bytes calldata encryptedShare) external {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (s.purpose != SessionPurpose.DKG) revert InvalidDKGShare();
        if (!isParticipant(sessionId, msg.sender)) revert NotParticipant();
        if (!isParticipant(sessionId, recipient)) revert NotParticipant();
        if (s.dkgShares[msg.sender][recipient].length != 0) revert DuplicateShare();
        if (encryptedShare.length > 256) revert InvalidDKGShare();

        s.dkgShares[msg.sender][recipient] = encryptedShare;
        s.dkgSharesCount += 1;

        emit DKGShareSubmitted(sessionId, msg.sender, recipient, encryptedShare);
    }

    function finalizeDKG(uint256 sessionId, bytes calldata groupPubkey) external onlyRole(CONFIRMER_ROLE) {
        Session storage s = sessions[sessionId];
        _requireCanFinalize(s);
        if (s.purpose != SessionPurpose.DKG) revert InvalidDKGShare();
        if (s.dkgSharesCount < s.total * (s.total - 1)) revert InsufficientDKGShares();
        _validateGroupPubkey(groupPubkey, "Schnorr");

        s.groupPubkey = groupPubkey;
        s.state = SessionState.FINALIZED;

        emit DKGCompleted(sessionId, groupPubkey);
    }

    function rejectSignatureRequest(uint256 sessionId, string calldata reason) external override {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (!isParticipant(sessionId, msg.sender)) revert NotParticipant();
        if (s.refused[msg.sender]) revert DuplicateRefusal();

        s.refused[msg.sender] = true;
        s.refusalCount += 1;

        emit ParticipantRefused(sessionId, msg.sender, reason);
    }

    function getSession(uint256 sessionId)
        external
        view
        override
        returns (
            uint256 id,
            address creator,
            bytes memory groupPubkey,
            bytes32 messageHash,
            bool messageBound,
            uint256 threshold,
            uint256 total,
            uint64 deadline,
            bool enforceSharesCheck,
            address verifierOverride,
            uint256 state,
            uint256 commitsCount,
            uint256 sharesCount,
            uint256 refusalCount,
            uint256 purpose,
            address originContract,
            uint256 originId,
            uint16 networkId,
            bytes32 poolId,
            uint256 dkgSharesCount
        )
    {
        Session storage s = sessions[sessionId];
        id = s.id;
        creator = s.creator;
        groupPubkey = s.groupPubkey;
        messageHash = s.messageHash;
        messageBound = s.messageBound;
        threshold = s.threshold;
        total = s.total;
        deadline = s.deadline;
        enforceSharesCheck = s.enforceSharesCheck;
        verifierOverride = s.verifierOverride;
        state = uint256(s.state);
        commitsCount = s.commitsCount;
        sharesCount = s.sharesCount;
        refusalCount = s.refusalCount;
        purpose = uint256(s.purpose);
        originContract = s.origin.originContract;
        originId = s.origin.originId;
        networkId = s.origin.networkId;
        poolId = s.origin.poolId;
        dkgSharesCount = s.dkgSharesCount;
    }

    function getDKGShare(uint256 sessionId, address sender, address recipient) external view returns (bytes memory) {
        return sessions[sessionId].dkgShares[sender][recipient];
    }

    function participantsOf(uint256 sessionId) external view returns (address[] memory) {
        return sessions[sessionId].participants.values();
    }

    function isParticipant(uint256 sessionId, address who) public view returns (bool) {
        return sessions[sessionId].participants.contains(who);
    }

    function hasCommitted(uint256 sessionId, address who) external view returns (bool) {
        return sessions[sessionId].nonceCommitments[who] != bytes32(0);
    }

    function hasSubmittedShare(uint256 sessionId, address who) external view returns (bool) {
        return sessions[sessionId].signatureShares[who].length != 0;
    }

    function getParticipantPublicKeyShare(uint256 sessionId, address who)
        external
        view
        returns (uint256 x, uint256 y, bool registered)
    {
        PublicKeyShare storage share = sessions[sessionId].publicKeyShares[who];
        return (share.x, share.y, share.registered);
    }

    function getNonceCommitmentPoints(uint256 sessionId, address who)
        external
        view
        returns (uint256 dX, uint256 dY, uint256 eX, uint256 eY, bool submitted)
    {
        NonceCommitmentPair storage nonce = sessions[sessionId].noncePointCommitments[who];
        return (nonce.dX, nonce.dY, nonce.eX, nonce.eY, nonce.submitted);
    }

    function getVerifiedShareStatus(uint256 sessionId, address who)
        external
        view
        returns (bool verified, uint256 share)
    {
        share = sessions[sessionId].verifiedSignatureShares[who];
        verified = share != 0;
    }

    function getNonceSetStatus(uint256 sessionId)
        external
        view
        returns (
            bool locked,
            bytes32 transcriptHash,
            uint256 aggregateNonceX,
            uint256 aggregateNonceY,
            uint256 verifiedShares
        )
    {
        Session storage s = sessions[sessionId];
        return (s.nonceSetLocked, s.nonceTranscriptHash, s.aggregateNonceX, s.aggregateNonceY, s.verifiedSharesCount);
    }

    function hasRefused(uint256 sessionId, address who) external view returns (bool) {
        return sessions[sessionId].refused[who];
    }

    function isFinalized(uint256 sessionId) external view returns (bool) {
        return sessions[sessionId].state == SessionState.FINALIZED;
    }

    function getResult(uint256 sessionId) external view returns (bytes memory sig, bytes32 msgHash) {
        Session storage s = sessions[sessionId];
        require(s.state == SessionState.FINALIZED, "not finalized");
        return (s.aggregatedSig, s.messageHash);
    }

    function getAuthorizationResult(uint256 sessionId)
        external
        view
        returns (
            bytes memory groupPubkey,
            bytes32 messageHash,
            bool enforceSharesCheck,
            address verifierOverride,
            address originContract,
            uint256 threshold,
            uint256 verifiedShares,
            bytes memory aggregateSignature
        )
    {
        Session storage s = sessions[sessionId];
        require(s.state == SessionState.FINALIZED, "not finalized");
        return (
            s.groupPubkey,
            s.messageHash,
            s.enforceSharesCheck,
            s.verifierOverride,
            s.origin.originContract,
            s.threshold,
            s.verifiedSharesCount,
            s.aggregatedSig
        );
    }

    function createSession(
        bytes calldata groupPubkey,
        address[] calldata participants,
        uint256 threshold,
        uint64 deadline,
        bool enforceSharesCheck,
        address verifierOverride,
        SessionPurpose purpose,
        OriginMeta calldata origin
    ) external returns (uint256 sessionId) {
        if (!hasRole(SESSION_CREATOR_ROLE, msg.sender)) {
            if (!_containsParticipant(participants, msg.sender)) revert UnauthorizedSessionCreator();
            if (!enforceSharesCheck) revert ParticipantSessionMustBeEnforced();
            if (verifierOverride != address(0)) revert ParticipantSessionVerifierOverride();
            if (origin.originContract == address(0) || origin.originContract.code.length == 0) {
                revert InvalidOriginContract();
            }
        }
        sessionId = _createSessionInternal(
            0,
            groupPubkey,
            participants,
            threshold,
            deadline,
            enforceSharesCheck,
            verifierOverride,
            purpose,
            origin,
            "",
            "Schnorr"
        );
    }

    function _containsParticipant(address[] calldata participants, address account) internal pure returns (bool) {
        for (uint256 i = 0; i < participants.length; i++) {
            if (participants[i] == account) return true;
        }
        return false;
    }

    function createSessionWithId(
        uint256 explicitId,
        bytes calldata groupPubkey,
        address[] calldata participants,
        uint256 threshold,
        uint64 deadline
    ) external onlyRole(SESSION_CREATOR_ROLE) {
        OriginMeta memory emptyOrigin =
            OriginMeta({originContract: address(0), originId: 0, networkId: 0, poolId: bytes32(0)});
        _createSessionInternal(
            explicitId,
            groupPubkey,
            participants,
            threshold,
            deadline,
            false,
            address(0),
            SessionPurpose.DKG,
            emptyOrigin,
            "",
            "Schnorr"
        );
    }

    function _createSessionInternal(
        uint256 explicitId,
        bytes memory groupPubkey,
        address[] memory participants,
        uint256 threshold,
        uint64 deadline,
        bool enforceSharesCheck,
        address verifierOverride,
        SessionPurpose purpose,
        OriginMeta memory origin,
        bytes memory message,
        string memory signatureType
    ) internal returns (uint256 sessionId) {
        if (participants.length == 0 && explicitId == 0) revert BadTotal();
        if (threshold == 0 || threshold > participants.length) revert BadThreshold();
        if (deadline <= block.timestamp) revert DeadlineInPast();
        if (
            keccak256(abi.encodePacked(signatureType)) != keccak256(abi.encodePacked("Schnorr"))
                && keccak256(abi.encodePacked(signatureType)) != keccak256(abi.encodePacked("ECDSA"))
        ) {
            revert InvalidSignatureType();
        }
        if (
            keccak256(abi.encodePacked(signatureType)) == keccak256(abi.encodePacked("Schnorr"))
                && groupPubkey.length != 32
        ) {
            revert InvalidGroupPubkey();
        }
        if (
            keccak256(abi.encodePacked(signatureType)) == keccak256(abi.encodePacked("ECDSA"))
                && groupPubkey.length != 64
        ) {
            revert InvalidGroupPubkey();
        }
        _validateGroupPubkey(groupPubkey, signatureType);

        sessionId = explicitId == 0 ? ++nextSessionId : explicitId;
        Session storage s = sessions[sessionId];
        require(s.id == 0, "session exists");

        if (explicitId != 0 && explicitId > nextSessionId) {
            nextSessionId = explicitId;
        }

        s.id = sessionId;
        s.creator = msg.sender;
        s.groupPubkey = groupPubkey;
        s.messageHash = message.length > 0 ? keccak256(message) : bytes32(0);
        s.messageBound = message.length > 0;
        s.signatureType = signatureType;
        s.threshold = threshold;
        s.total = participants.length;
        s.deadline = deadline;
        s.enforceSharesCheck = enforceSharesCheck;
        s.verifierOverride = verifierOverride;
        s.state = SessionState.OPENED;
        s.purpose = purpose;
        s.origin = origin;

        for (uint256 i = 0; i < participants.length; i++) {
            if (participants[i] == address(0)) revert InvalidParticipant();
            if (!s.participants.add(participants[i])) revert DuplicateParticipant();
            s.participantIndexes[participants[i]] = i + 1;
        }

        emit SessionCreated(
            sessionId,
            msg.sender,
            groupPubkey,
            message,
            signatureType,
            threshold,
            participants.length,
            deadline,
            enforceSharesCheck,
            verifierOverride,
            purpose,
            origin
        );
        emit SessionOpened(sessionId, msg.sender, groupPubkey, threshold, participants.length, deadline);
    }

    function bindMessageHash(uint256 sessionId, bytes32 messageHash) external {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (s.creator != msg.sender) {
            _checkRole(CONFIRMER_ROLE, msg.sender);
        }
        if (s.messageBound) revert MessageAlreadyBound();

        s.messageHash = messageHash;
        s.messageBound = true;

        emit MessageBound(sessionId, messageHash);
    }

    function extendDeadline(uint256 sessionId, uint64 newDeadline) external {
        Session storage s = sessions[sessionId];
        if (msg.sender != s.creator) {
            _checkRole(ADMIN_ROLE, msg.sender);
        }
        require(newDeadline > s.deadline && newDeadline > block.timestamp, "bad new deadline");
        s.deadline = newDeadline;
        emit DeadlineExtended(sessionId, newDeadline);
    }

    function finalizeSession(uint256 sessionId, bytes calldata aggregatedSignature, bytes32 messageHash)
        external
        override
    {
        Session storage s = sessions[sessionId];
        _requireCanFinalize(s);
        if (!hasRole(CONFIRMER_ROLE, msg.sender) && !s.participants.contains(msg.sender)) {
            revert NotParticipant();
        }

        if (!s.messageBound) {
            s.messageHash = messageHash;
            s.messageBound = true;
            emit MessageBound(sessionId, messageHash);
        } else {
            if (s.messageHash != messageHash) revert MessageMismatch();
        }

        _verifyAndFinalize(s, aggregatedSignature);
    }

    function _verifyAndFinalize(Session storage s, bytes calldata aggregatedSignature) internal returns (bool) {
        if (s.enforceSharesCheck) {
            require(s.verifiedSharesCount >= s.threshold, "verified shares < threshold");
            if (_decodeSignatureRx(aggregatedSignature) != s.aggregateNonceX) revert AggregateNonceMismatch();
        }

        IFROSTVerifier v = s.verifierOverride != address(0) ? IFROSTVerifier(s.verifierOverride) : defaultVerifier;

        bool ok = v.verify(s.groupPubkey, s.messageHash, aggregatedSignature, s.signatureType);
        if (!ok) revert InvalidSignature();

        s.state = SessionState.FINALIZED;
        s.aggregatedSig = aggregatedSignature;

        emit SessionFinalized(s.id, aggregatedSignature, s.messageHash);
        return true;
    }

    function abortSession(uint256 sessionId, string calldata reason) external {
        Session storage s = sessions[sessionId];
        if (s.state == SessionState.FINALIZED) revert AlreadyFinalized();
        if (s.state == SessionState.ABORTED) revert AlreadyAborted();
        if (msg.sender != s.creator) {
            _checkRole(ADMIN_ROLE, msg.sender);
        }

        s.state = SessionState.ABORTED;
        emit SessionAborted(sessionId, reason);
    }

    function addSessionParticipant(uint256 sessionId, address participant) external onlyRole(ADMIN_ROLE) {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (!hasRole(CONFIRMER_ROLE, participant)) revert NotParticipant();
        if (!s.participants.add(participant)) revert DuplicateCommit();
        s.total = s.participants.length();
    }

    function setSessionThreshold(uint256 sessionId, uint256 threshold) external onlyRole(ADMIN_ROLE) {
        Session storage s = sessions[sessionId];
        _requireSessionOpen(s);
        if (threshold == 0 || threshold > s.total) revert BadThreshold();
        s.threshold = threshold;
    }

    function addCustodian(address custodian) external onlyRole(ADMIN_ROLE) {
        require(custodian != address(0), "custodian=0");
        if (custodians.add(custodian)) {
            _grantRole(CONFIRMER_ROLE, custodian);
            emit CustodianAdded(custodian);
        }
    }

    function removeCustodian(address custodian) external onlyRole(ADMIN_ROLE) {
        if (custodians.remove(custodian)) {
            _revokeRole(CONFIRMER_ROLE, custodian);
            emit CustodianRemoved(custodian);
        }
    }

    function _lockNonceSet(Session storage s) internal {
        bytes32 transcript = keccak256(
            abi.encode(
                "SATOHSI_FROST_NONCE_TRANSCRIPT_V1", block.chainid, address(this), s.id, s.groupPubkey, s.messageHash
            )
        );

        uint256 participantCount = s.participants.length();
        for (uint256 i = 0; i < participantCount; i++) {
            address signer = s.participants.at(i);
            NonceCommitmentPair storage nonce = s.noncePointCommitments[signer];
            if (nonce.submitted) {
                transcript = keccak256(abi.encode(transcript, signer, nonce.dX, nonce.dY, nonce.eX, nonce.eY));
            }
        }

        uint256 aggregateX;
        uint256 aggregateY;
        bool hasAggregate;
        for (uint256 i = 0; i < participantCount; i++) {
            address signer = s.participants.at(i);
            NonceCommitmentPair storage nonce = s.noncePointCommitments[signer];
            if (nonce.submitted) {
                uint256 rho = _bindingFactor(transcript, s.id, signer);
                (uint256 rX, uint256 rY) = _participantNonce(nonce, rho);
                if (!hasAggregate) {
                    aggregateX = rX;
                    aggregateY = rY;
                    hasAggregate = true;
                } else {
                    (aggregateX, aggregateY) = _addPoints(aggregateX, aggregateY, rX, rY);
                }
            }
        }

        if (!hasAggregate || !_isValidNonIdentityPoint(aggregateX, aggregateY)) {
            revert InvalidNonceCommitment();
        }
        if ((aggregateY & 1) != 0) {
            aggregateY = Secp256k1.P - aggregateY;
            s.nonceSetNegated = true;
        }

        s.nonceSetLocked = true;
        s.nonceTranscriptHash = transcript;
        s.aggregateNonceX = aggregateX;
        s.aggregateNonceY = aggregateY;
    }

    function _verifySignatureShare(Session storage s, address signer, uint256 share) internal view returns (bool) {
        PublicKeyShare storage pkShare = s.publicKeyShares[signer];
        NonceCommitmentPair storage nonce = s.noncePointCommitments[signer];

        uint256 rho = _bindingFactor(s.nonceTranscriptHash, s.id, signer);
        (uint256 rX, uint256 rY) = _participantNonce(nonce, rho);
        if (s.nonceSetNegated) {
            rY = Secp256k1.P - rY;
        }
        uint256 lambda = _lagrangeCoefficient(s, signer);
        (uint256 groupX,) = _decodeGroupPubkey(s.groupPubkey);
        uint256 challenge = _bip340Challenge(s.aggregateNonceX, groupX, s.messageHash);
        uint256 challengeScale = mulmod(challenge, lambda, Secp256k1.N);

        (uint256 leftX, uint256 leftY) = _mulBasePoint(share);
        (uint256 scaledPkX, uint256 scaledPkY) = _mulPoint(pkShare.x, pkShare.y, challengeScale);
        (uint256 rightX, uint256 rightY) = _addPoints(rX, rY, scaledPkX, scaledPkY);

        return leftX == rightX && leftY == rightY;
    }

    function _participantNonce(NonceCommitmentPair storage nonce, uint256 rho)
        internal
        view
        returns (uint256 rX, uint256 rY)
    {
        (uint256 scaledEX, uint256 scaledEY) = _mulPoint(nonce.eX, nonce.eY, rho);
        return _addPoints(nonce.dX, nonce.dY, scaledEX, scaledEY);
    }

    function _bindingFactor(bytes32 transcriptHash, uint256 sessionId, address signer)
        internal
        pure
        returns (uint256 rho)
    {
        rho = uint256(keccak256(abi.encode("SATOHSI_FROST_BINDING_FACTOR_V1", sessionId, transcriptHash, signer)))
            % Secp256k1.N;
        if (rho == 0) rho = 1;
    }

    function _lagrangeCoefficient(Session storage s, address participant) internal view returns (uint256 coefficient) {
        uint256 xi = s.participantIndexes[participant];
        if (xi == 0) revert NotParticipant();

        coefficient = 1;
        uint256 participantCount = s.participants.length();
        for (uint256 i = 0; i < participantCount; i++) {
            address other = s.participants.at(i);
            if (other == participant || !s.noncePointCommitments[other].submitted) continue;

            uint256 xj = s.participantIndexes[other];
            uint256 numerator = Secp256k1.N - (xj % Secp256k1.N);
            uint256 denominator = xi >= xj ? xi - xj : Secp256k1.N - (xj - xi);
            coefficient = mulmod(coefficient, mulmod(numerator, _invertScalar(denominator), Secp256k1.N), Secp256k1.N);
        }
    }

    function _invertScalar(uint256 scalar) internal view returns (uint256) {
        require(scalar != 0 && scalar < Secp256k1.N, "bad scalar inverse");
        uint256 memPtr = Memory.allocate(192);
        return ModExp.modexp(memPtr, scalar, Secp256k1.N - 2, Secp256k1.N);
    }

    function _bip340Challenge(uint256 rx, uint256 px, bytes32 messageHash) internal pure returns (uint256) {
        bytes32 tag = sha256("BIP0340/challenge");
        return uint256(sha256(abi.encodePacked(tag, tag, bytes32(rx), bytes32(px), messageHash))) % Secp256k1.N;
    }

    function _mulBasePoint(uint256 scalar) internal view returns (uint256 x, uint256 y) {
        return _mulPoint(Secp256k1.GX, Secp256k1.GY, scalar);
    }

    function _mulPoint(uint256 x, uint256 y, uint256 scalar) internal view returns (uint256 resultX, uint256 resultY) {
        (uint256 px, uint256 py, uint256 pz) = Secp256k1Arithmetic.mulProjectivePoint(x, y, 1, scalar);
        uint256 memPtr = Memory.allocate(192);
        return Secp256k1Arithmetic.convertProjectivePointToAffinePoint(memPtr, px, py, pz);
    }

    function _addPoints(uint256 x1, uint256 y1, uint256 x2, uint256 y2)
        internal
        view
        returns (uint256 resultX, uint256 resultY)
    {
        (uint256 p1x, uint256 p1y, uint256 p1z) = Secp256k1Arithmetic.convertAffinePointToProjectivePoint(x1, y1);
        (uint256 p2x, uint256 p2y, uint256 p2z) = Secp256k1Arithmetic.convertAffinePointToProjectivePoint(x2, y2);
        (uint256 rx, uint256 ry, uint256 rz) = Secp256k1Arithmetic.addProjectivePoint(p1x, p1y, p1z, p2x, p2y, p2z);
        uint256 memPtr = Memory.allocate(192);
        return Secp256k1Arithmetic.convertProjectivePointToAffinePoint(memPtr, rx, ry, rz);
    }

    function _decodeGroupPubkey(bytes memory groupPubkey) internal view returns (uint256 pubX, uint256 pubY) {
        if (groupPubkey.length == 32) {
            pubX = _readUint256FromMemory(groupPubkey, 0);
            pubY = Secp256k1.deriveY(pubX);
        } else if (groupPubkey.length == 33) {
            bytes1 prefix = groupPubkey[0];
            if (prefix != 0x02 && prefix != 0x03) revert InvalidGroupPubkey();
            pubX = _readUint256FromMemory(groupPubkey, 1);
            pubY = Secp256k1.calculateY(pubX, prefix == 0x03);
        } else if (groupPubkey.length == 64) {
            pubX = _readUint256FromMemory(groupPubkey, 0);
            pubY = _readUint256FromMemory(groupPubkey, 32);
        } else {
            revert InvalidGroupPubkey();
        }
        if (!Secp256k1.isOnCurve(pubX, pubY)) revert InvalidGroupPubkey();
    }

    function _decodeSignatureRx(bytes calldata signature) internal pure returns (uint256 rx) {
        if (signature.length != 64 && signature.length != 96) revert InvalidSignature();
        assembly ("memory-safe") {
            rx := calldataload(signature.offset)
        }
    }

    function _readUint256FromMemory(bytes memory data, uint256 offset) internal pure returns (uint256 value) {
        require(data.length >= offset + 32, "read out of bounds");
        assembly ("memory-safe") {
            value := mload(add(add(data, 32), offset))
        }
    }

    function _isValidNonIdentityPoint(uint256 x, uint256 y) internal pure returns (bool) {
        return x != 0 && y != 0 && Secp256k1.isOnCurve(x, y);
    }

    function _requireSessionOpen(Session storage s) internal view {
        if (s.state != SessionState.OPENED) revert SessionNotOpen();
        if (block.timestamp > s.deadline) revert SessionExpired();
    }

    function _requireCanFinalize(Session storage s) internal view {
        if (s.state == SessionState.FINALIZED) revert AlreadyFinalized();
        if (s.state == SessionState.ABORTED) revert AlreadyAborted();
        if (s.state != SessionState.OPENED) revert SessionNotOpen();
    }

    function _isParticipant(Session storage s, address who) internal view returns (bool) {
        return s.participants.contains(who);
    }

    function _validateGroupPubkey(bytes memory groupPubkey, string memory signatureType) internal view {
        uint256 pubX;
        uint256 pubY;

        if (groupPubkey.length == 32) {
            pubX = _readUint256FromMemory(groupPubkey, 0);
            pubY = Secp256k1.deriveY(pubX);
        } else if (groupPubkey.length == 33) {
            bytes1 prefix = groupPubkey[0];
            if (prefix != 0x02 && prefix != 0x03) revert InvalidGroupPubkey();
            pubX = _readUint256FromMemory(groupPubkey, 1);
            pubY = Secp256k1.calculateY(pubX, prefix == 0x03);
        } else if (groupPubkey.length == 64) {
            pubX = _readUint256FromMemory(groupPubkey, 0);
            pubY = _readUint256FromMemory(groupPubkey, 32);
        } else {
            revert InvalidGroupPubkey();
        }

        if (!Secp256k1.isOnCurve(pubX, pubY)) revert InvalidGroupPubkey();
        if (keccak256(abi.encodePacked(signatureType)) == keccak256(abi.encodePacked("Schnorr")) && (pubY & 1) != 0) {
            revert InvalidGroupPubkey();
        }
    }

    function _readUint256FromCalldata(bytes calldata data, uint256 offset) private pure returns (uint256 value) {
        assembly ("memory-safe") {
            value := calldataload(add(data.offset, offset))
        }
    }

    function getSessionParticipants(uint256 sessionId) external view override returns (address[] memory) {
        Session storage s = sessions[sessionId];
        return s.participants.values();
    }
}
