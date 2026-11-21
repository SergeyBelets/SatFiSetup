// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./WithdrawalFeePlanVerifier.sol";

contract WithdrawalFeePayments {
    BitcoinAddressRegistry public immutable addresses;
    BitcoinWithdrawalEscrow public immutable escrow;
    address public immutable credits;
    WithdrawalFeePlanVerifier public immutable verifier;
    bytes32 public immutable scope;
    uint256 public nonce;
    mapping(bytes32=>bytes32) public receipts;
    mapping(bytes32=>WithdrawalFeeTypes.Candidate) private candidates;
    mapping(bytes32=>WithdrawalFeeTypes.Verified) private verified;
    mapping(bytes32=>bytes32) public termsHash;
    mapping(bytes32=>bool) public bound;
    mapping(bytes32=>bool) public settled;
    mapping(bytes32=>bytes32) public admissionReceipt;
    mapping(bytes32=>bytes32) public winner;
    mapping(bytes32=>bytes32) public inputHash;
    mapping(bytes32=>bytes32) public rootForRequest;
    mapping(bytes32=>bytes32[]) public versions;
    event Prepared(bytes32 indexed batch,bytes32 indexed terms,bytes32 txid,uint256 fee);
    event Admitted(bytes32 indexed batch,bytes32 indexed receipt);
    event Settled(bytes32 indexed root,bytes32 indexed winner,uint256 fee);
    constructor(address registry,address credits_,address verifier_) {
        require(credits_ != address(0) && verifier_.codehash == keccak256(type(WithdrawalFeePlanVerifier).runtimeCode),"fee payment config");
        addresses=BitcoinAddressRegistry(registry);escrow=BitcoinWithdrawalEscrow(addresses.escrow());credits=credits_;
        verifier=WithdrawalFeePlanVerifier(verifier_);
        scope=keccak256(abi.encode(keccak256("SATOHSI_WITHDRAWAL_FEE_PAYMENTS_V1"),block.chainid,address(this),registry,credits_,verifier_,
            address(escrow),addresses.graphId(),addresses.bitcoinGenesis(),addresses.walletId(),addresses.walletResult(),addresses.keyEpoch()));
    }
    function hasPlan(bytes32 id) external view returns(bool) { return termsHash[id] != 0; }
    function versionCount(bytes32 root) external view returns(uint256) { return versions[root].length; }
    function candidate(bytes32 id) external view returns(WithdrawalFeeTypes.Candidate memory) { return candidates[id]; }
    function verification(bytes32 id) external view returns(WithdrawalFeeTypes.Verified memory) { return verified[id]; }
    function getPrepareOperationHash(WithdrawalFeeTypes.Candidate calldata p) public view returns(bytes32) {
        return addresses.batches().authorization().hashOperation(address(this),this.prepare.selector,keccak256(abi.encode(scope,p)),p.control.sequence);
    }
    function _fence(uint256 cg,uint256 dg) private view {
        require(escrow.withdrawalFees() == credits && IWithdrawalFeeCredits(credits).payments() == address(this),"fee payments unbound");
        addresses.requireCustody(cg);
        if(dg != 0)require(dg == addresses.derivationGeneration() && uint8(addresses.derivationState()) == 0,"fee derivation fence");
    }
    function prepare(WithdrawalFeeTypes.Candidate calldata p,bytes32 op) external {
        require(p.control.sequence == nonce && p.control.dg == addresses.derivationGeneration()
            && block.timestamp <= p.control.deadline && termsHash[p.batch] == 0,"fee candidate sequence/window");
        _fence(p.control.cg,0);WithdrawalFeeTypes.Verified memory v=verifier.verify(address(this),p);_fence(p.control.cg,v.dg);
        require(versions[v.root].length < 16,"fee family versions");
        require(op == getPrepareOperationHash(p),"fee prepare hash");addresses.batches().authorization().consumeOperation(op);++nonce;
        candidates[p.batch]=p;verified[p.batch]=v;termsHash[p.batch]=keccak256(abi.encode(scope,p));
        receipts[op]=termsHash[p.batch];versions[v.root].push(p.batch);
        emit Prepared(p.batch,termsHash[p.batch],v.txid,v.fee);
    }
    function onReserve(bytes32 id,bytes32 hash,bytes32[] calldata inputs) external {
        require(msg.sender == addresses.batches().feeReserve(),"fee reserve caller");
        WithdrawalFeeTypes.Candidate storage p=candidates[id];WithdrawalFeeTypes.Verified storage v=verified[id];
        require(termsHash[id] != 0 && !bound[id] && p.planHash == hash && keccak256(abi.encode(inputs)) == v.inputHash,"fee candidate approval");
        require(block.timestamp <= p.control.deadline,"fee admission deadline");_fence(p.control.cg,v.dg);
        // Repeat canonical validation at admission; a candidate alone locks
        // neither a mutable budget nor an owner's consent for a Bitcoin nonce.
        WithdrawalFeeTypes.Candidate memory copy=p;
        WithdrawalFeeTypes.Verified memory nowVerified=verifier.verify(address(this),copy);
        require(keccak256(abi.encode(nowVerified)) == keccak256(abi.encode(v)),"fee candidate changed");
        IWithdrawalFeeCredits ledger=IWithdrawalFeeCredits(credits);
        for(uint256 i;i<p.requests.length;++i) {
            bytes32 request=p.requests[i];WithdrawalFeeTypes.Budget memory b=ledger.getBudget(request);
            bytes32 oldRoot=rootForRequest[request];
            require(oldRoot == 0 || oldRoot == v.root || addresses.batches().batchState(oldRoot) == BitcoinBatchRegistry.State.Cancelled,"fee other family");
            if(b.revision != 0 && !b.closed) {
                require(b.revision == p.charges[i].revision && ledger.admitted(id,request) == keccak256(abi.encode(termsHash[id],b.revision)),"fee exact owner consent required");
                ledger.bindBudget(request,v.root,b.revision);
            }
            rootForRequest[request]=v.root;
        }
        ledger.lockClaims(v.root,p.credits);
        IUserFeeReserve(msg.sender).lockUserFamily(v.root,p.treasury,v.freeKeys,v.inputFree,v.treasuryFee);
        bound[id]=true;inputHash[v.root]=v.inputHash;admissionReceipt[id]=keccak256(abi.encode(scope,id,termsHash[id],v));
        emit Admitted(id,admissionReceipt[id]);
    }
    function checkSigning(bytes32 id) external view {
        WithdrawalFeeTypes.Candidate storage p=candidates[id];WithdrawalFeeTypes.Verified storage v=verified[id];
        _fence(p.control.cg,v.dg);
        verifier.checkKeys(address(this),p.raw);
        require(bound[id] && admissionReceipt[id] != 0 && !settled[id] && winner[v.root] == 0,"fee admitted receipt required");
        // Mutable free-budget revision/deadline is intentionally not consumed
        // a second time. Locked original tranches and the receipt survive it.
        for(uint256 i;i<p.requests.length;++i) {
            WithdrawalFeeTypes.Budget memory b=IWithdrawalFeeCredits(credits).getBudget(p.requests[i]);
            if(p.charges[i].revision != 0)require(!b.closed && b.root == v.root && (b.mode == WithdrawalFeeTypes.Mode.BTC
                || b.locked >= uint256(p.charges[i].sats)*1e10),"fee tranche missing");
        }
    }
    function feeBurn(bytes32 id) external view returns(uint256) { return bound[id] && !settled[id] ? verified[id].tokenBurn : 0; }
    function settle(bytes32 id,bytes calldata raw,uint256 burned,bytes32 blockHash) external {
        require(msg.sender == addresses.batches().feeReserve(),"fee reserve caller");
        WithdrawalFeeTypes.Candidate storage p=candidates[id];WithdrawalFeeTypes.Verified storage v=verified[id];
        require(bound[id] && !settled[id] && winner[v.root] == 0 && BitcoinTxParser.txId(raw) == v.txid,"fee exact winner");
        BitcoinWithdrawalEscrow.Payment memory paid=escrow.payment(id);
        require(paid.settled && keccak256(abi.encode(paid.requests)) == keccak256(abi.encode(p.requests)),"fee payment receipt");
        uint256 principal;for(uint256 i;i<p.requests.length;++i) {
            BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(p.requests[i]);if(r.token != address(0))principal+=r.amount/1e10;
        }
        require(burned == principal+v.tokenBurn,"fee exact token burn");
        // No new owner call, revision check, issuance fence or external signer
        // policy is allowed to invalidate an already admitted exact winner.
        winner[v.root]=id;settled[id]=true;IWithdrawalFeeCredits ledger=IWithdrawalFeeCredits(credits);
        ledger.settleClaims(v.root,v.txid,p.credits);
        for(uint256 i;i<p.requests.length;++i)if(p.charges[i].revision != 0)ledger.settleBudget(p.requests[i],p.charges[i].sats);
        IUserFeeReserve(msg.sender).settleUserFamily(v.root,v.txid,raw,v.treasuryFee,blockHash);
        emit Settled(v.root,id,v.fee);
    }
}
