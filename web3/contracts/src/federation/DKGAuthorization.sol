// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "../FROSTAuthorization.sol";
import "../FROSTCoordinator.sol";

/// @notice New immutable deployment for a specific completed EVM_AUTH epoch.
contract DKGAuthorization is FROSTAuthorization {
    bytes32 public immutable dkgId;
    bytes32 public immutable resultHash;
    address public immutable registry;

    constructor(address coordinator_, bytes32 id, bytes memory group, address[] memory roster, uint256 threshold_)
        FROSTAuthorization(coordinator_, group, roster, threshold_)
    {
        FROSTCoordinator c = FROSTCoordinator(coordinator_);
        registry = address(c.dkgRegistry());
        (
            ,
            bytes32 result,,
            bytes32 purpose,
            bytes memory canonicalGroup,
            address[] memory canonicalRoster,
            uint256 canonicalThreshold
        ) = c.dkgRegistry().getCompletedResult(id);
        require(
            purpose == keccak256("EVM_AUTH") && keccak256(group) == keccak256(canonicalGroup)
                && keccak256(abi.encode(roster)) == keccak256(abi.encode(canonicalRoster))
                && threshold_ == canonicalThreshold,
            "DKG binding"
        );
        dkgId = id;
        resultHash = result;
    }

    function _checkDKG(uint256 session) internal view override {
        FROSTCoordinator c = FROSTCoordinator(address(coordinator));
        require(c.sessionDKGId(session) == dkgId && c.sessionDKGResult(session) == resultHash, "DKG result mismatch");
    }
}
