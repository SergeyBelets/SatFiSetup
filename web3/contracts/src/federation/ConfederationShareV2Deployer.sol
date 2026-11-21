// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../tokens/ConfederationShareV2.sol";
import "./ControlledDeployer.sol";

contract ConfederationShareV2Deployer is ControlledDeployer {
    mapping(address => bytes32) public deployedTokenCodehash;
    event ConfederationTokenDeployed(address indexed shareToken, address indexed multiPool);

    function deployConfederationToken(string calldata name, string calldata symbol, address multiPool)
        external
        onlyAuthorizedDeployer
        returns (address shareToken)
    {
        shareToken = address(new ConfederationShareV2(name, symbol, multiPool));
        deployedTokenCodehash[shareToken] = shareToken.codehash;
        emit ConfederationTokenDeployed(shareToken, multiPool);
    }
}
