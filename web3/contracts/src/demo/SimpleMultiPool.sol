// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "../tokens/MultiPoolShareToken.sol";
import "../FROSTAuthorization.sol";
import "../federation/IFeeManager.sol";

import "../federation/BitcoinWithdrawalEscrow.sol";

/**
 * @title SimpleMultiPool
 * @notice Multi-pool DAO that aggregates multiple mining pools
 * @dev Users deposit MP tokens from individual pools and receive S-tokens (share tokens)
 *      Withdrawals are managed via FROST multisig
 */
contract SimpleMultiPool is ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 public constant WITHDRAWAL_CANCEL_DELAY = 7 days;
    uint256 public constant FEE_REPORT_GAS_LIMIT = 100_000;
    uint256 private constant SAT = 1e10;
    uint256 transient private quotedNetPlusOne;

    // ======== STATE ========

    /// @notice FROST multisig for managing withdrawals
    FROSTAuthorization public immutable frostMultisig;

    /// @notice Share token for this MultiPool
    MultiPoolShareToken public immutable shareToken;

    /// @notice MultiPool identifier
    string public multiPoolId;

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
    mapping(address => mapping(address => uint256)) public userPoolBalances; // user => mpToken => balance
    mapping(address => mapping(address => uint256)) public pendingUserPoolShares; // user => mpToken => pending withdrawal shares

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
        bytes32 operationHash;
    }

    mapping(uint256 => WithdrawalRequest) public withdrawals;
    mapping(uint256 => bool) public withdrawalCancelled;
    uint256 public withdrawalCounter;
    uint256 public operationNonce;
    mapping(address => mapping(bool => uint256)) public accruedFees;
    /// @notice Accrued fees whose manager notification has not succeeded yet.
    mapping(address => mapping(bool => uint256)) public pendingUnreportedFees;

    BitcoinWithdrawalEscrow public withdrawalEscrow;
    mapping(uint256 => bool) public withdrawalPrepared;
    mapping(address=>uint256) public escrowedMP;
    struct FeeLock { address owner; address token; uint256 amount; }
    mapping(bytes32=>FeeLock) private feeLocks;
    function feeLock(bytes32 key) external view returns(address,address,uint256) {
        FeeLock memory f=feeLocks[key];return(f.owner,f.token,f.amount);
    }
    /// @notice Owner-initiated, fee-only S tranche. No additional unwrap fee and
    /// no arbitrary intermediary or permission to spend another user's claim.
    function consentFee(WithdrawalFeeTypes.Consent calldata c) external nonReentrant {
        address ledger=withdrawalEscrow.withdrawalFees();require(ledger != address(0) && c.pool == address(this)
            && c.mode == WithdrawalFeeTypes.Mode.S,"fee S profile");
        BitcoinWithdrawalEscrow.Request memory r=withdrawalEscrow.getRequest(c.request);
        require(r.owner == msg.sender && r.token != address(0) && pools[r.token].exists,"fee S owner/underlying");
        FeeLock storage f=feeLocks[c.request];uint256 amount=uint256(c.cap)*SAT;
        require(amount >= f.amount && (f.amount == 0 || f.owner == msg.sender && f.token == r.token),"fee S lock binding");
        uint256 extra=amount-f.amount;require(getAvailableUserPoolBalance(msg.sender,r.token) >= extra,"fee available per-MP shares");
        if(extra != 0) {
            shareToken.escrow(msg.sender,address(this),extra);pendingUserPoolShares[msg.sender][r.token]+=extra;
            escrowedMP[r.token]+=extra;
        }
        f.owner=msg.sender;f.token=r.token;f.amount=amount;
        IERC20(r.token).forceApprove(ledger,extra);IWithdrawalFeeConsent(ledger).consentS(msg.sender,c);IERC20(r.token).forceApprove(ledger,0);
    }
    function settleFeeBudget(bytes32 key,address owner,address token,uint256 held,uint256 spent) external nonReentrant {
        require(msg.sender == withdrawalEscrow.withdrawalFees() && msg.sender != address(0),"fee ledger only");
        FeeLock storage f=feeLocks[key];require(f.owner == owner && f.token == token && f.amount == held && spent <= held,"fee S settlement binding");
        f.amount=0;escrowedMP[token]-=held;pendingUserPoolShares[owner][token]-=held;userPoolBalances[owner][token]-=spent;
        pools[token].totalDeposited-=spent;
        if(spent != 0)shareToken.burn(address(this),spent);
        if(held != spent)shareToken.escrow(address(this),owner,held-spent);
        require(IERC20(token).balanceOf(address(this))+escrowedMP[token] >= pools[token].totalDeposited,"fee S tracked backing");
    }
    event WithdrawalPrepared(uint256 indexed withdrawalId);

    function getSetWithdrawalEscrowOperationHash(address escrow) public view returns(bytes32) {
        return frostMultisig.hashOperation(address(this),this.setWithdrawalEscrow.selector,keccak256(abi.encode(escrow)),operationNonce);
    }
    function setWithdrawalEscrow(address escrow,bytes32 op) external {
        require(address(withdrawalEscrow)==address(0) && escrow.code.length>0,"escrow config");
        require(address(BitcoinWithdrawalEscrow(escrow).batches().authorization())==address(frostMultisig),"escrow auth");
        require(op==getSetWithdrawalEscrowOperationHash(escrow),"operation hash");
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

    // ======== CONSTRUCTOR ========

    constructor(string memory _multiPoolId, address _frostMultisig, address _shareToken) {
        require(bytes(_multiPoolId).length > 0, "Empty multipool ID");
        require(_frostMultisig != address(0), "Invalid FROST address");
        require(_shareToken != address(0), "Invalid share token");

        multiPoolId = _multiPoolId;
        frostMultisig = FROSTAuthorization(_frostMultisig);
        shareToken = MultiPoolShareToken(_shareToken);

        require(shareToken.multiPool() == address(this), "Share token pool mismatch");
    }

    // ======== FEE MANAGER SETUP ========

    /**
     * @notice Set the fee manager (callable once during setup)
     * @param _feeManager Address of the fee manager contract
     * @param operationHash FROST-signed operation hash for this setup action
     */
    function setFeeManager(address _feeManager, bytes32 operationHash) external {
        require(address(feeManager) == address(0), "Fee manager already set");
        require(_feeManager != address(0), "Invalid fee manager");
        require(operationHash == getSetFeeManagerOperationHash(_feeManager), "Operation hash mismatch");
        require(frostMultisig.isOperationApproved(operationHash, address(this)), "FROST threshold not met");

        (address authorization, address validator) = IFeeManager(_feeManager).getFeeSecurityConfig();
        require(authorization == address(frostMultisig) && validator == address(0), "Fee security mismatch");
        require(IFeeManager(_feeManager).authorizedFeeCollectors(address(this)), "Fee collector not authorized");

        frostMultisig.consumeOperation(operationHash);
        operationNonce++;

        feeManager = IFeeManager(_feeManager);

        emit FeeManagerSet(_feeManager, operationHash);
    }

    // ======== POOL MANAGEMENT ========

    /**
     * @notice Add a pool to the MultiPool (requires FROST threshold approval)
     * @param mpToken Address of the pool's MP token
     * @param poolId Pool identifier
     * @param operationHash FROST-signed operation hash (from governance proposal)
     */
    function addPool(address mpToken, string calldata poolId, bytes32 operationHash) external {
        require(mpToken != address(0), "Invalid MP token");
        require(!pools[mpToken].isActive, "Pool already exists");
        require(operationHash == getAddPoolOperationHash(mpToken, poolId), "Operation hash mismatch");

        // Verify FROST threshold approval
        require(frostMultisig.isOperationApproved(operationHash, address(this)), "FROST threshold not met");

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
     * @notice Deactivate a pool (requires FROST threshold approval)
     * @param mpToken Address of the pool's MP token
     * @param operationHash FROST-signed operation hash (from governance proposal)
     */
    function deactivatePool(address mpToken, bytes32 operationHash) external {
        require(pools[mpToken].isActive, "Pool not active");
        require(operationHash == getDeactivatePoolOperationHash(mpToken), "Operation hash mismatch");

        // Verify FROST threshold approval
        require(frostMultisig.isOperationApproved(operationHash, address(this)), "FROST threshold not met");

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
    function deposit(address mpToken, uint256 mpAmount) external nonReentrant returns (uint256 shareAmount) {
        require(pools[mpToken].isActive, "Pool not active");
        require(mpAmount > 0, "Zero amount");

        // Transfer MP tokens from user to MultiPool
        uint256 balanceBefore = IERC20(mpToken).balanceOf(address(this));
        IERC20(mpToken).safeTransferFrom(msg.sender, address(this), mpAmount);
        require(IERC20(mpToken).balanceOf(address(this)) == balanceBefore + mpAmount, "Inexact deposit transfer");

        // Calculate share amount (1:1 ratio for simplicity in demo)
        // If feeManager is set, deduct fee from share amount
        shareAmount = mpAmount;
        if (address(feeManager) != address(0)) {
            uint256 fee = feeManager.calculateFee(true, mpAmount);
            shareAmount = mpAmount - fee;
            _accrueFee(mpToken, fee, true);
        }

        // Mint S-tokens to user
        shareToken.mint(msg.sender, shareAmount);

        // Update tracking
        pools[mpToken].totalDeposited += mpAmount;
        userPoolBalances[msg.sender][mpToken] += shareAmount;

        emit Deposited(msg.sender, mpToken, mpAmount, shareAmount);
    }

    // ======== WITHDRAWAL (S -> BTC) ========

    /**
     * @notice Request withdrawal - lock S-tokens, request BTC via FROST
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
     * @param metadata Metadata to include in OP_RETURN (e.g., "WITHDRAWAL-REF:FED1-2025-001")
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
     * @param nftTokenId NFT token ID for fee discount
     * @return withdrawalId Withdrawal request ID
     */
    function requestWithdrawalWithNFT(address mpToken, uint256 shareAmount, bytes calldata btcScript, uint256 nftTokenId)
        external
        nonReentrant
        returns (uint256 withdrawalId)
    {
        return _requestWithdrawalWithNFT(mpToken, shareAmount, btcScript, "", nftTokenId);
    }

    /**
     * @notice Internal function to create withdrawal request with NFT discount
     */
    function _requestWithdrawalWithNFT(address mpToken,uint256 shareAmount,bytes memory btcScript,
        bytes memory metadata,uint256 nftTokenId) internal returns(uint256 withdrawalId)
    {
        require(pools[mpToken].isActive || pools[mpToken].totalDeposited > 0, "Invalid pool");
        require(shareAmount > 0, "Zero amount");
        require(btcScript.length > 0 && btcScript.length <= 10000, "Invalid BTC script");
        require(shareToken.balanceOf(msg.sender) >= shareAmount, "Insufficient shares");
        require(getAvailableUserPoolBalance(msg.sender, mpToken) >= shareAmount, "Insufficient pool shares");
        pendingUserPoolShares[msg.sender][mpToken] += shareAmount;

        // Calculate MP token amount (1:1 for demo), apply fee if feeManager set
        uint256 fee;
        if (address(feeManager) != address(0)) {
            fee = feeManager.calculateFeeWithNFT(false,shareAmount,nftTokenId,msg.sender);
        }
        uint256 mpAmount = _roundWithdrawalAmount(shareAmount-fee);

        withdrawalId = withdrawalCounter++;
        uint256 requestTimestamp = block.timestamp;
        bytes32 operationHash = _hashWithdrawalOperation(
            this.processWithdrawal.selector,
            withdrawalId,
            msg.sender,
            mpToken,
            shareAmount,
            mpAmount,
            btcScript,
            metadata,
            requestTimestamp
        );

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

    /// @notice Guard the exact ordinary fee and payout in the transaction that
    /// locks S. The transient guard is cleared before returning to a multicall.
    function requestWithdrawalQuoted(address mpToken,uint256 shareAmount,bytes calldata btcScript,
        uint256 expectedFee,uint256 expectedPayout,uint256 nftTokenId,bool useNFT)
        external nonReentrant returns(uint256 id)
    {
        require(expectedPayout > 0 && shareAmount > expectedFee,"quoted payout");
        quotedNetPlusOne=shareAmount-expectedFee+1;
        id=useNFT ? _requestWithdrawalWithNFT(mpToken,shareAmount,btcScript,"",nftTokenId)
                  : _requestWithdrawal(mpToken,shareAmount,btcScript,"");
        quotedNetPlusOne=0;
        require(withdrawals[id].mpAmount == expectedPayout,"withdrawal quote changed");
    }

    function _requestWithdrawal(address mpToken,uint256 shareAmount,bytes memory btcScript,bytes memory metadata)
        internal returns(uint256 withdrawalId)
    {
        require(pools[mpToken].isActive || pools[mpToken].totalDeposited > 0, "Invalid pool");
        require(shareAmount > 0, "Zero amount");
        require(btcScript.length > 0 && btcScript.length <= 10000, "Invalid BTC script");
        require(shareToken.balanceOf(msg.sender) >= shareAmount, "Insufficient shares");
        require(getAvailableUserPoolBalance(msg.sender, mpToken) >= shareAmount, "Insufficient pool shares");
        pendingUserPoolShares[msg.sender][mpToken] += shareAmount;

        // Calculate MP token amount (1:1 for demo), apply fee if feeManager set
        uint256 fee;
        if (address(feeManager) != address(0)) {
            fee = feeManager.calculateFee(false,shareAmount);
        }
        uint256 mpAmount = _roundWithdrawalAmount(shareAmount-fee);

        withdrawalId = withdrawalCounter++;
        uint256 requestTimestamp = block.timestamp;
        bytes32 operationHash = _hashWithdrawalOperation(
            this.processWithdrawal.selector,
            withdrawalId,
            msg.sender,
            mpToken,
            shareAmount,
            mpAmount,
            btcScript,
            metadata,
            requestTimestamp
        );

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
    function _roundWithdrawalAmount(uint256 netAmount) private view returns (uint256 amount) {
        require(quotedNetPlusOne == 0 || netAmount == quotedNetPlusOne-1,"withdrawal quote changed");
        amount = netAmount / SAT * SAT;
        require(amount > 0, "Zero amount");
    }

    /**
     * @notice Process withdrawal after FROST signatures collected
     * @param withdrawalId Withdrawal request ID
     * @param operationHash FROST operation hash with sufficient signatures
     */
    function processWithdrawal(uint256 withdrawalId, bytes32 operationHash) external nonReentrant {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        require(!withdrawal.isProcessed, "Already processed");
        require(!withdrawalCancelled[withdrawalId], "Withdrawal cancelled");
        require(withdrawal.shareAmount > 0, "Invalid withdrawal");
        require(operationHash == withdrawal.operationHash, "Operation hash mismatch");

        // Verify FROST threshold is met
        require(frostMultisig.isOperationApproved(operationHash, address(this)), "Insufficient FROST signatures");

        // Mark as executed in FROST
        frostMultisig.consumeOperation(operationHash);
        require(!withdrawalPrepared[withdrawalId],"Already prepared");
        withdrawalPrepared[withdrawalId]=true;
        emit WithdrawalPrepared(withdrawalId);
    }

    function cancelWithdrawal(uint256 withdrawalId) external nonReentrant {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        require(withdrawal.shareAmount > 0, "Invalid withdrawal");
        require(!withdrawal.isProcessed, "Already processed");
        require(!withdrawalCancelled[withdrawalId], "Withdrawal cancelled");
        require(msg.sender == withdrawal.requester, "Only requester");
        require(block.timestamp >= withdrawal.timestamp + WITHDRAWAL_CANCEL_DELAY, "Cancel delay not reached");

        _cancelWithdrawal(withdrawalId, false);
    }

    function rejectWithdrawal(uint256 withdrawalId, bytes32 operationHash) external nonReentrant {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        require(withdrawal.shareAmount > 0, "Invalid withdrawal");
        require(!withdrawal.isProcessed, "Already processed");
        require(!withdrawalCancelled[withdrawalId], "Withdrawal cancelled");
        require(operationHash == getRejectWithdrawalOperationHash(withdrawalId), "Operation hash mismatch");
        require(frostMultisig.isOperationApproved(operationHash, address(this)), "Insufficient FROST signatures");

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

    function claimFees(address mpToken, bool isWrap, address recipient, uint256 amount, bytes32 operationHash)
        external
        nonReentrant
    {
        require(recipient != address(0) && recipient != address(this), "Invalid recipient");
        require(amount > 0 && amount <= accruedFees[mpToken][isWrap], "Insufficient accrued fees");
        require(operationHash == getClaimFeesOperationHash(mpToken, isWrap, recipient, amount), "Operation hash mismatch");
        uint256 poolBalance = IERC20(mpToken).balanceOf(address(this));
        uint256 recipientBalance = IERC20(mpToken).balanceOf(recipient);
        require(poolBalance + escrowedMP[mpToken] >= pools[mpToken].totalDeposited, "Insufficient tracked backing");
        require(pools[mpToken].totalDeposited >= accruedFees[mpToken][true] + accruedFees[mpToken][false], "Insufficient tracked backing");
        frostMultisig.consumeOperation(operationHash);
        operationNonce++;
        accruedFees[mpToken][isWrap] -= amount;
        pools[mpToken].totalDeposited -= amount;
        IERC20(mpToken).safeTransfer(recipient, amount);
        require(IERC20(mpToken).balanceOf(address(this)) == poolBalance - amount, "Inexact fee debit");
        require(IERC20(mpToken).balanceOf(recipient) == recipientBalance + amount, "Inexact fee credit");
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
        return withdrawal.operationHash;
    }

    function getRejectWithdrawalOperationHash(uint256 withdrawalId) public view returns (bytes32) {
        WithdrawalRequest storage withdrawal = withdrawals[withdrawalId];
        return _hashWithdrawalOperation(
            this.rejectWithdrawal.selector,
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

    function _hashWithdrawalOperation(
        bytes4 selector,
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
            selector,
            keccak256(
                abi.encode(withdrawalId, requester, mpToken, shareAmount, mpAmount, btcScript, metadata, timestamp)
            ),
            withdrawalId
        );
    }

    /**
     * @notice Get total value locked across all pools
     * @return Total MP tokens deposited across all pools
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
     * @return Array of pool MP token addresses
     */
    function getAllPools() external view returns (address[] memory) {
        return poolList;
    }
}
