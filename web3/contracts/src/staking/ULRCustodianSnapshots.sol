// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {ULRStaking} from "./ULRStaking.sol";
import {FROSTAuthorization} from "../FROSTAuthorization.sol";
import {IConfederationFactory} from "../federation/interfaces/IConfederationFactory.sol";
import {ICustodianValidator} from "../federation/interfaces/ICustodianValidator.sol";

interface IULRTreasuryBinding {
    function snapshots() external view returns (ULRCustodianSnapshots);
    function staking() external view returns (ULRStaking);
    function factory() external view returns (IConfederationFactory);
    function confederationId() external view returns (string memory);
}

/// @notice Shared, immutable daily ULR-weight attestations and canonical treasury enrollment.
/// @dev FROST attests historical holdings, roster membership and inventory completeness.
///      This is not a proof of historical ERC-20 balances or of all token holders.
contract ULRCustodianSnapshots {
    uint256 public immutable MIN_CUSTODIAN_ULR;
    uint256 public constant ULR_SUPPLY = 10_000_000 ether;
    // Bound calldata/work independently of the economic 200k eligibility rule.
    uint256 public constant MAX_HOLDERS = 4096;
    uint256 public constant MAX_CONFEDERATIONS = 64;

    struct Report {
        uint256 day;
        uint256 snapshotBlock;
        bytes32 snapshotBlockHash;
        bytes32 evidenceHash;
    }

    struct Allocation {
        bytes32 scope;
        // Sorted uint16 indices, big endian, two bytes per owner. Packed to
        // keep the maximum inventory below transaction calldata/gas limits.
        bytes custodianIndices;
        bytes32 rosterHash;
    }

    struct DaySnapshot {
        bool published;
        uint256 totalULR;
        uint256 snapshotBlock;
        bytes32 snapshotBlockHash;
        bytes32 evidenceHash;
        bytes32 commitment;
    }

    struct ConfederationSnapshot {
        bool included;
        uint256 ulr;
        bytes32 rosterHash;
    }

    ULRStaking public immutable staking;
    FROSTAuthorization public immutable authorization;
    uint256 public operationNonce;
    mapping(bytes32 => address) public treasuries;
    mapping(bytes32 => uint256) public treasuryActivationDay;
    mapping(bytes32 => bool) public activeTreasuries;
    bytes32[] private activeScopes;
    mapping(uint256 => DaySnapshot) public daySnapshots;
    mapping(uint256 => mapping(bytes32 => ConfederationSnapshot)) public confederations;

    event TreasuryEnrolled(bytes32 indexed scope, address indexed treasury, address factory, string confederationId);
    event TreasuryActivated(bytes32 indexed scope, address indexed treasury, uint256 firstDay);
    event DayPublished(uint256 indexed day, uint256 totalULR, bytes32 commitment, bytes32 evidenceHash);

    error InvalidBinding();
    error InvalidDay();
    error InvalidReport();
    error AlreadyPublished();
    error AlreadyEnrolled();
    error TreasuryLimit();
    error InvalidOperation();
    error CustodianApprovalRequired();

    constructor(address staking_, address authorization_) {
        if (staking_.code.length == 0 || authorization_.code.length == 0) revert InvalidBinding();
        staking = ULRStaking(staking_);
        MIN_CUSTODIAN_ULR = staking.CUSTODIAN_THRESHOLD();
        authorization = FROSTAuthorization(authorization_);
    }

    function scopeFor(address factory, string memory confederationId) public view returns (bytes32) {
        return keccak256(abi.encode(block.chainid, factory, confederationId));
    }

    function getEnrollTreasuryOperationHash(address factory, string calldata id, address treasury)
        public
        view
        returns (bytes32)
    {
        IConfederationFactory.ConfederationConfig memory config = IConfederationFactory(factory).getConfederation(id);
        return FROSTAuthorization(config.frostMultisig)
            .hashOperation(address(this), this.enrollTreasury.selector, keccak256(abi.encode(factory, id, treasury)), 0);
    }

    /// @notice One treasury per factory/confederation, approved by that confederation's two authorities.
    function enrollTreasury(address factory, string calldata id, address treasury, bytes32 op) external {
        if (factory.code.length == 0 || treasury.code.length == 0 || bytes(id).length == 0) revert InvalidBinding();
        bytes32 scope = scopeFor(factory, id);
        if (treasuries[scope] != address(0)) revert AlreadyEnrolled();
        IConfederationFactory.ConfederationConfig memory config = IConfederationFactory(factory).getConfederation(id);
        if (!config.isActive || config.frostMultisig.code.length == 0 || config.custodianValidator.code.length == 0) {
            revert InvalidBinding();
        }
        IULRTreasuryBinding target = IULRTreasuryBinding(treasury);
        if (
            address(target.snapshots()) != address(this) || address(target.staking()) != address(staking)
                || address(target.factory()) != factory
                || keccak256(bytes(target.confederationId())) != keccak256(bytes(id))
        ) {
            revert InvalidBinding();
        }
        if (op != getEnrollTreasuryOperationHash(factory, id, treasury)) revert InvalidOperation();
        if (!ICustodianValidator(config.custodianValidator).meetsMinimumCustodians(op)) {
            revert CustodianApprovalRequired();
        }
        FROSTAuthorization(config.frostMultisig).consumeOperation(op);
        treasuries[scope] = treasury;
        emit TreasuryEnrolled(scope, treasury, factory, id);
    }

    function getActivateTreasuryOperationHash(address treasury) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this), this.activateTreasury.selector, keccak256(abi.encode(treasury)), 0
        );
    }

    /// @notice Admit an enrolled treasury to the system roster under system FROST.
    function activateTreasury(address treasury, bytes32 op) external {
        IULRTreasuryBinding target = IULRTreasuryBinding(treasury);
        bytes32 scope = scopeFor(address(target.factory()), target.confederationId());
        if (treasuries[scope] != treasury || treasury == address(0)) revert InvalidBinding();
        if (activeTreasuries[scope]) revert AlreadyEnrolled();
        if (activeScopes.length >= MAX_CONFEDERATIONS) revert TreasuryLimit();
        if (op != getActivateTreasuryOperationHash(treasury)) revert InvalidOperation();
        authorization.consumeOperation(op);
        uint256 day = block.timestamp / 1 days;
        activeTreasuries[scope] = true;
        treasuryActivationDay[scope] = day;
        activeScopes.push(scope);
        emit TreasuryActivated(scope, treasury, day);
    }

    /// @notice All treasuries activated by the end of the requested UTC day.
    function snapshotTreasuries(uint256 day) external view returns (address[] memory targets) {
        uint256 count = _snapshotTreasuryCount(day);
        targets = new address[](count);
        for (uint256 i; i < count; ++i) targets[i] = treasuries[activeScopes[i]];
    }

    function _snapshotTreasuryCount(uint256 day) private view returns (uint256 count) {
        // Activations are append-only and ordered by UTC day.
        while (count < activeScopes.length && treasuryActivationDay[activeScopes[count]] <= day) ++count;
    }

    function getPublishDayOperationHash(
        Report calldata report,
        address[] calldata owners,
        uint256[] calldata weights,
        Allocation[] calldata allocations
    ) public view returns (bytes32) {
        return authorization.hashOperation(
            address(this),
            this.publishDay.selector,
            keccak256(abi.encode(report, owners, weights, allocations)),
            operationNonce
        );
    }

    function publishDay(
        Report calldata report,
        address[] calldata owners,
        uint256[] calldata weights,
        Allocation[] calldata allocations,
        bytes32 op
    ) external {
        if (report.day < staking.firstEligibleDay() || report.day >= block.timestamp / 1 days) revert InvalidDay();
        if (daySnapshots[report.day].published) revert AlreadyPublished();
        if (
            owners.length != weights.length || owners.length > MAX_HOLDERS
                || allocations.length != _snapshotTreasuryCount(report.day)
                || report.snapshotBlock >= block.number || report.snapshotBlockHash == 0 || report.evidenceHash == 0
        ) revert InvalidReport();
        // Older anchors remain explicit FROST attestations; recent available hashes must agree.
        bytes32 knownHash = blockhash(report.snapshotBlock);
        if (knownHash != 0 && knownHash != report.snapshotBlockHash) revert InvalidReport();
        bytes memory active = new bytes(owners.length);
        uint256[] memory confederationWeights = new uint256[](allocations.length);
        for (uint256 i; i < allocations.length; ++i) {
            Allocation calldata allocation = allocations[i];
            if (
                allocation.scope == 0 || allocation.rosterHash == 0 || !activeTreasuries[allocation.scope]
                    || treasuryActivationDay[allocation.scope] > report.day
                    || (i > 0 && allocation.scope <= allocations[i - 1].scope)
                    || allocation.custodianIndices.length % 2 != 0
                    || allocation.custodianIndices.length / 2 > owners.length
            ) revert InvalidReport();
            uint256 previous;
            uint256 weight;
            for (uint256 j; j < allocation.custodianIndices.length; j += 2) {
                uint256 index = _indexAt(allocation.custodianIndices, j);
                if (index >= owners.length || (j > 0 && index <= previous)) {
                    revert InvalidReport();
                }
                active[index] = 0x01;
                uint256 value = weights[index];
                if (value > ULR_SUPPLY) revert InvalidReport();
                weight += value;
                previous = index;
            }
            confederationWeights[i] = weight;
        }
        uint256 total;
        address token = address(staking.stakeToken());
        for (uint256 i; i < owners.length; ++i) {
            if (
                owners[i] == address(0) || owners[i] == address(staking) || owners[i] == token
                    || (i > 0 && owners[i] <= owners[i - 1]) || weights[i] == 0
                    || (weights[i] < MIN_CUSTODIAN_ULR && active[i] == 0)
                    || weights[i] > ULR_SUPPLY
            ) revert InvalidReport();
            total += weights[i];
        }
        if (total > ULR_SUPPLY) revert InvalidReport();
        for (uint256 i; i < allocations.length; ++i) {
            Allocation calldata allocation = allocations[i];
            confederations[report.day][allocation.scope] =
                ConfederationSnapshot(true, confederationWeights[i], allocation.rosterHash);
        }
        bytes32 commitment = keccak256(abi.encode(report, owners, weights, allocations));
        if (op != authorization.hashOperation(address(this), this.publishDay.selector, commitment, operationNonce)) {
            revert InvalidOperation();
        }
        authorization.consumeOperation(op);
        ++operationNonce;
        daySnapshots[report.day] =
            DaySnapshot(true, total, report.snapshotBlock, report.snapshotBlockHash, report.evidenceHash, commitment);
        emit DayPublished(report.day, total, commitment, report.evidenceHash);
    }

    function _indexAt(bytes calldata packed, uint256 offset) private pure returns (uint256) {
        return (uint256(uint8(packed[offset])) << 8) | uint8(packed[offset + 1]);
    }
}
