// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "forge-std/Test.sol";
import "../contracts/src/federation/BitcoinMintAdmission.sol";
import "../contracts/src/federation/MiningPoolV1.sol";

// State-machine fixtures only. The real coordinator creates and aborts sessions,
// the real authorization hashes/consumes operations, and the real pool binds its
// admission. Completed DKG data, approvals, FINALIZED observations and successful
// mint observations are synthetic; this suite is not threshold-signature or SPV
// execution evidence. The verifier deliberately accepts no signature.
contract MintAdmissionVerifierFixture {
    function verify(bytes calldata, bytes32, bytes calldata, string calldata) external pure returns (bool) {
        return false;
    }
}

contract MintAdmissionDkgFixture {
    bytes32 public constant ID = keccak256("mint admission synthetic epoch");
    bytes32 public constant RESULT = keccak256("mint admission synthetic result");
    uint256 public constant GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798;
    uint256 public constant GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8;
    address[] private roster;

    constructor(address[] memory members) { roster = members; }

    function getCompletedResult(bytes32 id) external view
        returns (bytes32, bytes32, uint64, bytes32, bytes memory, address[] memory, uint256)
    {
        require(id == ID, "fixture epoch");
        return (ID, RESULT, 1, keccak256("EVM_AUTH"), abi.encodePacked(GX), roster, 2);
    }

    function getPublicKeySharePoint(bytes32 id, address) external pure returns (uint256, uint256) {
        require(id == ID, "fixture epoch");
        return (GX, GY);
    }
}

contract MintAdmissionAuthorizationFixture is DKGAuthorization {
    constructor(address coordinator_, bytes32 id, bytes memory group, address[] memory roster)
        DKGAuthorization(coordinator_, id, group, roster, 2) {}

    function fixtureApprove(bytes32 op, address target) external {
        operationTargets[op] = target;
        approvedOperations[op] = true;
    }
}

contract MintAdmissionBatchFixture {
    FROSTAuthorization public authorization;
    MintAdmissionDkgFixture public dkg;
    bytes32 public walletId;
    constructor(FROSTAuthorization auth, MintAdmissionDkgFixture registry) {
        authorization = auth;
        dkg = registry;
        walletId = registry.ID();
    }
}

contract MintAdmissionEscrowFixture {
    MintAdmissionBatchFixture public batches;
    address public spv;
    constructor(MintAdmissionBatchFixture batches_, address spv_) { batches = batches_; spv = spv_; }
}

contract MintAdmissionDepositFixture {
    address public addressRegistry;
    FROSTAuthorization public authorization;
    address public spv;
    mapping(bytes32 => bool) public consumed;
    mapping(bytes32 => bool) public walletOutput;
    mapping(address => bytes32) public poolScripts;

    constructor(FROSTAuthorization auth, address spv_) { authorization = auth; spv = spv_; }

    function recordConsumed(bytes32 outpoint) external { consumed[outpoint] = true; }
    function recordWalletOutput(bytes32 outpoint) external { walletOutput[outpoint] = true; }
    function fixtureRegister(address pool, bytes32 scriptHash) external { poolScripts[pool] = scriptHash; }

    // Proof and canonical UTXO validation belong to the deposit registry suite.
    function consume(bytes calldata raw, uint32 vout, bytes32, bytes32[] calldata, uint256)
        external returns (uint64)
    {
        bytes32 outpoint = keccak256(abi.encode(BitcoinTxParser.txId(raw), vout));
        require(!consumed[outpoint], "already consumed");
        consumed[outpoint] = true;
        return 1_000;
    }
}

contract MintAdmissionPoolFixture is MiningPoolV1 {
    constructor(address registry, address escrow_)
        MiningPoolV1("mint-admission-unit", registry, escrow_, "MP", "MP") {}

    function fixtureSetNonce(uint256 value) external { nonce = value; }

    function fixtureCompletedAllocation(bytes32 txid, uint32 vout, address[] memory recipients, uint256[] memory amounts)
        external
    {
        mintedAllocations[keccak256(abi.encode(txid, vout))] = keccak256(abi.encode(recipients, amounts));
        ++nonce;
    }
}

contract BitcoinMintAdmissionTest is Test {
    address internal constant ALICE = address(0xA11CE);
    address internal constant BOB = address(0xB0B);
    address internal constant CAROL = address(0xCA401);
    address internal constant RECIPIENT = address(0xC0FFEE);
    bytes32 internal constant TXID = keccak256("synthetic funding transaction");
    uint32 internal constant VOUT = 17;
    uint64 internal deadline;
    FROSTCoordinator internal coordinator;
    MintAdmissionDkgFixture internal dkg;
    MintAdmissionAuthorizationFixture internal authorization;
    MintAdmissionPoolFixture internal pool;
    BitcoinMintAdmission internal admission;

    function setUp() public {
        vm.warp(1_000);
        deadline = uint64(block.timestamp + 1 hours);
        address[] memory members = new address[](3);
        members[0] = ALICE; members[1] = BOB; members[2] = CAROL;
        // Artifact-backed CREATE keeps this test contract from embedding every
        // production initcode blob; it does not change the deployed runtimes.
        coordinator = FROSTCoordinator(deployCode("FROSTCoordinator.sol:FROSTCoordinator",
            abi.encode(address(new MintAdmissionVerifierFixture()))));
        dkg = new MintAdmissionDkgFixture(members);
        coordinator.bindDKGRegistry(address(dkg));
        authorization = MintAdmissionAuthorizationFixture(deployCode(
            "BitcoinMintAdmission.t.sol:MintAdmissionAuthorizationFixture",
            abi.encode(address(coordinator), dkg.ID(), abi.encodePacked(dkg.GX()), members)));
        MintAdmissionBatchFixture batches = new MintAdmissionBatchFixture(authorization, dkg);
        MintAdmissionEscrowFixture escrow = new MintAdmissionEscrowFixture(batches, address(dkg));
        MintAdmissionDepositFixture deposits = new MintAdmissionDepositFixture(authorization, address(dkg));
        pool = MintAdmissionPoolFixture(deployCode("BitcoinMintAdmission.t.sol:MintAdmissionPoolFixture",
            abi.encode(address(deposits), address(escrow))));
        deposits.fixtureRegister(address(pool), keccak256(pool.payoutScript()));
        admission = _deployAdmission(pool, ALICE);
        bytes32 bind = pool.getSetMintAdmissionOperationHash(address(admission));
        authorization.fixtureApprove(bind, address(pool));
        pool.setMintAdmission(address(admission), bind);
    }

    function _terms() internal pure returns (address[] memory recipients, uint256[] memory amounts) {
        recipients = new address[](1); recipients[0] = RECIPIENT;
        amounts = new uint256[](1); amounts[0] = 1_000 * 1e10;
    }

    function _deployAdmission(MintAdmissionPoolFixture target, address owner) internal returns (BitcoinMintAdmission) {
        return BitcoinMintAdmission(deployCode("BitcoinMintAdmission.sol:BitcoinMintAdmission",
            abi.encode(address(target), owner)));
    }

    function _open(bytes32 txid) internal returns (uint256) {
        return _openAt(txid, VOUT);
    }

    function _openAt(bytes32 txid, uint32 vout) internal returns (uint256) {
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        uint256 position = pool.nonce();
        vm.prank(ALICE);
        return admission.openMint(txid, vout, recipients, amounts, deadline, position);
    }

    function _mintRaw() internal view returns (bytes memory) {
        return abi.encodePacked(hex"0200000001", bytes32(0), hex"ffffffff00ffffffff01e80300000000000022",
            pool.payoutScript(), hex"00000000");
    }

    function _mint(bytes memory raw, bytes32 op) internal {
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        pool.mintFromProof(raw, 0, bytes32(uint256(1)), new bytes32[](0), 0, recipients, amounts, op);
    }

    function _claim() internal view returns (bytes32) { return admission.getClaimId(TXID, VOUT); }

    function _retry(uint256 previous) internal returns (uint256) {
        bytes32 claimId = _claim();
        uint256 position = pool.nonce();
        vm.prank(ALICE);
        return admission.retryMint(claimId, position, previous);
    }

    function _openFailure(bytes32 txid, bytes memory reason) internal {
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        uint256 position = pool.nonce();
        vm.prank(ALICE); vm.expectRevert(reason);
        admission.openMint(txid, VOUT, recipients, amounts, deadline, position);
    }

    function _retryFailure(uint256 previous, bytes memory reason) internal {
        bytes32 claimId = _claim();
        uint256 position = pool.nonce();
        vm.prank(ALICE); vm.expectRevert(reason);
        admission.retryMint(claimId, position, previous);
    }

    function _sessionWord(uint256 session, uint256 index) internal view returns (bytes32 word) {
        (bool ok, bytes memory result) = address(coordinator).staticcall(
            abi.encodeWithSelector(coordinator.getSession.selector, session));
        require(ok && result.length >= 20 * 32, "session fixture read");
        assembly ("memory-safe") { word := mload(add(add(result, 32), mul(index, 32))) }
    }

    function _fixtureFinalizedObservation(uint256 session) internal {
        // Only this read is mocked. No aggregate signature or finalized proof is
        // manufactured; tests below exercise admission's observed-state branch.
        (bool ok, bytes memory result) = address(coordinator).staticcall(
            abi.encodeWithSelector(coordinator.getSession.selector, session));
        require(ok, "fixture read");
        assembly ("memory-safe") { mstore(add(result, 352), 2) } // getSession state is word 10
        vm.mockCall(address(coordinator), abi.encodeWithSelector(coordinator.getSession.selector, session), result);
    }

    function _assertClaim(uint256 count, uint256 latest) internal view {
        (bytes32 txid, uint32 vout, address[] memory recipients, uint256[] memory amounts,
            uint64 originalDeadline, uint256 attempts, uint256 latestSession) = admission.getClaim(_claim());
        assertEq(txid, TXID); assertEq(vout, VOUT);
        assertEq(recipients.length, 1); assertEq(recipients[0], RECIPIENT);
        assertEq(amounts[0], 1_000 * 1e10); assertEq(originalDeadline, deadline);
        assertEq(attempts, count); assertEq(latestSession, latest);
    }

    function testRealPoolBindingIsOneTimeUsesDistinctAuthorizationAndPreservesMintNonce() public {
        assertEq(pool.mintAdmission(), address(admission));
        assertEq(pool.nonce(), 0);
        bytes32 bind = pool.getSetMintAdmissionOperationHash(address(admission));
        assertTrue(authorization.executedOperations(bind));
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        assertTrue(bind != pool.getMintOperationHash(TXID, VOUT, recipients, amounts));
        pool.fixtureSetNonce(9);
        assertEq(pool.getSetMintAdmissionOperationHash(address(admission)), bind);
        vm.expectRevert("admission binding");
        pool.setMintAdmission(address(admission), bind);
        assertEq(pool.nonce(), 9);
    }

    function testUnboundAdmissionAndNonparticipantCannotOpen() public {
        BitcoinMintAdmission other = _deployAdmission(pool, ALICE);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        vm.prank(ALICE); vm.expectRevert("unbound admission");
        other.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        vm.prank(address(0xBAD)); vm.expectRevert("participant");
        admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        vm.prank(BOB); vm.expectRevert("operator");
        admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        assertEq(coordinator.nextSessionId(), 0);
    }

    function testBindingContextFailureDoesNotConsumeApprovalOrMoveMintNonce() public {
        MintAdmissionPoolFixture second = MintAdmissionPoolFixture(deployCode(
            "BitcoinMintAdmission.t.sol:MintAdmissionPoolFixture", abi.encode(address(pool.deposits()), address(pool.escrow()))));
        bytes32 op = second.getSetMintAdmissionOperationHash(address(admission));
        authorization.fixtureApprove(op, address(second));
        vm.expectRevert("admission context");
        second.setMintAdmission(address(admission), op);
        assertFalse(authorization.executedOperations(op));
        assertEq(second.mintAdmission(), address(0)); assertEq(second.nonce(), 0);
        BitcoinMintAdmission matching = _deployAdmission(second, ALICE);
        bytes32 correct = second.getSetMintAdmissionOperationHash(address(matching));
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        second.setMintAdmission(address(matching), correct);
        assertEq(second.mintAdmission(), address(0)); assertEq(second.nonce(), 0);
    }

    function testNewClaimRequiresFutureDeadlineAndBoundedDistribution() public {
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.MintDeadlineInvalid.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, uint64(block.timestamp), 0);
        uint64 tooLong = uint64(block.timestamp + admission.MAX_MINT_WINDOW() + 1);
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.MintDeadlineInvalid.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, tooLong, 0);
        recipients = new address[](0); amounts = new uint256[](0);
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.InvalidMintDistribution.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        recipients = new address[](65); amounts = new uint256[](65);
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.InvalidMintDistribution.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        assertEq(coordinator.nextSessionId(), 0);
    }

    function testRealSessionAtomicallyBindsCreatorTargetEpochOperationAndSlot() public {
        uint256 session = _open(TXID);
        (bytes32 claimId, bytes32 op, uint256 position, uint64 originalDeadline, bool retired) = admission.attempts(session);
        assertEq(claimId, _claim()); assertEq(position, 0); assertEq(originalDeadline, deadline); assertFalse(retired);
        assertEq(_sessionWord(session, 1), bytes32(uint256(uint160(address(admission)))));
        assertEq(_sessionWord(session, 3), op);
        assertEq(uint256(_sessionWord(session, 7)), deadline);
        assertEq(uint256(_sessionWord(session, 8)), 1);
        assertEq(uint256(_sessionWord(session, 10)), 1);
        assertEq(_sessionWord(session, 15), bytes32(uint256(uint160(address(pool)))));
        assertEq(coordinator.sessionDKGId(session), dkg.ID());
        assertEq(coordinator.sessionDKGResult(session), dkg.RESULT());
        assertEq(admission.slotSession(0), session); assertTrue(admission.active(session));
        assertEq(admission.claimOperator(_claim()), ALICE);
        assertEq(pool.nonce(), 0); _assertClaim(1, session);
    }

    function testLostReceiptOtherParticipantAdoptsExactSessionWithoutAnotherPaidStart() public {
        uint256 session = _open(TXID);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        vm.prank(BOB);
        assertEq(admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0), session);
        vm.prank(CAROL);
        assertEq(admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0), session);
        assertEq(coordinator.nextSessionId(), session); _assertClaim(1, session);
    }

    function testDifferentClientsCannotOpenDifferentClaimsAtSamePoolNonce() public {
        uint256 session = _open(TXID);
        _openFailure(bytes32(uint256(2)), abi.encodeWithSelector(BitcoinMintAdmission.MintSlotBusy.selector));
        assertEq(coordinator.nextSessionId(), session);
        (,,,, uint64 absentDeadline, uint256 absentCount,) = admission.getClaim(admission.getClaimId(bytes32(uint256(2)), VOUT));
        assertEq(absentDeadline, 0); assertEq(absentCount, 0);
        assertEq(admission.slotSession(0), session);
    }

    function testClaimAllocationAndOriginalDeadlineAreImmutable() public {
        uint256 session = _open(TXID);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        recipients[0] = BOB;
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.MintClaimTermsMismatch.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        recipients[0] = RECIPIENT; ++amounts[0];
        vm.prank(BOB); vm.expectRevert(BitcoinMintAdmission.MintClaimTermsMismatch.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        --amounts[0];
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.MintClaimTermsMismatch.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, deadline + 1, 0);
        _assertClaim(1, session); assertEq(coordinator.nextSessionId(), session);
    }

    function testChangedExpectedNonceRejectsBeforeOpeningSession() public {
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        pool.fixtureSetNonce(1);
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.MintNonceChanged.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0);
        assertEq(coordinator.nextSessionId(), 0);
    }

    function testExpiredOpenSessionIsAbortedBeforeReleaseAndClaimCannotRefreshDeadline() public {
        uint256 session = _open(TXID);
        vm.warp(deadline + 1);
        assertTrue(admission.active(session)); // TTL alone has not made it non-executable.
        vm.prank(BOB); admission.retire(session);
        assertEq(uint256(_sessionWord(session, 10)), 3);
        assertFalse(admission.active(session)); assertEq(admission.slotSession(0), 0);
        assertEq(pool.nonce(), 1);
        vm.prank(CAROL); admission.retire(session); // lost retirement receipt
        _retryFailure(session, abi.encodeWithSelector(BitcoinMintAdmission.MintDeadlineInvalid.selector));
        _assertClaim(1, session);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.MintClaimTermsMismatch.selector);
        admission.openMint(TXID, VOUT, recipients, amounts, deadline + 1 hours, 1);
    }

    function testLiveOpenSessionCannotBeRetiredAndOriginalDeadlineOverridesAdminExtension() public {
        uint256 session = _open(TXID);
        vm.prank(BOB); vm.expectRevert("session live"); admission.retire(session);
        coordinator.extendDeadline(session, deadline + 1 days);
        vm.warp(deadline + 1);
        vm.prank(BOB); admission.retire(session);
        assertEq(uint256(_sessionWord(session, 10)), 3);
        _assertClaim(1, session);
    }

    function testSyntheticFinalizedObservationIsRetainedThroughDeadline() public {
        uint256 session = _open(TXID);
        _fixtureFinalizedObservation(session);
        vm.warp(deadline);
        vm.prank(BOB); vm.expectRevert(BitcoinMintAdmission.AdmissionAuthorizationStillExecutable.selector); admission.retire(session);
        assertTrue(admission.active(session)); assertEq(_retry(session), session);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        vm.prank(CAROL);
        assertEq(admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0), session);
        _assertClaim(1, session);
    }

    function testExpiredOpenWithApprovalAbortsSessionAndFencesApproval() public {
        uint256 session = _open(TXID);
        (, bytes32 op,,,) = admission.attempts(session);
        authorization.fixtureApprove(op, address(pool));
        vm.warp(deadline + 1);
        vm.prank(BOB); admission.retire(session);
        assertEq(admission.slotSession(0), 0);
        assertEq(pool.nonce(), 1);
        assertEq(uint256(_sessionWord(session, 10)), 3);
        assertTrue(authorization.isOperationApproved(op, address(pool)));
    }

    function testRetirementWinsSameBlockRaceAgainstProofMint() public {
        bytes memory raw = _mintRaw();
        uint256 session = _openAt(sha256(abi.encodePacked(sha256(raw))), 0);
        _fixtureFinalizedObservation(session);
        (, bytes32 op,,,) = admission.attempts(session);
        authorization.fixtureApprove(op, address(pool));
        vm.warp(deadline + 1);
        vm.prank(BOB); admission.retire(session);
        vm.expectRevert(bytes("hash")); _mint(raw, op);
        assertEq(pool.nonce(), 1);
        assertEq(pool.mpToken().totalSupply(), 0);
        assertEq(admission.operatorNonce(), 0);
        deadline += 1 hours;
        uint256 unrelated = _open(TXID);
        vm.prank(CAROL); admission.retire(session);
        assertEq(pool.nonce(), 1);
        assertEq(admission.slotSession(1), unrelated);
    }

    function testProofMintWinsSameBlockRaceAgainstRetirement() public {
        bytes memory raw = _mintRaw();
        uint256 session = _openAt(sha256(abi.encodePacked(sha256(raw))), 0);
        _fixtureFinalizedObservation(session);
        (, bytes32 op,,,) = admission.attempts(session);
        authorization.fixtureApprove(op, address(pool));
        vm.warp(deadline + 1);
        _mint(raw, op);
        vm.prank(BOB); admission.retire(session);
        assertEq(pool.nonce(), 1);
        assertEq(pool.mpToken().balanceOf(RECIPIENT), 1_000 * 1e10);
        assertEq(admission.slotSession(0), 0);
        assertTrue(authorization.executedOperations(op));
    }

    function testOnlyAdmissionCanAdvanceAnExactMintNonce() public {
        vm.prank(ALICE); vm.expectRevert("mint expiry");
        pool.expireMintNonce(0);
        vm.prank(address(admission)); vm.expectRevert("mint expiry");
        pool.expireMintNonce(1);
        assertEq(pool.nonce(), 0);
    }

    function testRenewalRequiresOperatorRetirementAndNewNonceButPreservesTerms() public {
        uint256 previous = _open(TXID);
        bytes32 claim = _claim();
        (, bytes32 oldOp,,,) = admission.attempts(previous);
        uint64 fresh = deadline + 1 hours;
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.RenewalUnavailable.selector);
        admission.renewMint(claim, 0, previous, fresh, 0, 0);
        vm.warp(deadline + 1);
        vm.prank(BOB); admission.retire(previous);
        vm.prank(BOB); vm.expectRevert(BitcoinMintAdmission.MintOperatorOrNonceMismatch.selector);
        admission.renewMint(claim, 1, previous, fresh, 0, 0);
        vm.prank(ALICE);
        uint256 current = admission.renewMint(claim, 1, previous, fresh, 0, 0);
        (bytes32 currentClaim, bytes32 currentOp, uint256 currentNonce, uint64 currentDeadline,) = admission.attempts(current);
        assertEq(currentClaim, claim);
        assertEq(currentNonce, 1);
        assertEq(currentDeadline, fresh);
        assertNotEq(currentOp, oldOp);
        _assertClaim(2, current);
        vm.prank(ALICE); vm.expectRevert(BitcoinMintAdmission.RenewalUnavailable.selector);
        admission.renewMint(claim, 1, previous, fresh, 0, 0);
    }

    function testFinalizedConsumedOutputReleasesOnlyItsOwnSlotAtCurrentNonce() public {
        uint256 session = _open(TXID);
        _fixtureFinalizedObservation(session);
        (, bytes32 op,,,) = admission.attempts(session);
        authorization.fixtureApprove(op, address(pool));

        bytes32 outpoint = keccak256(abi.encode(TXID, VOUT));
        MintAdmissionDepositFixture(address(pool.deposits())).recordConsumed(outpoint);
        assertFalse(admission.active(session));
        assertEq(pool.nonce(), 0);
        vm.prank(BOB);
        admission.retire(session);
        assertEq(admission.slotSession(0), 0);
        assertTrue(authorization.approvedOperations(op));
        _retryFailure(session, abi.encodeWithSelector(BitcoinMintAdmission.MintOutpointRecorded.selector));
        _openFailure(TXID, abi.encodeWithSelector(BitcoinMintAdmission.MintOutpointRecorded.selector));

        uint256 next = _open(bytes32(uint256(99)));
        vm.prank(CAROL);
        admission.retire(session);
        assertEq(admission.slotSession(0), next);
        assertTrue(admission.active(next));
        _assertClaim(1, session);
    }

    function testWalletOutputIsIneligibleBeforeAndAfterAdmission() public {
        MintAdmissionDepositFixture deposits = MintAdmissionDepositFixture(address(pool.deposits()));
        bytes32 other = bytes32(uint256(2));
        deposits.recordWalletOutput(keccak256(abi.encode(other, VOUT)));
        _openFailure(other, abi.encodeWithSelector(BitcoinMintAdmission.MintOutpointRecorded.selector));
        assertEq(coordinator.nextSessionId(), 0);

        uint256 session = _open(TXID);
        deposits.recordWalletOutput(keccak256(abi.encode(TXID, VOUT)));
        vm.prank(address(0xBAD)); vm.expectRevert("participant");
        admission.retire(session);
        assertEq(admission.slotSession(0), session);
        assertFalse(admission.active(session));

        vm.prank(BOB);
        admission.retire(session);
        assertEq(admission.slotSession(0), 0);
        _retryFailure(session, abi.encodeWithSelector(BitcoinMintAdmission.MintOutpointRecorded.selector));
    }

    function testPreviouslyApprovedExactOperationDoesNotOpenAnotherSession() public {
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        authorization.fixtureApprove(pool.getMintOperationHash(TXID, VOUT, recipients, amounts), address(pool));
        _openFailure(TXID, abi.encodeWithSelector(BitcoinMintAdmission.MintOperationApproved.selector));
        assertEq(coordinator.nextSessionId(), 0);
        (,,,, uint64 originalDeadline,,) = admission.getClaim(_claim());
        assertEq(originalDeadline, 0);
    }

    function testNonceAdvanceCreatesBoundedAttemptAndPreservesPreviousContext() public {
        uint256 previous = _open(TXID);
        (, bytes32 oldOp,,,) = admission.attempts(previous);
        pool.fixtureSetNonce(1);
        uint256 next = _retry(previous);
        assertTrue(next > previous); assertFalse(admission.active(previous)); assertTrue(admission.active(next));
        (, bytes32 storedOldOp, uint256 oldNonce, uint64 oldDeadline, bool retired) = admission.attempts(previous);
        assertEq(storedOldOp, oldOp); assertEq(oldNonce, 0); assertEq(oldDeadline, deadline); assertTrue(retired);
        (, bytes32 nextOp, uint256 nextNonce,,) = admission.attempts(next);
        assertTrue(nextOp != oldOp); assertEq(nextNonce, 1);
        assertEq(admission.slotSession(0), 0); assertEq(admission.slotSession(1), next);
        _assertClaim(2, next);
    }

    function testFinalizedStaleAttemptCanBeSupersededWithoutDeletingItsHistory() public {
        uint256 previous = _open(TXID);
        _fixtureFinalizedObservation(previous);
        (, bytes32 oldOp,,,) = admission.attempts(previous);
        authorization.fixtureApprove(oldOp, address(pool));
        pool.fixtureSetNonce(1);
        uint256 next = _retry(previous);
        assertTrue(authorization.approvedOperations(oldOp));
        assertEq(uint256(_sessionWord(previous, 10)), 2);
        assertTrue(admission.active(next)); _assertClaim(2, next);
    }

    function testThreeSameNonceAbortedAttemptsHaveDistinctSessionIdentity() public {
        uint256 first = _open(TXID);
        coordinator.abortSession(first, "unit fixture early cancellation");
        uint256 second = _retry(first);
        coordinator.abortSession(second, "unit fixture second cancellation");
        uint256 third = _retry(second);
        assertTrue(first < second && second < third);
        (, bytes32 firstOp,,,) = admission.attempts(first);
        (, bytes32 secondOp,,,) = admission.attempts(second);
        (, bytes32 thirdOp,,,) = admission.attempts(third);
        assertEq(firstOp, secondOp); assertEq(secondOp, thirdOp);
        assertFalse(admission.active(first)); assertFalse(admission.active(second)); assertTrue(admission.active(third));
        _assertClaim(3, third); assertEq(pool.nonce(), 0);
    }

    function testStaleRetryPredecessorCannotRebaseANewerAttempt() public {
        uint256 first = _open(TXID);
        coordinator.abortSession(first, "unit cancellation");
        uint256 second = _retry(first);
        _retryFailure(first, abi.encodeWithSelector(BitcoinMintAdmission.MintAttemptChanged.selector));
        assertEq(coordinator.nextSessionId(), second); _assertClaim(2, second);
    }

    function testRetryBudgetCannotResetOnNonceAdvance() public {
        uint256 session = _open(TXID);
        for (uint256 i = 1; i < admission.MAX_ATTEMPTS(); ++i) {
            pool.fixtureSetNonce(i);
            session = _retry(session);
        }
        pool.fixtureSetNonce(admission.MAX_ATTEMPTS());
        _retryFailure(session, abi.encodeWithSelector(BitcoinMintAdmission.MintAttemptLimit.selector));
        _assertClaim(admission.MAX_ATTEMPTS(), session);
        assertEq(coordinator.nextSessionId(), session);
    }

    function testSyntheticExactCompletionStopsRetryAndLeavesExactAllocationRecoverable() public {
        uint256 session = _open(TXID);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        pool.fixtureCompletedAllocation(TXID, VOUT, recipients, amounts);
        assertEq(pool.mintedAllocations(keccak256(abi.encode(TXID, VOUT))), keccak256(abi.encode(recipients, amounts)));
        vm.prank(BOB); admission.retire(session);
        _retryFailure(session, abi.encodeWithSelector(BitcoinMintAdmission.MintOutpointMinted.selector));
        _openFailure(TXID, abi.encodeWithSelector(BitcoinMintAdmission.MintOutpointMinted.selector));
        _assertClaim(1, session);
        assertTrue(_open(bytes32(uint256(99))) > session);
    }

    function testSyntheticConflictingAllocationRemainsConsumedWithoutRewritingClaim() public {
        uint256 session = _open(TXID);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        recipients[0] = BOB;
        pool.fixtureCompletedAllocation(TXID, VOUT, recipients, amounts);
        _retryFailure(session, abi.encodeWithSelector(BitcoinMintAdmission.MintOutpointMinted.selector));
        _assertClaim(1, session);
    }

    function testTypedOperatorTransferUsesSeparateNonceAndPreservesExistingMintSlot() public {
        uint256 mintSession = _open(TXID);
        (, bytes32 mintOp,,,) = admission.attempts(mintSession);
        vm.prank(BOB);
        uint256 transferSession = admission.openOperatorTransfer(BOB, deadline, 0, 0);
        vm.prank(CAROL);
        assertEq(admission.openOperatorTransfer(BOB, deadline, 0, transferSession), transferSession);
        bytes32 transferOp = admission.getTransferOperatorOperationHash(BOB);
        assertEq(_sessionWord(transferSession, 3), transferOp);
        assertEq(_sessionWord(transferSession, 15), bytes32(uint256(uint160(address(admission)))));
        authorization.fixtureApprove(transferOp, address(admission));
        vm.prank(address(0xFEED)); // An authorized operation can be relayed by anyone.
        admission.transferOperator(BOB, transferOp);
        assertEq(admission.operator(), BOB); assertEq(admission.operatorNonce(), 1); assertEq(pool.nonce(), 0);
        assertTrue(authorization.executedOperations(transferOp)); assertTrue(admission.active(mintSession));
        assertEq(admission.claimOperator(_claim()), ALICE);
        (, bytes32 unchangedOp,,,) = admission.attempts(mintSession); assertEq(unchangedOp, mintOp);
        (address[] memory recipients, uint256[] memory amounts) = _terms();
        vm.prank(CAROL);
        assertEq(admission.openMint(TXID, VOUT, recipients, amounts, deadline, 0), mintSession);
        vm.prank(ALICE); vm.expectRevert("operator");
        admission.openMint(bytes32(uint256(99)), VOUT, recipients, amounts, deadline, 0);
    }

    function testOperatorTransferAbortRetryUsesCanonicalPreviousSession() public {
        vm.prank(BOB);
        uint256 first = admission.openOperatorTransfer(BOB, deadline, 0, 0);
        coordinator.abortSession(first, "unit transfer cancellation");
        vm.prank(CAROL); admission.retireOperatorTransfer(first);
        assertEq(admission.latestOperatorTransferSession(0), first);
        vm.prank(CAROL); vm.expectRevert(BitcoinMintAdmission.MintAttemptChanged.selector);
        admission.openOperatorTransfer(BOB, deadline, 0, 0);
        vm.prank(BOB);
        uint256 second = admission.openOperatorTransfer(BOB, deadline, 0, first);
        coordinator.abortSession(second, "unit transfer second cancellation");
        vm.prank(CAROL); admission.retireOperatorTransfer(second);
        vm.prank(BOB);
        uint256 third = admission.openOperatorTransfer(BOB, deadline, 0, second);
        assertTrue(first < second && second < third);
        assertEq(admission.operatorNonce(), 0); assertEq(pool.nonce(), 0);
        assertTrue(admission.activeOperatorTransfer(third));
    }

    function testFinalizedOperatorTransferIsNotReleasedByExpiry() public {
        vm.prank(BOB);
        uint256 session = admission.openOperatorTransfer(BOB, deadline, 0, 0);
        _fixtureFinalizedObservation(session);
        vm.warp(deadline + 1);
        vm.prank(CAROL); vm.expectRevert(BitcoinMintAdmission.AdmissionAuthorizationStillExecutable.selector); admission.retireOperatorTransfer(session);
        assertTrue(admission.activeOperatorTransfer(session));
        bytes32 op = admission.getTransferOperatorOperationHash(BOB);
        authorization.fixtureApprove(op, address(admission));
        admission.transferOperator(BOB, op);
        assertEq(admission.operator(), BOB); assertEq(admission.operatorNonce(), 1); assertEq(pool.nonce(), 0);
    }

    function testOperatorTransferRequiresApprovalAndRejectsPreviousOperatorNonce() public {
        vm.prank(BOB);
        admission.openOperatorTransfer(BOB, deadline, 0, 0);
        bytes32 op = admission.getTransferOperatorOperationHash(BOB);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        admission.transferOperator(BOB, op);
        assertEq(admission.operator(), ALICE); assertEq(admission.operatorNonce(), 0);
        authorization.fixtureApprove(op, address(admission));
        admission.transferOperator(BOB, op);
        uint256 count = coordinator.nextSessionId();
        vm.prank(CAROL); vm.expectRevert(BitcoinMintAdmission.OperatorNonceChanged.selector);
        admission.openOperatorTransfer(ALICE, deadline, 0, 0);
        assertEq(coordinator.nextSessionId(), count); assertEq(pool.nonce(), 0);
    }
}
