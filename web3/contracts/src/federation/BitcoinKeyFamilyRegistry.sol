// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./DKGAuthorization.sol";
import "./DKGCoordinator.sol";

interface IFamilyGraph {
    function baseX() external view returns(bytes32);
    function bitcoinGenesis() external view returns(bytes32);
    function graphId() external view returns(bytes32);
    function chainId() external view returns(uint256);
    function dkg() external view returns(address);
    function walletId() external view returns(bytes32);
    function walletResult() external view returns(bytes32);
    function deposits() external view returns(address);
    function escrow() external view returns(address);
    function custodyState() external view returns(uint8);
    function derivationState() external view returns(uint8);
    function custodyGeneration() external view returns(uint256);
    function derivationGeneration() external view returns(uint256);
    function issuanceClosed() external view returns(bool);
}
interface IFamilyFeeEscrow { function withdrawalFees() external view returns(address); }
interface IFamilyCredits { function funding() external view returns(address); }

/// @notice Canonical directory for ONE explicitly enrolled project/chain root.
/// @dev No graph reassignment, key migration, arbitrary execution or balance authority.
/// An alternate directory is not this root: bootstrap/SDK must pin its identity.
/// Governance uses the existing EVM_AUTH authority; graph status uses its original quorum.
contract BitcoinKeyFamilyRegistry {
    uint256 public immutable chainId=block.chainid;
    address public immutable dkg;
    address public immutable coordinator;
    DKGAuthorization public governance;
    uint256 public governanceNonce;
    mapping(bytes32=>bool) public receipts;
    mapping(bytes32=>address) public receiptTarget;
    mapping(bytes32=>uint8) public receiptKind;
    struct Family {
        address registry;
        bytes32 registryCodeHash;
        bytes32 graphId;
        bytes32 genesis;
        bytes32 baseX;
        address graphDkg;
        bytes32 walletId;
        bytes32 walletResult;
        uint256 custodyGeneration;
        uint256 derivationGeneration;
        uint8 custodyState;
        uint8 derivationState;
        bool issuanceClosed;
        bool compromised;
        bool derivationRevoked;
    }
    mapping(bytes32=>Family) public families;
    mapping(address=>bytes32) public registryFamily;
    mapping(bytes32=>bytes32) public keyFamily;
    mapping(bytes32=>bytes32) public aliasBinding;
    event FamilyEnrolled(bytes32 indexed family,address indexed registry,bytes32 indexed baseX,bytes32 graphId);
    event FamilyObserved(bytes32 indexed family,uint256 cg,uint256 dg,bool closed,bool compromised,bool revoked);
    event DerivedKeyBound(bytes32 indexed family,bytes32 indexed key,bytes32 binding);
    event GovernanceRotated(address indexed previous,address indexed next);

    constructor(address authority) {
        require(authority.code.length>0,"family authority");governance=DKGAuthorization(authority);
        dkg=governance.registry();coordinator=address(governance.coordinator());
        require(dkg.code.length>0 && coordinator.code.length>0,"family DKG");
    }
    function familyId(bytes32 genesis,bytes32 key) public pure returns(bytes32) {
        return sha256(abi.encodePacked("SATOHSI_BTC_KEY_FAMILY_V1\x00",genesis,key));
    }
    function getEnrollOperationHash(address registry) public view returns(bytes32) {
        IFamilyGraph r=IFamilyGraph(registry);
        return governance.hashOperation(address(this),this.enroll.selector,
            keccak256(abi.encode(chainId,registry,registry.codehash,r.bitcoinGenesis(),r.baseX(),r.graphId(),
                r.dkg(),r.walletId(),r.walletResult(),r.deposits(),r.escrow())),governanceNonce);
    }
    function enroll(address registry,bytes32 op) external {
        if(receipts[op]) { require(receiptTarget[op]==registry && receiptKind[op]==1,"family receipt");return; }
        require(block.chainid==chainId && registry.code.length>0,"family chain/registry");
        require(op==getEnrollOperationHash(registry),"family enrollment hash");
        IFamilyGraph r=IFamilyGraph(registry);bytes32 key=r.baseX();bytes32 genesis=r.bitcoinGenesis();
        require(r.chainId()==chainId && r.dkg().code.length>0 && key!=0 && genesis!=0,"family graph");
        // A new independent graph may use another canonical DKG deployment.
        // The original root quorum admits it explicitly; key uniqueness is
        // shared across every admitted graph, DKG deployment and ceremony.
        (,bytes32 result,,bytes32 purpose,bytes memory group,,)=DKGCoordinator(r.dkg()).getCompletedResult(r.walletId());
        require(result!=0 && result==r.walletResult() && purpose==keccak256("BTC_SPEND")
            && group.length==32 && bytes32(group)==key,"family completed key");
        bytes32 id=familyId(genesis,key);
        // Both base keys and registered derived keys occupy the same namespace.
        require(keyFamily[key]==0 && families[id].registry==address(0) && registryFamily[registry]==0,"family already bound");
        governance.consumeOperation(op);++governanceNonce;
        Family storage f=families[id];f.registry=registry;f.registryCodeHash=registry.codehash;
        f.graphId=r.graphId();f.genesis=genesis;f.baseX=key;
        f.graphDkg=r.dkg();f.walletId=r.walletId();f.walletResult=r.walletResult();
        keyFamily[key]=id;registryFamily[registry]=id;receipts[op]=true;receiptTarget[op]=registry;receiptKind[op]=1;
        _sync(id,r);emit FamilyEnrolled(id,registry,key,f.graphId);
    }
    function _graph(address registry) private view returns(bytes32 id) {
        id=registryFamily[registry];Family storage f=families[id];IFamilyGraph r=IFamilyGraph(registry);
        require(block.chainid==chainId && id!=0 && f.registry==registry && registry.codehash==f.registryCodeHash
            && r.graphId()==f.graphId && r.baseX()==f.baseX && r.bitcoinGenesis()==f.genesis
            && r.dkg()==f.graphDkg && r.walletId()==f.walletId && r.walletResult()==f.walletResult,"family identity");
    }
    function requireGraph(address registry) external view returns(bytes32) { return _graph(registry); }
    function sync() external { bytes32 id=_graph(msg.sender);_sync(id,IFamilyGraph(msg.sender)); }
    function _sync(bytes32 id,IFamilyGraph r) private {
        Family storage f=families[id];uint256 cg=r.custodyGeneration();uint256 dg=r.derivationGeneration();
        uint8 cs=r.custodyState();uint8 ds=r.derivationState();bool closed=r.issuanceClosed();
        require(cs<=3 && ds<=2 && cg>0 && dg>0 && cg>=f.custodyGeneration && dg>=f.derivationGeneration,"family generation");
        if(cg==f.custodyGeneration)require(cs==f.custodyState && closed==f.issuanceClosed,"family custody unchanged");
        if(dg==f.derivationGeneration)require(ds==f.derivationState,"family derivation unchanged");
        require(!f.compromised || cs==3,"family custody terminal");
        require(!f.derivationRevoked || ds==2,"family derivation terminal");
        require(!f.issuanceClosed || closed,"family issuance terminal");
        f.custodyGeneration=cg;f.derivationGeneration=dg;f.custodyState=cs;f.derivationState=ds;
        f.issuanceClosed=closed || f.issuanceClosed || cs==1 || cs==3;
        f.compromised=f.compromised || cs==3;f.derivationRevoked=f.derivationRevoked || ds==2;
        emit FamilyObserved(id,cg,dg,f.issuanceClosed,f.compromised,f.derivationRevoked);
    }
    function fence(address registry,bool derived,bool issuing) public view {
        bytes32 id=_graph(registry);Family storage f=families[id];IFamilyGraph r=IFamilyGraph(registry);
        require(!f.compromised && f.custodyState<=1 && r.custodyState()==f.custodyState
            && r.custodyGeneration()==f.custodyGeneration,"family custody restricted");
        if(derived)require(!f.derivationRevoked && f.derivationState==0 && r.derivationState()==0
            && r.derivationGeneration()==f.derivationGeneration,"family derivation restricted");
        if(issuing)require(f.custodyState==0 && !f.issuanceClosed && !r.issuanceClosed(),"family issuance closed");
    }
    function bindDerived(address registry,bytes32 key,bytes32 binding) external {
        bytes32 id=_graph(registry);IFamilyGraph r=IFamilyGraph(registry);
        address credits=IFamilyFeeEscrow(r.escrow()).withdrawalFees();
        require(msg.sender==registry || credits!=address(0) && msg.sender==IFamilyCredits(credits).funding(),"family alias registrar");
        fence(registry,true,false);require(key!=0 && key!=r.baseX() && binding!=0,"family alias");
        require(keyFamily[key]==0 || keyFamily[key]==id && aliasBinding[key]==binding,"family alias conflict");
        keyFamily[key]=id;aliasBinding[key]=binding;emit DerivedKeyBound(id,key,binding);
    }
    function getRotateOperationHash(address next) public view returns(bytes32) {
        return governance.hashOperation(address(this),this.rotateGovernance.selector,keccak256(abi.encode(next)),governanceNonce);
    }
    function rotateGovernance(address next,bytes32 op) external {
        if(receipts[op]) { require(receiptTarget[op]==next && receiptKind[op]==2,"family receipt");return; }
        require(next.code.length>0 && op==getRotateOperationHash(next),"family rotate hash");
        DKGAuthorization a=DKGAuthorization(next);
        require(a.registry()==dkg && address(a.coordinator())==coordinator,"family rotate DKG");
        governance.consumeOperation(op);++governanceNonce;address previous=address(governance);governance=a;receipts[op]=true;receiptTarget[op]=next;receiptKind[op]=2;
        emit GovernanceRotated(previous,next);
    }
}
