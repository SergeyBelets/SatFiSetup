// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "forge-std/Test.sol";
import "../contracts/src/federation/DKGCoordinator.sol";
import "../contracts/src/federation/DKGHash.sol";

contract DKGV1Test is Test {
    DKGCoordinator d;
    address[] roster;
    bytes32 id = keccak256("synthetic ceremony");
    bytes32 gx = 0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798;

    function setUp() public {
        d = new DKGCoordinator();
        roster.push(address(1));
        roster.push(address(2));
        roster.push(address(3));
        bytes32[] memory keys = new bytes32[](3);
        keys[0] = bytes32(uint256(1));
        keys[1] = bytes32(uint256(2));
        keys[2] = bytes32(uint256(3));
        d.createCeremony(id, roster, 2, keys, 1, keccak256("EVM_AUTH"), d.PROTOCOL_V1());
    }

    function round1() internal {
        bytes32[] memory c = new bytes32[](2);
        c[0] = bytes32(uint256(1));
        c[1] = bytes32(uint256(2));
        for (uint256 i; i < 3; ++i) {
            vm.prank(roster[i]);
            d.submitRound1(id, bytes32(i + 1), c);
        }
    }

    function testIntegrationPhaseDurationIsRegistrarOnlyAndFixedAtCeremonyStart() public {
        vm.chainId(11155111);
        DKGCoordinator registry = new DKGCoordinator();
        assertEq(registry.PHASE_DURATION(), 1 hours);
        vm.prank(address(20));
        vm.expectRevert(DKGCoordinator.UnauthorizedCeremonyCreator.selector);
        registry.configureIntegrationPhaseDuration(6 hours);
        registry.configureIntegrationPhaseDuration(6 hours);
        assertEq(registry.PHASE_DURATION(), 6 hours);
        bytes32[] memory keys = new bytes32[](3);
        keys[0] = bytes32(uint256(1));
        keys[1] = bytes32(uint256(2));
        keys[2] = bytes32(uint256(3));
        registry.createCeremony(id, roster, 2, keys, 1, keccak256("EVM_AUTH"), registry.PROTOCOL_V1());
        assertEq(registry.getSessionInfo(id).phaseDeadline, block.timestamp + 6 hours);
        vm.expectRevert(DKGCoordinator.InvalidManifest.selector);
        registry.configureIntegrationPhaseDuration(7 hours);

        DKGCoordinator defaultRegistry = new DKGCoordinator();
        defaultRegistry.createCeremony(id, roster, 2, keys, 1, keccak256("EVM_AUTH"), defaultRegistry.PROTOCOL_V1());
        vm.expectRevert(DKGCoordinator.InvalidManifest.selector);
        defaultRegistry.configureIntegrationPhaseDuration(7 hours);

        vm.chainId(1);
        DKGCoordinator mainnetRegistry = new DKGCoordinator();
        assertEq(mainnetRegistry.PHASE_DURATION(), 1 days);
        vm.expectRevert(DKGCoordinator.InvalidManifest.selector);
        mainnetRegistry.configureIntegrationPhaseDuration(6 hours);
    }

    function testOutsiderCannotReserveBootstrapCeremonyId() public {
        bytes32[3] memory purposes = [keccak256("EVM_AUTH"), keccak256("EVM_AUTH"), keccak256("BTC_SPEND")];
        address[] memory attackers = new address[](2);
        attackers[0] = address(10);
        attackers[1] = address(11);
        bytes32[] memory keys = new bytes32[](2);
        keys[0] = bytes32(uint256(10));
        keys[1] = bytes32(uint256(11));
        bytes32 protocol = d.PROTOCOL_V1();
        for (uint256 i; i < purposes.length; ++i) {
            bytes32 bootstrapId = keccak256(abi.encode("invited role", i));
            vm.expectRevert(DKGCoordinator.UnauthorizedCeremonyCreator.selector);
            vm.prank(attackers[0]);
            d.createCeremony(bootstrapId, attackers, 2, keys, 1, purposes[i], protocol);
            (bytes32 context,,,,,,) = d.ceremonies(bootstrapId);
            assertEq(context, bytes32(0));

            keys[0] = bytes32(uint256(1));
            keys[1] = bytes32(uint256(2));
            address[] memory participants = new address[](2);
            participants[0] = roster[0];
            participants[1] = roster[1];
            d.createCeremony(bootstrapId, participants, 2, keys, 1, purposes[i], protocol);
            assertEq(d.getSessionParticipants(bootstrapId), participants);
        }
    }

    function testDelegationIsRegistrarOnlyAndScopedToAnUnusedId() public {
        bytes32 next = keccak256("additional federation");
        bytes32 other = keccak256("unrelated federation");
        address delegate = address(10);
        bytes32[] memory keys = new bytes32[](3);
        keys[0] = bytes32(uint256(1));
        keys[1] = bytes32(uint256(2));
        keys[2] = bytes32(uint256(3));
        bytes32 protocol = d.PROTOCOL_V1();
        bytes32 purpose = keccak256("EVM_AUTH");
        vm.expectRevert(DKGCoordinator.UnauthorizedCeremonyCreator.selector);
        vm.prank(delegate);
        d.authorizeCeremonyCreator(next, delegate);
        d.authorizeCeremonyCreator(next, delegate);
        vm.expectRevert(DKGCoordinator.UnauthorizedCeremonyCreator.selector);
        vm.prank(delegate);
        d.createCeremony(other, roster, 2, keys, 1, purpose, protocol);
        d.authorizeCeremonyCreator(next, address(0));
        vm.expectRevert(DKGCoordinator.UnauthorizedCeremonyCreator.selector);
        vm.prank(delegate);
        d.createCeremony(next, roster, 2, keys, 1, purpose, protocol);
        d.authorizeCeremonyCreator(next, delegate);
        vm.prank(delegate);
        d.createCeremony(next, roster, 2, keys, 1, purpose, protocol);
        assertEq(d.getSessionParticipants(next), roster);
        vm.expectRevert(DKGCoordinator.InvalidManifest.selector);
        d.authorizeCeremonyCreator(next, roster[1]);

        vm.expectRevert(DKGCoordinator.LegacyOnly.selector);
        vm.prank(delegate);
        d.failDKG(next, "creator is outside the roster");
        vm.expectRevert(DKGCoordinator.NotAParticipant.selector);
        vm.prank(delegate);
        d.abortCeremony(next, keccak256("creator is outside the roster"));
        vm.prank(roster[0]);
        d.abortCeremony(next, keccak256("participant abort"));
    }

    function round2() internal {
        for (uint256 i; i < 3; ++i) {
            for (uint256 j; j < 3; ++j) {
                if (i != j) {
                    vm.prank(roster[i]);
                    d.submitShareHash(id, roster[j], bytes32(uint256(123)));
                }
            }
        }
    }

    function testNoZeroSelfOrDuplicateShares() public {
        round1();
        vm.startPrank(roster[0]);
        vm.expectRevert(DKGCoordinator.InvalidHash.selector);
        d.submitShareHash(id, roster[1], bytes32(0));
        vm.expectRevert(DKGCoordinator.InvalidRecipient.selector);
        d.submitShareHash(id, roster[0], bytes32(uint256(1)));
        d.submitShareHash(id, roster[1], bytes32(uint256(1)));
        vm.expectRevert(DKGCoordinator.ShareAlreadySubmitted.selector);
        d.submitShareHash(id, roster[1], bytes32(uint256(2)));
        vm.stopPrank();
        (,, uint256 count,,) = d.getDKGStatus(id);
        assertEq(count, 1);
    }

    function testAllNMustAttestAndCompletedIsImmutable() public {
        round1();
        round2();
        for (uint256 i; i < 3; ++i) {
            vm.startPrank(roster[i]);
            for (uint256 j; j < 3; ++j) {
                if (i != j) d.submitReceipt(id, roster[j], bytes32(uint256(123)));
            }
            // Format/attestation unit fixture, not cryptographic VSS evidence.
            d.submitPublicKeyShare(id, abi.encodePacked(bytes1(i == 1 ? 0x03 : 0x02), gx));
            vm.stopPrank();
        }
        bytes32 result = d.computeResultHash(id, false, gx);
        for (uint256 i; i < 2; ++i) {
            vm.prank(roster[i]);
            d.attestResult(id, false, abi.encodePacked(gx), result);
        }
        (,,,, bool done) = d.getDKGStatus(id);
        assertFalse(done);
        vm.prank(roster[2]);
        d.attestResult(id, false, abi.encodePacked(gx), result);
        (,,,, done) = d.getDKGStatus(id);
        assertTrue(done);
        vm.expectRevert(DKGCoordinator.InvalidPhase.selector);
        d.failDKG(id, "cannot overwrite");
        vm.prank(roster[1]);
        vm.expectRevert(DKGCoordinator.InvalidPhase.selector);
        d.abortCeremony(id, bytes32(uint256(1)));
    }

    function testTimeoutIsTerminalAndCannotContinueWithSubset() public {
        vm.warp(block.timestamp + d.PHASE_DURATION() + 1);
        d.advancePhase(id);
        (IDKGCoordinator.DKGPhase phase,,,,) = d.getDKGStatus(id);
        assertEq(uint256(phase), uint256(IDKGCoordinator.DKGPhase.Failed));
        vm.expectRevert(DKGCoordinator.InvalidPhase.selector);
        d.advancePhase(id);
    }

    function testLegacyCompletionCannotBypassV1Result() public {
        round1();
        round2();
        vm.expectRevert(DKGCoordinator.LegacyOnly.selector);
        d.completeDKG(id, abi.encodePacked(gx));
    }

    function testContextGoldenVector() public pure {
        address[] memory p = new address[](3);
        p[0] = address(0x1111111111111111111111111111111111111111);
        p[1] = address(0x2222222222222222222222222222222222222222);
        p[2] = address(0x3333333333333333333333333333333333333333);
        bytes32[] memory keys = new bytes32[](3);
        keys[0] = bytes32(type(uint256).max / 255);
        keys[1] = bytes32(type(uint256).max / 255 * 2);
        keys[2] = bytes32(type(uint256).max / 255 * 3);
        bytes32 actual = DKGHash.contextHash(
            DKGHash.Context(
                31337,
                address(0x4444444444444444444444444444444444444444),
                bytes32(type(uint256).max / 255 * 0x55),
                7,
                keccak256("EVM_AUTH"),
                DKGHash.PROTOCOL,
                keccak256(abi.encode(p)),
                keccak256(abi.encode(keys)),
                3,
                2
            )
        );
        assertEq(actual, 0xcaba2e26cd8b477b7fd8e33715ff11f37b018ded58965a856dbe0900641bef40);
    }
}
