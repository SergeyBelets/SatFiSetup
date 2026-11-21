// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

interface IFROSTAuthorizationVerifier {
    function verify(
        bytes calldata groupPubkey,
        bytes32 messageHash,
        bytes calldata signature,
        string calldata signatureType
    ) external view returns (bool ok);
}

interface IFROSTAuthorizationCoordinator {
    function defaultVerifier() external view returns (address);

    function getSessionParticipants(uint256 sessionId) external view returns (address[] memory);

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
        );
}

/**
 * @title FROSTAuthorization
 * @notice Federation authorization boundary backed exclusively by finalized on-chain FROST sessions.
 * @dev There is no address-counter or bootstrap mode. The coordinator, canonical verifier,
 *      group key, ordered participant set and threshold are fixed at deployment.
 */
contract FROSTAuthorization {
    IFROSTAuthorizationCoordinator public immutable coordinator;
    IFROSTAuthorizationVerifier public immutable verifier;

    bytes public groupPubkey;
    bytes32 public immutable groupPubkeyHash;
    bytes32 public immutable participantsHash;
    uint256 public immutable threshold;

    address[] private participants;
    mapping(address => bool) private participantSet;
    mapping(uint256 => bool) public consumedSessions;
    mapping(bytes32 => address) public operationTargets;
    mapping(bytes32 => bool) public approvedOperations;
    mapping(bytes32 => bool) public executedOperations;

    event OperationAuthorized(
        uint256 indexed sessionId, bytes32 indexed operationHash, address indexed target, address relayer
    );
    event OperationExecuted(bytes32 indexed operationHash, address indexed target);

    error InvalidCoordinator();
    error InvalidVerifier();
    error InvalidGroupPubkey();
    error InvalidParticipant();
    error DuplicateParticipant();
    error InvalidThreshold();
    error SessionAlreadyConsumed();
    error SessionNotEnforced();
    error NonCanonicalVerifier();
    error GroupPubkeyMismatch();
    error ParticipantSetMismatch();
    error ThresholdMismatch();
    error InsufficientVerifiedShares();
    error OperationHashMismatch();
    error InvalidTarget();
    error SessionTargetMismatch();
    error OperationTargetMismatch(address expected, address actual);
    error OperationNotApproved();
    error OperationAlreadyExecuted();
    error InvalidAggregateSignature();

    constructor(address coordinator_, bytes memory groupPubkey_, address[] memory participants_, uint256 threshold_) {
        if (coordinator_ == address(0) || coordinator_.code.length == 0) revert InvalidCoordinator();
        if (groupPubkey_.length != 32) revert InvalidGroupPubkey();
        if (participants_.length == 0 || threshold_ == 0 || threshold_ > participants_.length) {
            revert InvalidThreshold();
        }

        coordinator = IFROSTAuthorizationCoordinator(coordinator_);
        address verifier_ = IFROSTAuthorizationCoordinator(coordinator_).defaultVerifier();
        if (verifier_ == address(0) || verifier_.code.length == 0) revert InvalidVerifier();
        verifier = IFROSTAuthorizationVerifier(verifier_);

        for (uint256 i = 0; i < participants_.length; i++) {
            address participant = participants_[i];
            if (participant == address(0)) revert InvalidParticipant();
            if (participantSet[participant]) revert DuplicateParticipant();
            participantSet[participant] = true;
            participants.push(participant);
        }

        groupPubkey = groupPubkey_;
        groupPubkeyHash = keccak256(groupPubkey_);
        participantsHash = keccak256(abi.encode(participants_));
        threshold = threshold_;
    }

    function authorizeOperation(uint256 sessionId, bytes32 operationHash, address target) external {
        _checkDKG(sessionId);
        if (target == address(0)) revert InvalidTarget();
        if (consumedSessions[sessionId]) revert SessionAlreadyConsumed();
        if (executedOperations[operationHash]) revert OperationAlreadyExecuted();

        (
            bytes memory sessionGroupPubkey,
            bytes32 sessionMessageHash,
            bool enforceSharesCheck,
            address verifierOverride,
            address originContract,
            uint256 sessionThreshold,
            uint256 verifiedShares,
            bytes memory aggregateSignature
        ) = coordinator.getAuthorizationResult(sessionId);

        if (!enforceSharesCheck) revert SessionNotEnforced();
        if (verifierOverride != address(0)) revert NonCanonicalVerifier();
        if (keccak256(sessionGroupPubkey) != groupPubkeyHash) revert GroupPubkeyMismatch();
        if (sessionThreshold != threshold) revert ThresholdMismatch();
        if (verifiedShares < threshold) revert InsufficientVerifiedShares();
        if (sessionMessageHash != operationHash) revert OperationHashMismatch();
        if (originContract != target) revert SessionTargetMismatch();
        if (keccak256(abi.encode(coordinator.getSessionParticipants(sessionId))) != participantsHash) {
            revert ParticipantSetMismatch();
        }

        address boundTarget = operationTargets[operationHash];
        if (boundTarget != address(0) && boundTarget != target) {
            revert OperationTargetMismatch(boundTarget, target);
        }
        if (!verifier.verify(sessionGroupPubkey, operationHash, aggregateSignature, "Schnorr")) {
            revert InvalidAggregateSignature();
        }

        consumedSessions[sessionId] = true;
        operationTargets[operationHash] = target;
        approvedOperations[operationHash] = true;

        emit OperationAuthorized(sessionId, operationHash, target, msg.sender);
    }

    function _checkDKG(uint256) internal view virtual {}

    function consumeOperation(bytes32 operationHash) external returns (bool) {
        if (executedOperations[operationHash]) revert OperationAlreadyExecuted();
        if (!approvedOperations[operationHash]) revert OperationNotApproved();

        address target = operationTargets[operationHash];
        if (target != msg.sender) revert OperationTargetMismatch(target, msg.sender);

        executedOperations[operationHash] = true;
        emit OperationExecuted(operationHash, msg.sender);
        return true;
    }

    function isOperationApproved(bytes32 operationHash, address target) external view returns (bool) {
        return approvedOperations[operationHash] && !executedOperations[operationHash]
            && operationTargets[operationHash] == target;
    }

    function hashOperation(address target, bytes4 selector, bytes32 paramsHash, uint256 nonce)
        external
        view
        returns (bytes32)
    {
        if (target == address(0)) revert InvalidTarget();
        return keccak256(
            abi.encode(
                "SATOHSI_FROST_AUTHORIZATION_V1", block.chainid, address(this), target, selector, paramsHash, nonce
            )
        );
    }

    function getParticipants() external view returns (address[] memory) {
        return participants;
    }

    function isParticipant(address account) external view returns (bool) {
        return participantSet[account];
    }
}
