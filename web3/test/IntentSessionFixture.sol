// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

/// @dev Explicit session fixture for public transport/byte-boundary tests only.
/// Real FROSTCoordinator's wider return ABI is exercised in CustodianMonetaryEpoch.
contract IntentSessionFixture {
    address public immutable dkgRegistry;
    mapping(uint256 => address) private creators;
    constructor(address registry) { dkgRegistry = registry; }
    function open(uint256 id, address creator) external { creators[id] = creator; }
    function getSession(uint256 id) external view returns(uint256, address) {
        return (creators[id] == address(0) ? 0 : id, creators[id]);
    }
}
