// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../FROSTAuthorization.sol";

/**
 * @title ProjectWalletRegistry
 * @notice Registry for project wallets that receive a share of collected fees
 * @dev Managed via FROST threshold signatures. Project wallets get their share
 *      of fees BEFORE the member/custodian split.
 */
contract ProjectWalletRegistry {
    struct WalletEntry {
        address wallet;
        uint256 shareBps;
        string label;
        bool isActive;
    }

    /// @notice FROST multisig for governance
    FROSTAuthorization public immutable frostMultisig;

    /// @notice All wallet entries
    WalletEntry[] private wallets;

    /// @notice Wallet address to index mapping
    mapping(address => uint256) private walletIndex;
    mapping(address => bool) private walletExists;
    uint256 public operationNonce;

    // ======== EVENTS ========

    event WalletAdded(address indexed wallet, uint256 shareBps, string label);
    event WalletRemoved(address indexed wallet);
    event WalletUpdated(address indexed wallet, uint256 shareBps, string label);
    event RegistryUpdated(uint256 walletCount);

    // ======== ERRORS ========

    error WalletAlreadyRegistered();
    error WalletNotFound();
    error InvalidWallet();
    error InvalidShare();
    error ArrayLengthMismatch();
    error InsufficientFROSTSignatures();
    error TotalSharesExceed100Percent();

    // ======== CONSTRUCTOR ========

    constructor(address _frostMultisig, address[] memory _wallets, uint256[] memory _shares, string[] memory _labels) {
        require(_frostMultisig != address(0), "Invalid FROST address");
        require(_wallets.length == _shares.length && _wallets.length == _labels.length, "Array length mismatch");

        frostMultisig = FROSTAuthorization(_frostMultisig);

        uint256 totalBps = 0;
        for (uint256 i = 0; i < _wallets.length; i++) {
            if (_wallets[i] == address(0)) revert InvalidWallet();
            if (_shares[i] > 10000) revert InvalidShare();
            if (walletExists[_wallets[i]]) revert WalletAlreadyRegistered();
            totalBps += _shares[i];
            if (totalBps > 10000) revert TotalSharesExceed100Percent();

            walletIndex[_wallets[i]] = wallets.length;
            walletExists[_wallets[i]] = true;
            wallets.push(WalletEntry({wallet: _wallets[i], shareBps: _shares[i], label: _labels[i], isActive: true}));
        }
    }

    // ======== MANAGEMENT ========

    /**
     * @notice Add a project wallet
     * @param wallet Wallet address
     * @param shareBps Share in basis points
     * @param label Human-readable label
     * @param approvalHash FROST-signed operation hash
     */
    function addWallet(address wallet, uint256 shareBps, string calldata label, bytes32 approvalHash) external {
        if (wallet == address(0)) revert InvalidWallet();
        if (walletExists[wallet]) revert WalletAlreadyRegistered();
        if (shareBps > 10000) revert InvalidShare();
        require(approvalHash == getAddWalletOperationHash(wallet, shareBps, label), "Operation hash mismatch");
        if (_getTotalActiveShares() + shareBps > 10000) revert TotalSharesExceed100Percent();

        if (!frostMultisig.isOperationApproved(approvalHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }
        frostMultisig.consumeOperation(approvalHash);
        operationNonce++;

        walletIndex[wallet] = wallets.length;
        walletExists[wallet] = true;
        wallets.push(WalletEntry({wallet: wallet, shareBps: shareBps, label: label, isActive: true}));

        emit WalletAdded(wallet, shareBps, label);
    }

    /**
     * @notice Remove a project wallet
     * @param wallet Wallet address to remove
     * @param approvalHash FROST-signed operation hash
     */
    function removeWallet(address wallet, bytes32 approvalHash) external {
        if (!walletExists[wallet]) revert WalletNotFound();
        require(approvalHash == getRemoveWalletOperationHash(wallet), "Operation hash mismatch");

        if (!frostMultisig.isOperationApproved(approvalHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }
        frostMultisig.consumeOperation(approvalHash);
        operationNonce++;

        uint256 idx = walletIndex[wallet];
        wallets[idx].isActive = false;

        emit WalletRemoved(wallet);
    }

    /**
     * @notice Batch update all wallets atomically
     * @param _wallets Array of wallet addresses
     * @param _shares Array of share basis points
     * @param _labels Array of labels
     * @param approvalHash FROST-signed operation hash
     */
    function batchUpdateWallets(
        address[] calldata _wallets,
        uint256[] calldata _shares,
        string[] calldata _labels,
        bytes32 approvalHash
    ) external {
        if (_wallets.length != _shares.length || _wallets.length != _labels.length) {
            revert ArrayLengthMismatch();
        }
        _validateWalletSet(_wallets, _shares);
        require(
            approvalHash == getBatchUpdateWalletsOperationHash(_wallets, _shares, _labels), "Operation hash mismatch"
        );

        if (!frostMultisig.isOperationApproved(approvalHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }
        frostMultisig.consumeOperation(approvalHash);
        operationNonce++;

        // Deactivate all existing wallets
        for (uint256 i = 0; i < wallets.length; i++) {
            walletExists[wallets[i].wallet] = false;
        }

        // Replace with new set
        delete wallets;
        for (uint256 i = 0; i < _wallets.length; i++) {
            walletIndex[_wallets[i]] = wallets.length;
            walletExists[_wallets[i]] = true;
            wallets.push(WalletEntry({wallet: _wallets[i], shareBps: _shares[i], label: _labels[i], isActive: true}));
        }

        emit RegistryUpdated(_wallets.length);
    }

    // ======== VIEW FUNCTIONS ========

    /**
     * @notice Get all active wallet entries
     * @return activeWallets Array of active wallet entries
     */
    function getWallets() external view returns (WalletEntry[] memory activeWallets) {
        uint256 activeCount = 0;
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) activeCount++;
        }

        activeWallets = new WalletEntry[](activeCount);
        uint256 idx = 0;
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) {
                activeWallets[idx++] = wallets[i];
            }
        }
    }

    /**
     * @notice Get wallet count (active only)
     */
    function getActiveWalletCount() external view returns (uint256 count) {
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) count++;
        }
    }

    /**
     * @notice Check if a wallet is registered and active
     */
    function isWalletActive(address wallet) external view returns (bool) {
        if (!walletExists[wallet]) return false;
        return wallets[walletIndex[wallet]].isActive;
    }

    function getTotalActiveShares() external view returns (uint256) {
        return _getTotalActiveShares();
    }

    function getAddWalletOperationHash(address wallet, uint256 shareBps, string memory label)
        public
        view
        returns (bytes32)
    {
        return frostMultisig.hashOperation(
            address(this), this.addWallet.selector, keccak256(abi.encode(wallet, shareBps, label)), operationNonce
        );
    }

    function getRemoveWalletOperationHash(address wallet) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.removeWallet.selector, keccak256(abi.encode(wallet)), operationNonce
        );
    }

    function getBatchUpdateWalletsOperationHash(
        address[] memory _wallets,
        uint256[] memory _shares,
        string[] memory _labels
    ) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this),
            this.batchUpdateWallets.selector,
            keccak256(abi.encode(_wallets, _shares, _labels)),
            operationNonce
        );
    }

    function _getTotalActiveShares() internal view returns (uint256 totalBps) {
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) totalBps += wallets[i].shareBps;
        }
    }

    function _validateWalletSet(address[] calldata _wallets, uint256[] calldata _shares) internal pure {
        uint256 totalBps = 0;
        for (uint256 i = 0; i < _wallets.length; i++) {
            if (_wallets[i] == address(0)) revert InvalidWallet();
            if (_shares[i] > 10000) revert InvalidShare();
            totalBps += _shares[i];
            if (totalBps > 10000) revert TotalSharesExceed100Percent();

            for (uint256 j = i + 1; j < _wallets.length; j++) {
                if (_wallets[i] == _wallets[j]) revert WalletAlreadyRegistered();
            }
        }
    }
}
