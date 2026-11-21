// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinHeaderTime.sol";

/// @notice Strict decoding and EVM acceptance checks for the complete V1 journal.
/// @dev This does NOT verify a proof. The integrating state contract must verify
/// these EXACT bytes with its immutable real verifier/program ID before writing
/// any state, and enforce its selected relay-certificate profile separately.
/// No production contract imports this component yet.
library BitcoinTransition {
    bytes32 internal constant DOMAIN = bytes32("SATOSHIFI_TRANSITION_V1");
    bytes32 internal constant NETWORK = bytes32("bitcoin-mainnet");
    struct State {
        bytes32 anchor;
        uint64 anchorHeight;
        bytes32 head;
        uint64 height;
        uint256 work;
        uint32 timestamp;
        bytes32 canonicalRoot;
        bytes32 commitment;
        bool halted;
    }
    struct Transition {
        uint256 evmChainId;
        address stateAddress;
        bytes32 checkpointDescriptor;
        State beforeState;
        State afterState;
        bytes32 batchCommitment;
        uint64 submitted;
        uint64 processed;
        BitcoinHeaderTime.Summary timeSummary;
        uint64 haltPosition;
        bytes32 haltOldHead;
        bytes32 haltCandidate;
        bytes32 haltLca;
        uint64 haltDepth;
    }
    error InvalidEncoding();
    error InvalidTransition();
    error WrongDeployment();
    error StaleState();
    error AlreadyHalted();
    error NoChange();

    function decode(bytes memory journal) internal pure returns (Transition memory t) {
        if (journal.length != 1024) revert InvalidEncoding();
        uint256[32] memory w = abi.decode(journal, (uint256[32]));
        if (bytes32(w[0]) != DOMAIN || bytes32(w[1]) != NETWORK || w[3] > type(uint160).max || w[26] > 1) {
            revert InvalidEncoding();
        }
        uint8[8] memory narrow64 = [uint8(6),12,16,20,21,23,27,31];
        for (uint256 i; i < narrow64.length; ++i) if (w[narrow64[i]] > type(uint64).max) revert InvalidEncoding();
        if (w[14] > type(uint32).max || w[18] > type(uint32).max) revert InvalidEncoding();
        t.evmChainId = w[2]; t.stateAddress = address(uint160(w[3])); t.checkpointDescriptor = bytes32(w[4]);
        t.beforeState = State(bytes32(w[5]),uint64(w[6]),bytes32(w[11]),uint64(w[12]),w[13],uint32(w[14]),bytes32(w[9]),bytes32(w[7]),false);
        t.afterState = State(bytes32(w[5]),uint64(w[6]),bytes32(w[15]),uint64(w[16]),w[17],uint32(w[18]),bytes32(w[10]),bytes32(w[8]),w[26]==1);
        t.batchCommitment = bytes32(w[19]); t.submitted = uint64(w[20]); t.processed = uint64(w[21]);
        t.timeSummary = BitcoinHeaderTime.decode(abi.encode(bytes32(w[22]),w[23],w[24],w[25]));
        t.haltPosition = uint64(w[27]); t.haltOldHead = bytes32(w[28]);
        t.haltCandidate = bytes32(w[29]); t.haltLca = bytes32(w[30]); t.haltDepth = uint64(w[31]);
        checkShape(t);
    }

    function checkShape(Transition memory t) private pure {
        State memory old = t.beforeState;
        State memory next = t.afterState;
        if (old.anchorHeight < 10 || old.anchorHeight % 2016 != 0 || old.height < old.anchorHeight
            || next.height < old.anchorHeight || old.work == 0 || next.work < old.work
            || t.submitted == 0 || t.submitted > 64 || t.processed == 0 || t.processed > t.submitted
            || t.timeSummary.headerCount > t.processed) revert InvalidTransition();
        if (next.commitment == old.commitment) revert NoChange();
        if (next.work == old.work) {
            if (next.head != old.head || next.height != old.height || next.timestamp != old.timestamp
                || next.canonicalRoot != old.canonicalRoot) revert InvalidTransition();
        } else if (next.head == old.head) revert InvalidTransition();
        if (next.halted) {
            if (t.haltPosition != t.processed || t.haltDepth <= 100
                || t.haltDepth > next.height - old.anchorHeight || t.haltOldHead != next.head
                || t.haltCandidate == next.head || t.haltLca == t.haltCandidate) revert InvalidTransition();
        } else if (t.processed != t.submitted || t.haltPosition != 0 || t.haltDepth != 0
            || t.haltOldHead != bytes32(0) || t.haltCandidate != bytes32(0) || t.haltLca != bytes32(0)) {
            revert InvalidTransition();
        }
    }

    /// Called after real proof verification and before any storage write. Uses
    /// the accepting EVM's clock/address/chain, never prover-provided time.
    function validate(bytes memory authenticatedJournal, bytes32 checkpointDescriptor, State memory current)
        internal view returns (Transition memory t)
    {
        if (current.halted) revert AlreadyHalted();
        t = decode(authenticatedJournal);
        if (t.evmChainId != block.chainid || t.stateAddress != address(this)
            || t.checkpointDescriptor != checkpointDescriptor) revert WrongDeployment();
        if (keccak256(abi.encode(t.beforeState)) != keccak256(abi.encode(current))) revert StaleState();
        BitcoinHeaderTime.validate(abi.encode(BitcoinHeaderTime.DOMAIN,t.timeSummary.headerCount,
            t.timeSummary.maximumTimestamp,t.timeSummary.lastTimestamp));
    }

    function useful(Transition memory t) internal pure returns (bool) {
        return t.afterState.work > t.beforeState.work;
    }
}
