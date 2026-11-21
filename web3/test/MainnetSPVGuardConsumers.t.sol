// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayIntegration.t.sol";
import "../contracts/src/federation/MainnetSPVGuard.sol";

/// @dev Real consumer/token contracts and the stored mainnet coinbase proof.
/// Authorization and batches are boundary fixtures; only the synthetic withdrawal
/// root and deep fork use the existing relay harness. No test bypass is deployed.
contract MainnetSPVGuardConsumersTest is MainnetFixtureBase {
    MainnetBatchBoundaryFixture private batches;
    MainnetMiningPoolFixture private mining;
    BitcoinDepositRegistry private deposits;
    BitcoinWithdrawalEscrow private escrow;
    PoolMpToken private token;
    bytes private coinbase;
    bytes private treasuryScript;
    bytes private withdrawalScript = hex"00141111111111111111111111111111111111111111";
    uint64 private principal;
    uint256 private constant SAT = 1e10;
    bytes32 private constant BATCH = bytes32(uint256(1));

    function setUp() public {
        loadMainnetFixture();
        string memory fixture = vm.readFile("test/fixtures/mainnet/checkpoint-coinbase.json");
        coinbase = vm.parseBytes(string.concat("0x", vm.parseJsonString(fixture, ".raw_tx")));
    }

    function _system(MainnetHeaderRelay relay) private returns (MainnetSPVGuard guard) {
        guard = new MainnetSPVGuard(address(relay), 7200);
        batches = new MainnetBatchBoundaryFixture();
        mining = new MainnetMiningPoolFixture();
        token = mining.mpToken();
        escrow = new BitcoinWithdrawalEscrow(address(batches), address(guard), 6, 294, 330);
        deposits = new BitcoinDepositRegistry(address(guard), batches.authorization(), 6, address(escrow));
        (principal, treasuryScript) = mining.output(coinbase);
        deposits.register(
            address(mining), treasuryScript, deposits.getRegisterOperationHash(address(mining), treasuryScript)
        );
        escrow.setDepositRegistry(address(deposits), escrow.getSetDepositRegistryOperationHash(address(deposits)));
        token.approve(address(escrow), type(uint256).max);
        for (uint64 height = CHECKPOINT + 1; height <= CHECKPOINT + 101; ++height) {
            relay.addBlockHeader(headerAt(height));
        }
        vm.warp(timestamp(headerAt(CHECKPOINT + 101)));
    }

    function testRealMainnetDepositMintBecomesUnavailableWhenGuardIsStale() public {
        MainnetHeaderRelay relay = newRelay(100);
        MainnetSPVGuard guard = _system(relay);
        uint256 snapshot = vm.snapshotState();
        assertTrue(guard.isFresh());
        assertEq(mining.deposit(deposits, coinbase, wireHash(CHECKPOINT), address(this)), principal);
        assertEq(token.totalSupply(), uint256(principal) * SAT);
        assertTrue(vm.revertToState(snapshot));
        // The underlying valid inclusion/confirmations alone must not permit a mint.
        vm.warp(block.timestamp + 7201);
        assertFalse(guard.isFresh());
        bytes32 checkpoint = wireHash(CHECKPOINT);
        assertTrue(relay.checkTxInclusion(checkpoint, digest(coinbase), new bytes32[](0), 0));
        vm.expectRevert("deposit proof");
        mining.deposit(deposits, coinbase, checkpoint, address(this));
        bytes32 outpoint = keccak256(abi.encode(digest(coinbase), uint32(0)));
        assertFalse(deposits.consumed(outpoint));
        assertEq(deposits.backingSats(outpoint), 0);
        assertEq(token.totalSupply(), 0);
        assertEq(address(deposits.spv()), address(guard));
        assertEq(address(escrow.spv()), address(guard));
    }

    function testActualDepositConsumerFailsClosedAfterPermanentRelayHalt() public {
        MainnetRelayHarness relay = newHarness(100);
        MainnetSPVGuard guard = _system(relay);
        assertTrue(guard.isFresh());
        _halt(relay);
        assertFalse(guard.isFresh());
        bytes32 checkpoint = wireHash(CHECKPOINT);
        vm.expectRevert("deposit proof");
        mining.deposit(deposits, coinbase, checkpoint, address(this));
        assertEq(token.totalSupply(), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(digest(coinbase), uint32(0)))), 0);
    }

    function testActualEscrowKeepsCollateralOnStaleOrHaltedProof() public {
        MainnetRelayHarness relay = newHarness(100);
        MainnetSPVGuard guard = _system(relay);
        mining.deposit(deposits, coinbase, wireHash(CHECKPOINT), address(this));
        escrow.requestDirect(1, address(token), 330 * SAT, withdrawalScript);
        bytes32 key = escrow.requestKey(address(this), 1);
        bytes memory transaction = abi.encodePacked(
            hex"0200000001", digest(coinbase), hex"0000000000ffffffff02",
            _le64(330), bytes1(uint8(withdrawalScript.length)), withdrawalScript,
            _le64(principal - 330), bytes1(uint8(treasuryScript.length)), treasuryScript, hex"00000000"
        );
        bytes32 input = keccak256(abi.encode(BitcoinTxParser.flipBytes32(digest(coinbase)), uint32(0)));
        batches.configure(input, BitcoinBatchRegistry.State.Reserved);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = key;
        escrow.bind(BATCH, digest(transaction), keys, escrow.getBindOperationHash(BATCH, digest(transaction), keys));
        batches.configure(input, BitcoinBatchRegistry.State.Signing);
        uint32 time = uint32(block.timestamp);
        bytes32 proof = relay.syntheticChild(relay.getMainchainHead(), 1, digest(transaction), time);
        bytes32 head = proof;
        for (uint32 i = 1; i <= 5; ++i) head = relay.syntheticChild(head, 1, bytes32(0), time + i);
        vm.warp(uint256(time) + 5);
        assertTrue(guard.isFresh());
        uint256 snapshot = vm.snapshotState();
        escrow.settle(BATCH, transaction, proof, new bytes32[](0), 0);
        assertTrue(escrow.payment(BATCH).settled);
        assertEq(token.totalSupply(), (uint256(principal) - 330) * SAT);
        assertTrue(vm.revertToState(snapshot));
        snapshot = vm.snapshotState();
        vm.warp(block.timestamp + 7201);
        vm.expectRevert("SPV proof");
        escrow.settle(BATCH, transaction, proof, new bytes32[](0), 0);
        _collateralUnchanged(transaction, key);
        assertTrue(vm.revertToState(snapshot));
        _halt(relay);
        vm.expectRevert("SPV proof");
        escrow.settle(BATCH, transaction, proof, new bytes32[](0), 0);
        _collateralUnchanged(transaction, key);
    }

    function _collateralUnchanged(bytes memory transaction, bytes32 key) private {
        assertFalse(escrow.payment(BATCH).settled);
        assertEq(token.totalSupply(), uint256(principal) * SAT);
        assertEq(token.balanceOf(address(escrow)), 330 * SAT);
        assertEq(deposits.backingSats(keccak256(abi.encode(digest(coinbase), uint32(0)))), principal);
        assertEq(deposits.backingSats(keccak256(abi.encode(digest(transaction), uint32(1)))), 0);
        vm.expectRevert("signature may exist");
        escrow.refund(key);
    }

    function _halt(MainnetRelayHarness relay) private {
        bytes32 oldHead = relay.getMainchainHead();
        relay.syntheticChild(relay.checkpointHash(), 1e30, bytes32(uint256(99)), uint32(block.timestamp));
        assertTrue(relay.halted());
        assertEq(relay.getMainchainHead(), oldHead);
    }

    function _le64(uint64 value) private pure returns (bytes8 result) {
        for (uint256 i; i < 8; ++i) {
            result |= bytes8(uint64(uint256(uint8(value >> (8 * i))) << (8 * (7 - i))));
        }
    }
}
