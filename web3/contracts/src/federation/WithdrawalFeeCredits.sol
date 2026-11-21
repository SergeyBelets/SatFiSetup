// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.sol";
import "./WithdrawalFeeTypes.sol";

interface IFeeFundingBindings {
    function credits() external view returns(address);
    function hasBinding(bytes32) external view returns(bool);
}

/// @notice Personal fee assets. Owner calls never advance quorum operation nonces.
contract WithdrawalFeeCredits is ReentrancyGuard {
    using SafeERC20 for IERC20;
    uint256 public constant Q=1e10;
    BitcoinAddressRegistry public immutable addresses;
    BitcoinWithdrawalEscrow public immutable escrow;
    address public immutable payments;
    address public immutable funding;
    bytes32 public immutable scope;
    mapping(bytes32=>WithdrawalFeeTypes.Budget) private budgets;
    mapping(bytes32=>WithdrawalFeeTypes.Claim) private claims;
    mapping(bytes32=>bytes32[]) private locations;
    mapping(bytes32=>uint256) private locationIndex;
    mapping(bytes32=>uint256) public protectedSats;
    bytes32[] private liveClaims;
    mapping(bytes32=>uint256) private liveIndex;
    mapping(address=>uint256) public lockedTokens;
    mapping(address=>uint256) public lockedMPFees;
    mapping(address=>uint256) public lockedSFees;
    mapping(bytes32=>mapping(bytes32=>bytes32)) public admitted;
    mapping(bytes32=>bool) public refundRequest;
    mapping(bytes32=>bytes32) public refundOrigin;
    mapping(bytes32=>uint256) public refundCommitted;
    mapping(bytes32=>uint256) public refundSpent;
    mapping(address=>uint256) public refundNonce;
    event Consented(bytes32 indexed request,address indexed owner,uint256 revision,uint8 mode,uint64 cap);
    event CreditIssued(bytes32 indexed id,bytes32 indexed request,bytes32 outpoint,uint64 sats);
    event CreditMoved(bytes32 indexed id,bytes32 outpoint,uint64 sats);
    event BudgetClosed(bytes32 indexed request,uint256 spent,uint256 returned);

    constructor(address registry,address payments_,address funding_) {
        addresses=BitcoinAddressRegistry(registry);escrow=BitcoinWithdrawalEscrow(addresses.escrow());
        require(payments_ != address(0) && funding_ != address(0)
            && IWithdrawalFeePayments(payments_).credits() == address(this)
            && IWithdrawalFeePayments(payments_).escrow() == address(escrow)
            && IFeeFundingBindings(funding_).credits() == address(this),"credit graph");
        payments=payments_;funding=funding_;
        scope=keccak256(abi.encode(keccak256("SATOHSI_WITHDRAWAL_FEE_CREDITS_V1"),block.chainid,address(this),registry,
            address(escrow),address(addresses.batches()),addresses.graphId(),addresses.bitcoinGenesis(),
            address(addresses.dkg()),addresses.walletId(),addresses.walletResult(),addresses.keyEpoch(),payments_,funding_));
    }
    modifier onlyPayments() { require(msg.sender == payments,"payments only");_; }
    function getBudget(bytes32 key) external view returns(WithdrawalFeeTypes.Budget memory) { return budgets[key]; }
    function getClaim(bytes32 id) external view returns(WithdrawalFeeTypes.Claim memory) { return claims[id]; }
    function claimsAt(bytes32 key) external view returns(bytes32[] memory) { return locations[key]; }
    function claimCount() external view returns(uint256) { return liveClaims.length; }
    function claimAt(uint256 i) external view returns(bytes32,WithdrawalFeeTypes.Claim memory) { bytes32 id=liveClaims[i];return(id,claims[id]); }
    function requestHash(bytes32 key) public view returns(bytes32) {
        BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(key);
        require(r.owner != address(0),"withdrawal missing");
        return keccak256(abi.encode(scope,key,r.pool,r.localId,r.token,r.owner,r.amount,r.script));
    }
    function _fence(uint256 cg,uint256 dg) private view {
        require(escrow.withdrawalFees() == address(this),"credits unbound");addresses.requireCustody(cg);
        require(dg == addresses.derivationGeneration(),"fee generation");
    }
    function _simple(address pool) private view {
        require(address(escrow.callbackPoolRegistry()) != address(0)
            && escrow.callbackPoolRegistry().isSimplePool(pool),"fee-only pool admission");
    }
    function _consent(WithdrawalFeeTypes.Consent calldata c,address owner,bool shares) private {
        _fence(c.cg,c.dg);BitcoinWithdrawalEscrow.Request memory r=escrow.getRequest(c.request);
        WithdrawalFeeTypes.Budget storage b=budgets[c.request];
        require(r.owner == owner && r.token != address(0) && (r.state == BitcoinWithdrawalEscrow.State.Pending
            || r.state == BitcoinWithdrawalEscrow.State.Bound) && !b.closed,"fee owner/state");
        require(c.revision == b.revision+1 && c.mode != WithdrawalFeeTypes.Mode.NONE && c.cap > 0
            && c.cap <= IUserFeeReserve(addresses.batches().feeReserve()).protocolFeeCap()
            && c.refundFeeCap <= IUserFeeReserve(addresses.batches().feeReserve()).protocolFeeCap()
            && c.deadline > block.timestamp && c.maxRate > 0,"fee consent bounds");
        require((c.mode == WithdrawalFeeTypes.Mode.S) == shares,"fee source caller");
        require(c.maxTokenDebit == (c.mode == WithdrawalFeeTypes.Mode.BTC ? 0 : uint256(c.cap)*Q),"fee token cap");
        require(r.token == IAddressPoolContext(PoolMpToken(r.token).poolDAO()).mpToken()
            && addresses.deposits() == address(escrow.depositRegistry()),"fee underlying graph");
        require(escrow.depositRegistry().poolScripts(PoolMpToken(r.token).poolDAO()) == keccak256(abi.encodePacked(hex"5120",addresses.baseX())),"fee MP not registered");
        bytes32 root=IWithdrawalFeePayments(payments).rootForRequest(c.request);
        if(b.revision == 0) {
            require(root == 0 && r.state == BitcoinWithdrawalEscrow.State.Pending,"NONE source already admitted");
            b.owner=owner;b.token=r.token;b.pool=c.pool;b.mode=c.mode;b.requestHash=requestHash(c.request);
            b.refundScript=c.refundScript;
        } else {
            require(b.owner == owner && b.requestHash == requestHash(c.request),"fee request changed");
            // Source switching is deliberately explicit cancel/reprepare, never
            // an implicit mixed-source update or an allowance-based change.
            require(b.mode == c.mode && b.pool == c.pool && keccak256(b.refundScript) == keccak256(c.refundScript),"fee source/refund fixed");
            require(c.cap >= b.cap,"fee cap monotonic");
        }
        if(c.mode == WithdrawalFeeTypes.Mode.S) _simple(c.pool);
        else require(c.pool == address(0),"fee owning pool");
        if(c.mode == WithdrawalFeeTypes.Mode.BTC) {
            require(c.refundScript.length == 22 || c.refundScript.length == 34,"fee refund script");
        } else {
            uint256 extra=(uint256(c.cap)-b.cap)*Q;
            if(extra != 0) {
                uint256 beforeBalance=IERC20(r.token).balanceOf(address(this));
                if(shares) {
                    (address who,address token,uint256 amount)=IWithdrawalFeePool(c.pool).feeLock(c.request);
                    require(who == owner && token == r.token && amount == uint256(c.cap)*Q,"fee S lock");
                }
                IERC20(r.token).safeTransferFrom(shares ? c.pool : owner,address(this),extra);
                require(IERC20(r.token).balanceOf(address(this)) == beforeBalance+extra,"fee exact lock");
                b.locked+=extra;lockedTokens[r.token]+=extra;
                if(shares)lockedSFees[r.token]+=extra;else lockedMPFees[r.token]+=extra;
            }
        }
        b.cap=c.cap;b.maxRate=c.maxRate;b.refundFeeCap=c.refundFeeCap;b.deadline=c.deadline;b.revision=c.revision;
        emit Consented(c.request,owner,c.revision,uint8(c.mode),c.cap);
    }
    function consent(WithdrawalFeeTypes.Consent calldata c) external nonReentrant { _consent(c,msg.sender,false); }
    function consentS(address owner,WithdrawalFeeTypes.Consent calldata c) external nonReentrant {
        require(msg.sender == c.pool,"fee pool caller");_simple(msg.sender);_consent(c,owner,true);
    }
    /// @notice Direct owner consent to exact costs for the entire immutable version.
    function approveVersion(bytes32 batch,bytes32 key,uint256 revision,bytes32 termsHash) external {
        WithdrawalFeeTypes.Budget storage b=budgets[key];
        require(msg.sender == b.owner && !b.closed && b.revision == revision && block.timestamp <= b.deadline,"fee exact consent owner");
        require(termsHash != 0,"fee terms");admitted[batch][key]=keccak256(abi.encode(termsHash,revision));
    }
    function bindBudget(bytes32 key,bytes32 root,uint256 revision) external onlyPayments {
        WithdrawalFeeTypes.Budget storage b=budgets[key];require(!b.closed && b.revision == revision,"fee stale candidate");
        require(b.root == 0 || b.root == root,"fee budget in other family");b.root=root;
    }
    function _close(bytes32 key,uint256 sats) private {
        WithdrawalFeeTypes.Budget storage b=budgets[key];require(!b.closed,"fee budget closed");b.closed=true;
        if(b.mode == WithdrawalFeeTypes.Mode.MP || b.mode == WithdrawalFeeTypes.Mode.S) {
            uint256 spent=sats*Q;uint256 held=b.locked;require(spent <= held,"fee lock shortfall");b.locked=0;lockedTokens[b.token]-=held;
            if(b.mode == WithdrawalFeeTypes.Mode.S)lockedSFees[b.token]-=held;else lockedMPFees[b.token]-=held;
            uint256 beforeBalance=IERC20(b.token).balanceOf(address(this));
            if(spent != 0)PoolMpToken(b.token).burnHeld(spent);
            uint256 rest=held-spent;address to=b.mode == WithdrawalFeeTypes.Mode.S ? b.pool : b.owner;
            uint256 recipientBefore=IERC20(b.token).balanceOf(to);
            if(rest != 0)IERC20(b.token).safeTransfer(to,rest);
            require(IERC20(b.token).balanceOf(address(this)) == beforeBalance-held && IERC20(b.token).balanceOf(to) == recipientBefore+rest,"fee exact refund");
            if(b.mode == WithdrawalFeeTypes.Mode.S)IWithdrawalFeePool(b.pool).settleFeeBudget(key,b.owner,b.token,held,spent);
            emit BudgetClosed(key,spent,rest);
        } else {
            require(sats <= b.cap,"fee BTC cap");bytes32 original=refundOrigin[key];
            if(original != 0) { refundCommitted[original]-=b.cap;refundSpent[original]+=sats; }
        }
    }
    function settleBudget(bytes32 key,uint256 sats) external onlyPayments nonReentrant { _close(key,sats); }
    function refundTokens(bytes32 key) external nonReentrant {
        WithdrawalFeeTypes.Budget storage b=budgets[key];require(msg.sender == b.owner,"fee refund owner");
        bytes32 root=IWithdrawalFeePayments(payments).rootForRequest(key);
        require(root == 0 || addresses.batches().batchState(root) == BitcoinBatchRegistry.State.Cancelled,"fee signature may exist");
        require(b.mode == WithdrawalFeeTypes.Mode.MP || b.mode == WithdrawalFeeTypes.Mode.S,"fee token refund only");_close(key,0);
    }
    function _remove(bytes32 id) private {
        WithdrawalFeeTypes.Claim storage c=claims[id];bytes32 at=c.outpoint;uint256 index=locationIndex[id]-1;
        bytes32 last=locations[at][locations[at].length-1];locations[at][index]=last;locationIndex[last]=index+1;
        locations[at].pop();delete locationIndex[id];protectedSats[at]-=c.sats;
    }
    function _place(bytes32 id,bytes32 at,uint64 sats) private {
        require(sats > 0 && locations[at].length < 32,"fee claim capacity");
        claims[id].outpoint=at;claims[id].sats=sats;claims[id].root=0;
        locations[at].push(id);locationIndex[id]=locations[at].length;protectedSats[at]+=sats;
    }
    function issue(bytes32 request,bytes32 txid,uint32 vout,uint64 sats) external {
        require(msg.sender == funding && budgets[request].owner != address(0),"fee issuer");
        bytes32 id=keccak256(abi.encode(scope,txid,vout));require(claims[id].request == 0,"fee duplicate credit");
        claims[id].request=request;_place(id,keccak256(abi.encode(txid,vout)),sats);
        liveClaims.push(id);liveIndex[id]=liveClaims.length;emit CreditIssued(id,request,claims[id].outpoint,sats);
    }
    function lockClaims(bytes32 root,WithdrawalFeeTypes.CreditUse[] calldata uses) external onlyPayments {
        require(uses.length <= 32,"fee claims bound");
        for(uint256 i;i<uses.length;++i) {
            WithdrawalFeeTypes.Claim storage c=claims[uses[i].id];require(c.sats > 0 && (c.root == 0 || c.root == root
                || addresses.batches().batchState(c.root) == BitcoinBatchRegistry.State.Cancelled),"fee credit locked");c.root=root;
        }
    }
    function settleClaims(bytes32 root,bytes32 txid,WithdrawalFeeTypes.CreditUse[] calldata uses) external onlyPayments {
        for(uint256 i;i<uses.length;++i) {
            bytes32 id=uses[i].id;WithdrawalFeeTypes.Claim storage c=claims[id];require(c.root == root,"fee claim root");
            uint64 rest=c.sats-uses[i].fee-uses[i].refund;_remove(id);
            if(rest != 0)_place(id,keccak256(abi.encode(txid,uses[i].carryVout)),rest);
            else {
                uint256 index=liveIndex[id]-1;bytes32 last=liveClaims[liveClaims.length-1];liveClaims[index]=last;liveIndex[last]=index+1;
                liveClaims.pop();delete liveIndex[id];c.sats=0;c.root=0;c.outpoint=0;
            }
            emit CreditMoved(id,c.outpoint,c.sats);
        }
    }
    function createRefund(bytes32 id,bytes32 original,uint64 payout,uint64 cap,uint64 rate,uint64 deadline,uint256 cg,uint256 dg) external {
        _fence(cg,dg);WithdrawalFeeTypes.Budget storage b=budgets[original];
        require(msg.sender == b.owner && b.mode == WithdrawalFeeTypes.Mode.BTC && refundOrigin[original] == 0
            && refundCommitted[original]+refundSpent[original]+cap <= b.refundFeeCap && payout > 0
            && rate > 0 && rate <= b.maxRate && deadline > block.timestamp,"fee refund consent");
        refundCommitted[original]+=cap;
        bytes32 key=keccak256(abi.encode(scope,msg.sender,refundNonce[msg.sender]++,id));require(budgets[key].owner == address(0),"fee refund duplicate");
        escrow.createCreditRefund(key,msg.sender,payout,b.refundScript);refundRequest[key]=true;refundOrigin[key]=original;
        budgets[key]=WithdrawalFeeTypes.Budget(msg.sender,b.token,address(0),WithdrawalFeeTypes.Mode.BTC,1,0,cap,rate,cap,deadline,
            requestHash(key),0,b.refundScript,false);
        emit Consented(key,msg.sender,1,uint8(WithdrawalFeeTypes.Mode.BTC),cap);
    }
    function raiseRefundCap(bytes32 key,uint64 cap,uint256 revision,uint256 cg,uint256 dg) external {
        _fence(cg,dg);WithdrawalFeeTypes.Budget storage b=budgets[key];
        require(msg.sender == b.owner && b.mode == WithdrawalFeeTypes.Mode.BTC && refundOrigin[key] == 0
            && revision == b.revision+1 && cap >= b.refundFeeCap && cap <= IUserFeeReserve(addresses.batches().feeReserve()).protocolFeeCap(),"fee refund cap consent");
        b.refundFeeCap=cap;b.revision=revision;
    }
    function cancelRefund(bytes32 key) external {
        WithdrawalFeeTypes.Budget storage b=budgets[key];require(msg.sender == b.owner && refundOrigin[key] != 0,"fee refund owner");
        bytes32 root=IWithdrawalFeePayments(payments).rootForRequest(key);
        require(root == 0 || addresses.batches().batchState(root) == BitcoinBatchRegistry.State.Cancelled,"fee signature may exist");
        _close(key,0);escrow.cancelCreditRefund(key);
    }
}
