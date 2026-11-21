// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {DKGCoordinator} from "../contracts/src/federation/DKGCoordinator.sol";
import {IFederationFactory} from "../contracts/src/federation/interfaces/IFederationFactory.sol";
import {IConfederationFactory} from "../contracts/src/federation/interfaces/IConfederationFactory.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";

/// @dev Admission tests use valid on-chain ceremony transitions with synthetic off-chain commitments.
///      These fixtures do not claim cryptographic VSS or Schnorr verification coverage.
contract EpochVerifierFixture {
    function verify(bytes calldata, bytes32, bytes calldata, string calldata) external pure returns (bool) {
        return true;
    }
}

contract EpochCoordinatorFixture {
    address public immutable defaultVerifier;
    address public dkgRegistry;

    constructor(address registry) {
        dkgRegistry = registry;
        defaultVerifier = address(new EpochVerifierFixture());
    }

    function setRegistry(address registry) external { dkgRegistry = registry; }
}

contract EpochFederationsFixture {
    mapping(string => IFederationFactory.FederationConfig) private configs;

    function setFederation(string memory id, address[] memory members, bool active) external {
        configs[id].federationId = id;
        configs[id].members = members;
        configs[id].threshold = members.length;
        configs[id].createdAt = block.timestamp;
        configs[id].isActive = active;
        configs[id].shareToken = address(0x5151);
    }

    function federationExists(string calldata id) external view returns (bool) {
        return configs[id].createdAt != 0;
    }
    function isFederationActive(string calldata id) external view returns (bool) { return configs[id].isActive; }
    function getFederation(string calldata id) external view returns (IFederationFactory.FederationConfig memory) {
        return configs[id];
    }
}

abstract contract ConfederationEpochFixture is Test {
    bytes32 internal constant GX = 0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798;
    bytes32 internal constant SECOND_GX = 0xc6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5;
    address internal constant OUTSIDER = address(0x9999);
    DKGCoordinator internal dkg;
    EpochCoordinatorFixture internal coordinator;
    uint256 private authorizationSession;

    function _baseSetUp() internal {
        vm.chainId(31337);
        vm.warp(10 days);
        dkg = new DKGCoordinator();
        coordinator = new EpochCoordinatorFixture(address(dkg));
    }

    function _founders(uint256 f) internal pure returns (address[] memory founders) {
        founders = new address[](f);
        for (uint256 i; i < f; ++i) founders[i] = address(uint160(0x100 + i));
    }

    function _custodians(uint256 c) internal pure returns (IConfederationFactory.CustodianMember[] memory custodians) {
        custodians = new IConfederationFactory.CustodianMember[](c);
        for (uint256 i; i < c; ++i) {
            custodians[i] = IConfederationFactory.CustodianMember("custodian", address(uint160(0x200 + i)), hex"02", true);
        }
    }

    function _roster(uint256 f, uint256 c) internal pure returns (address[] memory roster) {
        roster = new address[](f + c);
        for (uint256 i; i < f; ++i) roster[i] = address(uint160(0x100 + i));
        for (uint256 i; i < c; ++i) roster[f + i] = address(uint160(0x200 + i));
    }

    function _startCeremony(bytes32 id, address[] memory roster, uint256 threshold, bytes32 purpose) internal {
        bytes32[] memory keys = new bytes32[](roster.length);
        for (uint256 i; i < keys.length; ++i) keys[i] = bytes32(i + 1);
        bytes32 protocol = dkg.PROTOCOL_V1();
        vm.prank(dkg.ceremonyRegistrar());
        dkg.createCeremony(id, roster, uint16(threshold), keys, 1, purpose, protocol);
    }

    function _completeCeremony(bytes32 id, address[] memory roster, uint256 threshold, bytes32 purpose, bytes32 gx)
        internal
    {
        _startCeremony(id, roster, threshold, purpose);
        bytes32[] memory commitments = new bytes32[](threshold);
        for (uint256 i; i < threshold; ++i) commitments[i] = bytes32(i + 1);
        for (uint256 i; i < roster.length; ++i) {
            vm.prank(roster[i]);
            dkg.submitRound1(id, bytes32(i + 1), commitments);
        }
        for (uint256 i; i < roster.length; ++i) {
            for (uint256 j; j < roster.length; ++j) {
                if (i == j) continue;
                vm.prank(roster[i]);
                dkg.submitShareHash(id, roster[j], keccak256(abi.encode(id, i, j)));
            }
        }
        for (uint256 j; j < roster.length; ++j) {
            vm.startPrank(roster[j]);
            for (uint256 i; i < roster.length; ++i) {
                if (i != j) dkg.submitReceipt(id, roster[i], keccak256(abi.encode(id, i, j)));
            }
            dkg.submitPublicKeyShare(id, abi.encodePacked(bytes1(0x02), gx));
            vm.stopPrank();
        }
        bytes32 result = dkg.computeResultHash(id, false, gx);
        for (uint256 i; i < roster.length; ++i) {
            vm.prank(roster[i]);
            dkg.attestResult(id, false, abi.encodePacked(gx), result);
        }
    }

    function _approve(FROSTAuthorization auth, bytes32 dkgId, bytes32 op, address target) internal {
        uint256 session = ++authorizationSession;
        if (dkgId != bytes32(0)) {
            (, bytes32 result,,,,,) = dkg.getCompletedResult(dkgId);
            vm.mockCall(address(coordinator), abi.encodeWithSignature("sessionDKGId(uint256)", session), abi.encode(dkgId));
            vm.mockCall(address(coordinator), abi.encodeWithSignature("sessionDKGResult(uint256)", session), abi.encode(result));
        }
        vm.mockCall(address(coordinator), abi.encodeWithSignature("getSessionParticipants(uint256)", session),
            abi.encode(auth.getParticipants()));
        vm.mockCall(address(coordinator), abi.encodeWithSignature("getAuthorizationResult(uint256)", session),
            abi.encode(auth.groupPubkey(), op, true, address(0), target, auth.threshold(), auth.threshold(), hex"01"));
        auth.authorizeOperation(session, op, target);
    }
}
