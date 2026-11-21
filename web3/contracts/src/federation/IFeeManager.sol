// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IFeeManager
 * @notice Interface for fee calculation and management
 */
interface IFeeManager {
    enum FeeType { FixedPercentage, Dynamic }

    struct FeeConfig {
        FeeType feeType;
        uint256 fixedBps;         // For FixedPercentage: basis points (e.g. 100 = 1%)
        uint256 dynamicMinFee;    // For Dynamic: minimum fee amount
        uint256 dynamicCoeffBps;  // For Dynamic: coefficient in basis points
    }

    /**
     * @notice Calculate fee for a wrap or unwrap operation
     * @param isWrap True for deposit/wrap, false for withdrawal/unwrap
     * @param amount The amount to calculate fee on
     * @return fee The calculated fee amount
     */
    function calculateFee(bool isWrap, uint256 amount) external view returns (uint256 fee);

    /**
     * @notice Calculate fee with NFT discount applied
     * @param isWrap True for deposit/wrap, false for withdrawal/unwrap
     * @param amount The amount to calculate fee on
     * @param discountBps Discount in basis points (e.g. 5000 = 50% off)
     * @return fee The calculated fee amount after discount
     */
    function calculateFeeWithDiscount(bool isWrap, uint256 amount, uint256 discountBps) external view returns (uint256 fee);

    /**
     * @notice Calculate fee using an NFT discount and consume the NFT volume allowance
     * @param isWrap True for deposit/wrap, false for withdrawal/unwrap
     * @param amount The amount to calculate fee on and consume from NFT allowance
     * @param nftTokenId NFT token ID to consume
     * @param account Account that must own the NFT
     * @return fee The calculated fee amount after NFT discount
     */
    function calculateFeeWithNFT(
        bool isWrap,
        uint256 amount,
        uint256 nftTokenId,
        address account
    ) external returns (uint256 fee);

    /// @notice Non-consuming NFT quote; safe for wallets and arbitrary callers.
    function quoteFeeWithNFT(bool isWrap, uint256 amount, uint256 nftTokenId, address account)
        external view returns (uint256 fee);

    function authorizedFeeCollectors(address collector) external view returns (bool);
    function getFeeSecurityConfig() external view returns (address authorization, address validator);
    function recordFeeCollectedForToken(address token, uint256 amount, bool isWrap) external;
}
