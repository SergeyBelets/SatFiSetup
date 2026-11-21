// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {CustodianValidator} from "./CustodianValidator.sol";
import {CustodianDAOStaking} from "./CustodianDAOStaking.sol";

contract CustodianDAORateController {
    uint256 public constant INITIAL_RATE_PPM = 120; // 0.012% per UTC day
    uint256 public constant PROPOSAL_DURATION = 7 days;

    enum Parameter {
        BaseRate,
        UseReserve
    }

    struct Proposal {
        Parameter parameter;
        uint256 value;
        uint64 deadline;
        uint32 custodianCount;
        uint32 requiredVotes;
        uint32 votes;
        bool executed;
    }

    struct Checkpoint {
        uint64 effectiveDay;
        uint192 value;
    }

    CustodianValidator public immutable validator;
    CustodianDAOStaking public immutable staking;
    uint256 public proposalCount;
    mapping(Parameter => uint256) public lastExecutedProposal;
    mapping(uint256 => Proposal) public proposals;
    mapping(uint256 => mapping(address => bool)) public snapshotCustodian;
    mapping(uint256 => mapping(address => bool)) public hasVoted;
    mapping(uint256 => mapping(address => bool)) public signerHasVoted;
    Checkpoint[] private rateHistory;
    Checkpoint[] private reserveHistory;

    event Proposed(uint256 indexed id, Parameter parameter, uint256 value, uint256 requiredVotes);
    event Voted(uint256 indexed id, address indexed custodian, address indexed signer);
    event Executed(uint256 indexed id, uint256 effectiveDay);

    error InvalidAddress();
    error InvalidValue();
    error NotCustodianSigner();
    error NotSnapshotCustodian();
    error ProposalExpired();
    error AlreadyVoted();
    error ThresholdNotMet();
    error AlreadyExecuted();
    error ProposalSuperseded();

    constructor(address validator_, address staking_, bool initialUseReserve) {
        if (validator_ == address(0) || staking_ == address(0)) revert InvalidAddress();
        validator = CustodianValidator(validator_);
        staking = CustodianDAOStaking(staking_);
        uint64 today = uint64(block.timestamp / 1 days);
        rateHistory.push(Checkpoint(today, uint192(INITIAL_RATE_PPM)));
        reserveHistory.push(Checkpoint(today, initialUseReserve ? 1 : 0));
    }

    function propose(Parameter parameter, uint256 value, address custodian) external returns (uint256 id) {
        if (!_isCurrentCustodianSigner(custodian)) revert NotCustodianSigner();
        if (parameter == Parameter.BaseRate && value > 1_000_000) revert InvalidValue();
        if (parameter == Parameter.UseReserve && value > 1) revert InvalidValue();
        address[] memory custodians = validator.getCustodians();
        uint256 count = custodians.length;
        if (count == 0 || count > type(uint32).max) revert InvalidValue();

        id = ++proposalCount;
        uint256 required = (count * 2 + 2) / 3;
        proposals[id] = Proposal(
            parameter, value, uint64(block.timestamp + PROPOSAL_DURATION), uint32(count), uint32(required), 0, false
        );
        for (uint256 i = 0; i < count; i++) {
            snapshotCustodian[id][custodians[i]] = true;
        }
        emit Proposed(id, parameter, value, required);
    }

    function vote(uint256 id, address custodian) external {
        Proposal storage proposal = proposals[id];
        if (proposal.deadline == 0 || block.timestamp > proposal.deadline) revert ProposalExpired();
        if (proposal.executed) revert AlreadyExecuted();
        if (id <= lastExecutedProposal[proposal.parameter]) revert ProposalSuperseded();
        if (!snapshotCustodian[id][custodian]) revert NotSnapshotCustodian();
        if (!validator.isCustodian(custodian)) revert NotSnapshotCustodian();
        if (msg.sender != staking.getDAOSigner(custodian)) revert NotCustodianSigner();
        if (hasVoted[id][custodian] || signerHasVoted[id][msg.sender]) revert AlreadyVoted();
        hasVoted[id][custodian] = true;
        signerHasVoted[id][msg.sender] = true;
        proposal.votes++;
        emit Voted(id, custodian, msg.sender);
    }

    function execute(uint256 id) external {
        Proposal storage proposal = proposals[id];
        if (proposal.deadline == 0 || block.timestamp > proposal.deadline) revert ProposalExpired();
        if (proposal.executed) revert AlreadyExecuted();
        if (id <= lastExecutedProposal[proposal.parameter]) revert ProposalSuperseded();
        if (proposal.votes < proposal.requiredVotes) revert ThresholdNotMet();
        proposal.executed = true;
        lastExecutedProposal[proposal.parameter] = id;
        uint64 effectiveDay = uint64(block.timestamp / 1 days + 1);
        if (proposal.parameter == Parameter.BaseRate) {
            _append(rateHistory, effectiveDay, proposal.value);
        } else {
            _append(reserveHistory, effectiveDay, proposal.value);
        }
        emit Executed(id, effectiveDay);
    }

    function rateForDay(uint256 day) public view returns (uint256) {
        return _lookup(rateHistory, day);
    }

    function useReserveForDay(uint256 day) public view returns (bool) {
        return _lookup(reserveHistory, day) == 1;
    }

    function baseRatePpm() external view returns (uint256) {
        return rateForDay(block.timestamp / 1 days);
    }

    function useReserve() external view returns (bool) {
        return useReserveForDay(block.timestamp / 1 days);
    }

    function canVote(uint256 id, address custodian) external view returns (bool) {
        Proposal storage proposal = proposals[id];
        address signer = staking.getDAOSigner(custodian);
        return proposal.deadline != 0 && block.timestamp <= proposal.deadline && !proposal.executed
            && id > lastExecutedProposal[proposal.parameter]
            && snapshotCustodian[id][custodian] && validator.isCustodian(custodian) && !hasVoted[id][custodian]
            && !signerHasVoted[id][signer];
    }

    function _isCurrentCustodianSigner(address custodian) private view returns (bool) {
        return validator.isCustodian(custodian) && msg.sender == staking.getDAOSigner(custodian);
    }

    function _append(Checkpoint[] storage history, uint64 day, uint256 value) private {
        uint256 last = history.length - 1;
        if (history[last].effectiveDay == day) history[last].value = uint192(value);
        else history.push(Checkpoint(day, uint192(value)));
    }

    function _lookup(Checkpoint[] storage history, uint256 day) private view returns (uint256) {
        uint256 low;
        uint256 high = history.length;
        while (low < high) {
            uint256 mid = (low + high) / 2;
            if (history[mid].effectiveDay <= day) low = mid + 1;
            else high = mid;
        }
        return low == 0 ? history[0].value : history[low - 1].value;
    }
}
