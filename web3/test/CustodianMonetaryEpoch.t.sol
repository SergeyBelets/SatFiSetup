// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import "./ConfederationEpochFixture.sol";
import "../contracts/src/federation/ConfederationFactory.sol";
import "../contracts/src/federation/DKGOnchainBoard.sol";
import "../contracts/src/FROSTCoordinator.sol";

contract CustodianMonetaryEpochTest is ConfederationEpochFixture {
    EpochFederationsFixture private federations;
    ConfederationFactory private factory;
    FROSTCoordinator private publicCoordinator;
    address private constant REPRESENTATIVE = address(0x100);
    bytes32 private constant EPOCH = bytes32(uint256(111));
    function setUp() public {
        _baseSetUp();
        publicCoordinator = new FROSTCoordinator(address(new EpochVerifierFixture()));
        publicCoordinator.bindDKGRegistry(address(dkg));
        federations = new EpochFederationsFixture();
        federations.setFederation("source", _founders(1), true);
        factory = new ConfederationFactory(address(federations), address(dkg), address(0xCAFE), address(coordinator));
    }
    function roster() private pure returns(address[] memory a) {
        a = new address[](2); a[0] = address(0x200); a[1] = address(0x201);
    }
    function sources() private pure returns(string[] memory s) { s = new string[](1); s[0] = "source"; }
    function testSeparateCustodianDKGContainsNoFederationShare() public {
        _completeCeremony(EPOCH, roster(), 2, keccak256("EVM_AUTH"), GX);
        vm.prank(address(0x200));
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptCustodianConfederationDKG(EPOCH, sources(), _custodians(2), 2, 2, "custodian-only");
        factory.reserveConfederationId(EPOCH, "custodian-only");
        bytes32 other = keccak256("other custody epoch");
        IConfederationFactory.CustodianMember[] memory otherCustodians = _custodians(2);
        otherCustodians[0].signerAddress = OUTSIDER;
        address[] memory otherRoster = roster();
        otherRoster[0] = OUTSIDER;
        _completeCeremony(other, otherRoster, 2, keccak256("EVM_AUTH"), GX);
        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.InvalidConfederationReservation.selector);
        factory.adoptCustodianConfederationDKG(other, sources(), otherCustodians, 2, 2, "custodian-only");
        assertFalse(factory.confederationExists("custodian-only"));
        vm.prank(REPRESENTATIVE);
        factory.adoptCustodianConfederationDKG(EPOCH, sources(), _custodians(2), 2, 2, "custodian-only");
        assertEq(dkg.getSessionParticipants(EPOCH), roster());
        assertFalse(dkg.isSessionParticipant(EPOCH, REPRESENTATIVE));
        assertEq(factory.getFounders("custodian-only"), _founders(1));
        assertEq(factory.getConfederation("custodian-only").totalThreshold, 2);
        assertEq(factory.getConfederation("custodian-only").minCustodians, 2);
    }
    function testRepresentativeCannotBeAddedToTheCustodianEpoch() public {
        factory.reserveConfederationId(EPOCH, "bad");
        IConfederationFactory.CustodianMember[] memory custodians = _custodians(2);
        custodians[0].signerAddress = REPRESENTATIVE;
        vm.expectRevert("founder is not custodian");
        factory.adoptCustodianConfederationDKG(EPOCH, sources(), custodians, 2, 2, "bad");
    }
    function testCustodyAdoptionRejectsLegacyRosterAndWrongPurpose() public {
        factory.reserveConfederationId(EPOCH, "bad");
        _completeCeremony(EPOCH, _roster(1,2), 3, keccak256("EVM_AUTH"), GX);
        vm.prank(REPRESENTATIVE); vm.expectRevert("DKG roster");
        factory.adoptCustodianConfederationDKG(EPOCH, sources(), _custodians(2), 2, 2, "bad");
        _completeCeremony(bytes32(uint256(112)), roster(), 2, keccak256("BTC_SPEND"), GX);
        factory.reserveConfederationId(bytes32(uint256(112)), "bad2");
        vm.prank(REPRESENTATIVE); vm.expectRevert("DKG roster");
        factory.adoptCustodianConfederationDKG(bytes32(uint256(112)), sources(), _custodians(2), 2, 2, "bad2");
    }
    function board() private returns(DKGOnchainBoard b) {
        address[] memory invited = _roster(1,2);
        vm.prank(REPRESENTATIVE); b = new DKGOnchainBoard(bytes("unit-public-invitation"), invited);
        vm.prank(address(0x200)); b.enroll(bytes("unit-public-enrollment-a"));
        vm.prank(address(0x201)); b.enroll(bytes("unit-public-enrollment-b"));
        vm.prank(REPRESENTATIVE); b.bindRegistry(address(dkg));
        vm.prank(REPRESENTATIVE); b.bindIntentCoordinator(address(publicCoordinator));
    }
    function testCustodiansPublishAfterOperatorStopsWithoutGainingApprovalAuthority() public {
        _completeCeremony(EPOCH, roster(), 2, keccak256("EVM_AUTH"), GX);
        DKGOnchainBoard b = board();
        vm.prank(REPRESENTATIVE); b.enableCustodianPublishers(EPOCH);
        assertTrue(b.canPublishIntent(address(0x200)));
        assertFalse(b.canPublishIntent(OUTSIDER));
        vm.prank(address(0x200));uint256 sid=publicCoordinator.createSessionFromDKG(
            EPOCH,keccak256("unit-public-operation"),address(this),uint64(block.timestamp+1 days));
        vm.prank(address(0x200)); b.publishIntent(sid, bytes("unit-public-operation"));
        assertEq(b.intents(sid), bytes("unit-public-operation"));
        assertEq(b.registry(), address(dkg));
        vm.prank(OUTSIDER); vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);
        b.publishIntent(18, bytes("forged"));
        vm.prank(address(0x200)); vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);
        b.bindRegistry(address(factory));
        vm.prank(REPRESENTATIVE); vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);
        b.enableCustodianPublishers(EPOCH);
    }
    function testPublicTransportCannotDelegateToEpochContainingOperator() public {
        _completeCeremony(EPOCH, _roster(1,2), 3, keccak256("EVM_AUTH"), GX);
        DKGOnchainBoard b = board();
        vm.prank(REPRESENTATIVE); vm.expectRevert(DKGOnchainBoard.InvalidPacket.selector);
        b.enableCustodianPublishers(EPOCH);
        assertFalse(b.canPublishIntent(address(0x200)));
    }
    function testNoPublisherCanPreemptAnotherMembersSessionOrChunks() public {
        _completeCeremony(EPOCH,roster(),2,keccak256("EVM_AUTH"),GX);
        DKGOnchainBoard b=board();vm.prank(REPRESENTATIVE);b.enableCustodianPublishers(EPOCH);
        vm.prank(address(0x200));uint256 sid=publicCoordinator.createSessionFromDKG(
            EPOCH,keccak256("safe"),address(this),uint64(block.timestamp+1 days));
        address[3] memory attackers=[REPRESENTATIVE,address(0x201),OUTSIDER];
        for(uint256 i;i<attackers.length;++i) {
            vm.startPrank(attackers[i]);
            vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);b.publishIntent(sid,"poison");
            vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);b.publishFeeIntent(sid,"poison");
            vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);b.appendFeeIntent(sid,6,0,"poison");
            vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);b.commitFeeIntent(sid);
            vm.stopPrank();
        }
        vm.startPrank(address(0x200));
        vm.expectRevert(DKGOnchainBoard.NotAuthorized.selector);b.publishIntent(1000,"future id");
        b.appendFeeIntent(sid,4,0,"safe");b.commitFeeIntent(sid);b.commitFeeIntent(sid);
        vm.stopPrank();assertEq(b.intents(sid),bytes("safe"));assertEq(b.intentCount(),1);
    }
}
