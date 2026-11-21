// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title SimpleDistributionTable
 * @notice Table-based reward distribution for demo (no oracles, no calculators)
 * @dev Owner manually sets shares for each miner
 * @dev Includes AUDITOR role for read-only validation and monitoring
 */
contract SimpleDistributionTable is Ownable, AccessControl {
    // ======== ROLES ========

    bytes32 public constant AUDITOR_ROLE = keccak256("AUDITOR_ROLE");
    // ======== STATE ========

    struct MinerShares {
        address minerAddress;
        uint256 shares;
        string minerId; // Optional identifier
        bool isActive;
    }

    // Miner address => shares
    mapping(address => MinerShares) public miners;
    address[] public minerList;
    uint256 public totalShares;

    // Table approval (required by ApiMiningPool before distribution)
    bool internal _tableApproved;

    // ======== EVENTS ========

    event MinerAdded(address indexed miner, uint256 shares, string minerId);
    event MinerUpdated(address indexed miner, uint256 oldShares, uint256 newShares);
    event MinerRemoved(address indexed miner);
    event SharesRecalculated(uint256 totalShares);
    event AuditorAdded(address indexed auditor);
    event AuditorRemoved(address indexed auditor);
    event TableApprovedEvent(address indexed approver);

    // ======== CONSTRUCTOR ========

    constructor() Ownable(msg.sender) {
        // Grant DEFAULT_ADMIN_ROLE to deployer (owner)
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
    }

    // ======== MANAGEMENT ========

    function addMiner(
        address minerAddress,
        uint256 shares,
        string calldata minerId
    ) external onlyOwner {
        require(!_tableApproved, "Table is approved and locked");
        require(minerAddress != address(0), "Invalid address");
        require(shares > 0, "Shares must be > 0");
        require(!miners[minerAddress].isActive, "Miner already exists");

        miners[minerAddress] = MinerShares({
            minerAddress: minerAddress,
            shares: shares,
            minerId: minerId,
            isActive: true
        });

        minerList.push(minerAddress);
        totalShares += shares;

        emit MinerAdded(minerAddress, shares, minerId);
    }

    function updateMinerShares(address minerAddress, uint256 newShares) external onlyOwner {
        require(!_tableApproved, "Table is approved and locked");
        require(miners[minerAddress].isActive, "Miner not found");
        require(newShares > 0, "Shares must be > 0");

        uint256 oldShares = miners[minerAddress].shares;
        totalShares = totalShares - oldShares + newShares;
        miners[minerAddress].shares = newShares;

        emit MinerUpdated(minerAddress, oldShares, newShares);
    }

    function removeMiner(address minerAddress) external onlyOwner {
        require(!_tableApproved, "Table is approved and locked");
        require(miners[minerAddress].isActive, "Miner not found");

        totalShares -= miners[minerAddress].shares;
        miners[minerAddress].isActive = false;

        // Remove from list
        for (uint256 i = 0; i < minerList.length; i++) {
            if (minerList[i] == minerAddress) {
                minerList[i] = minerList[minerList.length - 1];
                minerList.pop();
                break;
            }
        }

        emit MinerRemoved(minerAddress);
    }

    // ======== TABLE APPROVAL ========

    /**
     * @notice Approve the table (owner or auditor, irreversible)
     * @dev After approval, miners cannot be added/updated/removed
     */
    function approveTable() external {
        require(
            hasRole(AUDITOR_ROLE, msg.sender) || owner() == msg.sender,
            "Only auditor or owner"
        );
        require(!_tableApproved, "Already approved");
        require(totalShares > 0, "No shares to approve");
        _tableApproved = true;
        emit TableApprovedEvent(msg.sender);
    }

    /**
     * @notice Check if the table is approved
     */
    function isTableApproved() external view returns (bool) {
        return _tableApproved;
    }

    // ======== DISTRIBUTION CALCULATION ========

    function calculateDistribution(uint256 totalAmount)
        external
        view
        returns (address[] memory recipients, uint256[] memory amounts)
    {
        require(totalShares > 0, "No shares");

        uint256 activeCount = 0;
        for (uint256 i = 0; i < minerList.length; i++) {
            if (miners[minerList[i]].isActive) {
                activeCount++;
            }
        }

        recipients = new address[](activeCount);
        amounts = new uint256[](activeCount);

        uint256 index = 0;
        uint256 distributed = 0;

        for (uint256 i = 0; i < minerList.length; i++) {
            address miner = minerList[i];
            if (miners[miner].isActive) {
                uint256 amount = (totalAmount * miners[miner].shares) / totalShares;
                recipients[index] = miner;
                amounts[index] = amount;
                distributed += amount;
                index++;
            }
        }

        // Handle remainder - give to first recipient
        if (distributed < totalAmount && activeCount > 0) {
            amounts[0] += (totalAmount - distributed);
        }
    }

    // ======== VIEWS ========

    function getMinerCount() external view returns (uint256) {
        uint256 count = 0;
        for (uint256 i = 0; i < minerList.length; i++) {
            if (miners[minerList[i]].isActive) {
                count++;
            }
        }
        return count;
    }

    function getAllMiners() external view returns (address[] memory) {
        uint256 activeCount = 0;
        for (uint256 i = 0; i < minerList.length; i++) {
            if (miners[minerList[i]].isActive) {
                activeCount++;
            }
        }

        address[] memory activeMiners = new address[](activeCount);
        uint256 index = 0;

        for (uint256 i = 0; i < minerList.length; i++) {
            if (miners[minerList[i]].isActive) {
                activeMiners[index] = minerList[i];
                index++;
            }
        }

        return activeMiners;
    }

    function getMinerShares(address minerAddress) external view returns (uint256) {
        return miners[minerAddress].shares;
    }

    function isMinerActive(address minerAddress) external view returns (bool) {
        return miners[minerAddress].isActive;
    }

    // ======== AUDITOR MANAGEMENT ========

    /**
     * @notice Add an auditor (only owner)
     * @param auditor Address of the auditor to add
     */
    function addAuditor(address auditor) external onlyOwner {
        require(auditor != address(0), "Invalid auditor address");
        grantRole(AUDITOR_ROLE, auditor);
        emit AuditorAdded(auditor);
    }

    /**
     * @notice Remove an auditor (only owner)
     * @param auditor Address of the auditor to remove
     */
    function removeAuditor(address auditor) external onlyOwner {
        revokeRole(AUDITOR_ROLE, auditor);
        emit AuditorRemoved(auditor);
    }

    /**
     * @notice Check if an address is an auditor
     * @param account Address to check
     */
    function isAuditor(address account) external view returns (bool) {
        return hasRole(AUDITOR_ROLE, account);
    }

    // ======== AUDITOR FUNCTIONS ========

    struct AuditReport {
        uint256 totalMiners;
        uint256 activeMiners;
        uint256 inactiveMiners;
        uint256 totalShares;
        uint256 minShares;
        uint256 maxShares;
        uint256 avgShares;
        bool hasZeroShares;
        bool hasDuplicates;
        address[] zeroShareMiners;
        string[] warnings;
    }

    /**
     * @notice Generate comprehensive audit report
     * @dev Available to auditors and owner
     * @return Detailed audit report with statistics and warnings
     */
    function generateAuditReport() external view returns (AuditReport memory) {
        require(
            hasRole(AUDITOR_ROLE, msg.sender) || owner() == msg.sender,
            "Only auditor or owner"
        );

        AuditReport memory report;

        // Count active/inactive miners
        uint256 activeCount = 0;
        uint256 inactiveCount = 0;
        uint256 minShares = type(uint256).max;
        uint256 maxShares = 0;
        uint256 sumShares = 0;

        address[] memory tempZeroShares = new address[](minerList.length);
        uint256 zeroCount = 0;

        for (uint256 i = 0; i < minerList.length; i++) {
            address miner = minerList[i];
            MinerShares memory minerData = miners[miner];

            if (minerData.isActive) {
                activeCount++;
                sumShares += minerData.shares;

                if (minerData.shares < minShares) {
                    minShares = minerData.shares;
                }
                if (minerData.shares > maxShares) {
                    maxShares = minerData.shares;
                }
                if (minerData.shares == 0) {
                    tempZeroShares[zeroCount] = miner;
                    zeroCount++;
                }
            } else {
                inactiveCount++;
            }
        }

        report.totalMiners = minerList.length;
        report.activeMiners = activeCount;
        report.inactiveMiners = inactiveCount;
        report.totalShares = totalShares;
        report.minShares = activeCount > 0 ? minShares : 0;
        report.maxShares = maxShares;
        report.avgShares = activeCount > 0 ? sumShares / activeCount : 0;
        report.hasZeroShares = zeroCount > 0;

        // Copy zero shares miners
        if (zeroCount > 0) {
            report.zeroShareMiners = new address[](zeroCount);
            for (uint256 i = 0; i < zeroCount; i++) {
                report.zeroShareMiners[i] = tempZeroShares[i];
            }
        }

        // Check for potential duplicates (same shares)
        report.hasDuplicates = _checkDuplicateShares();

        // Generate warnings
        report.warnings = _generateWarnings(report);

        return report;
    }

    /**
     * @notice Validate table integrity
     * @dev Checks that totalShares matches sum of active miner shares
     * @return isValid True if table is valid
     * @return mismatch Difference between totalShares and calculated sum
     */
    function validateTableIntegrity() external view returns (bool isValid, uint256 mismatch) {
        require(
            hasRole(AUDITOR_ROLE, msg.sender) || owner() == msg.sender,
            "Only auditor or owner"
        );

        uint256 calculatedSum = 0;
        for (uint256 i = 0; i < minerList.length; i++) {
            if (miners[minerList[i]].isActive) {
                calculatedSum += miners[minerList[i]].shares;
            }
        }

        isValid = (calculatedSum == totalShares);
        mismatch = calculatedSum > totalShares
            ? calculatedSum - totalShares
            : totalShares - calculatedSum;
    }

    /**
     * @notice Get detailed miner information (for auditing)
     * @param minerAddress Miner address
     * @return minerData Full miner data
     */
    function getMinerDetails(address minerAddress)
        external
        view
        returns (MinerShares memory minerData)
    {
        require(
            hasRole(AUDITOR_ROLE, msg.sender) || owner() == msg.sender,
            "Only auditor or owner"
        );
        return miners[minerAddress];
    }

    /**
     * @notice Get all miners with their full details (for auditing)
     * @return allMiners Array of all miner data
     */
    function getAllMinerDetails()
        external
        view
        returns (MinerShares[] memory allMiners)
    {
        require(
            hasRole(AUDITOR_ROLE, msg.sender) || owner() == msg.sender,
            "Only auditor or owner"
        );

        allMiners = new MinerShares[](minerList.length);
        for (uint256 i = 0; i < minerList.length; i++) {
            allMiners[i] = miners[minerList[i]];
        }
    }

    // ======== INTERNAL HELPERS ========

    /**
     * @notice Check if there are miners with identical shares
     * @dev This might indicate configuration errors
     */
    function _checkDuplicateShares() internal view returns (bool) {
        for (uint256 i = 0; i < minerList.length; i++) {
            if (!miners[minerList[i]].isActive) continue;

            for (uint256 j = i + 1; j < minerList.length; j++) {
                if (!miners[minerList[j]].isActive) continue;

                if (miners[minerList[i]].shares == miners[minerList[j]].shares) {
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * @notice Generate warnings based on audit report
     */
    function _generateWarnings(AuditReport memory report)
        internal
        pure
        returns (string[] memory)
    {
        string[] memory tempWarnings = new string[](10);
        uint256 warningCount = 0;

        if (report.activeMiners == 0) {
            tempWarnings[warningCount] = "No active miners";
            warningCount++;
        }

        if (report.totalShares == 0) {
            tempWarnings[warningCount] = "Total shares is zero";
            warningCount++;
        }

        if (report.hasZeroShares) {
            tempWarnings[warningCount] = "Some miners have zero shares";
            warningCount++;
        }

        if (report.hasDuplicates) {
            tempWarnings[warningCount] = "Multiple miners have identical shares";
            warningCount++;
        }

        if (report.maxShares > report.avgShares * 10 && report.activeMiners > 1) {
            tempWarnings[warningCount] = "Large share concentration (max > 10x average)";
            warningCount++;
        }

        // Copy to result array
        string[] memory result = new string[](warningCount);
        for (uint256 i = 0; i < warningCount; i++) {
            result[i] = tempWarnings[i];
        }

        return result;
    }
}
