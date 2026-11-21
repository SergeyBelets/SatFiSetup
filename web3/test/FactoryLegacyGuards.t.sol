// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {ConfederationFactory} from "../contracts/src/federation/ConfederationFactory.sol";
import {FederationFactory} from "../contracts/src/federation/FederationFactory.sol";
import {ConfederationDeployer} from "../contracts/src/federation/ConfederationDeployer.sol";
import {FederationDeployer} from "../contracts/src/federation/FederationDeployer.sol";
import {IDKGCoordinator} from "../contracts/src/federation/interfaces/IDKGCoordinator.sol";

contract FactoryLegacyGuardsTest is ConfederationEpochFixture {
    ConfederationFactory private conf;
    FederationFactory private fed;
    address private constant FED_DEPLOYER = address(0xF001);
    address private constant CONF_DEPLOYER = address(0xF002);

    function setUp() public {
        _baseSetUp();
        EpochFederationsFixture sources = new EpochFederationsFixture();
        sources.setFederation("source", _founders(1), true);
        fed = new FederationFactory(address(dkg), FED_DEPLOYER, address(coordinator));
        conf = new ConfederationFactory(address(sources), address(dkg), CONF_DEPLOYER, address(coordinator));
    }

    function _ids() private pure returns (string[] memory ids) { ids = new string[](1); ids[0] = "source"; }

    function testProductionLegacyStartsRejectBeforeOtherValidation() public {
        uint256[3] memory chains = [uint256(1), uint256(10), uint256(137)];
        for (uint256 i; i < chains.length; ++i) {
            vm.chainId(chains[i]);
            vm.expectRevert(FederationFactory.LegacyNetworkDisabled.selector);
            fed.initiateFederationDKG(new address[](0), 0, "");
            vm.expectRevert(ConfederationFactory.LegacyNetworkDisabled.selector);
            conf.initiateConfederation(new string[](0), _custodians(0), 0, 0, "");
        }
        assertEq(fed.getFederationCount(), 0);
        assertEq(conf.getConfederationCount(), 0);
    }

    function testProductionLegacyFinishAndCommitmentRejectBeforeSessionLookup() public {
        vm.chainId(1);
        vm.expectRevert(FederationFactory.LegacyNetworkDisabled.selector);
        fed.deployFederation(bytes32(0), "", "");
        vm.expectRevert(ConfederationFactory.LegacyNetworkDisabled.selector);
        conf.deployConfederation(bytes32(0), "", "");
        vm.expectRevert(ConfederationFactory.LegacyNetworkDisabled.selector);
        conf.submitInterFederationCommitment(bytes32(0), "", bytes32(0));
    }

    function testAlreadyCreatedCompletedLegacySessionCannotFinishOnProduction() public {
        bytes32 fedId = fed.initiateFederationDKG(_founders(2), 2, "legacy-fed");
        bytes32 confId = conf.initiateConfederation(_ids(), _custodians(1), 2, 1, "legacy-conf");
        IDKGCoordinator.DKGSessionInfo memory info;
        info.isCompleted = true;
        vm.mockCall(address(dkg), abi.encodeWithSelector(IDKGCoordinator.getSessionInfo.selector), abi.encode(info));
        vm.chainId(1);
        vm.expectRevert(FederationFactory.LegacyNetworkDisabled.selector);
        fed.deployFederation(fedId, "Federation", "FED");
        vm.expectRevert(ConfederationFactory.LegacyNetworkDisabled.selector);
        conf.deployConfederation(confId, "Confederation", "CONF");
        assertFalse(fed.isFederationActive("legacy-fed"));
        assertFalse(conf.isConfederationActive("legacy-conf"));
    }

    function testBothPermittedNetworksRetainLegacyStartAndFinish() public {
        uint256[2] memory chains = [uint256(31337), uint256(11155111)];
        for (uint256 i; i < chains.length; ++i) {
            vm.chainId(chains[i]);
            string memory fedName = string.concat("legacy-fed-", vm.toString(i));
            string memory confName = string.concat("legacy-conf-", vm.toString(i));
            bytes32 fedId = fed.initiateFederationDKG(_founders(2), 2, fedName);
            bytes32 confId = conf.initiateConfederation(_ids(), _custodians(1), 2, 1, confName);
            IDKGCoordinator.DKGSessionInfo memory info;
            info.isCompleted = true;
            vm.mockCall(address(dkg), abi.encodeWithSelector(IDKGCoordinator.getSessionInfo.selector), abi.encode(info));
            vm.mockCall(address(dkg), abi.encodeWithSelector(IDKGCoordinator.getGroupPublicKey.selector), abi.encode(abi.encodePacked(GX)));
            vm.mockCall(FED_DEPLOYER, abi.encodeWithSelector(FederationDeployer.deployFederationContracts.selector),
                abi.encode(address(1), address(2), address(3)));
            vm.mockCall(CONF_DEPLOYER, abi.encodeWithSelector(ConfederationDeployer.deployConfederationContracts.selector),
                abi.encode(address(4), address(5), address(6), address(7)));
            fed.deployFederation(fedId, "Federation", "FED");
            conf.deployConfederation(confId, "Confederation", "CONF");
            assertTrue(fed.isFederationActive(fedName));
            assertTrue(conf.isConfederationActive(confName));
        }
    }

    function testV1AdoptionRemainsAvailableOnProductionAndDeployReachesFeeGate() public {
        vm.chainId(1);
        bytes32 fedId = keccak256("production federation");
        bytes32 confId = keccak256("production confederation");
        _completeCeremony(fedId, _founders(2), 2, keccak256("EVM_AUTH"), GX);
        _completeCeremony(confId, _roster(1, 1), 2, keccak256("EVM_AUTH"), GX);
        fed.reserveFederationId(fedId, "v1-fed");
        conf.reserveConfederationId(confId, "v1-conf");
        vm.startPrank(address(0x100));
        fed.adoptFederationDKG(fedId, "v1-fed");
        conf.adoptConfederationDKG(confId, _ids(), _custodians(1), 2, 1, "v1-conf");
        vm.stopPrank();
        vm.mockCall(FED_DEPLOYER, abi.encodeWithSelector(FederationDeployer.deployFederationContractsV1.selector),
            abi.encode(address(1), address(2), address(3)));
        vm.mockCall(CONF_DEPLOYER, abi.encodeWithSelector(ConfederationDeployer.deployConfederationContractsV1.selector),
            abi.encode(address(4), address(5), address(6), address(7)));
        vm.prank(address(0x100));
        vm.expectRevert("deployment fee not configured");
        fed.deployFederation(fedId, "Federation", "FED");
        vm.prank(address(0x100));
        vm.expectRevert("deployment fee not configured");
        conf.deployConfederation(confId, "Confederation", "CONF");
        assertTrue(fed.v1Sessions(fedId));
        assertTrue(conf.v1Sessions(confId));
    }

    function testOtherCompletedDKGCannotClaimFederationId() public {
        string memory federationId = "agreed-federation";
        bytes32 intended = keccak256("intended DKG");
        bytes32 other = keccak256("other completed DKG");
        address[] memory intendedRoster = _founders(2);
        address[] memory otherRoster = _roster(0, 2);
        _completeCeremony(other, otherRoster, 2, keccak256("EVM_AUTH"), SECOND_GX);

        vm.prank(otherRoster[0]);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        fed.adoptFederationDKG(other, federationId);
        vm.prank(otherRoster[0]);
        vm.expectRevert(FederationFactory.NotAuthorized.selector);
        fed.reserveFederationId(other, federationId);

        fed.reserveFederationId(intended, federationId);
        vm.prank(otherRoster[0]);
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        fed.adoptFederationDKG(other, federationId);
        assertFalse(fed.federationExists(federationId));
        assertFalse(fed.v1Sessions(other));

        _completeCeremony(intended, intendedRoster, 2, keccak256("EVM_AUTH"), GX);
        vm.prank(otherRoster[0]);
        vm.expectRevert("DKG admission");
        fed.adoptFederationDKG(intended, federationId);
        vm.prank(intendedRoster[0]);
        fed.adoptFederationDKG(intended, federationId);
        assertEq(fed.getDKGSession(federationId), intended);
        assertEq(fed.getFederation(federationId).members, intendedRoster);
        fed.reserveFederationId(intended, federationId);

        fed.reserveFederationId(other, "other-federation");
        vm.prank(otherRoster[0]);
        fed.adoptFederationDKG(other, "other-federation");
        assertEq(fed.getDKGSession("other-federation"), other);
    }

    function testReservationIsOneToOneAndLegacyCannotBypassIt() public {
        vm.chainId(31337);
        bytes32 intended = keccak256("reserved DKG");
        bytes32 other = keccak256("other DKG");
        address[] memory members = _founders(2);
        vm.prank(members[0]);
        vm.expectRevert(FederationFactory.NotAuthorized.selector);
        fed.initiateFederationDKG(members, 2, "free-id");

        fed.reserveFederationId(intended, "protected-id");
        fed.reserveFederationId(intended, "protected-id");
        vm.expectRevert(FederationFactory.FederationAlreadyExists.selector);
        fed.reserveFederationId(other, "protected-id");
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        fed.reserveFederationId(intended, "alias");
        vm.expectRevert(FederationFactory.FederationAlreadyExists.selector);
        fed.initiateFederationDKG(members, 2, "protected-id");
        vm.mockCall(address(dkg), abi.encodeWithSelector(IDKGCoordinator.initiateDKG.selector), abi.encode(intended));
        vm.expectRevert(FederationFactory.InvalidFederationReservation.selector);
        fed.initiateFederationDKG(members, 2, "legacy-alias");
        assertEq(fed.reservedFederationSession("protected-id"), intended);
        assertEq(fed.getFederationCount(), 0);
    }
}
