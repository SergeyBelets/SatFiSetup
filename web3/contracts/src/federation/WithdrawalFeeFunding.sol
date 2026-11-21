// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./WithdrawalFeeCredits.sol";
import "../core/BitcoinWithdrawalFeeDerivation.sol";
interface IQualifiedWithdrawalFeeDerivation {
    function FEE_PROFILE() external view returns(bytes32);
    function FEE_QUALIFIED() external view returns(bool);
    function deriveFee(bytes calldata,bytes calldata) external view returns(BitcoinDepositDerivation.Descriptor memory);
}
contract WithdrawalFeeFunding {
    BitcoinAddressRegistry public immutable addresses;
    BitcoinDepositRegistry public immutable deposits;
    address public immutable credits;
    IQualifiedWithdrawalFeeDerivation public immutable verifier;
    bytes32 public immutable verifierRuntimeHash;
    bytes32 public immutable scope;
    uint256 public nonce;
    bytes public constant FEE_DOMAIN="SATOHSI_WITHDRAWAL_FEE_V1\x00";
    struct Binding { bytes32 request; bytes raw; bytes32 scriptHash; BitcoinDepositDerivation.Descriptor descriptor; uint256 registeredAt; bytes32 anchor; }
    mapping(bytes32=>Binding) private bindings;
    mapping(bytes32=>bytes32) public requestBinding;
    bytes32[] public bindingIds;
    mapping(bytes32=>uint256) public classified;
    event FeeAddressRegistered(bytes32 indexed id,bytes32 indexed request,bytes script);
    event Classified(bytes32 indexed transaction,uint256 next,bool complete);
    constructor(address registry,address credits_,address verifier_) {
        require(credits_ != address(0) && verifier_.code.length > 0,"fee funding config");
        addresses=BitcoinAddressRegistry(registry);deposits=BitcoinDepositRegistry(addresses.deposits());credits=credits_;
        verifier=IQualifiedWithdrawalFeeDerivation(verifier_);verifierRuntimeHash=verifier_.codehash;
        require(verifier.FEE_PROFILE() == keccak256("BASE_POST_DKG_EVEN_Y__WITHDRAWAL_FEE_TAPLEAF_V1"),"fee purpose profile");
        scope=keccak256(abi.encode(keccak256("SATOHSI_WITHDRAWAL_FEE_FUNDING_V1"),block.chainid,address(this),registry,credits_,verifier_,verifierRuntimeHash));
    }
    function binding(bytes32 id) external view returns(Binding memory) { return bindings[id]; }
    function bindingCount() external view returns(uint256) { return bindingIds.length; }
    function hasBinding(bytes32 request) external view returns(bool) { return requestBinding[request] != 0; }
    function _fence(uint256 cg,uint256 dg) private view {
        require(BitcoinWithdrawalEscrow(addresses.escrow()).withdrawalFees() == credits,"fee funding unbound");addresses.requireCustody(cg);
        require(dg == addresses.derivationGeneration() && uint8(addresses.derivationState()) == 0
            && verifier.FEE_QUALIFIED() && address(verifier).codehash == verifierRuntimeHash,"fee purpose qualification required");
    }
    function getRegisterOperationHash(bytes32 request,bytes calldata raw,WithdrawalFeeTypes.Control calldata c) public view returns(bytes32) {
        return addresses.batches().authorization().hashOperation(address(this),this.register.selector,keccak256(abi.encode(scope,request,raw,c)),c.sequence);
    }
    function register(bytes32 request,bytes calldata raw,WithdrawalFeeTypes.Control calldata c,bytes32 op) external {
        _fence(c.cg,c.dg);require(c.sequence == nonce && block.timestamp <= c.deadline && requestBinding[request] == 0,"fee registration sequence");
        WithdrawalFeeTypes.Budget memory b=IWithdrawalFeeCredits(credits).getBudget(request);
        require(b.mode == WithdrawalFeeTypes.Mode.BTC && !b.closed && block.timestamp <= b.deadline,"fee funding consent");
        BitcoinWithdrawalEscrow.Request memory original=BitcoinWithdrawalEscrow(addresses.escrow()).getRequest(request);
        require(original.state == BitcoinWithdrawalEscrow.State.Pending || original.state == BitcoinWithdrawalEscrow.State.Bound,"fee existing withdrawal required");
        require(raw.length == FEE_DOMAIN.length+478,"fee raw intent");bytes calldata d=raw[FEE_DOMAIN.length:FEE_DOMAIN.length+382];
        address pool=deposits.scriptPools(keccak256(abi.encodePacked(hex"5120",addresses.baseX())));
        require(bytes32(d[30:62]) == addresses.bitcoinGenesis() && uint256(bytes32(d[62:94])) == block.chainid
            && address(bytes20(d[94:114])) == address(addresses) && address(bytes20(d[114:134])) == pool
            && address(bytes20(d[134:154])) == b.token && bytes32(d[154:186]) == addresses.graphId()
            && address(bytes20(d[186:206])) == address(addresses.dkg()) && bytes32(d[206:238]) == addresses.walletId()
            && bytes32(d[238:270]) == addresses.walletResult() && uint64(bytes8(d[270:278])) == addresses.keyEpoch()
            && address(bytes20(d[278:298])) == b.owner && address(bytes20(d[298:318])) == b.owner,"fee intent graph/owner");
        require(bytes32(d[318:350]) == keccak256(abi.encode(scope,request)),"fee request nonce");
        uint256 tail=FEE_DOMAIN.length+382;
        require(bytes32(raw[tail:tail+32]) == request && bytes32(raw[tail+32:tail+64]) == keccak256(b.refundScript)
            && bytes32(raw[tail+64:tail+96]) == IWithdrawalFeeCredits(credits).scope(),"fee immutable funding purpose");
        _refund(b.refundScript);
        BitcoinDepositDerivation.Descriptor memory desc=verifier.deriveFee(raw,abi.encodePacked(hex"02",addresses.baseX()));
        bytes memory script=abi.encodePacked(hex"5120",desc.outputKey);
        require(op == getRegisterOperationHash(request,raw,c),"fee registration hash");addresses.batches().authorization().consumeOperation(op);++nonce;
        addresses.familyRegistry().bindDerived(address(addresses),desc.outputKey,desc.intentId);
        deposits.registerFeeScript(keccak256(script),desc.intentId);
        bindings[desc.intentId]=Binding(request,raw,keccak256(script),desc,block.number,blockhash(block.number-1));
        requestBinding[request]=desc.intentId;bindingIds.push(desc.intentId);emit FeeAddressRegistered(desc.intentId,request,script);
    }
    function _refund(bytes memory script) private view {
        bool supported=script.length == 22 && script[0] == 0x00 && script[1] == 0x14
            || script.length == 34 && script[0] == 0x51 && script[1] == 0x20;
        require(supported && deposits.scriptPools(keccak256(script)) == address(0) && deposits.feeScripts(keccak256(script)) == 0,"fee refund external route required");
    }
    /// @notice Resumable one-level classification; no input ownership/ancestry proof.
    function classify(bytes calldata raw,uint32 vout,uint256 count) external {
        require(count > 0 && count <= 16,"fee classification chunk");BitcoinFundingParser.Parsed memory p=BitcoinFundingParser.parse(raw,vout);
        require(deposits.feeScripts(keccak256(p.scriptPubKey)) != 0 && !deposits.custodyTransactions(p.txid),"fee internal or unknown ingress");
        uint256 next=classified[p.txid];uint256 end=next+count;if(end > p.inputs.length)end=p.inputs.length;
        bool coinbase=p.inputs.length == 1 && p.inputs[0].txidLE == 0 && p.inputs[0].vout == type(uint32).max;
        if(coinbase)end=1;
        else for(uint256 i=next;i<end;++i) {
            BitcoinDepositRegistry.OutputValue memory value=deposits.getValue(keccak256(abi.encode(p.inputs[i].txidLE,p.inputs[i].vout)));
            require(value.known,"fee input context unavailable");
            // Foreign reservations cannot appropriate external payments. Actual
            // custody scripts identify internal ingress before recordSpend too.
            require(deposits.scriptPools(keccak256(value.script)) == address(0)
                && deposits.feeScripts(keccak256(value.script)) == 0,"fee internal custody ingress");
        }
        classified[p.txid]=end;emit Classified(p.txid,end,end == p.inputs.length);
    }
    function admit(bytes calldata raw,uint32 vout,bytes32 blockHash,bytes32[] calldata siblings,uint256 index,uint256 cg,uint256 dg) external {
        _fence(cg,dg);BitcoinFundingParser.Parsed memory p=BitcoinFundingParser.parse(raw,vout);bytes32 id=deposits.feeScripts(keccak256(p.scriptPubKey));
        Binding storage b=bindings[id];require(b.request != 0 && p.amount > 0 && p.amount <= WithdrawalFeeTypes.MAX_MONEY,"fee target binding");
        require(classified[p.txid] == p.inputs.length && !deposits.custodyTransactions(p.txid),"fee ingress classification required");
        require(siblings.length <= 32 && index >> siblings.length == 0,"fee proof bounds");
        (bool canonical,uint64 depth)=deposits.spv().getBlockStatus(blockHash);
        bool coinbase=p.inputs.length == 1 && p.inputs[0].txidLE == 0 && p.inputs[0].vout == type(uint32).max;
        require(canonical && depth >= (coinbase ? 101 : deposits.depth()) && deposits.spv().checkTxInclusion(blockHash,p.txid,siblings,index),"fee funding proof/maturity");
        deposits.markUserFee(raw,vout);WithdrawalFeeCredits(credits).issue(b.request,p.txid,vout,p.amount);
    }
}
