// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianDAOIncomeVault} from "./CustodianDAOIncomeVault.sol";

contract CustodianDAOConversionAdapter is ReentrancyGuard {
    using SafeERC20 for IERC20;

    CustodianDAOIncomeVault public immutable incomeVault;
    FROSTAuthorization public immutable authorization;
    IERC20 public immutable rewardToken;
    IERC20 public immutable inputToken;
    address public immutable converter;
    bytes32 public immutable sourceId;
    uint256 public operationNonce;

    event IncomeConverted(
        uint256 indexed day, bytes32 indexed sourceId, address indexed payer, uint256 spentInput, uint256 receivedReward
    );

    error InvalidAddress();
    error InvalidSource();
    error InvalidAmount();
    error UnauthorizedPayer();
    error OperationHashMismatch();
    error ConversionFailed();
    error InsufficientOutput();
    error InvalidTransfer();

    constructor(address incomeVault_, address inputToken_, address converter_, bytes32 sourceId_) {
        if (incomeVault_.code.length == 0 || inputToken_.code.length == 0 || converter_.code.length == 0) {
            revert InvalidAddress();
        }
        if (sourceId_ == bytes32(0)) revert InvalidSource();
        incomeVault = CustodianDAOIncomeVault(incomeVault_);
        authorization = incomeVault.authorization();
        rewardToken = incomeVault.rewardToken();
        if (inputToken_ == address(rewardToken) || inputToken_ == converter_ || converter_ == address(rewardToken)) {
            revert InvalidAddress();
        }
        inputToken = IERC20(inputToken_);
        converter = converter_;
        sourceId = sourceId_;
    }

    function getConvertOperationHash(
        uint256 day,
        address payer,
        uint256 amountIn,
        uint256 minimumReward,
        bytes calldata converterCall
    ) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this),
            this.convertAndDeposit.selector,
            keccak256(abi.encode(day, payer, amountIn, minimumReward, keccak256(converterCall))),
            operationNonce
        );
    }

    function convertAndDeposit(
        uint256 day,
        address payer,
        uint256 amountIn,
        uint256 minimumReward,
        bytes calldata converterCall,
        bytes32 operationHash
    ) external nonReentrant returns (uint256 receivedReward) {
        if (msg.sender != payer) revert UnauthorizedPayer();
        if (amountIn == 0 || minimumReward == 0) revert InvalidAmount();
        if (operationHash != getConvertOperationHash(day, payer, amountIn, minimumReward, converterCall)) {
            revert OperationHashMismatch();
        }
        authorization.consumeOperation(operationHash);
        operationNonce++;

        uint256 inputBefore = inputToken.balanceOf(address(this));
        uint256 rewardBefore = rewardToken.balanceOf(address(this));
        inputToken.safeTransferFrom(payer, address(this), amountIn);
        if (inputToken.balanceOf(address(this)) - inputBefore != amountIn) revert InvalidTransfer();

        inputToken.forceApprove(converter, amountIn);
        (bool success,) = converter.call(converterCall);
        if (!success) revert ConversionFailed();
        inputToken.forceApprove(converter, 0);

        uint256 inputAfter = inputToken.balanceOf(address(this));
        if (inputAfter < inputBefore) revert InvalidTransfer();
        uint256 refund = inputAfter - inputBefore;
        if (refund > amountIn) revert InvalidTransfer();
        uint256 spentInput = amountIn - refund;
        if (spentInput == 0) revert InvalidTransfer();
        if (refund > 0) {
            uint256 payerBefore = inputToken.balanceOf(payer);
            inputToken.safeTransfer(payer, refund);
            if (inputToken.balanceOf(payer) - payerBefore != refund) revert InvalidTransfer();
        }

        receivedReward = rewardToken.balanceOf(address(this)) - rewardBefore;
        if (receivedReward < minimumReward) revert InsufficientOutput();
        rewardToken.forceApprove(address(incomeVault), receivedReward);
        incomeVault.deposit(day, sourceId, receivedReward);
        emit IncomeConverted(day, sourceId, payer, spentInput, receivedReward);
    }
}
