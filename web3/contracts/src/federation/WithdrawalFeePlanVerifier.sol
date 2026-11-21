// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.sol";
import "./WithdrawalFeeTypes.sol";
interface IFeePaymentView {
    function credits() external view returns(address);
    function addresses() external view returns(address);
    function inputHash(bytes32) external view returns(bytes32);
    function winner(bytes32) external view returns(bytes32);
}
interface IFeeKeyQualification {
    function verifier() external view returns(address);
    function verifierRuntimeHash() external view returns(bytes32);
}
interface IFeePurposeQualification { function FEE_QUALIFIED() external view returns(bool); }
/// @notice Bounded authenticated nominal-value accounting with exact owner allocation.
contract WithdrawalFeePlanVerifier {
    function _qualified(BitcoinAddressRegistry a,BitcoinDepositRegistry common,bytes memory script) private view {
        if(common.addressIntents(keccak256(script)) != 0)require(a.verifier().QUALIFIED()
            && address(a.verifier()).codehash == a.verifierRuntimeHash(),"fee deposit key unqualified");
        if(common.feeScripts(keccak256(script)) != 0) {
            IFeeKeyQualification f=IFeeKeyQualification(IWithdrawalFeeCredits(BitcoinWithdrawalEscrow(a.escrow()).withdrawalFees()).funding());
            address v=f.verifier();require(IFeePurposeQualification(v).FEE_QUALIFIED() && v.codehash == f.verifierRuntimeHash(),"fee purpose key unqualified");
        }
    }
    function checkKeys(address policy,bytes calldata raw) external view {
        BitcoinAddressRegistry a=BitcoinAddressRegistry(IFeePaymentView(policy).addresses());BitcoinDepositRegistry common=BitcoinDepositRegistry(a.deposits());
        BitcoinFundingParser.Parsed memory p=BitcoinFundingParser.parse(raw,0);require(p.inputs.length <= 16,"fee input bound");
        for(uint256 i;i<p.inputs.length;++i)_qualified(a,common,common.getValue(keccak256(abi.encode(p.inputs[i].txidLE,p.inputs[i].vout))).script);
    }
    function verify(address policy,WithdrawalFeeTypes.Candidate calldata p) external view returns(WithdrawalFeeTypes.Verified memory v) {
        IWithdrawalFeeCredits ledger=IWithdrawalFeeCredits(IFeePaymentView(policy).credits());
        BitcoinAddressRegistry a=BitcoinAddressRegistry(IFeePaymentView(policy).addresses());
        BitcoinDepositRegistry common=BitcoinDepositRegistry(a.deposits());BitcoinWithdrawalEscrow escrow=BitcoinWithdrawalEscrow(a.escrow());
        BitcoinBatchRegistry batches=a.batches();IUserFeeReserve reserve=IUserFeeReserve(batches.feeReserve());
        require(p.batch != 0 && p.planHash != 0 && p.requests.length > 0 && p.requests.length <= 32
            && p.charges.length == p.requests.length && p.credits.length <= 32,"fee plan bounds");
        BitcoinFundingParser.Parsed memory parsed=BitcoinFundingParser.parse(p.raw,0);
        require(parsed.inputs.length <= 16 && uint8(p.raw[4]) == parsed.inputs.length && parsed.outputCount <= 32,"fee spend template");
        uint256 vbytes=(p.raw.length*4+2+parsed.inputs.length*66+3)/4;require(vbytes <= reserve.maxVbytes(),"fee size profile");
        v.root=p.previous == 0 ? p.batch : batches.reservationRoot(p.previous);
        if(p.previous != 0)require(batches.batchState(p.previous) == BitcoinBatchRegistry.State.Signing
            && IFeePaymentView(policy).winner(v.root) == 0,"fee replacement state");
        bytes32[] memory keys=new bytes32[](parsed.inputs.length);v.freeKeys=new bytes32[](keys.length);
        uint256[] memory inputBacking=new uint256[](keys.length);uint256[] memory inputFree=new uint256[](keys.length);
        uint256 total;uint256 backing;uint256 count;
        for(uint256 i;i<keys.length;++i) {
            require(p.raw[41+41*i] == 0x00,"fee scriptSig template");
            BitcoinFundingParser.Input memory input=parsed.inputs[i];bytes32 k=keccak256(abi.encode(input.txidLE,input.vout));
            BitcoinDepositRegistry.OutputValue memory value=common.getValue(k);
            require(value.known && value.script.length == 34 && value.script[0] == 0x51 && value.script[1] == 0x20,"fee prevout value/key required");
            bytes32 h=keccak256(value.script);require(common.scriptPools(h) != address(0) || common.feeScripts(h) != 0,"fee custody input");_qualified(a,common,value.script);
            uint256 b=common.backingSats(k);uint256 free=reserve.creditAmount(k);uint256 personal=ledger.protectedSats(k);
            require(b+free+personal == value.sats && value.sats > 0,"fee pending or unclassified input");
            if(common.addressIntents(h) != 0 || common.feeScripts(h) != 0)v.dg=p.control.dg;
            backing+=b;inputBacking[i]=b;inputFree[i]=free;v.inputFree+=free;total+=value.sats;v.freeKeys[i]=k;count+=ledger.claimsAt(k).length;
            keys[i]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(input.txidLE),input.vout));
            for(uint256 j;j<i;++j)require(keys[i] != keys[j],"fee duplicate input");
        }
        require(total <= WithdrawalFeeTypes.MAX_MONEY && count == p.credits.length,"fee complete claim set");v.inputHash=keccak256(abi.encode(keys));
        if(p.previous != 0)require(v.inputHash == IFeePaymentView(policy).inputHash(v.root),"fee same complete inputs");
        // Cache each bounded obligation, budget and claim once. Repeated RPC
        // calls and whole-transaction rescans in allocation loops are avoided.
        BitcoinWithdrawalEscrow.Request[] memory requests=new BitcoinWithdrawalEscrow.Request[](p.requests.length);
        WithdrawalFeeTypes.Budget[] memory budgets=new WithdrawalFeeTypes.Budget[](p.requests.length);
        bool[] memory isRefund=new bool[](p.requests.length);bytes32[] memory origins=new bytes32[](p.requests.length);
        uint256 principal;uint256 userFee;uint256 externalDue;
        for(uint256 i;i<p.requests.length;++i) {
            require(p.charges[i].request == p.requests[i] && (i == 0 || p.requests[i] > p.requests[i-1]),"fee ordered requests");
            BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(p.requests[i]);requests[i]=r;
            require(r.state == BitcoinWithdrawalEscrow.State.Pending || (r.state == BitcoinWithdrawalEscrow.State.Bound && r.batch == v.root),"fee obligation state");
            bytes32 h=keccak256(r.script);require(r.amount > 0 && r.amount%1e10 == 0 && common.scriptPools(h) == address(0) && common.feeScripts(h) == 0,"fee payout profile");
            isRefund[i]=ledger.refundRequest(p.requests[i]);origins[i]=ledger.refundOrigin(p.requests[i]);
            if(r.token != address(0))principal+=r.amount/1e10;else require(isRefund[i],"fee unrelated treasury obligation");
            externalDue+=r.amount/1e10;WithdrawalFeeTypes.Budget memory b=ledger.getBudget(p.requests[i]);budgets[i]=b;uint256 c=p.charges[i].sats;
            if(b.revision == 0 || b.closed)require(c == 0 && p.charges[i].revision == 0,"fee NONE source");
            else {
                require(b.revision == p.charges[i].revision && b.deadline >= block.timestamp && c <= b.cap,"fee candidate revision/cap");
                if(b.mode == WithdrawalFeeTypes.Mode.MP || b.mode == WithdrawalFeeTypes.Mode.S) { require(c*1e10 <= b.locked,"fee token collateral");v.tokenBurn+=c; }
            }
            userFee+=c;
        }
        uint256[] memory outputValues=new uint256[](parsed.outputCount);bytes32[] memory outputScripts=new bytes32[](parsed.outputCount);
        bytes32 base=keccak256(abi.encodePacked(hex"5120",a.baseX()));uint256 outputTotal;uint256 change;
        for(uint32 i;i<parsed.outputCount;++i) {
            (uint64 amount,bytes memory script)=BitcoinTxParser.outputAt(p.raw,i);bytes32 h=keccak256(script);outputValues[i]=amount;outputScripts[i]=h;outputTotal+=amount;
            require(amount > 0 && amount <= WithdrawalFeeTypes.MAX_MONEY && common.feeScripts(h) == 0,"fee output profile");
            bool supported=script.length == 22 && script[0] == 0 && script[1] == 0x14 || script.length == 34 && script[0] == 0x51 && script[1] == 0x20;
            require(supported,"fee output script");
            require(amount >= (script.length == 22 ? escrow.minP2WPKHSats() : escrow.minP2TRSats()),"fee output minimum");
            if(h == base) { require(amount >= escrow.minP2TRSats(),"fee change minimum");change+=amount; }
            else require(common.scriptPools(h) == address(0),"fee non-base custody change");
        }
        require(outputTotal <= total,"fee negative fee");v.fee=total-outputTotal;
        require(v.fee <= reserve.protocolFeeCap() && userFee <= v.fee,"fee protocol allocation cap");v.treasuryFee=v.fee-userFee;
        WithdrawalFeeTypes.Claim[] memory claims=new WithdrawalFeeTypes.Claim[](p.credits.length);
        bytes32[] memory refundScripts=new bytes32[](p.credits.length);uint256[] memory ownCost=new uint256[](keys.length);
        uint256[] memory allocated=new uint256[](p.requests.length);uint256[] memory refunds=new uint256[](p.requests.length);
        uint256[] memory outputProtected=new uint256[](parsed.outputCount);uint256 carry;
        for(uint256 i;i<p.credits.length;++i) {
            WithdrawalFeeTypes.CreditUse memory u=p.credits[i];WithdrawalFeeTypes.Claim memory c=ledger.getClaim(u.id);claims[i]=c;
            require(c.sats > 0 && (c.root == 0 || c.root == v.root || batches.batchState(c.root) == BitcoinBatchRegistry.State.Cancelled),"fee credit state");
            for(uint256 j;j<i;++j)require(u.id != p.credits[j].id,"fee duplicate claim");
            bool found;for(uint256 j;j<keys.length;++j)if(c.outpoint == v.freeKeys[j]) { found=true;ownCost[j]+=u.fee+u.refund; }
            require(found && uint256(u.fee)+u.refund <= c.sats,"fee credit input/debit");
            if(u.payer == 0)require(u.fee == 0 && u.refund == 0,"fee unassigned credit debit");
            else {
                found=false;
                for(uint256 j;j<p.requests.length;++j)if(u.payer == p.requests[j]) {
                    require(u.payer == c.request || origins[j] == c.request,"fee credit owner/purpose");found=true;allocated[j]+=u.fee;refunds[j]+=u.refund;
                    if(!isRefund[j]) { externalDue+=u.refund;refundScripts[i]=keccak256(ledger.getBudget(c.request).refundScript); }
                }
                require(found,"fee credit owner/purpose");
            }
            uint256 rest=uint256(c.sats)-u.fee-u.refund;
            if(rest != 0) { require(u.carryVout < parsed.outputCount && outputScripts[u.carryVout] == base,"fee named carry destination");carry+=rest;outputProtected[u.carryVout]+=rest; }
        }
        for(uint256 i;i<p.requests.length;++i) {
            WithdrawalFeeTypes.Budget memory b=budgets[i];
            if(b.revision != 0 && !b.closed)require(v.fee <= uint256(b.maxRate)*vbytes,"fee owner rate bound");
            if(b.mode == WithdrawalFeeTypes.Mode.BTC && !b.closed)require(allocated[i] == p.charges[i].sats,"fee BTC source allocation");
            else require(allocated[i] == 0 && refunds[i] == 0,"fee wrong credit source");
            if(isRefund[i])require(refunds[i] == requests[i].amount/1e10,"fee refund principal");
        }
        for(uint256 i;i<parsed.outputCount;++i)if(outputScripts[i] != base) {
            uint256 paid;uint256 due;bytes32 h=outputScripts[i];
            for(uint256 j;j<parsed.outputCount;++j)if(outputScripts[j] == h)paid+=outputValues[j];
            for(uint256 j;j<requests.length;++j)if(keccak256(requests[j].script) == h)due+=requests[j].amount/1e10;
            for(uint256 j;j<p.credits.length;++j)if(refundScripts[j] == h)due+=p.credits[j].refund;
            require(due > 0 && paid == due,"fee exact aggregate payout/refund");
        }
        require(outputTotal-change == externalDue && backing >= principal+v.tokenBurn
            && change >= backing-principal-v.tokenBurn+carry,"fee principal and credit conservation");
        uint256 remaining=backing-principal-v.tokenBurn;
        for(uint256 i;i<parsed.outputCount;++i)if(outputScripts[i] == base) {
            uint256 assigned=outputValues[i] < remaining ? outputValues[i] : remaining;remaining-=assigned;
            require(outputProtected[i]+assigned <= outputValues[i],"fee per-output claim conservation");
        }
        if(userFee != 0)for(uint256 i;i<keys.length;++i) {
            if(inputBacking[i] != 0)require(ownCost[i] != 0 || backing-inputBacking[i] < principal+v.tokenBurn,"fee unrelated consolidation");
            else require(ownCost[i] != 0 || inputFree[i] != 0 && v.treasuryFee != 0 && v.inputFree-inputFree[i] < v.treasuryFee,"fee unrelated free input");
        }
        v.txid=parsed.txid;
    }
}
