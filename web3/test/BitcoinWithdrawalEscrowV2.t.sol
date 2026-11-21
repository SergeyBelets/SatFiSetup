// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {BitcoinWithdrawalEscrowV2} from "../contracts/src/federation/BitcoinWithdrawalEscrowV2.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {BitcoinTxParser} from "../contracts/src/core/BitcoinTxParser.sol";
import {EconomyApproval} from "./ConfederationDAOIncome.t.sol";

contract WithdrawalBitcoinFixtures {
    struct OutputValue { uint64 sats; bytes script; bool known; }
    EconomyApproval public immutable authorization;
    address public spendRecorder;
    address public token;
    address public constant feeReserve = address(0);
    bytes32 public input;
    bytes public script;
    bytes32 public prev;
    mapping(bytes32 => bytes32) public replacementOf;
    mapping(bytes32 => BitcoinBatchRegistry.State) public batchState;
    uint256 public burned;

    constructor(EconomyApproval auth, bytes32 prev_, bytes32 key, bytes memory script_) {
        authorization = auth;
        prev = prev_;
        input = key;
        script = script_;
    }

    function configure(address escrow, address token_) external { spendRecorder = escrow; token = token_; }
    function getInputs(bytes32) external view returns (bytes32[] memory values) {
        values = new bytes32[](1);
        values[0] = input;
    }
    function stage(bytes32 batch, BitcoinBatchRegistry.State state) external { batchState[batch] = state; }
    function replace(bytes32 batch, bytes32 previous) external { replacementOf[batch] = previous; }
    function poolScripts(address) external pure returns (bytes32) { return bytes32(uint256(1)); }
    function scriptPools(bytes32 key) external view returns (address) {
        return key == keccak256(script) ? address(this) : address(0);
    }
    function backingSats(bytes32 key) external view returns (uint256) {
        return key == keccak256(abi.encode(prev, uint32(0))) ? 10_000 : 0;
    }
    function getValue(bytes32 key) external view returns (OutputValue memory) {
        return OutputValue(10_000, script, key == keccak256(abi.encode(prev, uint32(0))));
    }
    function recordFamily(bytes32) external {}
    function recordSpend(bytes calldata, uint256 sats) external { burned = sats; }
    function mpToken() external view returns (address) { return token; }
    function shareToken() external view returns (address) { return token; }
    function deposits() external view returns (address) { return address(this); }
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
    mapping(uint256 => bool) public paid;
    address public bitcoinReserve;
    address public shareToken;

    constructor(BitcoinWithdrawalEscrowV2 escrow_, address auth) { withdrawalEscrow = escrow_; frostMultisig = auth; }
    function getPoolCount() external pure returns (uint256) { return 0; }
    function open(uint256 id, WithdrawalAsset mp, uint64 sats, bytes calldata script, uint64 limit) external {
        bitcoinReserve = mp.poolDAO();
        shareToken = address(mp);
        uint256 amount = uint256(sats) * 1e10;
        minerFeeLimitSats[id] = limit;
        mp.mint(address(this), amount);
        mp.approve(address(withdrawalEscrow), amount);
        withdrawalEscrow.lock(id, address(mp), msg.sender, amount, script);
    }
    function settleWithdrawal(uint256 id) external { paid[id] = true; }
}

contract BitcoinWithdrawalEscrowV2Test is Test {
    bytes32 private constant PREV = bytes32(uint256(123));
    bytes32 private constant BATCH = bytes32(uint256(8));
    bytes32 private constant REPLACEMENT = bytes32(uint256(9));
    bytes private constant PAYOUT = hex"00141111111111111111111111111111111111111111";
    bytes private constant CHANGE = hex"51202222222222222222222222222222222222222222222222222222222222222222";

    EconomyApproval private auth;
    WithdrawalBitcoinFixtures private fixtures;
    WithdrawalPoolFixture private pool;
    WithdrawalAsset private mp;
    BitcoinWithdrawalEscrowV2 private escrow;

    function setUp() public {
        auth = new EconomyApproval();
        bytes32 prevDisplay = bytes32(uint256(123) << 248);
        fixtures = new WithdrawalBitcoinFixtures(auth, PREV, keccak256(abi.encode(prevDisplay, uint32(0))), CHANGE);
        escrow = new BitcoinWithdrawalEscrowV2(address(fixtures), address(fixtures), 6, 294, 330, 100);
        mp = new WithdrawalAsset(address(fixtures));
        pool = new WithdrawalPoolFixture(escrow, address(auth));
        fixtures.configure(address(escrow), address(mp));
        bytes32 op = escrow.getSetDepositRegistryOperationHash(address(fixtures));
        auth.authorize(op, address(escrow));
        escrow.setDepositRegistry(address(fixtures), op);
        op = escrow.getSetCallbackPoolRegistryOperationHash(address(fixtures));
        auth.authorize(op, address(escrow));
        escrow.setCallbackPoolRegistry(address(fixtures), op);
        op = escrow.getRequireLocalCustodyOperationHash(address(pool));
        auth.authorize(op, address(escrow));
        escrow.requireLocalCustody(address(pool), op);
        fixtures.stage(BATCH, BitcoinBatchRegistry.State.Reserved);
    }

    function littleEndian(uint64 amount) private pure returns (bytes8) {
        uint64 reversed;
        for (uint256 i; i < 8; ++i) reversed |= uint64(uint256(uint8(amount >> (8 * i))) << (56 - i * 8));
        return bytes8(reversed);
    }

    function rawTx(uint64 payout, uint64 change) private pure returns (bytes memory) {
        return abi.encodePacked(hex"0200000001", PREV, hex"0000000000fdffffff02",
            littleEndian(payout), hex"16", PAYOUT, littleEndian(change), hex"22", CHANGE, hex"00000000");
    }

    function bind(bytes memory raw, bytes32[] memory keys) private {
        bytes32 op = escrow.getBindUserFundedOperationHash(BATCH, sha256(abi.encodePacked(sha256(raw))), keys);
        auth.authorize(op, address(escrow));
        escrow.bindUserFunded(BATCH, raw, keys, op);
    }

    function testDuplicateDestinationPaysOncePerRequestAndReplacementChangesBothShares() public {
        pool.open(1, mp, 3000, PAYOUT, 70);
        pool.open(2, mp, 4000, PAYOUT, 70);
        bytes32[] memory keys = new bytes32[](2);
        keys[0] = escrow.requestKey(address(pool), 1);
        keys[1] = escrow.requestKey(address(pool), 2);
        bind(rawTx(6899, 3000), keys);
        assertEq(escrow.minerFee(BATCH), 101);

        fixtures.stage(BATCH, BitcoinBatchRegistry.State.Signing);
        fixtures.stage(REPLACEMENT, BitcoinBatchRegistry.State.Reserved);
        fixtures.replace(REPLACEMENT, BATCH);
        bytes memory replacement = rawTx(6869, 3000);
        bytes32 op = escrow.getBindUserFundedReplacementOperationHash(BATCH, REPLACEMENT,
            sha256(abi.encodePacked(sha256(replacement))));
        auth.authorize(op, address(escrow));
        escrow.bindUserFundedReplacement(BATCH, REPLACEMENT, replacement, op);
        assertEq(escrow.minerFee(REPLACEMENT), 131);

        fixtures.stage(REPLACEMENT, BitcoinBatchRegistry.State.Signing);
        escrow.settle(REPLACEMENT, replacement, bytes32(0), new bytes32[](0), 0);
        assertEq(fixtures.burned(), 7000);
        assertEq(mp.totalSupply(), 0);
        assertTrue(pool.paid(1) && pool.paid(2));
        vm.expectRevert("input mismatch or spent");
        escrow.settle(BATCH, rawTx(6899, 3000), bytes32(0), new bytes32[](0), 0);
        assertEq(fixtures.burned(), 7000);
    }

    function testFeeCapAndOutputMinimumApplyToEveryRequest() public {
        pool.open(1, mp, 294, PAYOUT, 100);
        pool.open(2, mp, 1000, PAYOUT, 50);
        bytes32[] memory keys = new bytes32[](2);
        keys[0] = escrow.requestKey(address(pool), 1);
        keys[1] = escrow.requestKey(address(pool), 2);
        bytes memory raw = rawTx(1293, 8706);
        bytes32 op = escrow.getBindUserFundedOperationHash(BATCH, sha256(abi.encodePacked(sha256(raw))), keys);
        auth.authorize(op, address(escrow));
        vm.expectRevert("below withdrawal minimum");
        escrow.bindUserFunded(BATCH, raw, keys, op);
        keys[0] = keys[1];
        keys[1] = escrow.requestKey(address(pool), 1);
        raw = rawTx(1193, 8706);
        op = escrow.getBindUserFundedOperationHash(BATCH, sha256(abi.encodePacked(sha256(raw))), keys);
        auth.authorize(op, address(escrow));
        vm.expectRevert(BitcoinWithdrawalEscrowV2.InvalidFeePlan.selector);
        escrow.bindUserFunded(BATCH, raw, keys, op);
    }
}
