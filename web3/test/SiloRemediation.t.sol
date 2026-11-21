// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAOSilo4626Adapter} from "../contracts/src/federation/CustodianDAOSilo4626Adapter.sol";
import {DAOMockCoordinator, DAOMockVerifier} from "./CustodianDAO.t.sol";

contract RemediationAsset is ERC20 {
    constructor() ERC20("Asset", "A") {}

    function mint(address to, uint256 value) external {
        _mint(to, value);
    }

    function burn(address from, uint256 value) external {
        _burn(from, value);
    }
}

contract RemediationSilo is ERC4626 {
    uint256 public limit = type(uint256).max;
    bool public shortReceipt;

    constructor(IERC20 token) ERC20("Shares", "S") ERC4626(token) {}

    function configure(uint256 limit_, bool short_) external {
        limit = limit_;
        shortReceipt = short_;
    }

    function maxRedeem(address owner) public view override returns (uint256) {
        return Math.min(super.maxRedeem(owner), limit);
    }

    function _withdraw(address caller, address receiver, address owner, uint256 assets, uint256 shares)
        internal
        override
    {
        super._withdraw(caller, receiver, owner, assets, shares);
        if (shortReceipt && assets > 0) RemediationAsset(asset()).burn(receiver, 1);
    }
}

contract SiloRemediationTest is Test {
    RemediationAsset token;
    RemediationSilo silo;
    DAOMockCoordinator coordinator;
    FROSTAuthorization auth;
    CustodianDAOIncomeVault income;
    CustodianDAOSilo4626Adapter adapter;
    uint256 session;
    address constant RECIPIENT = address(0xBEEF);

    function setUp() public {
        token = new RemediationAsset();
        silo = new RemediationSilo(token);
        coordinator = new DAOMockCoordinator(address(new DAOMockVerifier()), address(1));
        address[] memory participants = new address[](1);
        participants[0] = address(1);
        auth = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), participants, 1);
        income = new CustodianDAOIncomeVault(address(token), address(auth));
        adapter = new CustodianDAOSilo4626Adapter(address(income), address(silo));
        bytes32 h = income.getSetSourceOperationHash(
            adapter.SOURCE_ID(), address(adapter), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0
        );
        authorize(h, address(income));
        income.setSource(
            adapter.SOURCE_ID(), address(adapter), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0, h
        );
    }

    function authorize(bytes32 h, address target) internal {
        coordinator.setResult(h, target);
        auth.authorizeOperation(++session, h, target);
    }

    function supply(uint256 amount) internal {
        token.mint(address(this), amount);
        token.approve(address(adapter), amount);
        bytes32 h = adapter.getSupplyOperationHash(address(this), amount, 1);
        authorize(h, address(adapter));
        adapter.supply(address(this), amount, 1, h);
        assertEq(token.allowance(address(adapter), address(silo)), 0);
    }

    function harvest() internal returns (uint256) {
        bytes32 h = adapter.getHarvestOperationHash(0, 1);
        authorize(h, address(adapter));
        return adapter.harvest(0, 1, h);
    }

    function test_OZPrincipal100Donation197() public {
        supply(100);
        token.mint(address(silo), 197);
        assertEq(silo.previewRedeem(100), 295);
        assertEq(harvest(), 194);
        assertEq(adapter.principalAssets(), 100);
        assertEq(silo.previewRedeem(silo.balanceOf(address(adapter))), 101);
        assertEq(income.dayYieldIncome(0), 194);
        assertEq(token.allowance(address(adapter), address(income)), 0);
    }

    function test_PostWithdrawalRoundingAllowsAuthorizedMinimum() public {
        supply(2);
        token.mint(address(silo), 2);
        assertEq(harvest(), 1);
        assertEq(adapter.principalAssets(), 2);
        assertEq(silo.previewRedeem(silo.balanceOf(address(adapter))), 2);
        assertEq(income.dayYieldIncome(0), 1);
    }

    function testFuzz_FractionalBoundaries(uint16 principalSeed, uint16 donationSeed) public {
        uint256 principal = bound(principalSeed, 2, 10000);
        uint256 donation = bound(donationSeed, 3, 10000);
        supply(principal);
        token.mint(address(silo), donation);
        uint256 shares = silo.balanceOf(address(adapter));
        uint256 candidate = silo.previewRedeem(shares) - principal;
        while (candidate > 0) {
            uint256 burned = silo.previewWithdraw(candidate);
            if (burned <= shares && silo.previewRedeem(shares - burned) >= principal) break;
            candidate--;
        }
        if (candidate == 0) {
            bytes32 h = adapter.getHarvestOperationHash(0, 1);
            authorize(h, address(adapter));
            vm.expectRevert(CustodianDAOSilo4626Adapter.InsufficientYield.selector);
            adapter.harvest(0, 1, h);
        } else {
            assertEq(harvest(), candidate);
            assertGe(silo.previewRedeem(silo.balanceOf(address(adapter))), principal);
        }
    }

    function test_PartialIlliquidLossAndFullExit() public {
        supply(101);
        token.burn(address(silo), 51);
        silo.configure(25, false);
        bytes32 h = adapter.getEmergencyRedeemOperationHash(RECIPIENT, 1);
        authorize(h, address(adapter));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InsufficientShares.selector);
        adapter.emergencyRedeem(RECIPIENT, 1, h);
        assertFalse(auth.executedOperations(h));
        h = adapter.getEmergencyRedeemPartialOperationHash(RECIPIENT, 25, 12);
        authorize(h, address(adapter));
        vm.expectEmit(true, false, false, true, address(adapter));
        emit CustodianDAOSilo4626Adapter.EmergencyRedeemed(RECIPIENT, 12, 25);
        assertEq(adapter.emergencyRedeemPartial(RECIPIENT, 25, 12, h), 12);
        assertEq(adapter.principalAssets(), 76);
        assertEq(silo.balanceOf(address(adapter)), 76);
        silo.configure(type(uint256).max, false);
        h = adapter.getEmergencyRedeemOperationHash(RECIPIENT, 1);
        authorize(h, address(adapter));
        adapter.emergencyRedeem(RECIPIENT, 1, h);
        assertEq(adapter.principalAssets(), 0);
        assertEq(silo.balanceOf(address(adapter)), 0);
    }

    function test_BindingReplayAndMinimumRollback() public {
        supply(100);
        bytes32 h = adapter.getEmergencyRedeemPartialOperationHash(RECIPIENT, 20, 21);
        authorize(h, address(adapter));
        vm.expectRevert(CustodianDAOSilo4626Adapter.OperationHashMismatch.selector);
        adapter.emergencyRedeemPartial(address(2), 20, 21, h);
        vm.expectRevert(CustodianDAOSilo4626Adapter.OperationHashMismatch.selector);
        adapter.emergencyRedeemPartial(RECIPIENT, 21, 21, h);
        vm.expectRevert(CustodianDAOSilo4626Adapter.OperationHashMismatch.selector);
        adapter.emergencyRedeemPartial(RECIPIENT, 20, 20, h);
        vm.expectRevert(CustodianDAOSilo4626Adapter.InsufficientYield.selector);
        adapter.emergencyRedeemPartial(RECIPIENT, 20, 21, h);
        assertEq(adapter.operationNonce(), 1);
        assertEq(adapter.principalAssets(), 100);
        assertEq(silo.balanceOf(address(adapter)), 100);
        assertEq(token.balanceOf(RECIPIENT), 0);
        assertFalse(auth.executedOperations(h));
        token.mint(address(silo), 10);
        adapter.emergencyRedeemPartial(RECIPIENT, 20, 21, h);
        vm.expectRevert(CustodianDAOSilo4626Adapter.OperationHashMismatch.selector);
        adapter.emergencyRedeemPartial(RECIPIENT, 20, 21, h);
    }

    function test_CeilWriteoffAndFullPartialExit() public {
        supply(100);
        token.mint(address(silo), 197);
        harvest();
        assertEq(silo.balanceOf(address(adapter)), 34);
        bytes32 h = adapter.getEmergencyRedeemPartialOperationHash(RECIPIENT, 1, 1);
        authorize(h, address(adapter));
        adapter.emergencyRedeemPartial(RECIPIENT, 1, 1, h);
        assertEq(adapter.principalAssets(), 97);
        h = adapter.getEmergencyRedeemPartialOperationHash(RECIPIENT, 33, 1);
        authorize(h, address(adapter));
        adapter.emergencyRedeemPartial(RECIPIENT, 33, 1, h);
        assertEq(adapter.principalAssets(), 0);
    }

    function test_ShortReceiptRollback() public {
        supply(100);
        token.mint(address(silo), 197);
        silo.configure(type(uint256).max, true);
        bytes32 h = adapter.getHarvestOperationHash(0, 1);
        authorize(h, address(adapter));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidTransfer.selector);
        adapter.harvest(0, 1, h);
        assertFalse(auth.executedOperations(h));
        assertEq(adapter.operationNonce(), 1);
        assertEq(silo.balanceOf(address(adapter)), 100);
        assertEq(adapter.principalAssets(), 100);
        assertEq(token.allowance(address(adapter), address(income)), 0);
        h = adapter.getEmergencyRedeemPartialOperationHash(RECIPIENT, 20, 1);
        authorize(h, address(adapter));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidTransfer.selector);
        adapter.emergencyRedeemPartial(RECIPIENT, 20, 1, h);
        assertFalse(auth.executedOperations(h));
        assertEq(adapter.principalAssets(), 100);
    }

    function test_PartialRejectsAboveLiquidityAndZeroShares() public {
        supply(100);
        silo.configure(10, false);
        bytes32 h = adapter.getEmergencyRedeemPartialOperationHash(RECIPIENT, 11, 1);
        authorize(h, address(adapter));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InsufficientShares.selector);
        adapter.emergencyRedeemPartial(RECIPIENT, 11, 1, h);
        assertFalse(auth.executedOperations(h));
        assertEq(adapter.operationNonce(), 1);
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidAmount.selector);
        adapter.emergencyRedeemPartial(RECIPIENT, 0, 1, bytes32(0));
    }

    function test_PrincipalWithdrawalExactReceiptRollback() public {
        supply(100);
        silo.configure(type(uint256).max, true);
        bytes32 h = adapter.getWithdrawOperationHash(RECIPIENT, 10);
        authorize(h, address(adapter));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidTransfer.selector);
        adapter.withdrawPrincipal(RECIPIENT, 10, h);
        assertFalse(auth.executedOperations(h));
        assertEq(adapter.principalAssets(), 100);
        assertEq(adapter.operationNonce(), 1);
        silo.configure(type(uint256).max, false);
        assertEq(adapter.withdrawPrincipal(RECIPIENT, 10, h), 10);
        assertEq(adapter.principalAssets(), 90);
        assertEq(token.balanceOf(RECIPIENT), 10);
    }

    function test_SelfRecipientRejected() public {
        supply(100);
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidAmount.selector);
        adapter.withdrawPrincipal(address(adapter), 1, bytes32(0));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidAmount.selector);
        adapter.emergencyRedeem(address(adapter), 1, bytes32(0));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidAmount.selector);
        adapter.emergencyRedeemPartial(address(adapter), 1, 1, bytes32(0));
    }
}
