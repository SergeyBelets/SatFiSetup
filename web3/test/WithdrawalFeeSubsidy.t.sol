// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinFeeReserve.t.sol";
import "../contracts/src/federation/WithdrawalFeeCredits.sol";
import "../contracts/src/federation/WithdrawalFeePayments.sol";
import "../contracts/src/federation/WithdrawalFeeFunding.sol";
import "../contracts/src/federation/SimpleMultiPoolDeployer.sol";
import "../contracts/src/federation/BitcoinSigningBoard.sol";
import "../contracts/src/federation/DKGOnchainBoard.sol";
import "./IntentSessionFixture.sol";

// Explicit test-only qualification. Native purpose separation and all money /
// accounting paths are real; DKG/quorum and SPV inherit the marked fixtures.
contract TestQualifiedFeeDerivation {
    BitcoinWithdrawalFeeDerivation public immutable candidate;
    bytes32 public constant FEE_PROFILE=keccak256("BASE_POST_DKG_EVEN_Y__WITHDRAWAL_FEE_TAPLEAF_V1");
    bool public constant FEE_QUALIFIED=true;
    constructor() { candidate=new BitcoinWithdrawalFeeDerivation(); }
    function deriveFee(bytes calldata raw,bytes calldata group) external view returns(BitcoinDepositDerivation.Descriptor memory) {
        return candidate.deriveFee(raw,group);
    }
}
contract WithdrawalFeeSubsidyTest is BitcoinFeeReserveTest {
    WithdrawalFeeCredits internal credits;
    WithdrawalFeePayments internal payments;
    WithdrawalFeeFunding internal funding;
    bytes internal destination=abi.encodePacked(hex"0014",bytes20(uint160(0x123456)));
    bytes internal refundScript=abi.encodePacked(hex"0014",bytes20(uint160(0x987654)));
    function parseInputs(bytes calldata raw) external pure returns(bytes32[] memory,uint32[] memory) { return BitcoinTxParser.parseAllInputs(raw); }
    function _report() internal override {
        uint256 personal;for(uint256 i;i<credits.claimCount();++i) { (,WithdrawalFeeTypes.Claim memory c)=credits.claimAt(i);personal+=c.sats; }
        uint256 backing=common.totalBackingSats();uint256 free=fees.freeSats();
        BitcoinFeeReserveStorage.Report memory r=BitcoinFeeReserveStorage.Report(_c(),1,2000,bytes32(uint256(123)),uint64(block.timestamp+300),
            backing+free+personal,backing,personal,free,keccak256("explicit observed Core fixture incl personal BTC"));
        fees.publishReconciliation(r,_op(fees.publishReconciliation.selector,abi.encode(r)));
    }
    function setUp() public override {
        _setUpSubsidy(6);
    }
    function _setUpSubsidy(uint32 confirmations) internal {
        _setUpFeeReserve(confirmations);WithdrawalFeePlanVerifier verifier=new WithdrawalFeePlanVerifier();
        TestQualifiedFeeDerivation derivation=new TestQualifiedFeeDerivation();
        address expected=vm.computeCreateAddress(address(this),vm.getNonce(address(this))+2);
        payments=new WithdrawalFeePayments(address(addresses),expected,address(verifier));
        funding=new WithdrawalFeeFunding(address(addresses),expected,address(derivation));
        credits=new WithdrawalFeeCredits(address(addresses),address(payments),address(funding));assertEq(address(credits),expected);
        bytes32 op=escrow.getSetWithdrawalFeesOperationHash(address(credits));_approve(op,address(escrow));escrow.setWithdrawalFees(address(credits),op);
    }
    function _consent(bytes32 key,WithdrawalFeeTypes.Mode mode,uint64 cap,address ownPool,uint256 revision) internal view returns(WithdrawalFeeTypes.Consent memory) {
        return WithdrawalFeeTypes.Consent(key,revision,mode,ownPool,cap,mode == WithdrawalFeeTypes.Mode.BTC ? 0 : uint256(cap)*1e10,
            1000,100,uint64(block.timestamp+3600),addresses.custodyGeneration(),addresses.derivationGeneration(),mode == WithdrawalFeeTypes.Mode.BTC ? refundScript : bytes(""));
    }
    function _mpConsent(bytes32 key,uint64 cap,uint256 revision) internal {
        vm.startPrank(RECIPIENT);pool.mpToken().approve(address(credits),uint256(cap)*1e10);credits.consent(_consent(key,WithdrawalFeeTypes.Mode.MP,cap,address(0),revision));vm.stopPrank();
    }
    function _rawUser(bytes32 previous,uint64 change) internal view returns(bytes memory) {
        return bytes.concat(hex"0200000001",abi.encodePacked(previous),hex"0000000000fdffffff02",_le(500,8),_compact(destination.length),destination,
            _le(change,8),_compact(pool.payoutScript().length),pool.payoutScript(),hex"00000000");
    }
    function _candidate(bytes32 id,bytes32 previous,bytes32 request,bytes memory raw,uint64 cost,uint256 revision) internal view returns(WithdrawalFeeTypes.Candidate memory p) {
        p.control=WithdrawalFeeTypes.Control(payments.nonce(),addresses.custodyGeneration(),addresses.derivationGeneration(),uint64(block.timestamp+3600));
        p.batch=id;p.previous=previous;p.planHash=keccak256(abi.encode("explicit public plan fixture",raw,id));p.raw=raw;
        p.requests=new bytes32[](1);p.requests[0]=request;p.charges=new WithdrawalFeeTypes.Charge[](1);p.charges[0]=WithdrawalFeeTypes.Charge(request,revision,cost);
        p.credits=new WithdrawalFeeTypes.CreditUse[](0);
    }
    function _prepareUser(WithdrawalFeeTypes.Candidate memory p) internal {
        bytes32 op=payments.getPrepareOperationHash(p);_approve(op,address(payments));payments.prepare(p,op);
    }
    function _approveOwner(bytes32 batch,bytes32 key,uint256 revision) internal {
        bytes32 terms=payments.termsHash(batch);vm.prank(RECIPIENT);credits.approveVersion(batch,key,revision,terms);
    }
    function _reserveUser(WithdrawalFeeTypes.Candidate memory p,bytes32 previousTx) internal {
        bytes32 op;
        if(p.previous == 0) {
            bytes32[] memory keys=new bytes32[](1);keys[0]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(previousTx),uint32(0)));
            op=batches.operationHash(p.batch,p.planHash,keys,p.control.deadline);_approve(op,address(batches));batches.reserve(p.batch,p.planHash,keys,p.control.deadline,op);
        } else {
            op=batches.getReplacementOperationHash(p.previous,p.batch,p.planHash,p.control.deadline);_approve(op,address(batches));
            batches.reserveReplacement(p.previous,p.batch,p.planHash,p.control.deadline,op);
        }
    }
    function _bindUser(WithdrawalFeeTypes.Candidate memory p) internal {
        bytes32 op;
        if(p.previous == 0) {
            op=escrow.getBindOperationHash(p.batch,_txid(p.raw),p.requests);_approve(op,address(escrow));escrow.bind(p.batch,_txid(p.raw),p.requests,op);
        } else {
            op=escrow.getBindReplacementOperationHash(p.previous,p.batch,_txid(p.raw));_approve(op,address(escrow));escrow.bindReplacement(p.previous,p.batch,_txid(p.raw),op);
        }
        vm.prank(address(0xa1));batches.beginSigning(p.batch);
    }
    function testSubsidyMPExactConsentBurnsOnlyActualFeeAndRefundsCap() public {
        bytes32 txid=_minted();bytes32 request=_request();_mpConsent(request,100,1);_report();
        assertEq(pool.mpToken().balanceOf(address(credits)),100*1e10);assertEq(common.totalBackingSats(),1000);
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("MP version"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);
        _approveOwner(p.batch,request,1);_reserveUser(p,txid);_bindUser(p);
        escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(pool.mpToken().totalSupply(),490*1e10);assertEq(common.totalBackingSats(),490);
        assertEq(pool.mpToken().balanceOf(RECIPIENT),490*1e10);assertEq(pool.mpToken().balanceOf(address(credits)),0);
        assertEq(fees.freeSats(),0);assertTrue(credits.getBudget(request).closed);
    }
    function testSubsidyAllowanceAndQuorumCannotReplaceOwnerVersionConsent() public {
        bytes32 txid=_minted();bytes32 request=_request();_mpConsent(request,100,1);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("no consent"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);
        bytes32[] memory keys=new bytes32[](1);keys[0]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid),uint32(0)));
        bytes32 op=batches.operationHash(p.batch,p.planHash,keys,p.control.deadline);_approve(op,address(batches));
        vm.expectRevert("fee exact owner consent required");batches.reserve(p.batch,p.planHash,keys,p.control.deadline,op);
        assertEq(batches.reservations(keys[0]),0);assertFalse(payments.bound(p.batch));
    }
    function testSubsidyOldBoundRevisionSurvivesIncreaseAndOldWinnerAfterPause() public {
        bytes32 txid=_minted();bytes32 request=_request();_mpConsent(request,100,1);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("old MP version"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);
        _approveOwner(p.batch,request,1);_reserveUser(p,txid);
        _mpConsent(request,200,2);_bindUser(p);
        WithdrawalFeeTypes.Candidate memory next=_candidate(keccak256("new MP version"),p.batch,request,_rawUser(txid,480),20,2);_prepareUser(next);
        _approveOwner(next.batch,request,2);_reserveUser(next,txid);_bindUser(next);
        vm.warp(block.timestamp+4000);_status(0,2,false);
        escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(credits.getBudget(request).locked,0);assertEq(pool.mpToken().balanceOf(RECIPIENT),490*1e10);
        assertEq(payments.winner(p.batch),p.batch);assertEq(common.totalBackingSats(),490);
        vm.expectRevert();escrow.settle(next.batch,next.raw,bytes32(uint256(1)),new bytes32[](0),0);
    }
    function testSubsidyTokenRefundCannotUnlockPotentialSignatureOrChangeOtherNonce() public {
        bytes32 txid=_minted();bytes32 request=_request();uint256 beforeNonce=escrow.nonce();uint256 beforeAuth=payments.nonce();
        _mpConsent(request,100,1);assertEq(escrow.nonce(),beforeNonce);assertEq(payments.nonce(),beforeAuth);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("locked MP"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);
        _approveOwner(p.batch,request,1);_reserveUser(p,txid);_bindUser(p);
        vm.prank(RECIPIENT);vm.expectRevert("fee signature may exist");credits.refundTokens(request);
        assertEq(credits.getBudget(request).locked,100*1e10);
    }
    function _simplePool() internal returns(SimpleMultiPool s) {
        FROSTAuthorizationDeployer fd=new FROSTAuthorizationDeployer();MultiPoolShareTokenDeployer sd=new MultiPoolShareTokenDeployer();
        SimpleMultiPoolDeployer factory=SimpleMultiPoolDeployer(deployCode("SimpleMultiPoolDeployer.sol:SimpleMultiPoolDeployer",abi.encode(address(fd),address(sd))));
        sd.setAuthorizedDeployer(address(factory),true);
        (address p,)=factory.deployWithAuthorization("fee owning pool",address(auth),"Fee shares","S");s=SimpleMultiPool(p);
        bytes32 op=s.getSetWithdrawalEscrowOperationHash(address(escrow));_approve(op,p);s.setWithdrawalEscrow(address(escrow),op);
        CallbackPoolRegistry callbacks=new CallbackPoolRegistry(address(escrow),address(auth),address(factory),address(0));
        op=escrow.getSetCallbackPoolRegistryOperationHash(address(callbacks));_approve(op,address(escrow));escrow.setCallbackPoolRegistry(address(callbacks),op);
        op=s.getAddPoolOperationHash(address(pool.mpToken()),"real MP");_approve(op,p);s.addPool(address(pool.mpToken()),"real MP",op);
        assertTrue(callbacks.isSimplePool(p));
    }
    function testSubsidySFeeOnlyForMPPrincipalPreservesPerMPClaimAndNoServiceFee() public {
        bytes32 txid=_minted();SimpleMultiPool s=_simplePool();
        vm.startPrank(RECIPIENT);pool.mpToken().approve(address(s),400*1e10);s.deposit(address(pool.mpToken()),400*1e10);vm.stopPrank();
        bytes32 request=_request();WithdrawalFeeTypes.Consent memory c=_consent(request,WithdrawalFeeTypes.Mode.S,100,address(s),1);
        uint256 poolNonce=s.operationNonce();vm.prank(RECIPIENT);s.consentFee(c);assertEq(s.operationNonce(),poolNonce);
        assertEq(s.pendingUserPoolShares(RECIPIENT,address(pool.mpToken())),100*1e10);assertEq(s.escrowedMP(address(pool.mpToken())),100*1e10);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("fee only S"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);
        _approveOwner(p.batch,request,1);_reserveUser(p,txid);_bindUser(p);escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(s.userPoolBalances(RECIPIENT,address(pool.mpToken())),390*1e10);assertEq(s.shareToken().balanceOf(RECIPIENT),390*1e10);
        assertEq(s.pendingUserPoolShares(RECIPIENT,address(pool.mpToken())),0);assertEq(s.escrowedMP(address(pool.mpToken())),0);
        assertEq(pool.mpToken().balanceOf(address(s)),390*1e10);assertEq(s.getPoolInfo(address(pool.mpToken())).totalDeposited,390*1e10);
        assertEq(s.accruedFees(address(pool.mpToken()),false),0);assertEq(s.withdrawalCounter(),0);assertEq(common.totalBackingSats(),490);
    }
    function testSubsidySPrincipalAndFeeHaveIndependentAtomicCallbacks() public {
        bytes32 txid=_minted();SimpleMultiPool s=_simplePool();
        vm.startPrank(RECIPIENT);pool.mpToken().approve(address(s),1000*1e10);s.deposit(address(pool.mpToken()),1000*1e10);
        uint256 id=s.requestWithdrawal(address(pool.mpToken()),500*1e10,destination);vm.stopPrank();
        bytes32 request=escrow.requestKey(address(s),id);bytes32 op=s.getProcessWithdrawalOperationHash(id);_approve(op,address(s));s.processWithdrawal(id,op);
        WithdrawalFeeTypes.Consent memory consent=_consent(request,WithdrawalFeeTypes.Mode.S,100,address(s),1);
        vm.prank(RECIPIENT);s.consentFee(consent);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("principal and fee S"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);
        _approveOwner(p.batch,request,1);_reserveUser(p,txid);_bindUser(p);escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(s.shareToken().totalSupply(),490*1e10);assertEq(s.userPoolBalances(RECIPIENT,address(pool.mpToken())),490*1e10);
        assertEq(s.escrowedMP(address(pool.mpToken())),0);assertEq(s.pendingUserPoolShares(RECIPIENT,address(pool.mpToken())),0);
        assertEq(s.getPoolInfo(address(pool.mpToken())).totalDeposited,490*1e10);assertTrue(s.getWithdrawal(id).isProcessed);
    }
    function _btcBinding(bytes32 request) internal returns(bytes memory script) {
        WithdrawalFeeTypes.Consent memory consent=_consent(request,WithdrawalFeeTypes.Mode.BTC,100,address(0),1);
        vm.prank(RECIPIENT);credits.consent(consent);
        bytes memory parent=abi.encodePacked("SATOHSI_DEPOSIT_INTENT_V1\x00",uint16(1),uint16(1),GENESIS,uint256(block.chainid),
            address(addresses),address(pool),address(pool.mpToken()),addresses.graphId(),address(dkg),dkg.BTC_ID(),dkg.BTC_RESULT(),uint64(9),
            RECIPIENT,RECIPIENT,keccak256(abi.encode(funding.scope(),request)),SERVICE);
        bytes memory raw=abi.encodePacked("SATOHSI_WITHDRAWAL_FEE_V1\x00",parent,request,keccak256(refundScript),credits.scope());
        WithdrawalFeeTypes.Control memory c=WithdrawalFeeTypes.Control(funding.nonce(),addresses.custodyGeneration(),addresses.derivationGeneration(),uint64(block.timestamp+3600));
        bytes32 op=funding.getRegisterOperationHash(request,raw,c);_approve(op,address(funding));funding.register(request,raw,c,op);
        WithdrawalFeeFunding.Binding memory b=funding.binding(funding.requestBinding(request));return abi.encodePacked(hex"5120",b.descriptor.outputKey);
    }
    function _topup(bytes memory script,uint256 tag) internal returns(bytes32 txid,bytes32 id) {
        bytes memory source=_funding(1,1,0,destination,bytes32(tag),false);common.cacheValue(source,0);
        bytes memory raw=_funding(1,1,0,script,_txid(source),false);funding.classify(raw,0,16);
        funding.admit(raw,0,bytes32(uint256(1)),new bytes32[](0),0,addresses.custodyGeneration(),addresses.derivationGeneration());
        txid=_txid(raw);id=keccak256(abi.encode(credits.scope(),txid,uint32(0)));
    }
    function _btcPayment(bytes32 principalTx,bytes32 feeTx,uint256 externalRefund,uint256 change) internal view returns(bytes memory) {
        bytes memory raw=bytes.concat(hex"0200000002",abi.encodePacked(principalTx),hex"0000000000fdffffff",abi.encodePacked(feeTx),hex"0000000000fdffffff",
            externalRefund == 0 ? bytes(hex"02") : bytes(hex"03"),_le(500,8),_compact(destination.length),destination);
        if(externalRefund != 0)raw=bytes.concat(raw,_le(externalRefund,8),_compact(refundScript.length),refundScript);
        return bytes.concat(raw,_le(change,8),_compact(pool.payoutScript().length),pool.payoutScript(),hex"00000000");
    }
    function _btcReserve(WithdrawalFeeTypes.Candidate memory p) internal {
        (bytes32[] memory prev,uint32[] memory vouts)=this.parseInputs(p.raw);bytes32[] memory keys=new bytes32[](prev.length);
        for(uint256 i;i<keys.length;++i)keys[i]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[i]),vouts[i]));
        bytes32 op=batches.operationHash(p.batch,p.planHash,keys,p.control.deadline);_approve(op,address(batches));batches.reserve(p.batch,p.planHash,keys,p.control.deadline,op);
    }
    function testSubsidyBTCInlineRefundNeverMintsAndThirdPartyPresenterDoesNotOwnCredit() public {
        bytes32 principalTx=_minted();bytes32 request=_request();bytes memory script=_btcBinding(request);(bytes32 feeTx,bytes32 id)=_topup(script,81);_report();
        assertEq(pool.mpToken().totalSupply(),1000*1e10);assertEq(credits.getBudget(request).owner,RECIPIENT);
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("BTC inline refund"),0,request,_btcPayment(principalTx,feeTx,990,500),10,1);
        p.credits=new WithdrawalFeeTypes.CreditUse[](1);p.credits[0]=WithdrawalFeeTypes.CreditUse(id,request,10,990,2);
        _prepareUser(p);_approveOwner(p.batch,request,1);_btcReserve(p);_bindUser(p);escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(credits.getClaim(id).sats,0);assertEq(common.totalBackingSats(),500);assertEq(pool.mpToken().totalSupply(),500*1e10);assertEq(fees.freeSats(),0);
    }
    function testSubsidyBTCNamedCarryAndSeparateRefundConserveBacking() public {
        bytes32 principalTx=_minted();bytes32 request=_request();bytes memory script=_btcBinding(request);(bytes32 feeTx,bytes32 id)=_topup(script,82);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("BTC carry"),0,request,_btcPayment(principalTx,feeTx,0,1490),10,1);
        p.credits=new WithdrawalFeeTypes.CreditUse[](1);p.credits[0]=WithdrawalFeeTypes.CreditUse(id,request,10,0,1);
        _prepareUser(p);_approveOwner(p.batch,request,1);_btcReserve(p);_bindUser(p);escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        bytes32 change=keccak256(abi.encode(_txid(p.raw),uint32(1)));assertEq(credits.protectedSats(change),990);assertEq(common.backingSats(change),500);assertEq(fees.freeSats(),0);
        bytes32 refundId=keccak256("explicit separate refund");bytes32 key=keccak256(abi.encode(credits.scope(),RECIPIENT,uint256(0),refundId));
        vm.startPrank(RECIPIENT);credits.createRefund(refundId,request,980,10,1000,uint64(block.timestamp+3600),addresses.custodyGeneration(),addresses.derivationGeneration());vm.stopPrank();_report();
        bytes memory raw=bytes.concat(hex"0200000001",abi.encodePacked(_txid(p.raw)),hex"0100000000fdffffff02",_le(980,8),_compact(refundScript.length),refundScript,
            _le(500,8),_compact(pool.payoutScript().length),pool.payoutScript(),hex"00000000");
        WithdrawalFeeTypes.Candidate memory r=_candidate(keccak256("refund family"),0,key,raw,10,1);r.credits=new WithdrawalFeeTypes.CreditUse[](1);
        r.credits[0]=WithdrawalFeeTypes.CreditUse(id,key,10,980,1);_prepareUser(r);_approveOwner(r.batch,key,1);_btcReserve(r);_bindUser(r);
        escrow.settle(r.batch,r.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(credits.getClaim(id).sats,0);assertEq(credits.refundSpent(request),10);assertEq(credits.refundCommitted(request),0);
        assertEq(common.totalBackingSats(),500);assertEq(pool.mpToken().totalSupply(),500*1e10);
    }
    function testSubsidyStaleCandidateRejectedBeforeAdmissionAndUnrelatedConsentIsIsolated() public {
        bytes32 txid=_minted();bytes32 request=_request();_mpConsent(request,100,1);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("stale"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);_approveOwner(p.batch,request,1);
        _mpConsent(request,100,2);bytes32[] memory keys=new bytes32[](1);keys[0]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid),uint32(0)));
        bytes32 operation=batches.operationHash(p.batch,p.planHash,keys,p.control.deadline);_approve(operation,address(batches));
        vm.expectRevert("fee candidate revision/cap");batches.reserve(p.batch,p.planHash,keys,p.control.deadline,operation);
        assertFalse(payments.bound(p.batch));assertEq(credits.getBudget(request).locked,100*1e10);
        p.batch=keccak256("current");p.control.sequence=payments.nonce();p.charges[0].revision=2;_prepareUser(p);_approveOwner(p.batch,request,2);
        vm.startPrank(RECIPIENT);pool.mpToken().approve(address(escrow),300*1e10);escrow.requestDirect(8,address(pool.mpToken()),300*1e10,destination);vm.stopPrank();
        bytes32 other=escrow.requestKey(RECIPIENT,8);uint256 sequence=payments.nonce();uint256 reserveNonce=fees.nonce();_mpConsent(other,1,1);
        assertEq(payments.nonce(),sequence);assertEq(fees.nonce(),reserveNonce);_reserveUser(p,txid);_bindUser(p);
    }
    function testSubsidyOwnerAndGenerationChecksRejectWithoutMovingPrincipal() public {
        _minted();bytes32 request=_request();WithdrawalFeeTypes.Consent memory c=_consent(request,WithdrawalFeeTypes.Mode.MP,100,address(0),1);
        vm.expectRevert("fee owner/state");credits.consent(c);
        c.cg+=1;vm.startPrank(RECIPIENT);pool.mpToken().approve(address(credits),100*1e10);vm.expectRevert();credits.consent(c);vm.stopPrank();
        assertEq(credits.lockedTokens(address(pool.mpToken())),0);assertEq(common.totalBackingSats(),1000);assertEq(pool.mpToken().balanceOf(address(escrow)),500*1e10);
    }
    function testSubsidyUnadmittedTokenRefundAndSBudgetRestoreSameAssets() public {
        _minted();SimpleMultiPool s=_simplePool();vm.startPrank(RECIPIENT);pool.mpToken().approve(address(s),400*1e10);s.deposit(address(pool.mpToken()),400*1e10);vm.stopPrank();
        bytes32 request=_request();WithdrawalFeeTypes.Consent memory c=_consent(request,WithdrawalFeeTypes.Mode.S,100,address(s),1);
        vm.prank(RECIPIENT);s.consentFee(c);vm.prank(RECIPIENT);credits.refundTokens(request);
        assertEq(s.shareToken().balanceOf(RECIPIENT),400*1e10);assertEq(pool.mpToken().balanceOf(address(s)),400*1e10);
        assertEq(s.pendingUserPoolShares(RECIPIENT,address(pool.mpToken())),0);assertEq(s.escrowedMP(address(pool.mpToken())),0);assertEq(s.getPoolInfo(address(pool.mpToken())).totalDeposited,400*1e10);
        assertEq(pool.mpToken().balanceOf(RECIPIENT),100*1e10);assertEq(pool.mpToken().balanceOf(address(escrow)),500*1e10);assertEq(common.totalBackingSats(),1000);
    }
    function testSubsidyRefundCapsAreCumulativeAndCancellationClosesTypedObligation() public {
        _minted();bytes32 original=_request();_btcBinding(original);bytes32 id=keccak256("refund cap");
        bytes32 key=keccak256(abi.encode(credits.scope(),RECIPIENT,uint256(0),id));uint256 cg=addresses.custodyGeneration();uint256 dg=addresses.derivationGeneration();
        vm.startPrank(RECIPIENT);credits.createRefund(id,original,500,60,1000,uint64(block.timestamp+100),cg,dg);
        vm.expectRevert("fee refund consent");credits.createRefund(bytes32(uint256(2)),original,500,41,1000,uint64(block.timestamp+100),cg,dg);
        credits.cancelRefund(key);vm.stopPrank();assertEq(credits.refundCommitted(original),0);assertEq(uint8(escrow.getRequest(key).state),uint8(BitcoinWithdrawalEscrow.State.Refunded));
        assertTrue(credits.getBudget(key).closed);assertEq(common.totalBackingSats(),1000);
    }
    function testSubsidyInternalIngressIsRejectedBeforeCreditEvenWithoutRecordSpend() public {
        bytes32 principalTx=_minted();bytes32 request=_request();bytes memory script=_btcBinding(request);
        bytes memory raw=_funding(1,1,0,script,principalTx,false);
        vm.expectRevert("fee internal custody ingress");funding.classify(raw,0,16);assertEq(credits.claimCount(),0);
        bytes memory unknown=_funding(1,1,0,script,bytes32(uint256(777)),false);
        vm.expectRevert("fee input context unavailable");funding.classify(unknown,0,16);assertEq(credits.claimCount(),0);
    }
    function testSubsidyExternal257InputsClassifiedInBoundedChunksWithoutOwnershipProof() public {
        _minted();bytes32 request=_request();bytes memory script=_btcBinding(request);
        bytes memory source=_funding(1,257,256,destination,bytes32(uint256(888)),false);
        for(uint32 i;i<257;++i)common.cacheValue(source,i);
        bytes memory raw=_funding(257,33,32,script,_txid(source),false);
        for(uint256 i;i<17;++i)funding.classify(raw,32,16);
        assertEq(funding.classified(_txid(raw)),257);funding.admit(raw,32,bytes32(uint256(1)),new bytes32[](0),0,addresses.custodyGeneration(),addresses.derivationGeneration());
        assertEq(credits.claimCount(),1);assertEq(pool.mpToken().totalSupply(),1000*1e10);
    }
    function _gasNodes() internal view returns(address[] memory nodes) {
        nodes=new address[](15);nodes[0]=address(payments);nodes[1]=address(credits);nodes[2]=address(addresses);nodes[3]=address(common);
        nodes[4]=address(fees);nodes[5]=address(fees.userModule());nodes[6]=address(batches);nodes[7]=address(escrow);nodes[8]=address(auth);
        nodes[9]=address(pool);nodes[10]=address(pool.mpToken());nodes[11]=address(payments.verifier());nodes[12]=address(addresses.verifier());nodes[13]=address(proof);nodes[14]=address(funding);
    }
    mapping(bytes32=>uint256) private gasSeen;
    uint256 private gasRun;
    function _gasBound(string memory label,uint256 spent,uint256 calldataBytes,address[] memory nodes) internal {
        // forge --isolate (also pinned in foundry.toml) already starts each
        // action with cold storage/accounts. Do not charge cold SLOAD twice.
        // Add intrinsic gas, all-nonzero calldata and an extra account margin.
        // Gasleft is gross execution gas, before transaction refunds.
        // Only external monetary/publication calls use this bound. Isolated
        // CREATE is measured separately by the tracer, not caller gasleft.
        uint256 upper=spent+21000+16*calldataBytes+2600*nodes.length;
        ++gasRun;uint256 slots;
        for(uint256 i;i<nodes.length;++i) {
            (bytes32[] memory reads,bytes32[] memory writes)=vm.accesses(nodes[i]);
            for(uint256 j;j<reads.length;++j) { bytes32 k=keccak256(abi.encode(nodes[i],reads[j]));if(gasSeen[k] != gasRun) { gasSeen[k]=gasRun;++slots; } }
            for(uint256 j;j<writes.length;++j) { bytes32 k=keccak256(abi.encode(nodes[i],writes[j]));if(gasSeen[k] != gasRun) { gasSeen[k]=gasRun;++slots; } }
        }
        emit log_named_uint("measured isolated cold gross",spent);emit log_named_uint("unique touched slots",slots);
        emit log_named_uint(label,upper);assertLt(upper,15_000_000,label);
    }
    function testSubsidyBounded16Inputs32Requests32OutputsMonetaryGas() public {
        bytes32[] memory prev=new bytes32[](16);
        for(uint256 i;i<16;++i) { bytes32 id=_register(100+i);bytes memory source=_funding(1,1,0,_script(id),bytes32(1000+i),false);_mint(source,_claim(id,source,0));prev[i]=_txid(source); }
        bytes32[] memory refs=new bytes32[](32);
        for(uint256 i;i<32;++i) {
            bytes memory dst=abi.encodePacked(hex"0014",bytes20(uint160(100+i%31)));
            vm.startPrank(RECIPIENT);pool.mpToken().approve(address(escrow),480*1e10);escrow.requestDirect(100+i,address(pool.mpToken()),480*1e10,dst);vm.stopPrank();
            refs[i]=escrow.requestKey(RECIPIENT,100+i);_mpConsent(refs[i],1,1);
        }
        for(uint256 i=1;i<32;++i)for(uint256 j=i;j>0 && refs[j] < refs[j-1];--j) { bytes32 t=refs[j];refs[j]=refs[j-1];refs[j-1]=t; }
        bytes memory raw=hex"0200000010";for(uint256 i;i<16;++i)raw=bytes.concat(raw,abi.encodePacked(prev[i]),hex"0000000000fdffffff");raw=bytes.concat(raw,hex"20");
        for(uint256 i;i<31;++i)raw=bytes.concat(raw,_le(i == 0 ? 960 : 480,8),hex"16",abi.encodePacked(hex"0014",bytes20(uint160(100+i))));
        raw=bytes.concat(raw,_le(608,8),hex"22",pool.payoutScript(),hex"00000000");_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("16/32/32"),0,refs[0],raw,1,1);p.requests=refs;p.charges=new WithdrawalFeeTypes.Charge[](32);
        for(uint256 i;i<32;++i)p.charges[i]=WithdrawalFeeTypes.Charge(refs[i],1,1);
        address[] memory nodes=_gasNodes();bytes32 op=payments.getPrepareOperationHash(p);_approve(op,address(payments));vm.record();uint256 gasBefore=gasleft();payments.prepare(p,op);
        _gasBound("prepare 16/32/32 cold upper",gasBefore-gasleft(),abi.encodeCall(payments.prepare,(p,op)).length,nodes);
        for(uint256 i;i<32;++i)_approveOwner(p.batch,refs[i],1);
        bytes32[] memory keys=new bytes32[](16);for(uint256 i;i<16;++i)keys[i]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[i]),uint32(0)));
        op=batches.operationHash(p.batch,p.planHash,keys,p.control.deadline);_approve(op,address(batches));vm.record();gasBefore=gasleft();batches.reserve(p.batch,p.planHash,keys,p.control.deadline,op);
        _gasBound("reserve 16/32/32 cold upper",gasBefore-gasleft(),abi.encodeCall(batches.reserve,(p.batch,p.planHash,keys,p.control.deadline,op)).length,nodes);
        op=escrow.getBindOperationHash(p.batch,_txid(raw),refs);_approve(op,address(escrow));vm.record();gasBefore=gasleft();escrow.bind(p.batch,_txid(raw),refs,op);
        _gasBound("bind 16/32/32 cold upper",gasBefore-gasleft(),abi.encodeCall(escrow.bind,(p.batch,_txid(raw),refs,op)).length,nodes);
        vm.prank(address(0xa1));batches.beginSigning(p.batch);vm.record();gasBefore=gasleft();escrow.settle(p.batch,raw,bytes32(uint256(1)),new bytes32[](0),0);
        _gasBound("settle 16/32/32 cold upper",gasBefore-gasleft(),raw.length+256,nodes);assertEq(common.totalBackingSats(),608);assertEq(pool.mpToken().totalSupply(),608*1e10);
    }
    function testSubsidy16Versions1000FreeRevisionsDoNotGrowWinnerSettlement() public {
        bytes32 txid=_minted();bytes32 request=_request();_mpConsent(request,100,1);_report();WithdrawalFeeTypes.Candidate memory first;
        bytes32 previous;
        for(uint64 i=1;i<=16;++i) {
            WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256(abi.encode("bounded versions",i)),previous,request,_rawUser(txid,500-i),i,1);
            _prepareUser(p);_approveOwner(p.batch,request,1);_reserveUser(p,txid);_bindUser(p);if(i == 1)first=p;previous=p.batch;
        }
        WithdrawalFeeTypes.Candidate memory extra=_candidate(keccak256("17th version"),previous,request,_rawUser(txid,483),17,1);
        vm.expectRevert("fee family versions");payments.prepare(extra,bytes32(0));
        for(uint256 revision=2;revision<=1001;++revision)_mpConsent(request,100,revision);
        address[] memory nodes=_gasNodes();vm.record();uint256 gasBefore=gasleft();escrow.settle(first.batch,first.raw,bytes32(uint256(1)),new bytes32[](0),0);
        _gasBound("settle after 16 versions / 1000 revisions cold upper",gasBefore-gasleft(),first.raw.length+256,nodes);assertEq(common.totalBackingSats(),499);
    }
    function testSubsidyLateBTCCreditsSurviveSignedWinnerAndClosedBudget() public {
        bytes32 principalTx=_minted();bytes32 request=_request();bytes memory script=_btcBinding(request);(bytes32 feeTx,bytes32 id)=_topup(script,910);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("late credit family"),0,request,_btcPayment(principalTx,feeTx,990,500),10,1);
        p.credits=new WithdrawalFeeTypes.CreditUse[](1);p.credits[0]=WithdrawalFeeTypes.CreditUse(id,request,10,990,2);
        _prepareUser(p);_approveOwner(p.batch,request,1);_btcReserve(p);_bindUser(p);(,bytes32 late)=_topup(script,911);
        escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);assertEq(credits.getClaim(late).sats,1000);assertTrue(credits.getBudget(request).closed);
        (,bytes32 later)=_topup(script,912);assertEq(credits.getClaim(later).sats,1000);assertEq(credits.claimCount(),2);assertEq(common.totalBackingSats(),500);
    }
    function testSubsidyCallbackFailureRollsBackFeePrincipalAndWinnerAtomically() public {
        bytes32 txid=_minted();SimpleMultiPool s=_simplePool();vm.startPrank(RECIPIENT);pool.mpToken().approve(address(s),400*1e10);s.deposit(address(pool.mpToken()),400*1e10);vm.stopPrank();
        bytes32 request=_request();WithdrawalFeeTypes.Consent memory c=_consent(request,WithdrawalFeeTypes.Mode.S,100,address(s),1);vm.prank(RECIPIENT);s.consentFee(c);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("atomic S callback"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);_approveOwner(p.batch,request,1);_reserveUser(p,txid);_bindUser(p);
        vm.mockCallRevert(address(s),abi.encodeWithSelector(IWithdrawalFeePool.settleFeeBudget.selector),abi.encodeWithSignature("Error(string)","explicit callback failure fixture"));
        vm.expectRevert("explicit callback failure fixture");escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(payments.winner(p.batch),0);assertFalse(escrow.payment(p.batch).settled);assertFalse(credits.getBudget(request).closed);
        assertEq(common.totalBackingSats(),1000);assertEq(pool.mpToken().totalSupply(),1000*1e10);assertEq(s.pendingUserPoolShares(RECIPIENT,address(pool.mpToken())),100*1e10);
        vm.clearMockedCalls();escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);assertEq(common.totalBackingSats(),490);
    }
    function testSubsidyBounded32NamedClaims16MixedLocationsRefundGas() public {
        _minted();bytes32 original=_request();_btcBinding(original);
        bytes memory carrier=bytes.concat(hex"0200000001",abi.encodePacked(bytes32(uint256(999))),hex"0000000000ffffffff10");
        for(uint256 i;i<16;++i)carrier=bytes.concat(carrier,_le(2000,8),hex"22",pool.payoutScript());carrier=bytes.concat(carrier,hex"00000000");
        bytes32 txid=_txid(carrier);WithdrawalFeeTypes.CreditUse[] memory historical=new WithdrawalFeeTypes.CreditUse[](32);bytes32 history=keccak256("GAS ONLY historical issued rights fixture");
        // GAS ONLY boundary: authority impersonation supplies already-issued,
        // carried rights. Real external ingress is exercised separately above.
        for(uint32 i;i<32;++i) {
            bytes32 source=bytes32(uint256(9900+i));vm.prank(address(funding));credits.issue(original,source,0,1000);
            historical[i]=WithdrawalFeeTypes.CreditUse(keccak256(abi.encode(credits.scope(),source,uint32(0))),0,0,0,i/2);
        }
        vm.prank(address(payments));credits.lockClaims(history,historical);vm.prank(address(payments));credits.settleClaims(history,txid,historical);
        for(uint32 i;i<16;++i)common.cacheValue(carrier,i);
        bytes32 id=keccak256("32 claim refund");bytes32 request=keccak256(abi.encode(credits.scope(),RECIPIENT,uint256(0),id));
        vm.startPrank(RECIPIENT);credits.createRefund(id,original,16000,32,1000,uint64(block.timestamp+3600),addresses.custodyGeneration(),addresses.derivationGeneration());vm.stopPrank();_report();
        bytes memory raw=hex"0200000010";for(uint32 i;i<16;++i)raw=bytes.concat(raw,abi.encodePacked(txid),_le(i,4),hex"00fdffffff");
        raw=bytes.concat(raw,hex"02",_le(16000,8),hex"16",refundScript,_le(15968,8),hex"22",pool.payoutScript(),hex"00000000");
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("32 claims version"),0,request,raw,32,1);p.credits=new WithdrawalFeeTypes.CreditUse[](32);
        for(uint256 i;i<32;++i)p.credits[i]=WithdrawalFeeTypes.CreditUse(historical[i].id,request,1,500,1);
        address[] memory nodes=_gasNodes();bytes32 op=payments.getPrepareOperationHash(p);_approve(op,address(payments));vm.record();uint256 beforeGas=gasleft();payments.prepare(p,op);
        _gasBound("prepare 32 claims / 16 locations cold upper",beforeGas-gasleft(),abi.encodeCall(payments.prepare,(p,op)).length,nodes);
        _approveOwner(p.batch,request,1);_btcReserve(p);_bindUser(p);vm.record();beforeGas=gasleft();escrow.settle(p.batch,raw,bytes32(uint256(1)),new bytes32[](0),0);
        _gasBound("settle 32 claims / 16 locations cold upper",beforeGas-gasleft(),raw.length+256,nodes);
        assertEq(credits.claimCount(),32);assertEq(credits.protectedSats(keccak256(abi.encode(_txid(raw),uint32(1)))),15968);assertEq(common.totalBackingSats(),1000);
    }
    function testSubsidyBoundedPublicIntentChunksHaveColdGasBoundAndOnlyFinalBytesVisible() public {
        address[] memory participants=new address[](2);participants[0]=address(this);participants[1]=RECIPIENT;
        DKGOnchainBoard board=new DKGOnchainBoard(bytes("PUBLIC transport fixture"),participants);board.bindRegistry(address(dkg));
        IntentSessionFixture sessions=new IntentSessionFixture(address(dkg));sessions.open(1,address(this));board.bindIntentCoordinator(address(sessions));
        address[] memory nodes=new address[](2);nodes[0]=address(board);nodes[1]=address(sessions);bytes memory chunk=new bytes(8192);
        for(uint256 i;i<chunk.length;++i)chunk[i]=0x61;
        for(uint256 i;i<16;++i) {
            vm.record();uint256 beforeGas=gasleft();board.appendFeeIntent(1,131072,i,chunk);
            _gasBound("public 8KiB intent chunk cold upper",beforeGas-gasleft(),chunk.length+256,nodes);assertEq(board.intents(1).length,0);
        }
        vm.record();uint256 beforeGas=gasleft();board.commitFeeIntent(1);_gasBound("public 128KiB intent commit cold upper",beforeGas-gasleft(),36,nodes);
        assertEq(board.intents(1).length,131072);assertEq(board.intentCount(),1);board.commitFeeIntent(1);assertEq(board.intentCount(),1);
    }
    function testSubsidyPublicPartsShortLongUnalignedAndConflictingRetry() public {
        address[] memory participants=new address[](2);participants[0]=address(this);participants[1]=RECIPIENT;
        DKGOnchainBoard board=new DKGOnchainBoard(bytes("PUBLIC bytes boundary fixture"),participants);board.bindRegistry(address(dkg));
        IntentSessionFixture sessions=new IntentSessionFixture(address(dkg));sessions.open(7,address(this));board.bindIntentCoordinator(address(sessions));
        uint256[6] memory sizes=[uint256(1),31,32,33,127,8192];uint256 total;for(uint256 i;i<6;++i)total+=sizes[i];bytes memory expected;
        for(uint256 i;i<6;++i) {
            bytes memory part=new bytes(sizes[i]);for(uint256 j;j<part.length;++j)part[j]=bytes1(uint8(i+j));expected=bytes.concat(expected,part);
            board.appendFeeIntent(7,total,i,part);assertEq(board.feeIntentPartial(7),expected);board.appendFeeIntent(7,total,i,part);
        }
        vm.expectRevert(DKGOnchainBoard.ConflictingRetry.selector);board.appendFeeIntent(7,total,0,hex"ff");
        board.commitFeeIntent(7);assertEq(board.intents(7),expected);assertEq(board.intentCount(),1);
    }
    function testSubsidyPublicSigningPlanStagingIsPerParticipantAndCommitsExactHash() public {
        bytes32 txid=_minted();bytes32 request=_request();_mpConsent(request,100,1);_report();
        BitcoinSigningBoard board=new BitcoinSigningBoard(address(batches),1024);bytes memory raw=new bytes(20000);
        for(uint256 i;i<raw.length;++i)raw[i]=0x61;
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("public parts plan"),0,request,_rawUser(txid,490),10,1);
        p.planHash=board.getPlanHash(p.batch,raw);_prepareUser(p);_approveOwner(p.batch,request,1);_reserveUser(p,txid);
        vm.startPrank(address(0xa2));board.appendFeePlan(p.batch,1,0,hex"ff");vm.expectRevert(BitcoinSigningBoard.PlanHashMismatch.selector);board.commitFeePlan(p.batch);vm.stopPrank();
        bytes memory first=new bytes(8192);for(uint256 i;i<first.length;++i)first[i]=0x61;
        vm.startPrank(address(0xa1));board.appendFeePlan(p.batch,raw.length,0,first);board.appendFeePlan(p.batch,raw.length,1,first);
        bytes memory last=new bytes(3616);for(uint256 i;i<last.length;++i)last[i]=0x61;board.appendFeePlan(p.batch,raw.length,2,last);
        assertEq(board.plan(p.batch).length,0);board.commitFeePlan(p.batch);vm.stopPrank();assertEq(board.plan(p.batch),raw);
    }
    function testSubsidyCoinbaseCreditNeeds101WhileOrdinaryNeeds6() public {
        _minted();bytes32 request=_request();bytes memory script=_btcBinding(request);bytes memory raw=_funding(1,1,0,script,0,true);funding.classify(raw,0,16);
        uint256 cg=addresses.custodyGeneration();uint256 dg=addresses.derivationGeneration();proof.setDepth(100);
        vm.expectRevert("fee funding proof/maturity");funding.admit(raw,0,bytes32(uint256(1)),new bytes32[](0),0,cg,dg);
        proof.setDepth(101);funding.admit(raw,0,bytes32(uint256(1)),new bytes32[](0),0,cg,dg);assertEq(credits.claimCount(),1);
        bytes memory source=_funding(1,1,0,destination,bytes32(uint256(450)),false);common.cacheValue(source,0);raw=_funding(1,1,0,script,_txid(source),false);funding.classify(raw,0,16);
        proof.setDepth(5);vm.expectRevert("fee funding proof/maturity");funding.admit(raw,0,bytes32(uint256(1)),new bytes32[](0),0,cg,dg);
        proof.setDepth(6);funding.admit(raw,0,bytes32(uint256(1)),new bytes32[](0),0,cg,dg);assertEq(credits.claimCount(),2);
    }
    function testSubsidyCoinbaseCreditUsesHigherConfiguredDepth() public {
        _setUpSubsidy(144);
        assertEq(common.depth(),144);
        proof.setDepth(144);_minted();
        bytes32 request=_request();bytes memory script=_btcBinding(request);
        bytes memory raw=_funding(1,1,0,script,0,true);funding.classify(raw,0,16);
        uint256 cg=addresses.custodyGeneration();uint256 dg=addresses.derivationGeneration();
        proof.setDepth(143);
        vm.expectRevert("fee funding proof/maturity");funding.admit(raw,0,bytes32(uint256(1)),new bytes32[](0),0,cg,dg);
        assertEq(credits.claimCount(),0);
        proof.setDepth(144);funding.admit(raw,0,bytes32(uint256(1)),new bytes32[](0),0,cg,dg);
        assertEq(credits.claimCount(),1);
    }
    function testSubsidySharedPrincipalAndRefundScriptUsesAggregateOnce() public {
        refundScript=destination;bytes32 principalTx=_minted();bytes32 request=_request();bytes memory script=_btcBinding(request);(bytes32 feeTx,bytes32 id)=_topup(script,990);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("same payout and refund"),0,request,_btcPayment(principalTx,feeTx,990,500),10,1);
        p.credits=new WithdrawalFeeTypes.CreditUse[](1);p.credits[0]=WithdrawalFeeTypes.CreditUse(id,request,10,990,2);
        _prepareUser(p);_approveOwner(p.batch,request,1);_btcReserve(p);_bindUser(p);escrow.settle(p.batch,p.raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(common.totalBackingSats(),500);assertEq(credits.claimCount(),0);
    }
    function testSubsidyCancelledReservedFamilyReleasesTreasuryLockAndTokensWithoutSignature() public {
        bytes32 txid=_minted();bytes32 request=_request();_mpConsent(request,100,1);_report();
        WithdrawalFeeTypes.Candidate memory p=_candidate(keccak256("cancel reserved"),0,request,_rawUser(txid,490),10,1);_prepareUser(p);_approveOwner(p.batch,request,1);_reserveUser(p,txid);
        bytes32 op=auth.hashOperation(address(batches),batches.cancel.selector,keccak256(abi.encode(p.batch)),batches.nonce());_approve(op,address(batches));batches.cancel(p.batch,op);
        fees.releaseUserFamily(p.batch);vm.prank(RECIPIENT);credits.refundTokens(request);
        assertFalse(fees.userLocked(p.batch));assertEq(credits.lockedTokens(address(pool.mpToken())),0);assertEq(pool.mpToken().balanceOf(RECIPIENT),500*1e10);
        assertEq(pool.mpToken().balanceOf(address(escrow)),500*1e10);assertEq(common.totalBackingSats(),1000);
    }
    function testSubsidyCanonicalSimpleFactoryBlobSizesHashAndNonce() public {
        FROSTAuthorizationDeployer fd=new FROSTAuthorizationDeployer();MultiPoolShareTokenDeployer sd=new MultiPoolShareTokenDeployer();
        SimpleMultiPoolDeployer factory=new SimpleMultiPoolDeployer(address(fd),address(sd));
        address[] memory nodes=new address[](3);nodes[0]=address(factory);nodes[1]=factory.creationPart0();nodes[2]=factory.creationPart1();
        bytes memory init=type(SimpleMultiPool).creationCode;uint256 half=init.length/2;
        assertEq(nodes[1].code.length,half+1);assertEq(nodes[2].code.length,init.length-half+1);
        assertLt(nodes[1].code.length,24576);assertLt(nodes[2].code.length,24576);assertEq(factory.poolCreationHash(),keccak256(init));assertEq(factory.nextMultiPoolNonce(),3);
    }
}
