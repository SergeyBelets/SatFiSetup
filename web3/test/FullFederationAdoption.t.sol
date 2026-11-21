// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./ConfederationEpochFixture.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ConfederationFactory} from "../contracts/src/federation/ConfederationFactory.sol";
import {FederationFactory} from "../contracts/src/federation/FederationFactory.sol";
import {FederationDeployer} from "../contracts/src/federation/FederationDeployer.sol";
import {ConfederationDeployer} from "../contracts/src/federation/ConfederationDeployer.sol";
import {SimpleMultiPoolDeployer} from "../contracts/src/federation/SimpleMultiPoolDeployer.sol";
import {MultiPoolDeployer} from "../contracts/src/federation/MultiPoolDeployer.sol";
import {FROSTAuthorizationDeployer} from "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import {FROSTCustodianDeployer} from "../contracts/src/federation/FROSTCustodianDeployer.sol";
import {MultiPoolShareTokenDeployer} from "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";
import {ConfederationTokenDeployer} from "../contracts/src/federation/ConfederationTokenDeployer.sol";
import {FeeSystemDeployer} from "../contracts/src/federation/FeeSystemDeployer.sol";
import {DKGAuthorization} from "../contracts/src/federation/DKGAuthorization.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {CustodianDAOFeeRouter} from "../contracts/src/federation/CustodianDAOFeeRouter.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";

contract EpochFeeToken is ERC20 {
    constructor() ERC20("Fee token", "FEE") {}
    function mint(address account, uint256 amount) external { _mint(account, amount); }
}

/// @dev Real factories, completed DKGs, token/deployer graphs, validators and fee pipeline.
///      Only finalized FROST signature results use the explicit unit fixture from ConfederationEpochFixture.
contract FullFederationAdoptionTest is ConfederationEpochFixture {
    uint256 private constant FEE = 50 ether;
    address private constant TREASURY = address(0x7777);
    bytes32 private constant PROJECT_ID = keccak256("project fee authority");
    bytes32 private constant SOURCE_ID = keccak256("source federation epoch");
    bytes32 private constant CONF_ID = keccak256("new confederation epoch");
    FederationFactory private fed;
    ConfederationFactory private conf;
    ConfederationDeployer private confDeployer;
    MultiPoolDeployer private multiPoolDeployer;
    EpochFeeToken private token;
    CustodianDAOIncomeVault private vault;
    CustodianDAOFeeRouter private router;
    DKGAuthorization private projectAuth;
    address private sourcePool;

    function setUp() public {
        _baseSetUp();
        token = new EpochFeeToken();
        _completeCeremony(PROJECT_ID, _founders(2), 2, keccak256("EVM_AUTH"), SECOND_GX);
        projectAuth = new DKGAuthorization(address(coordinator), PROJECT_ID, abi.encodePacked(SECOND_GX), _founders(2), 2);
        vault = new CustodianDAOIncomeVault(address(token), address(projectAuth));
        router = new CustodianDAOFeeRouter(address(vault), TREASURY, 6000, 0);
        _factories();
        bytes32 op = router.getSetPayerOperationHash(address(fed), true);
        _approve(projectAuth, PROJECT_ID, op, address(router));
        router.setPayer(address(fed), true, op);
        op = router.getSetPayerOperationHash(address(conf), true);
        _approve(projectAuth, PROJECT_ID, op, address(router));
        router.setPayer(address(conf), true, op);
        fed.configureDeploymentFee(address(router), FEE, false);
        conf.configureDeploymentFee(address(router), FEE, false);
        for (uint256 i; i < 3; ++i) token.mint(address(uint160(0x100 + i)), 10 * FEE);
        _completeCeremony(SOURCE_ID, _founders(3), 2, keccak256("EVM_AUTH"), GX);
        fed.reserveFederationId(SOURCE_ID, "source");
        vm.startPrank(address(0x100));
        fed.adoptFederationDKG(SOURCE_ID, "source");
        token.approve(address(fed), FEE);
        (, sourcePool,) = fed.deployFederation(SOURCE_ID, "Source shares", "SOURCE");
        vm.stopPrank();
        token.mint(sourcePool, 777 ether);
        _completeCeremony(CONF_ID, _roster(3, 2), 4, keccak256("EVM_AUTH"), GX);
    }

    function _factories() private {
        // Fee-system constructors are unrelated to the basic entity deploy path in this integration fixture.
        FeeSystemDeployer feeSystem = new FeeSystemDeployer(address(1), address(2), address(3), address(4));
        FROSTAuthorizationDeployer authDeployer = new FROSTAuthorizationDeployer();
        MultiPoolShareTokenDeployer shareDeployer = new MultiPoolShareTokenDeployer();
        SimpleMultiPoolDeployer simpleDeployer = new SimpleMultiPoolDeployer(address(authDeployer), address(shareDeployer));
        authDeployer.setAuthorizedDeployer(address(simpleDeployer), true);
        shareDeployer.setAuthorizedDeployer(address(simpleDeployer), true);
        FederationDeployer fedDeployer = new FederationDeployer(address(simpleDeployer), address(feeSystem));
        simpleDeployer.setAuthorizedDeployer(address(fedDeployer), true);
        fed = new FederationFactory(address(dkg), address(fedDeployer), address(coordinator));
        fedDeployer.setAuthorizedDeployer(address(fed), true);
        ConfederationTokenDeployer confTokens = new ConfederationTokenDeployer();
        multiPoolDeployer = new MultiPoolDeployer(address(confTokens));
        confTokens.setAuthorizedDeployer(address(multiPoolDeployer), true);
        FROSTCustodianDeployer custodianDeployer = new FROSTCustodianDeployer();
        confDeployer = new ConfederationDeployer(address(custodianDeployer), address(multiPoolDeployer), address(feeSystem));
        custodianDeployer.setAuthorizedDeployer(address(confDeployer), true);
        multiPoolDeployer.setAuthorizedDeployer(address(confDeployer), true);
        conf = new ConfederationFactory(address(fed), address(dkg), address(confDeployer), address(coordinator));
        confDeployer.setAuthorizedDeployer(address(conf), true);
    }

    function _adopt() private {
        conf.reserveConfederationId(CONF_ID, "confederation");
        vm.prank(address(0x102));
        conf.adoptConfederationFromFederation(CONF_ID, "source", _custodians(2), 4, 1, "confederation");
    }

    function _deploy() private returns (FROSTAuthorization auth, CustodianValidator validator) {
        _adopt();
        vm.startPrank(address(0x102));
        token.approve(address(conf), FEE);
        (address a, address v,,) = conf.deployConfederation(CONF_ID, "Confederation shares", "CONF");
        vm.stopPrank();
        auth = FROSTAuthorization(a);
        validator = CustodianValidator(v);
    }

    function testFullFederationDeploysV1ChargesFeeAndLeavesSourceUnchanged() public {
        bytes32 sourceBefore = keccak256(abi.encode(fed.getFederation("source")));
        uint256 assetsBefore = token.balanceOf(sourcePool);
        uint256 payerBefore = token.balanceOf(address(0x102));
        uint256 treasuryBefore = token.balanceOf(TREASURY);
        (FROSTAuthorization auth, CustodianValidator validator) = _deploy();
        assertEq(auth.getParticipants(), _roster(3, 2));
        assertEq(auth.threshold(), 4);
        assertEq(DKGAuthorization(address(auth)).dkgId(), CONF_ID);
        assertEq(validator.getMinCustodiansRequired(), 1);
        assertEq(conf.getFounders("confederation"), _founders(3));
        IConfederationFactory.ConfederationConfig memory config = conf.getConfederation("confederation");
        assertEq(config.federationCount, 1);
        assertEq(config.federationIds[0], "source");
        assertTrue(config.isActive);
        assertGt(config.multiPool.code.length, 0);
        assertGt(config.shareToken.code.length, 0);
        assertEq(token.balanceOf(address(0x102)), payerBefore - FEE);
        assertEq(token.balanceOf(TREASURY), treasuryBefore + FEE);
        assertEq(token.balanceOf(address(router)), 0);
        assertEq(token.balanceOf(address(vault)), 0);
        assertEq(token.balanceOf(address(conf)), 0);
        assertEq(vault.dayDeployFeeIncome(block.timestamp / 1 days), 0);
        assertEq(vault.settlement(), address(0));
        assertEq(keccak256(abi.encode(fed.getFederation("source"))), sourceBefore);
        assertEq(token.balanceOf(sourcePool), assetsBefore);
        assertTrue(fed.isFederationActive("source"));
    }

    function testNonFirstFounderCanDeactivateWithBothRequiredApprovals() public {
        (FROSTAuthorization auth, CustodianValidator validator) = _deploy();
        bytes32 op = conf.getDeactivateConfederationOperationHash("confederation");
        vm.prank(address(0x101));
        vm.expectRevert("FROST threshold not met");
        conf.deactivateConfederation("confederation", op);
        _approve(auth, CONF_ID, op, address(conf));
        vm.prank(address(0x101));
        vm.expectRevert("Custodian threshold not met");
        conf.deactivateConfederation("confederation", op);
        vm.prank(address(0x200));
        validator.registerCustodianSignature(op);
        vm.prank(OUTSIDER);
        vm.expectRevert(ConfederationFactory.NotAuthorized.selector);
        conf.deactivateConfederation("confederation", op);
        vm.prank(address(0x101));
        conf.deactivateConfederation("confederation", op);
        assertFalse(conf.isConfederationActive("confederation"));
        assertTrue(fed.isFederationActive("source"));
        assertTrue(auth.executedOperations(op));
    }

    function testValidatorChangesLeaveImmutableDkgMembershipAndFounderListUntouched() public {
        (FROSTAuthorization auth, CustodianValidator validator) = _deploy();
        address extra = address(0x3333);
        bytes32 op = validator.getAddCustodianOperationHash(extra);
        _approve(auth, CONF_ID, op, address(validator));
        vm.prank(address(0x200));
        validator.registerCustodianSignature(op);
        validator.addCustodian(extra, op);
        assertTrue(validator.isCustodian(extra));
        assertFalse(auth.isParticipant(extra));
        op = validator.getRemoveCustodianOperationHash(address(0x201));
        _approve(auth, CONF_ID, op, address(validator));
        vm.prank(address(0x200));
        validator.registerCustodianSignature(op);
        validator.removeCustodian(address(0x201), op);
        assertFalse(validator.isCustodian(address(0x201)));
        assertTrue(auth.isParticipant(address(0x201)));
        assertEq(auth.getParticipants(), _roster(3, 2));
        assertEq(dkg.getSessionParticipants(CONF_ID), _roster(3, 2));
        assertEq(conf.getFounders("confederation"), _founders(3));
        assertEq(conf.getCustodianMembers("confederation")[1].signerAddress, address(0x201));
    }

    function testPaidDeployFailureRollsBackGraphAndFeeButPreservesPriorAdoption() public {
        _adopt();
        address predictedPool = multiPoolDeployer.predictNextMultiPoolAddress();
        uint256 nonceBefore = multiPoolDeployer.nextMultiPoolNonce();
        uint256 payerBefore = token.balanceOf(address(0x102));
        uint256 treasuryBefore = token.balanceOf(TREASURY);
        vm.prank(address(0x102));
        vm.expectRevert();
        conf.deployConfederation(CONF_ID, "Confederation shares", "CONF");
        assertEq(predictedPool.code.length, 0);
        assertEq(multiPoolDeployer.nextMultiPoolNonce(), nonceBefore);
        assertEq(token.balanceOf(address(0x102)), payerBefore);
        assertEq(token.balanceOf(TREASURY), treasuryBefore);
        assertEq(conf.getConfederation("confederation").frostMultisig, address(0));
        assertFalse(conf.isConfederationActive("confederation"));
        assertTrue(conf.confederationExists("confederation"));
        assertTrue(conf.v1Sessions(CONF_ID));
        vm.startPrank(address(0x102));
        token.approve(address(conf), FEE);
        conf.deployConfederation(CONF_ID, "Confederation shares", "CONF");
        vm.stopPrank();
        assertTrue(conf.isConfederationActive("confederation"));
        assertEq(token.balanceOf(TREASURY), treasuryBefore + FEE);
    }

    function testMaximumRosterV1DeploymentAndConstructorPayloadsStayWithinLimits() public {
        EpochFederationsFixture sources = new EpochFederationsFixture();
        sources.setFederation("maximum-source", _founders(10), true);
        ConfederationFactory maximum = new ConfederationFactory(address(sources), address(dkg), address(confDeployer), address(coordinator));
        confDeployer.setAuthorizedDeployer(address(maximum), true);
        maximum.configureDeploymentFee(address(0), 0, true);
        bytes32 id = keccak256("maximum full membership epoch");
        address[] memory roster = _roster(10, 6);
        _completeCeremony(id, roster, 12, keccak256("EVM_AUTH"), GX);
        maximum.reserveConfederationId(id, "maximum-confederation");
        vm.startPrank(address(0x109));
        maximum.adoptConfederationFromFederation(id, "maximum-source", _custodians(6), 12, 6, "maximum-confederation");
        (address a, address v, address p, address s) = maximum.deployConfederation(id, "Maximum confederation shares", "MAX");
        vm.stopPrank();
        assertEq(FROSTAuthorization(a).getParticipants(), roster);
        assertEq(FROSTAuthorization(a).threshold(), 12);
        assertEq(CustodianValidator(v).getMinCustodiansRequired(), 6);
        assertEq(maximum.getFounders("maximum-confederation"), _founders(10));
        assertLe(address(maximum).code.length, 24_576);
        assertLe(address(confDeployer).code.length, 24_576);
        assertLe(address(multiPoolDeployer).code.length, 24_576);
        assertLe(a.code.length, 24_576);
        assertLe(v.code.length, 24_576);
        assertLe(p.code.length, 24_576);
        assertLe(s.code.length, 24_576);
        bytes memory authorizationInitcode = bytes.concat(type(DKGAuthorization).creationCode,
            abi.encode(address(coordinator), id, abi.encodePacked(GX), roster, uint256(12)));
        assertLe(authorizationInitcode.length, 49_152);
        // The largest custodian array is the other 16-person extreme: f=1,c=15,t=12.
        address[] memory maximumCustodians = new address[](15);
        for (uint256 i; i < 15; ++i) maximumCustodians[i] = address(uint160(0x200 + i));
        bytes memory validatorInitcode = bytes.concat(type(CustodianValidator).creationCode,
            abi.encode(maximumCustodians, uint256(11), a));
        assertLe(validatorInitcode.length, 49_152);
        bytes memory factoryInitcode = bytes.concat(type(ConfederationFactory).creationCode,
            abi.encode(address(sources), address(dkg), address(confDeployer), address(coordinator)));
        assertLe(factoryInitcode.length, 49_152);
    }
}
