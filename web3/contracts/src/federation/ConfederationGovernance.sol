// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../FROSTAuthorization.sol";
import "./ConfederationMultiPool.sol";
import "./CustodianValidator.sol";
import "./IFeeManager.sol";
import "../tokens/FederationNFTModifier.sol";
import "./ProjectWalletRegistry.sol";
import "./FeeDistributionCalculator.sol";

/**
 * @title ConfederationGovernance
 * @notice Governance contract for confederations requiring dual approval
 * @dev Requires both FROST threshold signatures AND minimum custodian signatures
 *      for proposal approval. This provides additional security for confederation
 *      operations that involve both federation members and institutional custodians.
 */
contract ConfederationGovernance {
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
        AddFederation,
        RemoveFederation,
        AddCustodian,
        RemoveCustodian,
        ChangeThreshold,
        ChangeCustodianMinimum,
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

    /// @notice Custodian validator for custodian approvals
    CustodianValidator public immutable custodianValidator;

    /// @notice ConfederationMultiPool managed by this governance
    ConfederationMultiPool public immutable multiPool;

    /// @notice Confederation identifier
    string public confederationId;

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
        bytes data;
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
    event FROSTVoteCast(uint256 indexed proposalId, address indexed voter);
    event CustodianVoteCast(uint256 indexed proposalId, address indexed custodian);

    // ======== ERRORS ========

    error NotAuthorized();
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
    error InsufficientFROSTSignatures();
    error InsufficientCustodianSignatures();

    // ======== CONSTRUCTOR ========

    /**
     * @notice Create a confederation governance contract
     * @param _confederationId Confederation identifier
     * @param _frostMultisig Address of the FROST multisig
     * @param _custodianValidator Address of the custodian validator
     * @param _multiPool Address of the ConfederationMultiPool
     */
    constructor(
        string memory _confederationId,
        address _frostMultisig,
        address _custodianValidator,
        address _multiPool
    ) {
        require(bytes(_confederationId).length > 0, "Empty confederation ID");
        require(_frostMultisig != address(0), "Invalid FROST address");
        require(_custodianValidator != address(0), "Invalid custodian validator");
        require(_multiPool != address(0), "Invalid MultiPool address");

        confederationId = _confederationId;
        frostMultisig = FROSTAuthorization(_frostMultisig);
        custodianValidator = CustodianValidator(_custodianValidator);
        multiPool = ConfederationMultiPool(_multiPool);
    }

    // ======== MODIFIERS ========

    modifier onlyAuthorized() {
        if (!frostMultisig.isParticipant(msg.sender) && !custodianValidator.isCustodian(msg.sender)) {
            revert NotAuthorized();
        }
        _;
    }

    // ======== PROPOSAL CREATION ========

    /**
     * @notice Create a proposal to add a new federation
     * @param federationAddress Address of the federation's FROST multisig
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeAddFederation(address federationAddress, string calldata description)
        external
        onlyAuthorized
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(federationAddress);
        return _createProposal(ProposalType.AddFederation, data, description);
    }

    /**
     * @notice Create a proposal to remove a federation
     * @param federationAddress Address of the federation's FROST multisig
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeRemoveFederation(address federationAddress, string calldata description)
        external
        onlyAuthorized
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(federationAddress);
        return _createProposal(ProposalType.RemoveFederation, data, description);
    }

    /**
     * @notice Create a proposal to add a new custodian
     * @param custodianAddress Address of the new custodian
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeAddCustodian(address custodianAddress, string calldata description)
        external
        onlyAuthorized
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(custodianAddress);
        return _createProposal(ProposalType.AddCustodian, data, description);
    }

    /**
     * @notice Create a proposal to remove a custodian
     * @param custodianAddress Address of the custodian to remove
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeRemoveCustodian(address custodianAddress, string calldata description)
        external
        onlyAuthorized
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(custodianAddress);
        return _createProposal(ProposalType.RemoveCustodian, data, description);
    }

    /**
     * @notice Create a proposal to change the FROST threshold
     * @param newThreshold New threshold value
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeThresholdChange(uint256 newThreshold, string calldata description)
        external
        view
        onlyAuthorized
        returns (uint256)
    {
        newThreshold;
        description;
        revert ConfigurationRotationRequiresNewDKG();
    }

    /**
     * @notice Create a proposal to change the minimum custodians required
     * @param newMinimum New minimum custodians value
     * @param description Description of the proposal
     * @return proposalId The created proposal ID
     */
    function proposeCustodianMinimumChange(uint256 newMinimum, string calldata description)
        external
        onlyAuthorized
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(newMinimum);
        return _createProposal(ProposalType.ChangeCustodianMinimum, data, description);
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
        onlyAuthorized
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
        onlyAuthorized
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(mpToken);
        return _createProposal(ProposalType.DeactivatePool, data, description);
    }

    /**
     * @notice Create a proposal to update fee configuration
     */
    function proposeUpdateFeeConfig(bool isWrap, bytes calldata feeConfig, string calldata description)
        external
        onlyAuthorized
        returns (uint256 proposalId)
    {
        bytes memory data = abi.encode(isWrap, feeConfig);
        return _createProposal(ProposalType.UpdateFeeConfig, data, description);
    }

    /**
     * @notice Create a proposal to mint an NFT discount
     */
    function proposeMintNFT(
        address to,
        uint256 expiration,
        uint256 volumeLimit,
        uint256 discountBps,
        string calldata description
    ) external onlyAuthorized returns (uint256 proposalId) {
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
        onlyAuthorized
        returns (uint256 proposalId)
    {
        return _createProposal(ProposalType.Custom, data, description);
    }

    // ======== VOTING ========

    /**
     * @notice Authorize a proposal from a finalized coordinator session
     * @param proposalId The proposal to vote on
     * @param sessionId Finalized enforced FROST session
     */
    function voteAsFROSTMember(uint256 proposalId, uint256 sessionId) external {
        if (!frostMultisig.isParticipant(msg.sender)) revert NotAuthorized();

        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Active) revert ProposalNotActive();
        if (block.timestamp > proposal.deadline) revert VotingPeriodEnded();

        frostMultisig.authorizeOperation(sessionId, proposal.operationHash, _operationTarget(proposal));

        emit FROSTVoteCast(proposalId, msg.sender);

        // Check if both thresholds are now met
        _checkAndUpdateProposalState(proposal);
    }

    /**
     * @notice Vote on a proposal as a custodian
     * @dev Custodian must register signature on CustodianValidator first
     * @param proposalId The proposal to vote on
     */
    function voteAsCustodian(uint256 proposalId) external {
        if (!custodianValidator.isCustodian(msg.sender)) revert NotAuthorized();

        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Active) revert ProposalNotActive();
        if (block.timestamp > proposal.deadline) revert VotingPeriodEnded();

        // Registration must originate from the custodian's wallet at the validator.
        // Forwarding it here would authenticate this governance contract instead.
        require(
            custodianValidator.hasCustodianSigned(proposal.operationHash, msg.sender),
            "Custodian signature not registered"
        );

        emit CustodianVoteCast(proposalId, msg.sender);

        // Check if both thresholds are now met
        _checkAndUpdateProposalState(proposal);
    }

    /**
     * @notice Update proposal state based on signatures (anyone can call)
     * @param proposalId The proposal to update
     */
    function updateProposalState(uint256 proposalId) external {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        if (proposal.state != ProposalState.Active) revert ProposalNotActive();

        _checkAndUpdateProposalState(proposal);
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

        // Verify both thresholds are still met
        if (!frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal))) {
            revert InsufficientFROSTSignatures();
        }
        if (!custodianValidator.meetsMinimumCustodians(proposal.operationHash)) {
            revert InsufficientCustodianSignatures();
        }

        proposal.executed = true;
        proposal.state = ProposalState.Executed;

        // Execute the action based on type
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

        bool frostMet = frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal));
        bool custodianMet = custodianValidator.meetsMinimumCustodians(proposal.operationHash);

        if (frostMet && custodianMet) {
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

        // Return stored terminal states
        if (
            proposal.state == ProposalState.Executed || proposal.state == ProposalState.Cancelled
                || proposal.state == ProposalState.Defeated
        ) {
            return proposal.state;
        }

        if (proposal.state == ProposalState.Succeeded) {
            return ProposalState.Succeeded;
        }

        // Dynamic calculation for active proposals
        if (block.timestamp > proposal.deadline) {
            bool frostMet = frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal));
            bool custodianMet = custodianValidator.meetsMinimumCustodians(proposal.operationHash);
            if (frostMet && custodianMet) {
                return ProposalState.Succeeded;
            }
            return ProposalState.Defeated;
        }

        return ProposalState.Active;
    }

    /**
     * @notice Get FROST vote count for a proposal
     */
    function getFROSTVoteCount(uint256 proposalId) external view returns (uint256) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        return frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal))
            ? frostMultisig.threshold()
            : 0;
    }

    /**
     * @notice Get custodian vote count for a proposal
     */
    function getCustodianVoteCount(uint256 proposalId) external view returns (uint256) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        return custodianValidator.countCustodianSignatures(proposal.operationHash);
    }

    /**
     * @notice Check if FROST threshold is met for a proposal
     */
    function isFROSTThresholdMet(uint256 proposalId) external view returns (bool) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        return frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal));
    }

    /**
     * @notice Check if custodian minimum is met for a proposal
     */
    function isCustodianMinimumMet(uint256 proposalId) external view returns (bool) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        return custodianValidator.meetsMinimumCustodians(proposal.operationHash);
    }

    /**
     * @notice Check if both thresholds are met (proposal can succeed)
     */
    function canSucceed(uint256 proposalId) external view returns (bool frostMet, bool custodianMet) {
        Proposal storage proposal = proposals[proposalId];
        if (proposal.id == 0) revert ProposalNotFound();
        frostMet = frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal));
        custodianMet = custodianValidator.meetsMinimumCustodians(proposal.operationHash);
    }

    /**
     * @notice Get the FROST threshold
     */
    function getFROSTThreshold() external view returns (uint256) {
        return frostMultisig.threshold();
    }

    /**
     * @notice Get the minimum custodians required
     */
    function getCustodianMinimum() external view returns (uint256) {
        return custodianValidator.getMinCustodiansRequired();
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
        if (proposalType == ProposalType.AddCustodian) {
            address newCustodian = abi.decode(data, (address));
            return custodianValidator.getAddCustodianOperationHash(newCustodian);
        }
        if (proposalType == ProposalType.RemoveCustodian) {
            address custodian = abi.decode(data, (address));
            return custodianValidator.getRemoveCustodianOperationHash(custodian);
        }
        if (proposalType == ProposalType.ChangeThreshold) {
            revert ConfigurationRotationRequiresNewDKG();
        }
        if (proposalType == ProposalType.ChangeCustodianMinimum) {
            uint256 newMinimum = abi.decode(data, (uint256));
            return custodianValidator.getSetMinCustodiansOperationHash(newMinimum);
        }
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
                "SATOHSI_CONFEDERATION_PROPOSAL_V1",
                block.chainid,
                address(this),
                confederationId,
                proposalId,
                proposalType,
                data
            )
        );
    }

    function _checkAndUpdateProposalState(Proposal storage proposal) internal {
        bool frostMet = frostMultisig.isOperationApproved(proposal.operationHash, _operationTarget(proposal));
        bool custodianMet = custodianValidator.meetsMinimumCustodians(proposal.operationHash);

        if (frostMet && custodianMet) {
            proposal.state = ProposalState.Succeeded;
        }
    }

    function _operationTarget(Proposal storage proposal) internal view returns (address) {
        if (
            proposal.proposalType == ProposalType.AddCustodian || proposal.proposalType == ProposalType.RemoveCustodian
                || proposal.proposalType == ProposalType.ChangeCustodianMinimum
        ) {
            return address(custodianValidator);
        }
        if (proposal.proposalType == ProposalType.AddPool || proposal.proposalType == ProposalType.DeactivatePool) {
            return address(multiPool);
        }
        return address(this);
    }

    function _executeAction(Proposal storage proposal) internal {
        if (_operationTarget(proposal) == address(this)) {
            frostMultisig.consumeOperation(proposal.operationHash);
        }
        if (proposal.proposalType == ProposalType.AddFederation) {
            // Federation management - signal only (manual off-chain process)
        } else if (proposal.proposalType == ProposalType.RemoveFederation) {
            // Federation management - signal only
        } else if (proposal.proposalType == ProposalType.AddCustodian) {
            address newCustodian = abi.decode(proposal.data, (address));
            custodianValidator.addCustodian(newCustodian, proposal.operationHash);
        } else if (proposal.proposalType == ProposalType.RemoveCustodian) {
            address custodian = abi.decode(proposal.data, (address));
            custodianValidator.removeCustodian(custodian, proposal.operationHash);
        } else if (proposal.proposalType == ProposalType.ChangeCustodianMinimum) {
            uint256 newMinimum = abi.decode(proposal.data, (uint256));
            custodianValidator.setMinCustodians(newMinimum, proposal.operationHash);
        } else if (proposal.proposalType == ProposalType.AddPool) {
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
        // are signal-only proposals - actual execution happens via direct contract calls with FROST + custodian approval
    }
}
