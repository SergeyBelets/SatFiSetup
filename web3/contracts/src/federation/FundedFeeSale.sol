// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianValidator} from "./CustodianValidator.sol";

interface IFundedFeeSaleDAO {
    function shareToken() external view returns (address);
    function authorization() external view returns (FROSTAuthorization);
    function validator() external view returns (CustodianValidator);
    function approvedToken(address token) external view returns (bool);
    function recordFeeSaleProceeds(uint256 amount) external;
}

interface IFundedFeeSalePool {
    function getFrostMultisig() external view returns (address);
    function claimFees(address token, bool isWrap, address recipient, uint256 amount, bytes32 operationHash) external;
}

/// @notice Exchanges captured S or MP fees for stablecoins already deposited by a buyer.
contract FundedFeeSale is ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 private constant BPS = 10_000;

    struct Bid {
        address buyer;
        uint256 feeAmount;
        uint256 stableAmount;
        uint256 deadline;
    }

    address public immutable source;
    IERC20 public immutable feeToken;
    IERC20 public immutable stablecoin;
    address public immutable treasury;
    FROSTAuthorization public immutable authorization;
    CustodianValidator public immutable validator;

    uint256 public sellBps;
    uint256 public inventory;
    uint256 public operationNonce;
    uint256 public bidCount;
    uint256 public stableEscrow;
    mapping(uint256 => Bid) public bids;
    mapping(bytes32 => bool) public processedMPClaims;

    error InvalidConfiguration();
    error InvalidSource();
    error InvalidAuthorization();
    error InvalidAmount();
    error InvalidBid();
    error InvalidTransfer();
    error InvalidClaim();
    error InsufficientInventory();

    event SellBpsSet(uint256 previousBps, uint256 newBps);
    event FeeRecorded(uint256 amount);
    event MPFeesCollected(bytes32 indexed claimOp, bool indexed isWrap, uint256 gross, uint256 saleAmount);
    event BidFunded(
        uint256 indexed id, address indexed buyer, uint256 feeAmount, uint256 stableAmount, uint256 deadline
    );
    event BidFilled(uint256 indexed id, address indexed buyer, uint256 feeAmount, uint256 stableAmount);
    event BidCancelled(uint256 indexed id, address indexed buyer, uint256 stableAmount);

    constructor(address source_, address feeToken_, address stablecoin_, address treasury_, uint256 sellBps_) {
        if (
            source_.code.length == 0 || feeToken_.code.length == 0 || stablecoin_.code.length == 0
                || feeToken_ == stablecoin_ || treasury_ == address(0) || treasury_ == address(this) || sellBps_ > BPS
        ) revert InvalidConfiguration();

        source = source_;
        feeToken = IERC20(feeToken_);
        stablecoin = IERC20(stablecoin_);
        treasury = treasury_;
        sellBps = sellBps_;

        if (source_ == treasury_) {
            IFundedFeeSaleDAO dao = IFundedFeeSaleDAO(source_);
            authorization = dao.authorization();
            validator = dao.validator();
            if (
                dao.shareToken() != feeToken_ || !dao.approvedToken(stablecoin_) || address(validator).code.length == 0
                    || validator.getFrostMultisig() != address(authorization)
            ) revert InvalidConfiguration();
        } else {
            authorization = FROSTAuthorization(IFundedFeeSalePool(source_).getFrostMultisig());
            validator = CustodianValidator(address(0));
        }
        if (address(authorization).code.length == 0) revert InvalidConfiguration();
    }

    function getSetSellBpsOperationHash(uint256 bps) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.setSellBps.selector, keccak256(abi.encode(bps)), operationNonce
        );
    }

    /// @notice Changes the portion captured from future fees. Inventory already captured is unchanged.
    function setSellBps(uint256 bps, bytes32 op) external nonReentrant {
        if (bps > BPS) revert InvalidAmount();
        _authorize(op, getSetSellBpsOperationHash(bps));
        emit SellBpsSet(sellBps, bps);
        sellBps = bps;
    }

    /// @notice Credits earmarked S already transferred by the bound DAO.
    function recordFee(uint256 amount) external nonReentrant {
        if (source != treasury || msg.sender != source) revert InvalidSource();
        if (amount == 0) revert InvalidAmount();
        inventory += amount;
        if (feeToken.balanceOf(address(this)) < inventory) revert InvalidTransfer();
        emit FeeRecorded(amount);
    }

    function getMPClaimOperationHash(bool isWrap, uint256 gross, uint256 claimNonce) public view returns (bytes32) {
        return authorization.hashOperation(
            source,
            IFundedFeeSalePool.claimFees.selector,
            keccak256(abi.encode(address(feeToken), isWrap, address(this), gross)),
            claimNonce
        );
    }

    function getCollectMPFeesOperationHash(bool isWrap, uint256 gross, uint256 claimNonce)
        public
        view
        returns (bytes32)
    {
        return authorization.hashOperation(
            address(this),
            this.collectMPFees.selector,
            keccak256(abi.encode(isWrap, gross, claimNonce, sellBps)),
            operationNonce
        );
    }

    /// @notice Captures one exact federation pool fee claim and forwards unsold MP to the fixed treasury.
    function collectMPFees(bool isWrap, uint256 gross, uint256 claimNonce, bytes32 op) external nonReentrant {
        if (source == treasury) revert InvalidSource();
        if (gross == 0) revert InvalidAmount();
        bytes32 claimOp = getMPClaimOperationHash(isWrap, gross, claimNonce);
        if (processedMPClaims[claimOp]) revert InvalidClaim();
        _authorize(op, getCollectMPFeesOperationHash(isWrap, gross, claimNonce));
        processedMPClaims[claimOp] = true;

        if (authorization.executedOperations(claimOp)) {
            // A relayer may have paid the identical pool claim to this sale already.
            if (authorization.operationTargets(claimOp) != source) revert InvalidClaim();
        } else {
            uint256 beforeBalance = feeToken.balanceOf(address(this));
            IFundedFeeSalePool(source).claimFees(address(feeToken), isWrap, address(this), gross, claimOp);
            if (feeToken.balanceOf(address(this)) != beforeBalance + gross) revert InvalidTransfer();
        }
        if (feeToken.balanceOf(address(this)) < inventory + gross) revert InvalidTransfer();

        uint256 saleAmount = Math.mulDiv(gross, sellBps, BPS);
        inventory += saleAmount;
        uint256 retained = gross - saleAmount;
        if (retained != 0) _sendExact(feeToken, treasury, retained);
        emit MPFeesCollected(claimOp, isWrap, gross, saleAmount);
    }

    /// @notice Funds a bid before approval and independently of currently available inventory.
    function fundBid(uint256 feeAmount, uint256 stableAmount, uint256 deadline)
        external
        nonReentrant
        returns (uint256 id)
    {
        if (feeAmount == 0 || stableAmount == 0 || deadline <= block.timestamp) revert InvalidBid();
        uint256 beforeSale = stablecoin.balanceOf(address(this));
        uint256 beforeBuyer = stablecoin.balanceOf(msg.sender);
        if (beforeSale < stableEscrow || beforeBuyer < stableAmount) revert InvalidTransfer();
        stablecoin.safeTransferFrom(msg.sender, address(this), stableAmount);
        if (
            stablecoin.balanceOf(address(this)) != beforeSale + stableAmount
                || stablecoin.balanceOf(msg.sender) != beforeBuyer - stableAmount
        ) revert InvalidTransfer();

        id = ++bidCount;
        stableEscrow += stableAmount;
        bids[id] = Bid(msg.sender, feeAmount, stableAmount, deadline);
        emit BidFunded(id, msg.sender, feeAmount, stableAmount, deadline);
    }

    function getFillBidOperationHash(uint256 id) public view returns (bytes32) {
        Bid memory bid = bids[id];
        if (bid.buyer == address(0)) revert InvalidBid();
        return authorization.hashOperation(
            address(this),
            this.fillBid.selector,
            keccak256(abi.encode(id, bid.buyer, bid.feeAmount, bid.stableAmount, bid.deadline)),
            operationNonce
        );
    }

    function fillBid(uint256 id, bytes32 op) external nonReentrant {
        Bid memory bid = bids[id];
        if (bid.buyer == address(0) || block.timestamp > bid.deadline) revert InvalidBid();
        if (bid.feeAmount > inventory) revert InsufficientInventory();
        if (feeToken.balanceOf(address(this)) < inventory) revert InvalidTransfer();
        _authorize(op, getFillBidOperationHash(id));
        _closeBid(id, bid.stableAmount);
        inventory -= bid.feeAmount;

        _sendExact(feeToken, bid.buyer, bid.feeAmount);
        _sendExact(stablecoin, treasury, bid.stableAmount);
        if (source == treasury) IFundedFeeSaleDAO(source).recordFeeSaleProceeds(bid.stableAmount);
        emit BidFilled(id, bid.buyer, bid.feeAmount, bid.stableAmount);
    }

    /// @notice A buyer may recover a bid's stablecoins after its inclusive fill deadline.
    function cancelBid(uint256 id) external nonReentrant {
        Bid memory bid = bids[id];
        if (bid.buyer != msg.sender || block.timestamp <= bid.deadline) revert InvalidBid();
        _closeBid(id, bid.stableAmount);
        _sendExact(stablecoin, bid.buyer, bid.stableAmount);
        emit BidCancelled(id, bid.buyer, bid.stableAmount);
    }

    function _closeBid(uint256 id, uint256 amount) private {
        if (stablecoin.balanceOf(address(this)) < stableEscrow) revert InvalidTransfer();
        stableEscrow -= amount;
        delete bids[id];
    }

    function _authorize(bytes32 op, bytes32 expected) private {
        if (op != expected) revert InvalidAuthorization();
        if (address(validator) != address(0)) {
            uint256 count = validator.getCustodianCount();
            if (count == 0 || validator.custodianSignatureCount(op) < (2 * count + 2) / 3) {
                revert InvalidAuthorization();
            }
        }
        authorization.consumeOperation(op);
        ++operationNonce;
    }

    function _sendExact(IERC20 token, address recipient, uint256 amount) private {
        uint256 beforeSale = token.balanceOf(address(this));
        uint256 beforeRecipient = token.balanceOf(recipient);
        if (beforeSale < amount) revert InvalidTransfer();
        token.safeTransfer(recipient, amount);
        if (
            token.balanceOf(address(this)) != beforeSale - amount
                || token.balanceOf(recipient) != beforeRecipient + amount
        ) revert InvalidTransfer();
    }
}
