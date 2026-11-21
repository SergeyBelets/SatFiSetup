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
    }

    struct Quote {
        bytes32 tariffId;
        address buyer;
        uint256 price;
        uint256 nonce;
        uint64 expiresAt;
    }

    bytes32 private constant PRICE_TYPEHASH =
        keccak256("Price(bytes32 tariffId,address buyer,address token,uint256 price,uint256 nonce,uint64 expiresAt)");

    ConfederationDAOIncome public immutable income;
    address public immutable priceSigner;
    mapping(bytes32 => Tariff) public tariffs;
    mapping(address => mapping(uint256 => bool)) public usedQuote;
    uint256 public operationNonce;

    event TariffApproved(bytes32 indexed id, uint64 durationSeconds, uint16 discountBps, uint256 volumeLimit);
    event PassPurchased(uint256 indexed tokenId, bytes32 indexed tariffId, address indexed buyer, uint256 price);

    error InvalidTariff();
    error InvalidQuote();
    error InvalidPayment();

    constructor(string memory name_, string memory symbol_, address income_, address feeManager, address operator)
        FederationNFTModifier(name_, symbol_, address(this))
        EIP712("SatoshiFi Confederation Pass", "1")
    {
        income = ConfederationDAOIncome(income_);
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
            || tariff.volumeLimit == 0 || tariffs[id].durationSeconds != 0) revert InvalidTariff();
        uint256 count = income.validator().getCustodianCount();
        if (count == 0 || op != getApproveTariffOperationHash(tariff)
            || income.validator().custodianSignatureCount(op) < (2 * count + 2) / 3) revert InvalidTariff();
        income.authorization().consumeOperation(op);
        ++operationNonce;
        tariffs[id] = tariff;
        emit TariffApproved(id, tariff.durationSeconds, tariff.discountBps, tariff.volumeLimit);
    }

    function priceDigest(Quote calldata quote) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(PRICE_TYPEHASH, quote.tariffId, quote.buyer, income.shareToken(),
            quote.price, quote.nonce, quote.expiresAt)));
    }

    function buy(Quote calldata quote, bytes calldata signature) external nonReentrant returns (uint256 tokenId) {
        Tariff memory tariff = tariffs[quote.tariffId];
        if (tariff.durationSeconds == 0 || quote.buyer != msg.sender || quote.price == 0
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
