// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.sol";
import {FROSTVerifier} from "../FROSTVerifier.sol";
import "./BitcoinFeeFundingVerifier.sol";
import "./BitcoinFeePlanVerifier.sol";
import "./BitcoinFeeCodeHashes.sol";
import "./WithdrawalFeeTypes.sol";
import "./BitcoinFeeReserveUser.sol";

interface IFeeSigningBoard {
    function dkg() external view returns (address);
    function batchRegistry() external view returns (address);
    function walletId() external view returns (bytes32);
}

/// @notice Canonical free-BTC allocations. Core completeness/unspent checks remain signer policy.
/// @dev No MP haircut, owner credit, BTC signer or arbitrary execution authority.
contract BitcoinFeeReserve is BitcoinFeeReserveStorage {
    uint256 public constant SAT = 1e10;
    uint256 public constant MAX_MONEY = 2100000000000000;
    BitcoinAddressRegistry public immutable addresses;
    BitcoinDepositRegistry public immutable deposits;
    BitcoinBatchRegistry public immutable batches;
    BitcoinWithdrawalEscrow public immutable escrow;
    FROSTAuthorization public immutable authorization;
    FROSTVerifier public immutable donorVerifier;
    BitcoinFeeFundingVerifier public immutable fundingVerifier;
    BitcoinFeePlanVerifier public immutable planVerifier;
    address public immutable signingBoard;
    bytes32 public immutable scope;
    bytes32 public immutable walletScriptHash;
    uint64 public immutable protocolFeeCap;
    uint32 public immutable maxVbytes;
    BitcoinFeeReserveUser public immutable userModule;

    constructor(address registry_, address board_, address donorVerifier_, address fundingVerifier_, address planVerifier_, uint64 feeCap_, uint32 maxVbytes_) {
        require(feeCap_ > 0 && feeCap_ <= MAX_MONEY && maxVbytes_ >= 100 && maxVbytes_ <= 100000,"fee protocol profile");
        protocolFeeCap=feeCap_; maxVbytes=maxVbytes_;
        userModule=new BitcoinFeeReserveUser(address(this));
        addresses = BitcoinAddressRegistry(registry_);
        deposits = BitcoinDepositRegistry(addresses.deposits());
        batches = BitcoinBatchRegistry(address(addresses.batches()));
        escrow = BitcoinWithdrawalEscrow(addresses.escrow());
        authorization = FROSTAuthorization(addresses.authorization());
        require(address(deposits.addressRegistry()) == registry_ && address(escrow.depositRegistry()) == address(deposits)
            && address(escrow.batches()) == address(batches) && batches.withdrawalEscrow() == address(escrow), "fee graph");
        require(IFeeSigningBoard(board_).batchRegistry() == address(batches)
            && IFeeSigningBoard(board_).dkg() == address(addresses.dkg())
            && IFeeSigningBoard(board_).walletId() == addresses.walletId(), "fee board");
        require(donorVerifier_.codehash == BitcoinFeeCodeHashes.DONOR, "donor verifier");
        require(fundingVerifier_.codehash == BitcoinFeeCodeHashes.FUNDING,"funding verifier");
        fundingVerifier=BitcoinFeeFundingVerifier(fundingVerifier_);
        require(planVerifier_.codehash == BitcoinFeeCodeHashes.PLAN,"plan verifier");
        planVerifier=BitcoinFeePlanVerifier(planVerifier_);
        signingBoard = board_; donorVerifier = FROSTVerifier(donorVerifier_);
        walletScriptHash = keccak256(abi.encodePacked(hex"5120", addresses.baseX()));
        require(deposits.scriptPools(walletScriptHash) != address(0), "fee base script");
        scope = keccak256(abi.encode(keccak256("SATOHSI_BTC_FEE_RESERVE_V1"), block.chainid, address(this), registry_,
            address(deposits), address(batches), address(escrow), board_, addresses.graphId(), addresses.bitcoinGenesis(),
            address(addresses.dkg()), addresses.walletId(), addresses.walletResult(), addresses.keyEpoch(), walletScriptHash,feeCap_,maxVbytes_));
    }
    function getOperationHash(bytes4 selector, bytes calldata data, uint256 sequence) public view returns (bytes32) {
        return authorization.hashOperation(address(this),selector,keccak256(abi.encode(scope,keccak256(data))),sequence);
    }
    function _consume(bytes4 selector, bytes memory data, Control memory c, bytes32 op) private {
        require(batches.feeReserve() == address(this) && c.sequence == nonce && block.timestamp <= c.deadline, "fee sequence/window");
        require(c.cg == addresses.custodyGeneration() && c.dg == addresses.derivationGeneration(), "fee generation");
        require(op == authorization.hashOperation(address(this),selector,keccak256(abi.encode(scope,keccak256(data))),c.sequence), "fee hash");
        authorization.consumeOperation(op); receipts[op]=keccak256(data); ++nonce;
    }
    function _custody(uint256 cg, uint256 dg) private view {
        addresses.requireCustody(cg);
        if (dg != 0) require(dg == addresses.derivationGeneration()
            && uint8(addresses.derivationState()) == 0 && address(addresses.verifier()).codehash == addresses.verifierRuntimeHash()
            && addresses.verifier().QUALIFIED(), "fee derivation");
    }
    function reconciliation() external view returns (Report memory) { return report; }
    function getPlan(bytes32 id) external view returns (Plan memory) { return plans[id]; }
    function rootInputHash(bytes32 id) external view returns (bytes32) { return plans[id].inputHash; }
    function creditAmount(bytes32 key) external view returns (uint256) {
        uint256 i=creditIndex[key]; return i == 0 ? 0 : credits[i-1].free;
    }
    function allocationCount() external view returns (uint256) { return allocationIds.length; }
    function familyCount(bytes32 root) external view returns (uint256) { return familyPlans[root].length; }
    function creditCount() external view returns (uint256) { return credits.length; }
    function creditAt(uint256 i) external view returns (Credit memory) { return credits[i]; }
    function credit(bytes32 key) external view returns (Credit memory) {
        uint256 i=creditIndex[key]; return i == 0 ? Credit(0,0,0,0,0) : credits[i-1];
    }
    function available() public view returns (uint256) { return freeSats-lockedSats-promisedSats; }
    function _healthy() private view {
        require(report.state == uint8(Health.HEALTHY) && block.timestamp <= report.validUntil
            && report.control.cg == addresses.custodyGeneration() && report.control.dg == addresses.derivationGeneration(), "fee reconciliation required");
        require(report.free >= freeSats, "fee new funding unreconciled");
    }
    function publishReconciliation(Report calldata r, bytes32 op) external { _delegateUser(abi.encodeCall(BitcoinFeeReserveUser.publishReconciliation,(r,op))); }
    function allocate(Grant calldata g, bytes32 op) external {
        _healthy(); _custody(g.control.cg,0);
        require(g.id != 0 && allocations[g.id].cap == 0 && g.cap != 0 && g.cap <= MAX_MONEY
            && g.maxFee > 0 && g.maxFee <= protocolFeeCap && g.maxFee <= g.cap && g.cap <= available(), "fee allocation budget");
        _consume(this.allocate.selector,abi.encode(g),g.control,op);
        allocations[g.id]=Allocation(g.cap,g.maxFee,g.treasury,true,false,0); allocationIds.push(g.id);
        promisedSats+=g.cap; emit Allocated(g.id,g.cap,g.maxFee);
    }
    function raiseCap(Grant calldata g, bytes32 op) external {
        _healthy(); _custody(g.control.cg,0); Allocation storage a=allocations[g.id];
        require(a.active && a.treasury == g.treasury && g.cap >= a.cap && g.cap <= MAX_MONEY
            && g.maxFee >= a.maxFee && g.maxFee <= protocolFeeCap && g.maxFee <= g.cap, "fee cap monotonic");
        uint256 extra=g.cap-a.cap;
        uint256 collateral=userLocked[a.root] ? userInputFree[a.root] : plans[a.root].inputFree;
        if(a.root != 0)require(g.cap <= collateral,"fee root collateral");
        if (a.bound) require(g.cap <= collateral, "fee collateral");
        else { require(extra <= available(), "fee cap budget"); promisedSats+=extra; }
        _consume(this.raiseCap.selector,abi.encode(g),g.control,op);
        a.cap=g.cap; a.maxFee=g.maxFee; emit Allocated(g.id,g.cap,g.maxFee);
    }
    function release(Control calldata c, bytes32 id, bytes32 op) external {
        Allocation storage a=allocations[id]; require(a.active, "fee allocation inactive");
        if(a.treasury) {
            BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(id);
            require(r.state == BitcoinWithdrawalEscrow.State.None || r.state == BitcoinWithdrawalEscrow.State.Refunded,"fee treasury obligation live");
        }
        if (a.root != 0) require(batches.batchState(a.root) == BitcoinBatchRegistry.State.None
            || batches.batchState(a.root) == BitcoinBatchRegistry.State.Cancelled, "fee signature may exist");
        _consume(this.release.selector,abi.encode(c,id),c,op);
        if (a.bound) {
            _unlock(a.root);
        } else promisedSats-=a.cap;
        a.active=false;
    }



    function donationHash(bytes32 txid,uint32 vout,uint256 sats) public view returns(bytes32) {
        return keccak256(abi.encode(keccak256("SATOHSI_UNCONDITIONAL_FEE_DONATION_V1"),scope,txid,vout,sats,walletScriptHash));
    }
    /// @notice Restricted external profile: one independently controlled P2TR input, no OP_RETURN.
    function admitDonation(Donation calldata d, bytes32 op) external {
        _custody(d.control.cg,0);
        (bytes32 txid,uint256 amount)=fundingVerifier.verify(address(this),d.funding);
        _consume(this.admitDonation.selector,abi.encode(d),d.control,op);
        deposits.cacheValue(d.funding.raw,d.funding.vout);
        deposits.markFeeOutput(txid,d.funding.vout); _putCredit(txid,d.funding.vout,amount,d.funding.blockHash);
        if(report.state == uint8(Health.HEALTHY))report.state=0;
    }
    function preparePlan(BitcoinFeePlanVerifier.Prepared calldata p, bytes32 op) external {
        _healthy(); _custody(p.control.cg,0);
        require(plans[p.batch].planHash == 0,"fee prepared already");
        BitcoinFeePlanVerifier.Verified memory v=planVerifier.verify(address(this),p);
        _custody(p.control.cg,v.dg);
        require(familyPlans[v.root].length < 16 && transactionPlans[v.txid] == 0,"fee version/transaction bound");
        Control memory c=Control(p.control.sequence,p.control.cg,p.control.dg,p.control.deadline);
        _consume(this.preparePlan.selector,abi.encode(p),c,op);
        familyPlans[v.root].push(p.batch);
        Allocation storage a=allocations[p.allocation];if(a.root == 0)a.root=v.root;
        planCreditKeys[p.batch]=v.creditKeys;
        plans[p.batch]=Plan(p.allocation,v.root,p.previous,p.planHash,v.txid,v.inputHash,v.requestHash,v.inputFree,v.fee,v.debit,v.burned,c.cg,v.dg,false,false);
        transactionPlans[v.txid]=p.batch;
        emit PreparedPlan(p.batch,p.allocation,v.txid,v.fee,v.debit);
    }
    function onReserve(bytes32 id,bytes32 planHash,bytes32[] calldata inputs) external {
        require(msg.sender == address(batches),"fee batch caller");
        IWithdrawalFeePayments user=_userPayments();
        if(address(user) != address(0) && user.hasPlan(id)) { user.onReserve(id,planHash,inputs);return; }
        Plan storage p=plans[id];
        require(p.planHash == planHash && p.inputHash == keccak256(abi.encode(inputs)) && !p.reserved,"fee unprepared plan");
        _healthy(); _custody(p.cg,p.dg); Allocation storage a=allocations[p.allocation]; require(a.active,"fee allocation inactive");
        if(!a.bound) {
            require(p.previous == 0 && p.inputFree >= a.cap && p.inputFree <= available()+a.cap,"fee initial family"); a.bound=true; promisedSats-=a.cap;
            // Credits are bounded by the actual input set, without scanning inventory.
            for(uint256 i;i<inputs.length;++i) {
                bytes32 ck=planCreditKeys[id][i]; uint256 at=creditIndex[ck];
                if(at != 0) { Credit storage c=credits[at-1]; require(c.root == 0,"fee input locked"); c.root=p.root; lockedSats+=c.free; rootCredits[p.root].push(ck); }
            }
        }
        p.reserved=true;
    }
    function checkSigning(bytes32 id) external view {
        IWithdrawalFeePayments user=_userPayments();
        if(address(user) != address(0) && user.hasPlan(id)) { _healthy();user.checkSigning(id);return; }
        Plan storage p=plans[id]; _healthy(); _custody(p.cg,p.dg);
        require(p.reserved && !p.settled && allocations[p.allocation].active && winners[p.root] == 0,"fee signing unavailable");
    }
    function checkTreasuryRequest(bytes32 key,uint256 sats) external view {
        _healthy(); _custody(addresses.custodyGeneration(),0); Allocation storage a=allocations[key];
        require(a.active && a.treasury && !a.bound && a.root == 0 && sats+a.maxFee <= a.cap,"fee treasury allocation required");
    }
    /// @notice Exact old winner may settle after an incident/generation change. No new permission is granted.
    function settle(bytes32 id,bytes calldata raw,uint256 burnedSats,bytes32 blockHash) external {
        require(msg.sender == address(escrow),"fee escrow caller");
        IWithdrawalFeePayments user=_userPayments();
        if(address(user) != address(0) && user.hasPlan(id)) { user.settle(id,raw,burnedSats,blockHash);return; }
        Plan storage p=plans[id];
        require(p.reserved && !p.settled && winners[p.root] == 0 && BitcoinTxParser.txId(raw) == p.txid
            && burnedSats == p.burned,"fee winner");
        BitcoinWithdrawalEscrow.Payment memory payment=escrow.payment(id);
        require(payment.settled && keccak256(abi.encode(payment.requests)) == p.requestHash,"fee payment refs");
        _unlock(p.root);
        bytes32[] storage creditKeys=rootCredits[p.root]; for(uint256 i;i<creditKeys.length;++i)_deleteCredit(creditKeys[i]);
        uint256 changeFree; uint256 outputs=BitcoinTxParser.outputCount(raw);
        for(uint32 i;i<outputs;++i) {
            (uint64 amount,bytes memory script)=BitcoinTxParser.outputAt(raw,i);
            if(keccak256(script) != walletScriptHash)continue;
            uint256 backing=deposits.backingSats(keccak256(abi.encode(p.txid,i)));
            uint256 free=amount-backing; changeFree+=free; _putCredit(p.txid,i,free,blockHash);
        }
        require(changeFree+p.debit == p.inputFree,"fee conservation");
        winners[p.root]=id; p.settled=true; allocations[p.allocation].active=false;
        if(report.state == uint8(Health.HEALTHY))report.state=0;
        emit Debited(p.allocation,id,p.debit);
    }
    function _userPayments() private view returns(IWithdrawalFeePayments) {
        address c=escrow.withdrawalFees();return IWithdrawalFeePayments(c == address(0) ? address(0) : IWithdrawalFeeCredits(c).payments());
    }
    function lockUserFamily(bytes32 root,bytes32 grant,bytes32[] calldata keys,uint256 free,uint256 maxDebit) external { _delegateUser(abi.encodeCall(BitcoinFeeReserveUser.lockUserFamily,(root,grant,keys,free,maxDebit))); }
    function settleUserFamily(bytes32 root,bytes32 txid,bytes calldata raw,uint256 debit,bytes32 blockHash) external { _delegateUser(abi.encodeCall(BitcoinFeeReserveUser.settleUserFamily,(root,txid,raw,debit,blockHash))); }
    function releaseUserFamily(bytes32 root) external { _delegateUser(abi.encodeCall(BitcoinFeeReserveUser.releaseUserFamily,(root))); }
    function _delegateUser(bytes memory data) private {
        (bool ok,bytes memory result)=address(userModule).delegatecall(data);
        if(!ok)assembly("memory-safe") { revert(add(result,32),mload(result)) }
    }
}
