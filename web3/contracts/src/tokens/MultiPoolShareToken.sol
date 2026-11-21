// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title MultiPoolShareToken (S-token)
 * @notice Share token representing ownership in a MultiPool
 * @dev Users receive S-tokens when they deposit MP tokens from individual pools
 *      S-tokens can be redeemed for a proportional share of all pools in MultiPool
 */
contract MultiPoolShareToken is ERC20, ERC20Permit, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");
    bytes32 public constant BURNER_ROLE = keccak256("BURNER_ROLE");

    /// @notice MultiPool contract that manages this share token
    address public immutable multiPool;

    event SharesMinted(address indexed to, uint256 amount);
    event SharesBurned(address indexed from, uint256 amount);

    error ShareTransfersDisabled();

    constructor(
        string memory name_,
        string memory symbol_,
        address multiPool_
    ) ERC20(name_, symbol_) ERC20Permit(name_) {
        require(multiPool_ != address(0), "multiPool=0");
        multiPool = multiPool_;

        // Grant all control to MultiPool contract
        _grantRole(DEFAULT_ADMIN_ROLE, multiPool_);
        _grantRole(MINTER_ROLE, multiPool_);
        _grantRole(BURNER_ROLE, multiPool_);
    }

    /**
     * @notice Mint new share tokens (only MultiPool)
     * @param to Recipient address
     * @param amount Amount to mint
     */
    function mint(address to, uint256 amount) external onlyRole(MINTER_ROLE) {
        _mint(to, amount);
        emit SharesMinted(to, amount);
    }

    /**
     * @notice Burn share tokens (only MultiPool)
     * @param from Address to burn from
     * @param amount Amount to burn
     */
    function burn(address from, uint256 amount) external onlyRole(BURNER_ROLE) {
        _burn(from, amount);
        emit SharesBurned(from, amount);
    }

    /// @notice Move shares into/out of escrow without making shares transferable.
    function escrow(address from, address to, uint256 amount) external {
        require(msg.sender == multiPool, "Only pool");
        require(from == multiPool || to == multiPool, "Escrow endpoint");
        super._update(from, to, amount);
    }

    function _update(address from, address to, uint256 value) internal override {
        if (from != address(0) && to != address(0)) revert ShareTransfersDisabled();
        super._update(from, to, value);
    }
}
