// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./interfaces/IConfederationFactory.sol";
import "./interfaces/IFederationFactory.sol";
import "./DKGAuthorization.sol";
import "./ZkSPVServiceStateView.sol";

interface ISPVTransportFactory { function federationFactory() external view returns (address); }

/// @notice Authenticated ciphertext delivery, with no monetary or relay authority.
/// @dev Plaintext schema and HPKE contexts are checked by clients. Full ciphertext
/// remains on chain. Revocation prevents new sends; historical bytes remain readable.
contract SPVMessageBoard {
    uint256 public constant MAX_PAYLOAD = 16384;
    uint256 public constant MAX_TTL = 1 days;
    uint32 public constant MAX_STATUS = 64;
    IConfederationFactory public immutable factory;
    IFederationFactory public immutable federations;
    DKGAuthorization public immutable authorization;
    ZkSPVServiceStateView public immutable stateView;
    string public confederationId;
    bytes32 public immutable domain;
    struct Route {address requester; address service; string federationId; bytes32 requestId;}
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
    mapping(bytes32 => mapping(address => address)) public requestDelegates;
    mapping(address => address) public serviceDelegates;
    event KeyRegistered(address indexed owner, uint64 indexed epoch, bytes32 publicKey);
    event DelegateSet(bytes32 indexed federation, address indexed principal, address indexed delegate, bool service, bool allowed);
    event MessagePublished(bytes32 indexed id, bytes32 indexed route, address indexed recipient, uint8 kind, uint32 sequence);

    constructor(address factory_, string memory confederationId_, address view_) {
        require(factory_.code.length != 0 && view_.code.length != 0, "binding");
        factory = IConfederationFactory(factory_);
        federations = IFederationFactory(ISPVTransportFactory(factory_).federationFactory());
        confederationId = confederationId_;
        IConfederationFactory.ConfederationConfig memory conf = factory.getConfederation(confederationId_);
        require(conf.isActive && conf.frostMultisig.code.length != 0, "confederation");
        authorization = DKGAuthorization(conf.frostMultisig);
        stateView = ZkSPVServiceStateView(view_);
        require(IZkSPVServiceState(stateView.state()).confederationId() == keccak256(bytes(confederationId_)), "state scope");
        domain = keccak256(abi.encode(keccak256("SATOSHIFI_SPV_MESSAGES_V1"), block.chainid,
            address(this), factory_, confederationId_, view_, stateView.state(), stateView.adapter(), stateView.policyHash()));
    }
    function registerKey(uint64 epoch, bytes32 publicKey) external {
        bytes32 prior = transportKeys[msg.sender][epoch];
        if (prior != 0) {require(prior == publicKey, "conflicting key"); return;}
        require(epoch > currentEpoch[msg.sender] && publicKey != 0 && !usedKeys[msg.sender][publicKey], "key");
        usedKeys[msg.sender][publicKey] = true; transportKeys[msg.sender][epoch] = publicKey; currentEpoch[msg.sender] = epoch;
        emit KeyRegistered(msg.sender, epoch, publicKey);
    }
    function _active() private view returns (IConfederationFactory.ConfederationConfig memory conf) {
        conf = factory.getConfederation(confederationId);
        require(conf.isActive && conf.frostMultisig == address(authorization), "inactive");
    }
    function _member(string memory federation, address person) private view returns (bool) {
        IConfederationFactory.ConfederationConfig memory conf = _active();
        bool included;
        for(uint256 i; i < conf.federationIds.length; ++i)
            if(keccak256(bytes(conf.federationIds[i])) == keccak256(bytes(federation))) included = true;
        if (!included) return false;
        IFederationFactory.FederationConfig memory fed = federations.getFederation(federation);
        if (!fed.isActive) return false;
        for(uint256 i; i < fed.members.length; ++i) if(fed.members[i] == person) return true;
        return false;
    }
    function _service(address person) private view returns (bool) {
        _active(); address[] memory roster = authorization.getParticipants();
        for(uint256 i; i < roster.length; ++i) if(roster[i] == person) return true;
        return false;
    }
    function requesterAllowed(string memory federation, address person) public view returns (bool) {
        address principal = requestDelegates[keccak256(bytes(federation))][person];
        return _member(federation, person) || principal != address(0) && _member(federation, principal);
    }
    function serviceAllowed(address person) public view returns (bool) {
        address principal = serviceDelegates[person];
        return _service(person) || principal != address(0) && _service(principal);
    }
    function setRequestDelegate(string calldata federation, address delegate, bool allowed) external {
        require(delegate != address(0) && _member(federation, msg.sender), "member");
        bytes32 f = keccak256(bytes(federation)); address prior = requestDelegates[f][delegate];
        require(prior == address(0) || prior == msg.sender, "principal");
        requestDelegates[f][delegate] = allowed ? msg.sender : address(0);
        emit DelegateSet(f,msg.sender,delegate,false,allowed);
    }
    function setServiceDelegate(address delegate, bool allowed) external {
        require(delegate != address(0) && _service(msg.sender), "member");
        address prior = serviceDelegates[delegate]; require(prior == address(0) || prior == msg.sender, "principal");
        serviceDelegates[delegate] = allowed ? msg.sender : address(0);
        emit DelegateSet(0,msg.sender,delegate,true,allowed);
    }
    function routeKey(bytes32 requestId, address requester, address service) public pure returns (bytes32) {
        return keccak256(abi.encode(requestId,requester,service));
    }
    function messageKey(bytes32 route, uint8 kind, uint32 sequence) public pure returns (bytes32) {
        return keccak256(abi.encode(route,kind,sequence));
    }
    function request(bytes32 requestId, string calldata federation, address recipient, uint64 senderEpoch,
        uint64 recipientEpoch, uint64 expiresAt, bytes calldata ciphertext) external returns (bytes32 id)
    {
        bytes32 r = routeKey(requestId,msg.sender,recipient);
        Route storage old = routes[r];
        if (old.requester == address(0)) {
            require(requestId != 0 && bytes(federation).length <= 128 && requesterAllowed(federation,msg.sender)
                && serviceAllowed(recipient), "request role");
            routes[r] = Route(msg.sender,recipient,federation,requestId);
        } else require(keccak256(bytes(old.federationId)) == keccak256(bytes(federation)), "conflicting route");
        return _publish(r,0,0,msg.sender,recipient,senderEpoch,recipientEpoch,expiresAt,ciphertext);
    }
    function reply(bytes32 route, uint8 kind, uint32 sequence, uint64 senderEpoch, uint64 recipientEpoch,
        uint64 expiresAt, bytes calldata ciphertext) external returns (bytes32)
    {
        Route storage r = routes[route];
        require(r.service == msg.sender && serviceAllowed(msg.sender) && requesterAllowed(r.federationId,r.requester), "reply role");
        require(kind == 1 && sequence == 0 || kind == 2 && sequence > 0 && sequence <= MAX_STATUS, "kind/sequence");
        return _publish(route,kind,sequence,msg.sender,r.requester,senderEpoch,recipientEpoch,expiresAt,ciphertext);
    }
    function _publish(bytes32 route, uint8 kind, uint32 sequence, address sender, address recipient,
        uint64 senderEpoch, uint64 recipientEpoch, uint64 expiresAt, bytes calldata ciphertext) private returns(bytes32 id)
    {
        id = messageKey(route,kind,sequence);
        Packet storage old = packets[id];
        if(old.payload.length != 0) {
            require(old.senderEpoch == senderEpoch && old.recipientEpoch == recipientEpoch && old.expiresAt == expiresAt
                && keccak256(old.payload) == keccak256(ciphertext), "conflicting retry"); return id;
        }
        require(expiresAt > block.timestamp && expiresAt <= block.timestamp + MAX_TTL, "expiry");
        require(sender != recipient && senderEpoch == currentEpoch[sender] && recipientEpoch == currentEpoch[recipient]
            && senderEpoch != 0 && recipientEpoch != 0, "key epoch");
        require(ciphertext.length > 48 && ciphertext.length <= MAX_PAYLOAD, "ciphertext size");
        packets[id] = Packet(route,kind,sequence,sender,recipient,senderEpoch,recipientEpoch,expiresAt,ciphertext);
        inboxes[recipient].push(id);
        emit MessagePublished(id,route,recipient,kind,sequence);
    }
    function route(bytes32 id) external view returns (Route memory) {return routes[id];}
    function packet(bytes32 id) external view returns (Packet memory) {return packets[id];}
    function messageCount(address recipient) external view returns (uint256) {return inboxes[recipient].length;}
    function messageAt(address recipient,uint256 index) external view returns(bytes32) {return inboxes[recipient][index];}
}
