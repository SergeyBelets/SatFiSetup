// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/ZkBitcoinStateV2.sol";

// Exact current IPaymentSPV ABI. Source/compiled ABI parity is also checked by
// qualification. No existing monetary contracts are switched to this component.
interface PaymentSPVAbi {
    function checkTxInclusion(bytes32,bytes32,bytes32[] calldata,uint256) external view returns(bool);
    function getBlockStatus(bytes32) external view returns(bool,uint64);
    function testHelpersEnabled() external view returns(bool);
}
contract PaymentBoundaryConsumer {
    function requirePayment(address adapter,bytes32 hash,bytes32 txid,bytes32[] calldata path,uint256 index) external view {
        PaymentSPVAbi s=PaymentSPVAbi(adapter);(bool canonical,uint64 count)=s.getBlockStatus(hash);
        require(!s.testHelpersEnabled() && canonical && count>=1 && s.checkTxInclusion(hash,txid,path,index),"unavailable payment");
    }
}
abstract contract FixtureBase is Test {
    ZkBitcoinStateV2 internal state;
    ZkSpvAdapter internal adapter;
    function cpFixture(uint256 i) internal view returns(string memory) {return vm.readFile(string.concat("fixtures/checkpoints-",vm.toString(i),".json"));}
    function step(uint256 i) internal view returns(string memory) {return vm.readFile(string.concat("fixtures/steps-",vm.toString(i),".json"));}
    function config() internal pure returns(ZkBitcoinStateV2.Config memory) {
        return ZkBitcoinStateV2.Config(bytes32(uint256(7)),bytes32(uint256(123)),bytes32("PROOF_ONLY_V1"),3600,7200,7200);
    }
    function cpKey(uint256 i,string memory key) internal pure returns(string memory) {
        return string.concat(".",key);
    }
    function deploy(ZkBitcoinStateV2.Config memory cfg,uint256 i) internal returns(ZkBitcoinStateV2) {
        return new ZkBitcoinStateV2(cfg,vm.parseJsonBytes(cpFixture(i),cpKey(i,"raw")),uint64(vm.parseJsonUint(cpFixture(i),cpKey(i,"height"))),
            uint256(vm.parseJsonBytes32(cpFixture(i),cpKey(i,"work"))),vm.parseJsonBytesArray(cpFixture(i),cpKey(i,"prior")));
    }
    function setUp() public virtual {
        vm.chainId(31337);vm.warp(1300000000);
        state=deploy(config(),0);adapter=state.adapter();
    }
    function key(uint256 i,string memory field) internal pure returns(string memory) {
        return string.concat(".",field);
    }
    function put(bytes memory j,uint256 i,bytes32 value) internal pure {
        assembly ("memory-safe") {mstore(add(add(j,32),mul(i,32)),value)}
    }
    function journal(uint256 i) internal view returns(bytes memory j) {
        j=vm.parseJsonBytes(step(i),key(i,"journal"));put(j,3,bytes32(uint256(uint160(address(state)))));
    }
    function seal() internal view returns(bytes memory) {return bytes.concat(state.verifier().SELECTOR(),new bytes(256));}
    // ONLY positive state-machine fixtures use Foundry's scoped mockCall. There
    // is no injected fake verifier contract, dev option, or real proof claim.
    function acceptBytes(bytes memory j) internal {
        bytes memory proof=seal();bytes memory callData=abi.encodeCall(IRiscZeroVerifier.verify,(proof,state.programImageId(),sha256(j)));
        vm.expectCall(address(state.verifier()),callData);
        vm.mockCall(address(state.verifier()),callData,bytes(""));
        state.submitTransition(j,proof);vm.clearMockedCalls();
    }
    function accept(uint256 i) internal {acceptBytes(journal(i));}
    function rejectBytes(bytes memory j) internal {
        bytes32 old=state.getState().commitment;uint64 version=state.stateVersion();uint256 usefulAt=state.lastUsefulAcceptedAt();
        bytes memory proof=seal();
        vm.mockCall(address(state.verifier()),abi.encodeCall(IRiscZeroVerifier.verify,(proof,state.programImageId(),sha256(j))),bytes(""));
        vm.expectRevert();state.submitTransition(j,proof);vm.clearMockedCalls();
        assertEq(state.getState().commitment,old);assertEq(state.stateVersion(),version);assertEq(state.lastUsefulAcceptedAt(),usefulAt);
    }
    function materialize(uint256 i,string memory name) internal returns(bytes32) {
        string memory prefix=key(i,string.concat(name,"."));
        return adapter.materializeHeader(uint64(vm.parseJsonUint(step(i),string.concat(prefix,"height"))),
            vm.parseJsonBytes(step(i),string.concat(prefix,"raw")),vm.parseJsonBytes32Array(step(i),string.concat(prefix,"siblings")));
    }
    function status(bytes32 h,bool expected,uint64 count) internal view {
        (bool ok,uint64 confirmations)=adapter.getBlockStatus(h);assertEq(ok,expected);assertEq(confirmations,count);
    }
}

contract ZkStateV2Test is FixtureBase {
    function testAllThreeRealCheckpointsMatchRustInitialization() public {
        for(uint256 i;i<3;++i) {
            ZkBitcoinStateV2 s=i==0?state:deploy(config(),i);BitcoinTransitionV2.State memory view_=s.getState();
            assertEq(view_.commitment,vm.parseJsonBytes32(cpFixture(i),cpKey(i,"commitment")));
            assertEq(view_.canonicalRoot,vm.parseJsonBytes32(cpFixture(i),cpKey(i,"canonicalRoot")));
            assertEq(view_.knownRoot,vm.parseJsonBytes32(cpFixture(i),cpKey(i,"knownRoot")));
            assertEq(view_.knownLog,vm.parseJsonBytes32(cpFixture(i),cpKey(i,"knownLog")));
            assertEq(s.checkpointDescriptor(),vm.parseJsonBytes32(cpFixture(i),cpKey(i,"descriptor")));
            assertEq(view_.knownCount,1);assertEq(s.stateVersion(),0);assertFalse(s.isFresh());assertFalse(s.hasUsefulUpdate());
        }
    }
    function testInvalidConfigurationAndUnsupportedRelayProfile() public {
        for(uint256 i;i<7;++i) {
            ZkBitcoinStateV2.Config memory cfg=config();
            if(i==0) cfg.programImageId=0;else if(i==1) cfg.confederationId=0;else if(i==2) cfg.relayProfile=bytes32("FROST");
            else if(i==3) cfg.maxStateAge=0;else if(i==4) cfg.maxTipLag=0;else if(i==5) cfg.maxTipLag=7201;else cfg.baselineMaxTipAge=86401;
            vm.expectRevert(ZkBitcoinStateV2.InvalidConfiguration.selector);deploy(cfg,0);
        }
    }
    function testInvalidCheckpointCannotSeedArbitraryState() public {
        bytes memory raw=vm.parseJsonBytes(cpFixture(0),cpKey(0,"raw"));raw[76]^=bytes1(uint8(1));
        bytes[] memory prior=vm.parseJsonBytesArray(cpFixture(0),cpKey(0,"prior"));
        uint256 work=uint256(vm.parseJsonBytes32(cpFixture(0),cpKey(0,"work")));ZkBitcoinStateV2.Config memory cfg=config();
        vm.expectRevert();new ZkBitcoinStateV2(cfg,raw,32256,work,prior);
    }
    function testPinnedRealVerifierRejectsFakeProofAndPreservesState() public {
        assertEq(state.verifier().BN254_CONTROL_ID(),ControlID.BN254_CONTROL_ID);
        assertEq(state.verifierCodeHash(),address(state.verifier()).codehash);
        assertEq(address(adapter.state()),address(state));
        bytes memory j=journal(0);bytes memory fake=seal();bytes32 old=state.getState().commitment;
        vm.expectRevert();state.submitTransition(j,fake);assertEq(state.getState().commitment,old);assertEq(state.stateVersion(),0);
        assertFalse(state.hasUsefulUpdate());
    }
    function testWrongSelectorDevAndTrailingSealAreRejectedByRealPath() public {
        bytes memory j=journal(0);
        assertEq(state.verifier().SELECTOR(),bytes4(0xe71e6620));
        vm.expectRevert();state.submitTransition(j,bytes.concat(bytes4(0x73c457ba),new bytes(256)));
        assertEq(state.stateVersion(),0);
        vm.expectRevert(ZkBitcoinStateV2.InvalidProofEncoding.selector);state.submitTransition(j,hex"00000000");
        vm.expectRevert();state.submitTransition(j,new bytes(260));
        bytes memory longSeal=bytes.concat(seal(),hex"00");
        vm.expectRevert(ZkBitcoinStateV2.InvalidProofEncoding.selector);state.submitTransition(j,longSeal);
    }
    function testProofIsBoundToExactJournalAndFixedImage() public {
        bytes memory j=journal(0);bytes memory proof=seal();
        vm.mockCall(address(state.verifier()),abi.encodeCall(IRiscZeroVerifier.verify,(proof,state.programImageId(),sha256(j))),bytes(""));
        put(j,2,bytes32(uint256(1)));vm.expectRevert();state.submitTransition(j,proof);vm.clearMockedCalls();
        assertEq(state.stateVersion(),0);
        j=journal(0);vm.mockCall(address(state.verifier()),abi.encodeCall(IRiscZeroVerifier.verify,(proof,bytes32(uint256(999)),sha256(j))),bytes(""));
        vm.expectRevert();state.submitTransition(j,proof);vm.clearMockedCalls();assertEq(state.stateVersion(),0);
    }
    function testWrongDeploymentStateAndFutureMaximumRemainAtomicAfterVerifierReturn() public {
        uint256[7] memory fields=[uint256(2),3,4,7,32,34,24];
        for(uint256 i;i<fields.length;++i) {bytes memory j=journal(0);put(j,fields[i],bytes32(type(uint256).max));rejectBytes(j);}
        bytes memory j=journal(0);put(j,24,bytes32(uint256(block.timestamp+7201)));rejectBytes(j);
        vm.warp(1300000000-7201);rejectBytes(journal(0));
    }
    function testReplayIsAtomicAndDoesNotRefresh() public {
        accept(0);uint256 at=state.lastUsefulAcceptedAt();vm.warp(block.timestamp+1);rejectBytes(journal(0));
        assertEq(state.stateVersion(),1);assertEq(state.lastUsefulAcceptedAt(),at);
    }
    function testSideBranchAdvancesVersionButNeverUsefulFreshness() public {
        accept(0);bytes32 root=state.getState().canonicalRoot;uint256 at=state.lastUsefulAcceptedAt();
        vm.warp(block.timestamp+100);accept(1);
        assertEq(state.stateVersion(),2);assertEq(state.lastUsefulAcceptedAt(),at);assertEq(state.getState().canonicalRoot,root);
    }
    function testFreshStateAgeBoundaryInclusiveAndOverflowSafe() public {
        accept(0);vm.warp(1300003600);assertTrue(state.isFresh());vm.warp(1300003601);assertFalse(state.isFresh());
        assertFalse(state.halted());vm.warp(type(uint256).max);assertFalse(state.isFresh());
    }
    function testFreshTipAgeBoundaryInclusiveAndNonmonotonicTimestamp() public {
        ZkBitcoinStateV2.Config memory cfg=config();cfg.maxStateAge=10000;cfg.maxTipLag=100;
        state=deploy(cfg,0);adapter=state.adapter();
        bytes memory j=vm.parseJsonBytes(vm.readFile("fixtures/nonmonotonic-0.json"),".journal");put(j,3,bytes32(uint256(uint160(address(state)))));acceptBytes(j);
        assertEq(state.getState().timestamp,1299999990);assertTrue(state.isFresh());
        vm.warp(1300000090);assertTrue(state.isFresh());vm.warp(1300000091);assertFalse(state.isFresh());
    }
    function testOldTipCatchupDoesNotManufactureFreshness() public {
        bytes memory j=vm.parseJsonBytes(vm.readFile("fixtures/old_tip-0.json"),".journal");put(j,3,bytes32(uint256(uint160(address(state)))));acceptBytes(j);
        assertTrue(state.hasUsefulUpdate());assertEq(state.lastUsefulAcceptedAt(),block.timestamp);assertFalse(state.isFresh());
    }
    function testFutureMaximumBoundaryUsesAcceptingEvmClock() public {
        vm.warp(1300000000-7200);accept(0);assertTrue(state.isFresh());assertEq(state.getState().timestamp,1300000000);
    }
    function testChangedVerifierRuntimeFailsClosed() public {
        bytes memory j=journal(0);bytes memory proof=seal();vm.etch(address(state.verifier()),hex"60006000f3");
        vm.expectRevert(ZkBitcoinStateV2.VerifierCodeChanged.selector);state.submitTransition(j,proof);
    }
    function testPermanentHaltKeepsSafePrefixAndRejectsAllLaterUpdates() public {
        for(uint256 i;i<8;++i) accept(i);
        assertTrue(state.halted());assertFalse(state.isFresh());assertEq(state.getMainchainHeight(),32357);
        (,bytes32 candidate,,uint64 depth,uint64 position)=state.haltRecord();assertTrue(candidate!=state.getMainchainHead());
        assertEq(depth,101);assertEq(position,2);assertEq(state.stateVersion(),8);rejectBytes(journal(7));
        PaymentBoundaryConsumer consumer=new PaymentBoundaryConsumer();bytes32 safe=state.getMainchainHead();
        vm.expectRevert();consumer.requirePayment(address(adapter),safe,bytes32(0),new bytes32[](0),0);
        assertTrue(state.halted());assertEq(state.stateVersion(),8);
    }
}

contract ZkAdapterV2Test is FixtureBase {
    function testCheckpointAndUnknownCannotAuthorizePaymentBeforeProgress() public {
        vm.warp(0);assertFalse(state.isFresh());vm.warp(state.getState().timestamp);assertFalse(state.isFresh());
        assertFalse(adapter.testHelpersEnabled());status(state.getMainchainHead(),false,0);status(bytes32(uint256(42)),false,0);
        assertFalse(adapter.checkTxInclusion(state.getMainchainHead(),bytes32(0),new bytes32[](0),0));
    }
    function testMembershipAndTransactionPathsPreservePaymentAbiAndHashOrder() public {
        accept(0);bytes32 h=materialize(0,"head");status(h,true,1);
        bytes32[] memory path=new bytes32[](1);path[0]=bytes32(uint256(type(uint256).max/255*0x22));
        bytes32 txid=bytes32(uint256(type(uint256).max/255*0x11));
        assertTrue(adapter.checkTxInclusion(h,txid,path,0));
        new PaymentBoundaryConsumer().requirePayment(address(adapter),h,txid,path,0);
        assertFalse(adapter.checkTxInclusion(h,txid,path,1));assertFalse(adapter.checkTxInclusion(h,txid,path,2));
        assertFalse(adapter.checkTxInclusion(h,bytes32(uint256(99)),path,0));
        assertFalse(adapter.checkTxInclusion(h,txid,new bytes32[](33),0));
    }
    function testMaterializationIsPermissionlessIdempotentAndDoesNotRefresh() public {
        accept(0);uint256 at=state.lastUsefulAcceptedAt();vm.warp(block.timestamp+100);
        vm.prank(address(0x2222));bytes32 h=materialize(0,"head");status(h,true,1);
        vm.prank(address(0x3333));assertEq(materialize(0,"head"),h);
        assertEq(state.lastUsefulAcceptedAt(),at);assertEq(state.stateVersion(),1);
    }
    function testSideBranchRetainsCacheAndDoesNotSubstituteItsTimestamp() public {
        accept(0);bytes32 h=materialize(0,"head");accept(1);status(h,true,1);
        assertEq(state.getState().timestamp,1300000000);vm.warp(1300003601);status(h,false,0);
        assertFalse(adapter.checkTxInclusion(h,bytes32(0),new bytes32[](0),0));assertFalse(state.halted());
    }
    function testCanonicalRootChangeInvalidatesCacheWithoutTraversal() public {
        accept(0);bytes32 anchor=materialize(0,"anchor");bytes32 old=materialize(0,"head");accept(1);accept(2);
        status(anchor,false,0);status(old,false,0);
        assertEq(materialize(2,"anchor"),anchor);status(anchor,true,3);bytes32 head=materialize(2,"head");status(head,true,1);
        assertEq(state.stateVersion(),3);
    }
    function testShorterHeavierReorgRejectsOldTailAndAllowsReprovenAnchor() public {
        for(uint256 i;i<4;++i) accept(i);bytes32 old=materialize(3,"head");bytes32 anchor=materialize(3,"anchor");
        accept(4);assertEq(state.getMainchainHeight(),32257);status(old,false,0);status(anchor,false,0);
        bytes32 winner=materialize(4,"head");status(winner,true,1);materialize(4,"anchor");status(anchor,true,2);
        vm.expectRevert(ZkSpvAdapter.InvalidMembership.selector);materialize(3,"head");
    }
    function testWrongHeightRawRootProofLengthAndLeafPositionAreRejected() public {
        accept(0);bytes memory raw=vm.parseJsonBytes(step(0),key(0,"head.raw"));bytes32[] memory proof=vm.parseJsonBytes32Array(step(0),key(0,"head.siblings"));
        vm.expectRevert();adapter.materializeHeader(32256,raw,proof);
        vm.expectRevert();adapter.materializeHeader(32255,raw,proof);
        vm.expectRevert();adapter.materializeHeader(32258,raw,proof);
        vm.expectRevert();adapter.materializeHeader(32257,raw,new bytes32[](63));
        vm.expectRevert();adapter.materializeHeader(32257,bytes.concat(raw,hex"00"),proof);
        raw[36]^=bytes1(uint8(1));vm.expectRevert();adapter.materializeHeader(32257,raw,proof);raw[36]^=bytes1(uint8(1));
        proof[0]^=bytes32(uint256(1));vm.expectRevert();adapter.materializeHeader(32257,raw,proof);
    }
    function testMaterializingWhileStaleDoesNotReopenMoneyOrSetHalt() public {
        accept(0);vm.warp(1300003601);bytes32 h=materialize(0,"head");status(h,false,0);
        assertFalse(state.isFresh());assertFalse(state.halted());assertEq(state.lastUsefulAcceptedAt(),1300000000);
    }
    function testHaltClosesCachedBlocksAndFurtherMaterialization() public {
        for(uint256 i;i<7;++i) accept(i);bytes32 h=materialize(6,"head");status(h,true,1);accept(7);
        status(h,false,0);assertFalse(adapter.checkTxInclusion(h,bytes32(0),new bytes32[](0),0));
        vm.expectRevert();materialize(7,"head");
    }
    function testRawStateCannotActAsPaymentSpvBypass() public {
        (bool ok,)=address(state).staticcall(abi.encodeCall(PaymentSPVAbi.getBlockStatus,(state.getMainchainHead())));assertFalse(ok);
        vm.expectRevert();new ZkSpvAdapter(address(state));
    }
}
