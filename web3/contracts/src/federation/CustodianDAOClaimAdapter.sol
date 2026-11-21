// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianDAOIncomeVault} from "./CustodianDAOIncomeVault.sol";

contract CustodianDAOClaimAdapter is ReentrancyGuard {
    using SafeERC20 for IERC20;

    CustodianDAOIncomeVault public immutable incomeVault;
    FROSTAuthorization public immutable authorization;
    IERC20 public immutable rewardToken;
    address public immutable protocol;
    bytes32 public immutable sourceId;
    uint256 public operationNonce;

    event IncomeClaimed(uint256 indexed day, bytes32 indexed sourceId, uint256 amount, bytes32 operationHash);

    error InvalidAddress();
    error InvalidSource();
    error OperationHashMismatch();
    error ProtocolCallFailed();
    error InsufficientIncome();

    constructor(address incomeVault_, address protocol_, bytes32 sourceId_) {
        if (
            incomeVault_ == address(0) || incomeVault_.code.length == 0 || protocol_ == address(0)
                || protocol_.code.length == 0
        ) revert InvalidAddress();
        if (sourceId_ == bytes32(0)) revert InvalidSource();
        incomeVault = CustodianDAOIncomeVault(incomeVault_);
        authorization = incomeVault.authorization();
        rewardToken = incomeVault.rewardToken();
        if (protocol_ == address(rewardToken)) revert InvalidAddress();
        protocol = protocol_;
        sourceId = sourceId_;
    }

    function getClaimOperationHash(uint256 day, bytes calldata protocolCall, uint256 minimumReceived)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this),
            this.claimAndDeposit.selector,
            keccak256(abi.encode(day, keccak256(protocolCall), minimumReceived)),
            operationNonce
        );
    }

    function claimAndDeposit(uint256 day, bytes calldata protocolCall, uint256 minimumReceived, bytes32 operationHash)
        external
        nonReentrant
        returns (uint256 received)
    {
        if (minimumReceived == 0) revert InsufficientIncome();
        if (operationHash != getClaimOperationHash(day, protocolCall, minimumReceived)) {
            revert OperationHashMismatch();
        }
        authorization.consumeOperation(operationHash);
        operationNonce++;

        uint256 beforeBalance = rewardToken.balanceOf(address(this));
        (bool success,) = protocol.call(protocolCall);
        if (!success) revert ProtocolCallFailed();
        received = rewardToken.balanceOf(address(this)) - beforeBalance;
        if (received < minimumReceived) revert InsufficientIncome();

        rewardToken.forceApprove(address(incomeVault), received);
        incomeVault.deposit(day, sourceId, received);
        emit IncomeClaimed(day, sourceId, received, operationHash);
    }
}
