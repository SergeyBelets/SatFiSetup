// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./DKGAuthorization.sol";
import "./ConfederationGasTreasury.sol";
import "./interfaces/IConfederationFactory.sol";

/// @dev A qualified state implementation must increment version on every state
/// transition (including a return to a previous root), and bind the full state
/// digest. Freshness is intentionally not a prerequisite for funding an update.
interface ISPVServiceStateView {
    function state() external view returns (address);
    function adapter() external view returns (address);
    function policyHash() external view returns (bytes32);
    function snapshot() external view returns (bytes32 stateHash, uint256 version, bool initialized, bool halted);
}

/// @notice One immutable payment ledger across treasury epochs for a confederation.
/// @dev Registered admission authority supplies canonical job IDs/semantic keys.
/// This contract cannot infer semantic equivalence of different opaque keys or
/// prove a job is needed/completed. Payment is nonrefundable operating funding.
/// No qualified production ISPVServiceStateView ships with this component yet.
contract SPVServicePaymentRegistry is ReentrancyGuard {
    uint256 public constant MAX_PENDING = 256;
    bytes32 public constant PROTOCOL = keccak256("SATOSHIFI_SPV_SERVICE_PAYMENT_V1");
    IConfederationFactory public immutable factory;
    string public confederationId;
    bytes32 public immutable confederationScope;
    DKGAuthorization public immutable authorization;
    ISPVServiceStateView public immutable stateView;
    address public immutable state;
    address public immutable adapter;
    bytes32 public immutable policyHash;
    bytes32 public immutable stateViewCodeHash;
    bytes32 public immutable stateCodeHash;
    bytes32 public immutable adapterCodeHash;
    bytes32 public immutable domain;

    struct Configuration {
        address treasury;
        address issuer;
        uint64 tariffVersion;
        bytes32 tariffHash;
        uint256 amount;
        bytes32 handoverCommitment;
    }
    struct JobTerms {
        bytes32 jobId;
        bytes32 semanticKey;
        bytes32 federationIdHash;
        address payer;
        bytes32 budgetScope;
        uint64 serviceDeadline;
    }
    struct Permission {
        uint256 nonce;
        uint256 generation;
        bytes32 stateHash;
        uint256 stateVersion;
        uint64 acceptBy;
        bool active;
    }
    struct Payment {
        address treasury;
        uint64 treasuryEpoch;
        uint64 tariffVersion;
        uint256 amount;
        uint256 permissionNonce;
    }

    Configuration public configuration;
    uint256 public configurationNonce;
    uint256 public generation;
    uint256 public permissionNonce;
    uint256 public pendingCount;
    uint64 public treasuryEpoch;
    bool public enabled;
    mapping(address => bool) public isTreasury;
    mapping(uint64 => bytes32) public tariffCommitments;
    mapping(bytes32 => bytes32) public semanticJob;
    mapping(bytes32 => JobTerms) public jobs;
    mapping(bytes32 => Permission) public permissions;
    mapping(bytes32 => Payment) public payments;

    event TreasuryCreated(address indexed treasury, uint64 indexed epoch, bytes32 rosterHash);
    event ConfigurationActivated(uint256 indexed generation, address indexed treasury, address indexed issuer,
        uint64 tariffVersion, bytes32 tariffHash, uint256 amount, bytes32 handoverCommitment);
    event AdmissionsPaused(uint256 indexed generation);
    event JobAdmitted(bytes32 indexed jobId, bytes32 indexed semanticKey, uint256 indexed nonce,
        uint256 generation, bytes32 stateHash, uint256 stateVersion, uint64 acceptBy);
    event PermissionCancelled(bytes32 indexed jobId, uint256 indexed nonce);
    event JobFunded(bytes32 indexed jobId, address indexed payer, address indexed treasury,
        uint64 epoch, uint64 tariffVersion, uint256 nonce, uint256 amount);

    constructor(address factory_, string memory confederationId_, address view_) {
        require(factory_.code.length != 0 && view_.code.length != 0, "binding");
        factory = IConfederationFactory(factory_);
        confederationId = confederationId_;
        IConfederationFactory.ConfederationConfig memory conf = factory.getConfederation(confederationId_);
        require(conf.isActive && conf.frostMultisig.code.length != 0, "confederation");
        authorization = DKGAuthorization(conf.frostMultisig);
        require(authorization.dkgId() != 0 && authorization.resultHash() != 0, "DKG epoch");
        stateView = ISPVServiceStateView(view_);
        state = stateView.state();
        adapter = stateView.adapter();
        policyHash = stateView.policyHash();
        require(state.code.length != 0 && adapter.code.length != 0 && policyHash != 0, "state binding");
        stateViewCodeHash = view_.codehash;
        stateCodeHash = state.codehash;
        adapterCodeHash = adapter.codehash;
        confederationScope = keccak256(abi.encode(block.chainid, factory_, confederationId_));
        domain = keccak256(abi.encode(PROTOCOL, block.chainid, address(this), confederationScope,
            view_, state, adapter, policyHash, authorization.dkgId(), authorization.resultHash()));
    }

    /// @notice Propose a canonical child; only threshold authorization activates it.
    function createTreasury(uint64 epoch, address[] calldata payees, uint256[] calldata weights)
        external returns (address treasury)
    {
        require(epoch == treasuryEpoch + 1, "next epoch");
        treasury = address(new ConfederationGasTreasury(address(this), address(authorization),
            confederationScope, epoch, payees, weights));
        isTreasury[treasury] = true;
        emit TreasuryCreated(treasury, epoch, ConfederationGasTreasury(payable(treasury)).rosterHash());
    }

    function getConfigureOperationHash(Configuration calldata next) public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.configure.selector,
            keccak256(abi.encode(domain, next)), configurationNonce);
    }

    function configure(Configuration calldata next, bytes32 operation) external nonReentrant {
        _state();
        require(isTreasury[next.treasury] && next.issuer != address(0), "configuration");
        uint64 epoch = ConfederationGasTreasury(payable(next.treasury)).epoch();
        require((epoch == treasuryEpoch && next.treasury == configuration.treasury)
            || epoch == treasuryEpoch + 1, "treasury epoch");
        // Explicit handover terms are mandatory for rotation, even if the terms
        // attest that there is no unfinished paid work. This is not proof of SLA.
        require(treasuryEpoch == 0 || epoch == treasuryEpoch || next.handoverCommitment != 0, "handover");
        require(next.tariffVersion != 0 && next.tariffHash != 0 && next.amount != 0, "tariff");
        bytes32 tariff = keccak256(abi.encode(next.tariffHash, next.amount));
        require(tariffCommitments[next.tariffVersion] == 0 || tariffCommitments[next.tariffVersion] == tariff,
            "immutable tariff");
        require(operation == getConfigureOperationHash(next), "operation");
        authorization.consumeOperation(operation);
        ++configurationNonce;
        ++generation;
        tariffCommitments[next.tariffVersion] = tariff;
        configuration = next;
        treasuryEpoch = epoch;
        enabled = true;
        emit ConfigurationActivated(generation, next.treasury, next.issuer, next.tariffVersion,
            next.tariffHash, next.amount, next.handoverCommitment);
    }

    function getPauseOperationHash() public view returns (bytes32) {
        return authorization.hashOperation(address(this), this.pauseAdmissions.selector, domain, configurationNonce);
    }

    /// @notice Revocation remains possible during a state halt or deactivation.
    function pauseAdmissions(bytes32 operation) external nonReentrant {
        require(operation == getPauseOperationHash(), "operation");
        authorization.consumeOperation(operation);
        ++configurationNonce;
        ++generation;
        enabled = false;
        emit AdmissionsPaused(generation);
    }

    function admit(JobTerms calldata terms, bytes32 observedState, uint256 observedVersion,
        uint64 acceptBy, uint256 previousNonce) external
    {
        require(enabled && msg.sender == configuration.issuer, "issuer/disabled");
        (bytes32 current, uint256 version) = _state();
        require(current == observedState && version == observedVersion, "state changed");
        require(terms.jobId != 0 && terms.semanticKey != 0 && terms.payer != address(0)
            && terms.budgetScope != 0, "job terms");
        require(acceptBy > block.timestamp && acceptBy <= terms.serviceDeadline, "deadline");
        require(!paid(terms.jobId), "already paid");
        Permission storage permission = permissions[terms.jobId];
        require(permission.nonce == previousNonce, "permission changed");
        if (jobs[terms.jobId].jobId == 0) {
            require(semanticJob[terms.semanticKey] == 0, "semantic job exists");
            _member(terms.federationIdHash);
            semanticJob[terms.semanticKey] = terms.jobId;
            jobs[terms.jobId] = terms;
        } else {
            require(keccak256(abi.encode(jobs[terms.jobId])) == keccak256(abi.encode(terms)), "immutable job");
        }
        if (!permission.active) {
            require(pendingCount < MAX_PENDING, "pending limit");
            ++pendingCount;
        }
        uint256 nonce = ++permissionNonce;
        permissions[terms.jobId] = Permission(nonce, generation, current, version, acceptBy, true);
        emit JobAdmitted(terms.jobId, terms.semanticKey, nonce, generation, current, version, acceptBy);
    }

    /// @notice Free a pending slot without deleting economic identity or history.
    function cancelPermission(bytes32 jobId, uint256 expectedNonce) external {
        Permission storage p = permissions[jobId];
        require(p.active && p.nonce == expectedNonce, "permission");
        require(msg.sender == configuration.issuer || !enabled || p.generation != generation
            || block.timestamp > p.acceptBy, "not cancellable");
        p.active = false;
        --pendingCount;
        emit PermissionCancelled(jobId, p.nonce);
    }

    function paid(bytes32 jobId) public view returns (bool) { return payments[jobId].treasury != address(0); }

    /// @notice Exact direct payer consent, one value transfer per stable job.
    /// @dev Value, permission consumption and paid marker revert atomically on failure.
    function fundRequest(bytes32 jobId, uint256 expectedNonce) external payable nonReentrant {
        require(enabled, "disabled");
        require(!paid(jobId), "already paid");
        JobTerms storage job = jobs[jobId];
        Permission storage p = permissions[jobId];
        require(p.active && p.nonce == expectedNonce && p.generation == generation, "permission");
        require(msg.sender == job.payer, "payer");
        require(msg.value == configuration.amount, "amount");
        require(block.timestamp <= p.acceptBy && block.timestamp <= job.serviceDeadline, "deadline");
        (bytes32 current, uint256 version) = _state();
        require(current == p.stateHash && version == p.stateVersion, "state changed");
        p.active = false;
        --pendingCount;
        payments[jobId] = Payment(configuration.treasury, treasuryEpoch, configuration.tariffVersion,
            msg.value, p.nonce);
        Address.sendValue(payable(configuration.treasury), msg.value);
        emit JobFunded(jobId, msg.sender, configuration.treasury, treasuryEpoch,
            configuration.tariffVersion, p.nonce, msg.value);
    }

    function _state() private view returns (bytes32 digest, uint256 version) {
        IConfederationFactory.ConfederationConfig memory conf = factory.getConfederation(confederationId);
        require(conf.isActive && conf.frostMultisig == address(authorization), "confederation");
        require(address(stateView).codehash == stateViewCodeHash && state.codehash == stateCodeHash
            && adapter.codehash == adapterCodeHash && stateView.state() == state && stateView.adapter() == adapter
            && stateView.policyHash() == policyHash, "state binding");
        bool initialized;
        bool halted;
        (digest, version, initialized, halted) = stateView.snapshot();
        require(initialized && !halted && digest != 0, "state unavailable");
    }

    function _member(bytes32 federationIdHash) private view {
        string[] memory ids = factory.getConfederation(confederationId).federationIds;
        for (uint256 i; i < ids.length; ++i) if (keccak256(bytes(ids[i])) == federationIdHash) return;
        revert("federation");
    }
}
