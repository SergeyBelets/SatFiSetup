// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/ERC20Permit.sol";
import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

interface IConfederationWithdrawalEscrow {
    function withdrawalEscrow() external view returns (address);
}

/**
 * @title ConfederationShareV2
 * @notice ERC20 share token for confederations (distinct from MultiPoolShareToken)
 * @dev S is issued only after the confederation reserve proves its Bitcoin backing.
 *      Redemption pays Bitcoin from that reserve after FROST authorization and SPV settlement.
 */
contract ConfederationShareV2 is ERC20, ERC20Permit, AccessControl {
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");
    bytes32 public constant BURNER_ROLE = keccak256("BURNER_ROLE");

    /// @notice ConfederationMultiPool contract that manages this token
    address public immutable confederationPool;

    event TokensMinted(address indexed to, uint256 amount);
    event TokensBurned(address indexed from, uint256 amount);

    constructor(
        string memory name_,
        string memory symbol_,
        address confederationPool_
    ) ERC20(name_, symbol_) ERC20Permit(name_) {
        require(confederationPool_ != address(0), "confederationPool=0");
        confederationPool = confederationPool_;

        // Grant all control to ConfederationMultiPool contract
        _grantRole(DEFAULT_ADMIN_ROLE, confederationPool_);
        _grantRole(MINTER_ROLE, confederationPool_);
        _grantRole(BURNER_ROLE, confederationPool_);
    }

    /**
     * @notice Mint new tokens (only ConfederationMultiPool)
     * @param to Recipient address
     * @param amount Amount to mint
     */
    function mint(address to, uint256 amount) external onlyRole(MINTER_ROLE) {
        _mint(to, amount);
        emit TokensMinted(to, amount);
    }

    /**
     * @notice Burn tokens (only ConfederationMultiPool)
     * @param from Address to burn from
     * @param amount Amount to burn
     */
    function burn(address from, uint256 amount) external onlyRole(BURNER_ROLE) {
        _burn(from, amount);
        emit TokensBurned(from, amount);
    }

    /// @notice Only the pool's fixed escrow burns its own locked S after Bitcoin proof settlement.
    function burnHeld(uint256 amount) external {
        require(msg.sender == IConfederationWithdrawalEscrow(confederationPool).withdrawalEscrow(), "escrow only");
        _burn(msg.sender, amount);
    }

    /// @notice Lock shares for withdrawal or refund the original owner.
    function escrow(address from, address to, uint256 amount) external {
        require(msg.sender == confederationPool, "Only pool");
        require(from == confederationPool || to == confederationPool, "Escrow endpoint");
        super._update(from, to, amount);
    }

}
