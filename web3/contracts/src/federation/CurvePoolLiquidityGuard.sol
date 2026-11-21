// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract CurvePoolLiquidityGuard {
    IERC20 public immutable wrapperToken;
    address public immutable curvePool;

    error InvalidAddress();

    constructor(address wrapperToken_, address curvePool_) {
        if (
            wrapperToken_ == address(0) || wrapperToken_.code.length == 0 || curvePool_ == address(0)
                || curvePool_.code.length == 0
        ) revert InvalidAddress();
        wrapperToken = IERC20(wrapperToken_);
        curvePool = curvePool_;
    }

    function availableLiquidity() external view returns (uint256) {
        return wrapperToken.balanceOf(curvePool);
    }
}
