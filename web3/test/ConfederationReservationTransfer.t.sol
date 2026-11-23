// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {ConfederationFactory} from "../contracts/src/federation/ConfederationFactory.sol";

contract ConfederationReservationTransferTest is ConfederationEpochFixture {
    string private constant ID = "recoverable-confederation";
    bytes32 private constant PREVIOUS = keccak256("previous confederation epoch");
    bytes32 private constant REPLACEMENT = keccak256("replacement confederation epoch");
    bytes32 private constant EVM_AUTH = keccak256("EVM_AUTH");
    bytes32 private constant TOKEN_HASH = keccak256(abi.encode("Confederation share", "CONF"));

    ConfederationFactory private factory;

    function setUp() public {
        _baseSetUp();
        EpochFederationsFixture sources = new EpochFederationsFixture();
        sources.setFederation("founder", _founders(1), true);
        factory = new ConfederationFactory(address(sources), address(dkg), address(0xCAFE), address(coordinator));
        _startCeremony(PREVIOUS, _roster(1, 1), 2, EVM_AUTH);
        factory.reserveConfederationId(PREVIOUS, ID, _formationHash(), TOKEN_HASH);
    }

    function _federations() private pure returns (string[] memory ids) {
        ids = new string[](1);
        ids[0] = "founder";
    }

    function _formationHash() private pure returns (bytes32) {
        return keccak256(abi.encode(uint8(0), _federations(), _custodians(1), uint256(2), uint256(1)));
    }

    function _abortPrevious() private {
        vm.prank(address(0x200));
        dkg.abortCeremony(PREVIOUS, keccak256("participant aborted ceremony"));
    }

    function testAbortedReservationMovesToCompletedEpochWithSameTermsAndRetiresOldSession() public {
        _abortPrevious();
        _completeCeremony(REPLACEMENT, _roster(1, 1), 2, EVM_AUTH, GX);

        vm.expectEmit(true, true, true, true, address(factory));
        emit ConfederationFactory.ConfederationReservationTransferred(ID, PREVIOUS, REPLACEMENT);
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        assertEq(factory.reservedConfederationSession(ID), REPLACEMENT);
        (bytes32 formation, bytes32 token) = factory.reservedConfederationTerms(ID);
        assertEq(formation, _formationHash());
        assertEq(token, TOKEN_HASH);
        assertFalse(factory.confederationExists(ID));

        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.reserveConfederationId(PREVIOUS, "old-alias", formation, token);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.reserveConfederationId(REPLACEMENT, "new-alias", formation, token);
        factory.reserveConfederationId(REPLACEMENT, ID, formation, token);

        vm.prank(address(0x200));
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptConfederationDKG(PREVIOUS, _federations(), _custodians(1), 2, 1, ID);
        vm.prank(address(0x200));
        factory.adoptConfederationDKG(REPLACEMENT, _federations(), _custodians(1), 2, 1, ID);
        assertEq(factory.getDKGSession(ID), REPLACEMENT);
        assertTrue(factory.v1Sessions(REPLACEMENT));
    }

    function testDeadlineAloneCannotMoveReservationAndExplicitFailureAllowsActiveReplacement() public {
        vm.warp(block.timestamp + dkg.PHASE_DURATION() + 1);
        _startCeremony(REPLACEMENT, _roster(1, 1), 2, EVM_AUTH);
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        assertEq(factory.reservedConfederationSession(ID), PREVIOUS);

        dkg.advancePhase(PREVIOUS);
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        assertEq(factory.reservedConfederationSession(ID), REPLACEMENT);
    }

    function testTransferRequiresAuthorityAndCanonicalRegistry() public {
        _abortPrevious();
        _startCeremony(REPLACEMENT, _roster(1, 1), 2, EVM_AUTH);
        vm.prank(address(0x200));
        vm.expectRevert(ConfederationFactory.NotAuthorized.selector);
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        coordinator.setRegistry(OUTSIDER);
        vm.expectRevert("DKG registry mismatch");
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        assertEq(factory.reservedConfederationSession(ID), PREVIOUS);
    }

    function testTransferRejectsUnavailableReplacementAndStalePreviousSession() public {
        _abortPrevious();
        _startCeremony(REPLACEMENT, _roster(1, 1), 2, EVM_AUTH);
        factory.reserveConfederationId(REPLACEMENT, "other-reservation", _formationHash(), TOKEN_HASH);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.transferConfederationReservation(ID, PREVIOUS, PREVIOUS);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.transferConfederationReservation(ID, bytes32(0), REPLACEMENT);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.transferConfederationReservation(ID, PREVIOUS, bytes32(0));
        assertEq(factory.reservedConfederationSession(ID), PREVIOUS);
    }

    function testTransferChecksRosterOrderBeforeChangingReservation() public {
        _abortPrevious();
        address[] memory roster = _roster(1, 1);
        (roster[0], roster[1]) = (roster[1], roster[0]);
        _startCeremony(REPLACEMENT, roster, 2, EVM_AUTH);
        vm.expectRevert(DKGCoordinator.InvalidReservationTransfer.selector);
        factory.transferConfederationReservation(ID, PREVIOUS, REPLACEMENT);
        assertEq(factory.reservedConfederationSession(ID), PREVIOUS);
    }

    function testTransferRejectsAlreadyAdoptedReservationEvenBeforeDeployment() public {
        bytes32 adopted = keccak256("adopted confederation epoch");
        _completeCeremony(adopted, _roster(1, 1), 2, EVM_AUTH, GX);
        factory.reserveConfederationId(adopted, "adopted", _formationHash(), TOKEN_HASH);
        vm.prank(address(0x200));
        factory.adoptConfederationDKG(adopted, _federations(), _custodians(1), 2, 1, "adopted");
        assertFalse(factory.isConfederationActive("adopted"));
        _startCeremony(REPLACEMENT, _roster(1, 1), 2, EVM_AUTH);

        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.transferConfederationReservation("adopted", adopted, REPLACEMENT);
        assertEq(factory.reservedConfederationSession("adopted"), adopted);
    }
}
