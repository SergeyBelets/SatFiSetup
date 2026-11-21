// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "satoshifi-core/BitcoinCheckpoint.sol";
import "./BitcoinTestnet4TransitionV2.sol";

/// @dev Initial V2 commitments, matching resume-policy::initialize. Validates the
/// actual checkpoint with the existing library; no caller-supplied initial root.
library BitcoinTestnet4CheckpointV2 {
    function initialize(bytes memory raw,uint64 height,uint256 work,bytes[] memory prior)
        internal pure returns(BitcoinTestnet4TransitionV2.State memory s,bytes32 descriptor)
    {
        BitcoinCheckpoint.Context memory c;
        (c,)=BitcoinCheckpoint.validate(raw,height,work,prior);
        bytes memory ancestors;
        for(uint256 i;i<prior.length;++i) ancestors=bytes.concat(ancestors,prior[i]);
        bytes[] memory binding=new bytes[](5);
        binding[0]=bytes("bitcoin-testnet4");binding[1]=raw;binding[2]=abi.encodePacked(height);
        binding[3]=abi.encodePacked(work);binding[4]=ancestors;
        descriptor=framedHash(bytes("testnet4-checkpoint-v1"),binding);
        bytes[] memory parts=new bytes[](3);
        parts[0]=bytes("bitcoin-testnet4");parts[1]=abi.encodePacked(c.blockHash);parts[2]=abi.encodePacked(height);
        bytes32 scope=framedHash(bytes("testnet4-anchor"),parts);
        bytes32 merkleRoot;
        assembly ("memory-safe") { merkleRoot := mload(add(raw,68)) }
        parts=new bytes[](5);
        parts[0]=abi.encodePacked(scope);parts[1]=abi.encodePacked(height);parts[2]=abi.encodePacked(c.blockHash);
        parts[3]=abi.encodePacked(merkleRoot);parts[4]=abi.encodePacked(c.epochStartTime);
        bytes32 history=framedHash(bytes("canonical-header"),parts);
        bytes32 empty=framedHash(bytes("history-empty"),new bytes[](0));
        parts=new bytes[](3);
        for(uint256 bit;bit<64;++bit) {
            parts[0]=abi.encodePacked(uint8(bit+1));
            bool right=((uint256(height)>>bit)&1)!=0;
            parts[1]=abi.encodePacked(right?empty:history);parts[2]=abi.encodePacked(right?history:empty);
            history=framedHash(bytes("history-node"),parts);
            parts[1]=abi.encodePacked(empty);parts[2]=parts[1];empty=framedHash(bytes("history-node"),parts);
        }
        parts=new bytes[](4);parts[0]=abi.encodePacked(scope);parts[1]=abi.encodePacked(c.blockHash);
        parts[2]=abi.encodePacked(height);parts[3]=abi.encodePacked(history);
        s.canonicalRoot=framedHash(bytes("canonical-root"),parts);
        parts=new bytes[](6);parts[0]=abi.encodePacked(scope);parts[1]=raw;parts[2]=abi.encodePacked(height);
        parts[3]=abi.encodePacked(work);parts[4]=abi.encodePacked(BitcoinCheckpoint.contextCommitment(c));parts[5]=abi.encodePacked(history);
        bytes32 leaf=framedHash(bytes("known-header-v2"),parts);
        parts=new bytes[](2);parts[0]=abi.encodePacked(descriptor);parts[1]=abi.encodePacked(leaf);
        s.knownLog=framedHash(bytes("resume-anchor-v2"),parts);
        empty=framedHash(bytes("resume-index-empty-v2"),new bytes[](0));
        bytes32 node=leaf;parts=new bytes[](3);
        for(uint256 bit;bit<256;++bit) {
            parts[0]=abi.encodePacked(uint16(bit+1));
            bool right=((uint256(c.blockHash)>>bit)&1)!=0;
            parts[1]=abi.encodePacked(right?empty:node);parts[2]=abi.encodePacked(right?node:empty);
            node=framedHash(bytes("resume-index-node-v2"),parts);
            parts[1]=abi.encodePacked(empty);parts[2]=parts[1];empty=framedHash(bytes("resume-index-node-v2"),parts);
        }
        s.anchor=c.blockHash;s.anchorHeight=height;s.head=c.blockHash;s.height=height;s.work=work;
        s.timestamp=c.epochStartTime;s.knownRoot=node;s.knownCount=1;
        s.commitment=BitcoinTestnet4TransitionV2.stateCommitment(s,descriptor,bytes32(0),bytes32(0),bytes32(0),0);
    }

    function framedHash(bytes memory domain,bytes[] memory parts) private pure returns(bytes32) {
        bytes memory encoded=abi.encodePacked("SATOSHIFI_FORK_V1\x00",uint32(domain.length),domain);
        for(uint256 i;i<parts.length;++i) encoded=bytes.concat(encoded,abi.encodePacked(uint64(parts[i].length),parts[i]));
        return sha256(encoded);
    }
}
