// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianDAOIncomeVault} from "./CustodianDAOIncomeVault.sol";

contract CustodianDAOFeeRouter is ReentrancyGuard {
    using SafeERC20 for IERC20;
    enum FeeStream { Wrap, Unwrap, Deploy }
    event FeeReceivedV1(uint256 indexed day,address indexed payer,FeeStream indexed stream,uint256 gross,uint256 daoAmount,uint256 treasuryAmount);


    CustodianDAOIncomeVault public immutable incomeVault;
    FROSTAuthorization public immutable authorization;
    IERC20 public immutable rewardToken;
    address public treasury;
    uint16 public wrapStakerBps;
    uint16 public deployStakerBps;
    uint256 public operationNonce;
    mapping(address => bool) public authorizedPayers;

    event FeeReceived(
        uint256 indexed day,
        address indexed payer,
        bool indexed isWrap,
        uint256 gross,
        uint256 stakerAmount,
        uint256 treasuryAmount
    );
    event FeeSharesChanged(uint16 wrapStakerBps, uint16 deployStakerBps);
    event TreasuryChanged(address indexed treasury);
    event PayerChanged(address indexed payer, bool authorized);

    error InvalidAddress();
    error InvalidShare();
    error InvalidAmount();
    error InvalidDay();
    error UnauthorizedPayer();
    error InvalidOperationHash();
    error InvalidTransfer();

    constructor(address incomeVault_, address treasury_, uint16 wrapStakerBps_, uint16 deployStakerBps_) {
        if (
            incomeVault_.code.length == 0 || treasury_ == address(0) || treasury_ == incomeVault_
                || treasury_ == address(this)
        ) {
            revert InvalidAddress();
        }
        if (wrapStakerBps_ > 10_000 || deployStakerBps_ > 10_000) revert InvalidShare();
        incomeVault = CustodianDAOIncomeVault(incomeVault_);
        authorization = incomeVault.authorization();
        rewardToken = incomeVault.rewardToken();
        if (treasury_ == address(rewardToken)) revert InvalidAddress();
        treasury = treasury_;
        wrapStakerBps = wrapStakerBps_;
        deployStakerBps = deployStakerBps_;
    }

    function getSetFeeSharesOperationHash(uint16 wrapBps, uint16 deployBps) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.setFeeShares.selector, keccak256(abi.encode(wrapBps, deployBps)), operationNonce
        );
    }

    function setFeeShares(uint16 wrapBps, uint16 deployBps, bytes32 operationHash) external {
        if (wrapBps > 10_000 || deployBps > 10_000) revert InvalidShare();
        if (operationHash != getSetFeeSharesOperationHash(wrapBps, deployBps)) revert InvalidOperationHash();
        authorization.consumeOperation(operationHash);
        operationNonce++;
        wrapStakerBps = wrapBps;
        deployStakerBps = deployBps;
        emit FeeSharesChanged(wrapBps, deployBps);
    }

    function getSetTreasuryOperationHash(address newTreasury) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.setTreasury.selector, keccak256(abi.encode(newTreasury)), operationNonce
        );
    }

    function setTreasury(address newTreasury, bytes32 operationHash) external {
        if (
            newTreasury == address(0) || newTreasury == address(incomeVault) || newTreasury == address(rewardToken)
                || newTreasury == address(this)
        ) {
            revert InvalidAddress();
        }
        if (operationHash != getSetTreasuryOperationHash(newTreasury)) revert InvalidOperationHash();
        authorization.consumeOperation(operationHash);
        operationNonce++;
        treasury = newTreasury;
        emit TreasuryChanged(newTreasury);
    }

    function getSetPayerOperationHash(address payer, bool allowed) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.setPayer.selector, keccak256(abi.encode(payer, allowed)), operationNonce
        );
    }

    function setPayer(address payer, bool allowed, bytes32 operationHash) external {
        if (payer == address(0)) revert InvalidAddress();
        if (operationHash != getSetPayerOperationHash(payer, allowed)) revert InvalidOperationHash();
        authorization.consumeOperation(operationHash);
        operationNonce++;
        authorizedPayers[payer] = allowed;
        emit PayerChanged(payer, allowed);
    }

    function depositFee(uint256 day, bool isWrap, uint256 amount)
        external
        nonReentrant
        returns (uint256 stakerAmount)
    {
        return _depositFee(day,isWrap?FeeStream.Wrap:FeeStream.Deploy,amount);
    }
    function depositFeeV1(uint256 day,FeeStream stream,uint256 amount) external nonReentrant returns(uint256) {
        return _depositFee(day,stream,amount);
    }
    function _depositFee(uint256 day,FeeStream stream,uint256 amount) private returns(uint256 stakerAmount) {
        if (!authorizedPayers[msg.sender]) revert UnauthorizedPayer();
        if (amount == 0) revert InvalidAmount();
        if (day < incomeVault.firstSettlementDay() || day > block.timestamp / 1 days || incomeVault.released(day)) {
            revert InvalidDay();
        }
        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        rewardToken.safeTransferFrom(msg.sender, address(this), amount);
        if (rewardToken.balanceOf(address(this)) - beforeBalance != amount) revert InvalidTransfer();

        uint256 shareBps = stream==FeeStream.Deploy ? deployStakerBps : wrapStakerBps;
        stakerAmount = Math.mulDiv(amount, shareBps, 10_000);
        if (stakerAmount > 0) {
            rewardToken.forceApprove(address(incomeVault), stakerAmount);
            incomeVault.deposit(day, stream==FeeStream.Wrap ? incomeVault.WRAP_FEE() : (stream==FeeStream.Unwrap ? incomeVault.UNWRAP_FEE() : incomeVault.DEPLOY_FEE()), stakerAmount);
            rewardToken.forceApprove(address(incomeVault),0);
        }
        uint256 treasuryAmount = amount - stakerAmount;
        if (treasuryAmount > 0) {
            uint256 treasuryBefore = rewardToken.balanceOf(treasury);
            rewardToken.safeTransfer(treasury, treasuryAmount);
            if (rewardToken.balanceOf(treasury) - treasuryBefore != treasuryAmount) revert InvalidTransfer();
        }
        emit FeeReceivedV1(day, msg.sender, stream, amount, stakerAmount, treasuryAmount);
    }
}
