// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "../FROSTAuthorization.sol";
import "./ControlledDeployer.sol";
import "./DKGAuthorization.sol";

/**
 * @notice Deploys one production FROSTAuthorization per call.
 */
contract FROSTAuthorizationDeployer is ControlledDeployer {
    event FROSTAuthorizationDeployed(address indexed authorization, address indexed coordinator);

    function deployFROSTAuthorization(
        address coordinator,
        bytes calldata groupPubkey,
        address[] calldata participants,
        uint256 threshold
    ) external onlyAuthorizedDeployer returns (address authorization) {
        authorization = address(new FROSTAuthorization(coordinator, groupPubkey, participants, threshold));
        emit FROSTAuthorizationDeployed(authorization, coordinator);
    }

    function deployFROSTAuthorizationV1(
        address coordinator,
        bytes calldata groupPubkey,
        address[] calldata participants,
        uint256 threshold,
        bytes32 dkgId
    ) external onlyAuthorizedDeployer returns (address authorization) {
        authorization = address(new DKGAuthorization(coordinator, dkgId, groupPubkey, participants, threshold));
        emit FROSTAuthorizationDeployed(authorization, coordinator);
    }
}
