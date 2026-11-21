// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianDAOIncomeVault} from "./CustodianDAOIncomeVault.sol";

contract CustodianDAOSilo4626Adapter is ReentrancyGuard {
    using SafeERC20 for IERC20;

    bytes32 public constant SOURCE_ID = keccak256("SILO");

    CustodianDAOIncomeVault public immutable incomeVault;
    FROSTAuthorization public immutable authorization;
    IERC20 public immutable rewardToken;
    IERC4626 public immutable siloVault;
    uint256 public principalAssets;
    uint256 public operationNonce;

    event PrincipalSupplied(address indexed payer, uint256 assets, uint256 shares);
    event PrincipalWithdrawn(address indexed recipient, uint256 assets);
    event YieldHarvested(uint256 indexed day, uint256 assets);
    event EmergencyRedeemed(address indexed recipient, uint256 assets, uint256 writtenOffPrincipal);

    event EmergencyPartiallyRedeemed(
        address indexed recipient, uint256 shares, uint256 assets, uint256 writtenOffPrincipal
    );

    error InvalidAddress();
    error InvalidVaultAsset();
    error InvalidAmount();
    error OperationHashMismatch();
    error UnauthorizedPayer();
    error InsufficientShares();
    error InsufficientYield();
    error InvalidTransfer();

    constructor(address incomeVault_, address siloVault_) {
        if (
            incomeVault_ == address(0) || incomeVault_.code.length == 0 || siloVault_ == address(0)
                || siloVault_.code.length == 0
        ) revert InvalidAddress();
        incomeVault = CustodianDAOIncomeVault(incomeVault_);
        authorization = incomeVault.authorization();
        rewardToken = incomeVault.rewardToken();
        siloVault = IERC4626(siloVault_);
        if (siloVault.asset() != address(rewardToken)) revert InvalidVaultAsset();
    }

    function getSupplyOperationHash(address payer, uint256 assets, uint256 minimumShares)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this), this.supply.selector, keccak256(abi.encode(payer, assets, minimumShares)), operationNonce
        );
    }

    function getHarvestOperationHash(uint256 day, uint256 minimumYield) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.harvest.selector, keccak256(abi.encode(day, minimumYield)), operationNonce
        );
    }

    function getWithdrawOperationHash(address recipient, uint256 assets) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.withdrawPrincipal.selector, keccak256(abi.encode(recipient, assets)), operationNonce
        );
    }

    function getEmergencyRedeemOperationHash(address recipient, uint256 minimumReceived)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this),
            this.emergencyRedeem.selector,
            keccak256(abi.encode(recipient, minimumReceived)),
            operationNonce
        );
    }

    function getEmergencyRedeemPartialOperationHash(address recipient, uint256 shares, uint256 minimumReceived)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this),
            this.emergencyRedeemPartial.selector,
            keccak256(abi.encode(recipient, shares, minimumReceived)),
            operationNonce
        );
    }

    function supply(address payer, uint256 assets, uint256 minimumShares, bytes32 operationHash)
        external
        nonReentrant
        returns (uint256 shares)
    {
        if (msg.sender != payer) revert UnauthorizedPayer();
        if (assets == 0 || minimumShares == 0) revert InvalidAmount();
        if (operationHash != getSupplyOperationHash(payer, assets, minimumShares)) revert OperationHashMismatch();
        authorization.consumeOperation(operationHash);
        operationNonce++;

        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        rewardToken.safeTransferFrom(payer, address(this), assets);
        if (rewardToken.balanceOf(address(this)) - beforeBalance != assets) revert InvalidTransfer();
        rewardToken.forceApprove(address(siloVault), assets);
        shares = siloVault.deposit(assets, address(this));
        rewardToken.forceApprove(address(siloVault), 0);
        if (shares < minimumShares) revert InsufficientShares();
        principalAssets += assets;
        emit PrincipalSupplied(payer, assets, shares);
    }

    function harvest(uint256 day, uint256 minimumYield, bytes32 operationHash)
        external
        nonReentrant
        returns (uint256 received)
    {
        if (minimumYield == 0) revert InvalidAmount();
        if (operationHash != getHarvestOperationHash(day, minimumYield)) revert OperationHashMismatch();
        authorization.consumeOperation(operationHash);
        operationNonce++;

        uint256 positionShares = siloVault.balanceOf(address(this));
        uint256 positionValue = siloVault.previewRedeem(positionShares);
        if (positionValue <= principalAssets) revert InsufficientYield();
        uint256 upper = Math.min(positionValue - principalAssets, siloVault.maxWithdraw(address(this)));
        uint256 withdrawalLimit = upper;
        // Asset subtraction alone is unsafe: withdraw rounds the shares burned up.
        // Search for the largest withdrawal whose remaining shares cover principal.
        uint256 yieldAssets;
        while (yieldAssets < upper) {
            uint256 candidate = yieldAssets + (upper - yieldAssets) / 2 + (upper - yieldAssets) % 2;
            uint256 burnedShares = siloVault.previewWithdraw(candidate);
            if (
                burnedShares <= positionShares
                    && siloVault.previewRedeem(positionShares - burnedShares) >= principalAssets
            ) {
                yieldAssets = candidate;
            } else {
                upper = candidate - 1;
            }
        }
        // Pre-state previews are conservative at rounding boundaries. Try the
        // authorized minimum atomically; the post-withdraw principal check is final.
        if (yieldAssets < minimumYield) {
            if (minimumYield > withdrawalLimit) revert InsufficientYield();
            yieldAssets = minimumYield;
        }

        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        siloVault.withdraw(yieldAssets, address(this), address(this));
        received = rewardToken.balanceOf(address(this)) - beforeBalance;
        if (received != yieldAssets) revert InvalidTransfer();
        if (received < minimumYield) revert InsufficientYield();
        if (siloVault.previewRedeem(siloVault.balanceOf(address(this))) < principalAssets) revert InsufficientYield();
        rewardToken.forceApprove(address(incomeVault), received);
        incomeVault.deposit(day, SOURCE_ID, received);
        rewardToken.forceApprove(address(incomeVault), 0);
        emit YieldHarvested(day, received);
    }

    function withdrawPrincipal(address recipient, uint256 assets, bytes32 operationHash)
        external
        nonReentrant
        returns (uint256 received)
    {
        if (recipient == address(0) || recipient == address(this) || assets == 0 || assets > principalAssets) {
            revert InvalidAmount();
        }
        if (operationHash != getWithdrawOperationHash(recipient, assets)) revert OperationHashMismatch();
        authorization.consumeOperation(operationHash);
        operationNonce++;

        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        siloVault.withdraw(assets, address(this), address(this));
        received = rewardToken.balanceOf(address(this)) - beforeBalance;
        if (received != assets) revert InvalidTransfer();
        principalAssets -= assets;
        rewardToken.safeTransfer(recipient, received);
        emit PrincipalWithdrawn(recipient, received);
    }

    function emergencyRedeem(address recipient, uint256 minimumReceived, bytes32 operationHash)
        external
        nonReentrant
        returns (uint256 received)
    {
        if (recipient == address(0) || recipient == address(this) || minimumReceived == 0) revert InvalidAmount();
        if (operationHash != getEmergencyRedeemOperationHash(recipient, minimumReceived)) {
            revert OperationHashMismatch();
        }
        authorization.consumeOperation(operationHash);
        operationNonce++;

        uint256 shares = siloVault.balanceOf(address(this));
        received = _emergencyRedeem(recipient, shares, minimumReceived);
    }

    function emergencyRedeemPartial(address recipient, uint256 shares, uint256 minimumReceived, bytes32 operationHash)
        external
        nonReentrant
        returns (uint256 received)
    {
        if (recipient == address(0) || recipient == address(this) || shares == 0 || minimumReceived == 0) {
            revert InvalidAmount();
        }
        if (operationHash != getEmergencyRedeemPartialOperationHash(recipient, shares, minimumReceived)) {
            revert OperationHashMismatch();
        }
        authorization.consumeOperation(operationHash);
        operationNonce++;
        received = _emergencyRedeem(recipient, shares, minimumReceived);
    }

    function _emergencyRedeem(address recipient, uint256 shares, uint256 minimumReceived)
        private
        returns (uint256 received)
    {
        uint256 totalShares = siloVault.balanceOf(address(this));
        if (shares == 0 || shares > totalShares || shares > siloVault.maxRedeem(address(this))) {
            revert InsufficientShares();
        }
        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        uint256 reported = siloVault.redeem(shares, address(this), address(this));
        received = rewardToken.balanceOf(address(this)) - beforeBalance;
        if (received != reported) revert InvalidTransfer();
        if (received < minimumReceived) revert InsufficientYield();
        uint256 writtenOffPrincipal = shares == totalShares
            ? principalAssets
            : Math.mulDiv(principalAssets, shares, totalShares, Math.Rounding.Ceil);
        principalAssets -= writtenOffPrincipal;
        rewardToken.safeTransfer(recipient, received);
        emit EmergencyRedeemed(recipient, received, writtenOffPrincipal);
        emit EmergencyPartiallyRedeemed(recipient, shares, received, writtenOffPrincipal);
    }
}
