// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/utils/math/Math.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @notice Explicit funded treasury quote, not an oracle or a market-priced DEX.
/// Reward liquidity must be deposited before conversion. There is no mint path.
contract FundedFeeConverter is ReentrancyGuard {
    using SafeERC20 for IERC20;
    address public immutable treasury = msg.sender;
    IERC20 public immutable inputToken;
    IERC20 public immutable rewardToken;
    uint256 public immutable numerator;
    uint256 public immutable denominator;

    constructor(address input, address reward, uint256 n, uint256 d) {
        require(block.chainid == 1 || block.chainid == 31337 || block.chainid == 11155111, "unsupported chain");
        require(input.code.length > 0 && reward.code.length > 0 && input != reward && n > 0 && d > 0, "config");
        inputToken = IERC20(input);
        rewardToken = IERC20(reward);
        numerator = n;
        denominator = d;
    }

    /// @notice Treasury can recover inventory; no user order remains open across transactions.
    function withdrawLiquidity(address token, uint256 amount) external nonReentrant {
        require(
            msg.sender == treasury && (token == address(inputToken) || token == address(rewardToken)),
            "treasury inventory"
        );
        IERC20(token).safeTransfer(treasury, amount);
    }

    function convert(uint256 amount, uint256 minimum, uint256 deadline) external nonReentrant returns (uint256 out) {
        require(block.timestamp <= deadline && amount > 0, "expired or empty");
        out = Math.mulDiv(amount, numerator, denominator);
        require(out >= minimum && out > 0, "minimum output");
        uint256 beforeBalance = inputToken.balanceOf(address(this));
        inputToken.safeTransferFrom(msg.sender, address(this), amount);
        require(inputToken.balanceOf(address(this)) == beforeBalance + amount, "inexact input");
        rewardToken.safeTransfer(msg.sender, out);
    }
}
