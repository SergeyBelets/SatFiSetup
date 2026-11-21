// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "./UnitEscrowFixture.sol";

// Core contracts
import "../contracts/src/federation/DKGCoordinator.sol";
import "../contracts/src/federation/FederationFactory.sol";
import "../contracts/src/federation/FederationDeployer.sol";
import "../contracts/src/federation/ConfederationFactory.sol";
import "../contracts/src/federation/ConfederationDeployer.sol";
import "../contracts/src/federation/CustodianValidator.sol";
import "../contracts/src/federation/FederationGovernance.sol";
import "../contracts/src/federation/SimpleMultiPoolDeployer.sol";
import "../contracts/src/federation/FROSTAuthorizationDeployer.sol";
import "../contracts/src/federation/MultiPoolShareTokenDeployer.sol";
import "../contracts/src/federation/FROSTCustodianDeployer.sol";
import "../contracts/src/federation/MultiPoolDeployer.sol";
import "../contracts/src/federation/ConfederationTokenDeployer.sol";
import "../contracts/src/federation/FeeSystemDeployer.sol";
import "../contracts/src/federation/FederationFeeManagerDeployer.sol";
import "../contracts/src/federation/FeeDistributionCalculatorDeployer.sol";
import "../contracts/src/federation/FederationNFTModifierDeployer.sol";
import "../contracts/src/federation/ProjectWalletRegistryDeployer.sol";
import "../contracts/src/federation/ControlledDeployer.sol";
import "../contracts/src/demo/SimpleMultiPool.sol";
import "../contracts/src/FROSTAuthorization.sol";
import "../contracts/src/demo/SimpleMiningPool.sol";
import "../contracts/src/demo/SimpleMiningPoolWithBridge.sol";
import "../contracts/src/demo/SimpleMiningPoolWithCalculator.sol";
import "../contracts/src/demo/BitcoinRedemptionTxBuilder.sol";
import "../contracts/src/demo/SimpleSPVContract.sol";
import "../contracts/src/tokens/PoolMpToken.sol";
import "../contracts/src/tokens/MultiPoolShareToken.sol";
import "../contracts/src/tokens/ConfederationToken.sol";
import "../contracts/src/tokens/CustodianDAOToken.sol";
import "../contracts/src/tokens/FederationNFTModifier.sol";
import "../contracts/src/federation/IFeeManager.sol";
import "../contracts/src/federation/FederationFeeManager.sol";
import "../contracts/src/federation/FeeDistributionCalculator.sol";
import "../contracts/src/federation/ProjectWalletRegistry.sol";
import "../contracts/src/federation/GlobalProjectWalletRegistry.sol";
import {FROSTVerifier} from "../contracts/src/FROSTVerifier.sol";
import {FROSTCoordinator} from "../contracts/src/FROSTCoordinator.sol";
import {Secp256k1} from "../contracts/src/vendor/cryptography/Secp256k1.sol";
import {Secp256k1Arithmetic} from "../contracts/src/vendor/cryptography/Secp256k1Arithmetic.sol";
import {Memory} from "../contracts/src/vendor/cryptography/Memory.sol";

contract ReadinessFeed {
    uint8 public constant decimals = 8;
}

contract MockAggregateFROSTVerifier {
    bool public result = true;

    function setResult(bool _result) external {
        result = _result;
    }

    function verify(bytes calldata, bytes32, bytes calldata, string calldata) external view returns (bool ok) {
        return result;
    }
}

contract MockAuthorizationCoordinator {
    struct Result {
        bytes groupPubkey;
        bytes32 messageHash;
        address target;
        uint256 threshold;
        bytes aggregateSignature;
    }

    address public immutable defaultVerifier;
    uint256 public nextSessionId;
    mapping(uint256 => Result) private results;
    mapping(uint256 => address[]) private sessionParticipants;

    constructor(address verifier) {
        defaultVerifier = verifier;
    }

    function createFinalizedAuthorization(
        bytes calldata groupPubkey,
        address[] calldata participants,
        uint256 threshold,
        bytes32 messageHash,
        address target
    ) external returns (uint256 sessionId) {
        sessionId = ++nextSessionId;
        results[sessionId] = Result(groupPubkey, messageHash, target, threshold, hex"01");
        sessionParticipants[sessionId] = participants;
    }

    function getSessionParticipants(uint256 sessionId) external view returns (address[] memory) {
        return sessionParticipants[sessionId];
    }

    function getAuthorizationResult(uint256 sessionId)
        external
        view
        returns (bytes memory, bytes32, bool, address, address, uint256, uint256, bytes memory)
    {
        Result storage result = results[sessionId];
        return (
            result.groupPubkey,
            result.messageHash,
            true,
            address(0),
            result.target,
            result.threshold,
            result.threshold,
            result.aggregateSignature
        );
    }
}

/**
 * @title CriticalTests
 * @notice Regression suite covering critical functionality and audit remediations
 * @dev Categories: DKG, Federation, Confederation, MultiPool, FROST, Custodian, Governance
 */
contract CriticalTests is Test {
    // ============ CONTRACTS ============
    DKGCoordinator public dkgCoordinator;
    FederationFactory public fedFactory;
    FederationDeployer public fedDeployer;
    ConfederationFactory public confFactory;
    ConfederationDeployer public confDeployer;
    DKGCoordinator public confDKG;
    SimpleSPVContract public spv;
    MockAggregateFROSTVerifier public authorizationVerifier;
    MockAuthorizationCoordinator public authorizationCoordinator;

    // ============ ADDRESSES ============
    address public member1 = address(0x1001);
    address public member2 = address(0x1002);
    address public member3 = address(0x1003);
    address public custodian1 = address(0x2001);
    address public custodian2 = address(0x2002);
    address public unauthorized = address(0x9999);

    // ============ SETUP ============
    function setUp() public {
        // Deploy infrastructure
        dkgCoordinator = new DKGCoordinator();
        authorizationVerifier = new MockAggregateFROSTVerifier();
        authorizationCoordinator = new MockAuthorizationCoordinator(address(authorizationVerifier));

        FederationFeeManagerDeployer feeManagerDeployer = new FederationFeeManagerDeployer();
        FeeDistributionCalculatorDeployer feeDistributorDeployer = new FeeDistributionCalculatorDeployer();
        FederationNFTModifierDeployer nftModifierDeployer = new FederationNFTModifierDeployer();
        ProjectWalletRegistryDeployer walletRegistryDeployer = new ProjectWalletRegistryDeployer();
        FeeSystemDeployer feeSystemDeployer = new FeeSystemDeployer(
            address(feeManagerDeployer),
            address(feeDistributorDeployer),
            address(nftModifierDeployer),
            address(walletRegistryDeployer)
        );
        feeManagerDeployer.setAuthorizedDeployer(address(feeSystemDeployer), true);
        feeDistributorDeployer.setAuthorizedDeployer(address(feeSystemDeployer), true);
        nftModifierDeployer.setAuthorizedDeployer(address(feeSystemDeployer), true);
        walletRegistryDeployer.setAuthorizedDeployer(address(feeSystemDeployer), true);

        // Deploy sub-deployers for federation
        FROSTAuthorizationDeployer frostAuthorizationDeployer = new FROSTAuthorizationDeployer();
        MultiPoolShareTokenDeployer shareTokenDeployer = new MultiPoolShareTokenDeployer();
        SimpleMultiPoolDeployer simpleMultiPoolDeployer =
            new SimpleMultiPoolDeployer(address(frostAuthorizationDeployer), address(shareTokenDeployer));
        frostAuthorizationDeployer.setAuthorizedDeployer(address(simpleMultiPoolDeployer), true);
        shareTokenDeployer.setAuthorizedDeployer(address(simpleMultiPoolDeployer), true);
        fedDeployer = new FederationDeployer(address(simpleMultiPoolDeployer), address(feeSystemDeployer));
        simpleMultiPoolDeployer.setAuthorizedDeployer(address(fedDeployer), true);

        // Deploy sub-deployers for confederation
        FROSTCustodianDeployer frostCustodianDeployer = new FROSTCustodianDeployer();
        ConfederationTokenDeployer confederationTokenDeployer = new ConfederationTokenDeployer();
        MultiPoolDeployer multiPoolDeployer = new MultiPoolDeployer(address(confederationTokenDeployer));
        confederationTokenDeployer.setAuthorizedDeployer(address(multiPoolDeployer), true);
        confDeployer = new ConfederationDeployer(
            address(frostCustodianDeployer), address(multiPoolDeployer), address(feeSystemDeployer)
        );
        frostCustodianDeployer.setAuthorizedDeployer(address(confDeployer), true);
        multiPoolDeployer.setAuthorizedDeployer(address(confDeployer), true);

        confDKG = new DKGCoordinator();

        // Deploy factories
        fedFactory =
            new FederationFactory(address(dkgCoordinator), address(fedDeployer), address(authorizationCoordinator));
        fedDeployer.setAuthorizedDeployer(address(fedFactory), true);
        confFactory = new ConfederationFactory(
            address(fedFactory), address(confDKG), address(confDeployer), address(authorizationCoordinator)
        );
        confDeployer.setAuthorizedDeployer(address(confFactory), true);

        // Deploy SPV for pools
        spv = new SimpleSPVContract(bytes32(0), 0, bytes32(0), bytes32(0), 0, 0);
    }

    // ============ HELPERS ============
    function _getMembers() internal view returns (address[] memory) {
        address[] memory members = new address[](3);
        members[0] = member1;
        members[1] = member2;
        members[2] = member3;
        return members;
    }

    function _validBIP340GroupPubkey() internal pure returns (bytes memory) {
        return hex"F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9";
    }

    function _authorize(FROSTAuthorization authorization, bytes32 operationHash, address target)
        internal
        returns (uint256 sessionId)
    {
        sessionId = authorizationCoordinator.createFinalizedAuthorization(
            authorization.groupPubkey(),
            authorization.getParticipants(),
            authorization.threshold(),
            operationHash,
            target
        );
        authorization.authorizeOperation(sessionId, operationHash, target);
    }

    function _newAuthorization(address[] memory participants, uint256 threshold)
        internal
        returns (FROSTAuthorization)
    {
        return new FROSTAuthorization(
            address(authorizationCoordinator), _validBIP340GroupPubkey(), participants, threshold
        );
    }

    function _newAuthorization() internal returns (FROSTAuthorization) {
        return _newAuthorization(_getMembers(), 2);
    }

    function _basePoint(uint256 scalar) internal view returns (uint256 x, uint256 y) {
        (uint256 px, uint256 py, uint256 pz) =
            Secp256k1Arithmetic.mulProjectivePoint(Secp256k1.GX, Secp256k1.GY, 1, scalar);
        return Secp256k1Arithmetic.convertProjectivePointToAffinePoint(Memory.allocate(192), px, py, pz);
    }

    function _completeDKG(DKGCoordinator dkg, bytes32 sessionId) internal {
        address[] memory participants = dkg.getSessionParticipants(sessionId);
        uint256 threshold = dkg.getSessionInfo(sessionId).threshold;

        _advanceDKGToPublicKeyPhase(dkg, sessionId, participants.length);

        bytes memory groupPublicKey = _validBIP340GroupPubkey();
        for (uint256 i = 0; i < threshold; i++) {
            vm.prank(participants[i]);
            dkg.approveGroupPublicKey(sessionId, groupPublicKey);
        }

        // Complete after threshold participants approved the same group key
        dkg.completeDKG(sessionId, groupPublicKey);
    }

    function _advanceDKGToPublicKeyPhase(DKGCoordinator dkg, bytes32 sessionId, uint256 pkShareSubmitCount) internal {
        address[] memory participants = dkg.getSessionParticipants(sessionId);
        uint256 threshold = dkg.getSessionInfo(sessionId).threshold;

        // VSS commitments
        bytes32[] memory commitments = new bytes32[](threshold);
        commitments[0] = keccak256("c0");
        commitments[1] = keccak256("c1");
        for (uint256 i = 0; i < participants.length; i++) {
            vm.prank(participants[i]);
            dkg.submitVSSCommitments(sessionId, commitments);
        }

        // Share hashes
        for (uint256 i = 0; i < participants.length; i++) {
            for (uint256 j = 0; j < participants.length; j++) {
                vm.prank(participants[i]);
                dkg.submitShareHash(sessionId, participants[j], keccak256(abi.encodePacked(i, j)));
            }
        }

        // Public key shares
        for (uint256 i = 0; i < pkShareSubmitCount; i++) {
            vm.prank(participants[i]);
            dkg.submitPublicKeyShare(sessionId, abi.encodePacked(bytes1(0x02), bytes32(Secp256k1.GX)));
        }
    }

    function _createFederation(string memory fedId) internal returns (bytes32 sessionId, address frost, address pool) {
        sessionId = fedFactory.initiateFederationDKG(_getMembers(), 2, fedId);
        _completeDKG(dkgCoordinator, sessionId);
        (frost, pool,) = fedFactory.deployFederation(sessionId, "Share", "S");
        UnitEscrowFixture escrow = new UnitEscrowFixture(frost);
        bytes32 setup = SimpleMultiPool(pool).getSetWithdrawalEscrowOperationHash(address(escrow));
        _authorize(FROSTAuthorization(frost),setup,pool);
        SimpleMultiPool(pool).setWithdrawalEscrow(address(escrow),setup);
    }

    function _setupPoolWithMpToken() internal returns (PoolMpToken mpToken, SimpleMiningPool miningPool) {
        miningPool = new SimpleMiningPool(address(spv), "TEST-POOL", hex"76a914abcdef88ac", 0);
        spv.setPoolContract(address(miningPool));
        mpToken = new PoolMpToken("Test MP", "mpBTC", address(miningPool), false);
        miningPool.setMpToken(address(mpToken));

        // Grant MINTER_ROLE to test contract (miningPool is admin from constructor)
        // Using startPrank/stopPrank because vm.prank only affects the NEXT call,
        // and mpToken.MINTER_ROLE() would consume it before grantRole()
        vm.startPrank(address(miningPool));
        mpToken.grantRole(mpToken.MINTER_ROLE(), address(this));
        vm.stopPrank();
    }

    // ======================================================================
    // DKG TESTS (5)
    // ======================================================================

    /// @notice Test 1: DKG initiation creates valid session
    function test_DKG_InitiateSession() public {
        address[] memory members = _getMembers();
        bytes32 sessionId = dkgCoordinator.initiateDKG(members, 2, "test-fed");

        assertTrue(sessionId != bytes32(0), "Session ID should not be zero");

        IDKGCoordinator.DKGSessionInfo memory info = dkgCoordinator.getSessionInfo(sessionId);
        assertEq(info.threshold, 2);
        assertEq(info.participantCount, 3);
        assertFalse(info.isCompleted);
    }

    /// @notice Test 2: Full DKG flow completes successfully
    function test_DKG_FullFlow() public {
        address[] memory members = _getMembers();
        bytes32 sessionId = dkgCoordinator.initiateDKG(members, 2, "test-fed");

        _completeDKG(dkgCoordinator, sessionId);

        IDKGCoordinator.DKGSessionInfo memory info = dkgCoordinator.getSessionInfo(sessionId);
        assertTrue(info.isCompleted, "DKG should be completed");
    }

    /// @notice Test 3: DKG completion requires threshold participant approval for the group key
    function test_DKG_RequiresThresholdGroupKeyApproval() public {
        address[] memory members = _getMembers();
        bytes32 sessionId = dkgCoordinator.initiateDKG(members, 2, "test-fed");

        _advanceDKGToPublicKeyPhase(dkgCoordinator, sessionId, members.length);

        vm.expectRevert(DKGCoordinator.GroupPublicKeyNotApproved.selector);
        dkgCoordinator.completeDKG(sessionId, _validBIP340GroupPubkey());

        vm.prank(member1);
        dkgCoordinator.approveGroupPublicKey(sessionId, _validBIP340GroupPubkey());

        vm.expectRevert(DKGCoordinator.GroupPublicKeyNotApproved.selector);
        dkgCoordinator.completeDKG(sessionId, _validBIP340GroupPubkey());

        vm.prank(member2);
        dkgCoordinator.approveGroupPublicKey(sessionId, _validBIP340GroupPubkey());

        dkgCoordinator.completeDKG(sessionId, _validBIP340GroupPubkey());
        IDKGCoordinator.DKGSessionInfo memory info = dkgCoordinator.getSessionInfo(sessionId);
        assertTrue(info.isCompleted, "DKG should complete after threshold approvals");
    }

    /// @notice Test 4: DKG can complete with threshold public key shares
    function test_DKG_CompletesWithThresholdPublicKeyShares() public {
        address[] memory members = _getMembers();
        bytes32 sessionId = dkgCoordinator.initiateDKG(members, 2, "test-fed");

        _advanceDKGToPublicKeyPhase(dkgCoordinator, sessionId, 2);

        vm.prank(member1);
        dkgCoordinator.approveGroupPublicKey(sessionId, _validBIP340GroupPubkey());
        vm.prank(member2);
        dkgCoordinator.approveGroupPublicKey(sessionId, _validBIP340GroupPubkey());

        dkgCoordinator.completeDKG(sessionId, _validBIP340GroupPubkey());
        IDKGCoordinator.DKGSessionInfo memory info = dkgCoordinator.getSessionInfo(sessionId);
        assertEq(info.pkShareCount, 2, "Only threshold PK shares should be required");
        assertTrue(info.isCompleted, "DKG should be completed");
    }

    /// @notice DKG rejects malformed or non-curve group public keys
    function test_DKG_RejectsInvalidGroupPublicKey() public {
        address[] memory members = _getMembers();
        bytes32 sessionId = dkgCoordinator.initiateDKG(members, 2, "test-fed");

        _advanceDKGToPublicKeyPhase(dkgCoordinator, sessionId, members.length);

        vm.expectRevert(DKGCoordinator.InvalidGroupPublicKey.selector);
        vm.prank(member1);
        dkgCoordinator.approveGroupPublicKey(sessionId, hex"02");

        vm.expectRevert(DKGCoordinator.InvalidGroupPublicKey.selector);
        dkgCoordinator.completeDKG(sessionId, hex"02");
    }

    /// @notice Test 3: DKG rejects invalid threshold
    function test_DKG_RejectInvalidThreshold() public {
        address[] memory members = _getMembers();

        vm.expectRevert();
        dkgCoordinator.initiateDKG(members, 5, "test-fed"); // threshold > participants
    }

    /// @notice Test 4: Only participants can submit to DKG
    function test_DKG_RejectNonParticipant() public {
        address[] memory members = _getMembers();
        bytes32 sessionId = dkgCoordinator.initiateDKG(members, 2, "test-fed");

        bytes32[] memory commitments = new bytes32[](2);
        vm.prank(unauthorized);
        vm.expectRevert();
        dkgCoordinator.submitVSSCommitments(sessionId, commitments);
    }

    /// @notice Test 5: DKG enforces phase ordering
    function test_DKG_EnforcePhaseOrder() public {
        address[] memory members = _getMembers();
        bytes32 sessionId = dkgCoordinator.initiateDKG(members, 2, "test-fed");

        // Try to submit share hash before commitments (wrong phase)
        vm.prank(member1);
        vm.expectRevert();
        dkgCoordinator.submitShareHash(sessionId, member2, keccak256("hash"));
    }

    // ======================================================================
    // FEDERATION FACTORY TESTS (4)
    // ======================================================================

    /// @notice Test 6: Create and deploy federation
    function test_Federation_CreateAndDeploy() public {
        (, address frost, address pool) = _createFederation("fed-1");

        assertTrue(frost != address(0), "FROST should be deployed");
        assertTrue(pool != address(0), "Pool should be deployed");
        assertTrue(fedFactory.isFederationActive("fed-1"), "Federation should be active");
    }

    /// @notice Test 7: Deactivate federation requires FROST threshold
    function test_Federation_DeactivateWithFROST() public {
        (, address frostAddr,) = _createFederation("fed-1");

        bytes32 opHash = fedFactory.getDeactivateFederationOperationHash("fed-1");
        FROSTAuthorization frost = FROSTAuthorization(frostAddr);

        // Sign with threshold (2 of 3)
        _authorize(frost, opHash, address(fedFactory));

        // Deactivate
        vm.prank(member1);
        fedFactory.deactivateFederation("fed-1", opHash);

        assertFalse(fedFactory.isFederationActive("fed-1"), "Federation should be deactivated");
    }

    /// @notice Test 8: Unauthorized cannot deactivate federation
    function test_Federation_UnauthorizedCannotDeactivate() public {
        _createFederation("fed-1");

        vm.prank(unauthorized);
        vm.expectRevert();
        fedFactory.deactivateFederation("fed-1", keccak256("op"));
    }

    /// @notice Test 9: Cannot deploy without completing DKG
    function test_Federation_CannotDeployWithoutDKG() public {
        fedFactory.initiateFederationDKG(_getMembers(), 2, "fed-1");
        bytes32 sessionId = fedFactory.getDKGSession("fed-1");

        vm.expectRevert();
        fedFactory.deployFederation(sessionId, "Share", "S");
    }

    /// @notice Test 10: Federation deployment cannot be repeated for the same DKG session
    function test_Federation_CannotDeployTwice() public {
        bytes32 sessionId = fedFactory.initiateFederationDKG(_getMembers(), 2, "fed-unique");
        _completeDKG(dkgCoordinator, sessionId);

        fedFactory.deployFederation(sessionId, "Share", "S");

        vm.expectRevert(FederationFactory.FederationAlreadyDeployed.selector);
        fedFactory.deployFederation(sessionId, "Share2", "S2");
    }

    /// @notice Infrastructure deployers reject arbitrary direct callers.
    function test_Deployer_RejectsUnauthorizedDirectDeployment() public {
        FROSTAuthorizationDeployer frostDeployer = new FROSTAuthorizationDeployer();
        MultiPoolShareTokenDeployer tokenDeployer = new MultiPoolShareTokenDeployer();
        SimpleMultiPoolDeployer deployer = new SimpleMultiPoolDeployer(address(frostDeployer), address(tokenDeployer));
        frostDeployer.setAuthorizedDeployer(address(deployer), true);
        tokenDeployer.setAuthorizedDeployer(address(deployer), true);

        vm.prank(unauthorized);
        vm.expectRevert();
        deployer.deploySimpleMultiPool(
            "spam", _getMembers(), 2, "Spam Share", "SPAM", address(authorizationCoordinator), _validBIP340GroupPubkey()
        );
    }

    function test_SimpleMultiPoolDeployer_PreservesDependencyOwnership() public {
        FROSTAuthorizationDeployer frostDeployer = new FROSTAuthorizationDeployer();
        MultiPoolShareTokenDeployer tokenDeployer = new MultiPoolShareTokenDeployer();
        SimpleMultiPoolDeployer deployer = new SimpleMultiPoolDeployer(address(frostDeployer), address(tokenDeployer));
        frostDeployer.setAuthorizedDeployer(address(deployer), true);
        tokenDeployer.setAuthorizedDeployer(address(deployer), true);

        address expectedPool = deployer.predictNextMultiPoolAddress();
        (address poolAddr, address frostAddr, address tokenAddr) = deployer.deploySimpleMultiPool(
            "split",
            _getMembers(),
            2,
            "Split Share",
            "SPLIT",
            address(authorizationCoordinator),
            _validBIP340GroupPubkey()
        );

        assertEq(poolAddr, expectedPool, "Predicted pool address mismatch");
        assertEq(MultiPoolShareToken(tokenAddr).multiPool(), poolAddr, "Share token owner must be pool");
        assertEq(SimpleMultiPool(poolAddr).getFrostMultisig(), frostAddr, "Pool FROST mismatch");
        assertEq(SimpleMultiPool(poolAddr).getShareToken(), tokenAddr, "Pool share token mismatch");
        assertTrue(
            MultiPoolShareToken(tokenAddr).hasRole(MultiPoolShareToken(tokenAddr).MINTER_ROLE(), poolAddr),
            "Pool must retain mint role"
        );

        address secondExpectedPool = deployer.predictNextMultiPoolAddress();
        (address secondPool,, address secondToken) = deployer.deploySimpleMultiPool(
            "split-2",
            _getMembers(),
            2,
            "Second Share",
            "SPLIT2",
            address(authorizationCoordinator),
            _validBIP340GroupPubkey()
        );
        assertEq(secondPool, secondExpectedPool, "Second predicted pool address mismatch");
        assertEq(MultiPoolShareToken(secondToken).multiPool(), secondPool, "Second share token owner mismatch");
    }

    function test_SimpleMultiPoolLeafDeployers_RejectUnauthorizedCallers() public {
        FROSTAuthorizationDeployer frostDeployer = new FROSTAuthorizationDeployer();
        MultiPoolShareTokenDeployer tokenDeployer = new MultiPoolShareTokenDeployer();

        address frostAddr = frostDeployer.deployFROSTAuthorization(
            address(authorizationCoordinator), _validBIP340GroupPubkey(), _getMembers(), 2
        );
        assertGt(frostAddr.code.length, 0, "Authorization should be deployed");

        vm.startPrank(unauthorized);
        vm.expectRevert(abi.encodeWithSelector(ControlledDeployer.UnauthorizedDeployer.selector, unauthorized));
        frostDeployer.deployFROSTAuthorization(
            address(authorizationCoordinator), _validBIP340GroupPubkey(), _getMembers(), 2
        );
        vm.expectRevert(abi.encodeWithSelector(ControlledDeployer.UnauthorizedDeployer.selector, unauthorized));
        tokenDeployer.deployMultiPoolShareToken("Share", "S", address(this));
        vm.stopPrank();
    }

    function test_DeploymentOrchestrators_RejectZeroSubDeployers() public {
        FROSTAuthorizationDeployer frostDeployer = new FROSTAuthorizationDeployer();
        MultiPoolShareTokenDeployer tokenDeployer = new MultiPoolShareTokenDeployer();

        vm.expectRevert(SimpleMultiPoolDeployer.InvalidSubDeployer.selector);
        new SimpleMultiPoolDeployer(address(0), address(tokenDeployer));
        vm.expectRevert(SimpleMultiPoolDeployer.InvalidSubDeployer.selector);
        new SimpleMultiPoolDeployer(address(frostDeployer), address(0));

        FederationFeeManagerDeployer feeManagerDeployer = new FederationFeeManagerDeployer();
        FeeDistributionCalculatorDeployer feeDistributorDeployer = new FeeDistributionCalculatorDeployer();
        FederationNFTModifierDeployer nftModifierDeployer = new FederationNFTModifierDeployer();
        ProjectWalletRegistryDeployer walletRegistryDeployer = new ProjectWalletRegistryDeployer();

        vm.expectRevert(FeeSystemDeployer.InvalidSubDeployer.selector);
        new FeeSystemDeployer(
            address(0), address(feeDistributorDeployer), address(nftModifierDeployer), address(walletRegistryDeployer)
        );
        vm.expectRevert(FeeSystemDeployer.InvalidSubDeployer.selector);
        new FeeSystemDeployer(
            address(feeManagerDeployer), address(feeDistributorDeployer), address(nftModifierDeployer), address(0)
        );
    }

    /// @notice Federation deployment always pins the production coordinator and canonical verifier.
    function test_Federation_DeploysCoordinatorEnforcedAuthorization() public {
        bytes32 sessionId = fedFactory.initiateFederationDKG(_getMembers(), 2, "fed-crypto");
        _completeDKG(dkgCoordinator, sessionId);

        (address frostAddr,,) = fedFactory.deployFederation(sessionId, "Share", "S");
        FROSTAuthorization frost = FROSTAuthorization(frostAddr);

        assertEq(address(frost.coordinator()), address(authorizationCoordinator), "Coordinator must be canonical");
        assertEq(address(frost.verifier()), address(authorizationVerifier), "Verifier must come from coordinator");
        assertEq(frost.threshold(), 2, "DKG threshold mismatch");

        bytes32 opHash = fedFactory.getDeactivateFederationOperationHash("fed-crypto");
        _authorize(frost, opHash, address(fedFactory));
        vm.prank(member1);
        fedFactory.deactivateFederation("fed-crypto", opHash);
        assertFalse(fedFactory.isFederationActive("fed-crypto"), "Finalized session should execute action");
    }

    // ======================================================================
    // MULTIPOOL TESTS (6)
    // ======================================================================

    /// @notice Test 10: Deposit to MultiPool
    function test_MultiPool_Deposit() public {
        (,, address poolAddr) = _createFederation("fed-1");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);

        // Create and add a pool
        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "TEST-POOL");
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "TEST-POOL", addPoolHash);

        // Mint and deposit
        mpToken.mint(member1, 1000);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000);
        multiPool.deposit(address(mpToken), 1000);
        vm.stopPrank();

        assertEq(multiPool.shareToken().balanceOf(member1), 1000, "Should have share tokens");
    }

    /// @notice Test 11: Request withdrawal from MultiPool
    function test_MultiPool_RequestWithdrawal() public {
        (,, address poolAddr) = _createFederation("fed-1");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);

        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "TEST-POOL");
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "TEST-POOL", addPoolHash);

        mpToken.mint(member1, 1000 * 1e10);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000 * 1e10);
        multiPool.deposit(address(mpToken), 1000 * 1e10);

        uint256 withdrawalId = multiPool.requestWithdrawal(address(mpToken), 500 * 1e10, hex"00141111111111111111111111111111111111111111");
        vm.stopPrank();

        (address requester,,,,,,,,) = multiPool.withdrawals(withdrawalId);
        assertEq(requester, member1, "Requester should be member1");
    }

    /// @notice Test 12: Process withdrawal requires FROST threshold
    function test_MultiPool_ProcessWithdrawalRequiresFROST() public {
        (,, address poolAddr) = _createFederation("fed-1");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);

        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "TEST-POOL");
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "TEST-POOL", addPoolHash);

        mpToken.mint(member1, 1000 * 1e10);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000 * 1e10);
        multiPool.deposit(address(mpToken), 1000 * 1e10);
        uint256 withdrawalId = multiPool.requestWithdrawal(address(mpToken), 500 * 1e10, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");
        vm.stopPrank();

        // Try to process without FROST signatures
        bytes32 processHash = multiPool.getProcessWithdrawalOperationHash(withdrawalId);
        vm.expectRevert("Insufficient FROST signatures");
        multiPool.processWithdrawal(withdrawalId, processHash);
    }

    /// @notice Test 13: Process withdrawal with valid FROST
    function test_MultiPool_ProcessWithdrawalWithFROST() public {
        (,, address poolAddr) = _createFederation("fed-1");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);

        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "TEST-POOL");
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "TEST-POOL", addPoolHash);

        mpToken.mint(member1, 1000 * 1e10);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000 * 1e10);
        multiPool.deposit(address(mpToken), 1000 * 1e10);
        uint256 withdrawalId = multiPool.requestWithdrawal(address(mpToken), 500 * 1e10, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");
        vm.stopPrank();

        // Sign and process
        bytes32 processHash = multiPool.getProcessWithdrawalOperationHash(withdrawalId);
        _authorize(frost, processHash, address(multiPool));

        multiPool.processWithdrawal(withdrawalId, processHash);
        assertFalse(multiPool.getWithdrawal(withdrawalId).isProcessed);
        UnitEscrowFixture(address(multiPool.withdrawalEscrow())).deliver(address(multiPool),withdrawalId);

        (,,,,,, bool isProcessed,,) = multiPool.withdrawals(withdrawalId);
        assertTrue(isProcessed, "Withdrawal should be processed");
    }

    /// @notice Test 14: Cannot withdraw more than balance
    function test_MultiPool_CannotWithdrawMoreThanBalance() public {
        (,, address poolAddr) = _createFederation("fed-1");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);

        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "TEST-POOL");
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "TEST-POOL", addPoolHash);

        mpToken.mint(member1, 100);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 100);
        multiPool.deposit(address(mpToken), 100);

        vm.expectRevert("Insufficient shares");
        multiPool.requestWithdrawal(address(mpToken), 500, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");
        vm.stopPrank();
    }

    /// @notice Test 15: Withdrawal with metadata (OP_RETURN)
    function test_MultiPool_WithdrawalWithMetadata() public {
        (,, address poolAddr) = _createFederation("fed-1");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);

        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "TEST-POOL");
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "TEST-POOL", addPoolHash);

        mpToken.mint(member1, 1000 * 1e10);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000 * 1e10);
        multiPool.deposit(address(mpToken), 1000 * 1e10);

        bytes memory metadata = bytes("REF:TX-001");
        uint256 withdrawalId = multiPool.requestWithdrawalWithMetadata(address(mpToken), 500 * 1e10, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798", metadata);
        vm.stopPrank();

        (,,,,, bytes memory storedMeta,,,) = multiPool.withdrawals(withdrawalId);
        assertEq(storedMeta, metadata, "Metadata should be stored");
    }

    /// @notice Test 16: Fee manager setup requires target-bound FROST approval
    function test_MultiPool_SetFeeManagerRequiresFROST() public {
        (,, address poolAddr) = _createFederation("fee-guard");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());

        IFeeManager.FeeConfig memory wrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 500,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });
        IFeeManager.FeeConfig memory unwrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 0,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        FederationFeeManager feeManagerContract =
            new FederationFeeManager(address(frost), address(0), address(0x5555), wrapConfig, unwrapConfig);

        _authorizeFeeCollector(frost, feeManagerContract, address(multiPool));
        bytes32 setFeeHash = multiPool.getSetFeeManagerOperationHash(address(feeManagerContract));
        vm.prank(unauthorized);
        vm.expectRevert("FROST threshold not met");
        multiPool.setFeeManager(address(feeManagerContract), setFeeHash);
        _authorize(frost, setFeeHash, address(multiPool));

        multiPool.setFeeManager(address(feeManagerContract), setFeeHash);
        assertEq(address(multiPool.feeManager()), address(feeManagerContract), "Fee manager should be set");
    }

    /// @notice Test 17: S-token balance cannot be redeemed against a different MP token pool
    function test_MultiPool_CannotWithdrawFromDifferentPool() public {
        (,, address poolAddr) = _createFederation("pool-accounting");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());

        (PoolMpToken mpTokenA,) = _setupPoolWithMpToken();
        (PoolMpToken mpTokenB,) = _setupPoolWithMpToken();

        bytes32 addPoolAHash = multiPool.getAddPoolOperationHash(address(mpTokenA), "POOL-A");
        _authorize(frost, addPoolAHash, address(multiPool));
        multiPool.addPool(address(mpTokenA), "POOL-A", addPoolAHash);

        bytes32 addPoolBHash = multiPool.getAddPoolOperationHash(address(mpTokenB), "POOL-B");
        _authorize(frost, addPoolBHash, address(multiPool));
        multiPool.addPool(address(mpTokenB), "POOL-B", addPoolBHash);

        mpTokenA.mint(member1, 1000);

        vm.startPrank(member1);
        mpTokenA.approve(address(multiPool), 1000);
        multiPool.deposit(address(mpTokenA), 1000);

        vm.expectRevert("Insufficient pool shares");
        multiPool.requestWithdrawal(address(mpTokenB), 1, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");
        vm.stopPrank();
    }

    /// @notice Test 18: Re-adding a deactivated pool preserves TVL and pool list uniqueness
    function test_MultiPool_ReaddPoolPreservesAccounting() public {
        (,, address poolAddr) = _createFederation("pool-readd");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "POOL-A");
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "POOL-A", addPoolHash);

        mpToken.mint(member1, 1000);
        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000);
        multiPool.deposit(address(mpToken), 1000);
        vm.stopPrank();

        bytes32 deactivateHash = multiPool.getDeactivatePoolOperationHash(address(mpToken));
        _authorize(frost, deactivateHash, address(multiPool));
        multiPool.deactivatePool(address(mpToken), deactivateHash);

        bytes32 reactivateHash = multiPool.getAddPoolOperationHash(address(mpToken), "POOL-A2");
        _authorize(frost, reactivateHash, address(multiPool));
        multiPool.addPool(address(mpToken), "POOL-A2", reactivateHash);

        assertEq(multiPool.getPoolCount(), 1, "Pool list should not contain duplicates");
        assertEq(multiPool.getTotalValueLocked(), 1000, "TVL should survive reactivation");
    }

    /// @notice Test 19: Withdrawal requests lock shares without burning until final processing
    function test_MultiPool_WithdrawalLockPreventsDoubleSpendAndCanCancel() public {
        (,, address poolAddr) = _createFederation("pool-lock");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "POOL-L");
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "POOL-L", addPoolHash);

        mpToken.mint(member1, 1000 * 1e10);
        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000 * 1e10);
        multiPool.deposit(address(mpToken), 1000 * 1e10);

        uint256 withdrawalId = multiPool.requestWithdrawal(address(mpToken), 700 * 1e10, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");
        assertEq(multiPool.shareToken().balanceOf(member1), 300 * 1e10, "Requested shares are escrowed");
        assertEq(multiPool.shareToken().balanceOf(address(multiPool)),700 * 1e10);
        assertEq(multiPool.pendingUserPoolShares(member1, address(mpToken)), 700 * 1e10, "Shares should be locked");
        assertEq(
            multiPool.getAvailableUserPoolBalance(member1, address(mpToken)), 300 * 1e10, "Only unlocked shares available"
        );

        vm.expectRevert("Insufficient shares");
        multiPool.requestWithdrawal(address(mpToken), 400 * 1e10, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");

        vm.warp(block.timestamp + multiPool.WITHDRAWAL_CANCEL_DELAY());
        multiPool.cancelWithdrawal(withdrawalId);
        vm.stopPrank();

        assertTrue(multiPool.withdrawalCancelled(withdrawalId), "Withdrawal should be cancelled");
        assertEq(multiPool.pendingUserPoolShares(member1, address(mpToken)), 0, "Lock should be released");
        assertEq(
            multiPool.getAvailableUserPoolBalance(member1, address(mpToken)), 1000 * 1e10, "Shares should be available again"
        );
    }

    /// @notice Withdrawal operation hashes are immutable after request creation.
    function test_MultiPool_WithdrawalHashStableAcrossGovernanceNonceChange() public {
        (,, address poolAddr) = _createFederation("pool-hash-stable");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());
        (PoolMpToken mpTokenA,) = _setupPoolWithMpToken();
        (PoolMpToken mpTokenB,) = _setupPoolWithMpToken();

        bytes32 addPoolAHash = multiPool.getAddPoolOperationHash(address(mpTokenA), "POOL-A");
        _authorize(frost, addPoolAHash, address(multiPool));
        multiPool.addPool(address(mpTokenA), "POOL-A", addPoolAHash);

        mpTokenA.mint(member1, 1000 * 1e10);
        vm.startPrank(member1);
        mpTokenA.approve(address(multiPool), 1000 * 1e10);
        multiPool.deposit(address(mpTokenA), 1000 * 1e10);
        uint256 withdrawalId = multiPool.requestWithdrawal(address(mpTokenA), 400 * 1e10, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");
        vm.stopPrank();

        bytes32 processHashBefore = multiPool.getProcessWithdrawalOperationHash(withdrawalId);

        bytes32 addPoolBHash = multiPool.getAddPoolOperationHash(address(mpTokenB), "POOL-B");
        _authorize(frost, addPoolBHash, address(multiPool));
        multiPool.addPool(address(mpTokenB), "POOL-B", addPoolBHash);

        assertEq(
            multiPool.getProcessWithdrawalOperationHash(withdrawalId),
            processHashBefore,
            "Withdrawal hash should not depend on governance nonce"
        );
        _authorize(frost, processHashBefore, address(multiPool));
        multiPool.processWithdrawal(withdrawalId, processHashBefore);
        UnitEscrowFixture(address(multiPool.withdrawalEscrow())).deliver(address(multiPool),withdrawalId);

        (,,,,,, bool processed,,) = multiPool.withdrawals(withdrawalId);
        assertTrue(processed, "Withdrawal should process with original hash");
    }

    // ======================================================================
    // FROST AUTHORIZATION TESTS
    // ======================================================================

    /// @notice Only a finalized coordinator session can approve an operation.
    function test_FROST_RequiresFinalizedCoordinatorSession() public {
        FROSTAuthorization frost = _newAuthorization();
        bytes32 opHash = keccak256("operation");

        vm.expectRevert();
        frost.authorizeOperation(999, opHash, address(this));

        _authorize(frost, opHash, address(this));
        assertTrue(frost.isOperationApproved(opHash, address(this)), "Finalized session should approve operation");
    }

    /// @notice Test 18: FROST prevents replay attack
    function test_FROST_PreventReplay() public {
        FROSTAuthorization frost = _newAuthorization();

        bytes32 opHash = keccak256("operation");
        _authorize(frost, opHash, address(this));

        frost.consumeOperation(opHash);

        vm.expectRevert(FROSTAuthorization.OperationAlreadyExecuted.selector);
        frost.consumeOperation(opHash);
    }

    /// @notice Test 19: FROST approvals can only be consumed by their bound target
    function test_FROST_PreventPreConsumeByWrongTarget() public {
        FROSTAuthorization frost = _newAuthorization();
        bytes32 opHash = keccak256("target-bound-operation");
        _authorize(frost, opHash, address(this));

        vm.expectRevert(
            abi.encodeWithSelector(FROSTAuthorization.OperationTargetMismatch.selector, address(this), unauthorized)
        );
        vm.prank(unauthorized);
        frost.consumeOperation(opHash);

        frost.consumeOperation(opHash);
        assertTrue(frost.executedOperations(opHash), "Bound target should consume approval");
    }

    /// @notice Enforced FROST sessions reject legacy hash-only shares.
    function test_FROSTCoordinator_EnforcedSessionsRequireVerifiableShares() public {
        MockAggregateFROSTVerifier verifier = new MockAggregateFROSTVerifier();
        FROSTCoordinator coordinator = new FROSTCoordinator(address(verifier));
        coordinator.grantRole(coordinator.SESSION_CREATOR_ROLE(), address(this));
        coordinator.grantRole(coordinator.CONFIRMER_ROLE(), address(this));

        FROSTCoordinator.OriginMeta memory origin = FROSTCoordinator.OriginMeta({
            originContract: address(this),
            originId: 1,
            networkId: 0,
            poolId: bytes32("pool")
        });

        uint256 sessionId = coordinator.createSession(
            _validBIP340GroupPubkey(),
            _getMembers(),
            2,
            uint64(block.timestamp + 1 days),
            true,
            address(0),
            FROSTCoordinator.SessionPurpose.WITHDRAWAL,
            origin
        );
        coordinator.bindMessageHash(sessionId, keccak256("btc-sighash"));

        vm.prank(member1);
        vm.expectRevert(FROSTCoordinator.VerifiableCommitmentRequired.selector);
        coordinator.submitNonceCommit(sessionId, keccak256("legacy-nonce"));

        uint256 gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798;
        uint256 gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8;

        vm.prank(member1);
        coordinator.submitNonceCommitments(sessionId, gx, gy, gx, gy);
        vm.prank(member2);
        coordinator.submitNonceCommitments(sessionId, gx, gy, gx, gy);
        vm.prank(member3);
        vm.expectRevert(FROSTCoordinator.NonceSetLocked.selector);
        coordinator.submitNonceCommitments(sessionId, gx, gy, gx, gy);

        vm.prank(member1);
        vm.expectRevert(FROSTCoordinator.PublicKeyShareMissing.selector);
        coordinator.submitVerifiableSignatureShare(sessionId, 1);
    }

    /// @notice Any session participant can aggregate and finalize without a designated leader role.
    function test_FROSTCoordinator_ParticipantCanFinalize() public {
        MockAggregateFROSTVerifier verifier = new MockAggregateFROSTVerifier();
        FROSTCoordinator coordinator = new FROSTCoordinator(address(verifier));
        coordinator.grantRole(coordinator.SESSION_CREATOR_ROLE(), address(this));

        FROSTCoordinator.OriginMeta memory origin = FROSTCoordinator.OriginMeta({
            originContract: address(this),
            originId: 2,
            networkId: 0,
            poolId: bytes32("pool")
        });

        uint256 sessionId = coordinator.createSession(
            _validBIP340GroupPubkey(),
            _getMembers(),
            2,
            uint64(block.timestamp + 1 days),
            false,
            address(0),
            FROSTCoordinator.SessionPurpose.WITHDRAWAL,
            origin
        );
        bytes32 messageHash = keccak256("operation-hash");
        coordinator.bindMessageHash(sessionId, messageHash);

        vm.prank(member1);
        coordinator.finalizeSession(sessionId, hex"01", messageHash);
        assertTrue(coordinator.isFinalized(sessionId), "Participant should finalize without CONFIRMER_ROLE");

        uint256 secondSessionId = coordinator.createSession(
            _validBIP340GroupPubkey(),
            _getMembers(),
            2,
            uint64(block.timestamp + 1 days),
            false,
            address(0),
            FROSTCoordinator.SessionPurpose.WITHDRAWAL,
            origin
        );
        coordinator.bindMessageHash(secondSessionId, messageHash);

        vm.prank(unauthorized);
        vm.expectRevert(FROSTCoordinator.NotParticipant.selector);
        coordinator.finalizeSession(secondSessionId, hex"01", messageHash);
    }

    /// @notice Real verifiable share -> BIP340 aggregate -> authorization -> target consumption.
    function test_FROST_EndToEndCoordinatorAuthorization() public {
        FROSTVerifier realVerifier = new FROSTVerifier();
        FROSTCoordinator realCoordinator = new FROSTCoordinator(address(realVerifier));

        address[] memory participants = new address[](1);
        participants[0] = member1;
        bytes memory groupPubkey = abi.encodePacked(bytes32(Secp256k1.GX));
        FROSTAuthorization authorization =
            new FROSTAuthorization(address(realCoordinator), groupPubkey, participants, 1);

        bytes32 operationHash = authorization.hashOperation(
            address(this), this.test_FROST_EndToEndCoordinatorAuthorization.selector, keccak256("e2e"), 0
        );
        FROSTCoordinator.OriginMeta memory origin = FROSTCoordinator.OriginMeta({
            originContract: address(this),
            originId: 77,
            networkId: uint16(block.chainid),
            poolId: bytes32("frost-e2e")
        });

        vm.prank(unauthorized);
        vm.expectRevert(FROSTCoordinator.UnauthorizedSessionCreator.selector);
        realCoordinator.createSession(
            groupPubkey,
            participants,
            1,
            uint64(block.timestamp + 1 days),
            true,
            address(0),
            FROSTCoordinator.SessionPurpose.UNKNOWN,
            origin
        );

        vm.prank(member1);
        vm.expectRevert(FROSTCoordinator.ParticipantSessionMustBeEnforced.selector);
        realCoordinator.createSession(
            groupPubkey,
            participants,
            1,
            uint64(block.timestamp + 1 days),
            false,
            address(0),
            FROSTCoordinator.SessionPurpose.UNKNOWN,
            origin
        );

        vm.prank(member1);
        vm.expectRevert(FROSTCoordinator.ParticipantSessionVerifierOverride.selector);
        realCoordinator.createSession(
            groupPubkey,
            participants,
            1,
            uint64(block.timestamp + 1 days),
            true,
            address(realVerifier),
            FROSTCoordinator.SessionPurpose.UNKNOWN,
            origin
        );

        vm.prank(member1);
        uint256 sessionId = realCoordinator.createSession(
            groupPubkey,
            participants,
            1,
            uint64(block.timestamp + 1 days),
            true,
            address(0),
            FROSTCoordinator.SessionPurpose.UNKNOWN,
            origin
        );
        vm.prank(member1);
        realCoordinator.bindMessageHash(sessionId, operationHash);
        vm.prank(member1);
        realCoordinator.registerParticipantPublicKeyShare(sessionId, member1, Secp256k1.GX, Secp256k1.GY);

        vm.prank(member1);
        realCoordinator.submitNonceCommitments(sessionId, Secp256k1.GX, Secp256k1.GY, Secp256k1.GX, Secp256k1.GY);

        bytes32 transcript = keccak256(
            abi.encode(
                "SATOHSI_FROST_NONCE_TRANSCRIPT_V1",
                block.chainid,
                address(realCoordinator),
                sessionId,
                groupPubkey,
                operationHash
            )
        );
        transcript = keccak256(abi.encode(transcript, member1, Secp256k1.GX, Secp256k1.GY, Secp256k1.GX, Secp256k1.GY));
        uint256 rho = uint256(keccak256(abi.encode("SATOHSI_FROST_BINDING_FACTOR_V1", sessionId, transcript, member1)))
            % Secp256k1.N;
        if (rho == 0) rho = 1;

        uint256 nonceScalar = addmod(1, rho, Secp256k1.N);
        (uint256 aggregateNonceX, uint256 aggregateNonceY) = _basePoint(nonceScalar);
        if ((aggregateNonceY & 1) != 0) {
            nonceScalar = Secp256k1.N - nonceScalar;
            aggregateNonceY = Secp256k1.P - aggregateNonceY;
        }

        bytes32 challengeTag = sha256("BIP0340/challenge");
        uint256 challenge = uint256(
            sha256(
                abi.encodePacked(
                    challengeTag, challengeTag, bytes32(aggregateNonceX), bytes32(Secp256k1.GX), operationHash
                )
            )
        ) % Secp256k1.N;
        uint256 signatureShare = addmod(nonceScalar, challenge, Secp256k1.N);

        vm.prank(member1);
        realCoordinator.submitVerifiableSignatureShare(sessionId, signatureShare);

        (bool locked,, uint256 coordinatorNonceX, uint256 coordinatorNonceY, uint256 verifiedShares) =
            realCoordinator.getNonceSetStatus(sessionId);
        assertTrue(locked, "Nonce set should be locked");
        assertEq(coordinatorNonceX, aggregateNonceX, "Aggregate nonce x mismatch");
        assertEq(coordinatorNonceY, aggregateNonceY, "Aggregate nonce y mismatch");
        assertEq(verifiedShares, 1, "Share must be verified on-chain");

        bytes memory aggregateSignature = abi.encodePacked(bytes32(aggregateNonceX), bytes32(signatureShare));
        vm.prank(member1);
        realCoordinator.finalizeSession(sessionId, aggregateSignature, operationHash);

        authorization.authorizeOperation(sessionId, operationHash, address(this));
        assertTrue(authorization.isOperationApproved(operationHash, address(this)), "Operation should be approved");
        authorization.consumeOperation(operationHash);
        assertTrue(authorization.executedOperations(operationHash), "Target should consume operation once");
    }

    /// @notice Finalized coordinator results authorize target-bound operations and are re-verified.
    function test_FROST_AggregateSignatureApprovalPath() public {
        FROSTAuthorization frost = _newAuthorization();

        address target = address(0xCAFE);
        bytes32 opHash = frost.hashOperation(target, bytes4(keccak256("execute()")), keccak256("params"), 0);
        _authorize(frost, opHash, target);
        assertTrue(frost.isOperationApproved(opHash, target), "Coordinator result should satisfy authorization");

        vm.prank(target);
        frost.consumeOperation(opHash);
        assertTrue(frost.executedOperations(opHash), "Target should consume aggregate approval");

        bytes32 rejectedOpHash = frost.hashOperation(target, bytes4(keccak256("reject()")), keccak256("params"), 1);
        uint256 rejectedSession = authorizationCoordinator.createFinalizedAuthorization(
            frost.groupPubkey(), frost.getParticipants(), frost.threshold(), rejectedOpHash, target
        );
        authorizationVerifier.setResult(false);
        vm.expectRevert(FROSTAuthorization.InvalidAggregateSignature.selector);
        frost.authorizeOperation(rejectedSession, rejectedOpHash, target);
        authorizationVerifier.setResult(true);
    }

    /// @notice Imported FROST verifier accepts a real BIP340 Schnorr test vector
    function test_FROSTVerifier_BIP340Vector() public {
        FROSTVerifier verifier = new FROSTVerifier();

        bytes memory groupPubkey = hex"F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9";
        bytes32 messageHash = bytes32(0);
        bytes memory signature =
            hex"E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0";

        assertTrue(verifier.verify(groupPubkey, messageHash, signature, "Schnorr"), "BIP340 vector should verify");
        assertFalse(
            verifier.verify(groupPubkey, bytes32(uint256(1)), signature, "Schnorr"), "Signature should be message-bound"
        );
    }

    // ======================================================================
    // CUSTODIAN VALIDATOR TESTS (3)
    // ======================================================================

    /// @notice Test 19: Register custodian signature
    function test_Custodian_RegisterSignature() public {
        address[] memory custodians = new address[](2);
        custodians[0] = custodian1;
        custodians[1] = custodian2;

        CustodianValidator validator = new CustodianValidator(custodians, 2, address(0x1));

        bytes32 opHash = keccak256("operation");

        vm.prank(custodian1);
        validator.registerCustodianSignature(opHash);

        assertTrue(validator.hasCustodianSigned(opHash, custodian1), "Custodian1 should have signed");
    }

    /// @notice Test 20: Custodian meets minimum threshold
    function test_Custodian_MeetsMinimum() public {
        address[] memory custodians = new address[](2);
        custodians[0] = custodian1;
        custodians[1] = custodian2;

        CustodianValidator validator = new CustodianValidator(custodians, 2, address(0x1));

        bytes32 opHash = keccak256("operation");

        vm.prank(custodian1);
        validator.registerCustodianSignature(opHash);
        assertFalse(validator.meetsMinimumCustodians(opHash), "Should not meet minimum yet");

        vm.prank(custodian2);
        validator.registerCustodianSignature(opHash);
        assertTrue(validator.meetsMinimumCustodians(opHash), "Should meet minimum");
    }

    /// @notice Test 21: Non-custodian cannot register
    function test_Custodian_NonCustodianCannotRegister() public {
        address[] memory custodians = new address[](2);
        custodians[0] = custodian1;
        custodians[1] = custodian2;

        CustodianValidator validator = new CustodianValidator(custodians, 2, address(0x1));

        vm.prank(unauthorized);
        vm.expectRevert(CustodianValidator.NotACustodian.selector);
        validator.registerCustodianSignature(keccak256("op"));
    }

    /// @notice Test 24: Custodian management requires both FROST and custodian approvals
    function test_Custodian_ManagementRequiresDualApproval() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        address[] memory custodians = new address[](2);
        custodians[0] = custodian1;
        custodians[1] = custodian2;
        CustodianValidator validator = new CustodianValidator(custodians, 2, address(frost));

        address newCustodian = address(0x2003);
        bytes32 opHash = validator.getAddCustodianOperationHash(newCustodian);
        _authorize(frost, opHash, address(validator));

        vm.expectRevert(CustodianValidator.ApprovalNotMet.selector);
        validator.addCustodian(newCustodian, opHash);

        vm.prank(custodian1);
        validator.registerCustodianSignature(opHash);
        vm.prank(custodian2);
        validator.registerCustodianSignature(opHash);

        validator.addCustodian(newCustodian, opHash);
        assertTrue(validator.isCustodian(newCustodian), "New custodian should be added after dual approval");
    }

    // ======================================================================
    // CONFEDERATION TESTS (3)
    // ======================================================================

    /// @notice Test 22: Create confederation with federation + custodians
    function test_Confederation_Create() public {
        _createFederation("fed-1");

        IConfederationFactory.CustodianMember[] memory custodians = new IConfederationFactory.CustodianMember[](2);
        custodians[0] = IConfederationFactory.CustodianMember({
            custodianId: "cust-1",
            signerAddress: custodian1,
            custodianPubkey: hex"02",
            isInstitutional: true
        });
        custodians[1] = IConfederationFactory.CustodianMember({
            custodianId: "cust-2",
            signerAddress: custodian2,
            custodianPubkey: hex"03",
            isInstitutional: true
        });

        string[] memory fedIds = new string[](1);
        fedIds[0] = "fed-1";

        bytes32 sessionId = confFactory.initiateConfederation(fedIds, custodians, 3, 2, "conf-1");

        assertTrue(sessionId != bytes32(0), "Session should be created");
        assertTrue(confFactory.confederationExists("conf-1"), "Confederation should exist");
    }

    /// @notice Test 23: Deploy confederation after DKG
    function test_Confederation_Deploy() public {
        _createFederation("fed-1");

        IConfederationFactory.CustodianMember[] memory custodians = new IConfederationFactory.CustodianMember[](2);
        custodians[0] = IConfederationFactory.CustodianMember("c1", custodian1, hex"02", true);
        custodians[1] = IConfederationFactory.CustodianMember("c2", custodian2, hex"03", true);

        string[] memory fedIds = new string[](1);
        fedIds[0] = "fed-1";

        bytes32 sessionId = confFactory.initiateConfederation(fedIds, custodians, 3, 2, "conf-1");

        _completeDKG(confDKG, sessionId);

        (address frost, address validator, address pool, address shareToken) =
            confFactory.deployConfederation(sessionId, "S", "S");

        assertTrue(frost != address(0));
        assertTrue(validator != address(0));
        assertTrue(pool != address(0));
        assertTrue(shareToken != address(0));
        assertTrue(confFactory.isConfederationActive("conf-1"));
    }

    /// @notice Test 24: Confederation deployment cannot be repeated for the same DKG session
    function test_Confederation_CannotDeployTwice() public {
        _createFederation("fed-1");

        IConfederationFactory.CustodianMember[] memory custodians = new IConfederationFactory.CustodianMember[](2);
        custodians[0] = IConfederationFactory.CustodianMember("c1", custodian1, hex"02", true);
        custodians[1] = IConfederationFactory.CustodianMember("c2", custodian2, hex"03", true);

        string[] memory fedIds = new string[](1);
        fedIds[0] = "fed-1";

        bytes32 sessionId = confFactory.initiateConfederation(fedIds, custodians, 3, 2, "conf-unique");
        _completeDKG(confDKG, sessionId);

        confFactory.deployConfederation(sessionId, "S", "S");

        vm.expectRevert(ConfederationFactory.ConfederationAlreadyDeployed.selector);
        confFactory.deployConfederation(sessionId, "S2", "S2");
    }

    /// @notice Confederation deployment always pins the production coordinator and canonical verifier.
    function test_Confederation_DeploysCoordinatorEnforcedAuthorization() public {
        _createFederation("fed-1");

        IConfederationFactory.CustodianMember[] memory custodians = new IConfederationFactory.CustodianMember[](2);
        custodians[0] = IConfederationFactory.CustodianMember("c1", custodian1, hex"02", true);
        custodians[1] = IConfederationFactory.CustodianMember("c2", custodian2, hex"03", true);

        string[] memory fedIds = new string[](1);
        fedIds[0] = "fed-1";

        bytes32 sessionId = confFactory.initiateConfederation(fedIds, custodians, 3, 2, "conf-crypto");
        _completeDKG(confDKG, sessionId);

        (address frostAddr, address validatorAddr,,) = confFactory.deployConfederation(sessionId, "S", "S");
        FROSTAuthorization frost = FROSTAuthorization(frostAddr);

        assertEq(address(frost.coordinator()), address(authorizationCoordinator), "Coordinator must be canonical");
        assertEq(address(frost.verifier()), address(authorizationVerifier), "Verifier must come from coordinator");
        assertEq(frost.threshold(), 3, "DKG threshold mismatch");

        bytes32 opHash = confFactory.getDeactivateConfederationOperationHash("conf-crypto");
        _authorize(frost, opHash, address(confFactory));
        vm.prank(custodian1);
        CustodianValidator(validatorAddr).registerCustodianSignature(opHash);
        vm.prank(custodian2);
        CustodianValidator(validatorAddr).registerCustodianSignature(opHash);
        vm.prank(member1);
        confFactory.deactivateConfederation("conf-crypto", opHash);
        assertFalse(confFactory.isConfederationActive("conf-crypto"), "Finalized session should execute action");
    }

    /// @notice Test 24: Cannot create confederation with non-existent federation
    function test_Confederation_RejectNonExistentFederation() public {
        IConfederationFactory.CustodianMember[] memory custodians = new IConfederationFactory.CustodianMember[](1);
        custodians[0] = IConfederationFactory.CustodianMember("c1", custodian1, hex"02", true);

        string[] memory fedIds = new string[](1);
        fedIds[0] = "non-existent";

        vm.expectRevert();
        confFactory.initiateConfederation(fedIds, custodians, 1, 1, "conf-1");
    }

    // ======================================================================
    // GOVERNANCE TESTS (3)
    // ======================================================================

    /// @notice FROST participant and threshold changes require a new DKG/key rotation.
    function test_Governance_ThresholdChangeRequiresNewDKG() public {
        (, address frostAddr, address poolAddr) = _createFederation("fed-1");

        FederationGovernance governance = new FederationGovernance("fed-1", frostAddr, poolAddr);

        vm.prank(member1);
        vm.expectRevert(FederationGovernance.ConfigurationRotationRequiresNewDKG.selector);
        governance.proposeThresholdChange(3, "Change threshold to 3");
    }

    /// @notice Governance voting relays a finalized FROST session for the concrete target action.
    function test_Governance_AuthorizesProposalFromCoordinatorSession() public {
        (, address frostAddr, address poolAddr) = _createFederation("fed-1");

        FederationGovernance governance = new FederationGovernance("fed-1", frostAddr, poolAddr);
        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        vm.prank(member1);
        uint256 proposalId = governance.proposeAddPool(address(mpToken), "GOV-POOL", "Add pool");

        // Get operation hash
        (,,,,,,, bytes32 opHash,,) = governance.proposals(proposalId);
        assertTrue(opHash != bytes32(0), "Operation hash should be set");

        FROSTAuthorization frost = FROSTAuthorization(frostAddr);
        uint256 sessionId = authorizationCoordinator.createFinalizedAuthorization(
            frost.groupPubkey(), frost.getParticipants(), frost.threshold(), opHash, poolAddr
        );
        vm.prank(member1);
        governance.vote(proposalId, sessionId);

        assertTrue(frost.isOperationApproved(opHash, poolAddr), "Target action should be authorized");
    }

    function test_Governance_SelfTargetProposalConsumesAuthorization() public {
        (, address frostAddr, address poolAddr) = _createFederation("fed-governance-consume");
        FederationGovernance governance = new FederationGovernance("fed-governance-consume", frostAddr, poolAddr);

        vm.prank(member1);
        uint256 proposalId = governance.proposeCustomAction(hex"1234", "Signal action");
        (,,,,,,, bytes32 operationHash,,) = governance.proposals(proposalId);

        FROSTAuthorization authorization = FROSTAuthorization(frostAddr);
        uint256 sessionId = authorizationCoordinator.createFinalizedAuthorization(
            authorization.groupPubkey(),
            authorization.getParticipants(),
            authorization.threshold(),
            operationHash,
            address(governance)
        );
        vm.prank(member1);
        governance.vote(proposalId, sessionId);

        vm.warp(block.timestamp + governance.VOTING_PERIOD() + governance.EXECUTION_DELAY());
        governance.execute(proposalId);
        assertTrue(authorization.executedOperations(operationHash), "Governance must consume self-target approval");
    }

    /// @notice Test 27: Only members can propose
    function test_Governance_OnlyMembersCanPropose() public {
        (, address frostAddr, address poolAddr) = _createFederation("fed-1");

        FederationGovernance governance = new FederationGovernance("fed-1", frostAddr, poolAddr);

        vm.prank(unauthorized);
        vm.expectRevert();
        governance.proposeAddPool(address(0x1234), "POOL", "Add pool");
    }

    // ======================================================================
    // INTEGRATION TESTS (3)
    // ======================================================================

    /// @notice Test 28: Full federation lifecycle
    function test_Integration_FederationLifecycle() public {
        // 1. Create + DKG + Deploy
        bytes32 sessionId = fedFactory.initiateFederationDKG(_getMembers(), 2, "lifecycle-fed");
        assertTrue(fedFactory.federationExists("lifecycle-fed"));

        _completeDKG(dkgCoordinator, sessionId);

        (address frost, address pool, address shareToken) = fedFactory.deployFederation(sessionId, "Share", "S");
        assertTrue(fedFactory.isFederationActive("lifecycle-fed"));
        assertTrue(frost != address(0) && pool != address(0) && shareToken != address(0));

        // 2. Deactivate
        bytes32 opHash = fedFactory.getDeactivateFederationOperationHash("lifecycle-fed");
        FROSTAuthorization frostContract = FROSTAuthorization(frost);
        _authorize(frostContract, opHash, address(fedFactory));

        vm.prank(member1);
        fedFactory.deactivateFederation("lifecycle-fed", opHash);
        assertFalse(fedFactory.isFederationActive("lifecycle-fed"));
    }

    /// @notice Test 29: Multiple federations coexist
    function test_Integration_MultipleFederations() public {
        _createFederation("fed-1");
        _createFederation("fed-2");

        assertTrue(fedFactory.isFederationActive("fed-1"));
        assertTrue(fedFactory.isFederationActive("fed-2"));
        assertEq(fedFactory.getFederationCount(), 2);
    }

    /// @notice Test 30: End-to-end deposit and withdrawal
    function test_Integration_DepositWithdrawFlow() public {
        (,, address poolAddr) = _createFederation("e2e-fed");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());

        // Setup pool
        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "E2E-POOL");
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "E2E-POOL", addPoolHash);

        // Deposit
        mpToken.mint(member1, 1000 * 1e10);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000 * 1e10);
        multiPool.deposit(address(mpToken), 1000 * 1e10);
        assertEq(multiPool.shareToken().balanceOf(member1), 1000 * 1e10);

        // Request withdrawal
        uint256 withdrawalId = multiPool.requestWithdrawal(address(mpToken), 500 * 1e10, hex"512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798");
        assertEq(multiPool.shareToken().balanceOf(member1), 500 * 1e10);
        assertEq(multiPool.shareToken().balanceOf(address(multiPool)),500 * 1e10);
        assertEq(multiPool.pendingUserPoolShares(member1, address(mpToken)), 500 * 1e10);
        assertEq(multiPool.getAvailableUserPoolBalance(member1, address(mpToken)), 500 * 1e10);
        vm.stopPrank();

        // Process withdrawal
        bytes32 processHash = multiPool.getProcessWithdrawalOperationHash(withdrawalId);
        _authorize(frost, processHash, address(multiPool));

        multiPool.processWithdrawal(withdrawalId, processHash);
        assertFalse(multiPool.getWithdrawal(withdrawalId).isProcessed);
        UnitEscrowFixture(address(multiPool.withdrawalEscrow())).deliver(address(multiPool),withdrawalId);

        (,,,,,, bool processed,,) = multiPool.withdrawals(withdrawalId);
        assertTrue(processed);
        assertEq(multiPool.shareToken().balanceOf(member1), 500 * 1e10);
        assertEq(multiPool.pendingUserPoolShares(member1, address(mpToken)), 0);
    }

    // ======================================================================
    // CONFEDERATION TOKEN TESTS (2)
    // ======================================================================

    /// @notice Test 31: ConfederationToken mint and burn
    function test_ConfederationToken_MintBurn() public {
        // Deploy token with this contract as the pool
        ConfederationToken token = new ConfederationToken("Conf Token", "CONF", address(this));

        // Mint tokens
        token.mint(member1, 1000);
        assertEq(token.balanceOf(member1), 1000, "Should have 1000 tokens after mint");

        // Burn tokens
        token.burn(member1, 400);
        assertEq(token.balanceOf(member1), 600, "Should have 600 tokens after burn");
    }

    /// @notice Test 32: ConfederationToken access control
    function test_ConfederationToken_AccessControl() public {
        ConfederationToken token = new ConfederationToken("Conf Token", "CONF", address(this));

        // Unauthorized cannot mint
        vm.prank(unauthorized);
        vm.expectRevert();
        token.mint(member1, 1000);

        // Unauthorized cannot burn
        vm.prank(unauthorized);
        vm.expectRevert();
        token.burn(member1, 100);
    }

    // ======================================================================
    // FEE MANAGER TESTS (3)
    // ======================================================================

    function test_FeeSystemDeployer_DeploysExternalComponentsAndTransfersNFTRoles() public {
        FederationFeeManagerDeployer feeManagerDeployer = new FederationFeeManagerDeployer();
        FeeDistributionCalculatorDeployer feeDistributorDeployer = new FeeDistributionCalculatorDeployer();
        FederationNFTModifierDeployer nftModifierDeployer = new FederationNFTModifierDeployer();
        ProjectWalletRegistryDeployer walletRegistryDeployer = new ProjectWalletRegistryDeployer();
        FeeSystemDeployer deployer = new FeeSystemDeployer(
            address(feeManagerDeployer),
            address(feeDistributorDeployer),
            address(nftModifierDeployer),
            address(walletRegistryDeployer)
        );
        feeManagerDeployer.setAuthorizedDeployer(address(deployer), true);
        feeDistributorDeployer.setAuthorizedDeployer(address(deployer), true);
        nftModifierDeployer.setAuthorizedDeployer(address(deployer), true);
        walletRegistryDeployer.setAuthorizedDeployer(address(deployer), true);

        FROSTAuthorization frost = _newAuthorization();
        IFeeManager.FeeConfig memory feeConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 100,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });
        FeeDistributionCalculator.DistributionConfig memory distConfig = FeeDistributionCalculator.DistributionConfig({
            memberShareBps: 10000,
            custodianShareBps: 0,
            memberMode: FeeDistributionCalculator.DistributionMode.Equal,
            custodianMode: FeeDistributionCalculator.DistributionMode.Equal
        });
        address[] memory wallets = new address[](1);
        wallets[0] = address(0x5001);
        uint256[] memory shares = new uint256[](1);
        shares[0] = 10000;
        string[] memory labels = new string[](1);
        labels[0] = "Project";

        (address feeManager, address feeDistributor, address nftModifier, address walletRegistry) = deployer
            .deployFeeSystem(
            address(frost),
            address(0),
            address(0x5555),
            feeConfig,
            feeConfig,
            distConfig,
            wallets,
            shares,
            labels,
            "Discount",
            "DISC"
        );

        assertGt(feeManager.code.length, 0, "Fee manager not deployed");
        assertGt(feeDistributor.code.length, 0, "Fee distributor not deployed");
        assertGt(nftModifier.code.length, 0, "NFT modifier not deployed");
        assertGt(walletRegistry.code.length, 0, "Wallet registry not deployed");

        FederationNFTModifier nft = FederationNFTModifier(nftModifier);
        assertTrue(nft.hasRole(nft.DEFAULT_ADMIN_ROLE(), feeManager), "Typed manager must be NFT admin");
        assertTrue(nft.hasRole(nft.MINTER_ROLE(), feeManager), "Typed manager must be NFT minter");
        assertFalse(nft.hasRole(nft.MINTER_ROLE(), address(frost)), "Authorization is not a call executor");
        assertTrue(nft.hasRole(nft.USAGE_ROLE(), feeManager), "Fee manager must consume NFT discounts");
        assertFalse(nft.hasRole(nft.DEFAULT_ADMIN_ROLE(), address(deployer)), "Orchestrator admin must be revoked");

        // The handoff must provide an executable typed mint, not merely roles.
        FederationFeeManager manager = FederationFeeManager(feeManager);
        bytes32 bind = manager.getSetNFTModifierOperationHash(nftModifier);
        _authorize(frost, bind, feeManager);
        manager.setNFTModifier(nftModifier, bind);
        bytes32 mint = manager.getMintNFTOperationHash(nftModifier, member1, block.timestamp + 1 days, 1000 ether, 2500);
        _authorize(frost, mint, feeManager);
        manager.mintNFT(nftModifier, member1, block.timestamp + 1 days, 1000 ether, 2500, mint);
        assertEq(nft.ownerOf(0), member1, "Managed mint must reach the collection");
    }

    function test_FeeSystemLeafDeployers_RejectUnauthorizedCallers() public {
        FederationFeeManagerDeployer feeManagerDeployer = new FederationFeeManagerDeployer();
        FeeDistributionCalculatorDeployer feeDistributorDeployer = new FeeDistributionCalculatorDeployer();
        FederationNFTModifierDeployer nftModifierDeployer = new FederationNFTModifierDeployer();
        ProjectWalletRegistryDeployer walletRegistryDeployer = new ProjectWalletRegistryDeployer();
        FROSTAuthorization frost = _newAuthorization();
        IFeeManager.FeeConfig memory feeConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 100,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });
        FeeDistributionCalculator.DistributionConfig memory distConfig = FeeDistributionCalculator.DistributionConfig({
            memberShareBps: 10000,
            custodianShareBps: 0,
            memberMode: FeeDistributionCalculator.DistributionMode.Equal,
            custodianMode: FeeDistributionCalculator.DistributionMode.Equal
        });
        address[] memory wallets = new address[](0);
        uint256[] memory shares = new uint256[](0);
        string[] memory labels = new string[](0);
        bytes memory unauthorizedError =
            abi.encodeWithSelector(ControlledDeployer.UnauthorizedDeployer.selector, unauthorized);

        vm.startPrank(unauthorized);
        vm.expectRevert(unauthorizedError);
        feeManagerDeployer.deployFederationFeeManager(address(frost), address(0), address(0x5555), feeConfig, feeConfig);
        vm.expectRevert(unauthorizedError);
        feeDistributorDeployer.deployFeeDistributionCalculator(address(frost), distConfig);
        vm.expectRevert(unauthorizedError);
        nftModifierDeployer.deployFederationNFTModifier("Discount", "DISC", address(this));
        vm.expectRevert(unauthorizedError);
        walletRegistryDeployer.deployProjectWalletRegistry(address(frost), wallets, shares, labels);
        vm.stopPrank();
    }

    /// @notice Test 33: FeeManager fixed percentage calculation
    function test_FeeManager_FixedPercentage() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        IFeeManager.FeeConfig memory wrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 200, // 2%
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });
        IFeeManager.FeeConfig memory unwrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 100, // 1%
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        FederationFeeManager feeManager =
            new FederationFeeManager(address(frost), address(0), address(0x5555), wrapConfig, unwrapConfig);

        // 2% of 10000 = 200
        assertEq(feeManager.calculateFee(true, 10000), 200, "Wrap fee should be 2%");
        // 1% of 10000 = 100
        assertEq(feeManager.calculateFee(false, 10000), 100, "Unwrap fee should be 1%");
    }

    /// @notice Test 34: FeeManager dynamic fee calculation
    function test_FeeManager_DynamicFee() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        IFeeManager.FeeConfig memory wrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.Dynamic,
            fixedBps: 0,
            dynamicMinFee: 50, // Minimum fee of 50
            dynamicCoeffBps: 100 // 1% coefficient
        });
        IFeeManager.FeeConfig memory unwrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 0,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        FederationFeeManager feeManager =
            new FederationFeeManager(address(frost), address(0), address(0x5555), wrapConfig, unwrapConfig);

        // Dynamic: minFee(50) + 10000 * 100 / 10000 = 50 + 100 = 150
        assertEq(feeManager.calculateFee(true, 10000), 150, "Dynamic fee should be minFee + coeff%");
    }

    /// @notice Test 35: FeeManager rejects invalid bps and caps dynamic fees at amount
    function test_FeeManager_ValidatesFeeConfig() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        IFeeManager.FeeConfig memory validConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 100,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });
        IFeeManager.FeeConfig memory invalidConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 10001,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        vm.expectRevert(FederationFeeManager.InvalidFeeConfig.selector);
        new FederationFeeManager(address(frost), address(0), address(0x5555), invalidConfig, validConfig);

        IFeeManager.FeeConfig memory cappedDynamic = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.Dynamic,
            fixedBps: 0,
            dynamicMinFee: 2000,
            dynamicCoeffBps: 10000
        });
        FederationFeeManager feeManager =
            new FederationFeeManager(address(frost), address(0), address(0x5555), cappedDynamic, validConfig);

        assertEq(feeManager.calculateFee(true, 1000), 1000, "Fee should not exceed amount");
    }

    /// @notice Test 36: FeeManager fee accounting can only be recorded by authorized collectors
    function test_FeeManager_RecordFeeRequiresAuthorizedCollector() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        IFeeManager.FeeConfig memory config = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 100,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        FederationFeeManager feeManager =
            new FederationFeeManager(address(frost), address(0), address(0x5555), config, config);

        address collector = address(0x8888);

        vm.prank(collector);
        vm.expectRevert(FederationFeeManager.UnauthorizedFeeCollector.selector);
        feeManager.recordFeeCollected(100, true);

        bytes32 opHash = feeManager.getSetFeeCollectorOperationHash(collector, true);
        _authorize(frost, opHash, address(feeManager));

        feeManager.setFeeCollector(collector, true, opHash);

        vm.prank(collector);
        feeManager.recordFeeCollected(100, true);
        assertEq(feeManager.totalCollected(), 100, "Authorized collector should update total");
    }

    /// @notice Test 35: FeeManager discount calculation
    function test_FeeManager_Discount() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        IFeeManager.FeeConfig memory config = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 1000, // 10%
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        FederationFeeManager feeManager =
            new FederationFeeManager(address(frost), address(0), address(0x5555), config, config);

        // 10% of 10000 = 1000, with 50% discount = 500
        assertEq(feeManager.calculateFeeWithDiscount(true, 10000, 5000), 500, "50% discount on 10% fee");
        // Full discount (100%) = 0
        assertEq(feeManager.calculateFeeWithDiscount(true, 10000, 10000), 0, "100% discount = no fee");
        // No discount = full fee
        assertEq(feeManager.calculateFeeWithDiscount(true, 10000, 0), 1000, "0% discount = full fee");
    }

    /// @notice FeeManager consumes NFT discounts only for the NFT owner
    function test_FeeManager_NFTDiscountRequiresOwner() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        IFeeManager.FeeConfig memory config = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 1000,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        FederationFeeManager feeManager =
            new FederationFeeManager(address(frost), address(0), address(0x5555), config, config);
        FederationNFTModifier nft = new FederationNFTModifier("Fee NFT", "FNFT", address(this));

        nft.grantRole(nft.USAGE_ROLE(), address(feeManager));

        bytes32 opHash = feeManager.getSetNFTModifierOperationHash(address(nft));
        _authorize(frost, opHash, address(feeManager));
        feeManager.setNFTModifier(address(nft), opHash);

        uint256 tokenId = nft.mint(member1, block.timestamp + 1 days, 20000, 5000);

        bytes32 collectorOp = feeManager.getSetFeeCollectorOperationHash(address(this), true);
        _authorize(frost, collectorOp, address(feeManager));
        feeManager.setFeeCollector(address(this), true, collectorOp);

        assertEq(feeManager.calculateFeeWithNFT(true, 10000, tokenId, member1), 500, "Owner gets 50% fee discount");

        vm.expectRevert(FederationNFTModifier.NotTokenOwner.selector);
        feeManager.calculateFeeWithNFT(true, 1000, tokenId, member2);
    }

    // ======================================================================
    // FEE DISTRIBUTION CALCULATOR TESTS (2)
    // ======================================================================

    /// @notice Test 36: FeeDistributionCalculator equal split
    function test_FeeDistribution_EqualSplit() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        FeeDistributionCalculator.DistributionConfig memory distConfig = FeeDistributionCalculator.DistributionConfig({
            memberShareBps: 7000, // 70%
            custodianShareBps: 3000, // 30%
            memberMode: FeeDistributionCalculator.DistributionMode.Equal,
            custodianMode: FeeDistributionCalculator.DistributionMode.Equal
        });

        FeeDistributionCalculator calc = new FeeDistributionCalculator(address(frost), distConfig);

        FeeDistributionCalculator.Recipient[] memory members = new FeeDistributionCalculator.Recipient[](2);
        members[0] = FeeDistributionCalculator.Recipient({addr: member1, weight: 0});
        members[1] = FeeDistributionCalculator.Recipient({addr: member2, weight: 0});

        FeeDistributionCalculator.Recipient[] memory custodians = new FeeDistributionCalculator.Recipient[](2);
        custodians[0] = FeeDistributionCalculator.Recipient({addr: custodian1, weight: 0});
        custodians[1] = FeeDistributionCalculator.Recipient({addr: custodian2, weight: 0});

        (FeeDistributionCalculator.Result[] memory results, uint256 distributed, uint256 remainder) =
            calc.calculateDistribution(10000, members, custodians);

        assertEq(results.length, 4, "Should have 4 recipients");
        // 70% of 10000 = 7000, split equally = 3500 each
        assertEq(results[0].amount, 3500, "Member1 should get 3500");
        assertEq(results[1].amount, 3500, "Member2 should get 3500");
        // 30% of 10000 = 3000, split equally = 1500 each
        assertEq(results[2].amount, 1500, "Custodian1 should get 1500");
        assertEq(results[3].amount, 1500, "Custodian2 should get 1500");
        assertEq(distributed, 10000, "Should distribute full amount");
        assertEq(remainder, 0, "No remainder");
    }

    /// @notice Test 37: FeeDistributionCalculator percentage-based split
    function test_FeeDistribution_PercentageSplit() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        FeeDistributionCalculator.DistributionConfig memory distConfig = FeeDistributionCalculator.DistributionConfig({
            memberShareBps: 10000, // 100% to members
            custodianShareBps: 0,
            memberMode: FeeDistributionCalculator.DistributionMode.ByPercentage,
            custodianMode: FeeDistributionCalculator.DistributionMode.Equal
        });

        FeeDistributionCalculator calc = new FeeDistributionCalculator(address(frost), distConfig);

        FeeDistributionCalculator.Recipient[] memory members = new FeeDistributionCalculator.Recipient[](3);
        members[0] = FeeDistributionCalculator.Recipient({addr: member1, weight: 50}); // 50%
        members[1] = FeeDistributionCalculator.Recipient({addr: member2, weight: 30}); // 30%
        members[2] = FeeDistributionCalculator.Recipient({addr: member3, weight: 20}); // 20%

        FeeDistributionCalculator.Recipient[] memory custodians = new FeeDistributionCalculator.Recipient[](0);

        (FeeDistributionCalculator.Result[] memory results, uint256 distributed,) =
            calc.calculateDistribution(10000, members, custodians);

        assertEq(results.length, 3, "Should have 3 recipients");
        assertEq(results[0].amount, 5000, "Member1 should get 50%");
        assertEq(results[1].amount, 3000, "Member2 should get 30%");
        assertEq(results[2].amount, 2000, "Member3 should get 20%");
        assertEq(distributed, 10000, "Full distribution");
    }

    // ======================================================================
    // NFT MODIFIER TESTS (3)
    // ======================================================================

    /// @notice Test 38: NFT mint and get discount
    function test_NFT_MintAndGetDiscount() public {
        FederationNFTModifier nft = new FederationNFTModifier("Fee NFT", "FNFT", address(this));

        uint256 tokenId = nft.mint(member1, block.timestamp + 1 days, 100000, 5000); // 50% discount, 100k volume limit

        assertEq(nft.ownerOf(tokenId), member1, "Member1 should own NFT");

        (uint256 discountBps, uint256 remainingVolume, bool isValid) = nft.getDiscount(tokenId);
        assertEq(discountBps, 5000, "Discount should be 50%");
        assertEq(remainingVolume, 100000, "Full volume remaining");
        assertTrue(isValid, "NFT should be valid");
    }

    /// @notice Test 39: NFT use discount tracks volume
    function test_NFT_UseDiscountTracksVolume() public {
        FederationNFTModifier nft = new FederationNFTModifier("Fee NFT", "FNFT", address(this));
        nft.grantRole(nft.USAGE_ROLE(), address(this));

        uint256 tokenId = nft.mint(member1, block.timestamp + 1 days, 1000, 3000); // 30% discount, 1000 volume

        // Use 600 volume through an authorized fee manager path
        uint256 discount = nft.useDiscountFor(tokenId, member1, 600);
        assertEq(discount, 3000, "Should return 30% discount");

        (, uint256 remaining,) = nft.getDiscount(tokenId);
        assertEq(remaining, 400, "Should have 400 remaining");

        // Try to exceed volume limit
        vm.expectRevert(FederationNFTModifier.VolumeLimitExceeded.selector);
        nft.useDiscountFor(tokenId, member1, 500);

        vm.expectRevert(FederationNFTModifier.NotTokenOwner.selector);
        nft.useDiscountFor(tokenId, member2, 100);
    }

    /// @notice Test 40: NFT burn expired
    function test_NFT_BurnExpired() public {
        FederationNFTModifier nft = new FederationNFTModifier("Fee NFT", "FNFT", address(this));

        uint256 tokenId = nft.mint(member1, block.timestamp + 1 hours, 100000, 5000);

        // Cannot burn before expiration
        vm.expectRevert("NFT not expired");
        nft.burnExpired(tokenId);

        // Fast forward past expiration
        vm.warp(block.timestamp + 2 hours);

        // Now anyone can burn
        vm.prank(unauthorized);
        nft.burnExpired(tokenId);

        // Token no longer exists
        vm.expectRevert();
        nft.ownerOf(tokenId);
    }

    // ======================================================================
    // PROJECT WALLET REGISTRY TESTS (2)
    // ======================================================================

    /// @notice Test 41: ProjectWalletRegistry add and query wallets
    function test_ProjectWallet_AddAndQuery() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        address[] memory initialWallets = new address[](1);
        initialWallets[0] = address(0x7001);
        uint256[] memory initialShares = new uint256[](1);
        initialShares[0] = 5000; // 50%
        string[] memory initialLabels = new string[](1);
        initialLabels[0] = "Treasury";

        ProjectWalletRegistry registry =
            new ProjectWalletRegistry(address(frost), initialWallets, initialShares, initialLabels);

        assertEq(registry.getActiveWalletCount(), 1, "Should have 1 wallet");
        assertTrue(registry.isWalletActive(address(0x7001)), "Treasury should be active");

        // Add another wallet with FROST approval
        bytes32 opHash = registry.getAddWalletOperationHash(address(0x7002), 3000, "Dev Fund");
        _authorize(frost, opHash, address(registry));

        registry.addWallet(address(0x7002), 3000, "Dev Fund", opHash);

        assertEq(registry.getActiveWalletCount(), 2, "Should have 2 wallets");
        assertTrue(registry.isWalletActive(address(0x7002)), "Dev Fund should be active");
    }

    /// @notice Test 42: ProjectWalletRegistry remove wallet
    function test_ProjectWallet_RemoveWallet() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        address[] memory initialWallets = new address[](2);
        initialWallets[0] = address(0x7001);
        initialWallets[1] = address(0x7002);
        uint256[] memory initialShares = new uint256[](2);
        initialShares[0] = 5000;
        initialShares[1] = 3000;
        string[] memory initialLabels = new string[](2);
        initialLabels[0] = "Treasury";
        initialLabels[1] = "Dev Fund";

        ProjectWalletRegistry registry =
            new ProjectWalletRegistry(address(frost), initialWallets, initialShares, initialLabels);

        // Remove wallet with FROST approval
        bytes32 opHash = registry.getRemoveWalletOperationHash(address(0x7001));
        _authorize(frost, opHash, address(registry));

        registry.removeWallet(address(0x7001), opHash);

        assertEq(registry.getActiveWalletCount(), 1, "Should have 1 active wallet");
        assertFalse(registry.isWalletActive(address(0x7001)), "Treasury should be inactive");
        assertTrue(registry.isWalletActive(address(0x7002)), "Dev Fund should still be active");
    }

    /// @notice Test 43: ProjectWalletRegistry validates duplicate wallets and total shares
    function test_ProjectWallet_ValidatesSharesAndDuplicates() public {
        address[] memory signers = _getMembers();
        FROSTAuthorization frost = _newAuthorization(signers, 2);

        address[] memory duplicateWallets = new address[](2);
        duplicateWallets[0] = address(0x7001);
        duplicateWallets[1] = address(0x7001);
        uint256[] memory duplicateShares = new uint256[](2);
        duplicateShares[0] = 4000;
        duplicateShares[1] = 4000;
        string[] memory duplicateLabels = new string[](2);
        duplicateLabels[0] = "Treasury";
        duplicateLabels[1] = "Treasury Again";

        vm.expectRevert(ProjectWalletRegistry.WalletAlreadyRegistered.selector);
        new ProjectWalletRegistry(address(frost), duplicateWallets, duplicateShares, duplicateLabels);

        address[] memory initialWallets = new address[](1);
        initialWallets[0] = address(0x7001);
        uint256[] memory initialShares = new uint256[](1);
        initialShares[0] = 9000;
        string[] memory initialLabels = new string[](1);
        initialLabels[0] = "Treasury";

        ProjectWalletRegistry registry =
            new ProjectWalletRegistry(address(frost), initialWallets, initialShares, initialLabels);

        bytes32 opHash = registry.getAddWalletOperationHash(address(0x7002), 2000, "Dev Fund");
        vm.expectRevert(ProjectWalletRegistry.TotalSharesExceed100Percent.selector);
        registry.addWallet(address(0x7002), 2000, "Dev Fund", opHash);

        address[] memory oversizedWallets = new address[](2);
        oversizedWallets[0] = address(0x7003);
        oversizedWallets[1] = address(0x7004);
        uint256[] memory oversizedShares = new uint256[](2);
        oversizedShares[0] = 6000;
        oversizedShares[1] = 5000;
        string[] memory oversizedLabels = new string[](2);
        oversizedLabels[0] = "A";
        oversizedLabels[1] = "B";

        vm.expectRevert(ProjectWalletRegistry.TotalSharesExceed100Percent.selector);
        registry.batchUpdateWallets(oversizedWallets, oversizedShares, oversizedLabels, bytes32(0));
    }

    /// @notice Test 44: GlobalProjectWalletRegistry fee accounting requires registered fee manager
    function test_GlobalProjectWallet_RecordFeeRequiresRegisteredFeeManager() public {
        address[] memory wallets = new address[](0);
        uint256[] memory shares = new uint256[](0);
        string[] memory labels = new string[](0);

        GlobalProjectWalletRegistry registry = new GlobalProjectWalletRegistry(address(this), wallets, shares, labels);

        address feeManager = address(0x9001);

        vm.prank(feeManager);
        vm.expectRevert(GlobalProjectWalletRegistry.UnauthorizedFeeManager.selector);
        registry.recordFeeReceived(100);

        registry.registerFederation(feeManager, "fed-1");

        vm.prank(feeManager);
        registry.recordFeeReceived(100);
        assertEq(registry.totalReceived(), 100, "Registered fee manager should record fee");

        vm.deal(feeManager, 1 ether);
        vm.prank(feeManager);
        (bool success,) = address(registry).call{value: 1 ether}("");
        assertTrue(success, "Registered fee manager should be able to send native fees");
        assertEq(registry.totalReceived(), 1 ether + 100, "Native fee receipt should be tracked");
    }

    // ======================================================================
    // CUSTOM SPV DEPTH TESTS (2)
    // ======================================================================

    /// @notice Test 43: SPV isMatureWithDepth uses custom depth
    function test_SPV_CustomDepth() public {
        // isMatureWithDepth(bytes32(0), 0) should return false for non-existent block
        assertFalse(spv.isMatureWithDepth(bytes32(0), 0), "Non-existent block not mature");
        assertFalse(spv.isMatureWithDepth(bytes32(0), 3), "Non-existent block not mature with custom depth");
    }

    /// @notice Test 44: SimpleMiningPool stores custom confirmation depth
    function test_MiningPool_CustomConfirmationDepth() public {
        SimpleMiningPool poolDefault = new SimpleMiningPool(address(spv), "DEFAULT", hex"76a914abcdef88ac", 0);
        assertEq(poolDefault.confirmationDepth(), 0, "Default depth should be 0");

        SimpleMiningPool poolCustom = new SimpleMiningPool(address(spv), "CUSTOM", hex"76a914abcdef88ac", 3);
        assertEq(poolCustom.confirmationDepth(), 3, "Custom depth should be 3");
    }

    /// @notice Test 45: Rejected mining pool redemption refunds escrowed MP tokens
    function test_MiningPool_RejectedRedemptionRefundsEscrow() public {
        (PoolMpToken mpToken, SimpleMiningPool miningPool) = _setupPoolWithMpToken();
        uint64 amountSat = 100;
        uint256 tokenAmount = uint256(amountSat) * miningPool.SAT_TO_TOKEN_MULTIPLIER();

        mpToken.mint(member1, tokenAmount);

        vm.startPrank(member1);
        mpToken.approve(address(miningPool), tokenAmount);
        uint256 redemptionId = miningPool.requestRedemption(amountSat, hex"0014abcdef");
        vm.stopPrank();

        assertEq(mpToken.balanceOf(member1), 0, "Requester tokens should be escrowed");
        assertEq(mpToken.balanceOf(address(miningPool)), tokenAmount, "Pool should hold escrow");

        miningPool.confirmRedemption(redemptionId, false);

        assertTrue(miningPool.redemptionRefunded(redemptionId), "Rejected redemption should be refunded");
        assertFalse(miningPool.redemptionApproved(redemptionId), "Rejected redemption should not be approved");
        assertEq(mpToken.balanceOf(member1), tokenAmount, "Requester should receive refund");
        assertEq(mpToken.balanceOf(address(miningPool)), 0, "Escrow should be empty");
    }

    /// @notice Test 46: Approved mining pool redemption burns escrowed MP tokens
    function test_MiningPool_ApprovedRedemptionBurnsEscrow() public {
        (PoolMpToken mpToken, SimpleMiningPool miningPool) = _setupPoolWithMpToken();
        uint64 amountSat = 100;
        uint256 tokenAmount = uint256(amountSat) * miningPool.SAT_TO_TOKEN_MULTIPLIER();

        mpToken.mint(member1, tokenAmount);

        vm.startPrank(member1);
        mpToken.approve(address(miningPool), tokenAmount);
        uint256 redemptionId = miningPool.requestRedemption(amountSat, hex"0014abcdef");
        vm.stopPrank();

        assertEq(mpToken.totalSupply(), tokenAmount, "Escrow should not burn at request time");

        miningPool.confirmRedemption(redemptionId, true);

        assertTrue(miningPool.redemptionApproved(redemptionId), "Redemption should be approved");
        assertFalse(miningPool.redemptionRefunded(redemptionId), "Approved redemption should not be refunded");
        assertEq(mpToken.balanceOf(address(miningPool)), 0, "Escrow should be burned");
        assertEq(mpToken.totalSupply(), 0, "Supply should be burned at final approval");
    }

    /// @notice Bridge deposits cannot be overwritten before processing
    function test_MiningPoolWithBridge_DepositCannotBeOverwritten() public {
        SimpleMiningPoolWithBridge bridgePool =
            new SimpleMiningPoolWithBridge(address(spv), "BRIDGE-POOL", hex"76a914abcdef88ac", 1);
        spv.setPoolContract(address(bridgePool));

        PoolMpToken mpToken = new PoolMpToken("Bridge MP", "bmpBTC", address(bridgePool), false);
        bridgePool.setMpToken(address(mpToken));

        bytes32 txid = keccak256("bridge-tx");
        bytes32 blockHash = keccak256("bridge-block");
        bytes32 headHash = keccak256("bridge-head");
        bytes32[] memory siblings = new bytes32[](0);
        bytes memory opReturnData = bytes.concat(hex"6a2c", bytes("ETH:0000000000000000000000000000000000001001"));

        spv.addBlockWithHash(blockHash, 1);
        spv.setBlockMerkleRoot(blockHash, txid);
        spv.addBlockWithHash(headHash, 2);

        bytes32 depositKey = bridgePool.registerBridgeDeposit(txid, 0, 100, blockHash, siblings, 0, opReturnData);

        vm.expectRevert("Already registered");
        bridgePool.registerBridgeDeposit(txid, 0, 200, blockHash, siblings, 0, opReturnData);

        bridgePool.processBridgeDeposit(depositKey);
        assertEq(
            mpToken.balanceOf(member1),
            100 * bridgePool.SAT_TO_TOKEN_MULTIPLIER(),
            "Bridge recipient should receive original amount"
        );

        vm.expectRevert("Already processed");
        bridgePool.processBridgeDeposit(depositKey);

        vm.expectRevert("Not registered");
        bridgePool.processBridgeDeposit(keccak256("missing-bridge-deposit"));
    }

    /// @notice Test 45: SPV pool registration is admin-only
    function test_SPV_AdminProtectsPoolRegistration() public {
        address poolAddress = address(0x123456);

        vm.expectRevert(SimpleSPVContract.OnlySPVAdmin.selector);
        vm.prank(unauthorized);
        spv.setPoolContract(poolAddress);

        spv.setPoolContract(poolAddress);
        assertTrue(spv.poolContracts(poolAddress), "Pool should be registered by admin");
    }

    /// @notice Test 46: SPV test helpers are admin-only
    function test_SPV_AdminProtectsTestHelpers() public {
        bytes32 blockHash = keccak256("test-block");
        bytes32 merkleRoot = keccak256("test-root");

        vm.expectRevert(SimpleSPVContract.OnlySPVAdmin.selector);
        vm.prank(unauthorized);
        spv.addBlockWithHash(blockHash, 1);

        spv.addBlockWithHash(blockHash, 1);
        assertTrue(spv.blockExists(blockHash), "Admin should be able to add helper block");

        vm.expectRevert(SimpleSPVContract.OnlySPVAdmin.selector);
        vm.prank(unauthorized);
        spv.setBlockMerkleRoot(blockHash, merkleRoot);

        spv.setBlockMerkleRoot(blockHash, merkleRoot);
        assertEq(spv.getBlockMerkleRoot(blockHash), merkleRoot, "Admin should be able to set helper merkle root");
    }

    /// @notice SPV demo/test helpers can be permanently disabled for production-like deployments
    function test_SPV_CanPermanentlyDisableTestHelpers() public {
        spv.disableTestHelpers();
        assertFalse(spv.testHelpersEnabled(), "Test helpers should be disabled");

        vm.expectRevert(SimpleSPVContract.SPVTestHelpersDisabled.selector);
        spv.addBlockWithHash(keccak256("disabled-block"), 1);

        vm.expectRevert(SimpleSPVContract.SPVTestHelpersDisabled.selector);
        spv.setBlockMerkleRoot(keccak256("disabled-block"), keccak256("root"));
    }

    /// @notice Calculator pool and its stateless BTC helper remain deployable under EIP-170
    function test_CalculatorPool_RuntimeAndHelperStayBelowEIP170() public {
        SimpleMiningPoolWithCalculator pool =
            new SimpleMiningPoolWithCalculator(address(spv), "SIZE-POOL", hex"76a914abcdef88ac", 0);
        address helper = address(pool.redemptionTxBuilder());

        assertGt(address(pool).code.length, 0, "Pool runtime should exist");
        assertLt(address(pool).code.length, 24_576, "Pool runtime exceeds EIP-170");
        assertGt(helper.code.length, 0, "Helper runtime should exist");
        assertLt(helper.code.length, 24_576, "Helper runtime exceeds EIP-170");
    }

    /// @notice BTC builder enforces RBF and produces ANYONECANPAY sighashes
    function test_BitcoinRedemptionTxBuilder_ConstructsRbfAnyoneCanPayTx() public {
        BitcoinRedemptionTxBuilder builder = new BitcoinRedemptionTxBuilder();
        BTCScriptConstructor.UTXOInput[] memory inputs = new BTCScriptConstructor.UTXOInput[](1);
        inputs[0] = BTCScriptConstructor.UTXOInput({
            txid: bytes32(uint256(1)),
            vout: 0,
            amount: 2_500,
            scriptPubKey: hex"00140000000000000000000000000000000000000000",
            scriptCode: hex"76a914000000000000000000000000000000000000000088ac",
            sequence: 0xfffffffd
        });

        BTCScriptConstructor.TxOutput[] memory outputs = new BTCScriptConstructor.TxOutput[](1);
        outputs[0] = BTCScriptConstructor.TxOutput({
            amount: 1_000,
            scriptPubKey: hex"00141111111111111111111111111111111111111111"
        });

        (BTCScriptConstructor.UnsignedTx memory unsignedTx, bytes memory encodedTx) =
            builder.constructRedemptionTx(inputs, outputs, bytes(""), 500);

        assertEq(unsignedTx.version, 2, "Unexpected BTC transaction version");
        assertEq(unsignedTx.inputs.length, 1, "Unexpected input count");
        assertEq(unsignedTx.outputs.length, 1, "Unexpected output count");
        assertEq(unsignedTx.sighashes.length, 1, "Unexpected sighash count");
        assertEq(unsignedTx.sighashType, 0x81, "Expected SIGHASH_ALL | SIGHASH_ANYONECANPAY");
        assertGt(encodedTx.length, 0, "Encoded transaction should not be empty");

        inputs[0].sequence = 0xfffffffe;
        vm.expectRevert("RBF disabled");
        builder.constructRedemptionTx(inputs, outputs, bytes(""), 500);
    }

    // ======================================================================
    // INTEGRATION: DEPOSIT WITH FEE MANAGER (1)
    // ======================================================================

    /// @notice Test 47: MultiPool deposit deducts fee when feeManager is set
    function test_Integration_DepositWithFee() public {
        (,, address poolAddr) = _createFederation("fee-fed");
        SimpleMultiPool multiPool = SimpleMultiPool(poolAddr);
        FROSTAuthorization frost = FROSTAuthorization(multiPool.getFrostMultisig());

        // Deploy fee manager with 5% wrap fee
        IFeeManager.FeeConfig memory wrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 500, // 5%
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });
        IFeeManager.FeeConfig memory unwrapConfig = IFeeManager.FeeConfig({
            feeType: IFeeManager.FeeType.FixedPercentage,
            fixedBps: 0,
            dynamicMinFee: 0,
            dynamicCoeffBps: 0
        });

        FederationFeeManager feeManagerContract =
            new FederationFeeManager(address(frost), address(0), address(0x5555), wrapConfig, unwrapConfig);

        // Set fee manager on pool
        _authorizeFeeCollector(frost, feeManagerContract, address(multiPool));
        bytes32 setFeeHash = multiPool.getSetFeeManagerOperationHash(address(feeManagerContract));
        _authorize(frost, setFeeHash, address(multiPool));
        multiPool.setFeeManager(address(feeManagerContract), setFeeHash);

        // Setup mining pool + MP token
        (PoolMpToken mpToken,) = _setupPoolWithMpToken();

        bytes32 addPoolHash = multiPool.getAddPoolOperationHash(address(mpToken), "FEE-POOL");
        _authorize(frost, addPoolHash, address(multiPool));
        multiPool.addPool(address(mpToken), "FEE-POOL", addPoolHash);

        // Deposit 1000 MP tokens
        mpToken.mint(member1, 1000);

        vm.startPrank(member1);
        mpToken.approve(address(multiPool), 1000);
        multiPool.deposit(address(mpToken), 1000);
        vm.stopPrank();

        // Should receive 950 share tokens (1000 - 5% fee = 950)
        assertEq(multiPool.shareToken().balanceOf(member1), 950, "Should receive 950 after 5% fee");
        assertEq(multiPool.accruedFees(address(mpToken), true), 50);
        assertEq(feeManagerContract.collectedFeesByToken(address(mpToken), true), 50);
        assertEq(feeManagerContract.totalCollected(), 50);
    }

    function _authorizeFeeCollector(FROSTAuthorization frost, FederationFeeManager manager, address collector) internal {
        bytes32 hash = manager.getSetFeeCollectorOperationHash(collector, true);
        _authorize(frost, hash, address(manager));
        if (address(manager.custodianValidator()) != address(0)) {
            _approveCustodians(manager.custodianValidator(), hash);
        }
        manager.setFeeCollector(collector, true, hash);
    }

    function _approveCustodians(CustodianValidator validator, bytes32 hash) internal {
        address[] memory signers = validator.getCustodians();
        for (uint256 i; i < signers.length; i++) {
            vm.prank(signers[i]);
            validator.registerCustodianSignature(hash);
        }
    }

    function _readinessConfederation() internal returns (ConfederationMultiPool pool, FROSTAuthorization frost) {
        _createFederation("readiness-fed");
        string[] memory ids = new string[](1);
        ids[0] = "readiness-fed";
        IConfederationFactory.CustodianMember[] memory custodians = new IConfederationFactory.CustodianMember[](2);
        custodians[0] = IConfederationFactory.CustodianMember("c1", custodian1, hex"02", true);
        custodians[1] = IConfederationFactory.CustodianMember("c2", custodian2, hex"02", true);
        bytes32 session = confFactory.initiateConfederation(ids, custodians, 3, 2, "readiness-conf");
        _completeDKG(confDKG, session);
        (address auth,, address poolAddr,) = confFactory.deployConfederation(session, "Conf Share", "CS");
        pool = ConfederationMultiPool(poolAddr);
        frost = FROSTAuthorization(auth);
        UnitEscrowFixture escrow = new UnitEscrowFixture(auth);
        bytes32 setup = pool.getSetWithdrawalEscrowOperationHash(address(escrow));
        _authorize(frost,setup,address(pool)); _approveCustodians(pool.custodianValidator(),setup);
        pool.setWithdrawalEscrow(address(escrow),setup);
    }

    function test_DKG_PublicSharesUseValidatedSEC1AndExposeCoordinates() public {
        bytes32 session = dkgCoordinator.initiateDKG(_getMembers(), 2, "pk-format");
        _advanceDKGToPublicKeyPhase(dkgCoordinator, session, 0);
        uint256 oddY = Secp256k1.P - Secp256k1.GY;
        vm.prank(member1);
        dkgCoordinator.submitPublicKeyShare(session, abi.encodePacked(bytes1(0x03), bytes32(Secp256k1.GX)));
        (uint256 x, uint256 y) = dkgCoordinator.getPublicKeySharePoint(session, member1);
        assertEq(x, Secp256k1.GX);
        assertEq(y, oddY);
        vm.prank(member2);
        dkgCoordinator.submitPublicKeyShare(session, abi.encodePacked(bytes1(0x04), bytes32(x), bytes32(y)));
        assertEq(dkgCoordinator.getPublicKeyShare(session, member1), dkgCoordinator.getPublicKeyShare(session, member2));
    }

    function test_DKG_RejectsOpaqueOffCurveAndNoncanonicalPublicShares() public {
        bytes32 session = dkgCoordinator.initiateDKG(_getMembers(), 2, "pk-invalid");
        _advanceDKGToPublicKeyPhase(dkgCoordinator, session, 0);
        vm.startPrank(member1);
        vm.expectRevert(DKGCoordinator.InvalidPublicKeyShare.selector);
        dkgCoordinator.submitPublicKeyShare(session, bytes("arbitrary bytes"));
        vm.expectRevert(DKGCoordinator.InvalidPublicKeyShare.selector);
        dkgCoordinator.submitPublicKeyShare(session, abi.encodePacked(bytes1(0x04), bytes32(Secp256k1.GX), bytes32(uint256(1))));
        vm.expectRevert(DKGCoordinator.InvalidPublicKeyShare.selector);
        dkgCoordinator.submitPublicKeyShare(session, abi.encodePacked(bytes1(0x02), bytes32(Secp256k1.P)));
        vm.stopPrank();
        vm.expectRevert(DKGCoordinator.EmptyPublicKey.selector);
        dkgCoordinator.getPublicKeySharePoint(session, member1);
    }

    function test_DKG_CompressedGroupKeyIsStoredAsFactoryCompatibleXOnly() public {
        bytes32 session = fedFactory.initiateFederationDKG(_getMembers(), 2, "compressed-group");
        _advanceDKGToPublicKeyPhase(dkgCoordinator, session, 3);
        bytes memory key = abi.encodePacked(bytes1(0x02), bytes32(Secp256k1.GX));
        vm.prank(member1);
        dkgCoordinator.approveGroupPublicKey(session, key);
        vm.prank(member2);
        dkgCoordinator.approveGroupPublicKey(session, key);
        dkgCoordinator.completeDKG(session, key);
        assertEq(dkgCoordinator.getGroupPublicKey(session), abi.encodePacked(bytes32(Secp256k1.GX)));
        (address auth,,) = fedFactory.deployFederation(session, "S", "S");
        assertEq(FROSTAuthorization(auth).groupPubkey(), abi.encodePacked(bytes32(Secp256k1.GX)));
    }

    function test_MultiPool_RetainsFullTaprootAndWitnessScriptsInSignedRequest() public {
        (,, address poolAddr) = _createFederation("script-fed");
        SimpleMultiPool pool = SimpleMultiPool(poolAddr);
        FROSTAuthorization auth = FROSTAuthorization(pool.getFrostMultisig());
        (PoolMpToken mp,) = _setupPoolWithMpToken();
        bytes32 addHash = pool.getAddPoolOperationHash(address(mp), "SCRIPT");
        _authorize(auth, addHash, address(pool));
        pool.addPool(address(mp), "SCRIPT", addHash);
        mp.mint(member1, 1000 * 1e10);
        vm.startPrank(member1);
        mp.approve(address(pool), 1000 * 1e10);
        pool.deposit(address(mp), 1000 * 1e10);
        bytes memory taproot = abi.encodePacked(hex"5120", bytes32(Secp256k1.GX));
        bytes memory witness = hex"00141111111111111111111111111111111111111111";
        uint256 a = pool.requestWithdrawal(address(mp), 100 * 1e10, taproot);
        uint256 b = pool.requestWithdrawalWithNFT(address(mp), 100 * 1e10, witness, 0);
        vm.expectRevert("Invalid BTC script");
        pool.requestWithdrawal(address(mp), 100 * 1e10, "");
        vm.expectRevert("Invalid BTC script");
        pool.requestWithdrawal(address(mp), 100 * 1e10, new bytes(10001));
        vm.stopPrank();
        assertEq(pool.getWithdrawal(a).btcScript, taproot);
        assertEq(pool.getWithdrawal(b).btcScript, witness);
        assertTrue(pool.getProcessWithdrawalOperationHash(a) != pool.getProcessWithdrawalOperationHash(b));
    }

    function test_MultiPool_FeesClaimOnlyAccruedTokensAndCancelledRequestAddsNoFee() public {
        (,, address poolAddr) = _createFederation("fee-flow");
        SimpleMultiPool pool = SimpleMultiPool(poolAddr);
        FROSTAuthorization auth = FROSTAuthorization(pool.getFrostMultisig());
        IFeeManager.FeeConfig memory config = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, 500, 0, 0);
        FederationFeeManager manager = new FederationFeeManager(address(auth), address(0), member3, config, config);
        bytes32 setup = pool.getSetFeeManagerOperationHash(address(manager));
        _authorize(auth, setup, address(pool));
        vm.expectRevert("Fee collector not authorized");
        pool.setFeeManager(address(manager), setup);
        _authorizeFeeCollector(auth, manager, address(pool));
        pool.setFeeManager(address(manager), setup);
        assertEq(fedFactory.getFederation("fee-flow").feeManager, address(manager));
        (PoolMpToken mp,) = _setupPoolWithMpToken();
        bytes32 addHash = pool.getAddPoolOperationHash(address(mp), "FEES");
        _authorize(auth, addHash, address(pool));
        pool.addPool(address(mp), "FEES", addHash);
        mp.mint(member1, 1000 * 1e10);
        vm.startPrank(member1);
        mp.approve(address(pool), 1000 * 1e10);
        pool.deposit(address(mp), 1000 * 1e10);
        uint256 cancelled = pool.requestWithdrawal(address(mp), 100 * 1e10, hex"00141111111111111111111111111111111111111111");
        vm.warp(block.timestamp + 7 days);
        pool.cancelWithdrawal(cancelled);
        uint256 paid = pool.requestWithdrawal(address(mp), 400 * 1e10, hex"00141111111111111111111111111111111111111111");
        vm.stopPrank();
        assertEq(pool.accruedFees(address(mp), false), 0);
        bytes32 paidHash = pool.getProcessWithdrawalOperationHash(paid);
        _authorize(auth, paidHash, address(pool));
        pool.processWithdrawal(paid, paidHash);
        assertEq(pool.accruedFees(address(mp),false),0);
        UnitEscrowFixture(address(pool.withdrawalEscrow())).deliver(address(pool),paid);
        assertEq(pool.accruedFees(address(mp), false), 20 * 1e10);
        assertEq(manager.totalCollected(), 70 * 1e10);
        bytes32 claimHash = pool.getClaimFeesOperationHash(address(mp), true, member3, 50 * 1e10);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        pool.claimFees(address(mp), true, member3, 50 * 1e10, claimHash);
        _authorize(auth, claimHash, address(pool));
        vm.expectRevert("Insufficient accrued fees");
        pool.claimFees(address(mp), true, member3, 51 * 1e10, claimHash);
        vm.expectRevert("Operation hash mismatch");
        pool.claimFees(address(mp), true, member2, 50 * 1e10, claimHash);
        pool.claimFees(address(mp), true, member3, 50 * 1e10, claimHash);
        assertEq(mp.balanceOf(member3), 50 * 1e10);
        assertEq(pool.accruedFees(address(mp), true), 0);
        assertEq(pool.getPoolInfo(address(mp)).totalDeposited, 570 * 1e10);
        assertEq(pool.getUserPoolBalance(member1, address(mp)), 550 * 1e10);
    }

    function test_Confederation_FeeClaimsRequireCustodiansAndKeepScriptBytes() public {
        (ConfederationMultiPool pool, FROSTAuthorization auth) = _readinessConfederation();
        CustodianValidator validator = pool.custodianValidator();
        IFeeManager.FeeConfig memory config = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, 500, 0, 0);
        FederationFeeManager manager = new FederationFeeManager(address(auth), address(validator), member3, config, config);
        _authorizeFeeCollector(auth, manager, address(pool));
        bytes32 setup = pool.getSetFeeManagerOperationHash(address(manager));
        _authorize(auth, setup, address(pool));
        _approveCustodians(validator, setup);
        pool.setFeeManager(address(manager), setup);
        assertEq(confFactory.getConfederation("readiness-conf").feeManager, address(manager));
        (PoolMpToken mp,) = _setupPoolWithMpToken();
        bytes32 addHash = pool.getAddPoolOperationHash(address(mp), "CONF-FEES");
        _authorize(auth, addHash, address(pool));
        _approveCustodians(validator, addHash);
        pool.addPool(address(mp), "CONF-FEES", addHash);
        mp.mint(member1, 1000 * 1e10);
        vm.startPrank(member1);
        mp.approve(address(pool), 1000 * 1e10);
        pool.deposit(address(mp), 1000 * 1e10);
        bytes memory script = abi.encodePacked(hex"5120", bytes32(Secp256k1.GX));
        uint256 id = pool.requestWithdrawal(address(mp), 400 * 1e10, script);
        vm.stopPrank();
        assertEq(pool.getWithdrawal(id).btcScript, script);
        bytes32 process = pool.getProcessWithdrawalOperationHash(id);
        _authorize(auth, process, address(pool));
        _approveCustodians(validator, process);
        pool.processWithdrawal(id);
        UnitEscrowFixture(address(pool.withdrawalEscrow())).deliver(address(pool),id);
        assertEq(manager.totalCollected(), 70 * 1e10);
        bytes32 claim = pool.getClaimFeesOperationHash(address(mp), false, member3, 20 * 1e10);
        _authorize(auth, claim, address(pool));
        vm.expectRevert(ConfederationMultiPool.InsufficientCustodianSignatures.selector);
        pool.claimFees(address(mp), false, member3, 20 * 1e10, claim);
        assertFalse(auth.executedOperations(claim));
        _approveCustodians(validator, claim);
        pool.claimFees(address(mp), false, member3, 20 * 1e10, claim);
        assertEq(mp.balanceOf(member3), 20 * 1e10);
        assertEq(pool.getPoolInfo(address(mp)).totalDeposited, 600 * 1e10);
    }

    function test_Confederation_CanonicalDAORegistrationValidatesGraphAndDualApproval() public {
        (ConfederationMultiPool pool, FROSTAuthorization auth) = _readinessConfederation();
        CustodianDAOToken token = new CustodianDAOToken("Custodian", "CDAO", member1);
        (PoolMpToken reward,) = _setupPoolWithMpToken();
        CustodianDAOStaking staking = new CustodianDAOStaking(address(token), address(reward), address(pool.custodianValidator()));
        CustodianDAORateController controller = new CustodianDAORateController(address(pool.custodianValidator()), address(staking), true);
        ChainlinkBTCDailyTWAP twap = new ChainlinkBTCDailyTWAP(address(new ReadinessFeed()));
        CustodianDAOIncomeVault vault = new CustodianDAOIncomeVault(address(reward), address(auth));
        CustodianDAODecisionBoard board = new CustodianDAODecisionBoard(address(staking), address(auth));
        CustodianDAODailySettlement settlement = new CustodianDAODailySettlement(
            address(reward), address(staking), address(controller), address(auth), address(twap), address(vault)
        , address(0xBEEF));
        staking.setDistributor(address(settlement));
        vault.setSettlement(address(settlement));
        IConfederationFactory.CustodianDAOConfig memory dao = IConfederationFactory.CustodianDAOConfig(
            address(token), address(staking), address(controller), address(twap), address(vault), address(board), address(settlement)
        );
        assertEq(confFactory.getCustodianDAO("readiness-conf").settlement, address(0));
        bytes32 hash = confFactory.getRegisterCustodianDAOOperationHash("readiness-conf", dao);
        _authorize(auth, hash, address(confFactory));
        vm.expectRevert("Custodian threshold not met");
        confFactory.registerCustodianDAO("readiness-conf", dao, hash);
        _approveCustodians(pool.custodianValidator(), hash);
        IConfederationFactory.CustodianDAOConfig memory wrong = abi.decode(abi.encode(dao), (IConfederationFactory.CustodianDAOConfig));
        wrong.token = address(reward);
        bytes32 wrongHash = confFactory.getRegisterCustodianDAOOperationHash("readiness-conf", wrong);
        _authorize(auth, wrongHash, address(confFactory));
        _approveCustodians(pool.custodianValidator(), wrongHash);
        vm.expectRevert(ConfederationFactory.InvalidCustodianDAO.selector);
        confFactory.registerCustodianDAO("readiness-conf", wrong, wrongHash);
        assertFalse(auth.executedOperations(wrongHash));
        confFactory.registerCustodianDAO("readiness-conf", dao, hash);
        assertEq(confFactory.getCustodianDAO("readiness-conf").settlement, address(settlement));
        assertEq(confFactory.getCustodianDAO("readiness-conf").decisions, address(board));
        vm.expectRevert(ConfederationFactory.CustodianDAOAlreadyRegistered.selector);
        confFactory.registerCustodianDAO("readiness-conf", dao, hash);
    }

    function test_ConfederationTokenLeafAndPoolDeployerPreserveRolesAndStayBelowLimit() public {
        ConfederationTokenDeployer leaf = new ConfederationTokenDeployer();
        vm.expectRevert(MultiPoolDeployer.InvalidSubDeployer.selector);
        new MultiPoolDeployer(address(0));
        MultiPoolDeployer deployer = new MultiPoolDeployer(address(leaf));
        vm.prank(unauthorized);
        vm.expectRevert(abi.encodeWithSelector(ControlledDeployer.UnauthorizedDeployer.selector, unauthorized));
        leaf.deployConfederationToken("S", "S", member1);
        leaf.setAuthorizedDeployer(address(deployer), true);
        FROSTAuthorization auth = _newAuthorization();
        address[] memory custodians = new address[](2);
        custodians[0] = custodian1;
        custodians[1] = custodian2;
        CustodianValidator validator = new CustodianValidator(custodians, 2, address(auth));
        for (uint256 i; i < 2; i++) {
            address predicted = deployer.predictNextMultiPoolAddress();
            (address pool, address share) = deployer.deployMultiPool("CONF", address(auth), address(validator), "S", "S");
            assertEq(pool, predicted);
            assertEq(ConfederationToken(share).confederationPool(), pool);
            assertTrue(ConfederationToken(share).hasRole(bytes32(0), pool));
            assertFalse(ConfederationToken(share).hasRole(bytes32(0), address(leaf)));
        }
        assertLt(address(deployer).code.length, 24576);
        assertLt(address(leaf).code.length, 24576);
    }
}
