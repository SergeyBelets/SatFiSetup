// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
import "./PublicByteParts.sol";

interface IDKGBoardRegistry {
    function isSessionParticipant(bytes32 id, address participant) external view returns (bool);
    function getParticipantIndex(bytes32 id, address participant) external view returns (uint256);
    function getCompletedResult(bytes32 id) external view returns
        (bytes32,bytes32,uint256,bytes32,bytes memory,address[] memory,uint256);
}

interface IIntentSessionCoordinator {
    function dkgRegistry() external view returns (address);
    // Wire-compatible prefix of FROSTCoordinator's wider, flat return tuple.
    function getSession(uint256 id) external view returns (uint256 sessionId, address creator);
}

/// @notice Public onboarding and immutable DKG packets. Round two payloads MUST be HPKE ciphertext.
/// @dev No signing authority. Agents validate canonical registry hashes and EVM/HPKE authentication.
contract DKGOnchainBoard {
    uint256 public constant MAX_BATCH_PACKETS = 16;
    uint256 public constant MAX_BATCH_BYTES = 16384;
    address public operator;
    address public registry;
    uint256 public registryBlock;
    bytes public invitation;
    mapping(uint256 => bytes) private legacyIntents;
    struct FeeParts { uint256 total;uint256 size;bytes[] chunks;bool committed; }
    mapping(uint256=>FeeParts) private feeIntents;
    uint256 public latestSession;
    uint256[] public intentSessions;
    event IntentPublished(uint256 indexed session, bytes32 intentHash);
    bytes32 public invitationHash;
    bytes32 public custodyPublisherEpoch;
    address public intentCoordinator;
    event CustodyPublishersEnabled(bytes32 indexed epoch);
    mapping(address => bool) public invited;
    mapping(address => bytes) private enrollments;
    mapping(bytes32 => bytes) private packets;
    event InvitationPublished(address indexed operator, bytes32 indexed invitationHash);
    event Enrolled(address indexed participant, bytes32 enrollmentHash);
    event RegistryBound(address indexed registry);
    event PacketPublished(bytes32 indexed sessionId, address indexed sender, address indexed recipient, uint8 kind, bytes32 payloadHash);
    error NotAuthorized();
    error InvalidPacket();
    error ConflictingRetry();

    constructor(bytes memory publicInvitation, address[] memory participants) {
        if (publicInvitation.length == 0 || publicInvitation.length > 8192 || participants.length < 2 || participants.length > 16) revert InvalidPacket();
        operator = msg.sender;
        invitation = publicInvitation;
        invitationHash = keccak256(publicInvitation);
        for (uint256 i; i < participants.length; ++i) {
            if (participants[i] == address(0) || invited[participants[i]]) revert InvalidPacket();
            invited[participants[i]] = true;
        }
        if (!invited[msg.sender]) revert NotAuthorized();
        emit InvitationPublished(msg.sender, invitationHash);
    }

    function enroll(bytes calldata publicEnrollment) external {
        if (!invited[msg.sender]) revert NotAuthorized();
        if (publicEnrollment.length == 0 || publicEnrollment.length > 2048) revert InvalidPacket();
        bytes storage previous = enrollments[msg.sender];
        if (previous.length != 0) {
            if (keccak256(previous) != keccak256(publicEnrollment)) revert ConflictingRetry();
            return;
        }
        enrollments[msg.sender] = publicEnrollment;
        emit Enrolled(msg.sender, keccak256(publicEnrollment));
    }

    function enrollment(address participant) external view returns (bytes memory) { return enrollments[participant]; }

    /// @notice One-time transport delegation after a custodian-only EVM DKG.
    /// This grants publication of PUBLIC intents, never monetary approval.
    function enableCustodianPublishers(bytes32 epoch) external {
        if (msg.sender != operator || registry == address(0) || intentCoordinator == address(0)
                || custodyPublisherEpoch != 0 || epoch == 0)
            revert NotAuthorized();
        (,,,bytes32 purpose,,address[] memory roster,) = IDKGBoardRegistry(registry).getCompletedResult(epoch);
        if (purpose != keccak256("EVM_AUTH") || roster.length < 2) revert InvalidPacket();
        for (uint256 i; i < roster.length; ++i)
            if (!invited[roster[i]] || roster[i] == operator || enrollments[roster[i]].length == 0) revert InvalidPacket();
        custodyPublisherEpoch = epoch;
        emit CustodyPublishersEnabled(epoch);
    }

    function bindIntentCoordinator(address coordinator) external {
        if (msg.sender != operator || registry == address(0)) revert NotAuthorized();
        if (intentCoordinator != address(0)) {
            if (intentCoordinator != coordinator) revert ConflictingRetry();
            return;
        }
        if (coordinator.code.length == 0 || IIntentSessionCoordinator(coordinator).dkgRegistry() != registry)
            revert InvalidPacket();
        intentCoordinator = coordinator;
    }

    function canPublishIntent(address sender) public view returns (bool) {
        return sender == operator || custodyPublisherEpoch != 0
            && IDKGBoardRegistry(registry).isSessionParticipant(custodyPublisherEpoch, sender);
    }

    function _requireIntentPublisher(uint256 session) private view {
        if (!canPublishIntent(msg.sender)) revert NotAuthorized();
        // No pre-binding writes: otherwise an operator could reserve future ids
        // before this ownership check is enabled.
        if (intentCoordinator == address(0)) revert NotAuthorized();
        (uint256 id, address creator) = IIntentSessionCoordinator(intentCoordinator).getSession(session);
        // Even the original operator cannot preempt another member's intent,
        // poison its partial chunks, or reserve an as-yet uncreated session.
        if (session == 0 || id != session || creator != msg.sender) revert NotAuthorized();
    }

    function bindRegistry(address canonicalRegistry) external {
        if (msg.sender != operator) revert NotAuthorized();
        if (canonicalRegistry.code.length == 0) revert InvalidPacket();
        if (registry != address(0)) {
            if (registry != canonicalRegistry) revert ConflictingRetry();
            return;
        }
        registry = canonicalRegistry;
        registryBlock = block.number;
        emit RegistryBound(canonicalRegistry);
    }

    /// @notice Public typed operation for independent local review; never a signing instruction.
    function publishIntent(uint256 session, bytes calldata publicIntent) external {
        _publishIntent(session,publicIntent,32768);
    }
    /// @notice Explicit larger PUBLIC financial-payload transport. No authority
    /// or secret transport semantics are granted by this publication selector.
    function publishFeeIntent(uint256 session,bytes calldata publicIntent) external {
        _publishIntent(session,publicIntent,131072);
    }
    function feeIntentPartial(uint256 session) public view returns(bytes memory raw) {
        FeeParts storage p=feeIntents[session];return PublicByteParts.join(p.chunks,p.size);
    }
    function intents(uint256 session) public view returns(bytes memory) {
        return feeIntents[session].committed ? feeIntentPartial(session) : legacyIntents[session];
    }
    function appendFeeIntent(uint256 session,uint256 total,uint256 index,bytes calldata chunk) external {
        _requireIntentPublisher(session);FeeParts storage p=feeIntents[session];
        if(index < p.chunks.length) { if(p.total != total || keccak256(p.chunks[index]) != keccak256(chunk))revert ConflictingRetry();return; }
        if(registry == address(0) || session == 0 || p.committed || legacyIntents[session].length != 0 || total == 0 || total > 131072
            || index != p.chunks.length || index >= 16 || chunk.length == 0 || chunk.length > 8192 || p.size+chunk.length > total)revert InvalidPacket();
        if(p.total == 0)p.total=total;else if(p.total != total)revert ConflictingRetry();p.chunks.push(chunk);p.size+=chunk.length;
    }
    function commitFeeIntent(uint256 session) external {
        _requireIntentPublisher(session);FeeParts storage p=feeIntents[session];
        if(p.committed)return;
        if(p.total == 0 || p.size != p.total)revert InvalidPacket();bytes memory raw=feeIntentPartial(session);
        if(legacyIntents[session].length != 0) { if(keccak256(raw) != keccak256(legacyIntents[session]))revert ConflictingRetry();return; }
        p.committed=true;intentSessions.push(session);latestSession=session;emit IntentPublished(session,keccak256(raw));
    }
    function _publishIntent(uint256 session,bytes calldata publicIntent,uint256 maximum) private {
        _requireIntentPublisher(session);
        if (registry == address(0) || session == 0 || publicIntent.length == 0 || publicIntent.length > maximum) revert InvalidPacket();
        bytes memory previous=intents(session);
        if (previous.length != 0) {
            if (keccak256(previous) != keccak256(publicIntent)) revert ConflictingRetry();
            return;
        }
        legacyIntents[session] = publicIntent;
        intentSessions.push(session);
        latestSession = session;
        emit IntentPublished(session, keccak256(publicIntent));
    }

    /// @notice Enumerate immutable intents without an archival eth_getLogs scan.
    function intentCount() external view returns (uint256) { return intentSessions.length; }

    function packetKey(bytes32 id, uint8 kind, address sender, address recipient) public pure returns (bytes32) {
        return keccak256(abi.encode(id, kind, sender, recipient));
    }

    function publish(bytes32 id, uint8 kind, address recipient, bytes calldata payload) external {
        if (registry == address(0) || !IDKGBoardRegistry(registry).isSessionParticipant(id, msg.sender)) revert NotAuthorized();
        _publish(id, kind, recipient, payload);
    }

    /// @notice Bounded packets for one session and the caller's own identity.
    /// @dev This never forwards a registry submission: msg.sender must remain the participant there.
    function publishBatch(bytes32 id, uint8[] calldata kinds, address[] calldata recipients, bytes[] calldata payloads) external {
        uint256 n = kinds.length;
        if (n == 0 || n > MAX_BATCH_PACKETS || recipients.length != n || payloads.length != n) revert InvalidPacket();
        if (registry == address(0) || !IDKGBoardRegistry(registry).isSessionParticipant(id, msg.sender)) revert NotAuthorized();
        uint256 size;
        for (uint256 i; i < n; ++i) {
            size += payloads[i].length;
            if (size > MAX_BATCH_BYTES) revert InvalidPacket();
            for (uint256 j; j < i; ++j) {
                if (kinds[i] == kinds[j] && recipients[i] == recipients[j]) revert InvalidPacket();
            }
        }
        for (uint256 i; i < n; ++i) _publish(id, kinds[i], recipients[i], payloads[i]);
    }

    function _publish(bytes32 id, uint8 kind, address recipient, bytes calldata payload) private {
        if (kind > 2 || payload.length == 0 || payload.length > 16384) revert InvalidPacket();
        if (kind == 2) {
            if (recipient == msg.sender || !IDKGBoardRegistry(registry).isSessionParticipant(id, recipient)) revert InvalidPacket();
        } else if (recipient != address(0)) revert InvalidPacket();
        bytes32 key = packetKey(id, kind, msg.sender, recipient);
        bytes storage previous = packets[key];
        if (previous.length != 0) {
            if (keccak256(previous) != keccak256(payload)) revert ConflictingRetry();
            return;
        }
        packets[key] = payload;
        emit PacketPublished(id, msg.sender, recipient, kind, keccak256(payload));
    }

    function packet(bytes32 id, uint8 kind, address sender, address recipient) external view returns (bytes memory) {
        return packets[packetKey(id, kind, sender, recipient)];
    }
}
