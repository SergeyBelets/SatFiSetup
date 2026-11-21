// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/ZkBitcoinTestnet4StateV2.sol";
import "../contracts/ZkTestnet4SPVServiceStateView.sol";

/// Native testnet4 vectors; positive state-machine tests use scoped mockCall.
/// These are not real Groth16 proofs and do not qualify a deployment.
contract Testnet4StateTest is Test {
    ZkBitcoinTestnet4StateV2 state;
    string fixture;
    bytes storedJournal;
    function setUp() public {
        vm.chainId(31337);
        fixture=vm.readFile("fixtures/testnet4-state-v2.json");
        bytes memory raw=parse(".checkpoint.checkpoint_header");
        bytes[] memory prior=new bytes[](10);
        for(uint256 i;i<10;++i) prior[i]=parse(string.concat(".checkpoint.checkpoint_predecessors[",vm.toString(i),"]"));
        uint64 height=uint64(vm.parseUint(vm.parseJsonString(fixture,".checkpoint.checkpoint_height")));
        uint256 work=uint256(vm.parseBytes32(string.concat("0x",vm.parseJsonString(fixture,".checkpoint.checkpoint_chainwork"))));
        state=new ZkBitcoinTestnet4StateV2(ZkBitcoinTestnet4StateV2.Config(bytes32(uint256(7)),
            0xa6d885a56488897d9b181b5d224652ad67e020d3868060a3e5aae20a4ae82ab8,
            bytes32("PROOF_ONLY_V1"),3600,7200,7200),raw,height,work,prior);
        bytes memory journal=parse(".journal");
        assertEq(state.checkpointDescriptor(),word(journal,4));
        assertEq(state.getState().commitment,word(journal,7));
        put(journal,3,bytes32(uint256(uint160(address(state)))));
        vm.warp(uint256(word(journal,18)));storedJournal=journal;
    }
    function parse(string memory key) private view returns(bytes memory) {
        return vm.parseBytes(string.concat("0x",vm.parseJsonString(fixture,key)));
    }
    function word(bytes memory j,uint256 n) private pure returns(bytes32 value) {
        assembly ("memory-safe") {value:=mload(add(add(j,32),mul(n,32)))}
    }
    function put(bytes memory j,uint256 n,bytes32 value) private pure {
        assembly ("memory-safe") {mstore(add(add(j,32),mul(n,32)),value)}
    }
    function seal() private view returns(bytes memory) {return bytes.concat(state.verifier().SELECTOR(),new bytes(256));}
    function mockVerify(bytes memory j) private {
        vm.mockCall(address(state.verifier()),abi.encodeCall(IRiscZeroVerifier.verify,
            (seal(),state.programImageId(),sha256(j))),bytes(""));
    }
    function testNativeInitializationTransitionAndTestnetMembershipAgree() public {
        bytes memory journal=storedJournal;
        assertFalse(state.isFresh());mockVerify(journal);state.submitTransition(journal,seal());vm.clearMockedCalls();
        assertEq(state.stateVersion(),1);assertEq(state.getMainchainHeight(),2017);assertTrue(state.isFresh());
        bytes memory raw=parse(".memberships[1].header");
        string[] memory encoded=vm.parseJsonStringArray(fixture,".memberships[1].siblings");
        bytes32[] memory siblings=new bytes32[](encoded.length);
        for(uint256 i;i<encoded.length;++i) siblings[i]=vm.parseBytes32(string.concat("0x",encoded[i]));
        bytes32 blockHash=state.adapter().materializeHeader(2017,raw,siblings);
        (bool canonical,uint64 depth)=state.adapter().getBlockStatus(blockHash);
        assertTrue(canonical);assertEq(depth,1);assertFalse(state.adapter().testHelpersEnabled());
    }
    function testMainnetJournalCannotUpdateTestnetStateEvenAfterVerifierReturn() public {
        bytes memory journal=storedJournal;bytes memory proof=seal();
        put(journal,1,bytes32("bitcoin-mainnet"));mockVerify(journal);
        vm.expectRevert();state.submitTransition(journal,proof);vm.clearMockedCalls();
        assertEq(state.stateVersion(),0);assertFalse(state.isFresh());
    }
    function testRealVerifierRejectsFakeSealWithoutStateMutation() public {
        bytes memory journal=storedJournal;bytes memory proof=seal();
        bytes32 before=state.getState().commitment;
        vm.expectRevert();state.submitTransition(journal,proof);
        assertEq(state.verifier().SELECTOR(),bytes4(0xe71e6620));
        vm.expectRevert();state.submitTransition(journal,bytes.concat(bytes4(0x73c457ba),new bytes(256)));
        assertEq(state.getState().commitment,before);assertEq(state.stateVersion(),0);
    }
    function testWrongChainAndReplayAreAtomic() public {
        bytes memory journal=storedJournal;bytes memory proof=seal();
        put(journal,2,bytes32(uint256(11155111)));mockVerify(journal);
        vm.expectRevert();state.submitTransition(journal,proof);vm.clearMockedCalls();
        assertEq(state.stateVersion(),0);
        put(journal,2,bytes32(uint256(31337)));mockVerify(journal);state.submitTransition(journal,seal());
        vm.expectRevert();state.submitTransition(journal,proof);vm.clearMockedCalls();
        assertEq(state.stateVersion(),1);
    }
    function testServiceViewBindsTestnetProgramBeforeInitialProof() public {
        ZkTestnet4SPVServiceStateView view_=new ZkTestnet4SPVServiceStateView(address(state));
        assertEq(view_.state(),address(state));assertEq(view_.adapter(),address(state.adapter()));
        (bytes32 commitment,uint256 version,bool initialized,bool halted)=view_.snapshot();
        assertEq(commitment,state.getState().commitment);assertEq(version,0);assertTrue(initialized);assertFalse(halted);
        assertFalse(state.isFresh());
        vm.mockCall(address(state),abi.encodeWithSelector(state.programImageId.selector),
            abi.encode(bytes32(0xa4c00c90dcc2600acd756df56ae4915e2180d0ebacc20c157382ec184fe0adea)));
        vm.expectRevert(bytes("profile"));new ZkTestnet4SPVServiceStateView(address(state));
    }

}
