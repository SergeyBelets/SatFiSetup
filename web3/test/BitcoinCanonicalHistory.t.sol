// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./MainnetRelayFixture.sol";
import "../contracts/src/core/BitcoinCanonicalHistory.sol";

contract CanonicalHistoryHarness {
    function verify(BitcoinCanonicalHistory.View memory v, uint64 height, bytes memory raw, bytes32[] memory siblings)
        external pure returns (bool) { return BitcoinCanonicalHistory.verify(v,height,raw,siblings); }
}

contract BitcoinCanonicalHistoryTest is MainnetFixtureBase {
    CanonicalHistoryHarness private history;
    function setUp() public {
        // Only the checkpoint, ten predecessors and three successors are used.
        // Avoid copying the entire 4,043-header fixture into test storage.
        realHeaders=slice(vm.readFileBinary("test/fixtures/mainnet/headers-32246-36288.bin"),0,14*80);
        expectedRecords=slice(vm.readFileBinary("test/fixtures/mainnet/expected-32246-36288.bin"),0,14*68);
        vm.warp(1600000000);
        history=new CanonicalHistoryHarness();
    }

    function fixture(uint256 index) internal view returns (
        BitcoinCanonicalHistory.View memory v, uint64 height, bytes memory raw, bytes32[] memory siblings, bool accepted)
    {
        string memory json=vm.readFile("test/fixtures/fork-policy-v1.json");
        string memory key=string.concat(".proofs[",vm.toString(index),"].");
        v=BitcoinCanonicalHistory.View(vm.parseJsonBytes32(json,string.concat(key,"anchor")),
            uint64(vm.parseJsonUint(json,string.concat(key,"anchor_height"))),
            vm.parseJsonBytes32(json,string.concat(key,"head")),
            uint64(vm.parseJsonUint(json,string.concat(key,"head_height"))),
            vm.parseJsonBytes32(json,string.concat(key,"canonical_root")),
            vm.parseJsonBool(json,string.concat(key,"halted")));
        height=uint64(vm.parseJsonUint(json,string.concat(key,"height")));
        raw=vm.parseJsonBytes(json,string.concat(key,"raw"));
        siblings=vm.parseJsonBytes32Array(json,string.concat(key,"siblings"));
        accepted=vm.parseJsonBool(json,string.concat(key,"accepted"));
    }

    function testSharedRustMembershipAndReorgCases() public view {
        for(uint256 i;i<9;++i) {
            (BitcoinCanonicalHistory.View memory v,uint64 h,bytes memory raw,bytes32[] memory proof,bool expected)=fixture(i);
            assertEq(history.verify(v,h,raw,proof),expected);
        }
    }

    function testChangedHeaderFieldsCannotReuseMembership() public view {
        uint256[6] memory offsets=[uint256(0),4,36,68,72,76];
        for(uint256 i;i<offsets.length;++i) {
            (BitcoinCanonicalHistory.View memory v,uint64 h,bytes memory raw,bytes32[] memory proof,)=fixture(0);
            raw[offsets[i]]^=bytes1(uint8(1)); assertFalse(history.verify(v,h,raw,proof));
        }
    }

    function testAcceptedRootAnchorHeadAndHeightAreBound() public view {
        for(uint256 i;i<6;++i) {
            (BitcoinCanonicalHistory.View memory v,uint64 h,bytes memory raw,bytes32[] memory proof,)=fixture(0);
            if(i==0) v.anchor^=bytes32(uint256(1));
            else if(i==1) v.anchorHeight+=2016;
            else if(i==2) v.head^=bytes32(uint256(1));
            else if(i==3) v.headHeight+=1;
            else if(i==4) v.canonicalRoot^=bytes32(uint256(1));
            else h-=1;
            assertFalse(history.verify(v,h,raw,proof));
        }
    }

    function testWitnessDepthOrderAndRawLengthAreExact() public view {
        (BitcoinCanonicalHistory.View memory v,uint64 h,bytes memory raw,bytes32[] memory proof,)=fixture(0);
        assertFalse(history.verify(v,h,new bytes(79),proof));
        assertFalse(history.verify(v,h,bytes.concat(raw,hex"00"),proof));
        assertFalse(history.verify(v,h,raw,new bytes32[](63)));
        assertFalse(history.verify(v,h,raw,new bytes32[](65)));
        for(uint256 i;i<32;++i) (proof[i],proof[63-i])=(proof[63-i],proof[i]);
        assertFalse(history.verify(v,h,raw,proof));
    }

    function differential(string memory name) internal {
        string memory json=vm.readFile("test/fixtures/fork-policy-v1.json");
        uint256[][] memory steps=abi.decode(vm.parseJson(json,string.concat(".traces.",name)),(uint256[][]));
        MainnetRelayHarness relay=newHarness(100);
        bytes32[] memory hashes=new bytes32[](steps.length+1); hashes[0]=relay.checkpointHash();
        for(uint256 i;i<steps.length;++i) {
            // Graph setup/comparison only; no aggregate production gas claim.
            if(i%32==0) vm.resetGasMetering();
            uint256[] memory step=steps[i]; bytes32 old=relay.getMainchainHead();
            hashes[i+1]=relay.syntheticChild(hashes[step[0]],step[1],bytes32(i+1),uint32(1300000000+i));
            assertEq(relay.getMainchainHead(),hashes[step[2]]);
            assertEq(relay.getMainchainHeight(),step[3]); assertEq(relay.halted(),step[4]==1);
            bytes32 common=relay.commonAncestor(old,hashes[i+1]); assertEq(common,hashes[step[5]]);
            (,,,uint64 oldHeight,,)=relay.blocks(old); (,,,uint64 commonHeight,,)=relay.blocks(common);
            assertEq(oldHeight-commonHeight,step[6]);
        }
    }
    function testDifferentialDepth100TieAndWinningSwitch() public { differential("depth100"); }
    function testDifferentialDepth101Halt() public { differential("depth101"); }
    function testDifferentialShorterHeavierWinner() public { differential("shorter"); }

    function testActualPublicBatchExactOverlapDoesNotAddWorkTwice() public {
        MainnetHeaderRelay relay=newRelay(100);
        bytes[] memory raw=new bytes[](3);
        raw[0]=headerAt(CHECKPOINT+1); raw[1]=raw[0]; raw[2]=headerAt(CHECKPOINT+2);
        relay.addBlockHeaders(raw);
        assertEq(relay.getMainchainHead(),wireHash(CHECKPOINT+2));
        (,,uint256 work,,,)=relay.blocks(relay.getMainchainHead()); assertEq(work,expectedWork(CHECKPOINT+2));
        relay.addBlockHeaders(raw); assertEq(relay.getMainchainHeight(),CHECKPOINT+2);
    }

    function testActualPublicBatchInvalidUnknownRollsBackPrefix() public {
        MainnetHeaderRelay relay=newRelay(100);
        bytes[] memory raw=new bytes[](2); raw[0]=headerAt(CHECKPOINT+1); raw[1]=headerAt(CHECKPOINT+3);
        vm.expectRevert(abi.encodeWithSelector(MainnetHeaderRelay.PrevNotFound.selector,wireHash(CHECKPOINT+2)));
        relay.addBlockHeaders(raw);
        assertEq(relay.getMainchainHead(),relay.checkpointHash());
        (,,,uint64 height,,)=relay.blocks(wireHash(CHECKPOINT+1)); assertEq(height,0);
    }

    function testActualPublicBatchHaltPersistsBeforeMalformedSuffix() public {
        MainnetRelayHarness relay=newHarness(100); bytes32 old=relay.checkpointHash();
        // Synthetic old ancestry; incoming candidate is an unchanged real header
        // through actual public addBlockHeaders, including its PoW/time checks.
        for(uint32 i;i<101;++i) {
            if(i%32==0) vm.resetGasMetering();
            old=relay.syntheticChild(old,1,bytes32(uint256(i+1)),1300000000+i);
        }
        bytes[] memory raw=new bytes[](3); raw[0]=headerAt(CHECKPOINT);
        raw[1]=headerAt(CHECKPOINT+1); raw[2]=hex"01";
        relay.addBlockHeaders(raw);
        assertTrue(relay.halted()); assertEq(relay.getMainchainHead(),old);
        (,,,uint64 height,,)=relay.blocks(wireHash(CHECKPOINT+1)); assertEq(height,CHECKPOINT+1);
        (bool canonical,uint64 confirmations)=relay.getBlockStatus(old);
        assertFalse(canonical); assertEq(confirmations,0);
    }
}
