// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "zk-state/ZkSpvAdapter.sol";
import "risc0-verifier/groth16/RiscZeroGroth16Verifier.sol";

/// @notice TEST ONLY: one proven linear regtest extension from an explicit checkpoint.
/// @dev No production registration/profile, resets, mock proofs or arbitrary roots.
/// Reuses the ordinary monetary adapter; does not implement mainnet V2 transitions.
contract TestRegtestDepositState {
    bytes32 public constant TEST_DOMAIN=bytes32("SATOSHIFI_REGTEST_DEPOSIT_V1");
    bytes32 public constant TEST_NETWORK=bytes32("bitcoin-regtest");
    bytes32 public immutable programImageId;
    bytes32 public immutable checkpointDescriptor;
    uint64 public constant MAX_STATE_AGE=3600;
    uint64 public constant MAX_TIP_LAG=7200;
    RiscZeroGroth16Verifier public immutable verifier;
    ZkSpvAdapter public immutable adapter;
    BitcoinTransitionV2.State private accepted;
    uint64 public stateVersion;
    uint256 public lastUsefulAcceptedAt;
    error InvalidTestConfiguration();
    error InvalidTestTransition();
    event TestRegtestAccepted(bytes32 indexed journalDigest,bytes32 indexed head,uint64 height);

    constructor(bytes32 image,bytes memory checkpoint) {
        if(block.chainid!=31337 || image==0 || checkpoint.length!=920) revert InvalidTestConfiguration();
        programImageId=image;checkpointDescriptor=sha256(checkpoint);
        uint64 height;
        for(uint256 i;i<8;i++) height=(height<<8)|uint64(uint8(checkpoint[i]));
        bytes32 work;assembly ("memory-safe") {work:=mload(add(checkpoint,40))}
        if(height<10 || height%2016!=0 || uint256(work)==0 || uint256(work)>type(uint64).max) revert InvalidTestConfiguration();
        bytes memory raw=new bytes(80);
        for(uint256 i;i<80;i++)raw[i]=checkpoint[840+i];
        accepted.anchor=sha256(abi.encodePacked(sha256(raw)));
        accepted.anchorHeight=height;accepted.head=accepted.anchor;accepted.height=height;accepted.work=uint256(work);
        accepted.timestamp=uint32(uint8(raw[68]))|(uint32(uint8(raw[69]))<<8)|(uint32(uint8(raw[70]))<<16)|(uint32(uint8(raw[71]))<<24);
        verifier=new RiscZeroGroth16Verifier(ControlID.CONTROL_ROOT,ControlID.BN254_CONTROL_ID);
        adapter=new ZkSpvAdapter(address(this));
    }
    function getState() external view returns(BitcoinTransitionV2.State memory){return accepted;}
    function isFresh() external view returns(bool){
        return stateVersion==1 && !accepted.halted && block.timestamp<=lastUsefulAcceptedAt+MAX_STATE_AGE
            && block.timestamp<=uint256(accepted.timestamp)+MAX_TIP_LAG;
    }
    function submitTransition(bytes calldata journal,bytes calldata seal) external {
        if(stateVersion!=0 || journal.length!=512 || seal.length!=260) revert InvalidTestTransition();
        bytes32[16] memory w=abi.decode(journal,(bytes32[16]));
        if(w[0]!=TEST_DOMAIN || w[1]!=TEST_NETWORK || uint256(w[2])!=block.chainid
            || w[3]!=bytes32(uint256(uint160(address(this)))) || w[4]!=checkpointDescriptor
            || w[5]!=accepted.anchor || uint256(w[6])!=accepted.anchorHeight
            || uint256(w[14])==0 || uint256(w[14])>64 || uint256(w[8])!=uint256(accepted.height)+uint256(w[14])
            || uint256(w[9])<=accepted.work || uint256(w[10])>type(uint32).max
            || uint256(w[13])<uint256(w[10]) || uint256(w[13])>block.timestamp+7200) revert InvalidTestTransition();
        // Verify the exact test domain before any state change.
        verifier.verify(seal,programImageId,sha256(journal));
        accepted.head=w[7];accepted.height=uint64(uint256(w[8]));accepted.work=uint256(w[9]);
        accepted.timestamp=uint32(uint256(w[10]));accepted.canonicalRoot=w[11];accepted.commitment=w[12];
        stateVersion=1;lastUsefulAcceptedAt=block.timestamp;
        emit TestRegtestAccepted(sha256(journal),accepted.head,accepted.height);
    }
}
