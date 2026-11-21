// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/federation/BitcoinWithdrawalEscrow.sol";

// Explicit unit boundary fixtures: proof/signature validity is tested separately
// against real FROST processes and Bitcoin Core in tests/e2e.
contract EscrowAuthorizationFixture {
    function hashOperation(address target, bytes4 method, bytes32 params, uint256 nonce) external pure returns(bytes32) {
        return keccak256(abi.encode(target,method,params,nonce));
    }
    function consumeOperation(bytes32) external pure returns(bool) { return true; }
}
contract EscrowBatchFixture {
    address public feeReserve;
    address public authorization = address(new EscrowAuthorizationFixture());
    mapping(bytes32=>bytes32) public replacementOf;
    function setReplacement(bytes32 next,bytes32 previous) external { replacementOf[next]=previous; }
    BitcoinBatchRegistry.State public state;
    bytes32[] internal inputs;
    function configure(bytes32 input, BitcoinBatchRegistry.State next) external { delete inputs; inputs.push(input); state=next; }
    function batchState(bytes32) external view returns(BitcoinBatchRegistry.State) { return state; }
    function getInputs(bytes32) external view returns(bytes32[] memory) { return inputs; }
}
contract EscrowProofFixture {
    uint64 public confirmations=6;
    function setDepth(uint64 value) external { confirmations=value; }
    function testHelpersEnabled() external pure returns(bool) { return false; }
    function getBlockStatus(bytes32) external view returns(bool,uint64) { return (true,confirmations); }
    function checkTxInclusion(bytes32,bytes32,bytes32[] calldata,uint256) external pure returns(bool) { return true; }
}
contract EscrowDepositFixture {
    address public spendRecorder;
    mapping(address=>bytes32) public poolScripts;
    uint256 public burnedSats;
    constructor(address escrow, address pool) { spendRecorder=escrow;poolScripts[pool]=bytes32(uint256(1)); }
    function recordSpend(bytes calldata,uint256 amount) external { require(msg.sender==spendRecorder); burnedSats=amount; }
    function recordFamily(bytes32) external view { require(msg.sender==spendRecorder); }
}
contract EscrowMiningFixture {
    PoolMpToken public mpToken = new PoolMpToken("Unit MP","UMP",address(this),false);
    function mint(address recipient,uint256 amount) external { mpToken.mint(recipient,amount); }
}
contract EscrowBoundaryTest is Test {
    BitcoinWithdrawalEscrow internal escrow;
    EscrowBatchFixture internal batches;
    EscrowProofFixture internal proof;
    EscrowMiningFixture internal mining;
    EscrowDepositFixture internal deposits;
    PoolMpToken internal token;
    bytes internal script=hex"00141111111111111111111111111111111111111111";
    bytes32 internal constant BATCH=bytes32(uint256(1));
    bytes32 internal constant PREV=bytes32(uint256(9));
    uint256 internal constant SAT=1e10;
    function setUp() public {
        batches=new EscrowBatchFixture(); proof=new EscrowProofFixture(); mining=new EscrowMiningFixture(); token=mining.mpToken();
        escrow=new BitcoinWithdrawalEscrow(address(batches),address(proof),6, 294, 330);
        deposits=new EscrowDepositFixture(address(escrow),address(mining));
        escrow.setDepositRegistry(address(deposits),escrow.getSetDepositRegistryOperationHash(address(deposits)));
        token.approve(address(escrow),type(uint256).max);
    }
    function raw(uint64 amount) internal view returns(bytes memory) {
        bytes8 little;
        for(uint256 i;i<8;i++) little|=bytes8(uint64(uint8(amount>>(i*8)))<<((7-i)*8));
        return abi.encodePacked(hex"0200000001",PREV,hex"0000000000ffffffff01",little,bytes1(uint8(script.length)),script,hex"00000000");
    }
    function bind(uint64 a,uint64 b,uint64 paid) internal returns(bytes memory transaction,bytes32[] memory keys) {
        mining.mint(address(this),(uint256(a)+b)*SAT);
        escrow.requestDirect(1,address(token),uint256(a)*SAT,script);
        escrow.requestDirect(2,address(token),uint256(b)*SAT,script);
        keys=new bytes32[](2); keys[0]=escrow.requestKey(address(this),1);keys[1]=escrow.requestKey(address(this),2);
        transaction=raw(paid); bytes32 txid=sha256(abi.encodePacked(sha256(transaction)));
        bytes32 input=keccak256(abi.encode(bytes32(uint256(9)<<248),uint32(0)));
        batches.configure(input,BitcoinBatchRegistry.State.Reserved);
        escrow.bind(BATCH,txid,keys,escrow.getBindOperationHash(BATCH,txid,keys));
        batches.configure(input,BitcoinBatchRegistry.State.Signing);
    }
    function testSharedScriptCannotCountOneInsufficientOutputTwice() public {
        (bytes memory transaction,)=bind(300,301,600);
        vm.expectRevert("underpayment"); escrow.settle(BATCH,transaction,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(token.balanceOf(address(escrow)),601*SAT); assertEq(token.totalSupply(),601*SAT);
        assertFalse(escrow.payment(BATCH).settled); assertEq(deposits.burnedSats(),0);
    }
    function testFuzzSettlementConservesAndCannotBurnTwice(uint32 a,uint32 b) public {
        a=uint32(bound(a,294,1000000));b=uint32(bound(b,294,1000000));
        (bytes memory transaction,)=bind(a,b,uint64(a)+b);
        escrow.settle(BATCH,transaction,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(token.totalSupply(),0);assertEq(token.balanceOf(address(escrow)),0);assertEq(deposits.burnedSats(),uint256(a)+b);
        vm.expectRevert("state");escrow.settle(BATCH,transaction,bytes32(uint256(1)),new bytes32[](0),0);
    }
    function testNoRefundAfterSigningAndNoBurnBeforeDepth() public {
        (bytes memory transaction,bytes32[] memory keys)=bind(300,301,601);
        vm.expectRevert("signature may exist");escrow.refund(keys[0]);
        proof.setDepth(5);
        vm.expectRevert("SPV proof");escrow.settle(BATCH,transaction,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(token.totalSupply(),601*SAT);
    }
    function testFuzzOnlyOneApprovedVersionCanBurn(bool payOriginal) public {
        (bytes memory original,)=bind(300,301,601);
        bytes32 next=bytes32(uint256(2));
        bytes memory replacement=raw(602);
        bytes32 txid=sha256(abi.encodePacked(sha256(replacement)));
        bytes32 input=keccak256(abi.encode(bytes32(uint256(9)<<248),uint32(0)));
        batches.setReplacement(next,BATCH);
        batches.configure(input,BitcoinBatchRegistry.State.Reserved);
        bytes32[] memory differentRequests = new bytes32[](1);
        differentRequests[0] = escrow.requestKey(address(this),1);
        bytes32 ordinaryHash = escrow.getBindOperationHash(next,txid,differentRequests);
        vm.expectRevert("replacement requires inherited requests");
        escrow.bind(next,txid,differentRequests,ordinaryHash);
        escrow.bindReplacement(BATCH,next,txid,escrow.getBindReplacementOperationHash(BATCH,next,txid));
        batches.configure(input,BitcoinBatchRegistry.State.Signing);
        escrow.settle(payOriginal?BATCH:next,payOriginal?original:replacement,bytes32(uint256(1)),new bytes32[](0),0);
        assertEq(token.totalSupply(),0);
        assertEq(deposits.burnedSats(),601);
        assertFalse(escrow.isBound(payOriginal?next:BATCH));
        vm.expectRevert("input mismatch or spent");
        escrow.settle(payOriginal?next:BATCH,payOriginal?replacement:original,bytes32(uint256(1)),new bytes32[](0),0);
    }
    function testCounterfeitTokenCannotBorrowRegisteredPoolIdentity() public {
        PoolMpToken counterfeit=new PoolMpToken("Fake","FAKE",address(mining),false);
        vm.expectRevert("unregistered backing token");escrow.requestDirect(1,address(counterfeit),SAT,script);
    }
}

contract DepositReservationFixture {
    mapping(bytes32=>bytes32) public reservations;
    function reserve(bytes32 key) external { reservations[key]=bytes32(uint256(1)); }
}
