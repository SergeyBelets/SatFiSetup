// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./BitcoinAddressRegistry.t.sol";
import "../contracts/src/federation/BitcoinFeeReserve.sol";

// Explicit DKG/approval/SPV/qualification boundaries inherited from the address
// fixture. Donor signatures, fee accounting, token burn and contract hooks are real.
contract FeeBoardFixture {
    address public batchRegistry; address public dkg; bytes32 public walletId;
    constructor(address b,address d,bytes32 w) { batchRegistry=b;dkg=d;walletId=w; }
}
contract BitcoinFeeReserveTest is BitcoinAddressRegistryTest {
    BitcoinFeeReserve internal fees;
    bytes32 constant GX=0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798;
    uint256 constant N=0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141;
    bytes32 constant ALLOCATION=keccak256("fee allocation");
    function setUp() public virtual override {
        _setUpFeeReserve(6);
    }
    function _setUpFeeReserve(uint32 confirmations) internal {
        _deploy(true, confirmations);
        address board=address(new FeeBoardFixture(address(batches),address(dkg),dkg.BTC_ID()));
        address donor=address(new FROSTVerifier());
        address funding=address(new BitcoinFeeFundingVerifier());
        address plan=address(new BitcoinFeePlanVerifier());
        fees=BitcoinFeeReserve(deployCode("BitcoinFeeReserve.sol:BitcoinFeeReserve",
            abi.encode(address(addresses),board,donor,funding,plan,uint64(1000),uint32(10000))));
        bytes32 op=batches.getSetFeeReserveOperationHash(address(fees));_approve(op,address(batches));batches.setFeeReserve(address(fees),op);
    }
    function _c() internal view returns(BitcoinFeeReserveStorage.Control memory) {
        return BitcoinFeeReserveStorage.Control(fees.nonce(),addresses.custodyGeneration(),addresses.derivationGeneration(),uint64(block.timestamp+3600));
    }
    function _op(bytes4 selector,bytes memory data) internal returns(bytes32 h) {
        h=fees.getOperationHash(selector,data,fees.nonce());_approve(h,address(fees));
    }
    function _donation() internal view returns(BitcoinFeeReserveStorage.Donation memory d) {
        bytes memory previous=_funding(1,1,0,abi.encodePacked(hex"5120",GX),bytes32(uint256(71)),false);
        bytes memory raw=_funding(1,1,0,pool.payoutScript(),_txid(previous),false);
        bytes32 message=fees.donationHash(_txid(raw),0,1000);
        bytes32 tag=sha256("BIP0340/challenge");
        uint256 challenge=uint256(sha256(abi.encodePacked(tag,tag,GX,GX,message)))%N;
        // Public test-only private scalar=nonce=1. Never used by SDK/live signers.
        bytes memory signature=abi.encodePacked(GX,bytes32(addmod(1,challenge,N)));
        d=BitcoinFeeReserveStorage.Donation(_c(),BitcoinFeeFundingVerifier.Funding(raw,previous,0,signature,bytes32(uint256(1)),new bytes32[](0),0));
    }
    function _fund() internal returns(bytes32 txid) {
        BitcoinFeeReserveStorage.Donation memory d=_donation();
        fees.admitDonation(d,_op(fees.admitDonation.selector,abi.encode(d)));return _txid(d.funding.raw);
    }
    function _report() internal virtual {
        uint256 backing=common.totalBackingSats();uint256 free=fees.freeSats();
        BitcoinFeeReserveStorage.Report memory r=BitcoinFeeReserveStorage.Report(_c(),1,2000,bytes32(uint256(123)),uint64(block.timestamp+300),
            backing+free,backing,0,free,keccak256("explicit observed Core fixture"));
        fees.publishReconciliation(r,_op(fees.publishReconciliation.selector,abi.encode(r)));
    }
    function _grant(uint256 cap,bool treasury_) internal {
        BitcoinFeeReserveStorage.Grant memory g=BitcoinFeeReserveStorage.Grant(_c(),ALLOCATION,cap,100,treasury_);
        fees.allocate(g,_op(fees.allocate.selector,abi.encode(g)));
    }
    function _payment(bytes32 feeTx,bytes32 backingTx,uint256 change) internal view returns(bytes memory) {
        bytes memory destination=abi.encodePacked(hex"0014",bytes20(uint160(0x123456)));
        return bytes.concat(hex"0200000002",abi.encodePacked(feeTx),hex"0000000000ffffffff",abi.encodePacked(backingTx),
            hex"0000000000ffffffff02",_le(500,8),_compact(destination.length),destination,
            _le(change,8),_compact(pool.payoutScript().length),pool.payoutScript(),hex"00000000");
    }
    function _request() internal returns(bytes32 ref) {
        vm.startPrank(RECIPIENT);pool.mpToken().approve(address(escrow),500*1e10);
        escrow.requestDirect(7,address(pool.mpToken()),500*1e10,abi.encodePacked(hex"0014",bytes20(uint160(0x123456))));vm.stopPrank();
        return escrow.requestKey(RECIPIENT,7);
    }
    function _prepare(bytes32 id,bytes32 previous,bytes memory raw,bytes32 ref) internal returns(bytes32 hash) {
        bytes32[] memory refs=new bytes32[](1);refs[0]=ref;hash=keccak256(abi.encode(raw,id));
        BitcoinFeePlanVerifier.Control memory c=BitcoinFeePlanVerifier.Control(fees.nonce(),addresses.custodyGeneration(),addresses.derivationGeneration(),uint64(block.timestamp+3600));
        BitcoinFeePlanVerifier.Prepared memory p=BitcoinFeePlanVerifier.Prepared(c,ALLOCATION,id,previous,hash,raw,refs);
        fees.preparePlan(p,_op(fees.preparePlan.selector,abi.encode(p)));
    }
    function _reserve(bytes32 id,bytes32 previous,bytes32 hash,bytes32 feeTx,bytes32 backingTx) internal {
        uint64 deadline=uint64(block.timestamp+3600);bytes32 op;
        if(previous == 0) {
            bytes32[] memory keys=new bytes32[](2);
            keys[0]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(feeTx),uint32(0)));
            keys[1]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(backingTx),uint32(0)));
            op=batches.operationHash(id,hash,keys,deadline);_approve(op,address(batches));batches.reserve(id,hash,keys,deadline,op);
        } else { op=batches.getReplacementOperationHash(previous,id,hash,deadline);_approve(op,address(batches));batches.reserveReplacement(previous,id,hash,deadline,op); }
    }
    function _bind(bytes32 id,bytes32 previous,bytes memory raw,bytes32 ref) internal {
        bytes32 op;
        if(previous == 0) {
            bytes32[] memory refs=new bytes32[](1);refs[0]=ref;
            op=escrow.getBindOperationHash(id,_txid(raw),refs);_approve(op,address(escrow));escrow.bind(id,_txid(raw),refs,op);
        } else { op=escrow.getBindReplacementOperationHash(previous,id,_txid(raw));_approve(op,address(escrow));escrow.bindReplacement(previous,id,_txid(raw),op); }
        vm.prank(address(0xa1));batches.beginSigning(id);
    }
    function _minted() internal returns(bytes32 txid) {
        bytes32 id=_register(11);bytes memory raw=_funding(1,1,0,_script(id),bytes32(uint256(91)),false);
        _mint(raw,_claim(id,raw,0));return _txid(raw);
    }
    function testFeeRealDonorProofCanonicalCreditCannotMintAgain() public {
        bytes32 txid=_fund();bytes32 key=keccak256(abi.encode(txid,uint32(0)));
        assertEq(fees.freeSats(),1000);assertTrue(common.walletOutput(key));assertEq(common.totalBackingSats(),0);
        BitcoinFeeReserveStorage.Donation memory d=_donation();
        vm.expectRevert("fee funding used");fees.admitDonation(d,bytes32(0));
        assertEq(fees.freeSats(),1000);assertEq(pool.mpToken().totalSupply(),0);
    }
    function testFeeUnsignedUnknownOrDepositMarkerCannotBecomeDonation() public {
        BitcoinFeeReserveStorage.Donation memory d=_donation();d.funding.signature[63]^=0x01;
        vm.expectRevert("fee donor consent");fees.admitDonation(d,bytes32(0));assertEq(fees.freeSats(),0);
        d=_donation();d.funding.raw=_funding(1,2,0,pool.payoutScript(),_txid(d.funding.previous),false);
        vm.expectRevert("fee deposit marker");fees.admitDonation(d,bytes32(0));
    }
    function testFeeGenerationAndObservationFenceAllocationWithoutTouchingPrincipal() public {
        _fund();_report();_grant(100,false);assertEq(fees.available(),900);
        BitcoinFeeReserveStorage.Grant memory g=BitcoinFeeReserveStorage.Grant(_c(),keccak256("other"),901,100,false);
        vm.expectRevert("fee allocation budget");fees.allocate(g,bytes32(0));
        _status(0,1,true);g.control=_c();g.cap=1;
        vm.expectRevert("fee reconciliation required");fees.allocate(g,bytes32(0));
        assertEq(fees.freeSats(),1000);assertEq(fees.promisedSats(),100);assertEq(common.totalBackingSats(),0);
    }
    function testFeeLegacyReservationCannotBypassPreparedBudget() public {
        bytes32[] memory inputs=new bytes32[](1);inputs[0]=bytes32(uint256(1));bytes32 id=keccak256("bypass");
        bytes32 op=batches.operationHash(id,bytes32(uint256(2)),inputs,uint64(block.timestamp+30));_approve(op,address(batches));
        vm.expectRevert("fee unprepared plan");batches.reserve(id,bytes32(uint256(2)),inputs,uint64(block.timestamp+30),op);
        assertEq(batches.reservations(inputs[0]),0);assertTrue(auth.approvedOperations(op));
    }
    function testFeeSettlementPreservesPrincipalAfterPauseAndChargesActualWinnerOnce() public {
        bytes32 feeTx=_fund();bytes32 backingTx=_minted();bytes32 ref=_request();_report();_grant(100,false);
        bytes32 id=keccak256("original");bytes memory raw=_payment(feeTx,backingTx,1490);
        _reserve(id,0,_prepare(id,0,raw,ref),feeTx,backingTx);_bind(id,0,raw,ref);
        assertEq(fees.lockedSats(),1000);assertEq(fees.promisedSats(),0);assertEq(fees.available(),0);
        BitcoinFeeReserveStorage.Control memory c=_c();bytes32 op=_op(fees.release.selector,abi.encode(c,ALLOCATION));
        vm.expectRevert("fee signature may exist");fees.release(c,ALLOCATION,op);
        _status(0,2,false);escrow.settle(id,raw,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(common.totalBackingSats(),500);assertEq(pool.mpToken().totalSupply(),500*1e10);
        assertEq(fees.freeSats(),990);assertEq(fees.lockedSats(),0);assertEq(fees.winners(id),id);
        vm.expectRevert("state");escrow.settle(id,raw,bytes32(uint256(1)),new bytes32[](0),0);
    }
    function testFeeReplacementKeepsOneCollateralAndOldWinnerRemainsPayable() public {
        bytes32 feeTx=_fund();bytes32 backingTx=_minted();bytes32 ref=_request();_report();_grant(100,false);
        bytes32 id=keccak256("original");bytes memory old=_payment(feeTx,backingTx,1490);
        _reserve(id,0,_prepare(id,0,old,ref),feeTx,backingTx);_bind(id,0,old,ref);
        bytes32 next=keccak256("replacement");bytes memory replacement=_payment(feeTx,backingTx,1480);
        _reserve(next,id,_prepare(next,id,replacement,ref),feeTx,backingTx);_bind(next,id,replacement,ref);
        assertEq(fees.lockedSats(),1000);assertEq(fees.familyCount(id),2);
        escrow.settle(id,old,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(fees.freeSats(),990);assertEq(fees.winners(id),id);
        vm.expectRevert();escrow.settle(next,replacement,bytes32(uint256(1)),new bytes32[](0),0);
    }
    function testFeeEmptyReserveDoesNotBlockValidAddressMint() public {
        _minted();assertEq(common.totalBackingSats(),1000);
        assertEq(pool.mpToken().totalSupply(),1000*1e10);assertEq(fees.freeSats(),0);
        _report();BitcoinFeeReserveStorage.Grant memory g=BitcoinFeeReserveStorage.Grant(_c(),ALLOCATION,1,1,false);
        vm.expectRevert("fee allocation budget");fees.allocate(g,bytes32(0));
        assertEq(common.totalBackingSats(),1000);
    }
    function testFeeTreasuryMustEarmarkWholeObligationAndCannotReleaseWhileLive() public {
        _fund();_report();uint256 id=99;bytes32 key=escrow.requestKey(address(escrow),id);
        bytes memory script=abi.encodePacked(hex"0014",bytes20(uint160(0x123456)));
        bytes32 op=escrow.getTreasuryOperationHash(id,RECIPIENT,500,script);_approve(op,address(escrow));
        vm.expectRevert("fee treasury allocation required");escrow.requestTreasury(id,RECIPIENT,500,script,op);
        BitcoinFeeReserveStorage.Grant memory g=BitcoinFeeReserveStorage.Grant(_c(),key,600,100,true);
        fees.allocate(g,_op(fees.allocate.selector,abi.encode(g)));
        escrow.requestTreasury(id,RECIPIENT,500,script,op);
        assertEq(fees.promisedSats(),600);assertEq(fees.available(),400);
        g=BitcoinFeeReserveStorage.Grant(_c(),ALLOCATION,401,100,false);
        vm.expectRevert("fee allocation budget");fees.allocate(g,bytes32(0));
        BitcoinFeeReserveStorage.Control memory c=_c();op=_op(fees.release.selector,abi.encode(c,key));
        vm.expectRevert("fee treasury obligation live");fees.release(c,key,op);
        assertEq(common.totalBackingSats(),0);assertEq(fees.freeSats(),1000);
    }
    function testFeeWholeUtxoLockCannotStrandOtherCanonicalPromises() public {
        bytes32 feeTx=_fund();bytes32 backingTx=_minted();bytes32 ref=_request();_report();_grant(100,false);
        bytes32 other=keccak256("other promise");BitcoinFeeReserveStorage.Grant memory g=BitcoinFeeReserveStorage.Grant(_c(),other,100,100,false);
        fees.allocate(g,_op(fees.allocate.selector,abi.encode(g)));
        bytes32 id=keccak256("original");bytes memory raw=_payment(feeTx,backingTx,1490);bytes32 hash=_prepare(id,0,raw,ref);
        bytes32[] memory keys=new bytes32[](2);keys[0]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(feeTx),uint32(0)));
        keys[1]=keccak256(abi.encode(BitcoinTxParser.flipBytes32(backingTx),uint32(0)));
        uint64 deadline=uint64(block.timestamp+3600);bytes32 op=batches.operationHash(id,hash,keys,deadline);_approve(op,address(batches));
        vm.expectRevert("fee initial family");batches.reserve(id,hash,keys,deadline,op);
        assertEq(fees.promisedSats(),200);assertEq(fees.lockedSats(),0);assertEq(batches.reservations(keys[0]),0);
        BitcoinFeeReserveStorage.Control memory c=_c();fees.release(c,other,_op(fees.release.selector,abi.encode(c,other)));
        _reserve(id,0,hash,feeTx,backingTx);assertEq(fees.available(),0);assertEq(fees.lockedSats(),1000);
    }
    function testFeeCannotPrepareFeeDebitBeyondFreeCreditEvenWithPrincipalInput() public {
        bytes32 feeTx=_fund();bytes32 backingTx=_minted();bytes32 ref=_request();_report();_grant(1000,false);
        bytes memory raw=_payment(feeTx,backingTx,499);bytes32 id=keccak256("haircut");bytes32[] memory refs=new bytes32[](1);refs[0]=ref;
        BitcoinFeePlanVerifier.Control memory c=BitcoinFeePlanVerifier.Control(fees.nonce(),addresses.custodyGeneration(),addresses.derivationGeneration(),uint64(block.timestamp+300));
        BitcoinFeePlanVerifier.Prepared memory p=BitcoinFeePlanVerifier.Prepared(c,ALLOCATION,id,0,keccak256(raw),raw,refs);
        vm.expectRevert("fee debit cap");fees.preparePlan(p,bytes32(0));
        assertEq(common.totalBackingSats(),1000);assertEq(pool.mpToken().totalSupply(),1000*1e10);assertEq(fees.lockedSats(),0);
    }
}
