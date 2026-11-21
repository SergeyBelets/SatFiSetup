// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./FederationGovernance.sol";
import "./interfaces/IFederationFactory.sol";

contract GovernanceRegistry {
    IFederationFactory public immutable factory;
    mapping(string => address) public governance;

    constructor(address factory_) {
        require(factory_.code.length > 0, "factory");
        factory = IFederationFactory(factory_);
    }

    function getRegisterOperationHash(string calldata id, address candidate) public view returns (bytes32) {
        IFederationFactory.FederationConfig memory f = factory.getFederation(id);
        return FROSTAuthorization(f.frostMultisig)
            .hashOperation(address(this), this.register.selector, keccak256(abi.encode(id, candidate)), 0);
    }

    function register(string calldata id, address candidate, bytes32 op) external {
        IFederationFactory.FederationConfig memory f = factory.getFederation(id);
        require(f.isActive && governance[id] == address(0), "federation state");
        FederationGovernance g = FederationGovernance(candidate);
        require(
            address(g.frostMultisig()) == f.frostMultisig && address(g.multiPool()) == f.multiPool
                && keccak256(bytes(g.federationId())) == keccak256(bytes(id)),
            "binding"
        );
        require(op == getRegisterOperationHash(id, candidate), "hash");
        FROSTAuthorization(f.frostMultisig).consumeOperation(op);
        governance[id] = candidate;
    }
}
