// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./DKGAuthorization.sol";

interface IAddressMessageRegistry {
    function authorization() external view returns (address);
    function graphId() external view returns (bytes32);
    function deposits() external view returns (address);
}
interface IAddressMessageDeposits { function addressRegistry() external view returns (address); }

/// @notice Authenticated ciphertext delivery for address requests, separate from SPV jobs.
/// @dev ACK/status have no registration, Ready, mint, fee or Bitcoin signing authority.
contract BitcoinAddressMessageBoard {
    uint256 public constant MAX_PAYLOAD = 4096;
    uint64 public constant MAX_TTL = 1 days;
    uint32 public constant MAX_STATUS = 64;
    IAddressMessageRegistry public immutable registry;
    DKGAuthorization public immutable authorization;
    bytes32 public immutable domain;
    struct Route { address requester; address service; bytes32 requestId; }
    struct Packet {
        bytes32 route; uint8 kind; uint32 sequence; address sender; address recipient;
        uint64 senderEpoch; uint64 recipientEpoch; uint64 expiresAt; bytes payload;
    }
    mapping(bytes32 => Route) private routes;
    mapping(bytes32 => Packet) private packets;
    mapping(address => bytes32[]) private inboxes;
    mapping(address => mapping(uint64 => bytes32)) public transportKeys;
    mapping(address => uint64) public currentEpoch;
    mapping(address => mapping(bytes32 => bool)) private usedKeys;
    event KeyRegistered(address indexed owner, uint64 epoch, bytes32 publicKey);
    event MessagePublished(bytes32 indexed id, bytes32 indexed route, address indexed recipient, uint8 kind, uint32 sequence);

    constructor(address registry_) {
        require(registry_.code.length != 0, "registry");
        registry = IAddressMessageRegistry(registry_);
        authorization = DKGAuthorization(registry.authorization());
        require(address(authorization).code.length != 0
            && IAddressMessageDeposits(registry.deposits()).addressRegistry() == registry_, "binding");
        domain = keccak256(abi.encode(keccak256("SATOHSI_ADDRESS_MESSAGES_V1"),
            block.chainid, address(this), registry_, registry.graphId(), address(authorization)));
    }
    function serviceAllowed(address service) public view returns (bool) {
        return authorization.isParticipant(service);
    }
    function registerKey(uint64 epoch, bytes32 publicKey) external {
        bytes32 old = transportKeys[msg.sender][epoch];
        if (old != 0) { require(old == publicKey, "conflicting key"); return; }
        require(epoch > currentEpoch[msg.sender] && publicKey != 0 && !usedKeys[msg.sender][publicKey], "key");
        usedKeys[msg.sender][publicKey] = true;
        transportKeys[msg.sender][epoch] = publicKey; currentEpoch[msg.sender] = epoch;
        emit KeyRegistered(msg.sender, epoch, publicKey);
    }
    function routeKey(bytes32 requestId, address requester, address service) public pure returns (bytes32) {
        return keccak256(abi.encode(requestId,requester,service));
    }
    function messageKey(bytes32 route, uint8 kind, uint32 sequence) public pure returns (bytes32) {
        return keccak256(abi.encode(route,kind,sequence));
    }
    function request(bytes32 requestId, address service, uint64 senderEpoch, uint64 recipientEpoch,
        uint64 expiresAt, bytes calldata ciphertext) external returns (bytes32)
    {
        bytes32 id = routeKey(requestId,msg.sender,service);
        Route storage old = routes[id];
        if (old.requester == address(0)) {
            require(requestId != 0 && serviceAllowed(service), "request role");
            routes[id] = Route(msg.sender,service,requestId);
        }
        return _publish(id,0,0,msg.sender,service,senderEpoch,recipientEpoch,expiresAt,ciphertext);
    }
    function reply(bytes32 route, uint8 kind, uint32 sequence, uint64 senderEpoch,
        uint64 recipientEpoch, uint64 expiresAt, bytes calldata ciphertext) external returns (bytes32)
    {
        Route storage r = routes[route];
        require(r.requester != address(0) && r.service == msg.sender && serviceAllowed(msg.sender), "reply role");
        require(kind == 1 && sequence == 0 || kind == 2 && sequence > 0 && sequence <= MAX_STATUS, "kind/sequence");
        return _publish(route,kind,sequence,msg.sender,r.requester,senderEpoch,recipientEpoch,expiresAt,ciphertext);
    }
    function _publish(bytes32 route, uint8 kind, uint32 sequence, address sender, address recipient,
        uint64 senderEpoch, uint64 recipientEpoch, uint64 expiresAt, bytes calldata ciphertext) private returns (bytes32 id)
    {
        id = messageKey(route,kind,sequence);
        Packet storage old = packets[id];
        // Exact historical delivery is recoverable after expiry/key rotation.
        if (old.payload.length != 0) {
            require(old.senderEpoch == senderEpoch && old.recipientEpoch == recipientEpoch
                && old.expiresAt == expiresAt && keccak256(old.payload) == keccak256(ciphertext), "conflicting retry");
            return id;
        }
        require(sender != recipient && expiresAt > block.timestamp && expiresAt <= block.timestamp + MAX_TTL, "expiry/recipient");
        require(senderEpoch != 0 && recipientEpoch != 0 && senderEpoch == currentEpoch[sender]
            && recipientEpoch == currentEpoch[recipient], "key epoch");
        require(ciphertext.length > 48 && ciphertext.length <= MAX_PAYLOAD, "ciphertext size");
        packets[id] = Packet(route,kind,sequence,sender,recipient,senderEpoch,recipientEpoch,expiresAt,ciphertext);
        inboxes[recipient].push(id);
        emit MessagePublished(id,route,recipient,kind,sequence);
    }
    function route(bytes32 id) external view returns (Route memory) { return routes[id]; }
    function packet(bytes32 id) external view returns (Packet memory) { return packets[id]; }
    function messageCount(address recipient) external view returns (uint256) { return inboxes[recipient].length; }
    function messageAt(address recipient, uint256 index) external view returns (bytes32) { return inboxes[recipient][index]; }
}
