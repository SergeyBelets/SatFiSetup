// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./IFeeManager.sol";
import "../FROSTAuthorization.sol";
import "./CustodianValidator.sol";
import "./FeeDistributionCalculator.sol";
import "./ProjectWalletRegistry.sol";
import "../tokens/FederationNFTModifier.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/**
 * @title FederationFeeManager
 * @notice Manages fee calculation, collection, and distribution for federations/confederations
 * @dev Supports fixed percentage and dynamic fee models with optional NFT-based discounts
 */
contract FederationFeeManager is IFeeManager, ReentrancyGuard {
    uint256 public constant MAX_BPS = 10000;

    // ======== STATE ========

    /// @notice FROST multisig for governance
    FROSTAuthorization public immutable frostMultisig;

    /// @notice Custodian validator (address(0) for federations, set for confederations)
    CustodianValidator public custodianValidator;

    /// @notice Treasury address for collected fees
    address public treasury;

    /// @notice Fee configuration for wrap (deposit) operations
    FeeConfig public wrapFeeConfig;

    /// @notice Fee configuration for unwrap (withdrawal) operations
    FeeConfig public unwrapFeeConfig;

    /// @notice Total fees collected (for tracking)
    uint256 public totalCollected;

    /// @notice NFT modifier for fee discounts (can be address(0))
    FederationNFTModifier public nftModifier;

    /// @notice Fee distribution calculator
    FeeDistributionCalculator public feeDistributor;

    /// @notice Project wallet registry
    ProjectWalletRegistry public walletRegistry;

    /// @notice Portion of total fees going to project wallets (basis points)
    uint256 public projectWalletShareBps;
    uint256 public operationNonce;
    mapping(address => bool) public override authorizedFeeCollectors;
    mapping(address => mapping(bool => uint256)) public collectedFeesByToken;
    mapping(bytes32 => bytes32) public nftOperationReceipts;
    mapping(bytes32 => uint256) public nftMintedTokenIds;
    mapping(bytes32 => address) public nftCreatedCollections;
    mapping(address => bool) public managedNFTCollections;

    // ======== EVENTS ========

    event FeeCollected(address indexed pool, uint256 amount, bool isWrap);
    event TokenFeeCollected(address indexed pool, address indexed token, uint256 amount, bool isWrap);
    event FeeConfigUpdated(
        bool isWrap, FeeType feeType, uint256 fixedBps, uint256 dynamicMinFee, uint256 dynamicCoeffBps
    );
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);
    event FeesDistributed(uint256 totalAmount, uint256 projectWalletAmount, uint256 memberCustodianAmount);
    event FeeCollectorAuthorizationUpdated(address indexed collector, bool authorized);
    event NFTModifierCreated(address indexed collection, bytes32 indexed operationHash);
    event NFTMintedWithApproval(address indexed collection, uint256 indexed tokenId, bytes32 indexed operationHash);

    // ======== ERRORS ========

    error InvalidTreasury();
    error InsufficientFROSTSignatures();
    error InsufficientCustodianSignatures();
    error InvalidFeeConfig();
    error UnauthorizedFeeCollector();
    error InvalidNFTConfiguration();

    // ======== CONSTRUCTOR ========

    constructor(
        address _frostMultisig,
        address _custodianValidator,
        address _treasury,
        FeeConfig memory _wrapConfig,
        FeeConfig memory _unwrapConfig
    ) {
        require(_frostMultisig != address(0), "Invalid FROST address");
        require(_treasury != address(0), "Invalid treasury");

        frostMultisig = FROSTAuthorization(_frostMultisig);
        if (_custodianValidator != address(0)) {
            custodianValidator = CustodianValidator(_custodianValidator);
        }
        _validateFeeConfig(_wrapConfig);
        _validateFeeConfig(_unwrapConfig);
        treasury = _treasury;
        wrapFeeConfig = _wrapConfig;
        unwrapFeeConfig = _unwrapConfig;
    }

    // ======== FEE CALCULATION ========

    /**
     * @notice Calculate fee for a wrap or unwrap operation
     * @param isWrap True for deposit, false for withdrawal
     * @param amount The amount to calculate fee on
     * @return fee The calculated fee amount
     */
    function calculateFee(bool isWrap, uint256 amount) external view override returns (uint256 fee) {
        FeeConfig storage config = isWrap ? wrapFeeConfig : unwrapFeeConfig;
        return _calculateFee(config, amount);
    }

    /**
     * @notice Calculate fee with NFT discount applied
     * @param isWrap True for deposit, false for withdrawal
     * @param amount The amount to calculate fee on
     * @param discountBps Discount in basis points (0-10000)
     * @return fee The calculated fee amount after discount
     */
    function calculateFeeWithDiscount(bool isWrap, uint256 amount, uint256 discountBps)
        external
        view
        override
        returns (uint256 fee)
    {
        FeeConfig storage config = isWrap ? wrapFeeConfig : unwrapFeeConfig;
        return _calculateFeeWithDiscount(config, amount, discountBps);
    }

    /**
     * @notice Calculate fee using an NFT discount and consume NFT volume allowance.
     * @param isWrap True for deposit, false for withdrawal
     * @param amount The amount to calculate fee on
     * @param nftTokenId NFT token ID to consume
     * @param account Account that must own the NFT
     * @return fee The calculated fee amount after NFT discount
     */
    function calculateFeeWithNFT(bool isWrap, uint256 amount, uint256 nftTokenId, address account)
        external
        override
        nonReentrant
        returns (uint256 fee)
    {
        // Only reviewed pool contracts may consume account-owned quota. Public
        // wallet previews use quoteFeeWithNFT and never enter this mutation path.
        if (!authorizedFeeCollectors[msg.sender] || msg.sender.code.length == 0) revert UnauthorizedFeeCollector();
        FeeConfig storage config = isWrap ? wrapFeeConfig : unwrapFeeConfig;
        uint256 discountBps = 0;

        if (address(nftModifier) != address(0)) {
            discountBps = nftModifier.useDiscountFor(nftTokenId, account, amount);
        }

        return _calculateFeeWithDiscount(config, amount, discountBps);
    }

    function quoteFeeWithNFT(bool isWrap, uint256 amount, uint256 nftTokenId, address account)
        external view override returns (uint256 fee)
    {
        uint256 discount = address(nftModifier) == address(0)
            ? 0 : nftModifier.previewDiscountFor(nftTokenId, account, amount);
        return _calculateFeeWithDiscount(isWrap ? wrapFeeConfig : unwrapFeeConfig, amount, discount);
    }

    // ======== GOVERNANCE ========

    /// @notice Create an optional managed collection after target-bound FROST/custodian approval.
    /// @dev The manager is its issuer/admin; no arbitrary-call execution is exposed.
    function createNFTModifier(string calldata name, string calldata symbol, bytes32 operationHash)
        external nonReentrant returns (address collection)
    {
        if (address(nftModifier) != address(0) || bytes(name).length == 0 || bytes(symbol).length == 0) {
            revert InvalidNFTConfiguration();
        }
        _verifyApproval(operationHash, getCreateNFTModifierOperationHash(name, symbol));
        FederationNFTModifier nft = new FederationNFTModifier(name, symbol, address(this));
        nft.grantRole(nft.USAGE_ROLE(), address(this));
        nftModifier = nft;
        collection = address(nft);
        managedNFTCollections[collection] = true;
        nftCreatedCollections[operationHash] = collection;
        nftOperationReceipts[operationHash] = keccak256(abi.encode(this.createNFTModifier.selector, name, symbol, collection));
        emit NFTModifierCreated(collection, operationHash);
    }

    /// @notice Mint in the exact approved collection; any relayer may deliver the approved call.
    function mintNFT(address collection, address to, uint256 expiration, uint256 volumeLimit,
        uint256 discountBps, bytes32 operationHash) external nonReentrant returns (uint256 tokenId)
    {
        if (collection == address(0) || collection != address(nftModifier) || to == address(0)
            || expiration <= block.timestamp || volumeLimit == 0 || discountBps > MAX_BPS) {
            revert InvalidNFTConfiguration();
        }
        _verifyApproval(operationHash, getMintNFTOperationHash(collection, to, expiration, volumeLimit, discountBps));
        tokenId = nftModifier.mint(to, expiration, volumeLimit, discountBps);
        nftOperationReceipts[operationHash] = keccak256(abi.encode(this.mintNFT.selector,
            collection, to, expiration, volumeLimit, discountBps));
        nftMintedTokenIds[operationHash] = tokenId;
        emit NFTMintedWithApproval(collection, tokenId, operationHash);
    }

    /**
     * @notice Update fee configuration
     * @param isWrap True for wrap config, false for unwrap config
     * @param config New fee configuration
     * @param operationHash FROST-signed operation hash
     */
    function setFeeConfig(bool isWrap, FeeConfig calldata config, bytes32 operationHash) external {
        _validateFeeConfig(config);
        _verifyApproval(operationHash, getSetFeeConfigOperationHash(isWrap, config));

        if (isWrap) {
            wrapFeeConfig = config;
        } else {
            unwrapFeeConfig = config;
        }

        emit FeeConfigUpdated(isWrap, config.feeType, config.fixedBps, config.dynamicMinFee, config.dynamicCoeffBps);
    }

    /**
     * @notice Update treasury address
     * @param newTreasury New treasury address
     * @param operationHash FROST-signed operation hash
     */
    function setTreasury(address newTreasury, bytes32 operationHash) external {
        if (newTreasury == address(0)) revert InvalidTreasury();
        _verifyApproval(operationHash, getSetTreasuryOperationHash(newTreasury));

        address oldTreasury = treasury;
        treasury = newTreasury;

        emit TreasuryUpdated(oldTreasury, newTreasury);
    }

    /**
     * @notice Set NFT modifier contract
     * @param _nftModifier NFT modifier address
     * @param operationHash FROST-signed operation hash
     */
    function setNFTModifier(address _nftModifier, bytes32 operationHash) external {
        _verifyApproval(operationHash, getSetNFTModifierOperationHash(_nftModifier));
        nftModifier = FederationNFTModifier(_nftModifier);
    }

    /**
     * @notice Set fee distribution calculator
     * @param _feeDistributor Fee distribution calculator address
     * @param operationHash FROST-signed operation hash
     */
    function setFeeDistributor(address _feeDistributor, bytes32 operationHash) external {
        _verifyApproval(operationHash, getSetFeeDistributorOperationHash(_feeDistributor));
        feeDistributor = FeeDistributionCalculator(_feeDistributor);
    }

    /**
     * @notice Set wallet registry
     * @param _walletRegistry Wallet registry address
     * @param operationHash FROST-signed operation hash
     */
    function setWalletRegistry(address _walletRegistry, bytes32 operationHash) external {
        _verifyApproval(operationHash, getSetWalletRegistryOperationHash(_walletRegistry));
        walletRegistry = ProjectWalletRegistry(_walletRegistry);
    }

    /**
     * @notice Set project wallet share percentage
     * @param _shareBps Share in basis points
     * @param operationHash FROST-signed operation hash
     */
    function setProjectWalletShareBps(uint256 _shareBps, bytes32 operationHash) external {
        require(_shareBps <= MAX_BPS, "Share exceeds 100%");
        _verifyApproval(operationHash, getSetProjectWalletShareBpsOperationHash(_shareBps));
        projectWalletShareBps = _shareBps;
    }

    /**
     * @notice Authorize or revoke a pool/fee collector that may record collected fees.
     * @param collector Pool or fee collector contract
     * @param authorized Whether the collector is allowed
     * @param operationHash FROST-signed operation hash
     */
    function setFeeCollector(address collector, bool authorized, bytes32 operationHash) external {
        if (collector == address(0)) revert UnauthorizedFeeCollector();
        _verifyApproval(operationHash, getSetFeeCollectorOperationHash(collector, authorized));
        authorizedFeeCollectors[collector] = authorized;
        emit FeeCollectorAuthorizationUpdated(collector, authorized);
    }

    /**
     * @notice Record collected fee (called by pool contracts)
     * @param amount Fee amount collected
     * @param isWrap Whether this was a wrap (deposit) fee
     */
    function recordFeeCollected(uint256 amount, bool isWrap) external {
        if (!authorizedFeeCollectors[msg.sender]) revert UnauthorizedFeeCollector();
        totalCollected += amount;
        emit FeeCollected(msg.sender, amount, isWrap);
    }

    function recordFeeCollectedForToken(address token, uint256 amount, bool isWrap) external override {
        if (!authorizedFeeCollectors[msg.sender] || token == address(0)) revert UnauthorizedFeeCollector();
        collectedFeesByToken[token][isWrap] += amount;
        totalCollected += amount;
        emit TokenFeeCollected(msg.sender, token, amount, isWrap);
    }

    function getFeeSecurityConfig() external view override returns (address authorization, address validator) {
        return (address(frostMultisig), address(custodianValidator));
    }

    // ======== INTERNAL ========

    function _calculateFee(FeeConfig storage config, uint256 amount) internal view returns (uint256) {
        uint256 fee;
        if (config.feeType == FeeType.FixedPercentage) {
            fee = amount * config.fixedBps / MAX_BPS;
        } else {
            // Dynamic: minFee + amount * coeffBps / 10000
            fee = config.dynamicMinFee + amount * config.dynamicCoeffBps / MAX_BPS;
        }
        return fee > amount ? amount : fee;
    }

    function _calculateFeeWithDiscount(FeeConfig storage config, uint256 amount, uint256 discountBps)
        internal
        view
        returns (uint256 fee)
    {
        fee = _calculateFee(config, amount);
        if (discountBps > 0 && discountBps <= MAX_BPS) {
            fee = fee * (MAX_BPS - discountBps) / MAX_BPS;
        }
    }

    function _validateFeeConfig(FeeConfig memory config) internal pure {
        if (config.feeType == FeeType.FixedPercentage) {
            if (config.fixedBps > MAX_BPS) revert InvalidFeeConfig();
        } else if (config.feeType == FeeType.Dynamic) {
            if (config.dynamicCoeffBps > MAX_BPS) revert InvalidFeeConfig();
        } else {
            revert InvalidFeeConfig();
        }
    }

    function _verifyApproval(bytes32 operationHash, bytes32 expectedHash) internal {
        require(operationHash == expectedHash, "Operation hash mismatch");
        if (!frostMultisig.isOperationApproved(operationHash, address(this))) {
            revert InsufficientFROSTSignatures();
        }
        // For confederations, also verify custodian threshold
        if (address(custodianValidator) != address(0)) {
            if (!custodianValidator.meetsMinimumCustodians(operationHash)) {
                revert InsufficientCustodianSignatures();
            }
        }
        frostMultisig.consumeOperation(operationHash);
        operationNonce++;
    }

    // ======== VIEW FUNCTIONS ========

    function getWrapFeeConfig() external view returns (FeeConfig memory) {
        return wrapFeeConfig;
    }

    function getUnwrapFeeConfig() external view returns (FeeConfig memory) {
        return unwrapFeeConfig;
    }

    function getSetFeeConfigOperationHash(bool isWrap, FeeConfig memory config) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setFeeConfig.selector, keccak256(abi.encode(isWrap, config)), operationNonce
        );
    }

    function getCreateNFTModifierOperationHash(string memory name, string memory symbol) public view returns (bytes32) {
        return frostMultisig.hashOperation(address(this), this.createNFTModifier.selector,
            keccak256(abi.encode(name, symbol)), operationNonce);
    }

    function getMintNFTOperationHash(address collection, address to, uint256 expiration,
        uint256 volumeLimit, uint256 discountBps) public view returns (bytes32)
    {
        return frostMultisig.hashOperation(address(this), this.mintNFT.selector,
            keccak256(abi.encode(collection, to, expiration, volumeLimit, discountBps)), operationNonce);
    }

    function getSetTreasuryOperationHash(address newTreasury) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setTreasury.selector, keccak256(abi.encode(newTreasury)), operationNonce
        );
    }

    function getSetNFTModifierOperationHash(address _nftModifier) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setNFTModifier.selector, keccak256(abi.encode(_nftModifier)), operationNonce
        );
    }

    function getSetFeeDistributorOperationHash(address _feeDistributor) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setFeeDistributor.selector, keccak256(abi.encode(_feeDistributor)), operationNonce
        );
    }

    function getSetWalletRegistryOperationHash(address _walletRegistry) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setWalletRegistry.selector, keccak256(abi.encode(_walletRegistry)), operationNonce
        );
    }

    function getSetProjectWalletShareBpsOperationHash(uint256 _shareBps) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setProjectWalletShareBps.selector, keccak256(abi.encode(_shareBps)), operationNonce
        );
    }

    function getSetFeeCollectorOperationHash(address collector, bool authorized) public view returns (bytes32) {
        return frostMultisig.hashOperation(
            address(this), this.setFeeCollector.selector, keccak256(abi.encode(collector, authorized)), operationNonce
        );
    }
}
