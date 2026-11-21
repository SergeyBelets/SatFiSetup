// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./EscrowBoundary.t.sol";
import "../contracts/src/federation/BitcoinDepositRegistry.sol";

contract TreasuryBatchFixture is EscrowBatchFixture {
    mapping(bytes32 => bytes32) public reservations;
    function configureAll(bytes32[] memory values, BitcoinBatchRegistry.State next) external {
        inputs = values; state = next;
    }
}

contract TreasuryWithdrawalTest is Test {
    BitcoinWithdrawalEscrow escrow;
    BitcoinDepositRegistry deposits;
    TreasuryBatchFixture batches;
    bytes wallet = hex"51201111111111111111111111111111111111111111111111111111111111111111";
    bytes destination = hex"00142222222222222222222222222222222222222222";
    bytes32 constant BATCH = bytes32(uint256(8));

    function setUp() public {
        batches = new TreasuryBatchFixture();
        EscrowProofFixture relay = new EscrowProofFixture();
        escrow = new BitcoinWithdrawalEscrow(address(batches), address(relay), 6, 294, 330);
        deposits = new BitcoinDepositRegistry(address(relay), batches.authorization(), 6, address(escrow));
        deposits.register(address(this), wallet, deposits.getRegisterOperationHash(address(this), wallet));
        escrow.setDepositRegistry(address(deposits), escrow.getSetDepositRegistryOperationHash(address(deposits)));
    }
    function amount(uint64 sats) internal pure returns(bytes8 out) {
        for(uint256 i; i < 8; i++) out |= bytes8(uint64(uint8(sats >> (i*8))) << ((7-i)*8));
    }
    function raw(bytes32 prev, uint64 change, uint64 paid) internal view returns(bytes memory) {
        bytes memory outputs = paid == 0 ? abi.encodePacked(hex"01", amount(change), bytes1(uint8(wallet.length)), wallet)
            : change == 0 ? abi.encodePacked(hex"01", amount(paid), bytes1(uint8(destination.length)), destination)
            : abi.encodePacked(hex"02", amount(change), bytes1(uint8(wallet.length)), wallet,
                amount(paid), bytes1(uint8(destination.length)), destination);
        return abi.encodePacked(hex"0200000001", prev, hex"0000000000ffffffff", outputs, hex"00000000");
    }
    function txid(bytes memory data) internal pure returns(bytes32) { return sha256(abi.encodePacked(sha256(data))); }
    function request(uint64 sats) internal returns(bytes32) {
        escrow.requestTreasury(1, address(this), sats, destination,
            escrow.getTreasuryOperationHash(1, address(this), sats, destination));
        return escrow.requestKey(address(escrow), 1);
    }
    function bind(bytes memory transaction, bytes32 prev, bytes32 ref) internal {
        bytes32[] memory keys = new bytes32[](1); keys[0] = ref;
        bytes32 input = keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev), uint32(0)));
        batches.configure(input, BitcoinBatchRegistry.State.Reserved);
        escrow.bind(BATCH, txid(transaction), keys, escrow.getBindOperationHash(BATCH, txid(transaction), keys));
        batches.configure(input, BitcoinBatchRegistry.State.Signing);
    }
    function testUnminted600SatChangeCanPay400WithoutBurningMP() public {
        bytes memory change = raw(bytes32(uint256(77)), 600, 0);
        vm.prank(address(escrow)); deposits.recordSpend(change, 0);
        bytes32 key = keccak256(abi.encode(txid(change), uint32(0)));
        assertTrue(deposits.walletOutput(key)); assertEq(deposits.backingSats(key), 0);
        bytes32 ref = request(400);
        bytes memory payment = raw(txid(change), 0, 400); // Remaining 200 sats are the explicitly approved fee.
        bind(payment, txid(change), ref);
        vm.expectRevert("signature may exist"); escrow.refund(ref);
        escrow.settle(BATCH, payment, bytes32(uint256(1)), new bytes32[](0), 0);
        assertTrue(escrow.payment(BATCH).settled);
        (,,,,,,,BitcoinWithdrawalEscrow.State state) = escrow.requests(ref);
        assertEq(uint256(state), uint256(BitcoinWithdrawalEscrow.State.Paid));
        vm.expectRevert("state"); escrow.settle(BATCH, payment, bytes32(uint256(1)), new bytes32[](0), 0);
    }
    function testTreasuryCannotTreatPrincipalAsBurned() public {
        bytes memory deposit = raw(bytes32(uint256(77)), 1600, 0);
        deposits.consume(deposit, 0, bytes32(uint256(1)), new bytes32[](0), 0);
        bytes memory payment = raw(txid(deposit), 1000, 400);
        bind(payment, txid(deposit), request(400));
        vm.expectRevert("miner fee consumes principal");
        escrow.settle(BATCH, payment, bytes32(uint256(1)), new bytes32[](0), 0);
        assertFalse(escrow.payment(BATCH).settled);
        assertEq(deposits.backingSats(keccak256(abi.encode(txid(deposit), uint32(0)))), 1600);
    }
    function testTreasuryPreservesAllExistingBacking() public {
        bytes memory deposit = raw(bytes32(uint256(77)), 1000, 0);
        deposits.consume(deposit, 0, bytes32(uint256(1)), new bytes32[](0), 0);
        // SPV boundary fixture represents a transaction funded by this backed input
        // and an independent 600-sat treasury input. recordSpend checks principal.
        bytes32 ref = request(400);
        bytes memory payment = abi.encodePacked(hex"0200000002", txid(deposit), hex"0000000000ffffffff",
            bytes32(uint256(88)), hex"0000000000ffffffff02", amount(1000), bytes1(uint8(wallet.length)), wallet,
            amount(400), bytes1(uint8(destination.length)), destination, hex"00000000");
        bytes32[] memory inputs = new bytes32[](2);
        inputs[0] = keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid(deposit)), uint32(0)));
        inputs[1] = keccak256(abi.encode(BitcoinTxParser.flipBytes32(bytes32(uint256(88))), uint32(0)));
        batches.configureAll(inputs, BitcoinBatchRegistry.State.Reserved);
        bytes32[] memory keys = new bytes32[](1); keys[0] = ref;
        escrow.bind(BATCH, txid(payment), keys, escrow.getBindOperationHash(BATCH, txid(payment), keys));
        batches.configureAll(inputs, BitcoinBatchRegistry.State.Signing);
        escrow.settle(BATCH, payment, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(txid(payment), uint32(0)))), 1000);
    }
    function testTreasuryTermsCannotBeChangedAndPendingCancellationMovesNoTokens() public {
        bytes32 op = escrow.getTreasuryOperationHash(1, address(this), 400, destination);
        vm.expectRevert(bytes("hash")); escrow.requestTreasury(1, address(this), 401, destination, op);
        bytes32 ref = request(400);
        vm.prank(address(99)); vm.expectRevert("owner"); escrow.refund(ref);
        escrow.refund(ref);
        (,,,,,,,BitcoinWithdrawalEscrow.State state) = escrow.requests(ref);
        assertEq(uint256(state), uint256(BitcoinWithdrawalEscrow.State.Refunded));
    }
    function testTreasuryRespectsOutputMinimum() public {
        bytes32 op = escrow.getTreasuryOperationHash(1, address(this), 293, destination);
        vm.expectRevert("below withdrawal minimum"); escrow.requestTreasury(1, address(this), 293, destination, op);
    }
}
