// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinCheckpointV2.sol";
import "./ZkSpvAdapter.sol";
import "risc0-verifier/groth16/RiscZeroGroth16Verifier.sol";

/// @notice Experimental immutable V2 state, explicit proof-only relay profile.
/// @dev Deploys the pinned real verifier itself; there is no mock/dev verifier,
/// upgrade/admin path, arbitrary initial root, checkpoint reset or FROST fallback.
/// Actual guest image and target policy still require qualification before use.
contract ZkBitcoinStateV2 {
    bytes32 public constant RELAY_PROFILE=bytes32("PROOF_ONLY_V1");
    bytes32 public constant JOURNAL_SCHEMA=bytes32("SATOSHIFI_TRANSITION_V2");
    uint256 public constant GROTH16_SEAL_BYTES=260;
    bytes32 public immutable confederationId;
    bytes32 public immutable programImageId;
    bytes32 public immutable checkpointDescriptor;
    uint64 public immutable maxStateAge;
    uint64 public immutable maxTipLag;
    uint64 public immutable baselineMaxTipAge;
    RiscZeroGroth16Verifier public immutable verifier;
    bytes32 public immutable verifierCodeHash;
    ZkSpvAdapter public immutable adapter;
    BitcoinTransitionV2.State private accepted;
    uint64 public stateVersion;
    uint256 public lastUsefulAcceptedAt;
    bool public hasUsefulUpdate;
    struct Halt {bytes32 oldHead;bytes32 candidate;bytes32 lca;uint64 depth;uint64 position;}
    Halt public haltRecord;
    struct Config {
        bytes32 confederationId;bytes32 programImageId;bytes32 relayProfile;
        uint64 maxStateAge;uint64 maxTipLag;uint64 baselineMaxTipAge;
    }
    error InvalidConfiguration();
    error InvalidProofEncoding();
    error VerifierCodeChanged();
    event StateAccepted(uint64 indexed version,bytes32 indexed commitment,bytes32 indexed journalDigest,
        bytes32 head,uint64 height,uint256 work,bool useful,bool halted);
    event PermanentHalt(bytes32 indexed safeHead,bytes32 indexed candidate,bytes32 lca,uint64 depth,uint64 position);

    constructor(Config memory config,bytes memory raw,uint64 height,uint256 work,bytes[] memory prior) {
        if(config.confederationId==bytes32(0) || config.programImageId==bytes32(0) || config.relayProfile!=RELAY_PROFILE
            || config.maxStateAge==0 || config.maxTipLag==0 || config.baselineMaxTipAge==0
            || config.baselineMaxTipAge>1 days || config.maxTipLag>config.baselineMaxTipAge) revert InvalidConfiguration();
        confederationId=config.confederationId;programImageId=config.programImageId;
        maxStateAge=config.maxStateAge;maxTipLag=config.maxTipLag;baselineMaxTipAge=config.baselineMaxTipAge;
        (BitcoinTransitionV2.State memory initial,bytes32 descriptor)=BitcoinCheckpointV2.initialize(raw,height,work,prior);
        accepted=initial;checkpointDescriptor=descriptor;
        RiscZeroGroth16Verifier deployed=new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT,ControlID.BN254_CONTROL_ID);
        if(deployed.SELECTOR()!=bytes4(0xe71e6620)) revert InvalidConfiguration();
        verifier=deployed;verifierCodeHash=address(deployed).codehash;
        adapter=new ZkSpvAdapter(address(this));
        // Checkpoint initialization is not useful proof progress. Fresh stays
        // false until a verified transition strictly increases canonical work.
    }
    function getState() external view returns(BitcoinTransitionV2.State memory) {return accepted;}
    function getMainchainHead() external view returns(bytes32) {return accepted.head;}
    function getMainchainHeight() external view returns(uint64) {return accepted.height;}
    function halted() external view returns(bool) {return accepted.halted;}

    function isFresh() public view returns(bool) {
        return !accepted.halted && hasUsefulUpdate && withinAge(lastUsefulAcceptedAt,maxStateAge)
            && withinAge(uint256(accepted.timestamp),maxTipLag);
    }
    function freshness() external view returns(bool fresh,uint256 evmNow,uint256 usefulAt,uint32 canonicalTipTime,
        uint64 stateAgeLimit,uint64 tipLagLimit,bool hasProgress,bool isHalted)
    {
        return(isFresh(),block.timestamp,lastUsefulAcceptedAt,accepted.timestamp,maxStateAge,maxTipLag,hasUsefulUpdate,accepted.halted);
    }
    function withinAge(uint256 timestamp,uint64 limit) private view returns(bool) {
        return block.timestamp<=timestamp || block.timestamp-timestamp<=limit;
    }

    /// State update is a separate transaction from materialization/money. The
    /// supported caller must retain a successful halt receipt before any money
    /// call, so a later monetary revert cannot roll the halt back (§6.7).
    function submitTransition(bytes calldata journal,bytes calldata seal) external {
        if(journal.length!=1216 || seal.length!=GROTH16_SEAL_BYTES) revert InvalidProofEncoding();
        if(address(verifier).codehash!=verifierCodeHash) revert VerifierCodeChanged();
        // Exact original bytes, immutable image, real pinned unconditional verifier.
        // No state writes or caller-defined callbacks precede successful validation.
        bytes32 journalDigest=sha256(journal);
        verifier.verify(seal,programImageId,journalDigest);
        BitcoinTransitionV2.Transition memory t=BitcoinTransitionV2.validate(journal,checkpointDescriptor,accepted);
        bool useful=BitcoinTransitionV2.useful(t);
        accepted=t.afterState;stateVersion+=1;
        if(useful) {hasUsefulUpdate=true;lastUsefulAcceptedAt=block.timestamp;}
        if(accepted.halted) {
            haltRecord=Halt(t.haltOldHead,t.haltCandidate,t.haltLca,t.haltDepth,t.haltPosition);
            emit PermanentHalt(t.haltOldHead,t.haltCandidate,t.haltLca,t.haltDepth,t.haltPosition);
        }
        emit StateAccepted(stateVersion,accepted.commitment,journalDigest,accepted.head,accepted.height,accepted.work,useful,accepted.halted);
    }
}
