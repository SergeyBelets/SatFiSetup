// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "satoshifi-core/BitcoinTransitionV2.sol";
import "risc0-verifier/groth16/RiscZeroGroth16Verifier.sol";

/// TEST ONLY: constructor accepts a synthetic native-model state, not a checkpoint.
/// This is never a production state, Bitcoin verifier, or monetary adapter.
contract TestPolicyReceiver {
    bytes32 public constant TEST_DOMAIN=bytes32("SATOSHIFI_TEST_POLICY_V1");
    bytes32 public constant TEST_NETWORK=bytes32("synthetic-no-pow");
    bytes32 public immutable programImageId;
    bytes32 public immutable checkpointDescriptor;
    RiscZeroGroth16Verifier public immutable verifier;
    BitcoinTransitionV2.State private accepted;
    uint64 public stateVersion;
    uint256 public lastUsefulAcceptedAt;
    bool public hasUsefulUpdate;
    uint64 public haltDepth;
    uint64 public haltPosition;
    error TestDomainRequired();

    constructor(bytes32 testImage,bytes32 descriptor,BitcoinTransitionV2.State memory syntheticInitial) {
        require(testImage!=bytes32(0) && !syntheticInitial.halted);
        programImageId=testImage;checkpointDescriptor=descriptor;accepted=syntheticInitial;
        verifier=new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT,ControlID.BN254_CONTROL_ID);
    }
    function getState() external view returns(BitcoinTransitionV2.State memory){return accepted;}
    function isFresh() external view returns(bool){return !accepted.halted && hasUsefulUpdate && block.timestamp<=lastUsefulAcceptedAt+3600;}
    function submit(bytes calldata modelJournal,bytes calldata seal) external {
        if(modelJournal.length!=1216 || seal.length!=260) revert TestDomainRequired();
        bytes32 domain;bytes32 network;
        assembly ("memory-safe") {domain:=calldataload(modelJournal.offset) network:=calldataload(add(modelJournal.offset,32))}
        if(domain!=TEST_DOMAIN || network!=TEST_NETWORK) revert TestDomainRequired();
        verifier.verify(seal,programImageId,sha256(modelJournal));
        // Explicit test-only adaptation AFTER authenticating the exact model bytes.
        // The production decoder rejects these model domains without this wrapper.
        bytes memory journal=modelJournal;
        bytes32 productionDomain=bytes32("SATOSHIFI_TRANSITION_V2");bytes32 productionNetwork=bytes32("bitcoin-mainnet");
        assembly ("memory-safe") {mstore(add(journal,32),productionDomain) mstore(add(journal,64),productionNetwork)}
        BitcoinTransitionV2.Transition memory t=BitcoinTransitionV2.validate(journal,checkpointDescriptor,accepted);
        accepted=t.afterState;stateVersion++;
        if(BitcoinTransitionV2.useful(t)){hasUsefulUpdate=true;lastUsefulAcceptedAt=block.timestamp;}
        if(accepted.halted){haltDepth=t.haltDepth;haltPosition=t.haltPosition;}
    }
    function productionDecoderRejects(bytes calldata modelJournal) external pure returns(bool){
        // Called by the runner expecting revert for every model journal.
        BitcoinTransitionV2.decode(modelJournal);return false;
    }
}
