// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC721} from "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title FederationNFTModifier
 * @notice ERC721 NFT that provides fee discounts to holders
 * @dev Each NFT has metadata including expiration, volume limit, and discount percentage.
 *      The feeManager uses USAGE_ROLE to track volume usage.
 */
contract FederationNFTModifier is ERC721, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");
    bytes32 public constant USAGE_ROLE = keccak256("USAGE_ROLE");

    struct NFTMetadata {
        uint256 expirationTimestamp;
        uint256 volumeLimit;      // Max BTC volume for discount (in token units)
        uint256 usedVolume;
        uint256 discountBps;      // 0-10000 (10000 = 100% = free)
        bool isActive;
    }

    /// @notice Token ID counter
    uint256 private _nextTokenId;

    /// @notice Metadata per token
    mapping(uint256 => NFTMetadata) public nftMetadata;
    struct DiscountReservation {
        uint256 tokenId;
        uint256 volume;
    }
    mapping(address => mapping(bytes32 => DiscountReservation)) private reservations;

    // ======== EVENTS ========

    event NFTMinted(uint256 indexed tokenId, address indexed to, uint256 discountBps, uint256 volumeLimit, uint256 expiration);
    event NFTUsed(uint256 indexed tokenId, uint256 volume, uint256 usedVolume, uint256 remainingVolume);
    event NFTVolumeReleased(uint256 indexed tokenId, uint256 volume, uint256 remainingVolume);
    event NFTExpired(uint256 indexed tokenId);
    event NFTBurned(uint256 indexed tokenId);

    // ======== ERRORS ========

    error NFTNotActive();
    error NFTExpiredError();
    error VolumeLimitExceeded();
    error NotTokenOwner();
    error InvalidDiscount();

    // ======== CONSTRUCTOR ========

    constructor(
        string memory name_,
        string memory symbol_,
        address admin
    ) ERC721(name_, symbol_) {
        require(admin != address(0), "Invalid admin");

        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        _grantRole(MINTER_ROLE, admin);
    }

    // ======== MINTING ========

    /**
     * @notice Mint a new fee discount NFT
     * @param to Recipient address
     * @param expiration Expiration timestamp
     * @param volumeLimit Maximum volume for discount
     * @param discountBps Discount in basis points (0-10000)
     * @return tokenId The minted token ID
     */
    function mint(
        address to,
        uint256 expiration,
        uint256 volumeLimit,
        uint256 discountBps
    ) external onlyRole(MINTER_ROLE) returns (uint256 tokenId) {
        if (discountBps > 10000) revert InvalidDiscount();

        tokenId = _nextTokenId++;
        _mint(to, tokenId);

        nftMetadata[tokenId] = NFTMetadata({
            expirationTimestamp: expiration,
            volumeLimit: volumeLimit,
            usedVolume: 0,
            discountBps: discountBps,
            isActive: true
        });

        emit NFTMinted(tokenId, to, discountBps, volumeLimit, expiration);
    }

    // ======== USAGE ========

    /**
     * @notice Use discount for a transaction volume
     * @dev Only callable by contracts with USAGE_ROLE (typically feeManager)
     * @param tokenId NFT token ID
     * @param volume Transaction volume
     * @return discountBps The discount in basis points
     */
    function useDiscount(uint256 tokenId, uint256 volume) external onlyRole(USAGE_ROLE) returns (uint256 discountBps) {
        if (ownerOf(tokenId) != msg.sender) revert NotTokenOwner();
        return _consumeDiscount(tokenId, volume);
    }

    /**
     * @notice Use discount for an account-owned NFT and transaction volume
     * @dev Only callable by contracts with USAGE_ROLE (typically feeManager)
     * @param tokenId NFT token ID
     * @param account Account that must own the NFT
     * @param volume Transaction volume
     * @return discountBps The discount in basis points
     */
    function useDiscountFor(
        uint256 tokenId,
        address account,
        uint256 volume
    ) external onlyRole(USAGE_ROLE) returns (uint256 discountBps) {
        if (ownerOf(tokenId) != account) revert NotTokenOwner();
        return _consumeDiscount(tokenId, volume);
    }

    /// @notice Public, non-consuming quote with the same ownership/expiry/quota checks as use.
    function previewDiscountFor(uint256 tokenId, address account, uint256 volume)
        external view returns (uint256)
    {
        if (ownerOf(tokenId) != account) revert NotTokenOwner();
        return _validateDiscount(tokenId, volume);
    }

    /// @notice Lock one allowance against the current owner and available volume.
    function reserveDiscountFor(bytes32 id, uint256 tokenId, address account, uint256 volume)
        external onlyRole(USAGE_ROLE) returns (uint256 discountBps)
    {
        require(volume != 0 && reservations[msg.sender][id].volume == 0, "discount reservation");
        if (ownerOf(tokenId) != account) revert NotTokenOwner();
        discountBps = _consumeDiscount(tokenId, volume);
        reservations[msg.sender][id] = DiscountReservation(tokenId, volume);
    }

    /// @notice Only the original reserving contract may settle or release this allowance.
    function closeDiscountReservation(bytes32 id, bool settled) external {
        DiscountReservation memory reservation = reservations[msg.sender][id];
        require(reservation.volume != 0, "discount reservation");
        delete reservations[msg.sender][id];
        if (!settled) {
            NFTMetadata storage meta = nftMetadata[reservation.tokenId];
            meta.usedVolume -= reservation.volume;
            emit NFTVolumeReleased(reservation.tokenId, reservation.volume, meta.volumeLimit - meta.usedVolume);
        }
    }

    function _consumeDiscount(uint256 tokenId, uint256 volume) internal returns (uint256 discountBps) {
        discountBps = _validateDiscount(tokenId, volume);
        NFTMetadata storage meta = nftMetadata[tokenId];
        meta.usedVolume += volume;

        uint256 remaining = meta.volumeLimit - meta.usedVolume;
        emit NFTUsed(tokenId, volume, meta.usedVolume, remaining);
    }

    function _validateDiscount(uint256 tokenId, uint256 volume) internal view returns (uint256) {
        NFTMetadata storage meta = nftMetadata[tokenId];
        if (!meta.isActive) revert NFTNotActive();
        if (block.timestamp >= meta.expirationTimestamp) revert NFTExpiredError();
        if (volume > meta.volumeLimit - meta.usedVolume) revert VolumeLimitExceeded();
        return meta.discountBps;
    }

    // ======== VIEW FUNCTIONS ========

    /**
     * @notice Get discount information for a token
     * @param tokenId NFT token ID
     * @return discountBps Discount in basis points
     * @return remainingVolume Volume remaining for discount
     * @return isValid Whether the NFT is currently valid
     */
    function getDiscount(uint256 tokenId) external view returns (
        uint256 discountBps,
        uint256 remainingVolume,
        bool isValid
    ) {
        NFTMetadata storage meta = nftMetadata[tokenId];
        discountBps = meta.discountBps;
        remainingVolume = meta.volumeLimit > meta.usedVolume ? meta.volumeLimit - meta.usedVolume : 0;
        isValid = meta.isActive && block.timestamp < meta.expirationTimestamp && remainingVolume > 0;
    }

    // ======== BURN ========

    /**
     * @notice Burn an expired NFT (anyone can call)
     * @param tokenId NFT token ID
     */
    function burnExpired(uint256 tokenId) external {
        NFTMetadata storage meta = nftMetadata[tokenId];
        require(block.timestamp >= meta.expirationTimestamp, "NFT not expired");

        meta.isActive = false;
        _burn(tokenId);

        emit NFTExpired(tokenId);
        emit NFTBurned(tokenId);
    }

    // ======== OVERRIDES ========

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, AccessControl) returns (bool) {
        return super.supportsInterface(interfaceId);
    }
}
