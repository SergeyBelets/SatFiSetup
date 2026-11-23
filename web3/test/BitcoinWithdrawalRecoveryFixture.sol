// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {IPaymentSPV} from "../contracts/src/federation/BitcoinWithdrawalEscrow.sol";
import {BitcoinWithdrawalEscrowV2} from "../contracts/src/federation/BitcoinWithdrawalEscrowV2.sol";
import {BitcoinWithdrawalRecovery} from "../contracts/src/federation/BitcoinWithdrawalRecovery.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {BitcoinDepositRegistry} from "../contracts/src/federation/BitcoinDepositRegistry.sol";
import {BitcoinTxParser} from "../contracts/src/core/BitcoinTxParser.sol";
import {CallbackPoolRegistry} from "../contracts/src/federation/CallbackPoolRegistry.sol";
import {ConfederationBitcoinReserve} from "../contracts/src/federation/ConfederationBitcoinReserve.sol";
import {ConfederationPoolV2} from "../contracts/src/federation/ConfederationPoolV2.sol";
import {ConfederationPoolV2Deployer} from "../contracts/src/federation/ConfederationPoolV2Deployer.sol";
import {ConfederationShareV2Deployer} from "../contracts/src/federation/ConfederationShareV2Deployer.sol";
import {ConfederationShareV2} from "../contracts/src/tokens/ConfederationShareV2.sol";
import {ConfederationDAOStake} from "../contracts/src/federation/ConfederationDAOStake.sol";
import {ConfederationDAOIncome} from "../contracts/src/federation/ConfederationDAOIncome.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {FederationFeeManager} from "../contracts/src/federation/FederationFeeManager.sol";
import {IFeeManager} from "../contracts/src/federation/IFeeManager.sol";
import {EconomyAsset, EconomySettlementLifecycle} from "./ConfederationDAOIncome.t.sol";

/// @dev Isolate DKG/EVM FROST authorization from the actual Bitcoin SPV and monetary contracts.
contract RecoveryAuthorizationFixture {
    address private immutable member;
    bytes32 private constant WALLET_KEY =
        0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798;
    bytes32 private constant SUCCESSOR_WALLET_KEY =
        0xc6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5;
    bytes32 private deniedOperation;
    bytes32 private lostBitcoinWallet;
    error MockOperationNotApproved();

    constructor(address member_) {
        member = member_;
    }

    function coordinator() external view returns (address) { return address(this); }
    function dkgRegistry() external view returns (address) { return address(this); }
    function threshold() external pure returns (uint256) { return 2; }
    function isSessionParticipant(bytes32 wallet, address candidate) external view returns (bool) {
        return wallet != lostBitcoinWallet && candidate == member;
    }
    function getParticipants() public view returns (address[] memory roster) {
        roster = new address[](2);
        roster[0] = member;
        roster[1] = address(0xE);
    }
    function getCompletedResult(bytes32 id) external view returns (
        bytes32, bytes32, uint64, bytes32, bytes memory, address[] memory, uint256
    ) {
        uint256 wallet = uint256(id);
        require(wallet > 0 && wallet <= 5, "unfinished DKG");
        address[] memory roster = getParticipants();
        if (wallet == 5) roster[1] = address(0xF);
        return (bytes32(0), id, uint64(wallet), wallet == 4 ? keccak256("EVM") : keccak256("BTC_SPEND"),
            abi.encodePacked(wallet == 1 || wallet == 3 ? WALLET_KEY : SUCCESSOR_WALLET_KEY), roster, 2);
    }
    function hashOperation(address target, bytes4 method, bytes32 args, uint256 nonce) external pure returns (bytes32) {
        return keccak256(abi.encode(target, method, args, nonce));
    }
    function denyOperation(bytes32 op) external { deniedOperation = op; }
    function loseBitcoinQuorum(bytes32 wallet) external { lostBitcoinWallet = wallet; }
    function consumeOperation(bytes32 op) external view returns (bool) {
        if (op == deniedOperation) revert MockOperationNotApproved();
        return true;
    }
    function isOperationApproved(bytes32 op, address) external view returns (bool) { return op != deniedOperation; }
}

/// @dev Both SPV routes run the same monetary scenarios. Bitcoin transaction bytes are
/// synthetic fixtures. The concrete backend verifies real header/transaction commitments.
abstract contract BitcoinWithdrawalRecoveryScenarios is Test {
    uint256 private constant SAT = 1e10;
    address private constant OWNER = address(0xA);
    address private constant OTHER = address(0xB);
    address private constant GUARDIAN = address(0xD);
    bytes32 private constant BATCH = bytes32(uint256(10));
    bytes32 private constant REPLACEMENT = bytes32(uint256(11));
    bytes32 private constant SUCCESSOR_BATCH = bytes32(uint256(12));
    bytes32 private constant SUCCESSOR_WALLET = bytes32(uint256(2));
    bytes32 private constant DECOY = bytes32(uint256(45));
    uint32 private constant COLD_HEIGHT = 10;
    uint32 private constant COLD_DEPTH = 8;
    uint64 private constant BACKING = 100_000;
    uint64 private constant SPONSOR_SATS = 3_000;
    uint64 private constant IDLE_BACKING = 20_000;
    uint64 private constant REQUESTED = 40_000;
    uint64 private constant PROTOCOL_FEE = 400;
    uint64 private constant PRINCIPAL = REQUESTED - PROTOCOL_FEE;
    uint64 private constant FEE = 150;
    bytes private constant PAYOUT = hex"00141111111111111111111111111111111111111111";
    bytes private constant SPONSOR = hex"00142222222222222222222222222222222222222222";
    bytes private constant SUCCESSOR =
        hex"5120c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5";

    RecoveryAuthorizationFixture private authorization;
    BitcoinWithdrawalEscrowV2 private escrow;
    BitcoinWithdrawalRecovery private recovery;
    BitcoinBatchRegistry private batches;
    BitcoinDepositRegistry private deposits;
    ConfederationBitcoinReserve private reserve;
    ConfederationPoolV2 private pool;
    ConfederationShareV2 private shares;
    CustodianValidator private validator;
    ConfederationDAOIncome private income;
    IPaymentSPV private spv;
    bytes32 private fundingTxid;
    bytes private sponsorFunding;
    uint256 private withdrawal;

    function _initializeSpv() internal virtual returns (IPaymentSPV);
    function _anchor(bytes32 merkleRoot, uint64 confirmations, uint64 minimumHeight)
        internal virtual returns (bytes32);
    function _advance(uint64 count) internal virtual;
    function _removeAnchor(bytes32 blockHash) internal virtual;

    function setUp() public {
        vm.warp(1_400_000_000);
        spv = _initializeSpv();
        _deploy();
        _fund();
    }

    function _deploy() private {
        authorization = new RecoveryAuthorizationFixture(address(this));
        address auth = address(authorization);
        batches = new BitcoinBatchRegistry(auth, auth, bytes32(uint256(1)));
        escrow = new BitcoinWithdrawalEscrowV2(address(batches), address(spv), 6, 294, 330, 200);
        batches.setWithdrawalEscrow(address(escrow), batches.getSetWithdrawalEscrowOperationHash(address(escrow)));
        deposits = new BitcoinDepositRegistry(address(spv), auth, 6, address(escrow));
        escrow.setDepositRegistry(address(deposits), escrow.getSetDepositRegistryOperationHash(address(deposits)));

        address[] memory custodians = new address[](2);
        custodians[0] = OWNER;
        custodians[1] = OTHER;
        validator = new CustodianValidator(custodians, 2, auth);
        ConfederationShareV2Deployer shareDeployer = new ConfederationShareV2Deployer();
        ConfederationPoolV2Deployer poolDeployer = new ConfederationPoolV2Deployer(address(shareDeployer));
        shareDeployer.setAuthorizedDeployer(address(poolDeployer), true);
        poolDeployer.setAuthorizedDeployer(address(this), true);
        (address pool_, address token) = poolDeployer.deployMultiPool("recovery", auth, address(validator), "Share", "S");
        pool = ConfederationPoolV2(pool_);
        shares = ConfederationShareV2(token);
        _installIncome(auth);
        bytes32 op = pool.getSetWithdrawalEscrowOperationHash(address(escrow));
        _approve(op);
        pool.setWithdrawalEscrow(address(escrow), op);

        reserve = new ConfederationBitcoinReserve("recovery", address(deposits), address(escrow), pool_);
        bytes memory script = reserve.payoutScript();
        deposits.register(address(reserve), script, deposits.getRegisterOperationHash(address(reserve), script));
        escrow.requireLocalCustody(pool_, escrow.getRequireLocalCustodyOperationHash(pool_));
        CallbackPoolRegistry callbacks = new CallbackPoolRegistry(
            address(escrow), auth, address(0), address(poolDeployer));
        escrow.setCallbackPoolRegistry(address(callbacks), escrow.getSetCallbackPoolRegistryOperationHash(address(callbacks)));
        op = pool.getBindBitcoinReserveOperationHash(address(reserve));
        _approve(op);
        pool.bindBitcoinReserve(address(reserve), op);

        recovery = BitcoinWithdrawalRecovery(escrow.recoveryFactory().deployRecovery(address(reserve),
            keccak256(script), keccak256(SUCCESSOR), keccak256(SPONSOR), COLD_HEIGHT, COLD_DEPTH, 600, GUARDIAN));
        escrow.setRecovery(address(recovery), escrow.getSetRecoveryOperationHash(address(recovery)));
    }

    function _installIncome(address auth) private {
        EconomyAsset ulr = new EconomyAsset(0);
        ConfederationDAOStake staking = new ConfederationDAOStake(address(ulr), address(validator), 200_000 ether);
        CustodianDAODecisionBoard decisions = new CustodianDAODecisionBoard(address(staking), auth);
        income = new ConfederationDAOIncome(address(pool), address(shares), auth, address(staking), address(decisions),
            address(new EconomySettlementLifecycle(address(pool))));
        bytes32 op = pool.getBindDAOIncomeOperationHash(address(income));
        _approve(op);
        pool.bindDAOIncome(address(income), op);
        IFeeManager.FeeConfig memory terms = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, 100, 0, 0);
        FederationFeeManager fees = new FederationFeeManager(auth, address(validator), address(income), terms, terms);
        op = fees.getSetFeeCollectorOperationHash(address(pool), true);
        _approve(op);
        fees.setFeeCollector(address(pool), true, op);
        op = pool.getSetFeeManagerOperationHash(address(fees));
        _approve(op);
        pool.setFeeManager(address(fees), op);
    }

    function _fund() private {
        bytes memory script = reserve.payoutScript();
        address[] memory recipients = new address[](1);
        recipients[0] = OWNER;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 60_000 * SAT;
        bytes memory firstMarker = abi.encodePacked(hex"6a38", reserve.getDepositData(0, recipients, amounts));
        amounts[0] = 40_000 * SAT;
        bytes memory secondMarker = abi.encodePacked(hex"6a38", reserve.getDepositData(1, recipients, amounts));
        bytes memory funding = abi.encodePacked(hex"0200000001", _input(bytes32(uint256(30)), 0, hex"ffffffff"),
            hex"04", _output(60_000, script), _output(40_000, script), _output(0, firstMarker),
            _output(0, secondMarker), hex"00000000");
        fundingTxid = _hash(funding);
        sponsorFunding = abi.encodePacked(hex"0200000001", _input(bytes32(uint256(77)), 0, hex"ffffffff"),
            hex"01", _output(SPONSOR_SATS, SPONSOR), hex"00000000");
        bytes32 sponsorTxid = _hash(sponsorFunding);
        bytes32 fundingBlock = _anchor(_pair(fundingTxid, sponsorTxid), COLD_DEPTH, 1);
        for (uint32 index; index < 2; ++index) {
            uint64 sats = index == 0 ? 60_000 : 40_000;
            amounts[0] = uint256(sats) * SAT;
            vm.prank(OWNER);
            reserve.lockDirectDepositQuote(fundingTxid, index, sats, 1);
            bytes32 op = reserve.getMintOperationHash(fundingTxid, index, recipients, amounts);
            reserve.mintFromProof(funding, index, fundingBlock, _path(sponsorTxid), 0, recipients, amounts, op);
        }
        // The funding fee above belongs to an ordinary mint. Flush it out of the
        // later cancellation comparison by looking at the before/after DAO balance.
        recovery.registerSponsor(sponsorFunding, 0, fundingBlock, _path(fundingTxid), 1);
        _checkSupply(BACKING);
    }

    function _openWithdrawal(bool begin) private returns (bytes memory payment) {
        _requestWithdrawal();
        bytes32[] memory outpoints = new bytes32[](2);
        outpoints[0] = _reservation(0);
        outpoints[1] = _reservation(1);
        payment = _payout(100);
        _reserveAndBind(BATCH, outpoints, payment);
        if (begin) batches.beginSigning(BATCH);
    }

    function _requestWithdrawal() private {
        vm.prank(OWNER);
        withdrawal = pool.requestWithdrawal(REQUESTED * SAT, PAYOUT, 200, PRINCIPAL - 200, type(uint64).max);
        bytes32 op = pool.getProcessWithdrawalOperationHash(withdrawal);
        _approve(op);
        pool.processWithdrawal(withdrawal);
        assertEq(pool.reservedS(), PRINCIPAL * SAT);
    }

    function _reserveAndBind(bytes32 batch, bytes32[] memory outpoints, bytes memory payment) private {
        _reserveBatch(batch, outpoints);
        _bindPayment(batch, payment);
    }

    function _reserveBatch(bytes32 batch, bytes32[] memory outpoints) private {
        uint64 deadline = uint64(vm.getBlockTimestamp() + 2 days);
        bytes32 plan = keccak256("withdrawal plan");
        batches.reserve(batch, plan, outpoints, deadline, batches.operationHash(batch, plan, outpoints, deadline));
    }

    function _bindPayment(bytes32 batch, bytes memory payment) private {
        (bytes32[] memory keys, bytes32 operation) = _paymentApproval(batch, payment);
        escrow.bindUserFunded(batch, payment, keys, operation);
    }

    function _paymentApproval(bytes32 batch, bytes memory payment)
        private view returns (bytes32[] memory keys, bytes32 operation)
    {
        keys = new bytes32[](1);
        keys[0] = escrow.requestKey(address(pool), withdrawal);
        operation = escrow.getBindUserFundedOperationHash(batch, _hash(payment), keys);
    }

    function _replacement() private returns (bytes memory payment) {
        payment = _payout(FEE);
        uint64 deadline = uint64(vm.getBlockTimestamp() + 1 days);
        bytes32 plan = keccak256("replacement plan");
        batches.reserveReplacement(BATCH, REPLACEMENT, plan, deadline,
            batches.getReplacementOperationHash(BATCH, REPLACEMENT, plan, deadline));
        escrow.bindUserFundedReplacement(BATCH, REPLACEMENT, payment,
            escrow.getBindUserFundedReplacementOperationHash(BATCH, REPLACEMENT, _hash(payment)));
        batches.beginSigning(REPLACEMENT);
    }

    function _pause() private {
        vm.prank(GUARDIAN);
        recovery.pause();
        assertTrue(recovery.paused());
    }

    function _approve(bytes32 op) private {
        vm.prank(OWNER);
        validator.registerCustodianSignature(op);
        vm.prank(OTHER);
        validator.registerCustodianSignature(op);
    }

    function _little(uint256 value, uint256 length) internal pure returns (bytes memory result) {
        result = new bytes(length);
        for (uint256 i; i < length; ++i) result[i] = bytes1(uint8(value >> (8 * i)));
    }

    function _hash(bytes memory data) internal pure returns (bytes32) {
        return sha256(abi.encodePacked(sha256(data)));
    }

    function _pair(bytes32 left, bytes32 right) private pure returns (bytes32) {
        return _hash(abi.encodePacked(left, right));
    }

    function _input(bytes32 parent, uint32 index, bytes4 sequence) private pure returns (bytes memory) {
        return abi.encodePacked(parent, _little(index, 4), hex"00", sequence);
    }

    function _output(uint64 sats, bytes memory script) private pure returns (bytes memory) {
        return abi.encodePacked(_little(sats, 8), bytes1(uint8(script.length)), script);
    }

    function _path(bytes32 other) private pure returns (bytes32[] memory path) {
        path = new bytes32[](1);
        path[0] = other;
    }

    function _reservation(uint32 index) private view returns (bytes32) {
        return _reservation(fundingTxid, index);
    }

    function _reservation(bytes32 txid, uint32 index) private pure returns (bytes32) {
        return keccak256(abi.encode(BitcoinTxParser.flipBytes32(txid), index));
    }

    function _payout(uint64 fee) private view returns (bytes memory) {
        return abi.encodePacked(hex"0200000002", _input(fundingTxid, 0, hex"fdffffff"),
            _input(fundingTxid, 1, hex"fdffffff"), hex"02", _output(PRINCIPAL - fee, PAYOUT),
            _output(BACKING - PRINCIPAL, reserve.payoutScript()), hex"00000000");
    }

    function _sweep(uint64 retained, uint64 fee, bytes memory successor, uint32 locktime, uint32 secondVout)
        private view returns (bytes memory)
    {
        return abi.encodePacked(hex"0200000003", _input(fundingTxid, 0, hex"fdffffff"),
            _input(fundingTxid, secondVout, hex"fdffffff"), _input(_hash(sponsorFunding), 0, hex"fdffffff"),
            hex"02", _output(retained, successor), _output(SPONSOR_SATS - fee, SPONSOR), _little(locktime, 4));
    }

    function _checkSupply(uint256 backing) private view {
        assertEq(deposits.totalBackingSats(), backing);
        assertEq(shares.totalSupply(), backing * SAT);
        assertEq(pool.totalPrincipal(), backing * SAT);
        assertEq(shares.balanceOf(address(escrow)), pool.reservedS());
    }

    function testRecoveryAfterCanonicalProofKeepsAllBackingAndReturnsOriginalSOnce() public {
        uint256 beforeOwner = shares.balanceOf(OWNER);
        uint256 beforeIncome = shares.balanceOf(address(income));
        bytes memory payout = _openWithdrawal(true);
        bytes memory replacement = _replacement();
        assertFalse(recovery.paused());
        bytes memory sweep = _sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 1);
        bytes32 decoy = bytes32(uint256(45));
        bytes32 sweepHash = _hash(sweep);
        bytes32 blockHash = _anchor(_pair(sweepHash, decoy), COLD_DEPTH, COLD_HEIGHT + 1);
        vm.prank(OTHER);
        recovery.recover(BATCH, sweep, blockHash, _path(decoy), 0);
        assertTrue(recovery.paused());
        assertEq(recovery.recoveredTxid(BATCH), sweepHash);
        assertEq(recovery.recoveryBlock(BATCH), blockHash);
        bytes32 successor = keccak256(abi.encode(sweepHash, uint32(0)));
        assertEq(deposits.backingSats(successor), BACKING);
        assertTrue(deposits.walletOutput(successor));
        assertEq(deposits.backingSats(keccak256(abi.encode(sweepHash, uint32(1)))), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(fundingTxid, uint32(0)))), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(fundingTxid, uint32(1)))), 0);
        assertEq(deposits.backingCount(), 1);
        _checkSupply(BACKING);

        vm.warp(vm.getBlockTimestamp() + pool.WITHDRAWAL_CANCEL_DELAY());
        vm.prank(OTHER);
        vm.expectRevert("cancel owner/delay");
        pool.cancelWithdrawal(withdrawal);
        vm.prank(OWNER);
        pool.cancelWithdrawal(withdrawal);
        assertEq(shares.balanceOf(OWNER), beforeOwner);
        assertEq(shares.balanceOf(address(income)), beforeIncome);
        assertEq(pool.pendingShares(OWNER), 0);
        assertEq(pool.reservedS(), 0);
        _checkSupply(BACKING);

        vm.prank(OWNER);
        vm.expectRevert(ConfederationPoolV2.InvalidWithdrawal.selector);
        pool.cancelWithdrawal(withdrawal);
        vm.expectRevert();
        escrow.settle(BATCH, payout, blockHash, _path(decoy), 0);
        vm.expectRevert();
        escrow.settle(REPLACEMENT, replacement, blockHash, _path(decoy), 0);
        // The exact winner can be reanchored without replaying reserve accounting.
        recovery.recover(BATCH, sweep, blockHash, _path(decoy), 0);
        assertEq(deposits.backingCount(), 1);
        assertEq(shares.balanceOf(OWNER), beforeOwner);
        _checkSupply(BACKING);
    }

    function testPayoutWinsThroughApprovedReplacementWhileRecoveryIsPaused() public {
        _assertPayoutWins(true);
    }

    function testOriginalPayoutCanStillWinAfterReplacementIsBound() public {
        _assertPayoutWins(false);
    }

    function _assertPayoutWins(bool replacementWins) private {
        uint256 beforeOwner = shares.balanceOf(OWNER);
        uint256 beforeIncome = shares.balanceOf(address(income));
        bytes memory original = _openWithdrawal(true);
        bytes memory replacement = _replacement();
        bytes memory sweep = _sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 1);
        _pause();
        bytes32 decoy = bytes32(uint256(45));
        bytes memory winner = replacementWins ? replacement : original;
        bytes32 winningBatch = replacementWins ? REPLACEMENT : BATCH;
        bytes32 blockHash = _anchor(_pair(_hash(winner), decoy), COLD_DEPTH, COLD_HEIGHT + 1);
        vm.prank(OTHER);
        escrow.settle(winningBatch, winner, blockHash, _path(decoy), 0);
        assertEq(shares.balanceOf(OWNER), beforeOwner - REQUESTED * SAT);
        assertEq(shares.balanceOf(address(income)), beforeIncome + PROTOCOL_FEE * SAT);
        assertEq(pool.pendingShares(OWNER), 0);
        assertEq(pool.reservedS(), 0);
        _checkSupply(BACKING - PRINCIPAL);
        assertEq(recovery.recoveredTxid(BATCH), bytes32(0));

        vm.warp(vm.getBlockTimestamp() + pool.WITHDRAWAL_CANCEL_DELAY());
        vm.prank(OWNER);
        vm.expectRevert(ConfederationPoolV2.InvalidWithdrawal.selector);
        pool.cancelWithdrawal(withdrawal);
        vm.expectRevert();
        escrow.settle(BATCH, original, blockHash, _path(decoy), 0);
        vm.expectRevert();
        escrow.settle(REPLACEMENT, replacement, blockHash, _path(decoy), 0);
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoveryProofInvalid.selector);
        recovery.recover(BATCH, sweep, blockHash, _path(decoy), 0);
        _checkSupply(BACKING - PRINCIPAL);
    }

    function testOwnerCannotRefundOnTimerOrInsufficientDepth() public {
        _openWithdrawal(true);
        _pause();
        vm.warp(vm.getBlockTimestamp() + pool.WITHDRAWAL_CANCEL_DELAY());
        vm.prank(OWNER);
        vm.expectRevert("signature may exist");
        pool.cancelWithdrawal(withdrawal);
        bytes memory sweep = _sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 1);
        bytes32 decoy = bytes32(uint256(45));
        bytes32 blockHash = _anchor(_pair(_hash(sweep), decoy), COLD_DEPTH - 1, COLD_HEIGHT + 1);
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoveryProofInvalid.selector);
        recovery.recover(BATCH, sweep, blockHash, _path(decoy), 0);
        assertEq(recovery.recoveredTxid(BATCH), bytes32(0));
        vm.prank(OWNER);
        vm.expectRevert("signature may exist");
        pool.cancelWithdrawal(withdrawal);
        _advance(1);
        recovery.recover(BATCH, sweep, blockHash, _path(decoy), 0);
        vm.prank(OWNER);
        pool.cancelWithdrawal(withdrawal);
        _checkSupply(BACKING);
    }

    function testRejectProofOfAnotherTransaction() public {
        bytes memory payout = _openWithdrawal(true);
        bytes memory sweep = _sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 1);
        bytes32 decoy = bytes32(uint256(45));
        bytes32 blockHash = _anchor(_pair(_hash(payout), decoy), COLD_DEPTH, COLD_HEIGHT + 1);
        assertFalse(spv.checkTxInclusion(blockHash, _hash(sweep), _path(decoy), 0));
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoveryProofInvalid.selector);
        recovery.recover(BATCH, sweep, blockHash, _path(decoy), 0);
        assertEq(recovery.recoveredTxid(BATCH), bytes32(0));
        _checkSupply(BACKING);
    }

    function testRejectRecoveryOneSatShortEvenWithValidInclusion() public {
        _rejectSweep(_sweep(BACKING - 1, FEE, SUCCESSOR, COLD_HEIGHT, 1),
            BitcoinWithdrawalRecovery.RecoveryBackingInvalid.selector);
    }

    function testRejectUnapprovedSuccessorEvenWithValidInclusion() public {
        _rejectSweep(_sweep(BACKING, FEE, reserve.payoutScript(), COLD_HEIGHT, 1),
            BitcoinWithdrawalRecovery.RecoveryBackingInvalid.selector);
    }

    function testRejectIncompleteInputSetEvenWithValidInclusion() public {
        _rejectSweep(_sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 2),
            BitcoinWithdrawalRecovery.RecoveryFamilyInvalid.selector);
    }

    function testRejectLocktimeBeforeRecoveryHeightEvenWithValidInclusion() public {
        _rejectSweep(_sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT - 1, 1),
            BitcoinWithdrawalRecovery.RecoveryHeightInvalid.selector);
    }

    function testRejectSponsorFeeAboveBoundEvenWithValidInclusion() public {
        _rejectSweep(_sweep(BACKING, 601, SUCCESSOR, COLD_HEIGHT, 1),
            BitcoinWithdrawalRecovery.RecoverySponsorInvalid.selector);
    }

    function testRecoverySponsorNamespaceCannotBeRegisteredAsDeposit() public {
        bytes32 sponsorHash = keccak256(SPONSOR);
        assertEq(deposits.feeScripts(sponsorHash), keccak256(SUCCESSOR));
        address unrelated = address(income);
        assertEq(deposits.poolScripts(unrelated), bytes32(0));
        bytes32 op = deposits.getRegisterOperationHash(unrelated, SPONSOR);
        vm.expectRevert(bytes("registered"));
        deposits.register(unrelated, SPONSOR, op);
        assertEq(deposits.scriptPools(sponsorHash), address(0));
        assertEq(deposits.feeScripts(sponsorHash), keccak256(SUCCESSOR));
    }

    function testGuardianPauseBlocksNewReservationButKeepsExistingFamily() public {
        _openWithdrawal(true);
        vm.prank(OTHER);
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoveryPauseGuardianOnly.selector);
        recovery.pause();
        _pause();
        bytes32[] memory inputs = new bytes32[](1);
        inputs[0] = bytes32(uint256(20));
        uint64 deadline = uint64(vm.getBlockTimestamp() + 2 days);
        bytes32 plan = keccak256("new reservation");
        bytes32 op = batches.operationHash(REPLACEMENT, plan, inputs, deadline);
        vm.expectRevert();
        batches.reserve(REPLACEMENT, plan, inputs, deadline, op);
        assertEq(uint256(batches.batchState(BATCH)), uint256(BitcoinBatchRegistry.State.Signing));
        _checkSupply(BACKING);
    }

    function testRotationAllowsSinglePayoutFromSuccessorWithoutMintingBackingAgain() public {
        bytes memory oldScript = reserve.payoutScript();
        (bytes memory sweep, bytes32 sweepBlock) = _recoverFamily();
        bytes32 sweepId = _hash(sweep);
        bytes32 ownedOutput = keccak256(abi.encode(sweepId, uint32(0)));
        uint256 oldWithdrawal = withdrawal;
        _rotate();
        assertTrue(recovery.paused());
        assertFalse(recovery.withdrawalsPaused());
        assertEq(recovery.activeScriptHash(), keccak256(SUCCESSOR));
        assertEq(reserve.payoutScript(), SUCCESSOR);
        assertEq(deposits.poolScripts(address(reserve)), keccak256(SUCCESSOR));
        assertEq(deposits.scriptPools(keccak256(oldScript)), address(reserve));
        assertEq(batches.walletId(), SUCCESSOR_WALLET);
        assertEq(batches.walletResult(), SUCCESSOR_WALLET);
        assertEq(deposits.backingSats(ownedOutput), BACKING);
        _checkSupply(BACKING);

        vm.prank(address(reserve));
        vm.expectRevert("outpoint consumed or change");
        deposits.consume(sweep, 0, sweepBlock, _path(DECOY), 0);
        bytes32 lateFunding = _hash(_newFunding(1_000, oldScript));
        vm.prank(OWNER);
        vm.expectRevert(ConfederationBitcoinReserve.BitcoinRecoveryPaused.selector);
        reserve.lockDirectDepositQuote(lateFunding, 0, 1_000, 1);
        _checkSupply(BACKING);

        vm.warp(vm.getBlockTimestamp() + pool.WITHDRAWAL_CANCEL_DELAY());
        vm.prank(OWNER);
        pool.cancelWithdrawal(oldWithdrawal);
        _checkSupply(BACKING);
        _requestWithdrawal();
        bytes32[] memory input = new bytes32[](1);
        input[0] = _reservation(sweepId, 0);
        _reserveBatch(SUCCESSOR_BATCH, input);
        (, bytes32 walletResult,,) = batches.batches(SUCCESSOR_BATCH);
        assertEq(walletResult, batches.walletResult());

        bytes memory badChange = _successorPayout(sweepId, oldScript);
        (bytes32[] memory keys, bytes32 operation) = _paymentApproval(SUCCESSOR_BATCH, badChange);
        vm.expectRevert();
        escrow.bindUserFunded(SUCCESSOR_BATCH, badChange, keys, operation);
        bytes memory payout = _successorPayout(sweepId, SUCCESSOR);
        _bindPayment(SUCCESSOR_BATCH, payout);
        batches.beginSigning(SUCCESSOR_BATCH);
        bytes32 payoutId = _hash(payout);
        bytes32 payoutBlock = _anchor(_pair(payoutId, DECOY), COLD_DEPTH, 1);
        escrow.settle(SUCCESSOR_BATCH, payout, payoutBlock, _path(DECOY), 0);
        assertEq(deposits.backingSats(ownedOutput), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(payoutId, uint32(1)))), BACKING - PRINCIPAL);
        assertEq(deposits.backingCount(), 1);
        _checkSupply(BACKING - PRINCIPAL);

        vm.expectRevert();
        escrow.settle(SUCCESSOR_BATCH, payout, payoutBlock, _path(DECOY), 0);
        _expectRotationRejected(SUCCESSOR_WALLET, BATCH);
        _checkSupply(BACKING - PRINCIPAL);
    }

    function testColdStartFundingQuorumLossRefundThenRotationAndSuccessorPayment() public {
        uint256 originalOwnerBalance = shares.balanceOf(OWNER);
        uint256 originalDaoBalance = shares.balanceOf(address(income));
        assertEq(originalOwnerBalance + originalDaoBalance, BACKING * SAT);
        bytes32 originalWallet = batches.walletId();
        bytes memory originalPayment = _openWithdrawal(true);
        uint256 originalWithdrawal = withdrawal;
        authorization.loseBitcoinQuorum(originalWallet);
        vm.expectRevert("wallet participant");
        batches.beginSigning(BATCH);
        vm.warp(vm.getBlockTimestamp() + pool.WITHDRAWAL_CANCEL_DELAY());
        vm.prank(OWNER);
        vm.expectRevert("signature may exist");
        pool.cancelWithdrawal(originalWithdrawal);

        bytes memory migration = _sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 1);
        bytes32 migrationTxid = _hash(migration);
        bytes32 migrationBlock = _anchor(_pair(migrationTxid, DECOY), COLD_DEPTH - 1, COLD_HEIGHT + 1);
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoveryProofInvalid.selector);
        recovery.recover(BATCH, migration, migrationBlock, _path(DECOY), 0);
        _advance(1);
        vm.prank(OTHER);
        recovery.recover(BATCH, migration, migrationBlock, _path(DECOY), 0);
        bytes32 migratedOutpoint = keccak256(abi.encode(migrationTxid, uint32(0)));
        assertEq(deposits.backingSats(migratedOutpoint), BACKING);
        assertEq(deposits.backingByScript(recovery.sourceScriptHash()), 0);
        _checkSupply(BACKING);

        vm.prank(OWNER);
        pool.cancelWithdrawal(originalWithdrawal);
        assertEq(shares.balanceOf(OWNER), originalOwnerBalance);
        assertEq(shares.balanceOf(address(income)), originalDaoBalance);
        assertEq(pool.reservedS(), 0);
        vm.prank(OWNER);
        vm.expectRevert(ConfederationPoolV2.InvalidWithdrawal.selector);
        pool.cancelWithdrawal(originalWithdrawal);
        vm.expectRevert();
        escrow.settle(BATCH, originalPayment, migrationBlock, _path(DECOY), 0);

        _rotate();
        assertEq(batches.walletId(), SUCCESSOR_WALLET);
        assertEq(reserve.payoutScript(), SUCCESSOR);
        _checkSupply(BACKING);
        _requestWithdrawal();
        bytes32[] memory inputs = new bytes32[](1);
        inputs[0] = _reservation(migrationTxid, 0);
        bytes memory payout = _successorPayout(migrationTxid, SUCCESSOR);
        _reserveAndBind(SUCCESSOR_BATCH, inputs, payout);
        batches.beginSigning(SUCCESSOR_BATCH);
        bytes32 payoutTxid = _hash(payout);
        bytes32 payoutBlock = _anchor(_pair(payoutTxid, DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        escrow.settle(SUCCESSOR_BATCH, payout, payoutBlock, _path(DECOY), 0);
        assertEq(deposits.backingSats(migratedOutpoint), 0);
        assertEq(deposits.backingSats(keccak256(abi.encode(payoutTxid, uint32(1)))), BACKING - PRINCIPAL);
        assertEq(pool.reservedS(), 0);
        _checkSupply(BACKING - PRINCIPAL);
    }

    function testRotationRequiresFrostAndCompletedDkgWithPinnedKeyPurposeAndRoster() public {
        _recoverFamily();
        for (uint256 id = 3; id <= 6; ++id) {
            _expectRotationRejected(bytes32(id), BATCH);
        }
        bytes32 op = recovery.getRotateOperationHash(SUCCESSOR_WALLET, BATCH);
        vm.expectRevert();
        recovery.rotate(SUCCESSOR_WALLET, BATCH, bytes32(uint256(77)));
        authorization.denyOperation(op);
        vm.prank(GUARDIAN);
        vm.expectRevert(RecoveryAuthorizationFixture.MockOperationNotApproved.selector);
        recovery.rotate(SUCCESSOR_WALLET, BATCH, op);
        assertTrue(recovery.withdrawalsPaused());
        assertEq(batches.walletId(), bytes32(uint256(1)));
        _checkSupply(BACKING);
    }

    function testRotationRequiresTheCanonicalRecoveryRoot() public {
        _openWithdrawal(true);
        _expectRotationRejected(SUCCESSOR_WALLET, BATCH);
        (, bytes32 blockHash) = _confirmFamilySweep();
        _expectRotationRejected(SUCCESSOR_WALLET, REPLACEMENT);
        _removeAnchor(blockHash);
        assertFalse(recovery.canRefund(BATCH));
        _expectRotationRejected(SUCCESSOR_WALLET, BATCH);
        assertTrue(recovery.withdrawalsPaused());
        _checkSupply(BACKING);
    }

    function testIdleSweepCannotTakeInputsFromASignedWithdrawalFamily() public {
        _openWithdrawal(true);
        _pause();
        bytes memory sweep = _sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 1);
        bytes32 txid = _hash(sweep);
        bytes32 blockHash = _anchor(_pair(txid, DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoveryFamilyInvalid.selector);
        recovery.sweep(sweep, blockHash, _path(DECOY), 0);
        assertEq(recovery.recoveredTxid(BATCH), bytes32(0));
        assertEq(deposits.backingCount(), 2);

        recovery.recover(BATCH, sweep, blockHash, _path(DECOY), 0);
        assertEq(recovery.recoveredTxid(BATCH), txid);
        assertEq(deposits.backingCount(), 1);
        _checkSupply(BACKING);
    }

    function testIdleSweepRequiresTheOldEpochToBePaused() public {
        bytes32 extraTxid = _fundIdleSource();
        bytes memory raw = abi.encodePacked(hex"0200000002", _input(extraTxid, 0, hex"fdffffff"),
            _input(_hash(sponsorFunding), 0, hex"fdffffff"), hex"02", _output(IDLE_BACKING, SUCCESSOR),
            _output(SPONSOR_SATS - FEE, SPONSOR), _little(COLD_HEIGHT, 4));
        bytes32 txid = _hash(raw);
        bytes32 blockHash = _anchor(_pair(txid, DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        assertFalse(recovery.paused());
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoverySweepRequiresPause.selector);
        recovery.sweep(raw, blockHash, _path(DECOY), 0);
        _checkSupply(BACKING + IDLE_BACKING);

        _pause();
        recovery.sweep(raw, blockHash, _path(DECOY), 0);
        assertEq(deposits.backingByScript(keccak256(SUCCESSOR)), IDLE_BACKING);
        _checkSupply(BACKING + IDLE_BACKING);
    }

    function testRotationRequiresProofThatIdleSourceBackingAlsoMoved() public {
        bytes32 extraTxid = _fundIdleSource();
        (bytes memory firstSweep, bytes32 firstBlock) = _recoverFamily();
        bytes32 firstId = _hash(firstSweep);
        bytes32 oldHash = keccak256(reserve.payoutScript());
        assertEq(deposits.backingByScript(oldHash), IDLE_BACKING);
        _expectRotationRejected(SUCCESSOR_WALLET, BATCH);

        recovery.registerSponsor(firstSweep, 1, firstBlock, _path(DECOY), 0);
        bytes memory idleSweep = _idleSweep(extraTxid, firstId);
        bytes32 idleId = _hash(idleSweep);
        bytes32 idleBlock = _anchor(_pair(idleId, DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        recovery.sweep(idleSweep, idleBlock, _path(DECOY), 0);
        bytes32 extraKey = keccak256(abi.encode(extraTxid, uint32(0)));
        bytes32 idleKey = keccak256(abi.encode(idleId, uint32(0)));
        assertEq(deposits.backingSats(extraKey), 0);
        assertEq(deposits.backingSats(idleKey), IDLE_BACKING);
        assertEq(deposits.backingByScript(oldHash), 0);
        assertEq(deposits.backingByScript(keccak256(SUCCESSOR)), BACKING + IDLE_BACKING);
        _rotate();
        assertFalse(recovery.withdrawalsPaused());

        _removeAnchor(idleBlock);
        assertFalse(recovery.withdrawalsPaused());
        recovery.checkMigration(firstId);
        vm.expectRevert(BitcoinWithdrawalRecovery.RecoveryProofInvalid.selector);
        recovery.checkMigration(idleId);
        bytes32 nextBlock = _anchor(_pair(idleId, DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        recovery.sweep(idleSweep, nextBlock, _path(DECOY), 0);
        recovery.checkMigration(idleId);
        assertEq(deposits.backingCount(), 2);
        _checkSupply(BACKING + IDLE_BACKING);
    }

    function testLateCommittedDepositPausesNewWithdrawalsUntilItsBackingMoves() public {
        address[] memory recipients = new address[](1);
        recipients[0] = OWNER;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = IDLE_BACKING * SAT;
        bytes memory marker = abi.encodePacked(hex"6a38", reserve.getDepositData(0, recipients, amounts));
        bytes memory raw = abi.encodePacked(hex"0200000001", _input(bytes32(uint256(99)), 1, hex"ffffffff"),
            hex"02", _output(IDLE_BACKING, reserve.payoutScript()), _output(0, marker), hex"00000000");
        bytes32 depositId = _hash(raw);
        vm.prank(OWNER);
        reserve.lockDirectDepositQuote(depositId, 0, IDLE_BACKING, 1);

        (bytes memory firstSweep, bytes32 firstBlock) = _recoverFamily();
        _rotate();
        assertFalse(recovery.withdrawalsPaused());
        _checkSupply(BACKING);

        bytes32 inboundBlock = _anchor(_pair(depositId, DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        bytes32 op = reserve.getMintOperationHash(depositId, 0, recipients, amounts);
        reserve.mintFromProof(raw, 0, inboundBlock, _path(DECOY), 0, recipients, amounts, op);
        assertTrue(recovery.withdrawalsPaused());
        assertEq(deposits.backingByScript(recovery.sourceScriptHash()), IDLE_BACKING);
        _checkSupply(BACKING + IDLE_BACKING);

        recovery.registerSponsor(firstSweep, 1, firstBlock, _path(DECOY), 0);
        bytes memory idleSweep = _idleSweep(depositId, _hash(firstSweep));
        bytes32 idleBlock = _anchor(_pair(_hash(idleSweep), DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        recovery.sweep(idleSweep, idleBlock, _path(DECOY), 0);
        assertFalse(recovery.withdrawalsPaused());
        assertEq(deposits.backingByScript(recovery.sourceScriptHash()), 0);
        assertEq(deposits.backingByScript(keccak256(SUCCESSOR)), BACKING + IDLE_BACKING);
        assertEq(deposits.backingCount(), 2);
        _checkSupply(BACKING + IDLE_BACKING);
    }

    function testRotationPausesOnOrphanedAnchorAndReanchorsWithoutCreditingAgain() public {
        (bytes memory sweep, bytes32 oldBlock) = _recoverFamily();
        _rotate();
        _removeAnchor(oldBlock);
        assertTrue(recovery.withdrawalsPaused());
        assertFalse(recovery.canRefund(BATCH));
        bytes32[] memory inputs = new bytes32[](1);
        inputs[0] = _reservation(_hash(sweep), 0);
        uint64 deadline = uint64(vm.getBlockTimestamp() + 2 days);
        bytes32 plan = keccak256("resumption after reorg");
        bytes32 operation = batches.operationHash(SUCCESSOR_BATCH, plan, inputs, deadline);
        vm.expectRevert(BitcoinBatchRegistry.RecoveryEpochPaused.selector);
        batches.reserve(SUCCESSOR_BATCH, plan, inputs, deadline, operation);

        bytes32 nextBlock = _anchor(_pair(_hash(sweep), DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        recovery.recover(BATCH, sweep, nextBlock, _path(DECOY), 0);
        assertFalse(recovery.withdrawalsPaused());
        assertTrue(recovery.canRefund(BATCH));
        assertEq(deposits.backingCount(), 1);
        _checkSupply(BACKING);
    }

    function _rotate() private {
        recovery.rotate(SUCCESSOR_WALLET, BATCH, recovery.getRotateOperationHash(SUCCESSOR_WALLET, BATCH));
    }

    function _expectRotationRejected(bytes32 wallet, bytes32 root) private {
        bytes32 operation = recovery.getRotateOperationHash(wallet, root);
        vm.expectRevert();
        recovery.rotate(wallet, root, operation);
    }

    function _recoverFamily() private returns (bytes memory sweep, bytes32 blockHash) {
        _openWithdrawal(true);
        return _confirmFamilySweep();
    }

    function _confirmFamilySweep() private returns (bytes memory sweep, bytes32 blockHash) {
        sweep = _sweep(BACKING, FEE, SUCCESSOR, COLD_HEIGHT, 1);
        blockHash = _anchor(_pair(_hash(sweep), DECOY), COLD_DEPTH, COLD_HEIGHT + 1);
        recovery.recover(BATCH, sweep, blockHash, _path(DECOY), 0);
    }

    function _newFunding(uint64 value, bytes memory script) private pure returns (bytes memory) {
        return abi.encodePacked(hex"0200000001", _input(bytes32(uint256(99)), 1, hex"ffffffff"),
            hex"01", _output(value, script), hex"00000000");
    }

    function _idleSweep(bytes32 source, bytes32 sponsor) private pure returns (bytes memory) {
        return abi.encodePacked(hex"0200000002", _input(source, 0, hex"fdffffff"),
            _input(sponsor, 1, hex"fdffffff"), hex"02", _output(IDLE_BACKING, SUCCESSOR),
            _output(SPONSOR_SATS - 2 * FEE, SPONSOR), _little(COLD_HEIGHT, 4));
    }

    function _fundIdleSource() private returns (bytes32 txid) {
        bytes memory raw = _newFunding(IDLE_BACKING, reserve.payoutScript());
        txid = _hash(raw);
        bytes32 blockHash = _anchor(_pair(txid, DECOY), COLD_DEPTH, 1);
        vm.prank(address(reserve));
        deposits.consume(raw, 0, blockHash, _path(DECOY), 0);
        vm.prank(address(reserve));
        pool.mintFromReserve(OWNER, IDLE_BACKING * SAT, true);
    }

    function _successorPayout(bytes32 previous, bytes memory change) private pure returns (bytes memory) {
        return abi.encodePacked(hex"0200000001", _input(previous, 0, hex"fdffffff"), hex"02",
            _output(PRINCIPAL - FEE, PAYOUT), _output(BACKING - PRINCIPAL, change), hex"00000000");
    }

    function _rejectSweep(bytes memory sweep, bytes4 errorSelector) private {
        _openWithdrawal(true);
        bytes32 decoy = bytes32(uint256(45));
        bytes32 blockHash = _anchor(_pair(_hash(sweep), decoy), COLD_DEPTH, COLD_HEIGHT + 1);
        assertTrue(spv.checkTxInclusion(blockHash, _hash(sweep), _path(decoy), 0));
        vm.expectRevert(errorSelector);
        recovery.recover(BATCH, sweep, blockHash, _path(decoy), 0);
        assertEq(pool.reservedS(), PRINCIPAL * SAT);
        assertEq(recovery.recoveredTxid(BATCH), bytes32(0));
        _checkSupply(BACKING);
    }
}
