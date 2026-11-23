// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {BitcoinDepositRegistry} from "../contracts/src/federation/BitcoinDepositRegistry.sol";
import {BitcoinWithdrawalEscrowV2} from "../contracts/src/federation/BitcoinWithdrawalEscrowV2.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {BitcoinWithdrawalFeeVerifier} from "../contracts/src/federation/BitcoinWithdrawalFeeVerifier.sol";
import {BitcoinTxParser} from "../contracts/src/core/BitcoinTxParser.sol";
import {EconomyApproval} from "./ConfederationDAOIncome.t.sol";

contract WithdrawalBitcoinFixtures {
    EconomyApproval public immutable authorization;
    BitcoinWithdrawalFeeVerifier public immutable withdrawalFeeVerifier;
    address public spendRecorder;
    address public deposits;
    address public token;
    address public constant feeReserve = address(0);
    address public constant recoveryController = address(0);
    bytes32 public constant walletResult = keccak256("test withdrawal wallet");
    bytes32 public immutable input;
    mapping(bytes32 => bytes32) public replacementOf;
    mapping(bytes32 => bytes32) public reservations;
    mapping(bytes32 => BitcoinBatchRegistry.State) public batchState;
    mapping(bytes32 => bytes32) public batchWalletResult;

    constructor(EconomyApproval auth, bytes32 key) {
        authorization = auth;
        withdrawalFeeVerifier = new BitcoinWithdrawalFeeVerifier();
        input = key;
    }

    function configure(address escrow, address token_, address registry) external {
        spendRecorder = escrow;
        token = token_;
        deposits = registry;
    }
    function getInputs(bytes32) external view returns (bytes32[] memory values) {
        values = new bytes32[](1);
        values[0] = input;
    }
    function stage(bytes32 batch, BitcoinBatchRegistry.State state) external {
        batchState[batch] = state;
        batchWalletResult[batch] = walletResult;
    }
    function replace(bytes32 batch, bytes32 previous) external { replacementOf[batch] = previous; }
    function mpToken() external view returns (address) { return token; }
    function shareToken() external view returns (address) { return token; }
    function testHelpersEnabled() external pure returns (bool) { return false; }
    function getBlockStatus(bytes32) external pure returns (bool, uint64) { return (true, 6); }
    function checkTxInclusion(bytes32, bytes32, bytes32[] calldata, uint256) external pure returns (bool) { return true; }
    function escrow() external view returns (address) { return spendRecorder; }
    function validateWithdrawal(address, uint256, address, address, uint256, bytes calldata, bool) external pure {}
}

contract WithdrawalAsset is ERC20 {
    address public immutable poolDAO;
    constructor(address registry) ERC20("Backing", "MP") { poolDAO = registry; }
    function mint(address to, uint256 amount) external { _mint(to, amount); }
    function burnHeld(uint256 amount) external { _burn(msg.sender, amount); }
}

contract WithdrawalPoolFixture {
    BitcoinWithdrawalEscrowV2 public immutable withdrawalEscrow;
    address public immutable frostMultisig;
    mapping(uint256 => uint64) public minerFeeLimitSats;
    mapping(uint256 => uint64) public minimumPayoutSats;
    mapping(uint256 => bool) public paid;
    address public bitcoinReserve;
    address public shareToken;

    constructor(BitcoinWithdrawalEscrowV2 escrow_, address auth) { withdrawalEscrow = escrow_; frostMultisig = auth; }
    function getPoolCount() external pure returns (uint256) { return 0; }
    function open(uint256 id, WithdrawalAsset mp, uint64 sats, bytes calldata script, uint64 limit, uint64 minimum)
        external
    {
        bitcoinReserve = mp.poolDAO();
        shareToken = address(mp);
        uint256 amount = uint256(sats) * 1e10;
        minerFeeLimitSats[id] = limit;
        minimumPayoutSats[id] = minimum;
        mp.mint(address(this), amount);
        mp.approve(address(withdrawalEscrow), amount);
        withdrawalEscrow.lock(id, address(mp), msg.sender, amount, script);
    }
    function settleWithdrawal(uint256 id) external { paid[id] = true; }
}

contract BitcoinWithdrawalEscrowV2Test is Test {
    bytes32 private constant BATCH = bytes32(uint256(8));
    bytes32 private constant REPLACEMENT = bytes32(uint256(9));
    bytes private constant PAYOUT = hex"00141111111111111111111111111111111111111111";
    bytes private constant CHANGE = hex"51202222222222222222222222222222222222222222222222222222222222222222";

    EconomyApproval private auth;
    WithdrawalBitcoinFixtures private fixtures;
    WithdrawalPoolFixture private pool;
    WithdrawalAsset private mp;
    BitcoinWithdrawalEscrowV2 private escrow;
    BitcoinDepositRegistry private deposits;
    bytes32 private prev;

    function setUp() public {
        auth = new EconomyApproval();
        bytes memory funding = abi.encodePacked(hex"0200000001", bytes32(uint256(123)), hex"0000000000ffffffff01",
            littleEndian(10_000), hex"22", CHANGE, hex"00000000");
        prev = _txid(funding);
        fixtures = new WithdrawalBitcoinFixtures(
            auth, keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev), uint32(0)))
        );
        escrow = new BitcoinWithdrawalEscrowV2(address(fixtures), address(fixtures), 6, 294, 330, 100);
        deposits = new BitcoinDepositRegistry(address(fixtures), address(auth), 6, address(escrow));
        mp = new WithdrawalAsset(address(fixtures));
        pool = new WithdrawalPoolFixture(escrow, address(auth));
        fixtures.configure(address(escrow), address(mp), address(deposits));
        bytes32 op = escrow.getSetDepositRegistryOperationHash(address(deposits));
        auth.authorize(op, address(escrow));
        escrow.setDepositRegistry(address(deposits), op);
        op = escrow.getSetCallbackPoolRegistryOperationHash(address(fixtures));
        auth.authorize(op, address(escrow));
        escrow.setCallbackPoolRegistry(address(fixtures), op);
        op = escrow.getRequireLocalCustodyOperationHash(address(pool));
        auth.authorize(op, address(escrow));
        escrow.requireLocalCustody(address(pool), op);
        fixtures.stage(BATCH, BitcoinBatchRegistry.State.Reserved);

        _register(address(fixtures), CHANGE);
        vm.prank(address(fixtures));
        deposits.consume(funding, 0, bytes32(0), new bytes32[](0), 0);
        assertEq(deposits.totalBackingSats(), 10_000);
    }

    function littleEndian(uint64 amount) private pure returns (bytes8) {
        uint64 reversed;
        for (uint256 i; i < 8; ++i) reversed |= uint64(uint256(uint8(amount >> (8 * i))) << (56 - i * 8));
        return bytes8(reversed);
    }

    function rawTx(uint64 payout, uint64 change) private view returns (bytes memory) {
        return abi.encodePacked(hex"0200000001", prev, hex"0000000000fdffffff02",
            littleEndian(payout), hex"16", PAYOUT, littleEndian(change), hex"22", CHANGE, hex"00000000");
    }

    function _txid(bytes memory raw) private pure returns (bytes32) {
        return sha256(abi.encodePacked(sha256(raw)));
    }

    function _register(address reserve, bytes memory script) private {
        bytes32 op = deposits.getRegisterOperationHash(reserve, script);
        auth.authorize(op, address(deposits));
        deposits.register(reserve, script, op);
    }

    function _assertOnlyChangeIsBacking(bytes memory raw, uint256 expectedChange) private view {
        bytes32 txid = _txid(raw);
        bytes32 payout = keccak256(abi.encode(txid, uint32(0)));
        bytes32 change = keccak256(abi.encode(txid, uint32(1)));
        assertFalse(deposits.walletOutput(payout));
        assertEq(deposits.backingSats(payout), 0);
        assertTrue(deposits.walletOutput(change));
        assertEq(deposits.backingSats(change), expectedChange);
        assertEq(deposits.totalBackingSats(), expectedChange);
        assertEq(deposits.backingCount(), 1);
    }

    function bind(bytes memory raw, bytes32[] memory keys) private {
        bytes32 op = escrow.getBindUserFundedOperationHash(BATCH, _txid(raw), keys);
        auth.authorize(op, address(escrow));
        escrow.bindUserFunded(BATCH, raw, keys, op);
    }

    function testDuplicateDestinationPaysOncePerRequestAndReplacementChangesBothShares() public {
        pool.open(1, mp, 3000, PAYOUT, 70, 2930);
        pool.open(2, mp, 4000, PAYOUT, 70, 3930);
        bytes32[] memory keys = new bytes32[](2);
        keys[0] = escrow.requestKey(address(pool), 1);
        keys[1] = escrow.requestKey(address(pool), 2);
        bind(rawTx(6899, 3000), keys);
        assertEq(escrow.minerFee(BATCH), 101);

        // A later deposit registration leaves the bound payout role intact
        // for the original and for any explicitly bound replacement.
        _register(address(mp), PAYOUT);
        fixtures.stage(BATCH, BitcoinBatchRegistry.State.Signing);
        fixtures.stage(REPLACEMENT, BitcoinBatchRegistry.State.Reserved);
        fixtures.replace(REPLACEMENT, BATCH);
        bytes memory tooExpensive = rawTx(6859, 3000);
        bytes32 invalid = escrow.getBindUserFundedReplacementOperationHash(BATCH, REPLACEMENT, _txid(tooExpensive));
        auth.authorize(invalid, address(escrow));
        vm.expectRevert(BitcoinWithdrawalEscrowV2.InvalidFeePlan.selector);
        escrow.bindUserFundedReplacement(BATCH, REPLACEMENT, tooExpensive, invalid);

        bytes memory replacement = rawTx(6860, 3000);
        bytes32 op = escrow.getBindUserFundedReplacementOperationHash(BATCH, REPLACEMENT, _txid(replacement));
        auth.authorize(op, address(escrow));
        escrow.bindUserFundedReplacement(BATCH, REPLACEMENT, replacement, op);
        assertEq(escrow.minerFee(REPLACEMENT), 140);

        fixtures.stage(REPLACEMENT, BitcoinBatchRegistry.State.Signing);
        escrow.settle(REPLACEMENT, replacement, bytes32(0), new bytes32[](0), 0);
        _assertOnlyChangeIsBacking(replacement, 3000);
        assertEq(mp.totalSupply(), 0);
        assertTrue(pool.paid(1) && pool.paid(2));
        vm.expectRevert("input mismatch or spent");
        escrow.settle(BATCH, rawTx(6899, 3000), bytes32(0), new bytes32[](0), 0);
        assertEq(deposits.totalBackingSats(), 3000);
    }

    function testSettlementKeepsPayoutExternalAfterRegistration() public {
        pool.open(1, mp, 7000, PAYOUT, 100, 6900);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = escrow.requestKey(address(pool), 1);
        bytes memory raw = rawTx(6900, 3000);
        vm.expectRevert("recorder");
        deposits.recordSpendWithChange(raw, 7000, 2);
        bind(raw, keys);
        fixtures.stage(BATCH, BitcoinBatchRegistry.State.Signing);

        _register(address(mp), PAYOUT);
        escrow.settle(BATCH, raw, bytes32(0), new bytes32[](0), 0);
        _assertOnlyChangeIsBacking(raw, 3000);
        assertTrue(pool.paid(1));
        assertEq(mp.totalSupply(), 0);

        vm.expectRevert("wallet spend is not new backing");
        vm.prank(address(mp));
        deposits.consume(raw, 0, bytes32(0), new bytes32[](0), 0);
        _assertOnlyChangeIsBacking(raw, 3000);
    }

    function testCannotBindNewPayoutAfterItBecomesRegistered() public {
        pool.open(1, mp, 7000, PAYOUT, 100, 6900);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = escrow.requestKey(address(pool), 1);
        _register(address(mp), PAYOUT);

        bytes memory raw = rawTx(6900, 3000);
        bytes32 op = escrow.getBindUserFundedOperationHash(BATCH, _txid(raw), keys);
        auth.authorize(op, address(escrow));
        vm.expectRevert(BitcoinWithdrawalEscrowV2.InvalidFeePlan.selector);
        escrow.bindUserFunded(BATCH, raw, keys, op);
        assertEq(uint256(escrow.getRequest(keys[0]).state), uint256(BitcoinWithdrawalEscrowV2.State.Pending));
        assertEq(deposits.totalBackingSats(), 10_000);
    }

    function testFeeCapAndOutputMinimumApplyToEveryRequest() public {
        vm.expectRevert(BitcoinWithdrawalEscrowV2.InvalidFeePlan.selector);
        pool.open(1, mp, 294, PAYOUT, 100, 294);
        pool.open(1, mp, 394, PAYOUT, 100, 294);
        pool.open(2, mp, 1000, PAYOUT, 50, 950);
        bytes32[] memory keys = new bytes32[](2);
        keys[0] = escrow.requestKey(address(pool), 2);
        keys[1] = escrow.requestKey(address(pool), 1);
        bytes memory raw = rawTx(1293, 8606);
        bytes32 op = escrow.getBindUserFundedOperationHash(BATCH, _txid(raw), keys);
        auth.authorize(op, address(escrow));
        vm.expectRevert(BitcoinWithdrawalEscrowV2.InvalidFeePlan.selector);
        escrow.bindUserFunded(BATCH, raw, keys, op);
        assertEq(escrow.minerFee(BATCH), 0);

        (keys[0], keys[1]) = (keys[1], keys[0]);
        bind(raw, keys);
        assertEq(escrow.minerFee(BATCH), 101);
    }
}
