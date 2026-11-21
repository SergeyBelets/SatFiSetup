// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./MainnetRelayFixture.sol";
import "../contracts/src/core/BitcoinCheckpoint.sol";

contract CheckpointHarness {
    function validate(bytes memory raw, uint64 height, uint256 work, bytes[] memory prior)
        external pure returns (BitcoinCheckpoint.Context memory, bytes32, bytes32)
    {
        (BitcoinCheckpoint.Context memory c, bytes32 descriptor) = BitcoinCheckpoint.validate(raw,height,work,prior);
        return (c, descriptor, BitcoinCheckpoint.contextCommitment(c));
    }
}

contract BitcoinCheckpointTest is MainnetFixtureBase {
    CheckpointHarness private factory;
    function setUp() public {
        realHeaders=slice(vm.readFileBinary("test/fixtures/mainnet/headers-32246-36288.bin"),0,12*80);
        expectedRecords=slice(vm.readFileBinary("test/fixtures/mainnet/expected-32246-36288.bin"),0,12*68);
        factory=new CheckpointHarness(); vm.warp(1600000000);
    }
    function testSharedRustContextsAndActualRelayConstructors() public {
        string memory json=vm.readFile("test/fixtures/checkpoint-policy-v1.json");
        for(uint256 i;i<3;++i) {
            string memory key=string.concat(".checkpoints[",vm.toString(i),"].");
            bytes memory raw=vm.parseJsonBytes(json,string.concat(key,"raw"));
            uint64 height=uint64(vm.parseJsonUint(json,string.concat(key,"height")));
            uint256 work=uint256(vm.parseJsonBytes32(json,string.concat(key,"work")));
            bytes[] memory prior=new bytes[](10);
            for(uint256 j;j<10;++j) prior[j]=vm.parseJsonBytes(json,string.concat(key,"prior[",vm.toString(j),"]"));
            (BitcoinCheckpoint.Context memory c,bytes32 descriptor,bytes32 contextHash)=factory.validate(raw,height,work,prior);
            assertEq(c.blockHash,vm.parseJsonBytes32(json,string.concat(key,"hash")));
            assertEq(c.height,height); assertEq(c.totalWork,work);
            assertEq(c.currentTargetBits,vm.parseJsonUint(json,string.concat(key,"bits")));
            assertEq(c.epochStartTime,vm.parseJsonUint(json,string.concat(key,"epoch_time")));
            uint256[] memory times=vm.parseJsonUintArray(json,string.concat(key,"timestamps"));
            for(uint256 j;j<11;++j) assertEq(c.timestamps[j],times[j]);
            assertEq(descriptor,vm.parseJsonBytes32(json,string.concat(key,"descriptor_commitment")));
            assertEq(contextHash,vm.parseJsonBytes32(json,string.concat(key,"context_commitment")));
            MainnetHeaderRelay relay=new MainnetHeaderRelay(raw,height,work,prior,100);
            assertEq(relay.getMainchainHead(),c.blockHash); assertEq(relay.getMainchainHeight(),height);
            (,,uint256 actualWork,,,)=relay.blocks(c.blockHash); assertEq(actualWork,work);
        }
    }

    function rejected(bytes memory raw,uint64 height,uint256 work,bytes[] memory prior) internal {
        (bool accepted,)=address(factory).call(abi.encodeCall(factory.validate,(raw,height,work,prior)));
        assertFalse(accepted,"checkpoint accepted invalid input");
        bool relayAccepted;
        try new MainnetHeaderRelay(raw,height,work,prior,100) returns(MainnetHeaderRelay) { relayAccepted=true; } catch {}
        assertFalse(relayAccepted,"relay differs from checkpoint rejection");
    }
    function testRejectsShapeAndHeightLikeRelay() public {
        rejected(new bytes(79),CHECKPOINT,expectedWork(CHECKPOINT),checkpointAncestors());
        rejected(bytes.concat(headerAt(CHECKPOINT),hex"00"),CHECKPOINT,expectedWork(CHECKPOINT),checkpointAncestors());
        rejected(headerAt(CHECKPOINT),0,expectedWork(CHECKPOINT),checkpointAncestors());
        rejected(headerAt(CHECKPOINT),CHECKPOINT+1,expectedWork(CHECKPOINT),checkpointAncestors());
        rejected(headerAt(CHECKPOINT),CHECKPOINT,expectedWork(CHECKPOINT),new bytes[](9));
        rejected(headerAt(CHECKPOINT),CHECKPOINT,expectedWork(CHECKPOINT),new bytes[](11));
    }
    function testRejectsEveryBrokenAncestorLikeRelay() public {
        for(uint256 i;i<10;++i) {
            bytes[] memory prior=checkpointAncestors(); prior[i][36]^=bytes1(uint8(1));
            rejected(headerAt(CHECKPOINT),CHECKPOINT,expectedWork(CHECKPOINT),prior);
        }
    }
    function testRejectsEveryAncestorLengthLikeRelay() public {
        for(uint256 i;i<10;++i) {
            bytes[] memory prior=checkpointAncestors(); prior[i]=new bytes(79);
            rejected(headerAt(CHECKPOINT),CHECKPOINT,expectedWork(CHECKPOINT),prior);
        }
    }
    function testRejectsInvalidTargetAndPowLikeRelay() public {
        uint32[8] memory values=[uint32(0),0x01003456,0x1d80ffff,0x23000001,0x22000100,0x21010000,0x1d010000,0x207fffff];
        for(uint256 i;i<values.length;++i) {
            bytes memory raw=headerAt(CHECKPOINT); write32(raw,72,values[i]);
            rejected(raw,CHECKPOINT,expectedWork(CHECKPOINT),checkpointAncestors());
        }
        bytes memory invalidPow=headerAt(CHECKPOINT); invalidPow[76]^=bytes1(uint8(1));
        rejected(invalidPow,CHECKPOINT,expectedWork(CHECKPOINT),checkpointAncestors());
    }
    function testWorkIncludesAnchorExactlyOnce() public {
        uint256 own=TargetsHelper.workFromTarget(TargetsHelper.bitsToTarget(expectedBits(CHECKPOINT)));
        rejected(headerAt(CHECKPOINT),CHECKPOINT,own-1,checkpointAncestors());
        for(uint256 i;i<3;++i) {
            uint256 work=i==0 ? own : i==1 ? expectedWork(CHECKPOINT) : type(uint256).max;
            (BitcoinCheckpoint.Context memory c,,)=factory.validate(headerAt(CHECKPOINT),CHECKPOINT,work,checkpointAncestors());
            assertEq(c.totalWork,work);
        }
    }
    function testPredecessorsDoNotEnterRelayCanonicalHistory() public {
        MainnetHeaderRelay relay=newRelay(100);
        for(uint64 h=FIRST;h<CHECKPOINT;++h) {
            (bool canonical,uint64 count)=relay.getBlockStatus(wireHash(h));
            assertFalse(canonical); assertEq(count,0);
        }
        bytes[] memory next=new bytes[](1); next[0]=headerAt(CHECKPOINT+1); relay.addBlockHeaders(next);
        (,,uint256 work,,,)=relay.blocks(wireHash(CHECKPOINT+1)); assertEq(work,expectedWork(CHECKPOINT+1));
    }
    function testDescriptorBindsTrustedConfiguration() public view {
        (,bytes32 original,bytes32 originalContext)=factory.validate(headerAt(CHECKPOINT),CHECKPOINT,expectedWork(CHECKPOINT),checkpointAncestors());
        (,bytes32 changedHeight,bytes32 heightContext)=factory.validate(headerAt(CHECKPOINT),CHECKPOINT+2016,expectedWork(CHECKPOINT),checkpointAncestors());
        (,bytes32 changedWork,bytes32 workContext)=factory.validate(headerAt(CHECKPOINT),CHECKPOINT,expectedWork(CHECKPOINT)+1,checkpointAncestors());
        assertTrue(original!=changedHeight && original!=changedWork);
        assertTrue(originalContext!=heightContext && originalContext!=workContext);
    }
}
