// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./IDistributionScheme.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title ManualDirectCalculatorMulti
 * @notice Multi-table distribution calculator with absolute amounts and dual UUID validation
 * @dev Production-ready contract for Bitcoin mining pool distribution
 *      Supports multiple independent tables (one per epoch) in a single contract.
 *      Each table can be created, populated, approved separately.
 *      Set activeTableId to use a specific table for calculations.
 *      No need for cloning or multiple deployments.
 * @custom:security-contact security@yourpool.com
 */
contract ManualDirectCalculatorMulti is IDistributionScheme, AccessControl {

    // ========================================
    // CONSTANTS
    // ========================================

    uint256 public constant MAX_RECIPIENTS = 10000;
    uint256 public constant MIN_AMOUNT = 1; // 1 satoshi
    uint256 public constant MAX_AMOUNT = type(uint64).max; // Bitcoin max: ~184 BTC
    uint256 public constant MAX_UUID_LENGTH = 128;
    uint256 public constant MIN_UUID_LENGTH = 1;

    bytes32 public constant AUDITOR_ROLE = keccak256("AUDITOR_ROLE");

    // ========================================
    // STRUCTURES
    // ========================================

    struct MinerRecord {
        address ethWallet;
        uint256 amountSats;
        string sharesUuid;     // UUID from 'shares' column
        string ownerUuid;      // UUID from 'owner' column
        bool isActive;
    }

    struct Table {
        address remainderRecipient;
        bool paused;
        bool approved;
        mapping(address => MinerRecord) miners;
        address[] minerList;
        mapping(bytes32 => bool) uniqueRecords;
        uint256 totalRequestedAmount;
    }

    // ========================================
    // STATE
    // ========================================

    /// @notice Mapping of tableId (epoch) to Table state
    mapping(uint256 => Table) internal tables;

    /// @notice Current active table ID for calculations
    uint256 public activeTableId;

    /// @notice Counter for next table ID
    uint256 public nextTableId;

    // ========================================
    // EVENTS
    // ========================================

    event TableCreated(uint256 indexed tableId, address remainderRecipient);
    event ActiveTableSet(uint256 indexed tableId);
    event RemainderRecipientUpdated(uint256 indexed tableId, address indexed oldRecipient, address indexed newRecipient);
    event MinerAdded(uint256 indexed tableId, address indexed wallet, uint256 amount, string sharesUuid, string ownerUuid);
    event MinerUpdated(uint256 indexed tableId, address indexed wallet, uint256 oldAmount, uint256 newAmount);
    event MinerRemoved(uint256 indexed tableId, address indexed wallet);
    event Paused(uint256 indexed tableId, address indexed by);
    event Unpaused(uint256 indexed tableId, address indexed by);
    event TableApproved(uint256 indexed tableId, address indexed auditor);
    event AuditorAdded(address indexed auditor);
    event AuditorRemoved(address indexed auditor);

    // ========================================
    // MODIFIERS
    // ========================================

    modifier onlyAdmin() {
        _checkRole(DEFAULT_ADMIN_ROLE);
        _;
    }

    modifier validTable(uint256 tableId) {
        require(tableId < nextTableId, "Invalid table ID");
        _;
    }

    modifier whenTableNotPaused(uint256 tableId) {
        require(!tables[tableId].paused, "Table is paused");
        _;
    }

    modifier whenTableNotApproved(uint256 tableId) {
        require(!tables[tableId].approved, "Table is approved and locked");
        _;
    }

    // ========================================
    // CONSTRUCTOR
    // ========================================

    constructor() {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
    }

    // ========================================
    // TABLE MANAGEMENT
    // ========================================

    /**
     * @notice Create a new table for an epoch
     * @param remainderRecipient Address for remainders in this table
     * @return tableId The ID of the new table
     */
    function createTable(address remainderRecipient) external onlyAdmin returns (uint256 tableId) {
        require(remainderRecipient != address(0), "Invalid remainder recipient");

        tableId = nextTableId++;
        Table storage table = tables[tableId];
        table.remainderRecipient = remainderRecipient;
        table.paused = false;
        table.approved = false;
        table.totalRequestedAmount = 0;

        emit TableCreated(tableId, remainderRecipient);
    }

    /**
     * @notice Set the active table for calculations
     * @param tableId The table ID to activate
     */
    function setActiveTable(uint256 tableId) external onlyAdmin validTable(tableId) {
        activeTableId = tableId;
        emit ActiveTableSet(tableId);
    }

    // ========================================
    // ROLE MANAGEMENT
    // ========================================

    /**
     * @notice Add an auditor (admin only)
     */
    function addAuditor(address auditor) external onlyAdmin {
        require(auditor != address(0), "Invalid address");
        grantRole(AUDITOR_ROLE, auditor);
        emit AuditorAdded(auditor);
    }

    /**
     * @notice Remove an auditor (admin only)
     */
    function removeAuditor(address auditor) external onlyAdmin {
        revokeRole(AUDITOR_ROLE, auditor);
        emit AuditorRemoved(auditor);
    }

    // ========================================
    // APPROVAL
    // ========================================

    /**
     * @notice Approve a specific table (auditor only, irreversible)
     * @dev Locks the table against further modifications
     * @param tableId The table to approve
     */
    function approveTable(uint256 tableId) external validTable(tableId) {
        _checkRole(AUDITOR_ROLE);
        Table storage table = tables[tableId];
        require(!table.approved, "Already approved");
        require(table.minerList.length > 0, "No miners to approve");
        table.approved = true;
        emit TableApproved(tableId, msg.sender);
    }

    /**
     * @notice Check if a specific table is approved
     * @param tableId The table to check
     */
    function isTableApproved(uint256 tableId) external view validTable(tableId) returns (bool) {
        return tables[tableId].approved;
    }

    // For compatibility with SimpleDistributionTable
    function isTableApproved() external view returns (bool) {
        return tables[activeTableId].approved;
    }

    // ========================================
    // MINER MANAGEMENT
    // ========================================

    /**
     * @notice Add miner to a specific table with validation
     * @param tableId The table (epoch) to add to
     * @param ethWallet Ethereum address
     * @param amountSats Absolute amount in satoshis
     * @param sharesUuid UUID from 'shares' column
     * @param ownerUuid UUID from 'owner' column
     */
    function addMiner(
        uint256 tableId,
        address ethWallet,
        uint256 amountSats,
        string calldata sharesUuid,
        string calldata ownerUuid
    ) external onlyAdmin validTable(tableId) whenTableNotApproved(tableId) {
        Table storage table = tables[tableId];

        // Address validation
        require(ethWallet != address(0), "Zero address");
        require(ethWallet != table.remainderRecipient, "Cannot be remainder recipient");
        require(!table.miners[ethWallet].isActive, "Address already exists");

        // Amount validation
        require(amountSats >= MIN_AMOUNT, "Amount below minimum");
        require(amountSats <= MAX_AMOUNT, "Amount exceeds maximum");

        // UUID validation
        require(
            bytes(sharesUuid).length >= MIN_UUID_LENGTH &&
            bytes(sharesUuid).length <= MAX_UUID_LENGTH,
            "Invalid sharesUuid length"
        );
        require(
            bytes(ownerUuid).length >= MIN_UUID_LENGTH &&
            bytes(ownerUuid).length <= MAX_UUID_LENGTH,
            "Invalid ownerUuid length"
        );

        // List size check
        require(table.minerList.length < MAX_RECIPIENTS, "Too many miners");

        // Uniqueness check
        bytes32 recordHash = keccak256(abi.encodePacked(ethWallet, sharesUuid, ownerUuid));
        require(!table.uniqueRecords[recordHash], "Duplicate record");

        // Add record (Solidity 0.8+ has built-in overflow protection)
        uint256 newTotal = table.totalRequestedAmount + amountSats;

        table.miners[ethWallet] = MinerRecord({
            ethWallet: ethWallet,
            amountSats: amountSats,
            sharesUuid: sharesUuid,
            ownerUuid: ownerUuid,
            isActive: true
        });

        table.minerList.push(ethWallet);
        table.uniqueRecords[recordHash] = true;
        table.totalRequestedAmount = newTotal;

        emit MinerAdded(tableId, ethWallet, amountSats, sharesUuid, ownerUuid);
    }

    /**
     * @notice Update miner amount in a specific table
     * @param tableId The table to update
     * @param ethWallet The wallet to update
     * @param newAmount New amount in satoshis
     */
    function updateMinerAmount(
        uint256 tableId,
        address ethWallet,
        uint256 newAmount
    ) external onlyAdmin validTable(tableId) whenTableNotApproved(tableId) {
        Table storage table = tables[tableId];
        require(table.miners[ethWallet].isActive, "Miner not found");
        require(newAmount >= MIN_AMOUNT, "Amount below minimum");
        require(newAmount <= MAX_AMOUNT, "Amount exceeds maximum");

        uint256 oldAmount = table.miners[ethWallet].amountSats;

        // Update total (Solidity 0.8+ has built-in overflow protection)
        if (newAmount > oldAmount) {
            table.totalRequestedAmount += (newAmount - oldAmount);
        } else {
            table.totalRequestedAmount -= (oldAmount - newAmount);
        }

        table.miners[ethWallet].amountSats = newAmount;

        emit MinerUpdated(tableId, ethWallet, oldAmount, newAmount);
    }

    /**
     * @notice Remove miner from a specific table
     * @param tableId The table to remove from
     * @param ethWallet The wallet to remove
     */
    function removeMiner(
        uint256 tableId,
        address ethWallet
    ) external onlyAdmin validTable(tableId) whenTableNotApproved(tableId) {
        Table storage table = tables[tableId];
        require(table.miners[ethWallet].isActive, "Miner not found");

        // Remove from uniqueness mapping
        bytes32 recordHash = keccak256(abi.encodePacked(
            ethWallet,
            table.miners[ethWallet].sharesUuid,
            table.miners[ethWallet].ownerUuid
        ));
        delete table.uniqueRecords[recordHash];

        // Update total
        table.totalRequestedAmount -= table.miners[ethWallet].amountSats;

        // Deactivate miner
        table.miners[ethWallet].isActive = false;

        // Remove from list
        for (uint256 i = 0; i < table.minerList.length; i++) {
            if (table.minerList[i] == ethWallet) {
                table.minerList[i] = table.minerList[table.minerList.length - 1];
                table.minerList.pop();
                break;
            }
        }

        emit MinerRemoved(tableId, ethWallet);
    }

    // ========================================
    // DISTRIBUTION CALCULATION (internal)
    // ========================================

    function _calculateInternal(uint256 totalAmount)
        internal view
        returns (
            DistributionResult[] memory results,
            uint256 distributedAmount,
            uint256 remainder
        )
    {
        Table storage table = tables[activeTableId];

        // Validate
        (bool isValid, string memory error) = validateInput(totalAmount, new WorkerData[](0), SchemeParams(0, 0));
        require(isValid, error);

        // Require approved
        require(table.approved, "Table not approved");

        // Use active table
        uint256 activeCount = table.minerList.length;
        require(activeCount > 0, "No active miners");

        uint256 totalRequested = table.totalRequestedAmount;
        require(totalRequested <= totalAmount, "Requested exceeds total");

        // Create results array (+1 for remainder if needed)
        remainder = totalAmount - totalRequested;
        bool hasRemainder = remainder > 0;
        uint256 resultsCount = hasRemainder ? activeCount + 1 : activeCount;
        results = new DistributionResult[](resultsCount);

        // Distribute
        uint256 resultIndex = 0;
        for (uint256 i = 0; i < activeCount; i++) {
            address wallet = table.minerList[i];
            MinerRecord memory miner = table.miners[wallet];
            uint256 amount = miner.amountSats;

            results[resultIndex] = DistributionResult({
                recipient: wallet,
                amount: amount,
                workerId: miner.ownerUuid,
                percentage: (amount * 10000) / totalAmount // Basis points
            });

            distributedAmount += amount;
            resultIndex++;
        }

        // Add remainder if any
        if (hasRemainder) {
            results[resultIndex] = DistributionResult({
                recipient: table.remainderRecipient,
                amount: remainder,
                workerId: "REMAINDER",
                percentage: (remainder * 10000) / totalAmount
            });
        }

        // Sanity check
        require(distributedAmount + remainder == totalAmount, "Math error");
    }

    // ========================================
    // DISTRIBUTION CALCULATION (external)
    // ========================================

    /**
     * @notice Calculate distribution for the active table
     * @dev Uses activeTableId; ignores workers and params
     */
    function calculate(
        uint256 totalAmount,
        WorkerData[] memory /*workers*/,
        SchemeParams memory /*params*/
    ) external view override returns (
        DistributionResult[] memory results,
        uint256 distributedAmount,
        uint256 remainder
    ) {
        return _calculateInternal(totalAmount);
    }

    /**
     * @notice Calculate for members (delegates to calculate)
     */
    function calculateForMembers(
        uint256 totalAmount,
        MemberData[] memory /*members*/,
        SchemeParams memory /*params*/
    ) external view override returns (
        DistributionResult[] memory results,
        uint256 distributedAmount,
        uint256 remainder
    ) {
        return _calculateInternal(totalAmount);
    }

    /**
     * @notice Simplified interface for ApiMiningPool
     * @dev Uses activeTableId. Returns amounts in satoshis (same units as stored).
     */
    function calculateDistribution(uint256 totalAmount)
        external view
        returns (address[] memory recipients, uint256[] memory amounts)
    {
        (DistributionResult[] memory results,,) = _calculateInternal(totalAmount);

        recipients = new address[](results.length);
        amounts = new uint256[](results.length);

        for (uint256 i = 0; i < results.length; i++) {
            recipients[i] = results[i].recipient;
            amounts[i] = results[i].amount;
        }
    }

    // ========================================
    // VALIDATION
    // ========================================

    function validateInput(
        uint256 totalAmount,
        WorkerData[] memory,
        SchemeParams memory
    ) public view override returns (bool isValid, string memory errorMessage) {
        Table storage table = tables[activeTableId];

        if (totalAmount == 0) {
            return (false, "Total amount is zero");
        }

        if (table.minerList.length == 0) {
            return (false, "No active miners");
        }

        if (table.remainderRecipient == address(0)) {
            return (false, "Remainder recipient not set");
        }

        if (table.paused) {
            return (false, "Table is paused");
        }

        uint256 calculatedTotal = 0;
        for (uint256 i = 0; i < table.minerList.length; i++) {
            address wallet = table.minerList[i];
            MinerRecord memory m = table.miners[wallet];
            if (!m.isActive) {
                return (false, "Inactive miner in list");
            }
            if (m.amountSats < MIN_AMOUNT || m.amountSats > MAX_AMOUNT) {
                return (false, "Invalid miner amount");
            }
            calculatedTotal += m.amountSats;
        }

        if (calculatedTotal != table.totalRequestedAmount) {
            return (false, "Cached total mismatch");
        }

        if (calculatedTotal > totalAmount) {
            return (false, "Requested exceeds available");
        }

        return (true, "");
    }

    function validateMemberInput(
        uint256 totalAmount,
        MemberData[] memory,
        SchemeParams memory params
    ) public view override returns (bool isValid, string memory errorMessage) {
        return validateInput(totalAmount, new WorkerData[](0), params);
    }

    // ========================================
    // INFORMATION
    // ========================================

    function getSchemeInfo() external pure override returns (
        string memory name,
        string memory version,
        string memory description
    ) {
        return (
            "ManualDirectMultiTable",
            "1.0.0",
            "Multi-table direct amounts with dual UUID validation and auditor approval"
        );
    }

    function estimateGas(
        uint256 workerCount,
        SchemeParams memory
    ) external pure override returns (uint256) {
        return 50000 + (workerCount * 5000);
    }

    // ========================================
    // ADMINISTRATION
    // ========================================

    /**
     * @notice Set remainder recipient for a specific table
     * @param tableId The table to update
     * @param newRecipient New remainder recipient
     */
    function setRemainderRecipient(
        uint256 tableId,
        address newRecipient
    ) external onlyAdmin validTable(tableId) whenTableNotApproved(tableId) {
        require(newRecipient != address(0), "Zero address");
        Table storage table = tables[tableId];
        address oldRecipient = table.remainderRecipient;
        table.remainderRecipient = newRecipient;
        emit RemainderRecipientUpdated(tableId, oldRecipient, newRecipient);
    }

    /**
     * @notice Pause a specific table
     * @param tableId The table to pause
     */
    function pause(uint256 tableId) external onlyAdmin validTable(tableId) {
        Table storage table = tables[tableId];
        require(!table.paused, "Already paused");
        table.paused = true;
        emit Paused(tableId, msg.sender);
    }

    /**
     * @notice Unpause a specific table
     * @param tableId The table to unpause
     */
    function unpause(uint256 tableId) external onlyAdmin validTable(tableId) {
        Table storage table = tables[tableId];
        require(table.paused, "Not paused");
        table.paused = false;
        emit Unpaused(tableId, msg.sender);
    }

    // ========================================
    // VIEWS
    // ========================================

    /**
     * @notice Get miner info for a specific table and wallet
     * @param tableId The table ID
     * @param wallet The wallet address
     */
    function getMinerInfo(uint256 tableId, address wallet) external view validTable(tableId) returns (
        uint256 amountSats,
        string memory sharesUuid,
        string memory ownerUuid,
        bool isActive
    ) {
        MinerRecord memory m = tables[tableId].miners[wallet];
        return (m.amountSats, m.sharesUuid, m.ownerUuid, m.isActive);
    }

    /**
     * @notice Get active miner count for a specific table
     * @param tableId The table ID
     */
    function getActiveMinerCount(uint256 tableId) external view validTable(tableId) returns (uint256) {
        return tables[tableId].minerList.length;
    }

    /**
     * @notice Get all miners for a specific table
     * @param tableId The table ID
     */
    function getAllMiners(uint256 tableId) external view validTable(tableId) returns (address[] memory) {
        return tables[tableId].minerList;
    }

    /**
     * @notice Get total requested amount for a specific table
     * @param tableId The table ID
     */
    function getTotalRequestedAmount(uint256 tableId) external view validTable(tableId) returns (uint256) {
        return tables[tableId].totalRequestedAmount;
    }

    /**
     * @notice Check if record is unique in a specific table
     * @param tableId The table ID
     * @param ethWallet Ethereum address
     * @param sharesUuid Shares UUID
     * @param ownerUuid Owner UUID
     */
    function isRecordUnique(
        uint256 tableId,
        address ethWallet,
        string calldata sharesUuid,
        string calldata ownerUuid
    ) external view validTable(tableId) returns (bool) {
        bytes32 recordHash = keccak256(abi.encodePacked(ethWallet, sharesUuid, ownerUuid));
        return !tables[tableId].uniqueRecords[recordHash];
    }

    /**
     * @notice Get remainder recipient for a specific table
     * @param tableId The table ID
     */
    function getRemainderRecipient(uint256 tableId) external view validTable(tableId) returns (address) {
        return tables[tableId].remainderRecipient;
    }

    /**
     * @notice Get paused status for a specific table
     * @param tableId The table ID
     */
    function getPaused(uint256 tableId) external view validTable(tableId) returns (bool) {
        return tables[tableId].paused;
    }
}
