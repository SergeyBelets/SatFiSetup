// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./EscrowBoundary.t.sol";
import "../contracts/src/federation/MiningPoolV1.sol";

contract MintDkgFixture {
    function getCompletedResult(bytes32) external pure returns(bytes32,bytes32,uint256,bytes32,bytes memory,address[] memory,uint256) {
        return (bytes32(0),bytes32(0),1,keccak256("BTC_SPEND"),
            hex"1111111111111111111111111111111111111111111111111111111111111111",new address[](0),2);
    }
}
contract MintBatchFixture is EscrowBatchFixture {
    address public dkg = address(new MintDkgFixture());
    bytes32 public walletId = bytes32(uint256(1));
    mapping(bytes32 => bytes32) public reservations;
}
contract MiningDepositAllocationTest is Test {
    MiningPoolV1 mining;
    BitcoinDepositRegistry deposits;
    EscrowProofFixture relay;
    function setUp() public {
        MintBatchFixture batches = new MintBatchFixture();
        relay = new EscrowProofFixture();
        BitcoinWithdrawalEscrow escrow = new BitcoinWithdrawalEscrow(address(batches), address(relay), 6, 294, 330);
        deposits = new BitcoinDepositRegistry(address(relay), batches.authorization(), 6, address(escrow));
        mining = new MiningPoolV1("allocation-unit", address(deposits), address(escrow), "MP", "MP");
        bytes memory wallet = mining.payoutScript();
        deposits.register(address(mining), wallet, deposits.getRegisterOperationHash(address(mining), wallet));
    }
    function distribution(address recipient) internal pure returns(address[] memory recipients,uint256[] memory amounts) {
        recipients = new address[](1); recipients[0] = recipient;
        amounts = new uint256[](1); amounts[0] = 1000 * 1e10;
    }
    function raw(bytes memory payload, bool coinbase) internal view returns(bytes memory) {
        if (payload.length == 0) return rawScript(new bytes(0), coinbase);
        return rawScript(abi.encodePacked(hex"6a",bytes1(uint8(payload.length)),payload), coinbase);
    }
    function rawScript(bytes memory script, bool coinbase) internal view returns(bytes memory) {
        bytes memory input = coinbase ? abi.encodePacked(bytes32(0), hex"ffffffff")
            : abi.encodePacked(bytes32(uint256(9)), hex"00000000");
        bytes memory outputs = abi.encodePacked(bytes1(script.length == 0 ? 0x01 : 0x02), hex"e80300000000000022", mining.payoutScript());
        if (script.length != 0) outputs = abi.encodePacked(outputs, hex"0000000000000000",bytes1(uint8(script.length)),script);
        return abi.encodePacked(hex"0200000001", input, hex"00ffffffff", outputs, hex"00000000");
    }
    function digest(bytes memory data) internal pure returns(bytes32) { return sha256(abi.encodePacked(sha256(data))); }
    function mint(bytes memory data, address recipient) internal {
        (address[] memory recipients,uint256[] memory amounts) = distribution(recipient);
        bytes32 op = mining.getMintOperationHash(digest(data),0,recipients,amounts);
        mining.mintFromProof(data,0,bytes32(uint256(1)),new bytes32[](0),0,recipients,amounts,op);
    }
    function mintFailure(bytes memory data, address recipient, bytes memory reason) internal {
        (address[] memory recipients,uint256[] memory amounts) = distribution(recipient);
        bytes32 op = mining.getMintOperationHash(digest(data),0,recipients,amounts);
        vm.expectRevert(reason);
        mining.mintFromProof(data,0,bytes32(uint256(1)),new bytes32[](0),0,recipients,amounts,op);
    }
    function testCommittedDepositMintsExactRecipientAndStoresAllocation() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes memory payload = mining.getDepositData(0,recipients,amounts);
        assertEq(payload.length,56);
        assertEq(payload,abi.encodePacked(hex"534d5031",address(this),mining.getDepositCommitment(0,recipients,amounts)));
        bytes memory data = raw(payload,false);
        mint(data,address(this));
        assertEq(mining.mpToken().balanceOf(address(this)),1000 * 1e10);
        assertEq(mining.mintedAllocations(keccak256(abi.encode(digest(data),uint32(0)))),keccak256(abi.encode(recipients,amounts)));
        mintFailure(data,address(this),"outpoint consumed or change");
    }
    function testThresholdApprovalCannotRedirectOrdinaryDeposit() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes memory data = raw(mining.getDepositData(0,recipients,amounts),false);
        mintFailure(data,address(99),"deposit allocation commitment");
        assertEq(mining.mpToken().totalSupply(),0);
        assertEq(mining.nonce(),0);
    }
    function testNoCommitmentCannotMintOrdinaryDeposit() public {
        bytes memory data = raw(hex"",false);
        mintFailure(data,address(this),"deposit allocation commitment");
    }
    function testWrongOutputIndexOrChainCommitmentCannotMint() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes memory wrongIndex = raw(mining.getDepositData(1,recipients,amounts),false);
        mintFailure(wrongIndex,address(this),"deposit allocation commitment");
        bytes memory wrongChain = raw(mining.getDepositData(0,recipients,amounts),false);
        vm.chainId(block.chainid + 1);
        mintFailure(wrongChain,address(this),"deposit allocation commitment");
    }
    function testDifferentPoolDomainCannotMint() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes32 wrong = keccak256(abi.encode(keccak256("SATOHSI_DEPOSIT_ALLOCATION_V1"),block.chainid,address(99),uint32(0),recipients,amounts));
        bytes memory data = raw(abi.encodePacked(hex"534d5031",address(this),wrong),false);
        mintFailure(data,address(this),"deposit allocation commitment");
    }
    function testCoinbaseAllocationRemainsThresholdPolicyAndRequires100Confirmations() public {
        bytes memory data = raw(hex"",true);
        relay.setDepth(99);
        mintFailure(data,address(this),"deposit proof");
        relay.setDepth(100);
        mint(data,address(this));
        assertEq(mining.mpToken().balanceOf(address(this)),1000 * 1e10);
    }
    function testTamperedExplicitRecipientCannotUseHonestAllocationHash() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes32 hash = mining.getDepositCommitment(0,recipients,amounts);
        bytes memory data = raw(abi.encodePacked(hex"534d5031",address(99),hash),false);
        mintFailure(data,address(this),"deposit allocation commitment");
        mintFailure(data,address(99),"deposit allocation commitment");
        assertEq(mining.mpToken().totalSupply(),0);
    }
    function testHashOnlyPayloadAndWrongVersionAreRejected() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes32 hash = mining.getDepositCommitment(0,recipients,amounts);
        mintFailure(raw(abi.encodePacked(hash),false),address(this),"deposit allocation commitment");
        mintFailure(raw(abi.encodePacked(hex"534d5032",address(this),hash),false),address(this),"deposit allocation commitment");
    }
    function testOnlyCoinbaseCanDistributeToMultipleRecipients() public {
        address[] memory recipients = new address[](2); recipients[0] = address(this); recipients[1] = address(99);
        uint256[] memory amounts = new uint256[](2); amounts[0] = 500 * 1e10; amounts[1] = 500 * 1e10;
        bytes memory ordinary = raw(abi.encodePacked(hex"534d5031",address(this),mining.getDepositCommitment(0,recipients,amounts)),false);
        bytes32 op = mining.getMintOperationHash(digest(ordinary),0,recipients,amounts);
        vm.expectRevert("single deposit recipient");
        mining.mintFromProof(ordinary,0,bytes32(uint256(1)),new bytes32[](0),0,recipients,amounts,op);
        bytes memory reward = raw(hex"",true);
        op = mining.getMintOperationHash(digest(reward),0,recipients,amounts);
        relay.setDepth(100);
        mining.mintFromProof(reward,0,bytes32(uint256(1)),new bytes32[](0),0,recipients,amounts,op);
        assertEq(mining.mpToken().balanceOf(address(this)),500 * 1e10);
        assertEq(mining.mpToken().balanceOf(address(99)),500 * 1e10);
    }
    function testMarkedCoinbaseRequiresExplicitRecipientAndStill100Confirmations() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes memory data = raw(mining.getDepositData(0,recipients,amounts),true);
        relay.setDepth(99);
        mintFailure(data,address(this),"deposit proof");
        relay.setDepth(100);
        mintFailure(data,address(99),"deposit allocation commitment");
        mint(data,address(this));
        assertEq(mining.mpToken().balanceOf(address(this)),1000 * 1e10);
    }
    function testMarkedCoinbaseCannotIgnoreWrongHashOrExplicitAddress() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes32 hash = mining.getDepositCommitment(0,recipients,amounts);
        relay.setDepth(100);
        mintFailure(raw(abi.encodePacked(hex"534d5031",address(99),hash),true),address(this),"deposit allocation commitment");
        mintFailure(raw(abi.encodePacked(hex"534d5031",address(this),bytes32(uint256(99))),true),address(this),"deposit allocation commitment");
    }
    function testMalformedMarkedCoinbaseCannotFallBackToUnboundRewardPolicy() public {
        (address[] memory recipients,uint256[] memory amounts) = distribution(address(this));
        bytes memory payload = mining.getDepositData(0,recipients,amounts);
        relay.setDepth(100);
        mintFailure(rawScript(abi.encodePacked(hex"6a4c38",payload),true),address(this),"deposit allocation commitment");
        mintFailure(rawScript(abi.encodePacked(hex"6a4d3800",payload),true),address(this),"deposit allocation commitment");
        mintFailure(rawScript(abi.encodePacked(hex"6a4e38000000",payload),true),address(this),"deposit allocation commitment");
        mintFailure(rawScript(hex"6a38534d5031",true),address(this),"deposit allocation commitment");
    }
    function testCoinbaseWithoutMarkerStillHasBoundedOutputScan() public {
        bytes memory outputs = abi.encodePacked(hex"21e80300000000000022",mining.payoutScript());
        for (uint256 i; i < 32; ++i) outputs = abi.encodePacked(outputs,hex"0000000000000000026a00");
        bytes memory data = abi.encodePacked(hex"0200000001",bytes32(0),hex"ffffffff00ffffffff",outputs,hex"00000000");
        relay.setDepth(100);
        mintFailure(data,address(this),"deposit outputs");
    }
}
