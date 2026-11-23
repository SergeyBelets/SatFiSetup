// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;
import {Test} from "forge-std/Test.sol";
import {FederationFactory} from "../contracts/src/federation/FederationFactory.sol";
import {ConfederationFactory} from "../contracts/src/federation/ConfederationFactory.sol";
import {FederationDeployer} from "../contracts/src/federation/FederationDeployer.sol";
import {ConfederationDeployer} from "../contracts/src/federation/ConfederationDeployer.sol";
import {IDKGCoordinator} from "../contracts/src/federation/interfaces/IDKGCoordinator.sol";
import {IFederationFactory} from "../contracts/src/federation/interfaces/IFederationFactory.sol";
import {IConfederationFactory} from "../contracts/src/federation/interfaces/IConfederationFactory.sol";

/// @dev Tests the real factory access-control boundary. Mock only completed DKG
/// data and the deployer response; integration tests exercise real deployments.
contract SecurityReviewFactoryDeploymentTest is Test {
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant OUTSIDER = address(0xBAD);
    address constant DKG = address(0x1001);
    address constant COORDINATOR = address(0x1002);
    address constant FED_DEPLOYER = address(0x1003);
    address constant CONF_DEPLOYER = address(0x1004);
    address constant FEDERATIONS = address(0x1005);
    bytes32 constant SESSION = keccak256("completed session");
    FederationFactory factory;
    ConfederationFactory confederationFactory;

    function setUp() public {
        vm.warp(10 days);
        vm.etch(COORDINATOR, hex"00");
        vm.mockCall(COORDINATOR, abi.encodeWithSignature("defaultVerifier()"), abi.encode(address(1)));
        vm.mockCall(COORDINATOR, abi.encodeWithSignature("dkgRegistry()"), abi.encode(DKG));
        vm.mockCall(DKG, abi.encodeWithSelector(IDKGCoordinator.initiateDKG.selector), abi.encode(SESSION));
        IDKGCoordinator.DKGSessionInfo memory info;
        info.isCompleted = true;
        vm.mockCall(DKG, abi.encodeWithSelector(IDKGCoordinator.getSessionInfo.selector), abi.encode(info));
        vm.mockCall(DKG, abi.encodeWithSelector(IDKGCoordinator.getGroupPublicKey.selector), abi.encode(abi.encodePacked(bytes32(uint256(1)))));
        vm.mockCall(DKG, abi.encodeWithSelector(IDKGCoordinator.getSessionParticipants.selector), abi.encode(_members()));
        vm.mockCall(DKG, abi.encodeWithSignature("isSessionParticipant(bytes32,address)", SESSION, ALICE), abi.encode(true));
        vm.mockCall(DKG, abi.encodeWithSignature("getCompletedResult(bytes32)", SESSION), abi.encode(
            bytes32(uint256(1)), bytes32(uint256(2)), bytes32(uint256(3)), keccak256("EVM_AUTH"),
            abi.encodePacked(bytes32(uint256(1))), _members(), uint256(2)
        ));
        vm.mockCall(FED_DEPLOYER, abi.encodeWithSelector(FederationDeployer.deployFederationContracts.selector), abi.encode(address(11), address(12), address(13)));
        vm.mockCall(FED_DEPLOYER, abi.encodeWithSelector(FederationDeployer.deployFederationContractsV1.selector), abi.encode(address(11), address(12), address(13)));
        vm.mockCall(CONF_DEPLOYER, abi.encodeWithSelector(ConfederationDeployer.deployConfederationContracts.selector), abi.encode(address(21), address(22), address(23), address(24)));
        vm.mockCall(CONF_DEPLOYER, abi.encodeWithSelector(ConfederationDeployer.deployConfederationContractsV1.selector), abi.encode(address(21), address(22), address(23), address(24)));
        vm.mockCall(FEDERATIONS, abi.encodeWithSelector(IFederationFactory.federationExists.selector), abi.encode(true));
        vm.mockCall(FEDERATIONS, abi.encodeWithSelector(IFederationFactory.isFederationActive.selector), abi.encode(true));
        IFederationFactory.FederationConfig memory federation;
        federation.members = new address[](1);
        federation.members[0] = ALICE;
        vm.mockCall(FEDERATIONS, abi.encodeWithSelector(IFederationFactory.getFederation.selector), abi.encode(federation));
        factory = new FederationFactory(DKG, FED_DEPLOYER, COORDINATOR);
        confederationFactory = new ConfederationFactory(FEDERATIONS, DKG, CONF_DEPLOYER, COORDINATOR);
        factory.configureDeploymentFee(address(0), 0, true);
        confederationFactory.configureDeploymentFee(address(0), 0, true);
    }

    function _members() private pure returns (address[] memory members) {
        members = new address[](2);
        members[0] = ALICE;
        members[1] = BOB;
    }

    function _custodians() private pure returns (IConfederationFactory.CustodianMember[] memory custodians) {
        custodians = new IConfederationFactory.CustodianMember[](1);
        custodians[0] = IConfederationFactory.CustodianMember("Bob", BOB, hex"02", true);
    }

    function _federations() private pure returns (string[] memory ids) {
        ids = new string[](1);
        ids[0] = "federation";
    }

    function testOutsiderCannotDeployFederation() public {
        factory.initiateFederationDKG(_members(), 2, "federation");
        vm.prank(OUTSIDER);
        vm.expectRevert(FederationFactory.NotAuthorized.selector);
        factory.deployFederation(SESSION, "Attacker metadata", "BAD");
    }

    function testOutsiderCannotDeployConfederation() public {
        confederationFactory.initiateConfederation(_federations(), _custodians(), 2, 1, "confederation");
        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.NotAuthorized.selector);
        confederationFactory.deployConfederation(SESSION, "Attacker metadata", "BAD");
    }

    function testOutsiderCannotDeployAdoptedFederation() public {
        factory.reserveFederationId(
            SESSION, "federation", keccak256(abi.encode(_members(), uint256(2))), keccak256(abi.encode("Share", "S"))
        );
        vm.prank(ALICE);
        factory.adoptFederationDKG(SESSION, "federation");
        vm.prank(OUTSIDER);
        vm.expectRevert(FederationFactory.NotAuthorized.selector);
        factory.deployFederation(SESSION, "Attacker metadata", "BAD");
    }

    function testOutsiderCannotDeployAdoptedConfederation() public {
        confederationFactory.reserveConfederationId(
            SESSION,
            "confederation",
            keccak256(abi.encode(uint8(0), _federations(), _custodians(), uint256(2), uint256(1))),
            keccak256(abi.encode("Share", "S"))
        );
        vm.prank(ALICE);
        confederationFactory.adoptConfederationDKG(SESSION, _federations(), _custodians(), 2, 1, "confederation");
        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.NotAuthorized.selector);
        confederationFactory.deployConfederation(SESSION, "Share", "S");
    }

    function testOriginalInitiatorCanDeployFederation() public {
        factory.initiateFederationDKG(_members(), 2, "federation");
        (address auth,,) = factory.deployFederation(SESSION, "Share", "S");
        assertEq(auth, address(11));
    }

    function testOriginalInitiatorCanDeployConfederation() public {
        confederationFactory.initiateConfederation(_federations(), _custodians(), 2, 1, "confederation");
        (address auth,,,) = confederationFactory.deployConfederation(SESSION, "Share", "S");
        assertEq(auth, address(21));
    }

    function testParticipantCanDeployFederation() public {
        factory.initiateFederationDKG(_members(), 2, "federation");
        vm.prank(BOB);
        (address auth,,) = factory.deployFederation(SESSION, "Share", "S");
        assertEq(auth, address(11));
    }

    function testParticipantCanDeployConfederation() public {
        confederationFactory.initiateConfederation(_federations(), _custodians(), 2, 1, "confederation");
        vm.prank(BOB);
        (address auth,,,) = confederationFactory.deployConfederation(SESSION, "Share", "S");
        assertEq(auth, address(21));
    }
}
