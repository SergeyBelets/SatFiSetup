// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./MainnetRelayFixture.sol";
import "./EscrowBoundary.t.sol";
import "../contracts/src/federation/BitcoinDepositRegistry.sol";

/// @dev Authentication/batch lifecycle boundaries only. Deposit, token, escrow and relay checks are real.
contract MainnetBatchBoundaryFixture is EscrowBatchFixture {
    mapping(bytes32 => bytes32) public reservations;
}

contract MainnetMiningPoolFixture {
    PoolMpToken public mpToken = new PoolMpToken("Mainnet test MP", "MMP", address(this), false);

    function deposit(BitcoinDepositRegistry registry, bytes calldata transaction, bytes32 blockHash, address recipient)
        external
        returns (uint64 sats)
    {
        sats = registry.consume(transaction, 0, blockHash, new bytes32[](0), 0);
        mpToken.mint(recipient, uint256(sats) * 1e10);
    }

    function output(bytes calldata transaction) external pure returns (uint64, bytes memory) {
        return BitcoinTxParser.outputAt(transaction, 0);
    }
}

contract MainnetRelayIntegrationTest is MainnetFixtureBase {
    MainnetRelayHarness internal relay;
    MainnetBatchBoundaryFixture internal batches;
    MainnetMiningPoolFixture internal mining;
    BitcoinDepositRegistry internal deposits;
    BitcoinWithdrawalEscrow internal escrow;
    PoolMpToken internal token;
    bytes internal coinbase;
    bytes internal treasuryScript;
    bytes internal withdrawalScript = hex"00141111111111111111111111111111111111111111";
    uint64 internal principal;
    uint256 internal constant SAT = 1e10;
    bytes32 internal constant BATCH = bytes32(uint256(1));

    function setUp() public {
        loadMainnetFixture();
        relay = newHarness(100);
        string memory fixture = vm.readFile("test/fixtures/mainnet/checkpoint-coinbase.json");
        coinbase = vm.parseBytes(string.concat("0x", vm.parseJsonString(fixture, ".raw_tx")));
        _system(address(relay));
        for (uint64 height = CHECKPOINT + 1; height <= CHECKPOINT + 101; ++height) {
            relay.addBlockHeader(headerAt(height));
        }
    }

    function _system(address activeRelay) internal {
        batches = new MainnetBatchBoundaryFixture();
        mining = new MainnetMiningPoolFixture();
        token = mining.mpToken();
        escrow = new BitcoinWithdrawalEscrow(address(batches), activeRelay, 6, 294, 330);
        deposits = new BitcoinDepositRegistry(activeRelay, batches.authorization(), 6, address(escrow));
        (principal, treasuryScript) = mining.output(coinbase);
        deposits.register(
            address(mining), treasuryScript, deposits.getRegisterOperationHash(address(mining), treasuryScript)
        );
        escrow.setDepositRegistry(address(deposits), escrow.getSetDepositRegistryOperationHash(address(deposits)));
        token.approve(address(escrow), type(uint256).max);
    }

    function testProductionRelayAndRealMainnetCoinbaseMintThroughDepositRegistry() public {
        MainnetHeaderRelay production = newRelay(100);
        _system(address(production));
        for (uint64 height = CHECKPOINT + 1; height <= CHECKPOINT + 99; ++height) {
            production.addBlockHeader(headerAt(height));
        }
        assertTrue(production.checkTxInclusion(wireHash(CHECKPOINT), digest(coinbase), new bytes32[](0), 0));
        uint64 credited = mining.deposit(deposits, coinbase, wireHash(CHECKPOINT), address(this));
        assertEq(credited, 5000000000);
        assertEq(token.balanceOf(address(this)), uint256(credited) * SAT);
        assertEq(deposits.backingSats(keccak256(abi.encode(digest(coinbase), uint32(0)))), credited);
        assertEq(address(escrow.spv()), address(production));
        assertFalse(production.testHelpersEnabled());
    }

    function testPersistentHaltPreventsRealDepositProofAndMPMint() public {
        uint256 snapshot = vm.snapshotState();
        mining.deposit(deposits, coinbase, wireHash(CHECKPOINT), address(this));
        assertEq(token.totalSupply(), uint256(principal) * SAT);
        assertTrue(vm.revertToState(snapshot));
        _halt();
        bytes32 checkpoint = wireHash(CHECKPOINT);
        vm.expectRevert("deposit proof");
        mining.deposit(deposits, coinbase, checkpoint, address(this));
        assertEq(token.totalSupply(), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(digest(coinbase), uint32(0)))), 0);
    }

    function testActiveEscrowSettlesAndSameProofFailsAfterPersistentHalt() public {
        (bytes memory transaction, bytes32 proofBlock, bytes32 key) = _preparePayment();
        uint256 snapshot = vm.snapshotState();
        escrow.settle(BATCH, transaction, proofBlock, new bytes32[](0), 0);
        assertTrue(escrow.payment(BATCH).settled);
        assertEq(token.totalSupply(), (uint256(principal) - 330) * SAT);
        assertEq(token.balanceOf(address(escrow)), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(digest(transaction), uint32(1)))), principal - 330);
        assertTrue(vm.revertToState(snapshot));
        _halt();
        vm.expectRevert("SPV proof");
        escrow.settle(BATCH, transaction, proofBlock, new bytes32[](0), 0);
        assertFalse(escrow.payment(BATCH).settled);
        assertEq(token.totalSupply(), uint256(principal) * SAT);
        assertEq(token.balanceOf(address(escrow)), 330 * SAT);
        vm.expectRevert("signature may exist");
        escrow.refund(key);
    }

    function _preparePayment() internal returns (bytes memory transaction, bytes32 proofBlock, bytes32 key) {
        mining.deposit(deposits, coinbase, wireHash(CHECKPOINT), address(this));
        escrow.requestDirect(1, address(token), 330 * SAT, withdrawalScript);
        key = escrow.requestKey(address(this), 1);
        transaction = abi.encodePacked(
            hex"0200000001",
            digest(coinbase),
            hex"0000000000ffffffff02",
            _le64(330),
            bytes1(uint8(withdrawalScript.length)),
            withdrawalScript,
            _le64(principal - 330),
            bytes1(uint8(treasuryScript.length)),
            treasuryScript,
            hex"00000000"
        );
        bytes32 input = keccak256(abi.encode(BitcoinTxParser.flipBytes32(digest(coinbase)), uint32(0)));
        batches.configure(input, BitcoinBatchRegistry.State.Reserved);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = key;
        escrow.bind(BATCH, digest(transaction), keys, escrow.getBindOperationHash(BATCH, digest(transaction), keys));
        batches.configure(input, BitcoinBatchRegistry.State.Signing);
        // A synthetic payment root isolates the escrow/halt boundary. Real-header PoW and retarget
        // acceptance are separately checked against all 4,033 checkpoint-and-forward mainnet headers.
        proofBlock = relay.syntheticChild(relay.getMainchainHead(), 1, digest(transaction), 1300000000);
        bytes32 head = proofBlock;
        for (uint32 i = 1; i <= 5; ++i) {
            head = relay.syntheticChild(head, 1, bytes32(0), 1300000000 + i);
        }
    }

    function _halt() internal {
        bytes32 oldHead = relay.getMainchainHead();
        relay.syntheticChild(relay.checkpointHash(), 1e30, bytes32(uint256(99)), 1301000000);
        assertTrue(relay.halted());
        assertEq(relay.getMainchainHead(), oldHead);
    }

    function _le64(uint64 value) internal pure returns (bytes8 result) {
        for (uint256 i; i < 8; ++i) {
            result |= bytes8(uint64(uint8(value >> (8 * i))) << (8 * (7 - i)));
        }
    }
}
