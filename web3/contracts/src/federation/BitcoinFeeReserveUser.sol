// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinFeeReserveStorage.sol";
import "./BitcoinAddressRegistry.sol";
import "./WithdrawalFeeTypes.sol";
interface IFeeReserveModuleContext {
    function deposits() external view returns(BitcoinDepositRegistry);
    function escrow() external view returns(BitcoinWithdrawalEscrow);
    function addresses() external view returns(BitcoinAddressRegistry);
    function scope() external view returns(bytes32);
    function walletScriptHash() external view returns(bytes32);
    function authorization() external view returns(FROSTAuthorization);
}
/// @notice Fixed constructor-created reserve module, not replaceable policy.
contract BitcoinFeeReserveUser is BitcoinFeeReserveStorage {
    address public immutable reserve;
    constructor(address reserve_) { reserve=reserve_; }
    function _context() private view returns(IFeeReserveModuleContext) {
        require(address(this) == reserve,"reserve delegate context");return IFeeReserveModuleContext(address(this));
    }
    function _deposits() private view returns(BitcoinDepositRegistry) { return _context().deposits(); }
    function _escrow() private view returns(BitcoinWithdrawalEscrow) { return _context().escrow(); }
    function _addresses() private view returns(BitcoinAddressRegistry) { return _context().addresses(); }
    function _userPayments() private view returns(IWithdrawalFeePayments) {
        address c=_escrow().withdrawalFees();return IWithdrawalFeePayments(c == address(0) ? address(0) : IWithdrawalFeeCredits(c).payments());
    }
    function available() private view returns(uint256) { return freeSats-lockedSats-promisedSats; }
    function _healthy() private view {
        require(report.state == uint8(Health.HEALTHY) && block.timestamp <= report.validUntil
            && report.control.cg == _addresses().custodyGeneration() && report.control.dg == _addresses().derivationGeneration()
            && report.free >= freeSats,"fee reconciliation required");
    }
    function _consume(bytes4 selector,bytes memory data,Control memory c,bytes32 op) private {
        IFeeReserveModuleContext ctx=_context();
        require(_addresses().batches().feeReserve() == address(this) && c.sequence == nonce && block.timestamp <= c.deadline,"fee sequence/window");
        require(c.cg == _addresses().custodyGeneration() && c.dg == _addresses().derivationGeneration(),"fee generation");
        FROSTAuthorization auth=ctx.authorization();
        require(op == auth.hashOperation(address(this),selector,keccak256(abi.encode(ctx.scope(),keccak256(data))),c.sequence),"fee hash");
        auth.consumeOperation(op);receipts[op]=keccak256(data);++nonce;
    }
    function lockUserFamily(bytes32 root,bytes32 grant,bytes32[] calldata keys,uint256 free,uint256 maxDebit) external {
        require(msg.sender == address(_userPayments()) && msg.sender != address(0),"user fee policy");_healthy();
        require(keys.length > 0 && keys.length <= 16,"user input bound");
        Allocation storage a=allocations[grant];uint256 cap;
        if(grant != 0) {
            require(a.active && !a.treasury && maxDebit <= a.cap && maxDebit <= a.maxFee,"user treasury allocation");cap=a.cap;
        } else require(maxDebit == 0,"user treasury source required");
        if(userLocked[root]) {
            require(userGrant[root] == grant && userInputFree[root] == free && userInputHash[root] == keccak256(abi.encode(keys)),"user family collateral changed");return;
        }
        require(free >= cap && free <= available()+cap,"user whole-input collateral");
        if(grant != 0) { require(!a.bound && a.root == 0,"user treasury already bound");a.bound=true;a.root=root;promisedSats-=cap; }
        uint256 actual;
        for(uint256 i;i<keys.length;++i) {
            uint256 at=creditIndex[keys[i]];if(at == 0)continue;
            Credit storage c=credits[at-1];require(c.root == 0,"user credit already locked");c.root=root;actual+=c.free;
            lockedSats+=c.free;rootCredits[root].push(keys[i]);
        }
        require(actual == free,"user input credit mismatch");userLocked[root]=true;userInputFree[root]=free;userGrant[root]=grant;userInputHash[root]=keccak256(abi.encode(keys));
    }
    function settleUserFamily(bytes32 root,bytes32 txid,bytes calldata raw,uint256 debit,bytes32 blockHash) external {
        require(msg.sender == address(_userPayments()) && msg.sender != address(0) && userLocked[root],"user fee policy");
        require(winners[root] == 0,"user winner already settled");
        _unlock(root);bytes32[] storage keys=rootCredits[root];for(uint256 i;i<keys.length;++i)_deleteCredit(keys[i]);
        uint256 freeChange;
        IWithdrawalFeeCredits user=IWithdrawalFeeCredits(_escrow().withdrawalFees());
        uint256 count=BitcoinTxParser.outputCount(raw);bytes32 wallet=_context().walletScriptHash();
        for(uint32 i;i<count;++i) {
            (uint64 sats,bytes memory script)=BitcoinTxParser.outputAt(raw,i);if(keccak256(script) != wallet)continue;
            bytes32 key=keccak256(abi.encode(txid,i));uint256 free=uint256(sats)-_deposits().backingSats(key)-user.protectedSats(key);
            freeChange+=free;_putCredit(txid,i,free,blockHash);
        }
        require(freeChange+debit == userInputFree[root],"user/treasury conservation");
        bytes32 grant=userGrant[root];if(grant != 0)allocations[grant].active=false;
        winners[root]=txid;if(report.state == uint8(Health.HEALTHY))report.state=0;emit Debited(grant,root,debit);
    }
    function releaseUserFamily(bytes32 root) external {
        require(userLocked[root] && _addresses().batches().batchState(root) == BitcoinBatchRegistry.State.Cancelled,"user signature may exist");
        _unlock(root);userLocked[root]=false;bytes32 grant=userGrant[root];
        if(grant != 0) { Allocation storage a=allocations[grant];a.bound=false;a.root=0;promisedSats+=a.cap; }
    }
    function publishReconciliation(Report calldata r, bytes32 op) external {
        require(r.state <= uint8(Health.RECOVERY_REQUIRED) && r.state != 0 && r.tip != 0 && r.evidence != 0, "fee report");
        require(r.validUntil > block.timestamp && r.validUntil <= block.timestamp+3600, "fee observation window");
        if (r.state == uint8(Health.HEALTHY)) {
            require(r.backing == _deposits().totalBackingSats() && r.free == freeSats
                && r.walletSats >= r.backing+r.pending+r.free, "fee principal inventory");
            address pool = _deposits().scriptPools(_context().walletScriptHash());
            require(IERC20(IAddressPoolContext(pool).mpToken()).totalSupply() == r.backing*1e10, "fee supply/backing");
        }
        _consume(this.publishReconciliation.selector,abi.encode(r),r.control,op);
        report=r; emit Reconciled(r.state,r.evidence,r.control.cg,r.control.dg);
    }
}
