// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title ControlledDeployer
 * @notice Minimal allowlist for infrastructure deployer contracts.
 * @dev The deployer owner is authorized by default and can authorize canonical
 *      factory/orchestrator contracts after the full deployment graph is known.
 */
abstract contract ControlledDeployer {
    address public immutable deployerOwner;
    mapping(address => bool) public authorizedDeployers;

    event AuthorizedDeployerSet(address indexed deployer, bool authorized);

    error NotDeployerOwner();
    error UnauthorizedDeployer(address caller);
    error InvalidAuthorizedDeployer();

    constructor() {
        deployerOwner = msg.sender;
        authorizedDeployers[msg.sender] = true;
        emit AuthorizedDeployerSet(msg.sender, true);
    }

    modifier onlyDeployerOwner() {
        if (msg.sender != deployerOwner) revert NotDeployerOwner();
        _;
    }

    modifier onlyAuthorizedDeployer() {
        if (!authorizedDeployers[msg.sender]) revert UnauthorizedDeployer(msg.sender);
        _;
    }

    function setAuthorizedDeployer(address deployer, bool authorized) external onlyDeployerOwner {
        if (deployer == address(0)) revert InvalidAuthorizedDeployer();
        authorizedDeployers[deployer] = authorized;
        emit AuthorizedDeployerSet(deployer, authorized);
    }
}
