// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./interfaces/ICustodianValidator.sol";
import "../FROSTAuthorization.sol";

interface ICustodianCandidateStaking {
    function CUSTODIAN_THRESHOLD() external view returns (uint256);
    function isCustodianCandidate(address owner) external view returns (bool);
    function getCustodianCandidates(uint256 offset, uint256 limit)
        external
        view
        returns (address[] memory candidates, uint256 nextOffset);
}

/**
 * @title CustodianValidator
 * @notice Validates custodian signatures for confederation operations
 * @dev Custodians are institutional entities that provide additional security layer.
 *      Operations in a confederation require BOTH FROST threshold AND minimum custodian signatures.
 */
contract CustodianValidator is ICustodianValidator {
    // ======== STATE ========

    struct CustodianConfig {
        address[] custodians;
        uint256 minRequired;
        mapping(address => bool) isCustodian;
    }

    CustodianConfig private config;

    // Bind each approval to the signer's current membership. Removing and later
    // re-adding the same wallet must not revive approvals from its prior tenure.
    mapping(address => uint256) public custodianGeneration;
    mapping(bytes32 => mapping(address => uint256)) private signatureGeneration;

    // Track executed management operations
    mapping(bytes32 => bool) public executed;
    mapping(bytes32 => bytes32) public operationReceipts;
    uint256 public operationNonce;

    // Reference to FROST multisig for management operations approval
    address public immutable frostMultisig;
    address public candidateStaking;
    event CandidateStakingBound(address indexed staking);

    // ======== ERRORS ========

    error NotACustodian();
    error AlreadySigned();
    error InvalidAddress();
    error CustodianAlreadyExists();
    error CustodianNotFound();
    error InvalidMinRequired();
    error ApprovalNotMet();
    error AlreadyExecuted();
    error WouldBreakMinimum();
    error CandidateSourceAlreadyBound();
    error NotEligibleCandidate();

    // ======== CONSTRUCTOR ========

    /**
     * @notice Initialize the custodian validator
     * @param initialCustodians Array of initial custodian addresses
     * @param minRequired_ Minimum custodian signatures required for operations
     * @param frostMultisig_ Address of the FROST multisig for management approvals
     */
    constructor(address[] memory initialCustodians, uint256 minRequired_, address frostMultisig_) {
        require(initialCustodians.length > 0, "No custodians");
        require(minRequired_ > 0 && minRequired_ <= initialCustodians.length, "Invalid min required");
        require(frostMultisig_ != address(0), "Invalid FROST address");

        frostMultisig = frostMultisig_;
        config.minRequired = minRequired_;

        for (uint256 i = 0; i < initialCustodians.length; i++) {
            address custodian = initialCustodians[i];
            require(custodian != address(0), "Invalid custodian address");
            require(!config.isCustodian[custodian], "Duplicate custodian");

            config.custodians.push(custodian);
            config.isCustodian[custodian] = true;
            custodianGeneration[custodian] = 1;
            emit CustodianAdded(custodian);
        }
    }

    // ======== MODIFIERS ========

    modifier onlyCustodian() {
        if (!config.isCustodian[msg.sender]) revert NotACustodian();
        _;
    }

    // ======== SIGNATURE FUNCTIONS ========

    /**
     * @notice Register a custodian's signature for an operation
     * @param operationHash The hash of the operation being signed
     */
    function registerCustodianSignature(bytes32 operationHash) external onlyCustodian {
        if (custodianSigned(operationHash, msg.sender)) revert AlreadySigned();

        signatureGeneration[operationHash][msg.sender] = custodianGeneration[msg.sender];

        emit CustodianSigned(operationHash, msg.sender);
    }

    /**
     * @notice Count how many custodians have signed an operation
     * @param operationHash The hash of the operation
     * @return The number of custodian signatures
     */
    function countCustodianSignatures(bytes32 operationHash) external view returns (uint256) {
        return custodianSignatureCount(operationHash);
    }

    /// @notice Preserve the original getter ABI while counting only active approvals.
    function custodianSignatureCount(bytes32 operationHash) public view returns (uint256 count) {
        for (uint256 i = 0; i < config.custodians.length; i++) {
            if (custodianSigned(operationHash, config.custodians[i])) count++;
        }
    }

    /// @notice An approval is valid only for the signer's uninterrupted membership.
    function custodianSigned(bytes32 operationHash, address custodian) public view returns (bool) {
        return config.isCustodian[custodian]
            && signatureGeneration[operationHash][custodian] == custodianGeneration[custodian];
    }

    /**
     * @notice Check if the minimum custodian threshold is met for an operation
     * @param operationHash The hash of the operation
     * @return True if minimum custodians have signed
     */
    function meetsMinimumCustodians(bytes32 operationHash) external view returns (bool) {
        return custodianSignatureCount(operationHash) >= config.minRequired;
    }

    /**
     * @notice Validate that custodian threshold is met (alias for meetsMinimumCustodians)
     * @param operationHash The hash of the operation
     * @return True if threshold is met
     */
    function validateCustodianThreshold(bytes32 operationHash) external view returns (bool) {
        return custodianSignatureCount(operationHash) >= config.minRequired;
    }

    // ======== CUSTODIAN MANAGEMENT ========

    /**
     * @notice Add a new custodian (requires FROST and custodian threshold approval)
     * @param custodian The address to add as custodian
     * @param approvalHash The operation hash that was approved via FROST
     */
    function addCustodian(address custodian, bytes32 approvalHash) external {
        _validateFROSTApproval(approvalHash);
        require(approvalHash == getAddCustodianOperationHash(custodian), "Operation hash mismatch");
        _validateCustodianApproval(approvalHash);

        if (custodian == address(0)) revert InvalidAddress();
        if (config.isCustodian[custodian]) revert CustodianAlreadyExists();
        if (
            candidateStaking != address(0)
                && !ICustodianCandidateStaking(candidateStaking).isCustodianCandidate(custodian)
        ) {
            revert NotEligibleCandidate();
        }

        FROSTAuthorization(frostMultisig).consumeOperation(approvalHash);
        executed[approvalHash] = true;
        operationReceipts[approvalHash] = keccak256(abi.encode(this.addCustodian.selector, custodian));
        operationNonce++;

        config.custodians.push(custodian);
        config.isCustodian[custodian] = true;
        custodianGeneration[custodian]++;

        emit CustodianAdded(custodian);
    }

    /**
     * @notice Remove a custodian (requires FROST and custodian threshold approval)
     * @param custodian The address to remove
     * @param approvalHash The operation hash that was approved via FROST
     */
    function removeCustodian(address custodian, bytes32 approvalHash) external {
        _validateFROSTApproval(approvalHash);
        require(approvalHash == getRemoveCustodianOperationHash(custodian), "Operation hash mismatch");
        _validateCustodianApproval(approvalHash);

        if (!config.isCustodian[custodian]) revert CustodianNotFound();
        if (config.custodians.length <= config.minRequired) revert WouldBreakMinimum();

        FROSTAuthorization(frostMultisig).consumeOperation(approvalHash);
        executed[approvalHash] = true;
        operationReceipts[approvalHash] = keccak256(abi.encode(this.removeCustodian.selector, custodian));
        operationNonce++;

        config.isCustodian[custodian] = false;

        // Remove from array
        for (uint256 i = 0; i < config.custodians.length; i++) {
            if (config.custodians[i] == custodian) {
                config.custodians[i] = config.custodians[config.custodians.length - 1];
                config.custodians.pop();
                break;
            }
        }

        emit CustodianRemoved(custodian);
    }

    /**
     * @notice Set minimum custodians required (requires FROST and custodian threshold approval)
     * @param minRequired The new minimum
     * @param approvalHash The operation hash that was approved via FROST
     */
    function setMinCustodians(uint256 minRequired, bytes32 approvalHash) external {
        _validateFROSTApproval(approvalHash);
        require(approvalHash == getSetMinCustodiansOperationHash(minRequired), "Operation hash mismatch");
        _validateCustodianApproval(approvalHash);

        if (minRequired == 0 || minRequired > config.custodians.length) {
            revert InvalidMinRequired();
        }

        FROSTAuthorization(frostMultisig).consumeOperation(approvalHash);
        executed[approvalHash] = true;
        operationReceipts[approvalHash] = keccak256(abi.encode(this.setMinCustodians.selector, minRequired));
        operationNonce++;

        config.minRequired = minRequired;

        emit MinCustodiansChanged(minRequired);
    }

    // ======== VIEW FUNCTIONS ========

    /**
     * @notice Check if an address is a registered custodian
     * @param account The address to check
     * @return True if the address is a custodian
     */
    function isCustodian(address account) external view returns (bool) {
        return config.isCustodian[account];
    }

    /**
     * @notice Get all registered custodians
     * @return Array of custodian addresses
     */
    function getCustodians() external view returns (address[] memory) {
        return config.custodians;
    }

    /**
     * @notice Get the minimum number of custodians required
     * @return The minimum custodian count
     */
    function getMinCustodiansRequired() external view returns (uint256) {
        return config.minRequired;
    }

    /// @notice Opt-in, one-time candidate source; admission remains FROST + active custodian approval.
    function bindCandidateStaking(address staking, bytes32 approvalHash) external {
        if (candidateStaking != address(0)) revert CandidateSourceAlreadyBound();
        if (staking.code.length == 0) revert InvalidAddress();
        uint256 candidateThreshold = ICustodianCandidateStaking(staking).CUSTODIAN_THRESHOLD();
        if (candidateThreshold == 0 || candidateThreshold > 10_000_000 ether) {
            revert InvalidAddress();
        }
        _validateFROSTApproval(approvalHash);
        require(approvalHash == getBindCandidateStakingOperationHash(staking), "Operation hash mismatch");
        _validateCustodianApproval(approvalHash);
        FROSTAuthorization(frostMultisig).consumeOperation(approvalHash);
        executed[approvalHash] = true;
        operationReceipts[approvalHash] = keccak256(abi.encode(this.bindCandidateStaking.selector, staking));
        ++operationNonce;
        candidateStaking = staking;
        emit CandidateStakingBound(staking);
    }

    function getBindCandidateStakingOperationHash(address staking) public view returns (bytes32) {
        return FROSTAuthorization(frostMultisig)
            .hashOperation(
                address(this), this.bindCandidateStaking.selector, keccak256(abi.encode(staking)), operationNonce
            );
    }

    function getCustodianCandidates(uint256 offset, uint256 limit)
        external
        view
        returns (address[] memory candidates, uint256 nextOffset)
    {
        if (candidateStaking == address(0)) return (new address[](0), 0);
        (address[] memory source, uint256 next) =
            ICustodianCandidateStaking(candidateStaking).getCustodianCandidates(offset, limit);
        uint256 count;
        for (uint256 i; i < source.length; ++i) {
            if (!config.isCustodian[source[i]]) ++count;
        }
        candidates = new address[](count);
        uint256 cursor;
        for (uint256 i; i < source.length; ++i) {
            if (!config.isCustodian[source[i]]) candidates[cursor++] = source[i];
        }
        return (candidates, next);
    }

    /**
     * @notice Get the total number of custodians
     * @return The custodian count
     */
    function getCustodianCount() external view returns (uint256) {
        return config.custodians.length;
    }

    /**
     * @notice Check if a custodian has signed an operation
     * @param operationHash The hash of the operation
     * @param custodian The custodian address
     * @return True if the custodian has signed
     */
    function hasCustodianSigned(bytes32 operationHash, address custodian) external view returns (bool) {
        return custodianSigned(operationHash, custodian);
    }

    /**
     * @notice Check if a management operation has been executed
     * @param operationHash The operation hash
     * @return True if executed
     */
    function isExecuted(bytes32 operationHash) external view returns (bool) {
        return executed[operationHash];
    }

    /**
     * @notice Get the FROST multisig address
     * @return The FROST multisig address
     */
    function getFrostMultisig() external view returns (address) {
        return frostMultisig;
    }

    function getAddCustodianOperationHash(address custodian) public view returns (bytes32) {
        return FROSTAuthorization(frostMultisig)
            .hashOperation(address(this), this.addCustodian.selector, keccak256(abi.encode(custodian)), operationNonce);
    }

    function getRemoveCustodianOperationHash(address custodian) public view returns (bytes32) {
        return FROSTAuthorization(frostMultisig)
            .hashOperation(
                address(this), this.removeCustodian.selector, keccak256(abi.encode(custodian)), operationNonce
            );
    }

    function getSetMinCustodiansOperationHash(uint256 minRequired) public view returns (bytes32) {
        return FROSTAuthorization(frostMultisig)
            .hashOperation(
                address(this), this.setMinCustodians.selector, keccak256(abi.encode(minRequired)), operationNonce
            );
    }

    // ======== INTERNAL ========

    /**
     * @notice Validate that FROST multisig has approved an operation
     * @param approvalHash The operation hash to validate
     */
    function _validateFROSTApproval(bytes32 approvalHash) internal view {
        if (executed[approvalHash]) revert AlreadyExecuted();

        require(
            FROSTAuthorization(frostMultisig).isOperationApproved(approvalHash, address(this)), "FROST approval not met"
        );
    }

    function _validateCustodianApproval(bytes32 approvalHash) internal view {
        if (custodianSignatureCount(approvalHash) < config.minRequired) revert ApprovalNotMet();
    }
}
