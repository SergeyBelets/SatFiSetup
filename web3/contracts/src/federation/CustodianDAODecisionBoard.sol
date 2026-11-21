// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianDAOStaking} from "./CustodianDAOStaking.sol";

contract CustodianDAODecisionBoard {
    uint256 public constant MIN_VOTE_DURATION = 1 days;
    uint256 public constant MAX_VOTE_DURATION = 30 days;
    uint256 public constant MAX_URI_LENGTH = 256;

    enum Choice {
        Against,
        For,
        Abstain
    }

    struct Decision {
        string metadataURI;
        bytes32 contentHash;
        uint64 snapshotDay;
        uint64 deadline;
        uint16 quorumBps;
        uint256 totalWeight;
        uint256 againstVotes;
        uint256 forVotes;
        uint256 abstainVotes;
        bool finalized;
        bool passed;
    }

    CustodianDAOStaking public immutable staking;
    FROSTAuthorization public immutable authorization;
    uint256 public operationNonce;
    uint256 public decisionCount;
    mapping(uint256 => Decision) public decisions;
    mapping(uint256 => mapping(address => bool)) public voted;
    mapping(bytes32 => bytes32) public creationReceipts;

    event DecisionCreated(uint256 indexed id, uint256 indexed snapshotDay, bytes32 indexed contentHash);
    event VoteCast(uint256 indexed id, address indexed owner, address indexed signer, Choice choice, uint256 weight);
    event DecisionFinalized(uint256 indexed id, bool passed, uint256 participation);

    error InvalidAddress();
    error InvalidDecision();
    error InvalidOperationHash();
    error NoEligibleStake();
    error VotingClosed();
    error VotingOpen();
    error UnauthorizedSigner();
    error AlreadyVoted();
    error AlreadyFinalized();

    constructor(address staking_, address authorization_) {
        if (staking_.code.length == 0 || authorization_.code.length == 0) revert InvalidAddress();
        staking = CustodianDAOStaking(staking_);
        authorization = FROSTAuthorization(authorization_);
    }

    function getCreateOperationHash(
        string calldata metadataURI,
        bytes32 contentHash,
        uint256 duration,
        uint16 quorumBps
    ) public view returns (bytes32) {
        uint256 currentDay = block.timestamp / 1 days;
        if (currentDay == 0) revert NoEligibleStake();
        uint256 snapshotDay = currentDay - 1;
        return authorization.hashOperation(
            address(this),
            this.createDecision.selector,
            keccak256(
                abi.encode(
                    keccak256(bytes(metadataURI)),
                    contentHash,
                    duration,
                    quorumBps,
                    snapshotDay,
                    staking.totalEligibleStake(snapshotDay)
                )
            ),
            operationNonce
        );
    }

    function createDecision(
        string calldata metadataURI,
        bytes32 contentHash,
        uint256 duration,
        uint16 quorumBps,
        bytes32 operationHash
    ) external returns (uint256 id) {
        if (
            bytes(metadataURI).length == 0 || bytes(metadataURI).length > MAX_URI_LENGTH || contentHash == bytes32(0)
                || duration < MIN_VOTE_DURATION || duration > MAX_VOTE_DURATION || quorumBps > 10_000
        ) revert InvalidDecision();
        uint256 currentDay = block.timestamp / 1 days;
        if (currentDay == 0 || currentDay - 1 < staking.firstEligibleDay()) revert NoEligibleStake();
        uint256 snapshotDay = currentDay - 1;
        uint256 totalWeight = staking.totalEligibleStake(snapshotDay);
        if (totalWeight == 0) revert NoEligibleStake();
        if (operationHash != getCreateOperationHash(metadataURI, contentHash, duration, quorumBps)) {
            revert InvalidOperationHash();
        }
        authorization.consumeOperation(operationHash);
        creationReceipts[operationHash] =
            keccak256(abi.encode(keccak256(bytes(metadataURI)), contentHash, duration, quorumBps));
        operationNonce++;

        id = ++decisionCount;
        Decision storage decision = decisions[id];
        decision.metadataURI = metadataURI;
        decision.contentHash = contentHash;
        decision.snapshotDay = uint64(snapshotDay);
        decision.deadline = uint64(block.timestamp + duration);
        decision.quorumBps = quorumBps;
        decision.totalWeight = totalWeight;
        emit DecisionCreated(id, snapshotDay, contentHash);
    }

    function vote(uint256 id, address owner, Choice choice) external {
        Decision storage decision = decisions[id];
        if (decision.deadline == 0 || block.timestamp >= decision.deadline || decision.finalized) {
            revert VotingClosed();
        }
        if (msg.sender != staking.getDAOSigner(owner)) revert UnauthorizedSigner();
        if (voted[id][owner]) revert AlreadyVoted();
        uint256 weight = staking.eligibleStake(owner, decision.snapshotDay);
        if (weight == 0) revert NoEligibleStake();

        voted[id][owner] = true;
        if (choice == Choice.Against) decision.againstVotes += weight;
        else if (choice == Choice.For) decision.forVotes += weight;
        else decision.abstainVotes += weight;
        emit VoteCast(id, owner, msg.sender, choice, weight);
    }

    function finalize(uint256 id) external returns (bool passed) {
        Decision storage decision = decisions[id];
        if (decision.deadline == 0 || block.timestamp < decision.deadline) revert VotingOpen();
        if (decision.finalized) revert AlreadyFinalized();
        uint256 participation = decision.forVotes + decision.againstVotes + decision.abstainVotes;
        passed = participation * 10_000 >= decision.totalWeight * decision.quorumBps
            && decision.forVotes > decision.againstVotes;
        decision.finalized = true;
        decision.passed = passed;
        emit DecisionFinalized(id, passed, participation);
    }

    function canVote(uint256 id, address owner) external view returns (bool) {
        Decision storage decision = decisions[id];
        return decision.deadline != 0 && block.timestamp < decision.deadline && !decision.finalized && !voted[id][owner]
            && staking.eligibleStake(owner, decision.snapshotDay) > 0;
    }
}
