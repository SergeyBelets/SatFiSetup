// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SimpleMultiPool} from "../contracts/src/demo/SimpleMultiPool.sol";
import {ConfederationMultiPool} from "../contracts/src/federation/ConfederationMultiPool.sol";
import {MultiPoolShareToken} from "../contracts/src/tokens/MultiPoolShareToken.sol";
import {ConfederationToken} from "../contracts/src/tokens/ConfederationToken.sol";
import {FederationFeeManager, IFeeManager} from "../contracts/src/federation/FederationFeeManager.sol";
import {FederationNFTModifier} from "../contracts/src/tokens/FederationNFTModifier.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {BitcoinWithdrawalEscrow} from "../contracts/src/federation/BitcoinWithdrawalEscrow.sol";
import "./ValueFlowTestFixtures.sol";
import {SimpleMultiPoolDeployer} from "../contracts/src/federation/SimpleMultiPoolDeployer.sol";
import {MultiPoolDeployer} from "../contracts/src/federation/MultiPoolDeployer.sol";
import {FROSTAuthorizationDeployer} from "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import {MultiPoolShareTokenDeployer} from "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";
import {ConfederationTokenDeployer} from "../contracts/src/federation/ConfederationTokenDeployer.sol";
import {CallbackPoolRegistry} from "../contracts/src/federation/CallbackPoolRegistry.sol";

interface IValueFlowPool {
    struct Withdrawal {
        address requester;
        address mpToken;
        uint256 shareAmount;
        uint256 mpAmount;
        bytes btcScript;
        bytes metadata;
        bool isProcessed;
        uint256 timestamp;
        bytes32 operationHash;
    }

    function getWithdrawal(uint256) external view returns (Withdrawal memory);
    function withdrawalCounter() external view returns (uint256);
    function pendingUserPoolShares(address, address) external view returns (uint256);
    function userPoolBalances(address, address) external view returns (uint256);
    function escrowedMP(address) external view returns (uint256);
    function accruedFees(address, bool) external view returns (uint256);
    function pendingUnreportedFees(address, bool) external view returns (uint256);
    function getTotalValueLocked() external view returns (uint256);
    function requestWithdrawal(address, uint256, bytes calldata) external returns (uint256);
    function requestWithdrawalWithMetadata(address, uint256, bytes calldata, bytes calldata) external returns (uint256);
    function requestWithdrawalWithNFT(address, uint256, bytes calldata, uint256) external returns (uint256);
    function deposit(address, uint256) external returns (uint256);
    function cancelWithdrawal(uint256) external;
    function setFeeManager(address, bytes32) external;
    function getSetFeeManagerOperationHash(address) external view returns (bytes32);
    function setWithdrawalEscrow(address, bytes32) external;
    function getSetWithdrawalEscrowOperationHash(address) external view returns (bytes32);
    function addPool(address, string calldata, bytes32) external;
    function getAddPoolOperationHash(address, string calldata) external view returns (bytes32);
    function getClaimFeesOperationHash(address, bool, address, uint256) external view returns (bytes32);
    function claimFees(address, bool, address, uint256, bytes32) external;
    function syncFeeReports(address, bool) external;
}

abstract contract WithdrawalRoundingBase is Test {
    uint256 internal constant SAT = 1e10;
    uint256 internal constant DEPOSIT = 10_000_000 * SAT;
    address internal constant TREASURY = address(0xBEEF);
    bytes internal p2wpkh = hex"00141111111111111111111111111111111111111111";
    bytes internal p2tr = hex"51202222222222222222222222222222222222222222222222222222222222222222";
    IValueFlowPool internal pool;
    IERC20 internal shares;
    PoolMpToken internal mp;
    ValueFlowMiningPool internal mining;
    ValueFlowCoordinator internal coordinator;
    FROSTAuthorization internal authorization;
    CustodianValidator internal validator;
    ValueFlowBatches internal batches;
    ValueFlowProof internal proof;
    ValueFlowDeposits internal deposits;
    BitcoinWithdrawalEscrow internal escrow;
    FederationFeeManager internal manager;
    FederationNFTModifier internal nft;
    address internal simpleOrigin;
    address internal confederationOrigin;

    function _confederated() internal pure virtual returns (bool);

    function setUp() public {
        vm.warp(100 days);
        coordinator = new ValueFlowCoordinator(address(this));
        address[] memory signers = new address[](1);
        signers[0] = address(this);
        if (_confederated()) {
            authorization = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), signers, 1);
            validator = new CustodianValidator(signers, 1, address(authorization));
            ConfederationTokenDeployer tokenOrigin = new ConfederationTokenDeployer();
            MultiPoolDeployer origin = new MultiPoolDeployer(address(tokenOrigin));
            tokenOrigin.setAuthorizedDeployer(address(origin), true);
            (address deployed, address token) = origin.deployMultiPool("rounding", address(authorization),
                address(validator), "Confederation Shares", "CS");
            shares = IERC20(token);
            pool = IValueFlowPool(deployed);
            confederationOrigin = address(origin);
        } else {
            FROSTAuthorizationDeployer authOrigin = new FROSTAuthorizationDeployer();
            MultiPoolShareTokenDeployer tokenOrigin = new MultiPoolShareTokenDeployer();
            SimpleMultiPoolDeployer origin = new SimpleMultiPoolDeployer(address(authOrigin), address(tokenOrigin));
            authOrigin.setAuthorizedDeployer(address(origin), true);
            tokenOrigin.setAuthorizedDeployer(address(origin), true);
            (address deployed, address auth, address token) = origin.deploySimpleMultiPool("rounding", signers, 1,
                "Federation Shares", "FS", address(coordinator), abi.encodePacked(bytes32(uint256(1))));
            shares = IERC20(token);
            pool = IValueFlowPool(deployed);
            authorization = FROSTAuthorization(auth);
            simpleOrigin = address(origin);
        }
        mining = new ValueFlowMiningPool();
        mp = mining.mpToken();
        batches = new ValueFlowBatches(address(authorization));
        proof = new ValueFlowProof();
        escrow = new BitcoinWithdrawalEscrow(address(batches), address(proof), 6, 294, 330);
        deposits = new ValueFlowDeposits(address(escrow), address(mining));
        bytes32 op = escrow.getSetDepositRegistryOperationHash(address(deposits));
        _approve(op, address(escrow));
        escrow.setDepositRegistry(address(deposits), op);
        op = pool.getSetWithdrawalEscrowOperationHash(address(escrow));
        _approve(op, address(pool));
        pool.setWithdrawalEscrow(address(escrow), op);
        CallbackPoolRegistry callbacks = new CallbackPoolRegistry(address(escrow), address(authorization),
            simpleOrigin, confederationOrigin);
        op = escrow.getSetCallbackPoolRegistryOperationHash(address(callbacks));
        _approve(op, address(escrow));
        escrow.setCallbackPoolRegistry(address(callbacks), op);
        op = pool.getAddPoolOperationHash(address(mp), "backing");
        _approve(op, address(pool));
        pool.addPool(address(mp), "backing", op);
        mining.mint(address(this), DEPOSIT);
        mp.approve(address(pool), DEPOSIT);
        pool.deposit(address(mp), DEPOSIT);
        mp.approve(address(escrow), type(uint256).max);
    }

    function _approve(bytes32 op, address target) internal {
        coordinator.approve(authorization, op, target);
        if (address(validator) != address(0)) validator.registerCustodianSignature(op);
    }

    function _configureFee(uint256 bps, uint256 discount) internal {
        IFeeManager.FeeConfig memory zero = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, 0, 0, 0);
        IFeeManager.FeeConfig memory unwrap = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, bps, 0, 0);
        manager = new FederationFeeManager(address(authorization), address(validator), TREASURY, zero, unwrap);
        bytes32 op = manager.getSetFeeCollectorOperationHash(address(pool), true);
        _approve(op, address(manager));
        manager.setFeeCollector(address(pool), true, op);
        op = pool.getSetFeeManagerOperationHash(address(manager));
        _approve(op, address(pool));
        pool.setFeeManager(address(manager), op);
        nft = new FederationNFTModifier("Discount", "DISC", address(this));
        nft.grantRole(nft.USAGE_ROLE(), address(manager));
        nft.mint(address(this), block.timestamp + 30 days, DEPOSIT, discount);
        op = manager.getSetNFTModifierOperationHash(address(nft));
        _approve(op, address(manager));
        manager.setNFTModifier(address(nft), op);
    }

    function _request(uint256 path, uint256 amount, bytes memory script) internal returns (uint256) {
        if (path == 0) return pool.requestWithdrawal(address(mp), amount, script);
        if (path == 1) return pool.requestWithdrawalWithMetadata(address(mp), amount, script, "invoice-42");
        return pool.requestWithdrawalWithNFT(address(mp), amount, script, 0);
    }

    function _assertStored(uint256 id, uint256 amount, uint256 nominalFee, bytes memory script, uint256 path) internal view {
        IValueFlowPool.Withdrawal memory w = pool.getWithdrawal(id);
        uint256 net = amount - nominalFee;
        uint256 payout = net / SAT * SAT;
        uint256 fee = amount - w.mpAmount;
        assertEq(w.shareAmount, amount);
        assertEq(w.mpAmount, payout);
        assertEq(w.mpAmount + fee, amount);
        assertEq(w.mpAmount % SAT, 0);
        assertEq(fee - nominalFee, net % SAT);
        assertLt(fee - nominalFee, SAT);
        bytes4 method = _confederated() ? ConfederationMultiPool.processWithdrawal.selector : SimpleMultiPool.processWithdrawal.selector;
        assertEq(w.operationHash, authorization.hashOperation(address(pool), method,
            keccak256(abi.encode(id, address(this), address(mp), amount, payout, script, path == 1 ? bytes("invoice-42") : bytes(""), w.timestamp)), id));
        bytes32 key = escrow.requestKey(address(pool), id);
        (address ownerPool, uint256 localId, address token, address owner, uint256 locked, bytes memory lockedScript,, BitcoinWithdrawalEscrow.State state) = escrow.requests(key);
        assertEq(ownerPool, address(pool));
        assertEq(localId, id);
        assertEq(token, address(mp));
        assertEq(owner, address(this));
        assertEq(locked, payout);
        assertEq(lockedScript, script);
        assertEq(uint256(state), uint256(BitcoinWithdrawalEscrow.State.Pending));
    }

    function _assertFailedUnchanged(uint256 path, uint256 amount, bytes memory script, bytes memory reason) internal {
        uint256 counter = pool.withdrawalCounter();
        uint256 pending = pool.pendingUserPoolShares(address(this), address(mp));
        uint256 shareBalance = shares.balanceOf(address(this));
        uint256 poolBalance = mp.balanceOf(address(pool));
        uint256 escrowBalance = mp.balanceOf(address(escrow));
        uint256 beforeUsed;
        if (address(nft) != address(0)) (,, beforeUsed,,) = nft.nftMetadata(0);
        vm.expectRevert(reason);
        _request(path, amount, script);
        assertEq(pool.withdrawalCounter(), counter);
        assertEq(pool.pendingUserPoolShares(address(this), address(mp)), pending);
        assertEq(shares.balanceOf(address(this)), shareBalance);
        assertEq(mp.balanceOf(address(pool)), poolBalance);
        assertEq(mp.balanceOf(address(escrow)), escrowBalance);
        assertEq(pool.getTotalValueLocked(), DEPOSIT);
        assertEq(pool.accruedFees(address(mp), false), 0);
        assertEq(manager.totalCollected(), 0);
        if (address(nft) != address(0)) {
            (,, uint256 afterUsed,,) = nft.nftMetadata(0);
            assertEq(afterUsed, beforeUsed);
        }
    }

    function _zeroError() internal pure returns (bytes memory) {
        return _confederated() ? abi.encodeWithSelector(ConfederationMultiPool.ZeroAmount.selector) : abi.encodeWithSignature("Error(string)", "Zero amount");
    }

    function test_ExplicitEscrowMinimumsAndConstructorValidation() public {
        bytes memory initcode = abi.encodePacked(type(BitcoinWithdrawalEscrow).creationCode,
            abi.encode(address(batches), address(proof), uint32(6), uint256(294), uint256(330)));
        assertEq(initcode.length, type(BitcoinWithdrawalEscrow).creationCode.length + 160);
        assertLe(initcode.length, 49_152);
        assertLe(address(escrow).code.length, 24_576);
        assertEq(escrow.minP2WPKHSats(), 294);
        assertEq(escrow.minP2TRSats(), 330);
        vm.expectRevert("withdrawal minimums");
        new BitcoinWithdrawalEscrow(address(batches), address(proof), 6, 0, 330);
        vm.expectRevert("withdrawal minimums");
        new BitcoinWithdrawalEscrow(address(batches), address(proof), 6, 294, 0);
        vm.expectRevert("withdrawal minimums");
        new BitcoinWithdrawalEscrow(address(batches), address(proof), 6, 293, 330);
        vm.expectRevert("withdrawal minimums");
        new BitcoinWithdrawalEscrow(address(batches), address(proof), 6, 294, 329);
        BitcoinWithdrawalEscrow higher = new BitcoinWithdrawalEscrow(address(batches), address(proof), 6, 500, 600);
        assertEq(higher.minP2WPKHSats(), 500);
        assertEq(higher.minP2TRSats(), 600);
    }

    function test_AllPathsRoundWithNoManagerBeforeHashingAndLocking() public {
        uint256[3] memory remainders = [uint256(0), 1, SAT - 1];
        for (uint256 path; path < 3; path++) {
            for (uint256 s; s < 2; s++) {
                bytes memory script = s == 0 ? p2wpkh : p2tr;
                for (uint256 r; r < 3; r++) {
                    uint256 amount = (s == 0 ? 294 : 330) * SAT + remainders[r];
                    uint256 id = _request(path, amount, script);
                    _assertStored(id, amount, 0, script, path);
                    assertEq(pool.getTotalValueLocked(), DEPOSIT);
                    assertEq(pool.accruedFees(address(mp), false), 0);
                }
            }
        }
    }

    function test_AllPathsEnforceScriptMinimumAndRollbackAllState() public {
        _configureFee(0, 0);
        for (uint256 path; path < 3; path++) {
            for (uint256 s; s < 2; s++) {
                bytes memory script = s == 0 ? p2wpkh : p2tr;
                uint256 minimum = s == 0 ? 294 : 330;
                _assertFailedUnchanged(path, (minimum - 1) * SAT, script,
                    abi.encodeWithSignature("Error(string)", "below withdrawal minimum"));
                uint256 id = _request(path, minimum * SAT, script);
                _assertStored(id, minimum * SAT, 0, script, path);
                id = _request(path, (minimum + 1) * SAT, script);
                _assertStored(id, (minimum + 1) * SAT, 0, script, path);
            }
        }
    }

    function test_OnePercentRoundingExamplesOnEveryPath() public {
        _configureFee(100, 0);
        for (uint256 path; path < 3; path++) {
            _assertFailedUnchanged(path, 101 * SAT, p2wpkh,
                abi.encodeWithSignature("Error(string)", "below withdrawal minimum"));
            uint256 id = _request(path, 297 * SAT, p2wpkh);
            _assertStored(id, 297 * SAT, 297 * SAT / 100, p2wpkh, path);
            assertEq(pool.getWithdrawal(id).mpAmount, 294 * SAT);
            id = _request(path, 334 * SAT, p2tr);
            _assertStored(id, 334 * SAT, 334 * SAT / 100, p2tr, path);
            assertEq(pool.getWithdrawal(id).mpAmount, 330 * SAT);
        }
    }

    function test_AllPathsExactRoundingRemaindersWithOnePercentFee() public {
        _configureFee(100, 0);
        uint256[3] memory remainders = [uint256(0), 1, SAT - 1];
        for (uint256 path; path < 3; path++) {
            for (uint256 s; s < 2; s++) {
                bytes memory script = s == 0 ? p2wpkh : p2tr;
                for (uint256 r; r < 3; r++) {
                    uint256 net = (s == 0 ? 294 : 330) * SAT + remainders[r];
                    uint256 amount = net * 100 / 99;
                    assertEq(amount - amount / 100, net);
                    uint256 id = _request(path, amount, script);
                    _assertStored(id, amount, amount / 100, script, path);
                }
            }
        }
    }

    function test_OneHundredPercentFeeAndSubSatoshiZeroRejectAtomically() public {
        _configureFee(10_000, 0);
        for (uint256 path; path < 3; path++) {
            _assertFailedUnchanged(path, 1_000 * SAT, p2wpkh, _zeroError());
        }
    }

    function test_SubSatoshiRoundingToZeroRejectsWithoutFee() public {
        _configureFee(0, 0);
        for (uint256 path; path < 3; path++) _assertFailedUnchanged(path, SAT - 1, p2wpkh, _zeroError());
    }

    function test_FullNFTDiscountStillRoundsAndRollsBackConsumedLimitOnFailure() public {
        _configureFee(10_000, 10_000);
        _assertFailedUnchanged(2, 293 * SAT + SAT - 1, p2wpkh,
            abi.encodeWithSignature("Error(string)", "below withdrawal minimum"));
        uint256 amount = 294 * SAT + SAT - 1;
        uint256 id = _request(2, amount, p2wpkh);
        _assertStored(id, amount, 0, p2wpkh, 2);
        (,, uint256 used,,) = nft.nftMetadata(0);
        assertEq(used, amount);
    }

    function test_DirectRequestsMinimumBoundariesFractionalAndZero() public {
        mining.mint(address(this), 10_000 * SAT);
        uint256 id = 100;
        for (uint256 s; s < 2; s++) {
            bytes memory script = s == 0 ? p2wpkh : p2tr;
            uint256 minimum = s == 0 ? 294 : 330;
            vm.expectRevert("below withdrawal minimum");
            escrow.requestDirect(id, address(mp), (minimum - 1) * SAT, script);
            vm.expectRevert("amount or duplicate");
            escrow.requestDirect(id, address(mp), minimum * SAT + 1, script);
            vm.expectRevert("amount or duplicate");
            escrow.requestDirect(id, address(mp), minimum * SAT + SAT - 1, script);
            vm.expectRevert("amount or duplicate");
            escrow.requestDirect(id, address(mp), 0, script);
            escrow.requestDirect(id++, address(mp), minimum * SAT, script);
            escrow.requestDirect(id++, address(mp), (minimum + 1) * SAT, script);
        }
        vm.expectRevert("script");
        escrow.requestDirect(id, address(mp), 1_000 * SAT, hex"76a914111111111111111111111111111111111111111188ac");
    }

    function _prepareAndBind(uint256 id) internal returns (bytes32 batch, bytes memory raw) {
        IValueFlowPool.Withdrawal memory w = pool.getWithdrawal(id);
        _approve(w.operationHash, address(pool));
        if (_confederated()) ConfederationMultiPool(address(pool)).processWithdrawal(id);
        else SimpleMultiPool(address(pool)).processWithdrawal(id, w.operationHash);
        batch = keccak256(abi.encode("batch", id));
        bytes32 prev = bytes32(id + 1);
        bytes32 display;
        for (uint256 i; i < 32; i++) display |= bytes32(uint256(uint8(prev[i])) << (8 * i));
        uint64 sats = uint64(w.mpAmount / SAT);
        bytes8 little;
        for (uint256 i; i < 8; i++) little |= bytes8(uint64(uint8(sats >> (8 * i))) << (8 * (7 - i)));
        raw = abi.encodePacked(hex"0200000001", prev, hex"0000000000ffffffff01", little,
            bytes1(uint8(w.btcScript.length)), w.btcScript, hex"00000000");
        bytes32 txid = sha256(abi.encodePacked(sha256(raw)));
        batches.configure(batch, keccak256(abi.encode(display, uint32(0))), BitcoinBatchRegistry.State.Reserved);
        bytes32[] memory keys = new bytes32[](1);
        keys[0] = escrow.requestKey(address(pool), id);
        bytes32 op = escrow.getBindOperationHash(batch, txid, keys);
        _approve(op, address(escrow));
        escrow.bind(batch, txid, keys, op);
        batches.setState(batch, BitcoinBatchRegistry.State.Signing);
    }

    function _settleAndCheck(uint256 id, uint256 sharesBefore, uint256 tvlBefore, uint256 accruedBefore) internal {
        IValueFlowPool.Withdrawal memory w = pool.getWithdrawal(id);
        uint256 fee = w.shareAmount - w.mpAmount;
        (bytes32 batch, bytes memory raw) = _prepareAndBind(id);
        escrow.settle(batch, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        assertTrue(pool.getWithdrawal(id).isProcessed);
        assertEq(shares.totalSupply(), sharesBefore - w.shareAmount);
        assertEq(pool.getTotalValueLocked(), tvlBefore - w.mpAmount);
        assertEq(pool.accruedFees(address(mp), false), accruedBefore + fee);
        assertEq(mp.balanceOf(address(pool)) + mp.balanceOf(address(escrow)), pool.getTotalValueLocked());
        vm.expectRevert("state");
        escrow.settle(batch, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        assertEq(pool.accruedFees(address(mp), false), accruedBefore + fee);
        assertEq(shares.totalSupply(), sharesBefore - w.shareAmount);
    }

    function test_AllPathsSettleExactFeeOnceAndClaimReducesTVL() public {
        _configureFee(100, 0);
        uint256 accrued;
        for (uint256 path; path < 3; path++) {
            uint256 amount = 1_001 * SAT + 13;
            uint256 supplyBefore = shares.totalSupply();
            uint256 tvlBefore = pool.getTotalValueLocked();
            uint256 id = _request(path, amount, path == 1 ? p2tr : p2wpkh);
            assertEq(pool.accruedFees(address(mp), false), accrued);
            assertEq(pool.getTotalValueLocked(), tvlBefore);
            _settleAndCheck(id, supplyBefore, tvlBefore, accrued);
            accrued += amount - pool.getWithdrawal(id).mpAmount;
        }
        assertEq(pool.pendingUserPoolShares(address(this), address(mp)), 0);
        assertEq(pool.escrowedMP(address(mp)), 0);
        assertEq(manager.collectedFeesByToken(address(mp), false), accrued);
        uint256 before = pool.getTotalValueLocked();
        bytes32 op = pool.getClaimFeesOperationHash(address(mp), false, TREASURY, accrued);
        _approve(op, address(pool));
        pool.claimFees(address(mp), false, TREASURY, accrued, op);
        assertEq(pool.getTotalValueLocked(), before - accrued);
        assertEq(mp.balanceOf(TREASURY), accrued);
        assertEq(pool.accruedFees(address(mp), false), 0);
    }

    function test_NoManagerDustSettlesAndCanBeReportedAfterConfiguration() public {
        uint256 amount = 294 * SAT + 1;
        uint256 id = _request(0, amount, p2wpkh);
        _settleAndCheck(id, DEPOSIT, DEPOSIT, 0);
        assertEq(pool.pendingUnreportedFees(address(mp), false), 1);
        _configureFee(0, 0);
        pool.syncFeeReports(address(mp), false);
        assertEq(pool.pendingUnreportedFees(address(mp), false), 0);
        assertEq(manager.totalCollected(), 1);
    }

    function test_AllPathsCancellationRefundsFullSharesAndNoFee() public {
        _configureFee(100, 0);
        for (uint256 path; path < 3; path++) {
            uint256 amount = 1_001 * SAT + 1;
            uint256 id = _request(path, amount, p2wpkh);
            vm.warp(pool.getWithdrawal(id).timestamp + 7 days);
            pool.cancelWithdrawal(id);
            assertEq(shares.balanceOf(address(this)), DEPOSIT);
            assertEq(mp.balanceOf(address(pool)), DEPOSIT);
            assertEq(pool.getTotalValueLocked(), DEPOSIT);
            assertEq(pool.pendingUserPoolShares(address(this), address(mp)), 0);
            assertEq(pool.accruedFees(address(mp), false), 0);
            assertEq(manager.totalCollected(), 0);
        }
    }

    function test_NoSevenDayRefundOnceBitcoinSigningHasStarted() public {
        uint256 id = _request(0, 1_000 * SAT, p2wpkh);
        _prepareAndBind(id);
        vm.warp(block.timestamp + 7 days);
        vm.expectRevert("signature may exist");
        pool.cancelWithdrawal(id);
        assertEq(pool.pendingUserPoolShares(address(this), address(mp)), 1_000 * SAT);
        assertEq(shares.totalSupply(), DEPOSIT);
        assertEq(pool.getTotalValueLocked(), DEPOSIT);
    }

    function test_DirectPendingRefundReturnsExactPrincipal() public {
        mining.mint(address(this), 330 * SAT);
        escrow.requestDirect(55, address(mp), 330 * SAT, p2tr);
        escrow.refund(escrow.requestKey(address(this), 55));
        assertEq(mp.balanceOf(address(this)), 330 * SAT);
        assertEq(mp.balanceOf(address(escrow)), 0);
    }

    function test_DirectMinimumRequestsSettleAndCannotBurnTwice() public {
        uint256 paid;
        for (uint256 s; s < 2; s++) {
            uint256 id = 100 + s;
            uint64 amount = s == 0 ? 294 : 330;
            bytes memory script = s == 0 ? p2wpkh : p2tr;
            mining.mint(address(this), uint256(amount) * SAT);
            escrow.requestDirect(id, address(mp), uint256(amount) * SAT, script);
            bytes32 batch = keccak256(abi.encode("direct", id));
            bytes32 prev = bytes32(id);
            bytes32 display;
            for (uint256 i; i < 32; i++) display |= bytes32(uint256(uint8(prev[i])) << (8 * i));
            bytes8 little;
            for (uint256 i; i < 8; i++) little |= bytes8(uint64(uint8(amount >> (8 * i))) << (8 * (7 - i)));
            bytes memory raw = abi.encodePacked(hex"0200000001", prev, hex"0000000000ffffffff01", little,
                bytes1(uint8(script.length)), script, hex"00000000");
            bytes32 txid = sha256(abi.encodePacked(sha256(raw)));
            batches.configure(batch, keccak256(abi.encode(display, uint32(0))), BitcoinBatchRegistry.State.Reserved);
            bytes32[] memory keys = new bytes32[](1);
            keys[0] = escrow.requestKey(address(this), id);
            bytes32 op = escrow.getBindOperationHash(batch, txid, keys);
            _approve(op, address(escrow));
            escrow.bind(batch, txid, keys, op);
            batches.setState(batch, BitcoinBatchRegistry.State.Signing);
            vm.expectRevert("signature may exist");
            escrow.refund(keys[0]);
            escrow.settle(batch, raw, bytes32(uint256(1)), new bytes32[](0), 0);
            paid += amount;
            assertEq(deposits.burnedSats(), paid);
            assertEq(mp.totalSupply(), DEPOSIT);
            assertEq(mp.balanceOf(address(escrow)), 0);
            vm.expectRevert("state");
            escrow.settle(batch, raw, bytes32(uint256(1)), new bytes32[](0), 0);
        }
    }

    function testFuzz_RoundedAccountingAndFee(uint64 amountSeed, uint16 bpsSeed, uint8 pathSeed) public {
        uint256 bps = bound(uint256(bpsSeed), 0, 9_000);
        uint256 amount = bound(uint256(amountSeed), 10_000 * SAT, 100_000 * SAT);
        uint256 path = uint256(pathSeed) % 3;
        _configureFee(bps, 0);
        uint256 id = _request(path, amount, p2tr);
        _assertStored(id, amount, amount * bps / 10_000, p2tr, path);
        _settleAndCheck(id, DEPOSIT, DEPOSIT, 0);
    }
}

contract SimpleWithdrawalRoundingTest is WithdrawalRoundingBase {
    function _confederated() internal pure override returns (bool) { return false; }

    function testQuotedWithdrawalLocksExactPrincipalAndOrdinaryFee() public {
        _configureFee(23,0);
        uint256 amount=100_000*SAT+17;
        uint256 fee=amount*23/10000;
        uint256 payout=(amount-fee)/SAT*SAT;
        uint256 id=SimpleMultiPool(address(pool)).requestWithdrawalQuoted(address(mp),amount,p2tr,fee,payout,0,false);
        _assertStored(id,amount,fee,p2tr,0);
    }

    function testQuotedWithdrawalRejectsEvenSubSatoshiFeeChange() public {
        _configureFee(23,0);
        uint256 amount=100_000*SAT+17;
        uint256 fee=amount*23/10000;
        uint256 beforeCounter=pool.withdrawalCounter();
        vm.expectRevert("withdrawal quote changed");
        SimpleMultiPool(address(pool)).requestWithdrawalQuoted(address(mp),amount,p2tr,fee+1,(amount-fee)/SAT*SAT,0,false);
        assertEq(pool.withdrawalCounter(),beforeCounter);
        assertEq(pool.pendingUserPoolShares(address(this),address(mp)),0);
        assertEq(mp.balanceOf(address(escrow)),0);
    }

    function testQuotedWithdrawalRejectsChangedPrincipalAndRollsBackLock() public {
        uint256 amount=100_000*SAT;
        vm.expectRevert("withdrawal quote changed");
        SimpleMultiPool(address(pool)).requestWithdrawalQuoted(address(mp),amount,p2tr,0,amount-SAT,0,false);
        assertEq(pool.withdrawalCounter(),0);
        assertEq(pool.pendingUserPoolShares(address(this),address(mp)),0);
        assertEq(mp.balanceOf(address(escrow)),0);
    }

    function testQuotedWithdrawalNFTConsumesVolumeOnce() public {
        _configureFee(100,5000);
        uint256 amount=100_000*SAT;
        uint256 fee=(amount/100)/2;
        uint256 id=SimpleMultiPool(address(pool)).requestWithdrawalQuoted(address(mp),amount,p2tr,fee,amount-fee,0,true);
        _assertStored(id,amount,fee,p2tr,2);
        (,,uint256 used,,)=nft.nftMetadata(0);
        assertEq(used,amount);
    }

    function testQuotedWithdrawalNFTMismatchDoesNotConsumeVolume() public {
        _configureFee(100,5000);
        uint256 amount=100_000*SAT;
        vm.expectRevert("withdrawal quote changed");
        SimpleMultiPool(address(pool)).requestWithdrawalQuoted(address(mp),amount,p2tr,amount/100,amount-amount/100,0,true);
        (,,uint256 used,,)=nft.nftMetadata(0);
        assertEq(used,0);
        assertEq(pool.withdrawalCounter(),0);
    }

    function testQuotedWithdrawalClearsGuardBeforeNextOrdinaryRequest() public {
        SimpleMultiPool(address(pool)).requestWithdrawalQuoted(address(mp),1000*SAT,p2tr,0,1000*SAT,0,false);
        uint256 id=pool.requestWithdrawal(address(mp),2000*SAT,p2tr);
        assertEq(pool.getWithdrawal(id).mpAmount,2000*SAT);
    }
}

contract ConfederationWithdrawalRoundingTest is WithdrawalRoundingBase {
    function _confederated() internal pure override returns (bool) { return true; }
}
