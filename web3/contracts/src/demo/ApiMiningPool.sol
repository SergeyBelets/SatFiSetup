// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/utils/Pausable.sol";
import "../tokens/PoolMpToken.sol";
import "./SimpleDistributionTable.sol";

/**
 * @title ApiMiningPool
 * @notice Simplified mining pool for API-based deposits (no SPV verification)
 * @dev Flow: API records deposits -> Auditor approves -> Distribution -> Withdrawal queue
 *
 * Use case: Pool account with external API that reports balance changes.
 * This is a stepping stone before full SPV integration.
 */
contract ApiMiningPool is AccessControl, ReentrancyGuard, Pausable {
    // ======== ROLES ========
    bytes32 public constant AUDITOR_ROLE = keccak256("AUDITOR_ROLE");
    bytes32 public constant OPERATOR_ROLE = keccak256("OPERATOR_ROLE");

    // ======== STATE ========
    PoolMpToken public mpToken;
    SimpleDistributionTable public distributionTable;
    string public poolId;

    // Deposit tracking
    enum DepositStatus { Pending, Approved, Distributed, Rejected }

    struct Deposit {
        string externalId;      // External reference (e.g., pool API transaction ID)
        uint64 amountSat;       // Amount in satoshis
        uint256 timestamp;      // When recorded
        DepositStatus status;
        string note;            // Optional note from operator/auditor
    }

    mapping(uint256 => Deposit) public deposits;
    uint256 public depositCounter;
    uint256 public totalDeposited;
    uint256 public totalDistributed;

    uint256 private constant SATOSHI_MULTIPLIER = 1e10; // 10^10 to convert sats to 18 decimal tokens

    // Withdrawal queue
    enum WithdrawalStatus { Pending, Processing, Sent, Cancelled }

    struct Withdrawal {
        address requester;
        uint64 amountSat;
        bytes btcScript;        // Bitcoin output script (where to send BTC)
        WithdrawalStatus status;
        uint256 timestamp;
        string txid;            // Bitcoin TXID once sent
        string note;            // Optional note
    }

    mapping(uint256 => Withdrawal) public withdrawals;
    uint256 public withdrawalCounter;
    uint256 public totalPendingWithdrawals;

    // ======== EVENTS ========
    event MpTokenSet(address indexed mpToken);
    event DistributionTableSet(address indexed table);

    event DepositRecorded(uint256 indexed depositId, string externalId, uint64 amountSat);
    event DepositApproved(uint256 indexed depositId, address indexed auditor);
    event DepositRejected(uint256 indexed depositId, address indexed auditor, string reason);
    event DepositDistributed(uint256 indexed depositId, address[] recipients, uint256[] amounts);

    event WithdrawalRequested(uint256 indexed withdrawalId, address indexed requester, uint64 amountSat);
    event WithdrawalProcessing(uint256 indexed withdrawalId);
    event WithdrawalSent(uint256 indexed withdrawalId, string txid);
    event WithdrawalCancelled(uint256 indexed withdrawalId, string reason);

    // ======== CONSTRUCTOR ========
    constructor(string memory _poolId) {
        require(bytes(_poolId).length > 0, "Empty pool ID");

        poolId = _poolId;

        // Grant roles to deployer
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(OPERATOR_ROLE, msg.sender);
    }

    // ======== SETUP ========

    function setMpToken(address _mpToken) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(address(mpToken) == address(0), "MP token already set");
        require(_mpToken != address(0), "Invalid MP token");
        mpToken = PoolMpToken(_mpToken);
        emit MpTokenSet(_mpToken);
    }

    function setDistributionTable(address _table) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(_table != address(0), "Invalid table");
        distributionTable = SimpleDistributionTable(_table);
        emit DistributionTableSet(_table);
    }

    // ======== PAUSE ========

    function pause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _pause();
    }

    function unpause() external onlyRole(DEFAULT_ADMIN_ROLE) {
        _unpause();
    }

    // ======== ROLE MANAGEMENT ========

    function addAuditor(address auditor) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(auditor != address(0), "Invalid address");
        grantRole(AUDITOR_ROLE, auditor);
    }

    function removeAuditor(address auditor) external onlyRole(DEFAULT_ADMIN_ROLE) {
        revokeRole(AUDITOR_ROLE, auditor);
    }

    function addOperator(address operator) external onlyRole(DEFAULT_ADMIN_ROLE) {
        require(operator != address(0), "Invalid address");
        grantRole(OPERATOR_ROLE, operator);
    }

    function removeOperator(address operator) external onlyRole(DEFAULT_ADMIN_ROLE) {
        revokeRole(OPERATOR_ROLE, operator);
    }

    function isAuditor(address account) external view returns (bool) {
        return hasRole(AUDITOR_ROLE, account);
    }

    function isOperator(address account) external view returns (bool) {
        return hasRole(OPERATOR_ROLE, account);
    }

    // ======== DEPOSITS (API -> Contract) ========

    /**
     * @notice Record a deposit from external API
     * @dev Called by operator when API reports new balance
     * @param externalId External reference ID from the API
     * @param amountSat Amount in satoshis
     * @param note Optional note
     * @return depositId The ID of the created deposit
     */
    function recordDeposit(
        string calldata externalId,
        uint64 amountSat,
        string calldata note
    ) external onlyRole(OPERATOR_ROLE) whenNotPaused returns (uint256 depositId) {
        require(amountSat > 0, "Zero amount");
        require(bytes(externalId).length > 0, "Empty external ID");

        depositId = depositCounter++;
        deposits[depositId] = Deposit({
            externalId: externalId,
            amountSat: amountSat,
            timestamp: block.timestamp,
            status: DepositStatus.Pending,
            note: note
        });

        emit DepositRecorded(depositId, externalId, amountSat);
    }

    /**
     * @notice Approve a pending deposit (auditor only)
     * @param depositId The deposit to approve
     */
    function approveDeposit(uint256 depositId) external onlyRole(AUDITOR_ROLE) whenNotPaused {
        Deposit storage deposit = deposits[depositId];
        require(deposit.amountSat > 0, "Deposit not found");
        require(deposit.status == DepositStatus.Pending, "Not pending");

        deposit.status = DepositStatus.Approved;
        totalDeposited += deposit.amountSat;

        emit DepositApproved(depositId, msg.sender);
    }

    /**
     * @notice Reject a pending deposit (auditor only)
     * @param depositId The deposit to reject
     * @param reason Reason for rejection
     */
    function rejectDeposit(uint256 depositId, string calldata reason) external onlyRole(AUDITOR_ROLE) whenNotPaused {
        Deposit storage deposit = deposits[depositId];
        require(deposit.amountSat > 0, "Deposit not found");
        require(deposit.status == DepositStatus.Pending, "Not pending");

        deposit.status = DepositStatus.Rejected;
        deposit.note = reason;

        emit DepositRejected(depositId, msg.sender, reason);
    }

    /**
     * @notice Distribute an approved deposit to recipients
     * @dev Called by owner/operator after auditor approval
     * @param depositId The approved deposit to distribute
     * @param recipients Array of recipient addresses
     * @param amounts Array of amounts in satoshis (must sum to deposit amount)
     */
    function distributeDeposit(
        uint256 depositId,
        address[] calldata recipients,
        uint256[] calldata amounts
    ) external onlyRole(OPERATOR_ROLE) nonReentrant whenNotPaused {
        require(address(mpToken) != address(0), "MP token not set");
        require(address(distributionTable) != address(0), "Distribution table not set");
        require(distributionTable.isTableApproved(), "Distribution table not approved");
        require(recipients.length == amounts.length, "Length mismatch");
        require(recipients.length > 0, "No recipients");

        Deposit storage deposit = deposits[depositId];
        require(deposit.amountSat > 0, "Deposit not found");
        require(deposit.status == DepositStatus.Approved, "Not approved");

        // Verify total amount matches
        uint256 totalAmount = 0;
        for (uint256 i = 0; i < amounts.length; i++) {
            totalAmount += amounts[i];
        }
        require(totalAmount == deposit.amountSat, "Amount mismatch");

        // Mint MP tokens to recipients (convert sats to token decimals)
        for (uint256 i = 0; i < recipients.length; i++) {
            if (amounts[i] > 0) {
                mpToken.mint(recipients[i], amounts[i] * SATOSHI_MULTIPLIER);
            }
        }

        deposit.status = DepositStatus.Distributed;
        totalDistributed += deposit.amountSat;

        emit DepositDistributed(depositId, recipients, amounts);
    }

    /**
     * @notice Distribute deposit using distribution table directly
     * @dev Convenience function that calculates distribution from table.
     *      The table returns proportional amounts in token units,
     *      which are minted directly.
     * @param depositId The approved deposit to distribute
     */
    function distributeDepositFromTable(uint256 depositId) external onlyRole(OPERATOR_ROLE) nonReentrant whenNotPaused {
        require(address(mpToken) != address(0), "MP token not set");
        require(address(distributionTable) != address(0), "Distribution table not set");
        require(distributionTable.isTableApproved(), "Distribution table not approved");

        Deposit storage deposit = deposits[depositId];
        require(deposit.amountSat > 0, "Deposit not found");
        require(deposit.status == DepositStatus.Approved, "Not approved");

        uint256 amountTokens = uint256(deposit.amountSat) * SATOSHI_MULTIPLIER;

        // Get distribution from table (amounts returned in same units as input)
        (address[] memory recipients, uint256[] memory amounts) =
            distributionTable.calculateDistribution(amountTokens);

        require(recipients.length > 0, "No recipients in table");

        // Mint MP tokens to recipients
        for (uint256 i = 0; i < recipients.length; i++) {
            if (amounts[i] > 0) {
                mpToken.mint(recipients[i], amounts[i]);
            }
        }

        deposit.status = DepositStatus.Distributed;
        totalDistributed += deposit.amountSat;
        emit DepositDistributed(depositId, recipients, amounts);
    }

    // ======== WITHDRAWALS (Contract -> API queue) ========

    /**
     * @notice Request withdrawal of MP tokens for BTC
     * @dev Burns MP tokens and creates withdrawal request
     * @param amountSat Amount to withdraw in satoshis
     * @param btcScript Bitcoin output script (where to send BTC)
     * @return withdrawalId The ID of the created withdrawal request
     */
    function requestWithdrawal(
        uint64 amountSat,
        bytes calldata btcScript
    ) external nonReentrant whenNotPaused returns (uint256 withdrawalId) {
        require(address(mpToken) != address(0), "MP token not set");
        require(amountSat > 0, "Zero amount");
        require(btcScript.length > 0, "Empty script");

        uint256 amountTokens = uint256(amountSat) * SATOSHI_MULTIPLIER;

        // Burn MP tokens
        mpToken.burn(msg.sender, amountTokens);

        withdrawalId = withdrawalCounter++;
        withdrawals[withdrawalId] = Withdrawal({
            requester: msg.sender,
            amountSat: amountSat,
            btcScript: btcScript,
            status: WithdrawalStatus.Pending,
            timestamp: block.timestamp,
            txid: "",
            note: ""
        });

        totalPendingWithdrawals += amountSat;
        emit WithdrawalRequested(withdrawalId, msg.sender, amountSat);
    }

    /**
     * @notice Mark withdrawal as being processed (picked up by API)
     * @param withdrawalId The withdrawal to mark
     */
    function markWithdrawalProcessing(uint256 withdrawalId) external onlyRole(OPERATOR_ROLE) whenNotPaused {
        Withdrawal storage withdrawal = withdrawals[withdrawalId];
        require(withdrawal.amountSat > 0, "Withdrawal not found");
        require(withdrawal.status == WithdrawalStatus.Pending, "Not pending");

        withdrawal.status = WithdrawalStatus.Processing;

        emit WithdrawalProcessing(withdrawalId);
    }

    /**
     * @notice Mark withdrawal as sent with Bitcoin TXID
     * @param withdrawalId The withdrawal to mark as sent
     * @param txid The Bitcoin transaction ID
     */
    function markWithdrawalSent(
        uint256 withdrawalId,
        string calldata txid
    ) external onlyRole(OPERATOR_ROLE) whenNotPaused {
        require(bytes(txid).length > 0, "Empty txid");

        Withdrawal storage withdrawal = withdrawals[withdrawalId];
        require(withdrawal.amountSat > 0, "Withdrawal not found");
        require(
            withdrawal.status == WithdrawalStatus.Pending ||
            withdrawal.status == WithdrawalStatus.Processing,
            "Invalid status"
        );

        withdrawal.status = WithdrawalStatus.Sent;
        withdrawal.txid = txid;
        totalPendingWithdrawals -= withdrawal.amountSat;

        emit WithdrawalSent(withdrawalId, txid);
    }

    /**
     * @notice Cancel a withdrawal and refund MP tokens
     * @dev Only for pending/processing withdrawals
     * @param withdrawalId The withdrawal to cancel
     * @param reason Reason for cancellation
     */
    function cancelWithdrawal(
        uint256 withdrawalId,
        string calldata reason
    ) external onlyRole(OPERATOR_ROLE) nonReentrant whenNotPaused {
        Withdrawal storage withdrawal = withdrawals[withdrawalId];
        require(withdrawal.amountSat > 0, "Withdrawal not found");
        require(
            withdrawal.status == WithdrawalStatus.Pending ||
            withdrawal.status == WithdrawalStatus.Processing,
            "Cannot cancel"
        );

        uint256 amountTokens = uint256(withdrawal.amountSat) * SATOSHI_MULTIPLIER;

        // Refund MP tokens
        mpToken.mint(withdrawal.requester, amountTokens);

        withdrawal.status = WithdrawalStatus.Cancelled;
        withdrawal.note = reason;
        totalPendingWithdrawals -= withdrawal.amountSat;

        emit WithdrawalCancelled(withdrawalId, reason);
    }

    // ======== VIEWS ========

    function getDeposit(uint256 depositId) external view returns (Deposit memory) {
        return deposits[depositId];
    }

    function getWithdrawal(uint256 withdrawalId) external view returns (Withdrawal memory) {
        return withdrawals[withdrawalId];
    }

    function getMpTokenBalance(address account) external view returns (uint256) {
        if (address(mpToken) == address(0)) return 0;
        return mpToken.balanceOf(account);
    }

    /**
     * @notice Get all pending deposits awaiting auditor approval
     */
    function getPendingDeposits()
        external
        view
        returns (uint256[] memory ids, Deposit[] memory pendingDeposits)
    {
        // Count pending
        uint256 count = 0;
        for (uint256 i = 0; i < depositCounter; i++) {
            if (deposits[i].status == DepositStatus.Pending) {
                count++;
            }
        }

        ids = new uint256[](count);
        pendingDeposits = new Deposit[](count);

        uint256 index = 0;
        for (uint256 i = 0; i < depositCounter; i++) {
            if (deposits[i].status == DepositStatus.Pending) {
                ids[index] = i;
                pendingDeposits[index] = deposits[i];
                index++;
            }
        }
    }

    /**
     * @notice Get all pending withdrawals awaiting BTC send
     */
    function getPendingWithdrawals()
        external
        view
        returns (uint256[] memory ids, Withdrawal[] memory pendingWithdrawals)
    {
        // Count pending + processing
        uint256 count = 0;
        for (uint256 i = 0; i < withdrawalCounter; i++) {
            if (
                withdrawals[i].status == WithdrawalStatus.Pending ||
                withdrawals[i].status == WithdrawalStatus.Processing
            ) {
                count++;
            }
        }

        ids = new uint256[](count);
        pendingWithdrawals = new Withdrawal[](count);

        uint256 index = 0;
        for (uint256 i = 0; i < withdrawalCounter; i++) {
            if (
                withdrawals[i].status == WithdrawalStatus.Pending ||
                withdrawals[i].status == WithdrawalStatus.Processing
            ) {
                ids[index] = i;
                pendingWithdrawals[index] = withdrawals[i];
                index++;
            }
        }
    }

    /**
     * @notice Get pool statistics
     */
    function getPoolStats()
        external
        view
        returns (
            uint256 depositsCount,
            uint256 withdrawalsCount,
            uint256 totalDepositedAmount,
            uint256 totalDistributedAmount,
            uint256 pendingWithdrawalsAmount,
            uint256 circulatingSupply
        )
    {
        depositsCount = depositCounter;
        withdrawalsCount = withdrawalCounter;
        totalDepositedAmount = totalDeposited;
        totalDistributedAmount = totalDistributed;
        pendingWithdrawalsAmount = totalPendingWithdrawals;

        if (address(mpToken) != address(0)) {
            circulatingSupply = mpToken.totalSupply();
        }
    }

    /**
     * @notice Get deposits paginated
     */
    function getDepositsPaginated(uint256 offset, uint256 limit)
        external
        view
        returns (uint256[] memory ids, Deposit[] memory deps, uint256 total)
    {
        total = depositCounter;

        if (offset >= total) {
            return (new uint256[](0), new Deposit[](0), total);
        }

        uint256 end = offset + limit;
        if (end > total) end = total;

        uint256 count = end - offset;
        ids = new uint256[](count);
        deps = new Deposit[](count);

        for (uint256 i = 0; i < count; i++) {
            ids[i] = offset + i;
            deps[i] = deposits[offset + i];
        }
    }

    /**
     * @notice Get withdrawals paginated
     */
    function getWithdrawalsPaginated(uint256 offset, uint256 limit)
        external
        view
        returns (uint256[] memory ids, Withdrawal[] memory wds, uint256 total)
    {
        total = withdrawalCounter;

        if (offset >= total) {
            return (new uint256[](0), new Withdrawal[](0), total);
        }

        uint256 end = offset + limit;
        if (end > total) end = total;

        uint256 count = end - offset;
        ids = new uint256[](count);
        wds = new Withdrawal[](count);

        for (uint256 i = 0; i < count; i++) {
            ids[i] = offset + i;
            wds[i] = withdrawals[offset + i];
        }
    }
}
