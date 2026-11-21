// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/federation/SPVServicePaymentRegistry.sol";

// Accounting/state-machine fixtures, NOT DKG, threshold signatures or SPV proof
// evidence. FROST operation hashing/consumption use the production implementation.
contract GasVerifierFixture {
    function verify(bytes calldata, bytes32, bytes calldata, string calldata) external pure returns (bool) {
        return false;
    }
}
contract GasCoordinatorFixture {
    address public immutable defaultVerifier;
    constructor(address verifier) { defaultVerifier = verifier; }
}
contract GasAuthorizationFixture is FROSTAuthorization {
    bytes32 public constant dkgId = keccak256("synthetic gas DKG");
    bytes32 public constant resultHash = keccak256("synthetic gas result");
    constructor(address coordinator, address[] memory roster)
        FROSTAuthorization(coordinator, abi.encodePacked(bytes32(uint256(1))), roster, 1) {}
    function fixtureApprove(bytes32 op, address target) external {
        operationTargets[op] = target;
        approvedOperations[op] = true;
    }
}
contract GasConfederationFixture {
    address public auth;
    bool public active = true;
    constructor(address auth_) { auth = auth_; }
    function setActive(bool value) external { active = value; }
    function getConfederation(string calldata id) external view
        returns (IConfederationFactory.ConfederationConfig memory conf)
    {
        require(keccak256(bytes(id)) == keccak256("conf"), "fixture conf");
        conf.confederationId = id;
        conf.frostMultisig = auth;
        conf.isActive = active;
        conf.federationIds = new string[](1);
        conf.federationIds[0] = "fed";
    }
}
contract GasStateFixture {
    address public immutable state = address(this);
    address public immutable adapter = address(this);
    bytes32 public policyHash = keccak256("synthetic gas policy");
    bytes32 public digest = keccak256("synthetic SPV state");
    uint256 public version = 1;
    bool public initialized = true;
    bool public halted;
    function snapshot() external view returns (bytes32, uint256, bool, bool) {
        return (digest, version, initialized, halted);
    }
    function change(bytes32 next) external { digest = next; ++version; }
    function availability(bool init, bool halt) external { initialized = init; halted = halt; }
    function setPolicy(bytes32 next) external { policyHash = next; }
}
contract GasRejectingPayeeFixture {
    receive() external payable { revert("payee unavailable"); }
}

contract SPVServicePaymentTest is Test {
    using stdStorage for StdStorage;
    SPVServicePaymentRegistry internal registry;
    ConfederationGasTreasury internal treasury;
    GasAuthorizationFixture internal auth;
    GasConfederationFixture internal factory;
    GasStateFixture internal state;
    address internal constant ALICE = address(0xA11CE);
    address internal constant BOB = address(0xB0B);
    address internal constant PAYER = address(0xCAFE);
    address internal constant ISSUER = address(0x155);
    bytes32 internal constant JOB = keccak256("opaque canonical job");
    uint256 internal constant PRICE = 101;

    function setUp() public {
        vm.warp(1000);
        address[] memory roster = new address[](1); roster[0] = ALICE;
        GasVerifierFixture verifier = new GasVerifierFixture();
        GasCoordinatorFixture coordinator = new GasCoordinatorFixture(address(verifier));
        auth = new GasAuthorizationFixture(address(coordinator), roster);
        factory = new GasConfederationFixture(address(auth));
        state = new GasStateFixture();
        registry = SPVServicePaymentRegistry(deployCode("SPVServicePaymentRegistry.sol:SPVServicePaymentRegistry",
            abi.encode(address(factory), "conf", address(state))));
        treasury = _treasury(1, ALICE, BOB);
        _configure(treasury, 1, PRICE, bytes32(0));
        vm.deal(PAYER, 100 ether);
        vm.deal(address(this), 100 ether);
    }

    function _treasury(uint64 epoch, address a, address b) internal returns (ConfederationGasTreasury) {
        address[] memory payees = new address[](2); payees[0] = a; payees[1] = b;
        uint256[] memory weights = new uint256[](2); weights[0] = 1; weights[1] = 2;
        return ConfederationGasTreasury(payable(registry.createTreasury(epoch, payees, weights)));
    }
    function _configuration(ConfederationGasTreasury target, uint64 tariff, uint256 price, bytes32 handover)
        internal pure returns (SPVServicePaymentRegistry.Configuration memory)
    {
        return SPVServicePaymentRegistry.Configuration(address(target), ISSUER, tariff,
            keccak256("tariff description"), price, handover);
    }
    function _configure(ConfederationGasTreasury target, uint64 tariff, uint256 price, bytes32 handover) internal {
        SPVServicePaymentRegistry.Configuration memory config = _configuration(target, tariff, price, handover);
        bytes32 op = registry.getConfigureOperationHash(config);
        auth.fixtureApprove(op, address(registry));
        registry.configure(config, op);
    }
    function _terms(bytes32 id) internal pure returns (SPVServicePaymentRegistry.JobTerms memory) {
        return SPVServicePaymentRegistry.JobTerms(id, keccak256(abi.encode("semantic", id)), keccak256("fed"),
            PAYER, keccak256("direct payer operating budget"), 2000);
    }
    function _admit(bytes32 id, uint256 previous) internal returns (uint256) {
        bytes32 digest = state.digest(); uint256 version = state.version();
        vm.prank(ISSUER);
        registry.admit(_terms(id), digest, version, 1500, previous);
        return registry.permissionNonce();
    }
    function _fund(bytes32 id, uint256 nonce) internal {
        vm.prank(PAYER); registry.fundRequest{value: PRICE}(id, nonce);
    }
    function _donate(ConfederationGasTreasury target, uint256 amount) internal {
        (bool ok,) = address(target).call{value: amount}(""); require(ok);
    }
    function _pause() internal {
        bytes32 op = registry.getPauseOperationHash(); auth.fixtureApprove(op, address(registry));
        registry.pauseAdmissions(op);
    }

    function testFundingAndZeroBalancePermissionlessClaim() public {
        _fund(JOB, _admit(JOB, 0));
        assertTrue(registry.paid(JOB)); assertEq(registry.pendingCount(), 0);
        assertEq(address(registry).balance, 0); assertEq(address(treasury).balance, PRICE);
        assertEq(ALICE.balance, 0); assertEq(treasury.claimFor(ALICE), 33);
        assertEq(ALICE.balance, 33); assertEq(treasury.claimFor(BOB), 67);
        assertEq(address(treasury).balance, 1); assertEq(treasury.receivedTotal(), PRICE);
    }
    function testDustAccumulatesAndClaimOrderDoesNotChangeEntitlement() public {
        _donate(treasury, 2); assertEq(treasury.claimFor(BOB), 1); assertEq(treasury.claimFor(ALICE), 0);
        _donate(treasury, 1); assertEq(treasury.claimFor(ALICE), 1); assertEq(treasury.claimFor(BOB), 1);
        assertEq(treasury.receivedTotal(), 3); assertEq(address(treasury).balance, 0);
    }
    function testDirectAndForcedFundsDoNotPayJob() public {
        _admit(JOB, 0); _donate(treasury, 3);
        // Synthetic forced balance accounting, without a receive event.
        vm.deal(address(treasury), 9);
        assertFalse(registry.paid(JOB)); assertEq(treasury.claimFor(ALICE), 3);
        assertEq(treasury.claimFor(BOB), 6);
    }
    function testUnknownOrZeroClaimDoesNotChangeAccounting() public {
        assertEq(treasury.claimFor(ALICE), 0); _donate(treasury, 3);
        assertEq(treasury.claimFor(address(0)), 0); assertEq(treasury.totalReleased(), 0);
    }
    function testSelfClaim() public {
        _donate(treasury, 6); vm.prank(ALICE); assertEq(treasury.claim(), 2);
    }
    function testUnavailablePayeeDoesNotBlockOtherPayee() public {
        GasRejectingPayeeFixture bad = new GasRejectingPayeeFixture();
        ConfederationGasTreasury other = _treasury(2, address(bad), BOB);
        _donate(other, 9); vm.expectRevert(); other.claimFor(address(bad));
        assertEq(other.released(address(bad)), 0); assertEq(other.totalReleased(), 0);
        assertEq(other.claimFor(BOB), 6); assertEq(other.claimable(address(bad)), 3);
    }
    function testMulDivHandlesLargeProduct() public {
        address[] memory payees = new address[](2); payees[0] = ALICE; payees[1] = BOB;
        uint256[] memory weights = new uint256[](2); weights[0] = 2 ** 200; weights[1] = 2 ** 200;
        ConfederationGasTreasury other = ConfederationGasTreasury(payable(registry.createTreasury(2, payees, weights)));
        vm.deal(address(other), 2 ** 100);
        assertEq(other.claimable(ALICE), 2 ** 99);
    }
    function testInvalidRostersRejected() public {
        address[] memory payees = new address[](2); payees[0] = ALICE; payees[1] = ALICE;
        uint256[] memory weights = new uint256[](2); weights[0] = 1; weights[1] = 1;
        vm.expectRevert("weight/duplicate"); registry.createTreasury(2, payees, weights);
        payees[1] = address(0); vm.expectRevert("payee"); registry.createTreasury(2, payees, weights);
        payees[1] = BOB; weights[1] = 0;
        vm.expectRevert("weight/duplicate"); registry.createTreasury(2, payees, weights);
        weights[0] = type(uint256).max; weights[1] = 1;
        vm.expectRevert(stdError.arithmeticError); registry.createTreasury(2, payees, weights);
        payees = new address[](0); weights = new uint256[](0);
        vm.expectRevert("roster length"); registry.createTreasury(2, payees, weights);
    }
    function testSelfPayeeRejected() public {
        address predicted = vm.computeCreateAddress(address(registry), vm.getNonce(address(registry)));
        vm.expectRevert("payee"); _treasury(2, predicted, BOB);
    }
    function testRosterBound() public {
        address[] memory payees = new address[](33); uint256[] memory weights = new uint256[](33);
        for (uint256 i; i < 33; ++i) { payees[i] = address(uint160(i + 1)); weights[i] = 1; }
        vm.expectRevert("roster length"); registry.createTreasury(2, payees, weights);
    }
    function testDuplicatePaymentAtomic() public {
        uint256 nonce = _admit(JOB, 0); _fund(JOB, nonce); uint256 balance = PAYER.balance;
        vm.prank(PAYER); vm.expectRevert("already paid"); registry.fundRequest{value: PRICE}(JOB, nonce);
        assertEq(PAYER.balance, balance); assertEq(address(treasury).balance, PRICE);
    }
    function testWrongPayerAmountAndNonceRejected() public {
        uint256 nonce = _admit(JOB, 0);
        vm.expectRevert("payer"); registry.fundRequest{value: PRICE}(JOB, nonce);
        vm.prank(PAYER); vm.expectRevert("amount"); registry.fundRequest{value: PRICE + 1}(JOB, nonce);
        vm.prank(PAYER); vm.expectRevert("permission"); registry.fundRequest{value: PRICE}(JOB, nonce + 1);
        assertFalse(registry.paid(JOB)); assertEq(address(treasury).balance, 0);
    }
    function testStateChangeReturnsValueAndKeepsPermission() public {
        uint256 nonce = _admit(JOB, 0); state.change(keccak256("next state"));
        uint256 beforeBalance = PAYER.balance;
        vm.prank(PAYER); vm.expectRevert("state changed"); registry.fundRequest{value: PRICE}(JOB, nonce);
        assertEq(PAYER.balance, beforeBalance); assertFalse(registry.paid(JOB));
        assertEq(registry.pendingCount(), 1); assertEq(address(treasury).balance, 0);
    }
    function testReturnToOldDigestStillInvalidatesPermission() public {
        uint256 nonce = _admit(JOB, 0); bytes32 old = state.digest();
        state.change(keccak256("next state")); state.change(old);
        vm.prank(PAYER); vm.expectRevert("state changed"); registry.fundRequest{value: PRICE}(JOB, nonce);
    }
    function testRevisionPreservesJobAndInvalidatesPreviousNonce() public {
        uint256 previous = _admit(JOB, 0); state.change(keccak256("next state"));
        uint256 next = _admit(JOB, previous); assertEq(registry.pendingCount(), 1);
        vm.prank(PAYER); vm.expectRevert("permission"); registry.fundRequest{value: PRICE}(JOB, previous);
        _fund(JOB, next);
    }
    function testRevisionCannotExtendEconomicDeadline() public {
        uint256 previous = _admit(JOB, 0);
        SPVServicePaymentRegistry.JobTerms memory terms = _terms(JOB); terms.serviceDeadline++;
        bytes32 digest = state.digest(); uint256 version = state.version();
        vm.prank(ISSUER); vm.expectRevert("immutable job"); registry.admit(terms, digest, version, 1500, previous);
    }
    function testSemanticDeduplicationAndFederationScope() public {
        _admit(JOB, 0); SPVServicePaymentRegistry.JobTerms memory terms = _terms(JOB);
        terms.jobId = keccak256("different job"); bytes32 digest = state.digest(); uint256 version = state.version();
        vm.prank(ISSUER); vm.expectRevert("semantic job exists"); registry.admit(terms, digest, version, 1500, 0);
        terms.semanticKey = keccak256("different key"); terms.federationIdHash = keccak256("other federation");
        vm.prank(ISSUER); vm.expectRevert("federation"); registry.admit(terms, digest, version, 1500, 0);
    }
    function testExpiryDoesNotErasePaidRecord() public {
        uint256 nonce = _admit(JOB, 0); _fund(JOB, nonce); vm.warp(3000);
        assertTrue(registry.paid(JOB)); vm.prank(PAYER); vm.expectRevert("already paid");
        registry.fundRequest{value: PRICE}(JOB, nonce);
    }
    function testExpiredUnpaidPermissionCanBeCancelledByAnyone() public {
        uint256 nonce = _admit(JOB, 0); vm.warp(1501);
        vm.prank(PAYER); vm.expectRevert("deadline"); registry.fundRequest{value: PRICE}(JOB, nonce);
        registry.cancelPermission(JOB, nonce); assertEq(registry.pendingCount(), 0);
        assertEq(registry.semanticJob(_terms(JOB).semanticKey), JOB);
    }
    function testActivePermissionOnlyIssuerCanCancel() public {
        uint256 nonce = _admit(JOB, 0); vm.expectRevert("not cancellable"); registry.cancelPermission(JOB, nonce);
        vm.prank(ISSUER); registry.cancelPermission(JOB, nonce);
        vm.prank(PAYER); vm.expectRevert("permission"); registry.fundRequest{value: PRICE}(JOB, nonce);
    }
    function testRotationKeepsPaidLedgerAndOldClaims() public {
        uint256 nonce = _admit(JOB, 0); _fund(JOB, nonce);
        ConfederationGasTreasury next = _treasury(2, ALICE, BOB);
        _configure(next, 1, PRICE, keccak256("explicit handover terms"));
        vm.prank(PAYER); vm.expectRevert("already paid"); registry.fundRequest{value: PRICE}(JOB, nonce);
        (address paidTreasury, uint64 epoch,,,) = registry.payments(JOB);
        assertEq(paidTreasury, address(treasury)); assertEq(epoch, 1);
        assertEq(treasury.claimFor(ALICE), 33); _donate(treasury, 1); assertEq(treasury.claimFor(ALICE), 1);
        assertEq(address(next).balance, 0);
    }
    function testRotationInvalidatesUnpaidPermissionUntilExplicitRevision() public {
        uint256 nonce = _admit(JOB, 0); ConfederationGasTreasury next = _treasury(2, ALICE, BOB);
        _configure(next, 1, PRICE, keccak256("handover"));
        vm.prank(PAYER); vm.expectRevert("permission"); registry.fundRequest{value: PRICE}(JOB, nonce);
        uint256 replacement = _admit(JOB, nonce); _fund(JOB, replacement);
        assertEq(address(next).balance, PRICE); assertEq(address(treasury).balance, 0);
    }
    function testPauseRevokesGenerationEvenWhileStateHalted() public {
        uint256 nonce = _admit(JOB, 0); state.availability(true, true); _pause();
        vm.prank(PAYER); vm.expectRevert("disabled"); registry.fundRequest{value: PRICE}(JOB, nonce);
        registry.cancelPermission(JOB, nonce); assertEq(registry.pendingCount(), 0);
    }
    function testDisabledByDefault() public {
        SPVServicePaymentRegistry other = SPVServicePaymentRegistry(deployCode(
            "SPVServicePaymentRegistry.sol:SPVServicePaymentRegistry", abi.encode(address(factory), "conf", address(state))));
        assertFalse(other.enabled()); vm.expectRevert("disabled"); other.fundRequest{value: 1}(JOB, 1);
    }
    function testConfigurationRequiresApprovalAndCannotReplay() public {
        SPVServicePaymentRegistry.Configuration memory config = _configuration(treasury, 1, PRICE, 0);
        bytes32 op = registry.getConfigureOperationHash(config);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector); registry.configure(config, op);
        auth.fixtureApprove(op, address(registry)); registry.configure(config, op);
        vm.expectRevert("operation"); registry.configure(config, op);
    }
    function testTariffImmutableAndRotationRequiresHandover() public {
        SPVServicePaymentRegistry.Configuration memory config = _configuration(treasury, 1, PRICE + 1, 0);
        bytes32 op = registry.getConfigureOperationHash(config);
        vm.expectRevert("immutable tariff"); registry.configure(config, op);
        ConfederationGasTreasury next = _treasury(2, ALICE, BOB);
        config = _configuration(next, 1, PRICE, 0); op = registry.getConfigureOperationHash(config);
        vm.expectRevert("handover"); registry.configure(config, op);
    }
    function testUninitializedHaltedAndDeactivatedStatesRejectPayment() public {
        uint256 nonce = _admit(JOB, 0); state.availability(false, false);
        vm.prank(PAYER); vm.expectRevert("state unavailable"); registry.fundRequest{value: PRICE}(JOB, nonce);
        state.availability(true, true);
        vm.prank(PAYER); vm.expectRevert("state unavailable"); registry.fundRequest{value: PRICE}(JOB, nonce);
        state.availability(true, false); factory.setActive(false);
        vm.prank(PAYER); vm.expectRevert("confederation"); registry.fundRequest{value: PRICE}(JOB, nonce);
    }
    function testPolicyChangeRejectsPayment() public {
        uint256 nonce = _admit(JOB, 0); state.setPolicy(keccak256("other policy"));
        vm.prank(PAYER); vm.expectRevert("state binding"); registry.fundRequest{value: PRICE}(JOB, nonce);
    }
    function testBoundedPendingAndCancellationFreesOneSlot() public {
        // Synthetic boundary setup: 256 actual admissions are exercised as
        // separate capped transactions in the Anvil receipt test, not one
        // artificial >16M-gas Solidity test transaction.
        stdstore.target(address(registry)).sig("pendingCount()").checked_write(255);
        _admit(bytes32(uint256(1)), 0);
        bytes32 digest = state.digest(); uint256 version = state.version();
        vm.prank(ISSUER); vm.expectRevert("pending limit"); registry.admit(_terms(bytes32(uint256(257))), digest, version, 1500, 0);
        vm.prank(ISSUER); registry.cancelPermission(bytes32(uint256(1)), 1);
        _admit(bytes32(uint256(257)), 0); assertEq(registry.pendingCount(), 256);
    }
    function testFuzzCumulativeAccounting(uint96 first, uint96 second, bool bobFirst) public {
        vm.deal(address(this), uint256(first) + second);
        _donate(treasury, first); treasury.claimFor(bobFirst ? BOB : ALICE);
        _donate(treasury, second); treasury.claimFor(ALICE); treasury.claimFor(BOB);
        uint256 received = uint256(first) + second;
        assertEq(treasury.released(ALICE), received / 3);
        assertEq(treasury.released(BOB), received * 2 / 3);
        assertEq(treasury.totalReleased() + address(treasury).balance, received);
    }
}
