// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC721Errors} from "@openzeppelin/contracts/interfaces/draft-IERC6093.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ConfederationShareV2} from "../tokens/ConfederationShareV2.sol";
import {FederationNFTModifier} from "../tokens/FederationNFTModifier.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {CustodianValidator} from "./CustodianValidator.sol";
import {ConfederationDAOIncome} from "./ConfederationDAOIncome.sol";
import {BitcoinWithdrawalEscrowV2} from "./BitcoinWithdrawalEscrowV2.sol";
import {BitcoinDepositRegistry} from "./BitcoinDepositRegistry.sol";
import {IFeeManager} from "./IFeeManager.sol";

interface IConfederationBitcoinReserve {
    function confederationPool() external view returns (address);
    function deposits() external view returns (BitcoinDepositRegistry);
    function escrow() external view returns (address);
    function payoutScript() external view returns (bytes memory);
}

interface IConfederationPassManager {
    function nftModifier() external view returns (address);
}

/// @notice Issues and redeems the confederation's S against its own proven Bitcoin reserve.
contract ConfederationPoolV2 is ReentrancyGuard {
    uint256 public constant WITHDRAWAL_CANCEL_DELAY = 7 days;
    uint256 private constant SAT = 1e10;
    FROSTAuthorization public immutable frostMultisig;
    CustodianValidator public immutable custodianValidator;
    ConfederationShareV2 public immutable shareToken;
    string public confederationId;
    IFeeManager public feeManager;
    ConfederationDAOIncome public daoIncome;
    BitcoinWithdrawalEscrowV2 public withdrawalEscrow;
    IConfederationBitcoinReserve public bitcoinReserve;
    uint256 public totalPrincipal;
    uint256 public reservedS;
    uint256 public operationNonce;
    uint256 public withdrawalCounter;
    mapping(address => uint256) public pendingShares;
    mapping(address => uint256) public wrapPass;
    mapping(address => bool) public wrapPassSelected;
    struct WithdrawalRequest {
        address requester;
        address token;
        uint256 shareAmount;
        uint256 principalAmount;
        bytes btcScript;
        bytes metadata;
        bool isProcessed;
        uint256 timestamp;
        bytes32 operationHash;
    }
    mapping(uint256 => WithdrawalRequest) public withdrawals;
    mapping(uint256 => bool) public withdrawalCancelled;
    mapping(uint256 => bool) public withdrawalPrepared;
    mapping(uint256 => uint64) public minerFeeLimitSats;
    event DAOIncomeBound(address indexed income, bytes32 indexed operationHash);
    event BitcoinReserveBound(address indexed reserve);
    event Deposited(address indexed user, uint256 grossAmount, uint256 shareAmount);
    event WithdrawalRequested(uint256 indexed withdrawalId, address indexed user, uint256 shareAmount, bytes32 operationHash);
    event WithdrawalPrepared(uint256 indexed withdrawalId);
    event WithdrawalProcessed(uint256 indexed withdrawalId, bytes32 operationHash);
    event WithdrawalCancelled(uint256 indexed withdrawalId, address indexed requester, bool thresholdApproved);
    event FeeManagerSet(address indexed feeManager, bytes32 indexed operationHash);
    error InvalidBinding();
    error ZeroAmount();
    error InsufficientReserve();
    error InvalidWithdrawal();

    constructor(string memory id, address auth, address validator, address shares) {
        require(bytes(id).length > 0 && auth.code.length > 0 && validator.code.length > 0, "pool config");
        confederationId = id;
        frostMultisig = FROSTAuthorization(auth);
        custodianValidator = CustodianValidator(validator);
        shareToken = ConfederationShareV2(shares);
        require(shareToken.confederationPool() == address(this) && custodianValidator.frostMultisig() == auth, "pool binding");
    }

    function _hash(bytes4 method, bytes32 args) private view returns (bytes32) {
        return frostMultisig.hashOperation(address(this), method, args, operationNonce);
    }
    function _authorize(bytes32 op, bytes32 expected) private {
        require(op == expected && custodianValidator.meetsMinimumCustodians(op), "pool approval");
        frostMultisig.consumeOperation(op);
        ++operationNonce;
    }
    function getBindDAOIncomeOperationHash(address income) public view returns (bytes32) {
        return _hash(this.bindDAOIncome.selector, keccak256(abi.encode(income)));
    }
    function bindDAOIncome(address income, bytes32 op) external {
        ConfederationDAOIncome node = ConfederationDAOIncome(income);
        if (address(daoIncome) != address(0) || node.pool() != address(this) || node.shareToken() != address(shareToken)
            || address(node.authorization()) != address(frostMultisig) || address(node.validator()) != address(custodianValidator)) {
            revert InvalidBinding();
        }
        _authorize(op, getBindDAOIncomeOperationHash(income));
        daoIncome = node;
        emit DAOIncomeBound(income, op);
    }
    function getSetWithdrawalEscrowOperationHash(address escrow) public view returns (bytes32) {
        return _hash(this.setWithdrawalEscrow.selector, keccak256(abi.encode(escrow)));
    }
    function setWithdrawalEscrow(address escrow, bytes32 op) external {
        require(address(withdrawalEscrow) == address(0) && escrow.code.length > 0, "escrow config");
        require(address(BitcoinWithdrawalEscrowV2(escrow).batches().authorization()) == address(frostMultisig), "escrow auth");
        _authorize(op, getSetWithdrawalEscrowOperationHash(escrow));
        withdrawalEscrow = BitcoinWithdrawalEscrowV2(escrow);
    }
    function getBindBitcoinReserveOperationHash(address reserve) public view returns (bytes32) {
        return _hash(this.bindBitcoinReserve.selector, keccak256(abi.encode(reserve)));
    }
    function bindBitcoinReserve(address reserve, bytes32 op) external {
        IConfederationBitcoinReserve node = IConfederationBitcoinReserve(reserve);
        if (address(bitcoinReserve) != address(0) || node.confederationPool() != address(this)
            || node.escrow() != address(withdrawalEscrow) || address(node.deposits()) != address(withdrawalEscrow.depositRegistry())
            || !withdrawalEscrow.localCustodyPools(address(this))
            || node.deposits().poolScripts(reserve) != keccak256(node.payoutScript())) revert InvalidBinding();
        _authorize(op, getBindBitcoinReserveOperationHash(reserve));
        bitcoinReserve = node;
        emit BitcoinReserveBound(reserve);
    }
    function getSetFeeManagerOperationHash(address manager) public view returns (bytes32) {
        return _hash(this.setFeeManager.selector, keccak256(abi.encode(manager)));
    }
    function setFeeManager(address manager, bytes32 op) external {
        require(address(feeManager) == address(0), "fee manager fixed");
        (address auth, address validator) = IFeeManager(manager).getFeeSecurityConfig();
        if (auth != address(frostMultisig) || validator != address(custodianValidator)
            || !IFeeManager(manager).authorizedFeeCollectors(address(this))) revert InvalidBinding();
        _authorize(op, getSetFeeManagerOperationHash(manager));
        feeManager = IFeeManager(manager);
        emit FeeManagerSet(manager, op);
    }

    /// @notice The reserve calls only after recording a new proven Bitcoin output.
    function mintFromReserve(address recipient, uint256 amount, bool selfWrap) external nonReentrant returns (uint256 net) {
        require(msg.sender == address(bitcoinReserve) && msg.sender != address(0), "Bitcoin reserve only");
        require(address(daoIncome) != address(0) && recipient != address(0), "issuance config");
        uint256 fee = (selfWrap && wrapPassSelected[recipient] ? _feeWithPass(true, amount, wrapPass[recipient], recipient)
            : address(feeManager) == address(0) ? 0 : feeManager.calculateFee(true, amount));
        if (fee >= amount) revert ZeroAmount();
        uint256 next = totalPrincipal + amount;
        if (next > bitcoinReserve.deposits().totalBackingSats() * SAT) revert InsufficientReserve();
        totalPrincipal = next;
        net = amount - fee;
        shareToken.mint(recipient, net);
        if (fee != 0) {
            shareToken.mint(address(daoIncome), fee);
            daoIncome.recordShareIncome(ConfederationDAOIncome.Stream.Wrap, fee);
        }
        emit Deposited(recipient, amount, net);
    }

    function requestWithdrawal(uint256 shares, bytes calldata script, uint64 feeLimit) external nonReentrant returns (uint256) {
        uint256 fee = address(feeManager) == address(0) ? 0 : feeManager.calculateFee(false, shares);
        return _request(shares, fee, script, feeLimit);
    }
    function requestWithdrawalWithNFT(uint256 shares, bytes calldata script, uint256 nftId, uint64 feeLimit)
        external nonReentrant returns (uint256)
    {
        return _request(shares, _feeWithPass(false, shares, nftId, msg.sender), script, feeLimit);
    }
    function setWrapPass(uint256 nftId) external {
        address modifier_ = IConfederationPassManager(address(feeManager)).nftModifier();
        require(FederationNFTModifier(modifier_).ownerOf(nftId) == msg.sender, "pass owner");
        wrapPass[msg.sender] = nftId;
        wrapPassSelected[msg.sender] = true;
    }
    function clearWrapPass() external {
        delete wrapPass[msg.sender];
        delete wrapPassSelected[msg.sender];
    }
    function _feeWithPass(bool wrap, uint256 amount, uint256 nftId, address owner) private returns (uint256 fee) {
        try feeManager.calculateFeeWithNFT(wrap, amount, nftId, owner) returns (uint256 quoted) {
            return quoted;
        } catch (bytes memory reason) {
            bytes4 selector = bytes4(reason);
            if (selector == FederationNFTModifier.NFTExpiredError.selector
                || selector == FederationNFTModifier.VolumeLimitExceeded.selector
                || (wrap && (selector == FederationNFTModifier.NotTokenOwner.selector
                    || selector == FederationNFTModifier.NFTNotActive.selector
                    || selector == IERC721Errors.ERC721NonexistentToken.selector))) {
                return feeManager.calculateFee(wrap, amount);
            }
            assembly ("memory-safe") { revert(add(reason, 32), mload(reason)) }
        }
    }
    function _request(uint256 shares, uint256 fee, bytes calldata script, uint64 feeLimit) private returns (uint256 id) {
        if (address(bitcoinReserve) == address(0) || address(daoIncome) == address(0)) revert InvalidBinding();
        if (fee >= shares) revert ZeroAmount();
        uint256 principal = (shares - fee) / SAT * SAT;
        if (principal == 0) revert ZeroAmount();
        if (principal > totalPrincipal - reservedS) revert InsufficientReserve();
        id = withdrawalCounter++;
        bytes memory metadata = abi.encode(feeLimit);
        WithdrawalRequest memory w = WithdrawalRequest(msg.sender, address(shareToken), shares, principal, script,
            metadata, false, block.timestamp, bytes32(0));
        w.operationHash = _withdrawalHash(id, w);
        withdrawals[id] = w;
        minerFeeLimitSats[id] = feeLimit;
        pendingShares[msg.sender] += shares;
        reservedS += principal;
        shareToken.escrow(msg.sender, address(this), shares);
        shareToken.approve(address(withdrawalEscrow), principal);
        withdrawalEscrow.lock(id, address(shareToken), msg.sender, principal, script);
        shareToken.approve(address(withdrawalEscrow), 0);
        emit WithdrawalRequested(id, msg.sender, shares, w.operationHash);
    }
    function _withdrawalHash(uint256 id, WithdrawalRequest memory w) private view returns (bytes32) {
        return frostMultisig.hashOperation(address(this), this.processWithdrawal.selector,
            keccak256(abi.encode(id, w.requester, w.token, w.shareAmount, w.principalAmount, w.btcScript, w.metadata, w.timestamp)), id);
    }
    function getProcessWithdrawalOperationHash(uint256 id) public view returns (bytes32) {
        return _withdrawalHash(id, withdrawals[id]);
    }
    function getRejectWithdrawalOperationHash(uint256 id) public view returns (bytes32) {
        WithdrawalRequest memory w = withdrawals[id];
        return frostMultisig.hashOperation(address(this), this.rejectWithdrawal.selector,
            keccak256(abi.encode(id, w.requester, w.token, w.shareAmount, w.principalAmount, w.btcScript, w.metadata, w.timestamp)), id);
    }
    function processWithdrawal(uint256 id) external nonReentrant {
        WithdrawalRequest storage w = withdrawals[id];
        if (w.shareAmount == 0 || w.isProcessed || withdrawalCancelled[id] || withdrawalPrepared[id]) revert InvalidWithdrawal();
        require(frostMultisig.isOperationApproved(w.operationHash, address(this))
            && custodianValidator.meetsMinimumCustodians(w.operationHash), "withdrawal approval");
        frostMultisig.consumeOperation(w.operationHash);
        withdrawalPrepared[id] = true;
        emit WithdrawalPrepared(id);
    }
    function settleWithdrawal(uint256 id) external nonReentrant {
        require(msg.sender == address(withdrawalEscrow), "escrow only");
        WithdrawalRequest storage w = withdrawals[id];
        if (!withdrawalPrepared[id] || w.isProcessed || withdrawalCancelled[id]) revert InvalidWithdrawal();
        w.isProcessed = true;
        reservedS -= w.principalAmount;
        totalPrincipal -= w.principalAmount;
        pendingShares[w.requester] -= w.shareAmount;
        uint256 fee = w.shareAmount - w.principalAmount;
        if (fee != 0) {
            shareToken.escrow(address(this), address(daoIncome), fee);
            daoIncome.recordShareIncome(ConfederationDAOIncome.Stream.Unwrap, fee);
        }
        emit WithdrawalProcessed(id, w.operationHash);
    }
    function cancelWithdrawal(uint256 id) external nonReentrant {
        WithdrawalRequest storage w = withdrawals[id];
        require(w.requester == msg.sender && block.timestamp >= w.timestamp + WITHDRAWAL_CANCEL_DELAY, "cancel owner/delay");
        _cancel(id, false);
    }
    function rejectWithdrawal(uint256 id) external nonReentrant {
        bytes32 op = getRejectWithdrawalOperationHash(id);
        require(frostMultisig.isOperationApproved(op, address(this)) && custodianValidator.meetsMinimumCustodians(op), "rejection approval");
        frostMultisig.consumeOperation(op);
        _cancel(id, true);
    }
    function _cancel(uint256 id, bool approved) private {
        WithdrawalRequest storage w = withdrawals[id];
        if (w.shareAmount == 0 || w.isProcessed || withdrawalCancelled[id]) revert InvalidWithdrawal();
        withdrawalEscrow.refund(withdrawalEscrow.requestKey(address(this), id));
        reservedS -= w.principalAmount;
        pendingShares[w.requester] -= w.shareAmount;
        withdrawalCancelled[id] = true;
        shareToken.escrow(address(this), w.requester, w.shareAmount);
        emit WithdrawalCancelled(id, w.requester, approved);
    }

    function getWithdrawal(uint256 id) external view returns (WithdrawalRequest memory) { return withdrawals[id]; }
    function getTotalValueLocked() external view returns (uint256) { return totalPrincipal; }
    function getFrostMultisig() external view returns (address) { return address(frostMultisig); }
    function getCustodianValidator() external view returns (address) { return address(custodianValidator); }
    function getShareToken() external view returns (address) { return address(shareToken); }
    function getPoolCount() external view returns (uint256) { return address(bitcoinReserve) == address(0) ? 0 : 1; }
    // ABI compatibility for the common callback registry; the only asset is S.
    function pendingUserPoolShares(address owner, address token) external view returns (uint256) {
        return token == address(shareToken) ? pendingShares[owner] : 0;
    }
    function escrowedMP(address token) external view returns (uint256) { return token == address(shareToken) ? reservedS : 0; }
}
