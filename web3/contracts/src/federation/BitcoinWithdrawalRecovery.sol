// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "../core/BitcoinTxParser.sol";
import "./BitcoinBatchRegistry.sol";
import "./BitcoinDepositRegistry.sol";
import {IPaymentSPV} from "./BitcoinWithdrawalEscrow.sol";

interface IBitcoinRecoveryEscrow {
    struct Payment {
        bytes32 txid;
        bytes32 blockHash;
        bool settled;
        bytes32[] requests;
    }
    struct Request {
        address pool;
        uint256 localId;
        address token;
        address owner;
        uint256 amount;
        bytes script;
        bytes32 batch;
        uint8 state;
    }
    function batches() external view returns (BitcoinBatchRegistry);
    function depositRegistry() external view returns (BitcoinDepositRegistry);
    function spv() external view returns (IPaymentSPV);
    function depth() external view returns (uint32);
    function recovery() external view returns (address);
    function isBound(bytes32 batch) external view returns (bool);
    function payment(bytes32 batch) external view returns (Payment memory);
    function getRequest(bytes32 key) external view returns (Request memory);
    function recordRecovery(bytes calldata raw, bytes32 txid) external;
}

interface IBitcoinRecoveryReserve {
    function escrow() external view returns (address);
    function deposits() external view returns (address);
    function confederationPool() external view returns (address);
    function shareToken() external view returns (address);
    function rotatePayoutScript(bytes32 successorKey) external;
}

/// @notice Settles exact, fully collateral-preserving migrations of V3 signing families.
/// @dev Bitcoin consensus enforces the Taproot policy. SPV authenticates the transaction
/// effects, and does not attest which valid Bitcoin witness path authorized them.
contract BitcoinWithdrawalRecovery is ReentrancyGuard {
    uint32 private constant HEIGHT_LOCKTIME_LIMIT = 500_000_000;
    uint256 private constant MAX_TX_BYTES = 100_000;

    IBitcoinRecoveryEscrow public immutable escrow;
    IBitcoinRecoveryReserve public immutable reserve;
    bytes32 public immutable sourceScriptHash;
    bytes32 public immutable successorScriptHash;
    bytes32 public immutable sponsorScriptHash;
    uint32 public immutable recoveryHeight;
    uint32 public immutable recoveryDepth;
    uint64 public immutable maxMinerFeeSats;
    address public immutable pauseGuardian;

    bool public paused;
    bytes32 public rotationRoot;
    mapping(bytes32 => uint64) public sponsorSats;
    mapping(bytes32 => bytes32) public recoveredTxid;
    mapping(bytes32 => bytes32) public migrationBlock;

    error InvalidRecoveryPolicy();
    error UnboundRecovery();
    error RecoveryProofInvalid();
    error RecoveryFamilyInvalid();
    error RecoveryBackingInvalid();
    error RecoverySponsorInvalid();
    error RecoveryHeightInvalid();
    error RecoveryPauseGuardianOnly();
    error RecoveryRotationInvalid();
    error RecoverySweepRequiresPause();

    event RecoveryPaused();
    event SponsorRegistered(bytes32 indexed outpoint, uint64 sats);
    event RecoveryConfirmed(bytes32 indexed root, bytes32 indexed txid, bytes32 indexed blockHash);
    event RecoverySweepConfirmed(bytes32 indexed txid, bytes32 indexed blockHash);
    event RecoveryWalletActivated(bytes32 indexed root, bytes32 indexed walletId, bytes32 indexed successorScript);

    constructor(
        address escrow_,
        address reserve_,
        bytes32 sourceScriptHash_,
        bytes32 successorScriptHash_,
        bytes32 sponsorScriptHash_,
        uint32 recoveryHeight_,
        uint32 recoveryDepth_,
        uint64 maxMinerFeeSats_,
        address pauseGuardian_
    ) {
        if (escrow_.code.length == 0 || reserve_.code.length == 0 || pauseGuardian_ == address(0)
            || sourceScriptHash_ == 0 || successorScriptHash_ == 0 || sponsorScriptHash_ == 0
            || sourceScriptHash_ == successorScriptHash_ || sourceScriptHash_ == sponsorScriptHash_
            || successorScriptHash_ == sponsorScriptHash_
            || recoveryHeight_ == 0 || recoveryHeight_ >= HEIGHT_LOCKTIME_LIMIT
            || maxMinerFeeSats_ == 0 || recoveryDepth_ < IBitcoinRecoveryEscrow(escrow_).depth()) {
            revert InvalidRecoveryPolicy();
        }
        escrow = IBitcoinRecoveryEscrow(escrow_);
        reserve = IBitcoinRecoveryReserve(reserve_);
        sourceScriptHash = sourceScriptHash_;
        successorScriptHash = successorScriptHash_;
        sponsorScriptHash = sponsorScriptHash_;
        recoveryHeight = recoveryHeight_;
        recoveryDepth = recoveryDepth_;
        maxMinerFeeSats = maxMinerFeeSats_;
        pauseGuardian = pauseGuardian_;
    }

    /// @dev Escrow's FROST-authorized, pre-funding binding checks this graph once.
    function validateBinding() external view {
        BitcoinDepositRegistry deposits = escrow.depositRegistry();
        if (msg.sender != address(escrow) || address(deposits) == address(0)
            || reserve.escrow() != address(escrow) || reserve.deposits() != address(deposits)
            || reserve.confederationPool() == address(0)
            || address(deposits.addressRegistry()) != address(0)
            || deposits.poolScripts(address(reserve)) != sourceScriptHash
            || deposits.totalBackingSats() != 0 || deposits.scriptPools(sponsorScriptHash) != address(0)
            || deposits.feeScripts(sponsorScriptHash) != 0) revert InvalidRecoveryPolicy();
    }

    function pause() external {
        _requireBound();
        if (msg.sender != pauseGuardian) revert RecoveryPauseGuardianOnly();
        _pause();
    }

    function _pause() private {
        if (paused) return;
        paused = true;
        emit RecoveryPaused();
    }

    function _requireBound() private view {
        if (escrow.recovery() != address(this)) revert UnboundRecovery();
    }

    function _proof(bytes calldata raw, bytes32 blockHash, bytes32[] calldata siblings, uint256 index)
        private view returns (bytes32 txid)
    {
        if (raw.length > MAX_TX_BYTES || siblings.length > 32 || index >> siblings.length != 0) {
            revert RecoveryProofInvalid();
        }
        txid = BitcoinTxParser.txId(raw);
        if (!_hasDepth(blockHash) || !escrow.spv().checkTxInclusion(blockHash, txid, siblings, index)) {
            revert RecoveryProofInvalid();
        }
    }

    function _hasDepth(bytes32 blockHash) private view returns (bool) {
        if (blockHash == 0) return false;
        (bool canonical, uint64 confirmations) = escrow.spv().getBlockStatus(blockHash);
        return canonical && confirmations >= recoveryDepth;
    }

    function recoveryBlock(bytes32 root) public view returns (bytes32) {
        return migrationBlock[recoveredTxid[root]];
    }

    function canRefund(bytes32 root) external view returns (bool) {
        return _hasDepth(recoveryBlock(root));
    }

    function successorWalletId() external view returns (bytes32) {
        return rotationRoot == 0 ? bytes32(0) : escrow.batches().walletId();
    }

    function successorWalletResult() external view returns (bytes32) {
        return rotationRoot == 0 ? bytes32(0) : escrow.batches().walletResult();
    }

    function activeScriptHash() external view returns (bytes32) {
        return rotationRoot == 0 ? sourceScriptHash : successorScriptHash;
    }

    /// @dev The old epoch stays paused. Previously committed, late funding may need another cold sweep.
    function withdrawalsPaused() external view returns (bool) {
        return paused && (rotationRoot == 0 || !_hasDepth(recoveryBlock(rotationRoot))
            || escrow.depositRegistry().backingByScript(sourceScriptHash) != 0);
    }

    function paymentScriptHash(bytes32 batch) external view returns (bytes32) {
        BitcoinBatchRegistry batches = escrow.batches();
        return batches.batchWalletResult(batch) == batches.initialWalletResult() ? sourceScriptHash : successorScriptHash;
    }

    /// @dev The latest canonical placement is required independently for each migrated input.
    function checkMigration(bytes32 txid) external view {
        bytes32 bound = migrationBlock[txid];
        if (bound != bytes32(0) && !_hasDepth(bound)) revert RecoveryProofInvalid();
    }

    function getRotateOperationHash(bytes32 nextWallet, bytes32 root) public view returns (bytes32) {
        return escrow.batches().authorization().hashOperation(address(this), this.rotate.selector,
            keccak256(abi.encode(nextWallet, root, recoveredTxid[root])), 0);
    }

    /// @notice Activate the independently enrolled Bitcoin DKG without changing S balances or backing.
    function rotate(bytes32 nextWallet, bytes32 root, bytes32 op) external nonReentrant {
        _requireBound();
        BitcoinDepositRegistry deposits = escrow.depositRegistry();
        if (rotationRoot != 0 || root == 0 || !_hasDepth(recoveryBlock(root))
            || deposits.totalBackingSats() != deposits.backingByScript(successorScriptHash)
            || op != getRotateOperationHash(nextWallet, root)) {
            revert RecoveryRotationInvalid();
        }
        BitcoinBatchRegistry batches = escrow.batches();
        batches.authorization().consumeOperation(op);
        bytes32 key = batches.activateRecoveryWallet(nextWallet, successorScriptHash);
        deposits.activateRecoveryScript(address(reserve), sourceScriptHash, successorScriptHash);
        reserve.rotatePayoutScript(key);
        rotationRoot = root;
        emit RecoveryWalletActivated(root, nextWallet, successorScriptHash);
    }

    /// @notice Authenticate value and script of a distinct confirmed, separately funded fee outpoint.
    function registerSponsor(bytes calldata raw, uint32 vout, bytes32 blockHash, bytes32[] calldata siblings, uint256 index)
        external
    {
        _requireBound();
        bytes32 txid = _proof(raw, blockHash, siblings, index);
        (uint64 value, bytes memory script) = BitcoinTxParser.outputAt(raw, vout);
        bytes32 key = keccak256(abi.encode(txid, vout));
        if (value == 0 || keccak256(script) != sponsorScriptHash || !_availableSponsor(escrow.depositRegistry(), key)) {
            revert RecoverySponsorInvalid();
        }
        sponsorSats[key] = value;
        emit SponsorRegistered(key, value);
    }

    /// @notice Publish the sole EVM outcome for a root after Bitcoin confirms an exact funded migration.
    function recover(bytes32 root, bytes calldata raw, bytes32 blockHash, bytes32[] calldata siblings, uint256 index)
        external nonReentrant
    {
        _requireBound();
        bytes32 txid = _proof(raw, blockHash, siblings, index);
        bytes32 previous = recoveredTxid[root];
        if (previous != 0) {
            // A reorg may move the same transaction to a different canonical block.
            // Re-anchor only the exact spend, with no second accounting entry.
            if (previous != txid) revert RecoveryFamilyInvalid();
        } else {
            _validateRecovery(root, raw);
            recoveredTxid[root] = txid;
            _pause();
            escrow.recordRecovery(raw, txid);
        }
        migrationBlock[txid] = blockHash;
        emit RecoveryConfirmed(root, txid, blockHash);
    }

    /// @notice Move idle recorded backing, including old change and late deposits, without changing S supply.
    function sweep(bytes calldata raw, bytes32 blockHash, bytes32[] calldata siblings, uint256 index)
        external nonReentrant
    {
        _requireBound();
        if (!paused) revert RecoverySweepRequiresPause();
        bytes32 txid = _proof(raw, blockHash, siblings, index);
        if (migrationBlock[txid] == 0) {
            _validateMigration(bytes32(0), new bytes32[](0), raw);
            escrow.recordRecovery(raw, txid);
        }
        migrationBlock[txid] = blockHash;
        emit RecoverySweepConfirmed(txid, blockHash);
    }

    function _validateRecovery(bytes32 root, bytes calldata raw) private view {
        BitcoinBatchRegistry batches = escrow.batches();
        if (root == 0 || batches.reservationRoot(root) != root || batches.batchWalletResult(root) != batches.initialWalletResult()
            || batches.batchState(root) != BitcoinBatchRegistry.State.Signing || !escrow.isBound(root)) {
            revert RecoveryFamilyInvalid();
        }
        IBitcoinRecoveryEscrow.Payment memory payment = escrow.payment(root);
        address pool = reserve.confederationPool();
        address token = reserve.shareToken();
        if (token == address(0) || payment.requests.length == 0) revert RecoveryFamilyInvalid();
        for (uint256 i; i < payment.requests.length; ++i) {
            IBitcoinRecoveryEscrow.Request memory request = escrow.getRequest(payment.requests[i]);
            if (request.pool != pool || request.token != token || request.batch != root) revert RecoveryFamilyInvalid();
        }

        _validateMigration(root, batches.getInputs(root), raw);
    }

    function _validateMigration(bytes32 root, bytes32[] memory expected, bytes calldata raw) private view {
        BitcoinBatchRegistry batches = escrow.batches();
        (bytes32[] memory prev, uint32[] memory indices) = BitcoinTxParser.parseAllInputs(raw);
        if (prev.length < 2 || prev.length > 17 || (root != 0 && prev.length != expected.length + 1)) {
            revert RecoveryFamilyInvalid();
        }
        uint256 count = prev.length - 1;
        _checkHeightLock(raw, count);
        BitcoinDepositRegistry deposits = escrow.depositRegistry();
        uint256 principal;
        for (uint256 i; i < count; ++i) {
            bytes32 reservation = keccak256(abi.encode(BitcoinTxParser.flipBytes32(prev[i]), indices[i]));
            if ((root != 0 && reservation != expected[i]) || batches.reservations(reservation) != root) {
                revert RecoveryFamilyInvalid();
            }
            bytes32 key = keccak256(abi.encode(prev[i], indices[i]));
            BitcoinDepositRegistry.OutputValue memory value = deposits.getValue(key);
            if (!value.known || value.sats == 0 || deposits.backingSats(key) != value.sats
                || keccak256(value.script) != sourceScriptHash || deposits.backingScripts(key) != sourceScriptHash) {
                revert RecoveryBackingInvalid();
            }
            principal += value.sats;
        }
        _validateOutputs(deposits, raw, principal, prev[count], indices[count]);
    }

    function _availableSponsor(BitcoinDepositRegistry deposits, bytes32 key) private view returns (bool) {
        return !deposits.consumed(key) && !deposits.walletOutput(key) && deposits.backingSats(key) == 0
            && deposits.scriptPools(sponsorScriptHash) == address(0)
            && deposits.feeScripts(sponsorScriptHash) == successorScriptHash;
    }

    function _validateOutputs(BitcoinDepositRegistry deposits, bytes calldata raw, uint256 principal,
        bytes32 sponsorTxid, uint32 sponsorVout) private view
    {
        bytes32 key = keccak256(abi.encode(sponsorTxid, sponsorVout));
        uint64 sponsor = sponsorSats[key];
        if (sponsor == 0 || !_availableSponsor(deposits, key)
            || escrow.batches().reservations(keccak256(abi.encode(BitcoinTxParser.flipBytes32(sponsorTxid), sponsorVout))) != 0) {
            revert RecoverySponsorInvalid();
        }
        uint256 outputs = BitcoinTxParser.outputCount(raw);
        if (outputs == 0 || outputs > 2) revert RecoveryBackingInvalid();
        (uint64 amount, bytes memory successor) = BitcoinTxParser.outputAt(raw, 0);
        if (amount != principal || keccak256(successor) != successorScriptHash
            || deposits.scriptPools(successorScriptHash) != address(reserve)) revert RecoveryBackingInvalid();
        uint64 change;
        if (outputs == 2) {
            bytes memory changeScript;
            (change, changeScript) = BitcoinTxParser.outputAt(raw, 1);
            if (change == 0 || keccak256(changeScript) != sponsorScriptHash) revert RecoverySponsorInvalid();
        }
        if (change >= sponsor || sponsor - change > maxMinerFeeSats) revert RecoverySponsorInvalid();
    }

    function _checkHeightLock(bytes calldata raw, uint256 principalInputs) private view {
        uint32 height = _readLE32(raw, raw.length - 4);
        if (height < recoveryHeight || height >= HEIGHT_LOCKTIME_LIMIT) revert RecoveryHeightInvalid();
        uint256 offset = raw[4] == 0x00 && raw[5] == 0x01 ? 6 : 4;
        offset += BitcoinTxParser._readVarInt(raw, offset).size;
        for (uint256 i; i < principalInputs; ++i) {
            offset += 36;
            BitcoinTxParser.VarInt memory size = BitcoinTxParser._readVarInt(raw, offset);
            offset += size.size + size.value;
            if (_readLE32(raw, offset) == type(uint32).max) revert RecoveryHeightInvalid();
            offset += 4;
        }
    }

    function _readLE32(bytes calldata raw, uint256 offset) private pure returns (uint32) {
        return uint32(uint8(raw[offset])) | uint32(uint8(raw[offset + 1])) << 8
            | uint32(uint8(raw[offset + 2])) << 16 | uint32(uint8(raw[offset + 3])) << 24;
    }
}
