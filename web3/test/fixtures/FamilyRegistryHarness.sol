// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "../../contracts/src/federation/BitcoinKeyFamilyRegistry.sol";

// UNIT ONLY: DKG completion and approval are explicit fixtures, not real quorum evidence.
contract FamilyDkgFixture {
    mapping(bytes32=>bytes32) public keys;
    function setKey(bytes32 id,bytes32 key) external { keys[id]=key; }
    function getCompletedResult(bytes32 id) external view returns(bytes32,bytes32,uint64,bytes32,bytes memory,address[] memory,uint256) {
        return(id,keccak256(abi.encode(id,keys[id])),1,keccak256("BTC_SPEND"),abi.encodePacked(keys[id]),new address[](0),1);
    }
}
contract FamilyAuthFixture {
    address public immutable registry;
    address public immutable coordinator;
    address public immutable admin=msg.sender;
    mapping(bytes32=>address) public approved;
    constructor(address d,address c) {registry=d;coordinator=c;}
    function hashOperation(address target,bytes4 selector,bytes32 params,uint256 nonce) external view returns(bytes32) {
        return keccak256(abi.encode(block.chainid,address(this),target,selector,params,nonce));
    }
    function approve(bytes32 op,address target) external { require(msg.sender==admin,"fixture admin");approved[op]=target; }
    function consumeOperation(bytes32 op) external returns(bool) { require(approved[op]==msg.sender,"fixture approval");delete approved[op];return true; }
}
contract FamilyGraphFixture {
    bytes32 public immutable baseX;bytes32 public immutable bitcoinGenesis;
    bytes32 public immutable graphId;uint256 public immutable chainId=block.chainid;
    address public immutable dkg;bytes32 public immutable walletId;bytes32 public immutable walletResult;
    address public deposits=address(0x1234);address public escrow=address(this);
    uint8 public custodyState;uint8 public derivationState=1;
    uint256 public custodyGeneration=1;uint256 public derivationGeneration=1;bool public issuanceClosed;
    address public withdrawalFees;
    constructor(address d,bytes32 id,bytes32 genesis,bytes32 graph) {
        dkg=d;walletId=id;baseX=FamilyDkgFixture(d).keys(id);bitcoinGenesis=genesis;graphId=graph;
        walletResult=keccak256(abi.encode(id,baseX));
    }
    function change(address root,uint8 cs,uint8 ds,bool closed,uint256 cg,uint256 dg) external {
        custodyState=cs;derivationState=ds;issuanceClosed=closed;custodyGeneration=cg;derivationGeneration=dg;
        BitcoinKeyFamilyRegistry(root).sync();
    }
    function aliasKey(address root,bytes32 key,bytes32 binding) external {BitcoinKeyFamilyRegistry(root).bindDerived(address(this),key,binding);}
}
