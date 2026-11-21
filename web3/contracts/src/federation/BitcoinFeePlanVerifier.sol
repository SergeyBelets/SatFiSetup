// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.sol";

interface IFeePlanContext {
    function deposits() external view returns(address);
    function batches() external view returns(address);
    function escrow() external view returns(address);
    function walletScriptHash() external view returns(bytes32);
    function maxVbytes() external view returns(uint32);
    function creditAmount(bytes32) external view returns(uint256);
    function winners(bytes32) external view returns(bytes32);
    function rootInputHash(bytes32) external view returns(bytes32);
    function allocations(bytes32) external view returns(uint256,uint256,bool,bool,bool,bytes32);
}

/// @notice Stateless, bounded Bitcoin value/principal validator; no grants or mutable policy.
contract BitcoinFeePlanVerifier {
    struct Control { uint256 sequence; uint256 cg; uint256 dg; uint64 deadline; }
    struct Prepared { Control control; bytes32 allocation; bytes32 batch; bytes32 previous; bytes32 planHash; bytes raw; bytes32[] requests; }
    struct Verified { bytes32 root; bytes32 txid; bytes32 inputHash; bytes32 requestHash; uint256 inputFree; uint256 fee; uint256 debit; uint256 burned; uint256 dg; bytes32[] creditKeys; }
    function verify(address reserve_,Prepared calldata p) external view returns(Verified memory v) {
        IFeePlanContext f=IFeePlanContext(reserve_);
        BitcoinDepositRegistry common=BitcoinDepositRegistry(f.deposits());
        BitcoinBatchRegistry batches=BitcoinBatchRegistry(f.batches());
        BitcoinWithdrawalEscrow escrow=BitcoinWithdrawalEscrow(f.escrow());
        (uint256 cap,uint256 maxFee,bool treasuryAllowed,bool active,bool bound,bytes32 allocationRoot)=f.allocations(p.allocation);
        require(active && p.batch != 0 && p.planHash != 0 && p.raw.length <= 100000
            && p.requests.length > 0 && p.requests.length <= 32,"fee plan");
        (bytes32[] memory prev,uint32[] memory vouts)=BitcoinTxParser.parseAllInputs(p.raw);
        require(prev.length <= 16 && uint8(p.raw[4]) == prev.length,"fee input bounds");
        require((p.raw.length*4+2+prev.length*66+3)/4 <= f.maxVbytes(),"fee vbytes profile");
        bytes32[] memory keys=new bytes32[](prev.length); v.creditKeys=new bytes32[](prev.length); uint256 total;
        for(uint256 i;i<prev.length;++i) {
            bytes32 k=keccak256(abi.encode(prev[i],vouts[i])); uint256 free=f.creditAmount(k);
            uint256 backing=common.backingSats(k); require(backing+free > 0,"fee unknown or pending input");
            if(escrow.withdrawalFees() != address(0)) {
                require(IWithdrawalFeeCredits(escrow.withdrawalFees()).protectedSats(k) == 0,"fee protected user credit");
                BitcoinDepositRegistry.OutputValue memory value=common.getValue(k);
                require(value.known && value.sats == backing+free,"fee full value required");
            }
            if(common.addressIntents(common.backingScripts(k)) != 0)v.dg=p.control.dg;
            v.creditKeys[i]=k; total+=backing+free; v.inputFree+=free;
            keys[i]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[i]),vouts[i]));
            for(uint256 j;j<i;++j)require(keys[j] != keys[i],"fee duplicate input");
        }
        v.inputHash=keccak256(abi.encode(keys));
        v.root=p.previous == 0 ? p.batch : batches.reservationRoot(p.previous);
        if(p.previous == 0)require(allocationRoot == 0 && v.inputFree >= cap,"fee root collateral");
        else require(bound && allocationRoot == v.root && f.winners(v.root) == 0
            && batches.batchState(p.previous) == BitcoinBatchRegistry.State.Signing
            && v.inputHash == f.rootInputHash(v.root),"fee replacement family");
        uint256 treasury; uint256 totalOut; uint256 change;
        for(uint256 i;i<p.requests.length;++i) {
            BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(p.requests[i]);
            require(r.state == BitcoinWithdrawalEscrow.State.Pending || (r.state == BitcoinWithdrawalEscrow.State.Bound && r.batch == v.root),"fee obligation state");
            require(r.amount % 1e10 == 0 && r.amount > 0,"fee obligation units");
            for(uint256 j;j<i;++j)require(p.requests[j] != p.requests[i],"fee duplicate obligation");
            if(r.token != address(0))v.burned+=r.amount/1e10;
            else { require(p.requests[i] == p.allocation,"fee treasury allocation binding"); treasury+=r.amount/1e10; }
        }
        bytes32 wallet=f.walletScriptHash();uint256 outputs=BitcoinTxParser.outputCount(p.raw);require(outputs <= 32,"fee output bounds");
        for(uint32 i;i<outputs;++i) {
            (uint64 amount,bytes memory script)=BitcoinTxParser.outputAt(p.raw,i);totalOut+=amount;
            if(keccak256(script) == wallet)change+=amount;
            else {
                require(common.scriptPools(keccak256(script)) == address(0),"fee custody destination");
                uint256 due;
                for(uint256 j;j<p.requests.length;++j) {
                    BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(p.requests[j]);
                    if(keccak256(r.script) == keccak256(script))due+=r.amount/1e10;
                }
                require(due > 0 && BitcoinTxParser.sumOutputsToScript(p.raw,script) == due,"fee exact payout");
            }
        }
        require(total >= totalOut && totalOut-change == v.burned+treasury,"fee principal payout");
        v.fee=total-totalOut; v.debit=v.fee+treasury;
        require(v.fee <= maxFee && v.debit <= cap && v.debit <= v.inputFree && (treasury == 0 || treasuryAllowed),"fee debit cap");
        require(total-v.inputFree >= v.burned && change >= total-v.inputFree-v.burned,"fee principal haircut");
        v.txid=BitcoinTxParser.txId(p.raw);v.requestHash=keccak256(abi.encode(p.requests));
    }
}
