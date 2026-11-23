// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {SignatureChecker} from "@openzeppelin/contracts/utils/cryptography/SignatureChecker.sol";
import {FederationNFTModifier} from "../tokens/FederationNFTModifier.sol";
import {ConfederationDAOIncome} from "./ConfederationDAOIncome.sol";
import {IFeeManager} from "./IFeeManager.sol";

contract ConfederationNFTPass is FederationNFTModifier, EIP712, ReentrancyGuard {
    using SafeERC20 for IERC20;

    struct Tariff {
        uint64 durationSeconds;
        uint16 discountBps;
        uint256 volumeLimit;
        uint256 minimumPrice;
        uint64 salesUntil;
    }

    struct Quote {
        bytes32 tariffId;
        address buyer;
        uint256 price;
        uint256 nonce;
        uint64 expiresAt;
        uint64 priceEpoch;
    }

    bytes32 private constant PRICE_TYPEHASH = keccak256(
        "Price(bytes32 tariffId,address buyer,address token,uint256 price,uint256 nonce,uint64 expiresAt,uint64 priceEpoch)"
    );

    ConfederationDAOIncome public immutable income;
    address public immutable initialPriceSigner;
    address public priceSigner;
    uint64 public priceEpoch = 1;
    bool public salesPaused;
    mapping(bytes32 => Tariff) public tariffs;
    mapping(address => mapping(uint256 => bool)) public usedQuote;
    uint256 public operationNonce;

    event TariffApproved(bytes32 indexed id, uint64 durationSeconds, uint16 discountBps, uint256 volumeLimit,
        uint256 minimumPrice, uint64 salesUntil);
    event PriceControlChanged(address indexed signer, uint64 indexed epoch, bool paused);
    event PassPurchased(uint256 indexed tokenId, bytes32 indexed tariffId, address indexed buyer, uint256 price);

    error InvalidTariff();
    error InvalidPriceControl();
    error InvalidQuote();
    error InvalidPayment();

    constructor(string memory name_, string memory symbol_, address income_, address feeManager, address operator)
        FederationNFTModifier(name_, symbol_, address(this))
        EIP712("SatoshiFi Confederation Pass", "2")
    {
        income = ConfederationDAOIncome(income_);
        initialPriceSigner = operator;
        priceSigner = operator;
        (address auth, address validator) = IFeeManager(feeManager).getFeeSecurityConfig();
        if (operator == address(0) || auth != address(income.authorization()) || validator != address(income.validator())) {
            revert InvalidTariff();
        }
        _grantRole(USAGE_ROLE, feeManager);
    }

    function getApproveTariffOperationHash(Tariff calldata tariff) public view returns (bytes32) {
        return income.authorization().hashOperation(address(this), this.approveTariff.selector,
            keccak256(abi.encode(tariff)), operationNonce);
    }

    function approveTariff(Tariff calldata tariff, bytes32 op) external {
        bytes32 id = keccak256(abi.encode(tariff));
        if (tariff.durationSeconds == 0 || tariff.discountBps == 0 || tariff.discountBps > 10_000
            || tariff.volumeLimit == 0 || tariff.minimumPrice == 0 || tariff.salesUntil <= block.timestamp
            || tariffs[id].durationSeconds != 0 || op != getApproveTariffOperationHash(tariff)
            || !_hasCustodianApproval(op)) revert InvalidTariff();
        income.authorization().consumeOperation(op);
        ++operationNonce;
        tariffs[id] = tariff;
        emit TariffApproved(id, tariff.durationSeconds, tariff.discountBps, tariff.volumeLimit,
            tariff.minimumPrice, tariff.salesUntil);
    }

    function getSetPriceControlOperationHash(address signer, bool paused) public view returns (bytes32) {
        return income.authorization().hashOperation(address(this), this.setPriceControl.selector,
            keccak256(abi.encode(signer, paused)), operationNonce);
    }

    /// @notice An effective pause, resume or rotation invalidates every outstanding quote.
    function setPriceControl(address signer, bool paused, bytes32 op) external {
        if (signer == address(0) || (signer == priceSigner && paused == salesPaused)
            || op != getSetPriceControlOperationHash(signer, paused) || !_hasCustodianApproval(op)) {
            revert InvalidPriceControl();
        }
        income.authorization().consumeOperation(op);
        ++operationNonce;
        priceSigner = signer;
        salesPaused = paused;
        ++priceEpoch;
        emit PriceControlChanged(signer, priceEpoch, paused);
    }

    function _hasCustodianApproval(bytes32 op) private view returns (bool) {
        uint256 count = income.validator().getCustodianCount();
        return count != 0 && income.validator().custodianSignatureCount(op) >= (2 * count + 2) / 3;
    }

    function priceDigest(Quote calldata quote) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(PRICE_TYPEHASH, quote.tariffId, quote.buyer, income.shareToken(),
            quote.price, quote.nonce, quote.expiresAt, quote.priceEpoch)));
    }

    function buy(Quote calldata quote, bytes calldata signature) external nonReentrant returns (uint256 tokenId) {
        Tariff memory tariff = tariffs[quote.tariffId];
        if (salesPaused || quote.priceEpoch != priceEpoch || tariff.durationSeconds == 0 || quote.buyer != msg.sender
            || quote.price < tariff.minimumPrice || quote.expiresAt > tariff.salesUntil
            || block.timestamp >= quote.expiresAt || usedQuote[msg.sender][quote.nonce]
            || !SignatureChecker.isValidSignatureNow(priceSigner, priceDigest(quote), signature)) revert InvalidQuote();

        usedQuote[msg.sender][quote.nonce] = true;
        IERC20 shares = IERC20(income.shareToken());
        uint256 beforeBalance = shares.balanceOf(address(income));
        shares.safeTransferFrom(msg.sender, address(income), quote.price);
        if (shares.balanceOf(address(income)) - beforeBalance != quote.price) revert InvalidPayment();
        income.recordShareIncome(ConfederationDAOIncome.Stream.Pass, quote.price);
        tokenId = this.mint(msg.sender, block.timestamp + tariff.durationSeconds, tariff.volumeLimit, tariff.discountBps);
        emit PassPurchased(tokenId, quote.tariffId, msg.sender, quote.price);
    }
}
