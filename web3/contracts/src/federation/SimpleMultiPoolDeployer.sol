// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../demo/SimpleMultiPool.sol";
import "./ControlledDeployer.sol";
import "./FROSTAuthorizationDeployer.sol";
import "./MultiPoolShareTokenDeployer.sol";
import {RLP} from "@openzeppelin/contracts/utils/RLP.sol";
import "./CreationCodePart.sol";

/**
 * @title SimpleMultiPoolDeployer
 * @notice Deploys SimpleMultiPool contract
 * @dev Separated from FederationDeployer to reduce contract size
 */
contract SimpleMultiPoolDeployer is ControlledDeployer {
    FROSTAuthorizationDeployer public immutable frostDeployer;
    MultiPoolShareTokenDeployer public immutable shareTokenDeployer;
    uint256 public nextMultiPoolNonce = 3;
    address public immutable creationPart0;
    address public immutable creationPart1;
    bytes32 public immutable poolCreationHash;
    /// @notice Creation provenance, written only after this deployer's CREATE succeeds.
    mapping(address => bytes32) public deployedPoolCodehash;

    event SimpleMultiPoolDeployed(
        string indexed federationId,
        address multiPool,
        address frostMultisig,
        address shareToken,
        address indexed caller
    );

    error InvalidSubDeployer();
    error UnexpectedMultiPoolAddress();

    constructor(address _frostDeployer, address _shareTokenDeployer) {
        if (_frostDeployer == address(0) || _shareTokenDeployer == address(0)) revert InvalidSubDeployer();
        frostDeployer = FROSTAuthorizationDeployer(_frostDeployer);
        shareTokenDeployer = MultiPoolShareTokenDeployer(_shareTokenDeployer);
        bytes memory code=type(SimpleMultiPool).creationCode;poolCreationHash=keccak256(code);
        uint256 half=code.length/2;bytes memory first=new bytes(half);bytes memory second=new bytes(code.length-half);
        assembly("memory-safe") {
            mcopy(add(first,32),add(code,32),half)
            mcopy(add(second,32),add(add(code,32),half),mload(second))
        }
        creationPart0=address(new CreationCodePart(first));creationPart1=address(new CreationCodePart(second));
    }
    function _createPool(string calldata id,address auth,address token) private returns(address pool) {
        address p0=creationPart0;address p1=creationPart1;uint256 n0=p0.code.length-1;uint256 n1=p1.code.length-1;
        bytes memory code=new bytes(n0+n1);
        assembly("memory-safe") { extcodecopy(p0,add(code,32),1,n0) extcodecopy(p1,add(add(code,32),n0),1,n1) }
        require(keccak256(code) == poolCreationHash,"pool creation provenance");
        bytes memory init=bytes.concat(code,abi.encode(id,auth,token));
        assembly("memory-safe") { pool:=create(0,add(init,32),mload(init)) }
        require(pool != address(0),"pool creation failed");
    }

    /**
     * @notice Deploy a SimpleMultiPool
     * @param federationId Federation identifier
     * @param members Federation member addresses
     * @param threshold Required signature threshold
     * @param shareTokenName Name for the share token
     * @param shareTokenSymbol Symbol for the share token
     * @return multiPool The deployed multipool address
     * @return frostMultisig The FROST multisig created by the pool
     * @return shareToken The deployed share token address
     */
    function deploySimpleMultiPool(
        string calldata federationId,
        address[] calldata members,
        uint256 threshold,
        string calldata shareTokenName,
        string calldata shareTokenSymbol,
        address coordinator,
        bytes calldata groupPubkey
    ) external onlyAuthorizedDeployer returns (address multiPool, address frostMultisig, address shareToken) {
        address expectedMultiPool = predictNextMultiPoolAddress();

        frostMultisig = frostDeployer.deployFROSTAuthorization(coordinator, groupPubkey, members, threshold);
        shareToken = shareTokenDeployer.deployMultiPoolShareToken(shareTokenName, shareTokenSymbol, expectedMultiPool);

        multiPool = _createPool(federationId, frostMultisig, shareToken);
        if (multiPool != expectedMultiPool) revert UnexpectedMultiPoolAddress();

        deployedPoolCodehash[multiPool] = multiPool.codehash;
        unchecked { nextMultiPoolNonce++; }

        emit SimpleMultiPoolDeployed(federationId, multiPool, frostMultisig, shareToken, msg.sender);
    }

    function deploySimpleMultiPoolV1(
        string calldata federationId,
        address[] calldata members,
        uint256 threshold,
        string calldata shareTokenName,
        string calldata shareTokenSymbol,
        address coordinator,
        bytes calldata groupPubkey,
        bytes32 dkgId
    ) external onlyAuthorizedDeployer returns (address multiPool, address frostMultisig, address shareToken) {
        address expectedMultiPool = predictNextMultiPoolAddress();

        frostMultisig = frostDeployer.deployFROSTAuthorizationV1(coordinator, groupPubkey, members, threshold, dkgId);
        shareToken = shareTokenDeployer.deployMultiPoolShareToken(shareTokenName, shareTokenSymbol, expectedMultiPool);

        multiPool = _createPool(federationId, frostMultisig, shareToken);
        if (multiPool != expectedMultiPool) revert UnexpectedMultiPoolAddress();

        deployedPoolCodehash[multiPool] = multiPool.codehash;
        unchecked { nextMultiPoolNonce++; }

        emit SimpleMultiPoolDeployed(federationId, multiPool, frostMultisig, shareToken, msg.sender);
    }

    function predictNextMultiPoolAddress() public view returns (address predicted) {
        RLP.Encoder memory encoder = RLP.encoder();
        RLP.push(encoder, address(this));
        RLP.push(encoder, nextMultiPoolNonce);
        predicted = address(uint160(uint256(keccak256(RLP.encode(encoder)))));
    }
    /// @notice Canonical pool with an existing, explicitly selected monetary
    /// authorizer. No new DKG/authority or ordinary CS route is implied.
    function deployWithAuthorization(string calldata id,address auth,string calldata name,string calldata symbol)
        external onlyAuthorizedDeployer returns(address pool,address token) {
        require(auth.code.length > 0,"pool authorization code");address expected=predictNextMultiPoolAddress();
        token=shareTokenDeployer.deployMultiPoolShareToken(name,symbol,expected);pool=_createPool(id,auth,token);
        require(pool == expected,"pool prediction");deployedPoolCodehash[pool]=pool.codehash;++nextMultiPoolNonce;
        emit SimpleMultiPoolDeployed(id,pool,auth,token,msg.sender);
    }
}
