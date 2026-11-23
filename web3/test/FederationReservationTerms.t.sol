// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {ConfederationEpochFixture} from "./ConfederationEpochFixture.sol";
import {DKGCoordinator} from "../contracts/src/federation/DKGCoordinator.sol";
import {FederationFactory} from "../contracts/src/federation/FederationFactory.sol";
import {FederationDeployer} from "../contracts/src/federation/FederationDeployer.sol";

contract FederationReservationTermsTest is ConfederationEpochFixture {
    string private constant ID = "reserved-federation";
    string private constant TOKEN_NAME = "Reviewed Federation Share";
    string private constant TOKEN_SYMBOL = "RFS";
    bytes32 private constant EPOCH = keccak256("reserved federation epoch");
    bytes32 private constant REPLACEMENT = keccak256("replacement federation ceremony");
    bytes32 private constant EVM_AUTH = keccak256("EVM_AUTH");
    address private constant DEPLOYER = address(0xF001);

    FederationFactory private factory;

    function setUp() public {
        _baseSetUp();
        factory = new FederationFactory(address(dkg), DEPLOYER, address(coordinator));
        factory.configureDeploymentFee(address(0), 0, true);
    }

    function _formation(address[] memory members, uint256 threshold) private pure returns (bytes32) {
        return keccak256(abi.encode(members, threshold));
    }

    function _metadata() private pure returns (bytes32) {
        return keccak256(abi.encode(TOKEN_NAME, TOKEN_SYMBOL));
    }

    function testReservationRequiresNonZeroTermsAndPreservesExactRetry() public {
        bytes32 formation = _formation(_founders(3), 2);
        bytes32 token = _metadata();
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.reserveFederationId(EPOCH, ID, bytes32(0), token);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.reserveFederationId(EPOCH, ID, formation, bytes32(0));
        assertEq(factory.reservedFederationSession(ID), bytes32(0));

        factory.reserveFederationId(EPOCH, ID, formation, token);
        factory.reserveFederationId(EPOCH, ID, formation, token);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.reserveFederationId(EPOCH, ID, _formation(_founders(3), 3), token);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.reserveFederationId(EPOCH, ID, formation, keccak256(abi.encode(TOKEN_NAME, "OTHER")));

        (bytes32 storedFormation, bytes32 storedToken) = factory.reservedFederationTerms(ID);
        assertEq(factory.reservedFederationSession(ID), EPOCH);
        assertEq(storedFormation, formation);
        assertEq(storedToken, token);
    }

    function testCompletedCeremonyMustMatchReservedThreshold() public {
        address[] memory members = _founders(3);
        factory.reserveFederationId(EPOCH, ID, _formation(members, 3), _metadata());
        _completeCeremony(EPOCH, members, 2, keccak256("EVM_AUTH"), GX);

        vm.prank(members[0]);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.adoptFederationDKG(EPOCH, ID);
        assertFalse(factory.federationExists(ID));
        assertFalse(factory.v1Sessions(EPOCH));
    }

    function testCompletedCeremonyMustMatchReservedRosterOrder() public {
        address[] memory members = _founders(3);
        address[] memory reservedRoster = _founders(3);
        (reservedRoster[0], reservedRoster[1]) = (reservedRoster[1], reservedRoster[0]);
        factory.reserveFederationId(EPOCH, ID, _formation(reservedRoster, 2), _metadata());
        _completeCeremony(EPOCH, members, 2, keccak256("EVM_AUTH"), GX);

        vm.prank(members[0]);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.adoptFederationDKG(EPOCH, ID);
        assertFalse(factory.federationExists(ID));
        assertFalse(factory.v1Sessions(EPOCH));
    }

    function testMemberMustUseReservedMetadataBeforeDeployment() public {
        address[] memory members = _founders(3);
        bytes32 formation = _formation(members, 2);
        factory.reserveFederationId(EPOCH, ID, formation, _metadata());
        _completeCeremony(EPOCH, members, 2, keccak256("EVM_AUTH"), GX);

        vm.prank(members[0]);
        factory.adoptFederationDKG(EPOCH, ID);
        factory.reserveFederationId(EPOCH, ID, formation, _metadata());

        vm.prank(members[1]);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.deployFederation(EPOCH, TOKEN_NAME, "OTHER");
        assertFalse(factory.isFederationActive(ID));
        assertEq(factory.getFederation(ID).shareToken, address(0));

        bytes memory callData = abi.encodeCall(FederationDeployer.deployFederationContractsV1,
            (ID, members, 2, TOKEN_NAME, TOKEN_SYMBOL, address(coordinator), abi.encodePacked(GX), EPOCH));
        address token = address(0xBB01);
        address pool = address(0xBB03);
        vm.mockCall(DEPLOYER, callData, abi.encode(address(0xBB02), pool, token));
        vm.mockCall(pool, abi.encodeWithSignature("feeManager()"), abi.encode(address(0)));
        vm.expectCall(DEPLOYER, callData);
        vm.prank(members[1]);
        factory.deployFederation(EPOCH, TOKEN_NAME, TOKEN_SYMBOL);
        assertTrue(factory.isFederationActive(ID));
        assertEq(factory.getFederation(ID).shareToken, token);
    }

    function testAbortedReservationTransfersToCompletedCeremonyAndRetainsTerms() public {
        address[] memory members = _founders(3);
        bytes32 formation = _formation(members, 2);
        factory.reserveFederationId(EPOCH, ID, formation, _metadata());
        _startCeremony(EPOCH, members, 2, EVM_AUTH);
        vm.prank(members[0]);
        dkg.abortCeremony(EPOCH, keccak256("ceremony withdrawn"));
        _completeCeremony(REPLACEMENT, members, 2, EVM_AUTH, GX);
        vm.warp(dkg.getSessionInfo(REPLACEMENT).phaseDeadline + 1);

        vm.expectEmit(true, true, true, false, address(factory));
        emit FederationFactory.FederationReservationTransferred(ID, EPOCH, REPLACEMENT);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        (bytes32 storedFormation, bytes32 storedToken) = factory.reservedFederationTerms(ID);
        assertEq(storedFormation, formation);
        assertEq(storedToken, _metadata());
        assertEq(factory.reservedFederationSession(ID), REPLACEMENT);

        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.reserveFederationId(EPOCH, "second-federation", formation, _metadata());
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.reserveFederationId(REPLACEMENT, "second-federation", formation, _metadata());
        vm.prank(members[0]);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.adoptFederationDKG(EPOCH, ID);

        vm.prank(members[0]);
        factory.adoptFederationDKG(REPLACEMENT, ID);
        assertTrue(factory.federationExists(ID));
        assertTrue(factory.v1Sessions(REPLACEMENT));
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.transferFederationReservation(ID, REPLACEMENT, keccak256("third ceremony"));
    }

    function testTimeoutHasToBeRecordedBeforeTransferToActiveReplacement() public {
        address[] memory members = _founders(3);
        factory.reserveFederationId(EPOCH, ID, _formation(members, 2), _metadata());
        _startCeremony(EPOCH, members, 2, EVM_AUTH);
        vm.warp(dkg.getSessionInfo(EPOCH).phaseDeadline + 1);
        _startCeremony(REPLACEMENT, members, 2, EVM_AUTH);

        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        assertEq(factory.reservedFederationSession(ID), EPOCH);

        dkg.advancePhase(EPOCH);
        vm.warp(dkg.getSessionInfo(REPLACEMENT).phaseDeadline);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        assertEq(factory.reservedFederationSession(ID), REPLACEMENT);
    }

    function testRecoveryRequiresAuthorityRegistryAndUnusedReplacement() public {
        address[] memory members = _founders(3);
        bytes32 formation = _formation(members, 2);
        factory.reserveFederationId(EPOCH, ID, formation, _metadata());
        _startCeremony(EPOCH, members, 2, EVM_AUTH);
        vm.prank(members[0]);
        dkg.abortCeremony(EPOCH, bytes32(0));
        _startCeremony(REPLACEMENT, members, 2, EVM_AUTH);

        vm.prank(members[0]);
        vm.expectRevert(FederationFactory.NotAuthorized.selector);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.transferFederationReservation(ID, EPOCH, EPOCH);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.transferFederationReservation(ID, EPOCH, bytes32(0));

        coordinator.setRegistry(address(0x1234));
        vm.expectRevert(bytes("DKG registry mismatch"));
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        coordinator.setRegistry(address(dkg));

        factory.reserveFederationId(REPLACEMENT, "second-federation", formation, _metadata());
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        assertEq(factory.reservedFederationSession(ID), EPOCH);
    }

    function testCoordinatorRejectsMismatchedOrIneligibleReplacementCeremonies() public {
        address[] memory members = _founders(3);
        factory.reserveFederationId(EPOCH, ID, _formation(members, 2), _metadata());
        _startCeremony(EPOCH, members, 2, EVM_AUTH);
        vm.prank(members[0]);
        dkg.abortCeremony(EPOCH, bytes32(0));

        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        bytes32 legacy = dkg.initiateDKG(members, 2, "legacy");
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, legacy);
        bytes32 bitcoin = keccak256("bitcoin ceremony");
        _startCeremony(bitcoin, members, 2, keccak256("BTC_SPEND"));
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, bitcoin);

        bytes32 differentThreshold = keccak256("different threshold");
        _startCeremony(differentThreshold, members, 3, EVM_AUTH);
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, differentThreshold);
        bytes32 differentRoster = keccak256("different roster order");
        address[] memory reordered = _founders(3);
        (reordered[0], reordered[1]) = (reordered[1], reordered[0]);
        _startCeremony(differentRoster, reordered, 2, EVM_AUTH);
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, differentRoster);

        _startCeremony(REPLACEMENT, members, 2, EVM_AUTH);
        vm.warp(dkg.getSessionInfo(REPLACEMENT).phaseDeadline + 1);
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        dkg.advancePhase(REPLACEMENT);
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferFederationReservation(ID, EPOCH, REPLACEMENT);
        assertEq(factory.reservedFederationSession(ID), EPOCH);
    }

    function testCoordinatorRequiresAnIncompleteV1EvmAuthorizationSource() public {
        address[] memory members = _founders(3);
        _startCeremony(REPLACEMENT, members, 2, EVM_AUTH);
        _completeCeremony(EPOCH, members, 2, EVM_AUTH, GX);
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        dkg.requireReservationTransfer(EPOCH, REPLACEMENT);

        bytes32 legacy = dkg.initiateDKG(members, 2, "legacy");
        dkg.failDKG(legacy, "ended legacy session");
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        dkg.requireReservationTransfer(legacy, REPLACEMENT);

        bytes32 bitcoin = keccak256("bitcoin ceremony");
        _startCeremony(bitcoin, members, 2, keccak256("BTC_SPEND"));
        vm.prank(members[0]);
        dkg.abortCeremony(bitcoin, bytes32(0));
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        dkg.requireReservationTransfer(bitcoin, REPLACEMENT);
    }
}
