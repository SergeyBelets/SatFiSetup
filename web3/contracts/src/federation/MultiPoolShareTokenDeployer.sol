// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../tokens/MultiPoolShareToken.sol";
import "./ControlledDeployer.sol";

/**
 * @title MultiPoolShareTokenDeployer
 * @notice Deploys one MultiPoolShareToken per call.
 */
contract MultiPoolShareTokenDeployer is ControlledDeployer {
    mapping(address => bytes32) public deployedTokenCodehash;
    event MultiPoolShareTokenDeployed(address indexed shareToken, address indexed multiPool);

    function deployMultiPoolShareToken(string calldata name, string calldata symbol, address multiPool)
        external
        onlyAuthorizedDeployer
        returns (address shareToken)
    {
        shareToken = address(new MultiPoolShareToken(name, symbol, multiPool));
        deployedTokenCodehash[shareToken] = shareToken.codehash;
        emit MultiPoolShareTokenDeployed(shareToken, multiPool);
    }
}
