// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../tokens/ConfederationToken.sol";
import "./ControlledDeployer.sol";

contract ConfederationTokenDeployer is ControlledDeployer {
    mapping(address => bytes32) public deployedTokenCodehash;
    event ConfederationTokenDeployed(address indexed shareToken, address indexed multiPool);

    function deployConfederationToken(string calldata name, string calldata symbol, address multiPool)
        external
        onlyAuthorizedDeployer
        returns (address shareToken)
    {
        shareToken = address(new ConfederationToken(name, symbol, multiPool));
        deployedTokenCodehash[shareToken] = shareToken.codehash;
        emit ConfederationTokenDeployed(shareToken, multiPool);
    }
}
