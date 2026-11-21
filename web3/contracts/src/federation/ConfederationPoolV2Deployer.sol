// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./ConfederationPoolV2.sol";
import "./ControlledDeployer.sol";
import "./ConfederationShareV2Deployer.sol";
import {RLP} from "@openzeppelin/contracts/utils/RLP.sol";

/**
 * @title ConfederationPoolV2Deployer
 * @notice Deploys ConfederationPoolV2 contract
 * @dev Separated from ConfederationDeployer to reduce contract size
 */
contract ConfederationPoolV2Deployer is ControlledDeployer {
    ConfederationShareV2Deployer public immutable shareTokenDeployer;
    uint256 public nextMultiPoolNonce = 1;
    /// @notice Creation provenance; callers cannot register an existing contract.
    mapping(address => bytes32) public deployedPoolCodehash;

    error InvalidSubDeployer();
    error UnexpectedMultiPoolAddress();

    constructor(address _shareTokenDeployer) {
        if (_shareTokenDeployer == address(0)) revert InvalidSubDeployer();
        shareTokenDeployer = ConfederationShareV2Deployer(_shareTokenDeployer);
    }

    event MultiPoolDeployed(
        string indexed confederationId, address multiPool, address shareToken, address indexed caller
    );

    /**
     * @notice Deploy confederation multipool
     * @param confederationId Confederation identifier
     * @param frostMultisig Address of the FROST multisig (already deployed)
     * @param custodianValidator Address of the custodian validator (already deployed)
     * @param shareTokenName Name for the share token
     * @param shareTokenSymbol Symbol for the share token
     * @return multiPool The deployed multipool address
     * @return shareToken The deployed share token address
     */
    function deployMultiPool(
        string calldata confederationId,
        address frostMultisig,
        address custodianValidator,
        string calldata shareTokenName,
        string calldata shareTokenSymbol
    ) external onlyAuthorizedDeployer returns (address multiPool, address shareToken) {
        address expectedMultiPool = predictNextMultiPoolAddress();
        shareToken = shareTokenDeployer.deployConfederationToken(shareTokenName, shareTokenSymbol, expectedMultiPool);
        ConfederationPoolV2 pool = new ConfederationPoolV2(
            confederationId, frostMultisig, custodianValidator, shareToken
        );
        multiPool = address(pool);
        if (multiPool != expectedMultiPool) revert UnexpectedMultiPoolAddress();
        deployedPoolCodehash[multiPool] = multiPool.codehash;
        unchecked { nextMultiPoolNonce++; }

        emit MultiPoolDeployed(confederationId, multiPool, shareToken, msg.sender);
    }

    function predictNextMultiPoolAddress() public view returns (address predicted) {
        RLP.Encoder memory encoder = RLP.encoder();
        RLP.push(encoder, address(this));
        RLP.push(encoder, nextMultiPoolNonce);
        predicted = address(uint160(uint256(keccak256(RLP.encode(encoder)))));
    }
}
