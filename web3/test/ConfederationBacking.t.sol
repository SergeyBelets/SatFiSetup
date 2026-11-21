// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {CustodyBatchFixture, CustodySPVFixture} from "./BitcoinCustodyTransfer.t.sol";
import {EconomyAsset} from "./ConfederationDAOIncome.t.sol";
import {BitcoinWithdrawalEscrowV2} from "../contracts/src/federation/BitcoinWithdrawalEscrowV2.sol";
import {BitcoinWithdrawalEscrow} from "../contracts/src/federation/BitcoinWithdrawalEscrow.sol";
import {BitcoinCustodyTransferV2} from "../contracts/src/federation/BitcoinCustodyTransferV2.sol";
import {ConfederationBitcoinReserve} from "../contracts/src/federation/ConfederationBitcoinReserve.sol";
import {BitcoinDepositRegistry} from "../contracts/src/federation/BitcoinDepositRegistry.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {MiningPoolV1} from "../contracts/src/federation/MiningPoolV1.sol";
import {CallbackPoolRegistry} from "../contracts/src/federation/CallbackPoolRegistry.sol";
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
import {FederationNFTModifier} from "../contracts/src/tokens/FederationNFTModifier.sol";
import {BitcoinTxParser} from "../contracts/src/core/BitcoinTxParser.sol";

/// @dev Real token, pool, escrow and deposit accounting. DKG/signatures and SPV are explicit fixtures.
contract ConfederationBackingTest is Test {
    uint256 private constant SAT = 1e10;
    address private constant ALICE = address(0xA);
    address private constant BOB = address(0xB);
    address private constant PAYER = address(0xD);
    bytes private constant PAYOUT = hex"00141111111111111111111111111111111111111111";
    bytes32 private constant BATCH = bytes32(uint256(12));
    CustodyBatchFixture private batches;
    CustodySPVFixture private spv;
    BitcoinWithdrawalEscrowV2 private escrow;
    BitcoinDepositRegistry private deposits;
    ConfederationBitcoinReserve private mining;
    ConfederationPoolV2 private pool;
    ConfederationShareV2 private shares;
    CustodianValidator private validator;
    ConfederationDAOIncome private income;
    FederationFeeManager private fees;
    FederationNFTModifier private selectedPass;
    ConfederationDAOStake private staking;
    bytes private funding;
    CustodyBatchFixture private sourceBatches;
    CustodySPVFixture private sourceSpv;
    BitcoinWithdrawalEscrow private sourceEscrow;
    BitcoinDepositRegistry private sourceDeposits;
    MiningPoolV1 private sourceMining;
    BitcoinCustodyTransferV2 private gateway;

    function setUp() public {
        vm.warp(100 days);
        batches = new CustodyBatchFixture(bytes32(uint256(22)));
        spv = new CustodySPVFixture();
        escrow = new BitcoinWithdrawalEscrowV2(address(batches), address(spv), 6, 294, 330, 100);
        batches.bindEscrow(address(escrow));
        deposits = new BitcoinDepositRegistry(address(spv), batches.authorization(), 6, address(escrow));
        escrow.setDepositRegistry(address(deposits), escrow.getSetDepositRegistryOperationHash(address(deposits)));


        address[] memory custodians = new address[](2);
        custodians[0] = ALICE;
        custodians[1] = BOB;
        validator = new CustodianValidator(custodians, 2, batches.authorization());
        ConfederationShareV2Deployer shareDeployer = new ConfederationShareV2Deployer();
        ConfederationPoolV2Deployer poolDeployer = new ConfederationPoolV2Deployer(address(shareDeployer));
        shareDeployer.setAuthorizedDeployer(address(poolDeployer), true);
        poolDeployer.setAuthorizedDeployer(address(this), true);
        (address pool_, address share_) = poolDeployer.deployMultiPool(
            "local", batches.authorization(), address(validator), "Satoshi Bitcoin", "sBTC");
        pool = ConfederationPoolV2(pool_);
        shares = ConfederationShareV2(share_);
        EconomyAsset ulr = new EconomyAsset(0);
        staking = new ConfederationDAOStake(address(ulr), address(validator), 200_000 ether);
        CustodianDAODecisionBoard decisions = new CustodianDAODecisionBoard(address(staking), batches.authorization());
        income = new ConfederationDAOIncome(pool_, share_, batches.authorization(), address(staking), address(decisions));
        bytes32 op = pool.getBindDAOIncomeOperationHash(address(income));
        _approve(op);
        pool.bindDAOIncome(address(income), op);
        op = pool.getSetWithdrawalEscrowOperationHash(address(escrow));
        _approve(op);
        pool.setWithdrawalEscrow(address(escrow), op);
        mining = new ConfederationBitcoinReserve("local", address(deposits), address(escrow), pool_);
        deposits.register(address(mining), mining.payoutScript(),
            deposits.getRegisterOperationHash(address(mining), mining.payoutScript()));
        escrow.requireLocalCustody(pool_, escrow.getRequireLocalCustodyOperationHash(pool_));
        CallbackPoolRegistry callbacks = new CallbackPoolRegistry(
            address(escrow), batches.authorization(), address(0), address(poolDeployer));
        escrow.setCallbackPoolRegistry(address(callbacks), escrow.getSetCallbackPoolRegistryOperationHash(address(callbacks)));
        op = pool.getBindBitcoinReserveOperationHash(address(mining));
        _approve(op);
        pool.bindBitcoinReserve(address(mining), op);
        IFeeManager.FeeConfig memory terms = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, 100, 0, 0);
        fees = new FederationFeeManager(batches.authorization(), address(validator), address(income), terms, terms);
        op = fees.getSetFeeCollectorOperationHash(address(pool), true);
        _approve(op);
        fees.setFeeCollector(address(pool), true, op);
        op = pool.getSetFeeManagerOperationHash(address(fees));
        _approve(op);
        pool.setFeeManager(address(fees), op);
        _configureSource();

        ulr.mint(ALICE, 200_000 ether);
        vm.startPrank(ALICE);
        ulr.approve(address(staking), type(uint256).max);
        staking.stake(200_000 ether);
        vm.stopPrank();
        funding = abi.encodePacked(hex"0200000001", bytes32(0), hex"ffffffff00ffffffff01",
            _little(100_000), hex"22", mining.payoutScript(), hex"00000000");
        address[] memory recipients = new address[](1);
        recipients[0] = ALICE;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 100_000 * SAT;
        mining.mintFromProof(funding, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts,
            mining.getMintOperationHash(_txid(funding), 0, recipients, amounts));

    }

    function _configureSource() private {
        sourceBatches = new CustodyBatchFixture(bytes32(uint256(11)));
        sourceSpv = new CustodySPVFixture();
        sourceEscrow = new BitcoinWithdrawalEscrow(address(sourceBatches), address(sourceSpv), 6, 294, 330);
        sourceBatches.bindEscrow(address(sourceEscrow));
        sourceDeposits = new BitcoinDepositRegistry(address(sourceSpv), sourceBatches.authorization(), 6, address(sourceEscrow));
        sourceEscrow.setDepositRegistry(address(sourceDeposits),
            sourceEscrow.getSetDepositRegistryOperationHash(address(sourceDeposits)));
        sourceMining = new MiningPoolV1("source", address(sourceDeposits), address(sourceEscrow), "Source MP", "SMP");
        sourceDeposits.register(address(sourceMining), sourceMining.payoutScript(),
            sourceDeposits.getRegisterOperationHash(address(sourceMining), sourceMining.payoutScript()));
        BitcoinCustodyTransferV2.Source[] memory sources = new BitcoinCustodyTransferV2.Source[](1);
        sources[0] = BitcoinCustodyTransferV2.Source(address(sourceEscrow), address(sourceMining.mpToken()),
            address(sourceEscrow).codehash, address(sourceMining.mpToken()).codehash, address(sourceMining).codehash,
            address(sourceDeposits).codehash, address(sourceBatches).codehash);
        gateway = new BitcoinCustodyTransferV2(address(mining), address(pool), sources);
        mining.setCustodyTransfer(address(gateway), mining.getSetCustodyTransferOperationHash(address(gateway)));
    }

    function _approve(bytes32 op) private {
        vm.prank(ALICE);
        validator.registerCustodianSignature(op);
        vm.prank(BOB);
        validator.registerCustodianSignature(op);
    }

    function _setUnwrapFee(uint256 bps) private {
        IFeeManager.FeeConfig memory terms = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, bps, 0, 0);
        bytes32 op = fees.getSetFeeConfigOperationHash(false, terms);
        _approve(op);
        fees.setFeeConfig(false, terms, op);
    }

    function _little(uint64 value) private pure returns (bytes8 encoded) {
        for (uint256 i; i < 8; ++i) encoded |= bytes8(uint64(uint8(value >> (8 * i))) << (56 - 8 * i));
    }

    function _txid(bytes memory raw) private pure returns (bytes32) {
        return sha256(abi.encodePacked(sha256(raw)));
    }

    function _invariant() private view {
        assertEq(shares.totalSupply(), deposits.totalBackingSats() * SAT);
        assertEq(shares.balanceOf(address(escrow)), pool.reservedS());
        assertEq(shares.totalSupply(), pool.getTotalValueLocked());
    }

    function testTransferredSCanPayBTCAndFeesAndGasComeOnlyFromRealIncome() public {
        _invariant();
        assertEq(shares.balanceOf(address(income)), 1_000 * SAT);
        assertEq(shares.balanceOf(ALICE), 99_000 * SAT);
        vm.prank(ALICE);
        shares.transfer(BOB, 40_000 * SAT);
        assertEq(sourceMining.mpToken().balanceOf(BOB), 0);
        _setUnwrapFee(200);
        vm.startPrank(BOB);
        uint256 id = pool.requestWithdrawal(40_000 * SAT, PAYOUT, 100);
        vm.stopPrank();
        assertEq(pool.getWithdrawal(id).principalAmount, 39_200 * SAT);
        _approve(pool.getProcessWithdrawalOperationHash(id));
        pool.processWithdrawal(id);
        assertEq(shares.balanceOf(address(income)), 1_000 * SAT);
        _invariant();

        bytes memory raw = abi.encodePacked(hex"0200000001", _txid(funding),
            hex"0000000000fdffffff02", _little(39_100), hex"16", PAYOUT,
            _little(60_800), hex"22", mining.payoutScript(), hex"00000000");
        bytes32 input = keccak256(abi.encode(BitcoinTxParser.flipBytes32(_txid(funding)), uint32(0)));
        batches.configure(input, BitcoinBatchRegistry.State.Reserved);
        bytes32[] memory requests = new bytes32[](1);
        requests[0] = escrow.requestKey(address(pool), id);
        escrow.bindUserFunded(BATCH, raw, requests,
            escrow.getBindUserFundedOperationHash(BATCH, _txid(raw), requests));
        assertEq(escrow.minerFee(BATCH), 100);
        batches.configure(input, BitcoinBatchRegistry.State.Signing);
        spv.setDepth(5);
        vm.expectRevert("SPV proof");
        escrow.settle(BATCH, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(shares.balanceOf(address(income)), 1_000 * SAT);
        spv.setDepth(6);
        escrow.settle(BATCH, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(deposits.totalBackingSats(), 60_800);
        assertEq(shares.balanceOf(address(income)), 1_800 * SAT);
        _invariant();
        assertEq(39_100 + escrow.minerFee(BATCH) + deposits.totalBackingSats(), 100_000);
        vm.expectRevert("state");
        escrow.settle(BATCH, raw, bytes32(uint256(1)), new bytes32[](0), 0);

        vm.warp(101 days);
        ConfederationDAOIncome.DayReport memory report;
        report.day = 100;
        report.evidenceHash = keccak256("verified receipts");
        report.tokens = income.rewardTokens();
        report.income = new uint256[4][](1);
        report.income[0][0] = 1_000 * SAT;
        report.income[0][1] = 800 * SAT;
        report.unitsPerEth = new uint256[](1);
        report.unitsPerEth[0] = 1_000_000 * SAT;
        report.expenses = new ConfederationDAOIncome.Expense[](1);
        report.expenses[0] = ConfederationDAOIncome.Expense(PAYER, keccak256("gas receipt"), 30_000 * 20 gwei);
        bytes32 op = income.getSettlementOperationHash(report);
        _approve(op);
        income.openDay(report, op);
        income.processDay(1);
        assertEq(income.reimbursement(PAYER, address(shares)), 600 * SAT);
        assertEq(income.dayReward(100, address(shares)), 1_200 * SAT);
        vm.prank(PAYER);
        income.claimReimbursement(address(shares));
        uint256[] memory days_ = new uint256[](1);
        days_[0] = 100;
        vm.prank(ALICE);
        income.claim(address(shares), days_);
        assertEq(shares.balanceOf(address(income)), 0);
        assertEq(shares.balanceOf(PAYER), 600 * SAT);
        assertEq(shares.totalSupply(), 60_800 * SAT);
        _invariant();
    }

    function testReserveIssuesOnlySAndRejectsUnprovenIssuance() public {
        (bool hasMP,) = address(mining).staticcall(abi.encodeWithSignature("mpToken()"));
        assertFalse(hasMP);
        vm.prank(BOB);
        vm.expectRevert("Bitcoin reserve only");
        pool.mintFromReserve(BOB, 1e10, true);
        _invariant();
    }

    function testFuzzSReserveStaysExactThroughCancelAndSettlement(uint256 sats, uint256 feeBps, uint256 fraction) public {
        uint256 amount = bound(sats, 1_000, 90_000) * SAT + bound(fraction, 0, SAT - 1);
        _setUnwrapFee(bound(feeBps, 0, 500));
        vm.prank(ALICE);
        shares.transfer(BOB, amount);
        vm.prank(BOB);
        uint256 id = pool.requestWithdrawal(amount, PAYOUT, 100);
        _invariant();
        vm.warp(block.timestamp + pool.WITHDRAWAL_CANCEL_DELAY());
        vm.prank(BOB);
        pool.cancelWithdrawal(id);
        assertEq(shares.balanceOf(BOB), amount);
        _invariant();
        vm.prank(BOB);
        id = pool.requestWithdrawal(amount, PAYOUT, 100);
        uint256 principal = pool.getWithdrawal(id).principalAmount;
        _approve(pool.getProcessWithdrawalOperationHash(id));
        pool.processWithdrawal(id);
        bytes memory raw = abi.encodePacked(hex"0200000001", _txid(funding), hex"0000000000fdffffff02",
            _little(uint64(principal / SAT - 100)), hex"16", PAYOUT,
            _little(uint64(100_000 - principal / SAT)), hex"22", mining.payoutScript(), hex"00000000");
        bytes32 input = keccak256(abi.encode(BitcoinTxParser.flipBytes32(_txid(funding)), uint32(0)));
        batches.configure(input, BitcoinBatchRegistry.State.Reserved);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = escrow.requestKey(address(pool), id);
        escrow.bindUserFunded(BATCH, raw, keys, escrow.getBindUserFundedOperationHash(BATCH, _txid(raw), keys));
        batches.configure(input, BitcoinBatchRegistry.State.Signing);
        escrow.settle(BATCH, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(shares.balanceOf(address(income)), 1_000 * SAT + amount - principal);
        _invariant();
    }

    function testHandoverMintsSAfterActualBTCMoveAndNeedsOnlyDestinationForWithdrawal() public {
        selectedPass = _selectFirstWrapPass(BOB);
        _exerciseHandover();
        (, uint256 remaining,) = selectedPass.getDiscount(0);
        assertEq(remaining, 100_000 * SAT);
    }

    function testFirstNFTDiscountAndTransferOfSelectedPass() public {
        FederationNFTModifier pass = _selectFirstWrapPass(ALICE);
        // The trusted reserve callback follows proof verification; give the
        // pool the backing that the registry would report after those credits.
        vm.mockCall(address(deposits), abi.encodeCall(deposits.totalBackingSats, ()), abi.encode(uint64(120_000)));
        vm.prank(address(mining));
        assertEq(pool.mintFromReserve(ALICE, 10_000 * SAT, true), 9_950 * SAT);
        vm.prank(ALICE);
        pass.transferFrom(ALICE, BOB, 0);
        vm.prank(address(mining));
        assertEq(pool.mintFromReserve(ALICE, 10_000 * SAT, true), 9_900 * SAT);
        (, uint256 remaining,) = pass.getDiscount(0);
        assertEq(remaining, 90_000 * SAT);
        vm.prank(ALICE);
        pool.clearWrapPass();
        assertFalse(pool.wrapPassSelected(ALICE));
        assertEq(shares.balanceOf(address(income)), 1_150 * SAT);
        _invariant();
    }

    function _selectFirstWrapPass(address owner) private returns (FederationNFTModifier pass) {
        pass = new FederationNFTModifier("Test wrap pass", "PASS", address(this));
        pass.grantRole(pass.USAGE_ROLE(), address(fees));
        assertEq(pass.mint(owner, block.timestamp + 1 days, 100_000 * SAT, 5_000), 0);
        bytes32 op = fees.getSetNFTModifierOperationHash(address(pass));
        _approve(op);
        fees.setNFTModifier(address(pass), op);
        vm.prank(PAYER);
        vm.expectRevert("pass owner");
        pool.setWrapPass(0);
        vm.prank(owner);
        pool.setWrapPass(0);
        assertTrue(pool.wrapPassSelected(owner));
        assertEq(pool.wrapPass(owner), 0);
    }

    function _exerciseHandover() private {
        bytes memory sourceFunding = abi.encodePacked(hex"0200000001", bytes32(0), hex"ffffffff00ffffffff01",
            _little(10_000), hex"22", sourceMining.payoutScript(), hex"00000000");
        address[] memory recipients = new address[](1);
        recipients[0] = ALICE;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = 10_000 * SAT;
        sourceMining.mintFromProof(sourceFunding, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts,
            sourceMining.getMintOperationHash(_txid(sourceFunding), 0, recipients, amounts));
        vm.startPrank(ALICE);
        address sourceToken = address(sourceMining.mpToken());
        (bool accepted,) = address(pool).call(abi.encodeWithSignature("deposit(address,uint256)", sourceToken, 10_000 * SAT));
        assertFalse(accepted);
        sourceMining.mpToken().approve(address(gateway), type(uint256).max);
        uint256 transfer = gateway.request(0, 10_000 * SAT, 9_900 * SAT, BOB);
        vm.stopPrank();
        assertEq(shares.balanceOf(BOB), 0);
        assertEq(sourceMining.mpToken().balanceOf(address(sourceEscrow)), 10_000 * SAT);
        assertEq(sourceMining.mpToken().totalSupply(), 10_000 * SAT);
        assertEq(sourceDeposits.totalBackingSats(), 10_000);
        assertEq(deposits.totalBackingSats(), 100_000);

        // Origin fees come from an unminted source treasury input. The entire
        // burned MP principal moves to the new wallet and is credited once.
        bytes memory operatingFunds = abi.encodePacked(hex"0200000001", bytes32(uint256(42)),
            hex"0000000000ffffffff01", _little(4_000), hex"22", sourceMining.payoutScript(), hex"00000000");
        bytes memory paid = abi.encodePacked(hex"0200000002", _txid(sourceFunding), hex"0000000000fdffffff",
            _txid(operatingFunds), hex"0000000000fdffffff02", _little(10_000), hex"22", mining.payoutScript(),
            _little(3_500), hex"22", sourceMining.payoutScript(), hex"00000000");
        uint256[] memory ids = new uint256[](1);
        ids[0] = transfer;
        vm.expectRevert("source payment not settled");
        gateway.finalize(ids, BATCH, paid, 0, bytes32(uint256(1)), new bytes32[](0), 0);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = sourceEscrow.requestKey(address(gateway), transfer);
        bytes32[] memory sourceInputs = new bytes32[](2);
        sourceInputs[0] = keccak256(abi.encode(BitcoinTxParser.flipBytes32(_txid(sourceFunding)), uint32(0)));
        sourceInputs[1] = keccak256(abi.encode(BitcoinTxParser.flipBytes32(_txid(operatingFunds)), uint32(0)));
        sourceBatches.configure(sourceInputs, BitcoinBatchRegistry.State.Reserved);
        sourceEscrow.bind(BATCH, _txid(paid), keys, sourceEscrow.getBindOperationHash(BATCH, _txid(paid), keys));
        sourceBatches.configure(sourceInputs, BitcoinBatchRegistry.State.Signing);
        sourceEscrow.settle(BATCH, paid, bytes32(uint256(1)), new bytes32[](0), 0);
        uint256 transferred = sourceDeposits.getValue(keccak256(abi.encode(_txid(paid), uint32(0)))).sats;
        uint256 sourceChange = sourceDeposits.getValue(keccak256(abi.encode(_txid(paid), uint32(1)))).sats;
        assertEq(transferred, 10_000);
        assertEq(10_000 + 4_000 - transferred - sourceChange, 500);
        assertEq(sourceMining.mpToken().totalSupply(), 0);
        assertEq(sourceDeposits.totalBackingSats(), 0);
        assertEq(deposits.totalBackingSats(), 100_000);
        bytes32 mintOp = mining.getMintOperationHash(_txid(paid), 0, recipients, amounts);
        vm.expectRevert("use custody transfer");
        mining.mintFromProof(paid, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts, mintOp);
        spv.setDepth(5);
        vm.expectRevert("transfer proof");
        gateway.finalize(ids, BATCH, paid, 0, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(shares.balanceOf(BOB), 0);
        spv.setDepth(6);
        gateway.finalize(ids, BATCH, paid, 0, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(sourceMining.mpToken().totalSupply(), 0);
        assertEq(sourceDeposits.totalBackingSats(), 0);
        assertEq(deposits.totalBackingSats(), 110_000);
        assertEq(shares.balanceOf(BOB), 9_900 * SAT);
        assertEq(shares.balanceOf(address(income)), 1_100 * SAT);
        vm.expectRevert("transfer state");
        gateway.finalize(ids, BATCH, paid, 0, bytes32(uint256(1)), new bytes32[](0), 0);
        _invariant();

        sourceSpv.setCanonical(false);
        vm.startPrank(BOB);
        uint256 id = pool.requestWithdrawal(5_000 * SAT, PAYOUT, 100);
        vm.stopPrank();
        _approve(pool.getProcessWithdrawalOperationHash(id));
        pool.processWithdrawal(id);
        bytes memory withdrawal = abi.encodePacked(hex"0200000001", _txid(paid), hex"0000000000fdffffff02",
            _little(4_850), hex"16", PAYOUT, _little(5_050), hex"22", mining.payoutScript(), hex"00000000");
        bytes32 input = keccak256(abi.encode(BitcoinTxParser.flipBytes32(_txid(paid)), uint32(0)));
        batches.configure(input, BitcoinBatchRegistry.State.Reserved);
        keys[0] = escrow.requestKey(address(pool), id);
        escrow.bindUserFunded(BATCH, withdrawal, keys,
            escrow.getBindUserFundedOperationHash(BATCH, _txid(withdrawal), keys));
        batches.configure(input, BitcoinBatchRegistry.State.Signing);
        escrow.settle(BATCH, withdrawal, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(escrow.minerFee(BATCH), 100);
        uint256 received = deposits.getValue(keccak256(abi.encode(_txid(withdrawal), uint32(0)))).sats;
        uint256 reserveChange = deposits.getValue(keccak256(abi.encode(_txid(withdrawal), uint32(1)))).sats;
        assertEq(received, 4_850);
        assertEq(100_000 + 10_000 + 4_000,
            deposits.totalBackingSats() + sourceChange + received + 500 + escrow.minerFee(BATCH));
        assertEq(deposits.totalBackingSats(), 100_000 + reserveChange);
        vm.expectRevert("state");
        escrow.settle(BATCH, withdrawal, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(deposits.totalBackingSats(), 105_050);
        assertEq(shares.balanceOf(address(income)), 1_150 * SAT);
        assertEq(income.dayIncome(100, address(shares), ConfederationDAOIncome.Stream.Wrap), 1_100 * SAT);
        assertEq(income.dayIncome(100, address(shares), ConfederationDAOIncome.Stream.Unwrap), 50 * SAT);
        assertEq(income.sweep(address(shares)), 0);
        _invariant();
    }
}
