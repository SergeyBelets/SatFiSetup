// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "satoshifi-core/BitcoinHeaderTime.sol";

/// @notice Strict decoding and EVM acceptance checks for the bounded V2 journal.
/// @dev This does NOT verify a proof. The integrating state contract must verify
/// these EXACT bytes with its immutable real verifier/program ID before writing
/// any state, and enforce its selected relay-certificate profile separately.
/// No production contract imports this component yet.
library BitcoinTestnet4TransitionV2 {
    bytes32 internal constant DOMAIN = bytes32("SATOSHIFI_TRANSITION_V2");
    bytes32 internal constant NETWORK = bytes32("bitcoin-testnet4");
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
        bytes32 knownRoot;
        bytes32 knownLog;
        uint64 knownCount;
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
        if (journal.length != 1216) revert InvalidEncoding();
        uint256[38] memory w = abi.decode(journal, (uint256[38]));
        if (bytes32(w[0]) != DOMAIN || bytes32(w[1]) != NETWORK || w[3] > type(uint160).max || w[26] > 1) {
            revert InvalidEncoding();
        }
        uint8[10] memory narrow64 = [uint8(6),12,16,20,21,23,27,31,34,37];
        for (uint256 i; i < narrow64.length; ++i) if (w[narrow64[i]] > type(uint64).max) revert InvalidEncoding();
        if (w[14] > type(uint32).max || w[18] > type(uint32).max) revert InvalidEncoding();
        t.evmChainId = w[2]; t.stateAddress = address(uint160(w[3])); t.checkpointDescriptor = bytes32(w[4]);
        t.beforeState = State(bytes32(w[5]),uint64(w[6]),bytes32(w[11]),uint64(w[12]),w[13],uint32(w[14]),bytes32(w[9]),bytes32(w[7]),false,bytes32(w[32]),bytes32(w[33]),uint64(w[34]));
        t.afterState = State(bytes32(w[5]),uint64(w[6]),bytes32(w[15]),uint64(w[16]),w[17],uint32(w[18]),bytes32(w[10]),bytes32(w[8]),w[26]==1,bytes32(w[35]),bytes32(w[36]),uint64(w[37]));
        t.batchCommitment = bytes32(w[19]); t.submitted = uint64(w[20]); t.processed = uint64(w[21]);
        t.timeSummary = BitcoinHeaderTime.decode(abi.encode(bytes32(w[22]),w[23],w[24],w[25]));
        t.haltPosition = uint64(w[27]); t.haltOldHead = bytes32(w[28]);
        t.haltCandidate = bytes32(w[29]); t.haltLca = bytes32(w[30]); t.haltDepth = uint64(w[31]);
        checkShape(t);
        if (stateCommitment(t.beforeState,t.checkpointDescriptor,bytes32(0),bytes32(0),bytes32(0),0) != t.beforeState.commitment
            || stateCommitment(t.afterState,t.checkpointDescriptor,t.haltOldHead,t.haltCandidate,t.haltLca,t.haltDepth) != t.afterState.commitment) {
            revert InvalidTransition();
        }
    }

    function checkShape(Transition memory t) private pure {
        State memory old = t.beforeState;
        State memory next = t.afterState;
        if (old.anchorHeight < 10 || old.anchorHeight % 2016 != 0 || old.height < old.anchorHeight
            || next.height < old.anchorHeight || old.work == 0 || next.work < old.work
            || t.submitted == 0 || t.submitted > 64 || t.processed == 0 || t.processed > t.submitted
            || t.timeSummary.headerCount > t.processed || old.knownCount == 0 || next.knownCount <= old.knownCount
            || next.knownCount - old.knownCount != t.timeSummary.headerCount
            || old.knownRoot == next.knownRoot || old.knownLog == next.knownLog) revert InvalidTransition();
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


    // Identical framed SHA-256 to resume-policy::State::commitment. All integer
    // widths here are the compact Rust widths, NOT 32-byte journal ABI words.
    function framedHash(bytes memory domain, bytes[] memory parts) private pure returns (bytes32) {
        bytes memory encoded=abi.encodePacked("SATOSHIFI_FORK_V1\x00",uint32(domain.length),domain);
        for (uint256 i; i<parts.length; ++i) encoded=bytes.concat(encoded,abi.encodePacked(uint64(parts[i].length),parts[i]));
        return sha256(encoded);
    }
    function stateCommitment(State memory s,bytes32 descriptor,bytes32 oldHead,bytes32 candidate,bytes32 lca,uint64 depth)
        internal pure returns(bytes32)
    {
        bytes[] memory parts=new bytes[](s.halted ? 4 : 0);
        if(s.halted) {
            parts[0]=abi.encodePacked(oldHead); parts[1]=abi.encodePacked(candidate);
            parts[2]=abi.encodePacked(lca); parts[3]=abi.encodePacked(depth);
        }
        bytes32 halt=framedHash(s.halted ? bytes("halt") : bytes("running"),parts);
        parts=new bytes[](12);
        parts[0]=abi.encodePacked(descriptor); parts[1]=abi.encodePacked(s.anchor); parts[2]=abi.encodePacked(s.anchorHeight);
        parts[3]=abi.encodePacked(s.knownRoot); parts[4]=abi.encodePacked(s.knownLog); parts[5]=abi.encodePacked(s.knownCount);
        parts[6]=abi.encodePacked(s.canonicalRoot); parts[7]=abi.encodePacked(s.head); parts[8]=abi.encodePacked(s.height);
        parts[9]=abi.encodePacked(s.work); parts[10]=abi.encodePacked(s.timestamp); parts[11]=abi.encodePacked(halt);
        return framedHash(bytes("resume-state-v2"),parts);
    }

    function useful(Transition memory t) internal pure returns (bool) {
        return t.afterState.work > t.beforeState.work;
    }
}
