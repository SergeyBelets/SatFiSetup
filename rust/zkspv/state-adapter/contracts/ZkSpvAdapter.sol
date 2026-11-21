// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "satoshifi-core/BitcoinTransitionV2.sol";
import "satoshifi-core/BitcoinCanonicalHistory.sol";

interface IZkBitcoinStateV2 {
    function getState() external view returns(BitcoinTransitionV2.State memory);
    function isFresh() external view returns(bool);
    function stateVersion() external view returns(uint64);
}

/// @notice IPaymentSPV-compatible monetary boundary. Materialization is optional,
/// permissionless and root-bound; it never changes Bitcoin state or freshness.
/// @dev Created only by its state constructor. Existing consumers remain unchanged.
contract ZkSpvAdapter {
    IZkBitcoinStateV2 public immutable state;
    uint256 public constant MAX_MERKLE_DEPTH=32;
    struct Header {bytes32 canonicalRoot;bytes32 merkleRoot;uint64 height;}
    mapping(bytes32=>Header) public materializedHeaders;
    error InvalidCreator();
    error InvalidMembership();
    event HeaderMaterialized(bytes32 indexed blockHash,uint64 height,bytes32 indexed canonicalRoot);

    constructor(address stateAddress) {
        if(stateAddress!=msg.sender || msg.sender.code.length!=0) revert InvalidCreator();
        state=IZkBitcoinStateV2(stateAddress);
    }
    function testHelpersEnabled() external pure returns(bool) {return false;}
    function isFresh() external view returns(bool) {return state.isFresh();}

    function materializeHeader(uint64 height,bytes calldata raw,bytes32[] calldata siblings) external returns(bytes32 blockHash) {
        BitcoinTransitionV2.State memory s=state.getState();
        BitcoinCanonicalHistory.View memory view_=BitcoinCanonicalHistory.View(s.anchor,s.anchorHeight,s.head,s.height,s.canonicalRoot,s.halted);
        if(!BitcoinCanonicalHistory.verify(view_,height,raw,siblings)) revert InvalidMembership();
        blockHash=sha256(abi.encodePacked(sha256(raw)));
        bytes32 merkleRoot;
        assembly ("memory-safe") {merkleRoot := calldataload(add(raw.offset,36))}
        materializedHeaders[blockHash]=Header(s.canonicalRoot,merkleRoot,height);
        emit HeaderMaterialized(blockHash,height,s.canonicalRoot);
    }
    function getBlockStatus(bytes32 blockHash) external view returns(bool,uint64) {
        BitcoinTransitionV2.State memory s=state.getState();Header memory h=materializedHeaders[blockHash];
        if(!state.isFresh() || !current(s,h)) return(false,0);
        return(true,s.height-h.height+1);
    }
    function checkTxInclusion(bytes32 blockHash,bytes32 txid,bytes32[] calldata siblings,uint256 index)
        external view returns(bool)
    {
        BitcoinTransitionV2.State memory s=state.getState();Header memory h=materializedHeaders[blockHash];
        if(!state.isFresh() || !current(s,h) || siblings.length>MAX_MERKLE_DEPTH || index>>siblings.length!=0) return false;
        // Exact transaction Merkle hashing/order from MainnetHeaderRelay. These
        // siblings remain the transaction path, never a packed history witness.
        bytes32 node=txid;
        for(uint256 i;i<siblings.length;++i) {
            node=(index&1)==0?sha256(abi.encodePacked(sha256(abi.encodePacked(node,siblings[i]))))
                :sha256(abi.encodePacked(sha256(abi.encodePacked(siblings[i],node))));
            index>>=1;
        }
        return node==h.merkleRoot;
    }
    function current(BitcoinTransitionV2.State memory s,Header memory h) private pure returns(bool) {
        return !s.halted && h.canonicalRoot==s.canonicalRoot && h.height>=s.anchorHeight && h.height<=s.height;
    }
}
