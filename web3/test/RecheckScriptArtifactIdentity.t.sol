// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ScriptArtifactIdentity} from "../script/ScriptArtifactIdentity.sol";
import {DAOTestUSD} from "../contracts/src/demo/DAOTestUSD.sol";
import {FROSTVerifier} from "../contracts/src/FROSTVerifier.sol";
import {FROSTAuthorizationDeployer} from "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import {MultiPoolShareTokenDeployer} from "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";

contract ScriptArtifactIdentityHarness is ScriptArtifactIdentity {
    function check(address target, string calldata artifact) external view { _requireArtifact(target, artifact); }
}

contract RecheckScriptArtifactIdentityTest is Test {
    ScriptArtifactIdentityHarness private identity;

    function setUp() public {
        vm.chainId(31337);
        identity = new ScriptArtifactIdentityHarness();
    }

    function test_ExactRuntimeAndDifferentLegitimateImmutableOwnersAreAccepted() public {
        identity.check(address(new FROSTVerifier()), "FROSTVerifier");
        vm.prank(address(0xA001));
        FROSTAuthorizationDeployer first = new FROSTAuthorizationDeployer();
        vm.prank(address(0xA002));
        FROSTAuthorizationDeployer second = new FROSTAuthorizationDeployer();
        assertNotEq(address(first).codehash, address(second).codehash);
        identity.check(address(first), "FROSTAuthorizationDeployer");
        identity.check(address(second), "FROSTAuthorizationDeployer");
    }

    function test_ERC20AndSiblingArtifactWithTheSameOwnerAreRejected() public {
        DAOTestUSD token = new DAOTestUSD();
        vm.expectRevert("runtime mismatch: FROSTVerifier");
        identity.check(address(token), "FROSTVerifier");
        MultiPoolShareTokenDeployer sibling = new MultiPoolShareTokenDeployer();
        vm.expectRevert("runtime mismatch: FROSTAuthorizationDeployer");
        identity.check(address(sibling), "FROSTAuthorizationDeployer");
    }

    function test_NonImmutableRuntimeMutationIsRejected() public {
        FROSTAuthorizationDeployer leaf = new FROSTAuthorizationDeployer();
        bytes memory changed = address(leaf).code;
        changed[changed.length - 1] ^= bytes1(uint8(1));
        vm.etch(address(leaf), changed);
        vm.expectRevert("runtime mismatch: FROSTAuthorizationDeployer");
        identity.check(address(leaf), "FROSTAuthorizationDeployer");
    }

    function test_RepeatedImmutableWordsCannotDisagree() public {
        vm.prank(address(0xA001));
        FROSTAuthorizationDeployer first = new FROSTAuthorizationDeployer();
        vm.prank(address(0xA002));
        FROSTAuthorizationDeployer second = new FROSTAuthorizationDeployer();
        bytes memory changed = address(first).code;
        bytes memory otherOwner = address(second).code;
        uint256 differences;
        for (uint256 i; i < changed.length; ++i) {
            if (changed[i] != otherOwner[i]) {
                if (differences == 0) changed[i] = otherOwner[i];
                differences++;
            }
        }
        assertGt(differences, 1, "owner must have repeated immutable references");
        vm.etch(address(first), changed);
        vm.expectRevert("inconsistent artifact immutable: FROSTAuthorizationDeployer");
        identity.check(address(first), "FROSTAuthorizationDeployer");
    }
}
