// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract CustodianDAOToken is ERC20 {
    uint256 public constant INITIAL_SUPPLY = 10_000_000 ether;

    error InvalidRecipient();
    error InvalidMetadata();

    constructor(string memory name_, string memory symbol_, address recipient) ERC20(name_, symbol_) {
        if (bytes(name_).length == 0 || bytes(symbol_).length == 0) revert InvalidMetadata();
        if (recipient == address(0)) revert InvalidRecipient();
        _mint(recipient, INITIAL_SUPPLY);
    }
}
