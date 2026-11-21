// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/core/BitcoinTransition.sol";

// Test-only, intentionally unauthenticated library caller. Never a proof verifier.
contract TransitionHarness {
    function decode(bytes memory journal) external pure returns (BitcoinTransition.Transition memory) {
        return BitcoinTransition.decode(journal);
    }
    function validate(bytes memory journal, bytes32 descriptor, BitcoinTransition.State memory current)
        external view returns (BitcoinTransition.Transition memory, bool)
    {
        BitcoinTransition.Transition memory t=BitcoinTransition.validate(journal,descriptor,current);
        return (t,BitcoinTransition.useful(t));
    }
}

contract BitcoinTransitionTest is Test {
    TransitionHarness private harness;
    function setUp() public {
        harness=new TransitionHarness(); vm.chainId(31337); vm.warp(1400000000);
    }
    function fixture(uint256 index) internal view returns (bytes memory) {
        string memory json=vm.readFile("test/fixtures/transition-policy-v1.json");
        return vm.parseJsonBytes(json,string.concat(".cases[",vm.toString(index),"].journal"));
    }
    function writeWord(bytes memory journal,uint256 index,bytes32 value) internal pure {
        assembly ("memory-safe") { mstore(add(add(journal,32),mul(index,32)),value) }
    }
    function localFixture(uint256 index) internal view returns(bytes memory journal) {
        journal=fixture(index);
        // Test fixture transport only. A real proof binds the original bytes and
        // MUST be regenerated for another accepting deployment.
        writeWord(journal,3,bytes32(uint256(uint160(address(harness)))));
    }
    function checked(bytes memory journal) internal view returns(BitcoinTransition.Transition memory t,bool useful) {
        BitcoinTransition.Transition memory input=harness.decode(journal);
        return harness.validate(journal,input.checkpointDescriptor,input.beforeState);
    }
    function testSharedRustJournalWordsAndAllSixTransitions() public view {
        string memory json=vm.readFile("test/fixtures/transition-policy-v1.json");
        for(uint256 i;i<6;++i) {
            (BitcoinTransition.Transition memory t,bool useful)=checked(localFixture(i));
            string memory key=string.concat(".cases[",vm.toString(i),"].");
            assertEq(useful,vm.parseJsonBool(json,string.concat(key,"useful")));
            assertEq(t.afterState.halted,vm.parseJsonBool(json,string.concat(key,"halted")));
            assertEq(t.processed,vm.parseJsonUint(json,string.concat(key,"processed")));
            assertEq(t.timeSummary.headerCount,vm.parseJsonUint(json,string.concat(key,"new_headers")));
            assertEq(t.timeSummary.maximumTimestamp,vm.parseJsonUint(json,string.concat(key,"maximum")));
            assertEq(t.afterState.timestamp,vm.parseJsonUint(json,string.concat(key,"tip_time")));
            assertEq(t.haltPosition,vm.parseJsonUint(json,string.concat(key,"halt_position")));
        }
    }
    function testExactLengthDomainAndNarrowIntegerEncoding() public {
        vm.expectRevert(); harness.decode(new bytes(1023));
        vm.expectRevert(); harness.decode(bytes.concat(fixture(0),hex"00"));
        uint256[15] memory indices=[uint256(0),1,3,6,12,14,16,18,20,21,23,24,25,26,27];
        for(uint256 i;i<indices.length;++i) {
            bytes memory journal=fixture(0); writeWord(journal,indices[i],bytes32(type(uint256).max));
            vm.expectRevert(); harness.decode(journal);
        }
        bytes memory journal=fixture(0); writeWord(journal,31,bytes32(type(uint256).max));
        vm.expectRevert(); harness.decode(journal);
        journal=fixture(0); writeWord(journal,22,bytes32(0)); vm.expectRevert(); harness.decode(journal);
    }
    function testChainAddressAndCheckpointAreRequired() public {
        bytes memory journal=localFixture(0); BitcoinTransition.Transition memory t=harness.decode(journal);
        vm.chainId(1); vm.expectRevert(BitcoinTransition.WrongDeployment.selector);
        harness.validate(journal,t.checkpointDescriptor,t.beforeState); vm.chainId(31337);
        writeWord(journal,3,bytes32(uint256(uint160(address(0x1234)))));
        vm.expectRevert(BitcoinTransition.WrongDeployment.selector); harness.validate(journal,t.checkpointDescriptor,t.beforeState);
        journal=localFixture(0); vm.expectRevert(BitcoinTransition.WrongDeployment.selector);
        harness.validate(journal,bytes32(uint256(t.checkpointDescriptor)^1),t.beforeState);
    }
    function testEveryAcceptedStateFieldAndReplayAreChecked() public {
        bytes memory journal=localFixture(0); BitcoinTransition.Transition memory t=harness.decode(journal);
        for(uint256 i;i<8;++i) {
            BitcoinTransition.State memory current=harness.decode(journal).beforeState;
            if(i==0) current.anchor^=bytes32(uint256(1)); else if(i==1) current.anchorHeight+=2016;
            else if(i==2) current.head^=bytes32(uint256(1)); else if(i==3) current.height+=1;
            else if(i==4) current.work+=1; else if(i==5) current.timestamp^=1;
            else if(i==6) current.canonicalRoot^=bytes32(uint256(1)); else current.commitment^=bytes32(uint256(1));
            vm.expectRevert(BitcoinTransition.StaleState.selector); harness.validate(journal,t.checkpointDescriptor,current);
        }
        vm.expectRevert(BitcoinTransition.StaleState.selector); harness.validate(journal,t.checkpointDescriptor,t.afterState);
        t.afterState.halted=true; vm.expectRevert(BitcoinTransition.AlreadyHalted.selector);
        harness.validate(journal,t.checkpointDescriptor,t.afterState);
    }
    function testMaximumOfLosingBranchControlsFutureTimeNotTip() public {
        bytes memory journal=localFixture(2); BitcoinTransition.Transition memory t=harness.decode(journal);
        assertGt(t.timeSummary.maximumTimestamp,t.afterState.timestamp);
        vm.warp(uint256(t.timeSummary.maximumTimestamp)-7200); checked(journal);
        vm.warp(uint256(t.timeSummary.maximumTimestamp)-7201); vm.expectRevert();
        harness.validate(journal,t.checkpointDescriptor,t.beforeState);
        vm.warp(type(uint256).max); checked(journal);
    }
    function testHaltPersistsSafePrefixPositionAndCandidateTime() public {
        bytes memory journal=localFixture(5); BitcoinTransition.Transition memory t=harness.decode(journal);
        assertEq(t.afterState.height,t.beforeState.height+1); assertEq(t.haltPosition,2);
        assertEq(t.submitted,3); assertEq(t.processed,2); assertEq(t.haltDepth,101);
        assertEq(t.haltOldHead,t.afterState.head); assertGt(t.timeSummary.maximumTimestamp,t.afterState.timestamp);
        uint256[6] memory indices=[uint256(21),26,27,28,29,31];
        for(uint256 i;i<indices.length;++i) {
            bytes memory changed=bytes.concat(journal); writeWord(changed,indices[i],bytes32(0));
            // A zero candidate hash is not intrinsically invalid; use the safe head instead.
            if(indices[i]==29) writeWord(changed,29,t.afterState.head);
            vm.expectRevert(); harness.decode(changed);
        }
    }
    function testZeroNewHeadersAndInconsistentProgressAreRejected() public {
        uint256[5] memory indices=[uint256(20),21,23,8,17];
        BitcoinTransition.Transition memory t=harness.decode(fixture(0));
        for(uint256 i;i<indices.length;++i) {
            bytes memory journal=fixture(0);
            writeWord(journal,indices[i],indices[i]==8 ? t.beforeState.commitment : bytes32(0));
            vm.expectRevert(); harness.decode(journal);
        }
        bytes memory journal=fixture(0); writeWord(journal,20,bytes32(uint256(65))); vm.expectRevert(); harness.decode(journal);
        journal=fixture(0); writeWord(journal,23,bytes32(uint256(2))); vm.expectRevert(); harness.decode(journal);
        journal=fixture(0); writeWord(journal,27,bytes32(uint256(1))); vm.expectRevert(); harness.decode(journal);
    }
    function testSideBranchDoesNotBecomeUsefulAndShorterHeavierDoes() public view {
        (BitcoinTransition.Transition memory side,bool sideUseful)=checked(localFixture(2));
        assertFalse(sideUseful); assertEq(side.afterState.head,side.beforeState.head);
        assertEq(side.afterState.canonicalRoot,side.beforeState.canonicalRoot);
        assertTrue(side.afterState.commitment!=side.beforeState.commitment);
        (BitcoinTransition.Transition memory shorter,bool shorterUseful)=checked(localFixture(3));
        assertTrue(shorterUseful); assertLt(shorter.afterState.height,shorter.beforeState.height);
    }
}
