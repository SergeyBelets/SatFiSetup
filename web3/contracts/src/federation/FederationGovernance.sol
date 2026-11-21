// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../FROSTAuthorization.sol";
import "../demo/SimpleMultiPool.sol";
import "./IFeeManager.sol";
import "../tokens/FederationNFTModifier.sol";
import "./ProjectWalletRegistry.sol";
import "./FeeDistributionCalculator.sol";

/**
 * @title FederationGovernance
 * @notice Governance contract for federations using FROST threshold signatures
 * @dev Allows federation members to propose and execute governance actions.
 *      Actions are approved via FROST threshold signatures.
 */
contract FederationGovernance {
    // ======== ENUMS ========

    enum ProposalState {
        Pending,
        Active,
        Defeated,
        Succeeded,
        Executed,
        Cancelled
    }

    enum ProposalType {
        AddMember,
        RemoveMember,
        ChangeThreshold,
        AddPool,
        DeactivatePool,
        Custom,
        UpdateFeeConfig,
        UpdateTreasury,
        MintNFT,
        UpdateProjectWallets,
        UpdateDistributionConfig
    }

    // ======== STATE ========

    /// @notice FROST multisig for approving proposals
    FROSTAuthorization public immutable frostMultisig;

    /// @notice MultiPool managed by this governance
    SimpleMultiPool public immutable multiPool;

    /// @notice Federation identifier
    string public federationId;

    /// @notice Fee system contracts (optional, can be address(0))
    IFeeManager public feeManager;
    FederationNFTModifier public nftModifier;
    ProjectWalletRegistry public walletRegistry;
    FeeDistributionCalculator public feeDistributor;

    /// @notice Proposal struct
    struct Proposal {
        uint256 id;
        address proposer;
        ProposalType proposalType;
        bytes data; // Encoded action data
        string description;
        uint256 createdAt;
        uint256 deadline;
        bytes32 operationHash;
        ProposalState state;
        bool executed;
    }

    /// @notice All proposals
    mapping(uint256 => Proposal) public proposals;

    /// @notice Proposal counter
    uint256 public proposalCounter;

    /// @notice Voting period in seconds
    uint256 public constant VOTING_PERIOD = 3 days;

    /// @notice Minimum delay before execution
    uint256 public constant EXECUTION_DELAY = 1 days;

    // ======== EVENTS ========

    event ProposalCreated(
        uint256 indexed proposalId,
        address indexed proposer,
        ProposalType proposalType,
        string description,
        bytes32 operationHash
    );

    event ProposalExecuted(uint256 indexed proposalId);
    event ProposalCancelled(uint256 indexed proposalId);
    event VoteCast(uint256 indexed proposalId, address indexed voter);

    // ======== ERRORS ========

    error NotAMember();
    error ProposalNotFound();
    error ProposalNotActive();
    error ProposalNotSucceeded();
    error ProposalAlreadyExecuted();
    error ExecutionTooEarly();
    error VotingPeriodEnded();
    error VotingPeriodNotEnded();
    error InvalidProposalType();
    error EmptyDescription();
    error ConfigurationRotationRequiresNewDKG();

    // ======== CONSTRUCTOR ========

    /**
     * @notice Create a federation governance contract
     * @param _federationId Federation identifier
     * @param _frostMultisig Address of the FROST multisig
     * @param _multiPool Address of the MultiPool
     */
    constructor(string memory _federationId, address _frostMultisig, address _multiPool) {
        require(bytes(_federationId).length > 0, "Empty federation ID");
        require(_frostMultisig != address(0), "Invalid FROST address");
        require(_multiPool != address(0), "Invalid MultiPool address");

        federationId = _federationId;
        frostMultisig = FROSTAuthorization(_frostMultisig);
        multiPool = SimpleMultiPool(_multiPool);
    }

    // ======== MODIFIERS ========

    modifier onlyMember() {
        if (!frostMultisig.isParticipant(msg.sender)) revert NotAMember();
        _;
    }

    // ======== PROPOSAL CREATION ========

    /**
     * @notice Create a proposal to add a new member
     * @param newMember Address of the new member
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeAddMember(address newMember, string calldata description)
        external
        view
        onlyMember
        returns (uint256)
    {
        newMember;
        description;
        revert ConfigurationRotationRequiresNewDKG();
    }

    /**
     * @notice Create a proposal to remove a member
     * @param member Address of the member to remove
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeRemoveMember(address member, string calldata description)
        external
        view
        onlyMember
        returns (uint256)
    {
        member;
        description;
        revert ConfigurationRotationRequiresNewDKG();
    }

    /**
     * @notice Create a proposal to change the threshold
     * @param newThreshold New threshold value
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeThresholdChange(uint256 newThreshold, string calldata description)
        external
        view
        onlyMember
        returns (uint256)
    {
        newThreshold;
        description;
        revert ConfigurationRotationRequiresNewDKG();
    }

    /**
     * @notice Create a proposal to add a pool
     * @param mpToken Address of the MP token
     * @param poolId Pool identifier
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeAddPool(address mpToken, string calldata poolId, string calldata description)
        external
        onlyMember
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(mpToken, poolId);
        return _createProposal(ProposalType.AddPool, data, description);
    }

    /**
     * @notice Create a proposal to deactivate a pool
     * @param mpToken Address of the MP token
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeDeactivatePool(address mpToken, string calldata description)
        external
        onlyMember
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(mpToken);
        return _createProposal(ProposalType.DeactivatePool, data, description);
    }

    /**
     * @notice Create a proposal to update fee configuration
     * @param isWrap True for wrap config, false for unwrap config
     * @param feeConfig Encoded fee config
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeUpdateFeeConfig(bool isWrap, bytes calldata feeConfig, string calldata description)
        external
        onlyMember
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(isWrap, feeConfig);
        return _createProposal(ProposalType.UpdateFeeConfig, data, description);
    }

    /**
     * @notice Create a proposal to mint an NFT discount
     * @param to Recipient address
     * @param expiration Expiration timestamp
     * @param volumeLimit Volume limit for discount
     * @param discountBps Discount in basis points
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeMintNFT(
        address to,
        uint256 expiration,
        uint256 volumeLimit,
        uint256 discountBps,
        string calldata description
    ) external onlyMember returns (uint256 proposalId) {
        bytes memory data = abi.encode(to, expiration, volumeLimit, discountBps);
        return _createProposal(ProposalType.MintNFT, data, description);
    }

    /**
     * @notice Create a custom proposal
     * @param data Encoded action data
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeCustomAction(bytes calldata data, string calldata description)
        external
        onlyMember
        returns (uint256 proposalId)
    {
        return _createProposal(ProposalType.Custom, data, description);
    }

    /**
     * @notice Create a proposal with action data (alias for proposeCustomAction)
     * @dev Matches design document API: proposeAction(bytes action, string description)
     * @param action Encoded action data
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeAction(bytes calldata action, string calldata description)
        external
        onlyMember
        returns (uint256 proposalId)
    {
        return _createProposal(ProposalType.Custom, action, description);
    }

    /**
     * @notice Propose member change (add or remove)
     * @dev Matches design document API: proposeMemberChange(address member, bool add)
     * @param member Address of the member
     * @param add True to add, false to remove
     * @return proposalId The created proposal ID
     */
    function proposeMemberChange(address member, bool add, string calldata description)
        external
        onlyMember
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(member);
        if (add) {
            return _createProposal(ProposalType.AddMember, data, description);
        } else {
            return _createProposal(ProposalType.RemoveMember, data, description);
        }
    }

    // ======== VOTING ========

    /**
     * @notice Authorize a proposal from a finalized coordinator session
     * @param proposalId The proposal to vote on
     * @param sessionId Finalized enforced FROST session
     */
    function vote(uint256 proposalId, uint256 sessionId) external onlyMember {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Active) revert ProposalNotActive();
        if (block.timestamp > proposal.deadline) revert VotingPeriodEnded();

        address target = _operationTarget(proposal);
        frostMultisig.authorizeOperation(sessionId, proposal.operationHash, target);

        emit VoteCast(proposalId, msg.sender);

        // Check if threshold is now met
        if (frostMultisig.isOperationApproved(proposal.operationHash, target)) {
            proposal.state = ProposalState.Succeeded;
        }
    }

    /**
     * @notice Vote on a proposal with operationHash verification
     * @dev Matches design document API: voteOnProposal(uint256 proposalId, bytes32 operationHash)
     * @param proposalId The proposal to vote on
     * @param operationHash The operation hash to verify (must match proposal's hash)
     */
    function voteOnProposal(uint256 proposalId, bytes32 operationHash, uint256 sessionId) external onlyMember {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Active) revert ProposalNotActive();
        if (block.timestamp > proposal.deadline) revert VotingPeriodEnded();
        require(proposal.operationHash == operationHash, "Operation hash mismatch");

        address target = _operationTarget(proposal);
        frostMultisig.authorizeOperation(sessionId, operationHash, target);

        emit VoteCast(proposalId, msg.sender);

        // Check if threshold is now met
        if (frostMultisig.isOperationApproved(proposal.operationHash, target)) {
            proposal.state = ProposalState.Succeeded;
        }
    }

    /**
     * @notice Update proposal state based on FROST signatures (anyone can call)
     * @dev Useful when signatures were collected outside of vote() calls
     * @param proposalId The proposal to update
     */
    function updateProposalState(uint256 proposalId) external {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Active) revert ProposalNotActive();

        if (frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal))) {
            proposal.state = ProposalState.Succeeded;
        }
    }

    // ======== EXECUTION ========

    /**
     * @notice Execute a succeeded proposal
     * @param proposalId The proposal to execute
     */
    function execute(uint256 proposalId) external {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Succeeded) revert ProposalNotSucceeded();
        if (proposal.executed) revert ProposalAlreadyExecuted();
        if (block.timestamp < proposal.deadline + EXECUTION_DELAY) revert ExecutionTooEarly();

        proposal.executed = true;
        proposal.state = ProposalState.Executed;

        // Execute the action based on type
        // Note: Action functions (addSigner, removeSigner, etc.) handle their own execution tracking
        _executeAction(proposal);

        emit ProposalExecuted(proposalId);
    }

    /**
     * @notice Execute a proposal with operationHash verification
     * @dev Matches design document API: executeProposal(uint256 proposalId, bytes32 operationHash)
     * @param proposalId The proposal to execute
     * @param operationHash The operation hash to verify (must match proposal's hash)
     */
    function executeProposal(uint256 proposalId, bytes32 operationHash) external {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        require(proposal.operationHash == operationHash, "Operation hash mismatch");
        if (proposal.state != ProposalState.Succeeded) revert ProposalNotSucceeded();
        if (proposal.executed) revert ProposalAlreadyExecuted();
        if (block.timestamp < proposal.deadline + EXECUTION_DELAY) revert ExecutionTooEarly();

        proposal.executed = true;
        proposal.state = ProposalState.Executed;

        _executeAction(proposal);

        emit ProposalExecuted(proposalId);
    }

    /**
     * @notice Cancel a proposal (only proposer can cancel pending proposals)
     * @param proposalId The proposal to cancel
     */
    function cancel(uint256 proposalId) external {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        require(msg.sender == proposal.proposer, "Only proposer can cancel");
        require(proposal.state == ProposalState.Pending || proposal.state == ProposalState.Active, "Cannot cancel");

        proposal.state = ProposalState.Cancelled;

        emit ProposalCancelled(proposalId);
    }

    /**
     * @notice Finalize a proposal after voting period ends
     * @param proposalId The proposal to finalize
     */
    function finalize(uint256 proposalId) external {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Active) revert ProposalNotActive();
        if (block.timestamp <= proposal.deadline) revert VotingPeriodNotEnded();

        if (frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal))) {
            proposal.state = ProposalState.Succeeded;
        } else {
            proposal.state = ProposalState.Defeated;
        }
    }

    // ======== VIEW FUNCTIONS ========

    /**
     * @notice Get proposal details
     */
    function getProposal(uint256 proposalId) external view returns (Proposal memory) {
        return proposals[proposalId];
    }

    /**
     * @notice Get current proposal state
     */
    function getProposalState(uint256 proposalId) external view returns (ProposalState) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();

        // Dynamic state calculation
        if (
            proposal.state == ProposalState.Executed || proposal.state == ProposalState.Cancelled
                || proposal.state == ProposalState.Defeated
        ) {
            return proposal.state;
        }

        if (proposal.state == ProposalState.Succeeded) {
            return ProposalState.Succeeded;
        }

        if (block.timestamp > proposal.deadline) {
            if (frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal))) {
                return ProposalState.Succeeded;
            }
            return ProposalState.Defeated;
        }

        return ProposalState.Active;
    }

    /**
     * @notice Get vote count for a proposal
     */
    function getVoteCount(uint256 proposalId) external view returns (uint256) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        return frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal))
            ? frostMultisig.threshold()
            : 0;
    }

    /**
     * @notice Check if threshold is met for a proposal
     */
    function isThresholdMet(uint256 proposalId) external view returns (bool) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        return frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal));
    }

    /**
     * @notice Get the FROST threshold
     */
    function getThreshold() external view returns (uint256) {
        return frostMultisig.threshold();
    }

    /**
     * @notice Get total proposal count
     */
    function getProposalCount() external view returns (uint256) {
        return proposalCounter;
    }

    // ======== INTERNAL FUNCTIONS ========

    function _createProposal(ProposalType proposalType, bytes memory data, string calldata description)
        internal
        returns (uint256 proposalId)
    {
        if (bytes(description).length == 0) revert EmptyDescription();

        proposalId = ++proposalCounter;

        bytes32 operationHash = _deriveOperationHash(proposalId, proposalType, data);

        proposals[proposalId] = Proposal({
            id: proposalId,
            proposer: msg.sender,
            proposalType: proposalType,
            data: data,
            description: description,
            createdAt: block.timestamp,
            deadline: block.timestamp + VOTING_PERIOD,
            operationHash: operationHash,
            state: ProposalState.Active,
            executed: false
        });

        emit ProposalCreated(proposalId, msg.sender, proposalType, description, operationHash);
    }

    function _deriveOperationHash(uint256 proposalId, ProposalType proposalType, bytes memory data)
        internal
        view
        returns (bytes32)
    {
        if (proposalType == ProposalType.AddPool) {
            (address mpToken, string memory poolId) = abi.decode(data, (address, string));
            return multiPool.getAddPoolOperationHash(mpToken, poolId);
        }
        if (proposalType == ProposalType.DeactivatePool) {
            address mpToken = abi.decode(data, (address));
            return multiPool.getDeactivatePoolOperationHash(mpToken);
        }

        return keccak256(
            abi.encode(
                "SATOHSI_FEDERATION_PROPOSAL_V1",
                block.chainid,
                address(this),
                federationId,
                proposalId,
                proposalType,
                data
            )
        );
    }

    function _operationTarget(Proposal storage proposal) internal view returns (address) {
        if (proposal.proposalType == ProposalType.AddPool || proposal.proposalType == ProposalType.DeactivatePool) {
            return address(multiPool);
        }
        return address(this);
    }

    function _executeAction(Proposal storage proposal) internal {
        if (_operationTarget(proposal) == address(this)) {
            frostMultisig.consumeOperation(proposal.operationHash);
        }
        if (proposal.proposalType == ProposalType.AddPool) {
            (address mpToken, string memory poolId) = abi.decode(proposal.data, (address, string));
            multiPool.addPool(mpToken, poolId, proposal.operationHash);
        } else if (proposal.proposalType == ProposalType.DeactivatePool) {
            address mpToken = abi.decode(proposal.data, (address));
            multiPool.deactivatePool(mpToken, proposal.operationHash);
        } else if (proposal.proposalType == ProposalType.MintNFT) {
            if (address(nftModifier) != address(0)) {
                (address to, uint256 expiration, uint256 volumeLimit, uint256 discountBps) =
                    abi.decode(proposal.data, (address, uint256, uint256, uint256));
                nftModifier.mint(to, expiration, volumeLimit, discountBps);
            }
        }
        // Custom, UpdateFeeConfig, UpdateTreasury, UpdateProjectWallets, UpdateDistributionConfig
        // are signal-only proposals - actual execution happens via direct contract calls with FROST approval
    }
}
