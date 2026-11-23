// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "../tokens/ConfederationToken.sol";
import "../FROSTAuthorization.sol";
import "./CustodianValidator.sol";
import "./IFeeManager.sol";

import "../federation/BitcoinWithdrawalEscrow.sol";

/**
 * @title ConfederationMultiPool
 * @notice Multi-pool DAO for confederations that requires both FROST and custodian signatures
 * @dev Extends SimpleMultiPool concept with mandatory custodian approval for withdrawals.
 *      This provides an additional security layer for institutional-grade operations.
 */
contract ConfederationMultiPool is ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 public constant WITHDRAWAL_CANCEL_DELAY = 7 days;
    uint256 public constant FEE_REPORT_GAS_LIMIT = 100_000;
    uint256 private constant SAT = 1e10;

    // ======== STATE ========

    /// @notice FROST multisig for managing withdrawals
    FROSTAuthorization public immutable frostMultisig;

    /// @notice Custodian validator for additional signature requirements
    CustodianValidator public immutable custodianValidator;

    /// @notice Share token for this MultiPool
    ConfederationToken public immutable shareToken;

    /// @notice MultiPool identifier
    string public confederationId;

    /// @notice Optional fee manager for fee calculations
    IFeeManager public feeManager;

    /// @notice Registered pools and their MP tokens
    struct PoolInfo {
        address mpToken;
        string poolId;
        bool isActive;
        bool exists;
        uint256 totalDeposited;
    }

    mapping(address => PoolInfo) public pools;
    address[] public poolList;

    /// @notice User balances per pool (for tracking)
    mapping(address => mapping(address => uint256)) public userPoolBalances;
    mapping(address => mapping(address => uint256)) public pendingUserPoolShares;

    /// @notice Withdrawal requests
    struct WithdrawalRequest {
        address requester;
        address mpToken;
        uint256 shareAmount;
        uint256 mpAmount;
        bytes btcScript;
        bytes metadata; // Optional metadata for OP_RETURN (Сценарий З)
        bool isProcessed;
        uint256 timestamp;
        bytes32 operationHash; // Hash that needs both FROST and custodian signatures
    }

    mapping(uint256 => WithdrawalRequest) public withdrawals;
    mapping(uint256 => bool) public withdrawalCancelled;
    uint256 public withdrawalCounter;
    uint256 public operationNonce;
    mapping(address => mapping(bool => uint256)) public accruedFees;
    /// @notice Accrued fees whose manager notification has not succeeded yet.
    mapping(address => mapping(bool => uint256)) public pendingUnreportedFees;

    BitcoinWithdrawalEscrow public withdrawalEscrow;
    function _requireLocalToken(address token) private view {
        if (address(withdrawalEscrow) != address(0)) withdrawalEscrow.checkDepositToken(address(this), token);
    }
    mapping(uint256 => bool) public withdrawalPrepared;
    mapping(address=>uint256) public escrowedMP;
    event WithdrawalPrepared(uint256 indexed withdrawalId);

    function getSetWithdrawalEscrowOperationHash(address escrow) public view returns(bytes32) {
        return frostMultisig.hashOperation(address(this),this.setWithdrawalEscrow.selector,keccak256(abi.encode(escrow)),operationNonce);
    }
    function setWithdrawalEscrow(address escrow,bytes32 op) external {
        require(address(withdrawalEscrow)==address(0) && escrow.code.length>0,"escrow config");
        require(address(BitcoinWithdrawalEscrow(escrow).batches().authorization())==address(frostMultisig),"escrow auth");
        require(op==getSetWithdrawalEscrowOperationHash(escrow),"operation hash");
        require(custodianValidator.meetsMinimumCustodians(op),"custodians");
        frostMultisig.consumeOperation(op); operationNonce++;
        withdrawalEscrow=BitcoinWithdrawalEscrow(escrow);
    }
    function _escrowWithdrawal(uint256 id) private {
        require(address(withdrawalEscrow)!=address(0),"escrow not configured");
        WithdrawalRequest storage w=withdrawals[id];
        shareToken.escrow(w.requester,address(this),w.shareAmount);
        escrowedMP[w.mpToken]+=w.mpAmount;
        IERC20(w.mpToken).forceApprove(address(withdrawalEscrow),w.mpAmount);
        withdrawalEscrow.lock(id,w.mpToken,w.requester,w.mpAmount,w.btcScript);
        IERC20(w.mpToken).forceApprove(address(withdrawalEscrow),0);
    }
    function settleWithdrawal(uint256 id) external nonReentrant {
        require(msg.sender==address(withdrawalEscrow),"escrow only");
        WithdrawalRequest storage w=withdrawals[id];
        require(withdrawalPrepared[id] && !w.isProcessed && !withdrawalCancelled[id],"withdrawal state");
        w.isProcessed=true;
        escrowedMP[w.mpToken]-=w.mpAmount;
        pendingUserPoolShares[w.requester][w.mpToken]-=w.shareAmount;
        userPoolBalances[w.requester][w.mpToken]-=w.shareAmount;
        shareToken.burn(address(this),w.shareAmount);
        _accrueFee(w.mpToken,w.shareAmount-w.mpAmount,false);
        pools[w.mpToken].totalDeposited-=w.mpAmount;
        emit WithdrawalProcessed(id,w.operationHash);
    }

    // ======== EVENTS ========

    event PoolAdded(address indexed mpToken, string poolId);
    event PoolDeactivated(address indexed mpToken);
    event Deposited(address indexed user, address indexed mpToken, uint256 mpAmount, uint256 shareAmount);
    event WithdrawalRequested(
        uint256 indexed withdrawalId,
        address indexed user,
        address indexed mpToken,
        uint256 shareAmount,
        bytes32 operationHash
    );
    event WithdrawalRequestedWithMetadata(
        uint256 indexed withdrawalId,
        address indexed user,
        address indexed mpToken,
        uint256 shareAmount,
        bytes32 operationHash,
        bytes metadata
    );
    event WithdrawalProcessed(uint256 indexed withdrawalId, bytes32 operationHash);
    event WithdrawalCancelled(uint256 indexed withdrawalId, address indexed requester, bool thresholdApproved);
    event FeeManagerSet(address indexed feeManager, bytes32 indexed operationHash);
    event FeeReportFailed(address indexed mpToken, bool indexed isWrap, uint256 amount);
    event FeesReported(address indexed mpToken, bool indexed isWrap, uint256 amount);

    event FeesClaimed(address indexed mpToken, bool isWrap, address indexed recipient, uint256 amount);

    // ======== ERRORS ========

    error EmptyConfederationId();
    error InvalidMpToken();
    error PoolAlreadyExists();
    error PoolNotActive();
    error ZeroAmount();
    error InsufficientShares();
    error InvalidPool();
    error AlreadyProcessed();
    error InvalidWithdrawal();
    error InsufficientFROSTSignatures();
    error InsufficientCustodianSignatures();
    error FeeManagerAlreadySet();
    error InvalidFeeManager();
    error InsufficientPoolShares();
    error WithdrawalCancelledError();
    error OnlyRequester();
    error CancelDelayNotReached();
    error InexactTransfer();
    error InvalidWithdrawalAuthorities();
    error InvalidShareTokenBinding();

    // ======== CONSTRUCTOR ========

    /**
     * @notice Create a confederation multi-pool
     * @param _confederationId Unique confederation identifier
     * @param _frostMultisig Address of the FROST multisig contract
     * @param _custodianValidator Address of the custodian validator contract
     * @param _shareToken Share token deployed for this pool's predicted address
     */
    constructor(
        string memory _confederationId,
        address _frostMultisig,
        address _custodianValidator,
        address _shareToken
    ) {
        if (bytes(_confederationId).length == 0) revert EmptyConfederationId();
        if (_frostMultisig == address(0) || _custodianValidator == address(0)) revert InvalidWithdrawalAuthorities();

        confederationId = _confederationId;
        frostMultisig = FROSTAuthorization(_frostMultisig);
        custodianValidator = CustodianValidator(_custodianValidator);

        shareToken = ConfederationToken(_shareToken);
        if (shareToken.confederationPool() != address(this)) revert InvalidShareTokenBinding();
    }

    // ======== FEE MANAGER SETUP ========

    /**
     * @notice Set the fee manager (callable once during setup)
     * @param _feeManager Address of the fee manager contract
     * @param operationHash Operation hash signed by FROST and custodians
     */
    function setFeeManager(address _feeManager, bytes32 operationHash) external {
        if (address(feeManager) != address(0)) revert FeeManagerAlreadySet();
        if (_feeManager == address(0)) revert InvalidFeeManager();
        require(operationHash == getSetFeeManagerOperationHash(_feeManager), "Operation hash mismatch");

        if (!frostMultisig.isOperationApproved(operationHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }
        if (!custodianValidator.meetsMinimumCustodians(operationHash)) {
            revert InsufficientCustodianSignatures();
        }

        (address authorization, address validator) = IFeeManager(_feeManager).getFeeSecurityConfig();
        if (authorization != address(frostMultisig) || validator != address(custodianValidator)) {
            revert InvalidFeeManager();
        }
        require(IFeeManager(_feeManager).authorizedFeeCollectors(address(this)), "Fee collector not authorized");

        frostMultisig.consumeOperation(operationHash);
        operationNonce++;

        feeManager = IFeeManager(_feeManager);

        emit FeeManagerSet(_feeManager, operationHash);
    }

    // ======== POOL MANAGEMENT ========

    /**
     * @notice Add a pool to the MultiPool (requires FROST + custodian threshold)
     * @param mpToken Address of the pool's MP token
     * @param poolId Pool identifier
     * @param operationHash Operation hash signed by FROST and custodians (from governance proposal)
     */
    function addPool(address mpToken, string calldata poolId, bytes32 operationHash) external {
        if (mpToken == address(0)) revert InvalidMpToken();
        if (pools[mpToken].isActive) revert PoolAlreadyExists();
        _requireLocalToken(mpToken);
        require(operationHash == getAddPoolOperationHash(mpToken, poolId), "Operation hash mismatch");

        // Verify FROST threshold approval
        if (!frostMultisig.isOperationApproved(operationHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }

        // Verify custodian threshold approval
        if (!custodianValidator.meetsMinimumCustodians(operationHash)) {
            revert InsufficientCustodianSignatures();
        }

        // Mark as executed to prevent replay
        frostMultisig.consumeOperation(operationHash);
        operationNonce++;

        PoolInfo storage pool = pools[mpToken];
        if (pool.exists) {
            pool.poolId = poolId;
            pool.isActive = true;
        } else {
            pools[mpToken] =
                PoolInfo({mpToken: mpToken, poolId: poolId, isActive: true, exists: true, totalDeposited: 0});
            poolList.push(mpToken);
        }

        emit PoolAdded(mpToken, poolId);
    }

    /**
     * @notice Deactivate a pool (requires FROST + custodian threshold)
     * @param mpToken Address of the pool's MP token
     * @param operationHash Operation hash signed by FROST and custodians (from governance proposal)
     */
    function deactivatePool(address mpToken, bytes32 operationHash) external {
        if (!pools[mpToken].isActive) revert PoolNotActive();
        require(operationHash == getDeactivatePoolOperationHash(mpToken), "Operation hash mismatch");

        // Verify FROST threshold approval
        if (!frostMultisig.isOperationApproved(operationHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }

        // Verify custodian threshold approval
        if (!custodianValidator.meetsMinimumCustodians(operationHash)) {
            revert InsufficientCustodianSignatures();
        }

        // Mark as executed to prevent replay
        frostMultisig.consumeOperation(operationHash);
        operationNonce++;

        pools[mpToken].isActive = false;

        emit PoolDeactivated(mpToken);
    }

    // ======== DEPOSIT (MP -> S) ========

    /**
     * @notice Deposit MP tokens and receive S-tokens
     * @param mpToken Address of the MP token to deposit
     * @param mpAmount Amount of MP tokens to deposit
     * @return shareAmount Amount of S-tokens minted
     */
    function deposit(address mpToken, uint256 mpAmount) external returns (uint256 shareAmount) {
        return depositFor(mpToken, mpAmount, msg.sender);
    }

    /// @notice The payer may give locally backed shares to a fixed recipient.
    function depositFor(address mpToken, uint256 mpAmount, address recipient)
        public nonReentrant returns (uint256 shareAmount)
    {
        if (!pools[mpToken].isActive) revert PoolNotActive();
        if (mpAmount == 0) revert ZeroAmount();
        _requireLocalToken(mpToken);

        // Transfer MP tokens from user to MultiPool
        uint256 balanceBefore = IERC20(mpToken).balanceOf(address(this));
        IERC20(mpToken).safeTransferFrom(msg.sender, address(this), mpAmount);
        if (IERC20(mpToken).balanceOf(address(this)) != balanceBefore + mpAmount) revert InexactTransfer();

        // Calculate share amount (1:1 ratio for simplicity), apply fee if set
        shareAmount = mpAmount;
        if (address(feeManager) != address(0)) {
            uint256 fee = feeManager.calculateFee(true, mpAmount);
            shareAmount = mpAmount - fee;
            _accrueFee(mpToken, fee, true);
        }

        // Mint S-tokens to user
        shareToken.mint(recipient, shareAmount);

        // Update tracking
        pools[mpToken].totalDeposited += mpAmount;
        userPoolBalances[recipient][mpToken] += shareAmount;

        emit Deposited(recipient, mpToken, mpAmount, shareAmount);
    }

    // ======== WITHDRAWAL (S -> BTC) ========

    /**
     * @notice Request withdrawal - lock S-tokens, create request for FROST + custodian approval
     * @param mpToken Which pool's MP tokens to redeem from
     * @param shareAmount Amount of S-tokens to lock
     * @param btcScript Bitcoin output script for withdrawal
     * @return withdrawalId Withdrawal request ID
     */
    function requestWithdrawal(address mpToken, uint256 shareAmount, bytes calldata btcScript)
        external
        nonReentrant
        returns (uint256 withdrawalId)
    {
        return _requestWithdrawal(mpToken, shareAmount, btcScript, "");
    }

    /**
     * @notice Request withdrawal with metadata for OP_RETURN (Сценарий З)
     * @param mpToken Which pool's MP tokens to redeem from
     * @param shareAmount Amount of S-tokens to lock
     * @param btcScript Bitcoin output script for withdrawal
     * @param metadata Metadata to include in OP_RETURN (e.g., "WITHDRAWAL-REF:CONF1-2025-001")
     * @return withdrawalId Withdrawal request ID
     */
    function requestWithdrawalWithMetadata(
        address mpToken,
        uint256 shareAmount,
        bytes calldata btcScript,
        bytes calldata metadata
    ) external nonReentrant returns (uint256 withdrawalId) {
        return _requestWithdrawal(mpToken, shareAmount, btcScript, metadata);
    }

    /**
     * @notice Request withdrawal with NFT discount
     * @param mpToken Which pool's MP tokens to redeem from
     * @param shareAmount Amount of S-tokens to lock
     * @param btcScript Bitcoin output script for withdrawal
     * @return withdrawalId Withdrawal request ID
     */
    function requestWithdrawalWithNFT(address mpToken, uint256 shareAmount, bytes calldata btcScript, uint256 nftTokenId)
        external
        nonReentrant
        returns (uint256 withdrawalId)
    {
        if (!(pools[mpToken].isActive || pools[mpToken].totalDeposited > 0)) revert InvalidPool();
        if (shareAmount == 0) revert ZeroAmount();
        require(btcScript.length > 0 && btcScript.length <= 10000, "Invalid BTC script");
        if (shareToken.balanceOf(msg.sender) < shareAmount) revert InsufficientShares();
        if (getAvailableUserPoolBalance(msg.sender, mpToken) < shareAmount) revert InsufficientPoolShares();
        pendingUserPoolShares[msg.sender][mpToken] += shareAmount;

        uint256 mpAmount = shareAmount;
        if (address(feeManager) != address(0)) {
            uint256 fee = feeManager.calculateFeeWithNFT(false, shareAmount, nftTokenId, msg.sender);
            mpAmount = shareAmount - fee;
        }
        mpAmount = _roundWithdrawalAmount(mpAmount);

        withdrawalId = withdrawalCounter++;
        uint256 requestTimestamp = block.timestamp;
        bytes32 operationHash = _hashWithdrawalOperation(
            withdrawalId, msg.sender, mpToken, shareAmount, mpAmount, btcScript, "", requestTimestamp
        );

        withdrawals[withdrawalId] = WithdrawalRequest({
            requester: msg.sender,
            mpToken: mpToken,
            shareAmount: shareAmount,
            mpAmount: mpAmount,
            btcScript: btcScript,
            metadata: "",
            isProcessed: false,
            timestamp: requestTimestamp,
            operationHash: operationHash
        });

        _escrowWithdrawal(withdrawalId);
        emit WithdrawalRequested(withdrawalId, msg.sender, mpToken, shareAmount, operationHash);
    }

    /**
     * @notice Internal function to create withdrawal request
     */
    function _requestWithdrawal(address mpToken, uint256 shareAmount, bytes memory btcScript, bytes memory metadata)
        internal
        returns (uint256 withdrawalId)
    {
        if (!(pools[mpToken].isActive || pools[mpToken].totalDeposited > 0)) revert InvalidPool();
        if (shareAmount == 0) revert ZeroAmount();
        require(btcScript.length > 0 && btcScript.length <= 10000, "Invalid BTC script");
        if (shareToken.balanceOf(msg.sender) < shareAmount) revert InsufficientShares();
        if (getAvailableUserPoolBalance(msg.sender, mpToken) < shareAmount) revert InsufficientPoolShares();
        pendingUserPoolShares[msg.sender][mpToken] += shareAmount;

        // Calculate MP token amount (1:1 for demo), apply fee if set
        uint256 mpAmount = shareAmount;
        if (address(feeManager) != address(0)) {
            uint256 fee = feeManager.calculateFee(false, shareAmount);
            mpAmount = shareAmount - fee;
        }
        mpAmount = _roundWithdrawalAmount(mpAmount);

        // Generate operation hash for signatures
        withdrawalId = withdrawalCounter++;
        uint256 requestTimestamp = block.timestamp;
        bytes32 operationHash = _hashWithdrawalOperation(
            withdrawalId, msg.sender, mpToken, shareAmount, mpAmount, btcScript, metadata, requestTimestamp
        );

        // Create withdrawal request
        withdrawals[withdrawalId] = WithdrawalRequest({
            requester: msg.sender,
            mpToken: mpToken,
            shareAmount: shareAmount,
            mpAmount: mpAmount,
            btcScript: btcScript,
            metadata: metadata,
            isProcessed: false,
            timestamp: requestTimestamp,
            operationHash: operationHash
        });

        _escrowWithdrawal(withdrawalId);
        if (metadata.length > 0) {
            emit WithdrawalRequestedWithMetadata(
                withdrawalId, msg.sender, mpToken, shareAmount, operationHash, metadata
            );
        } else {
            emit WithdrawalRequested(withdrawalId, msg.sender, mpToken, shareAmount, operationHash);
        }
    }

    /// @dev Round before storing or signing. The full difference accrues only at settlement.
    function _roundWithdrawalAmount(uint256 netAmount) private pure returns (uint256 amount) {
        amount = netAmount / SAT * SAT;
        if (amount == 0) revert ZeroAmount();
    }

    /**
     * @notice Process withdrawal after BOTH FROST and custodian signatures collected
     * @param withdrawalId Withdrawal request ID
     * @dev Requires both FROST threshold AND minimum custodian signatures
     */
    function processWithdrawal(uint256 withdrawalId) external nonReentrant {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        if (withdrawal.isProcessed) revert AlreadyProcessed();
        if (withdrawalCancelled[withdrawalId]) revert WithdrawalCancelledError();
        if (withdrawal.shareAmount == 0) revert InvalidWithdrawal();

        bytes32 operationHash = withdrawal.operationHash;

        // Verify FROST threshold is met
        if (!frostMultisig.isOperationApproved(operationHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }

        // Verify custodian threshold is met
        if (!custodianValidator.meetsMinimumCustodians(operationHash)) {
            revert InsufficientCustodianSignatures();
        }

        // Mark as executed in FROST
        frostMultisig.consumeOperation(operationHash);

        require(!withdrawalPrepared[withdrawalId],"Already prepared");
        withdrawalPrepared[withdrawalId]=true;
        emit WithdrawalPrepared(withdrawalId);
    }

    function cancelWithdrawal(uint256 withdrawalId) external nonReentrant {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        if (withdrawal.shareAmount == 0) revert InvalidWithdrawal();
        if (withdrawal.isProcessed) revert AlreadyProcessed();
        if (withdrawalCancelled[withdrawalId]) revert WithdrawalCancelledError();
        if (msg.sender != withdrawal.requester) revert OnlyRequester();
        if (block.timestamp < withdrawal.timestamp + WITHDRAWAL_CANCEL_DELAY) revert CancelDelayNotReached();

        _cancelWithdrawal(withdrawalId, false);
    }

    function claimFees(address mpToken, bool isWrap, address recipient, uint256 amount, bytes32 operationHash)
        external
        nonReentrant
    {
        require(recipient != address(0) && recipient != address(this), "Invalid recipient");
        require(amount > 0 && amount <= accruedFees[mpToken][isWrap], "Insufficient accrued fees");
        require(operationHash == getClaimFeesOperationHash(mpToken, isWrap, recipient, amount), "Operation hash mismatch");
        if (!custodianValidator.meetsMinimumCustodians(operationHash)) revert InsufficientCustodianSignatures();
        uint256 poolBalance = IERC20(mpToken).balanceOf(address(this));
        uint256 recipientBalance = IERC20(mpToken).balanceOf(recipient);
        require(poolBalance + escrowedMP[mpToken] >= pools[mpToken].totalDeposited, "Insufficient tracked backing");
        require(pools[mpToken].totalDeposited >= accruedFees[mpToken][true] + accruedFees[mpToken][false], "Insufficient tracked backing");
        frostMultisig.consumeOperation(operationHash);
        operationNonce++;
        accruedFees[mpToken][isWrap] -= amount;
        pools[mpToken].totalDeposited -= amount;
        IERC20(mpToken).safeTransfer(recipient, amount);
        if (IERC20(mpToken).balanceOf(address(this)) != poolBalance - amount
            || IERC20(mpToken).balanceOf(recipient) != recipientBalance + amount) revert InexactTransfer();
        emit FeesClaimed(mpToken, isWrap, recipient, amount);
    }

    function getClaimFeesOperationHash(address mpToken, bool isWrap, address recipient, uint256 amount)
        public
        view
        returns (bytes32)
    {
        return frostMultisig.hashOperation(
            address(this), this.claimFees.selector, keccak256(abi.encode(mpToken, isWrap, recipient, amount)), operationNonce
        );
    }

    function _accrueFee(address mpToken, uint256 amount, bool isWrap) private {
        if (amount == 0) return;
        accruedFees[mpToken][isWrap] += amount;
        pendingUnreportedFees[mpToken][isWrap] += amount;
        _syncFeeReports(mpToken, isWrap);
    }

    /// @notice Retry manager accounting without changing accrued or claimable fees.
    function syncFeeReports(address mpToken, bool isWrap) external nonReentrant {
        _syncFeeReports(mpToken, isWrap);
    }

    function _syncFeeReports(address mpToken, bool isWrap) private {
        uint256 amount = pendingUnreportedFees[mpToken][isWrap];
        // Rounding may accrue a fee even before the optional manager is configured.
        if (amount == 0 || address(feeManager) == address(0)) return;
        // The manager still enforces collector authorization, including after revocation.
        pendingUnreportedFees[mpToken][isWrap] = 0;
        try feeManager.recordFeeCollectedForToken{gas: FEE_REPORT_GAS_LIMIT}(mpToken, amount, isWrap) {
            emit FeesReported(mpToken, isWrap, amount);
        } catch {
            pendingUnreportedFees[mpToken][isWrap] = amount;
            emit FeeReportFailed(mpToken, isWrap, amount);
        }
    }

    function rejectWithdrawal(uint256 withdrawalId) external nonReentrant {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        if (withdrawal.shareAmount == 0) revert InvalidWithdrawal();
        if (withdrawal.isProcessed) revert AlreadyProcessed();
        if (withdrawalCancelled[withdrawalId]) revert WithdrawalCancelledError();

        bytes32 operationHash = getRejectWithdrawalOperationHash(withdrawalId);
        if (!frostMultisig.isOperationApproved(operationHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }
        if (!custodianValidator.meetsMinimumCustodians(operationHash)) {
            revert InsufficientCustodianSignatures();
        }

        frostMultisig.consumeOperation(operationHash);
        _cancelWithdrawal(withdrawalId, true);
    }

    function _cancelWithdrawal(uint256 withdrawalId, bool thresholdApproved) internal {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        withdrawalEscrow.refund(withdrawalEscrow.requestKey(address(this),withdrawalId));
        escrowedMP[withdrawal.mpToken]-=withdrawal.mpAmount;
        shareToken.escrow(address(this),withdrawal.requester,withdrawal.shareAmount);
        withdrawalCancelled[withdrawalId] = true;
        pendingUserPoolShares[withdrawal.requester][withdrawal.mpToken] -= withdrawal.shareAmount;

        emit WithdrawalCancelled(withdrawalId, withdrawal.requester, thresholdApproved);
    }

    // ======== VIEW FUNCTIONS ========

    function getPoolCount() external view returns (uint256) {
        return poolList.length;
    }

    function getPoolInfo(address mpToken) external view returns (PoolInfo memory) {
        return pools[mpToken];
    }

    function getUserPoolBalance(address user, address mpToken) external view returns (uint256) {
        return userPoolBalances[user][mpToken];
    }

    function getAvailableUserPoolBalance(address user, address mpToken) public view returns (uint256) {
        return userPoolBalances[user][mpToken] - pendingUserPoolShares[user][mpToken];
    }

    function getWithdrawal(uint256 withdrawalId) external view returns (WithdrawalRequest memory) {
        return withdrawals[withdrawalId];
    }

    function getFrostMultisig() external view returns (address) {
        return address(frostMultisig);
    }

    function getCustodianValidator() external view returns (address) {
        return address(custodianValidator);
    }

    function getShareToken() external view returns (address) {
        return address(shareToken);
    }

    function getAddPoolOperationHash(address mpToken, string memory poolId) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.addPool.selector, keccak256(abi.encode(mpToken, poolId)), operationNonce
        );
    }

    function getSetFeeManagerOperationHash(address _feeManager) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setFeeManager.selector, keccak256(abi.encode(_feeManager)), operationNonce
        );
    }

    function getDeactivatePoolOperationHash(address mpToken) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.deactivatePool.selector, keccak256(abi.encode(mpToken)), operationNonce
        );
    }

    function getProcessWithdrawalOperationHash(uint256 withdrawalId) public view returns (bytes32) {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        return _hashWithdrawalOperation(
            withdrawalId,
            withdrawal.requester,
            withdrawal.mpToken,
            withdrawal.shareAmount,
            withdrawal.mpAmount,
            withdrawal.btcScript,
            withdrawal.metadata,
            withdrawal.timestamp
        );
    }

    function getRejectWithdrawalOperationHash(uint256 withdrawalId) public view returns (bytes32) {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        return frostMultisig.hashOperation(
            address(this),
            this.rejectWithdrawal.selector,
            keccak256(
                abi.encode(
                    withdrawalId,
                    withdrawal.requester,
                    withdrawal.mpToken,
                    withdrawal.shareAmount,
                    withdrawal.mpAmount,
                    withdrawal.btcScript,
                    withdrawal.metadata,
                    withdrawal.timestamp
                )
            ),
            withdrawalId
        );
    }

    function _hashWithdrawalOperation(
        uint256 withdrawalId,
        address requester,
        address mpToken,
        uint256 shareAmount,
        uint256 mpAmount,
        bytes memory btcScript,
        bytes memory metadata,
        uint256 timestamp
    ) internal view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this),
            this.processWithdrawal.selector,
            keccak256(
                abi.encode(withdrawalId, requester, mpToken, shareAmount, mpAmount, btcScript, metadata, timestamp)
            ),
            withdrawalId
        );
    }

    /**
     * @notice Get total value locked across all pools
     */
    function getTotalValueLocked() external view returns (uint256) {
        uint256 total = 0;
        for (uint256 i = 0; i < poolList.length; i++) {
            total += pools[poolList[i]].totalDeposited;
        }
        return total;
    }

    /**
     * @notice Get all pools
     */
    function getAllPools() external view returns (address[] memory) {
        return poolList;
    }

    /**
     * @notice Check if a withdrawal can be processed
     * @param withdrawalId Withdrawal request ID
     * @return frostReady True if FROST threshold is met
     * @return custodianReady True if custodian threshold is met
     */
    function canProcessWithdrawal(uint256 withdrawalId) external view returns (bool frostReady, bool custodianReady) {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        if (withdrawal.shareAmount == 0 || withdrawal.isProcessed || withdrawalCancelled[withdrawalId]) {
            return (false, false);
        }

        bytes32 operationHash = withdrawal.operationHash;
        frostReady = frostMultisig.isOperationApproved(operationHash, address(this));
        custodianReady = custodianValidator.meetsMinimumCustodians(operationHash);
    }

    /**
     * @notice Get signature status for a withdrawal
     * @param withdrawalId Withdrawal request ID
     * @return frostSigs Number of FROST signatures
     * @return frostThreshold Required FROST threshold
     * @return custodianSigs Number of custodian signatures
     * @return custodianMin Required minimum custodians
     */
    function getWithdrawalSignatureStatus(uint256 withdrawalId)
        external
        view
        returns (uint256 frostSigs, uint256 frostThreshold, uint256 custodianSigs, uint256 custodianMin)
    {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        bytes32 operationHash = withdrawal.operationHash;

        frostSigs = frostMultisig.isOperationApproved(operationHash, address(this)) ? frostMultisig.threshold() : 0;
        frostThreshold = frostMultisig.threshold();
        custodianSigs = custodianValidator.countCustodianSignatures(operationHash);
        custodianMin = custodianValidator.getMinCustodiansRequired();
    }
}
