// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Unbacked test accounting token, restricted to explicit integration chains.
contract DAOTestUSD is ERC20 {
    constructor() ERC20("Test USD", "TUSD") {
        require(block.chainid == 31337 || block.chainid == 11155111, "integration network only");
    }

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

