// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../FROSTAuthorization.sol";

/**
 * @title FeeDistributionCalculator
 * @notice Calculates fee distribution between members and custodians
 * @dev Supports equal and percentage-based distribution modes.
 *      Project wallets get their share BEFORE this split (handled by FeeManager).
 */
contract FeeDistributionCalculator {
    enum DistributionMode {
        Equal,
        ByPercentage
    }

    struct DistributionConfig {
        uint256 memberShareBps; // e.g. 7000 = 70%
        uint256 custodianShareBps; // e.g. 3000 = 30%
        DistributionMode memberMode;
        DistributionMode custodianMode;
    }

    struct Recipient {
        address addr;
        uint256 weight; // Used in ByPercentage mode
    }

    struct Result {
        address recipient;
        uint256 amount;
    }

    /// @notice FROST multisig for governance
    FROSTAuthorization public immutable frostMultisig;

    /// @notice Distribution configuration
    DistributionConfig public config;
    uint256 public operationNonce;

    // ======== EVENTS ========

    event ConfigUpdated(uint256 memberShareBps, uint256 custodianShareBps);

    // ======== ERRORS ========

    error NoRecipients();
    error InvalidConfig();
    error InsufficientFROSTSignatures();

    // ======== CONSTRUCTOR ========

    constructor(address _frostMultisig, DistributionConfig memory _config) {
        require(_frostMultisig != address(0), "Invalid FROST address");
        if (_config.memberShareBps + _config.custodianShareBps > 10000) revert InvalidConfig();

        frostMultisig = FROSTAuthorization(_frostMultisig);
        config = _config;
    }

    // ======== DISTRIBUTION CALCULATION ========

    /**
     * @notice Calculate fee distribution between members and custodians
     * @param total Total amount to distribute
     * @param members Array of member recipients with weights
     * @param custodians Array of custodian recipients with weights
     * @return results Array of (recipient, amount) pairs
     * @return distributed Total amount distributed
     * @return remainder Amount left over (due to rounding)
     */
    function calculateDistribution(uint256 total, Recipient[] calldata members, Recipient[] calldata custodians)
        external
        view
        returns (Result[] memory results, uint256 distributed, uint256 remainder)
    {
        // Validate: if share > 0, group must have recipients
        if (config.memberShareBps > 0 && members.length == 0) revert NoRecipients();
        if (config.custodianShareBps > 0 && custodians.length == 0) revert NoRecipients();

        uint256 memberTotal = total * config.memberShareBps / 10000;
        uint256 custodianTotal = total * config.custodianShareBps / 10000;

        // Calculate individual distributions
        Result[] memory memberResults = _distributeGroup(members, memberTotal, config.memberMode);
        Result[] memory custodianResults = _distributeGroup(custodians, custodianTotal, config.custodianMode);

        // Combine results
        results = new Result[](memberResults.length + custodianResults.length);
        uint256 idx = 0;
        for (uint256 i = 0; i < memberResults.length; i++) {
            results[idx++] = memberResults[i];
            distributed += memberResults[i].amount;
        }
        for (uint256 i = 0; i < custodianResults.length; i++) {
            results[idx++] = custodianResults[i];
            distributed += custodianResults[i].amount;
        }

        remainder = total - distributed;
    }

    // ======== GOVERNANCE ========

    /**
     * @notice Update distribution configuration
     * @param _config New configuration
     * @param operationHash FROST-signed operation hash
     */
    function setConfig(DistributionConfig calldata _config, bytes32 operationHash) external {
        if (_config.memberShareBps + _config.custodianShareBps > 10000) revert InvalidConfig();
        require(operationHash == getSetConfigOperationHash(_config), "Operation hash mismatch");

        if (!frostMultisig.isOperationApproved(operationHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }
        frostMultisig.consumeOperation(operationHash);
        operationNonce++;

        config = _config;

        emit ConfigUpdated(_config.memberShareBps, _config.custodianShareBps);
    }

    function getSetConfigOperationHash(DistributionConfig memory _config) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setConfig.selector, keccak256(abi.encode(_config)), operationNonce
        );
    }

    // ======== INTERNAL ========

    function _distributeGroup(Recipient[] calldata recipients, uint256 total, DistributionMode mode)
        internal
        pure
        returns (Result[] memory results)
    {
        if (recipients.length == 0) {
            return new Result[](0);
        }

        results = new Result[](recipients.length);

        if (mode == DistributionMode.Equal) {
            uint256 perRecipient = total / recipients.length;
            for (uint256 i = 0; i < recipients.length; i++) {
                results[i] = Result({recipient: recipients[i].addr, amount: perRecipient});
            }
        } else {
            // ByPercentage: distribute proportional to weight
            uint256 totalWeight = 0;
            for (uint256 i = 0; i < recipients.length; i++) {
                totalWeight += recipients[i].weight;
            }
            require(totalWeight > 0, "Total weight is zero");

            for (uint256 i = 0; i < recipients.length; i++) {
                results[i] = Result({recipient: recipients[i].addr, amount: total * recipients[i].weight / totalWeight});
            }
        }
    }

    // ======== VIEW FUNCTIONS ========

    function getConfig() external view returns (DistributionConfig memory) {
        return config;
    }
}
