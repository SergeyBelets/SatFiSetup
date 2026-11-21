// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title GlobalProjectWalletRegistry
 * @notice Global project wallets that collect fee shares from ALL federations/confederations
 * @dev This is the "shared project wallet" that aggregates fees across the entire ecosystem.
 *      Owner-managed (not FROST-gated) because it operates above the federation level.
 *
 *      Flow:
 *      1. Owner deploys GlobalProjectWalletRegistry with project wallets
 *      2. Each federation/confederation's FeeManager sends projectWalletShare here
 *      3. Owner can distribute accumulated fees to project wallets
 *      4. New federations are registered to track which fee managers contribute
 */
contract GlobalProjectWalletRegistry is Ownable, ReentrancyGuard {
    // ======== STRUCTS ========

    struct ProjectWallet {
        address wallet;
        uint256 shareBps;    // Share in basis points (10000 = 100%)
        string label;
        bool isActive;
    }

    struct FederationEntry {
        address feeManager;
        string federationId;
        bool isActive;
    }

    // ======== STATE ========

    /// @notice All project wallets
    ProjectWallet[] private wallets;

    /// @notice Wallet address to index mapping
    mapping(address => uint256) private walletIndex;
    mapping(address => bool) private walletExists;

    /// @notice Registered federations that contribute fees
    FederationEntry[] private federations;
    mapping(address => uint256) private federationIndex;
    mapping(address => bool) private federationRegistered;

    /// @notice Total fees received (for tracking)
    uint256 public totalReceived;

    /// @notice Total fees distributed (for tracking)
    uint256 public totalDistributed;

    // ======== EVENTS ========

    event ProjectWalletAdded(address indexed wallet, uint256 shareBps, string label);
    event ProjectWalletRemoved(address indexed wallet);
    event ProjectWalletUpdated(address indexed wallet, uint256 newShareBps);
    event FederationRegistered(address indexed feeManager, string federationId);
    event FederationUnregistered(address indexed feeManager);
    event FeesReceived(address indexed from, uint256 amount);
    event FeesDistributed(uint256 totalAmount, uint256 walletCount);
    event WalletPaid(address indexed wallet, uint256 amount);

    // ======== ERRORS ========

    error WalletAlreadyRegistered();
    error WalletNotFound();
    error InvalidWallet();
    error InvalidShare();
    error TotalSharesExceed100Percent();
    error FederationAlreadyRegistered();
    error FederationNotRegistered();
    error NoBalance();
    error TransferFailed();
    error UnauthorizedFeeManager();

    // ======== CONSTRUCTOR ========

    /**
     * @notice Create global project wallet registry
     * @param _owner Registry owner (project admin)
     * @param _wallets Initial project wallet addresses
     * @param _shares Initial share basis points per wallet
     * @param _labels Initial labels per wallet
     */
    constructor(
        address _owner,
        address[] memory _wallets,
        uint256[] memory _shares,
        string[] memory _labels
    ) Ownable(_owner) {
        require(
            _wallets.length == _shares.length && _wallets.length == _labels.length,
            "Array length mismatch"
        );

        uint256 totalBps = 0;
        for (uint256 i = 0; i < _wallets.length; i++) {
            if (_wallets[i] == address(0)) revert InvalidWallet();
            totalBps += _shares[i];

            walletIndex[_wallets[i]] = wallets.length;
            walletExists[_wallets[i]] = true;
            wallets.push(ProjectWallet({
                wallet: _wallets[i],
                shareBps: _shares[i],
                label: _labels[i],
                isActive: true
            }));
        }

        if (totalBps > 10000) revert TotalSharesExceed100Percent();
    }

    // ======== RECEIVE FEES ========

    /**
     * @notice Receive ETH/native token fees from any federation's fee manager
     * @dev Called by fee managers when distributing project wallet shares
     */
    receive() external payable {
        if (!_isFederationActive(msg.sender)) revert UnauthorizedFeeManager();
        totalReceived += msg.value;
        emit FeesReceived(msg.sender, msg.value);
    }

    /**
     * @notice Record fee receipt (for ERC20 tokens tracked off-chain or via approve+transfer)
     * @dev For ERC20 fee tracking, federations transfer tokens directly, this records the event
     */
    function recordFeeReceived(uint256 amount) external {
        if (!_isFederationActive(msg.sender)) revert UnauthorizedFeeManager();
        totalReceived += amount;
        emit FeesReceived(msg.sender, amount);
    }

    // ======== FEDERATION MANAGEMENT ========

    /**
     * @notice Register a federation's fee manager
     * @param feeManager Address of the federation's FederationFeeManager
     * @param federationId Federation identifier
     */
    function registerFederation(address feeManager, string calldata federationId) external onlyOwner {
        if (feeManager == address(0)) revert InvalidWallet();
        if (federationRegistered[feeManager]) revert FederationAlreadyRegistered();

        federationIndex[feeManager] = federations.length;
        federationRegistered[feeManager] = true;
        federations.push(FederationEntry({
            feeManager: feeManager,
            federationId: federationId,
            isActive: true
        }));

        emit FederationRegistered(feeManager, federationId);
    }

    /**
     * @notice Unregister a federation
     * @param feeManager Address of the federation's FederationFeeManager
     */
    function unregisterFederation(address feeManager) external onlyOwner {
        if (!federationRegistered[feeManager]) revert FederationNotRegistered();

        uint256 idx = federationIndex[feeManager];
        federations[idx].isActive = false;

        emit FederationUnregistered(feeManager);
    }

    // ======== WALLET MANAGEMENT ========

    /**
     * @notice Add a project wallet
     * @param wallet Wallet address
     * @param shareBps Share in basis points
     * @param label Human-readable label
     */
    function addWallet(address wallet, uint256 shareBps, string calldata label) external onlyOwner {
        if (wallet == address(0)) revert InvalidWallet();
        if (walletExists[wallet]) revert WalletAlreadyRegistered();

        // Validate total shares don't exceed 100%
        uint256 totalBps = shareBps;
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) totalBps += wallets[i].shareBps;
        }
        if (totalBps > 10000) revert TotalSharesExceed100Percent();

        walletIndex[wallet] = wallets.length;
        walletExists[wallet] = true;
        wallets.push(ProjectWallet({
            wallet: wallet,
            shareBps: shareBps,
            label: label,
            isActive: true
        }));

        emit ProjectWalletAdded(wallet, shareBps, label);
    }

    /**
     * @notice Remove a project wallet
     * @param wallet Wallet address to remove
     */
    function removeWallet(address wallet) external onlyOwner {
        if (!walletExists[wallet]) revert WalletNotFound();

        uint256 idx = walletIndex[wallet];
        wallets[idx].isActive = false;

        emit ProjectWalletRemoved(wallet);
    }

    /**
     * @notice Update a wallet's share
     * @param wallet Wallet address
     * @param newShareBps New share in basis points
     */
    function updateWalletShare(address wallet, uint256 newShareBps) external onlyOwner {
        if (!walletExists[wallet]) revert WalletNotFound();

        uint256 idx = walletIndex[wallet];
        require(wallets[idx].isActive, "Wallet not active");

        // Validate total shares
        uint256 totalBps = newShareBps;
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive && i != idx) {
                totalBps += wallets[i].shareBps;
            }
        }
        if (totalBps > 10000) revert TotalSharesExceed100Percent();

        wallets[idx].shareBps = newShareBps;

        emit ProjectWalletUpdated(wallet, newShareBps);
    }

    /**
     * @notice Batch update all wallets atomically
     * @param _wallets Array of wallet addresses
     * @param _shares Array of share basis points
     * @param _labels Array of labels
     */
    function batchUpdateWallets(
        address[] calldata _wallets,
        uint256[] calldata _shares,
        string[] calldata _labels
    ) external onlyOwner {
        require(
            _wallets.length == _shares.length && _wallets.length == _labels.length,
            "Array length mismatch"
        );

        uint256 totalBps = 0;
        for (uint256 i = 0; i < _shares.length; i++) {
            totalBps += _shares[i];
        }
        if (totalBps > 10000) revert TotalSharesExceed100Percent();

        // Deactivate all existing wallets
        for (uint256 i = 0; i < wallets.length; i++) {
            walletExists[wallets[i].wallet] = false;
        }

        // Replace with new set
        delete wallets;
        for (uint256 i = 0; i < _wallets.length; i++) {
            if (_wallets[i] == address(0)) revert InvalidWallet();
            walletIndex[_wallets[i]] = wallets.length;
            walletExists[_wallets[i]] = true;
            wallets.push(ProjectWallet({
                wallet: _wallets[i],
                shareBps: _shares[i],
                label: _labels[i],
                isActive: true
            }));
        }
    }

    // ======== DISTRIBUTION ========

    /**
     * @notice Distribute accumulated native token (ETH) fees to project wallets
     * @dev Distributes the entire contract balance proportionally based on shareBps
     */
    function distributeNativeFees() external onlyOwner nonReentrant {
        uint256 balance = address(this).balance;
        if (balance == 0) revert NoBalance();

        uint256 distributed = 0;
        uint256 activeCount = 0;

        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive && wallets[i].shareBps > 0) {
                uint256 amount = balance * wallets[i].shareBps / 10000;
                if (amount > 0) {
                    (bool success, ) = wallets[i].wallet.call{value: amount}("");
                    if (!success) revert TransferFailed();
                    distributed += amount;
                    activeCount++;
                    emit WalletPaid(wallets[i].wallet, amount);
                }
            }
        }

        totalDistributed += distributed;
        emit FeesDistributed(distributed, activeCount);
    }

    // ======== VIEW FUNCTIONS ========

    /**
     * @notice Get all active project wallets
     */
    function getWallets() external view returns (ProjectWallet[] memory activeWallets) {
        uint256 activeCount = 0;
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) activeCount++;
        }

        activeWallets = new ProjectWallet[](activeCount);
        uint256 idx = 0;
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) {
                activeWallets[idx++] = wallets[i];
            }
        }
    }

    /**
     * @notice Get all registered federations
     */
    function getFederations() external view returns (FederationEntry[] memory activeFeds) {
        uint256 activeCount = 0;
        for (uint256 i = 0; i < federations.length; i++) {
            if (federations[i].isActive) activeCount++;
        }

        activeFeds = new FederationEntry[](activeCount);
        uint256 idx = 0;
        for (uint256 i = 0; i < federations.length; i++) {
            if (federations[i].isActive) {
                activeFeds[idx++] = federations[i];
            }
        }
    }

    /**
     * @notice Get active wallet count
     */
    function getActiveWalletCount() external view returns (uint256 count) {
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) count++;
        }
    }

    /**
     * @notice Get registered federation count
     */
    function getActiveFederationCount() external view returns (uint256 count) {
        for (uint256 i = 0; i < federations.length; i++) {
            if (federations[i].isActive) count++;
        }
    }

    /**
     * @notice Check if a wallet is registered and active
     */
    function isWalletActive(address wallet) external view returns (bool) {
        if (!walletExists[wallet]) return false;
        return wallets[walletIndex[wallet]].isActive;
    }

    /**
     * @notice Check if a federation is registered
     */
    function isFederationRegistered(address feeManager) external view returns (bool) {
        return _isFederationActive(feeManager);
    }

    /**
     * @notice Get total active shares in basis points
     */
    function getTotalActiveShareBps() external view returns (uint256 totalBps) {
        for (uint256 i = 0; i < wallets.length; i++) {
            if (wallets[i].isActive) {
                totalBps += wallets[i].shareBps;
            }
        }
    }

    function _isFederationActive(address feeManager) internal view returns (bool) {
        if (!federationRegistered[feeManager]) return false;
        return federations[federationIndex[feeManager]].isActive;
    }
}
