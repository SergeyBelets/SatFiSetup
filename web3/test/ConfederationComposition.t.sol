// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {ConfederationFactory} from "../contracts/src/federation/ConfederationFactory.sol";
import {IDKGCoordinator} from "../contracts/src/federation/interfaces/IDKGCoordinator.sol";

contract ConfederationCompositionTest is ConfederationEpochFixture {
    EpochFederationsFixture private federations;
    ConfederationFactory private factory;

    function setUp() public {
        _baseSetUp();
        federations = new EpochFederationsFixture();
        factory = new ConfederationFactory(address(federations), address(dkg), address(0xCAFE), address(coordinator));
    }

    function _sources(uint256 f) private returns (string[] memory ids) {
        ids = new string[](f);
        for (uint256 i; i < f; ++i) {
            ids[i] = string.concat("federation-", vm.toString(i));
            address[] memory members = new address[](2);
            members[0] = address(uint160(0x100 + i));
            members[1] = address(uint160(0x1000 + i));
            federations.setFederation(ids[i], members, true);
        }
    }

    function _adopt(uint256 f, uint256 c, uint256 t, uint256 m, string memory id) private returns (bytes32 session) {
        string[] memory ids = _sources(f);
        session = keccak256(bytes(id));
        _completeCeremony(session, _roster(f, c), t, keccak256("EVM_AUTH"), GX);
        factory.reserveConfederationId(session, id);
        vm.prank(address(0x100));
        factory.adoptConfederationDKG(session, ids, _custodians(c), t, m, id);
        assertEq(factory.getFounders(id), _founders(f));
        assertEq(dkg.getSessionParticipants(session), _roster(f, c));
        assertTrue(factory.v1Sessions(session));
        IConfederationFactory.ConfederationConfig memory config = factory.getConfederation(id);
        assertEq(config.federationCount, f);
        assertEq(config.totalThreshold, t);
        assertEq(config.minCustodians, m);
    }

    function testMinimumConfigurationsF1ThroughF6() public {
        for (uint256 f = 1; f <= 6; ++f) {
            uint256 c = (f + 1) / 2;
            uint256 t = (7 * (f + c) + 9) / 10;
            _adopt(f, c, t, t - f, string.concat("minimum-", vm.toString(f)));
        }
    }

    function testNonminimalCustodiansAndBothMinCustodianBounds() public {
        _adopt(2, 2, 3, 1, "lower-bound");
        _adopt(2, 2, 3, 2, "upper-bound");
        _adopt(6, 3, 7, 3, "explicit-three");
    }

    function testMaximumSupportedRosterIsSixteenAndPreservesOrder() public {
        assertEq(dkg.MAX_PARTICIPANTS(), 16);
        _adopt(10, 6, 12, 2, "maximum");
        assertEq(factory.getFounders("maximum").length + factory.getCustodianMembers("maximum").length, 16);
    }

    function testLegacyUsesSameCompositionAndStoresEveryRepresentative() public {
        string[] memory ids = _sources(3);
        bytes32 session = factory.initiateConfederation(ids, _custodians(2), 4, 1, "legacy");
        assertEq(dkg.getSessionParticipants(session), _roster(3, 2));
        assertEq(factory.getFounders("legacy"), _founders(3));
        assertFalse(factory.v1Sessions(session));
    }

    function testRejectsNoFounder() public {
        vm.expectRevert(ConfederationFactory.InvalidFederations.selector);
        factory.initiateConfederation(new string[](0), _custodians(2), 2, 2, "bad");
    }

    function testRejectsInsufficientCustodians() public {
        string[] memory ids = _sources(3);
        vm.expectRevert(ConfederationFactory.InvalidCustodians.selector);
        factory.initiateConfederation(ids, _custodians(1), 4, 1, "bad");
    }

    function testRejectsNoCustodians() public {
        string[] memory ids = _sources(1);
        vm.expectRevert(ConfederationFactory.InvalidCustodians.selector);
        factory.initiateConfederation(ids, _custodians(0), 1, 0, "bad");
    }

    function testRejectsRosterAboveProtocolBound() public {
        string[] memory ids = _sources(10);
        vm.expectRevert(ConfederationFactory.InvalidRosterSize.selector);
        factory.initiateConfederation(ids, _custodians(7), 12, 2, "bad");
    }

    function testRejectsZeroThreshold() public { _badThreshold(1, 1, 0, 1); }
    function testRejectsThresholdOverRoster() public { _badThreshold(1, 1, 3, 1); }
    function testRejectsThresholdBelowSeventyPercent() public { _badThreshold(2, 2, 2, 1); }
    function testRejectsFoundersOnlyThreshold() public { _badThreshold(4, 2, 4, 1); }
    function testRejectsZeroMinCustodiansWithoutDefault() public { _badThreshold(2, 2, 3, 0); }
    function testRejectsMinCustodiansBelowTMinusF() public { _badThreshold(1, 2, 3, 1); }
    function testRejectsMinCustodiansAboveCustodianCount() public { _badThreshold(2, 2, 3, 3); }

    function _badThreshold(uint256 f, uint256 c, uint256 t, uint256 m) private {
        string[] memory ids = _sources(f);
        vm.expectRevert(ConfederationFactory.InvalidThreshold.selector);
        factory.initiateConfederation(ids, _custodians(c), t, m, "bad");
        assertEq(factory.getConfederationCount(), 0);
    }

    function testRejectsZeroFounder() public {
        string[] memory ids = _sources(1);
        federations.setFederation(ids[0], new address[](1), true);
        vm.expectRevert(ConfederationFactory.InvalidAddress.selector);
        factory.initiateConfederation(ids, _custodians(1), 2, 1, "bad");
    }

    function testRejectsZeroCustodian() public {
        string[] memory ids = _sources(1);
        IConfederationFactory.CustodianMember[] memory custodians = _custodians(1);
        custodians[0].signerAddress = address(0);
        vm.expectRevert(ConfederationFactory.InvalidAddress.selector);
        factory.initiateConfederation(ids, custodians, 2, 1, "bad");
    }

    function testRejectsDuplicateCustodians() public {
        string[] memory ids = _sources(1);
        IConfederationFactory.CustodianMember[] memory custodians = _custodians(2);
        custodians[1].signerAddress = custodians[0].signerAddress;
        vm.expectRevert(ConfederationFactory.InvalidAddress.selector);
        factory.initiateConfederation(ids, custodians, 3, 2, "bad");
    }

    function testRejectsFounderCustodianOverlap() public {
        string[] memory ids = _sources(1);
        IConfederationFactory.CustodianMember[] memory custodians = _custodians(1);
        custodians[0].signerAddress = address(0x100);
        vm.expectRevert(ConfederationFactory.InvalidAddress.selector);
        factory.initiateConfederation(ids, custodians, 2, 1, "bad");
    }

    function testRejectsRepeatedFederation() public {
        string[] memory ids = _sources(2);
        ids[1] = ids[0];
        vm.expectRevert(ConfederationFactory.InvalidFederations.selector);
        factory.initiateConfederation(ids, _custodians(1), 3, 1, "bad");
    }

    function testRejectsIdenticalRepresentativesAcrossFederations() public {
        string[] memory ids = _sources(2);
        federations.setFederation(ids[1], _founders(1), true);
        vm.expectRevert(ConfederationFactory.InvalidAddress.selector);
        factory.initiateConfederation(ids, _custodians(1), 3, 1, "bad");
    }

    function testRejectsMissingAndInactiveSources() public {
        string[] memory ids = new string[](1);
        ids[0] = "absent";
        vm.expectRevert(ConfederationFactory.FederationNotFound.selector);
        factory.initiateConfederation(ids, _custodians(1), 2, 1, "missing");
        federations.setFederation(ids[0], _founders(1), false);
        vm.expectRevert(ConfederationFactory.FederationNotActive.selector);
        factory.initiateConfederation(ids, _custodians(1), 2, 1, "inactive");
    }

    function testAdoptionRejectsWrongRegistryBeforeOtherInputs() public {
        coordinator.setRegistry(address(0xBAD));
        vm.expectRevert("DKG registry mismatch");
        factory.adoptConfederationDKG(bytes32(0), new string[](0), _custodians(0), 0, 0, "");
        vm.expectRevert("DKG registry mismatch");
        factory.adoptConfederationFromFederation(bytes32(0), "", _custodians(0), 0, 0, "");
    }

    function testOtherCompletedDKGCannotClaimFreeConfederationId() public {
        string[] memory ids = _sources(1);
        string memory id = "agreed-confederation";
        bytes32 intended = keccak256("intended confederation DKG");
        bytes32 other = keccak256("other confederation DKG");
        IConfederationFactory.CustodianMember[] memory custodians = _custodians(1);
        custodians[0].signerAddress = OUTSIDER;
        address[] memory roster = _roster(1, 1);
        roster[1] = OUTSIDER;
        _completeCeremony(other, roster, 2, keccak256("EVM_AUTH"), GX);

        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptConfederationDKG(other, ids, custodians, 2, 1, id);
        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.NotAuthorized.selector);
        factory.reserveConfederationId(other, id);

        factory.reserveConfederationId(intended, id);
        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptConfederationDKG(other, ids, custodians, 2, 1, id);
        assertEq(factory.getConfederationCount(), 0);

        _completeCeremony(intended, _roster(1, 1), 2, keccak256("EVM_AUTH"), GX);
        vm.prank(address(0x200));
        factory.adoptConfederationDKG(intended, ids, _custodians(1), 2, 1, id);
        assertEq(factory.getDKGSession(id), intended);
        factory.reserveConfederationId(intended, id);

        factory.reserveConfederationId(other, "other-confederation");
        vm.prank(OUTSIDER);
        factory.adoptConfederationDKG(other, ids, custodians, 2, 1, "other-confederation");
        assertEq(factory.getDKGSession("other-confederation"), other);
    }

    function testLegacyCannotBypassConfederationReservation() public {
        string[] memory ids = _sources(1);
        bytes32 intended = keccak256("reserved confederation DKG");
        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.NotAuthorized.selector);
        factory.initiateConfederation(ids, _custodians(1), 2, 1, "free-id");

        factory.reserveConfederationId(intended, "reserved-id");
        factory.reserveConfederationId(intended, "reserved-id");
        vm.expectRevert(ConfederationFactory.ConfederationAlreadyExists.selector);
        factory.initiateConfederation(ids, _custodians(1), 2, 1, "reserved-id");
        vm.expectRevert(ConfederationFactory.ConfederationAlreadyExists.selector);
        factory.reserveConfederationId(keccak256("wrong DKG"), "reserved-id");
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.reserveConfederationId(intended, "alias");

        vm.mockCall(address(dkg), abi.encodeWithSelector(IDKGCoordinator.initiateDKG.selector), abi.encode(intended));
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.initiateConfederation(ids, _custodians(1), 2, 1, "alias");
        assertEq(factory.getConfederationCount(), 0);
    }

    function testAdoptionRejectsUnfinishedDKG() public {
        string[] memory ids = _sources(2);
        _startCeremony(bytes32(uint256(1)), _roster(2, 2), 3, keccak256("EVM_AUTH"));
        factory.reserveConfederationId(bytes32(uint256(1)), "bad");
        vm.prank(address(0x100));
        vm.expectRevert(DKGCoordinator.DKGNotComplete.selector);
        factory.adoptConfederationDKG(bytes32(uint256(1)), ids, _custodians(2), 3, 1, "bad");
    }

    function testAdoptionRejectsWrongPurposeRosterOrderAndThreshold() public {
        string[] memory ids = _sources(2);
        for (uint256 kind; kind < 4; ++kind) {
            bytes32 id = bytes32(kind + 1);
            string memory confederationId = string.concat("bad-", vm.toString(kind));
            address[] memory roster = _roster(2, 2);
            if (kind == 1) roster[3] = OUTSIDER;
            if (kind == 2) (roster[0], roster[1]) = (roster[1], roster[0]);
            _completeCeremony(id, roster, kind == 3 ? 4 : 3, kind == 0 ? keccak256("BTC_SPEND") : keccak256("EVM_AUTH"), GX);
            factory.reserveConfederationId(id, confederationId);
            vm.prank(address(0x100));
            vm.expectRevert("DKG roster");
            factory.adoptConfederationDKG(id, ids, _custodians(2), 3, 1, confederationId);
        }
        assertEq(factory.getConfederationCount(), 0);
    }

    function testAdoptionRejectsOutsiderAndRepeatedSessionOrId() public {
        string[] memory ids = _sources(2);
        bytes32 session = keccak256("admission");
        _completeCeremony(session, _roster(2, 2), 3, keccak256("EVM_AUTH"), GX);
        factory.reserveConfederationId(session, "first");
        vm.prank(OUTSIDER);
        vm.expectRevert("DKG admission");
        factory.adoptConfederationDKG(session, ids, _custodians(2), 3, 1, "first");
        vm.prank(address(0x200));
        factory.adoptConfederationDKG(session, ids, _custodians(2), 3, 1, "first");
        vm.prank(address(0x100));
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptConfederationDKG(session, ids, _custodians(2), 3, 1, "second");
        vm.prank(address(0x100));
        vm.expectRevert(ConfederationFactory.ConfederationAlreadyExists.selector);
        factory.adoptConfederationDKG(bytes32(uint256(99)), ids, _custodians(2), 3, 1, "first");
        assertEq(factory.getConfederationCount(), 1);
    }

    function testFullFederationUsesAllMembersAndRejectsRepresentativeOnlyDKG() public {
        federations.setFederation("source", _founders(3), true);
        bytes32 incorrect = keccak256("representative-only");
        _completeCeremony(incorrect, _roster(1, 2), 3, keccak256("EVM_AUTH"), GX);
        factory.reserveConfederationId(incorrect, "incorrect");
        vm.prank(address(0x100));
        vm.expectRevert("DKG roster");
        factory.adoptConfederationFromFederation(incorrect, "source", _custodians(2), 4, 1, "incorrect");
        bytes32 correct = keccak256("full-roster");
        _completeCeremony(correct, _roster(3, 2), 4, keccak256("EVM_AUTH"), GX);
        vm.prank(address(0x102));
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptConfederationFromFederation(correct, "source", _custodians(2), 4, 1, "full");
        factory.reserveConfederationId(correct, "full");
        vm.prank(address(0x100));
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptConfederationFromFederation(incorrect, "source", _custodians(2), 4, 1, "full");
        vm.prank(address(0x102));
        factory.adoptConfederationFromFederation(correct, "source", _custodians(2), 4, 1, "full");
        assertEq(factory.getFounders("full"), _founders(3));
        assertEq(factory.getConfederation("full").federationCount, 1);
        assertEq(factory.getFederationMembers("full").length, 1);
        assertTrue(factory.v1Sessions(correct));
        assertTrue(federations.isFederationActive("source"));
    }

    function testFullFederationValidatesWholeFounderListAndSameThresholdRule() public {
        address[] memory founders = _founders(3);
        bytes32 session = keccak256("full validation");
        factory.reserveConfederationId(session, "bad");
        federations.setFederation("source", founders, true);
        vm.expectRevert(ConfederationFactory.InvalidCustodians.selector);
        factory.adoptConfederationFromFederation(session, "source", _custodians(1), 4, 1, "bad");
        vm.expectRevert(ConfederationFactory.InvalidThreshold.selector);
        factory.adoptConfederationFromFederation(session, "source", _custodians(2), 3, 1, "bad");
        founders[2] = founders[0];
        federations.setFederation("source", founders, true);
        vm.expectRevert(ConfederationFactory.InvalidAddress.selector);
        factory.adoptConfederationFromFederation(session, "source", _custodians(2), 4, 1, "bad");
        founders[2] = address(0);
        federations.setFederation("source", founders, true);
        vm.expectRevert(ConfederationFactory.InvalidAddress.selector);
        factory.adoptConfederationFromFederation(session, "source", _custodians(2), 4, 1, "bad");
    }
}
