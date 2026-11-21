// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {MockAggregateFROSTVerifier, MockAuthorizationCoordinator} from "./CriticalTests.t.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {ConfederationMultiPool} from "../contracts/src/federation/ConfederationMultiPool.sol";
import {ConfederationGovernance} from "../contracts/src/federation/ConfederationGovernance.sol";
import {ConfederationToken} from "../contracts/src/tokens/ConfederationToken.sol";

/// @dev Mock only finalized FROST signatures, as the existing unit suite does.
/// The authorization boundary, validator, governance and protected pool are real.
contract SecurityReviewGovernanceTest is Test {
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant CAROL = address(0xCA401);
    address constant MP = address(0x7777);
    MockAuthorizationCoordinator coordinator;
    FROSTAuthorization auth;
    CustodianValidator validator;
    ConfederationMultiPool pool;
    ConfederationGovernance governance;

    function setUp() public {
        vm.warp(10 days);
        coordinator = new MockAuthorizationCoordinator(address(new MockAggregateFROSTVerifier()));
        address[] memory participants = new address[](1);
        participants[0] = address(this);
        auth = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), participants, 1);
        address[] memory custodians = new address[](3);
        custodians[0] = ALICE;
        custodians[1] = BOB;
        custodians[2] = CAROL;
        validator = new CustodianValidator(custodians, 2, address(auth));
        address predictedPool = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        ConfederationToken shares = new ConfederationToken("Shares", "S", predictedPool);
        pool = new ConfederationMultiPool("security-review", address(auth), address(validator), address(shares));
        governance = new ConfederationGovernance("security-review", address(auth), address(validator), address(pool));
    }

    function _approve(bytes32 operationHash, address target) private {
        uint256 session = coordinator.createFinalizedAuthorization(
            auth.groupPubkey(), auth.getParticipants(), auth.threshold(), operationHash, target
        );
        auth.authorizeOperation(session, operationHash, target);
    }

    function _sign(address signer, bytes32 operationHash) private {
        vm.prank(signer);
        validator.registerCustodianSignature(operationHash);
    }

    function _removeAlice() private {
        bytes32 operationHash = validator.getRemoveCustodianOperationHash(ALICE);
        _approve(operationHash, address(validator));
        _sign(BOB, operationHash);
        _sign(CAROL, operationHash);
        validator.removeCustodian(ALICE, operationHash);
    }

    function testRemovedCustodianCannotSatisfyPendingPoolApproval() public {
        bytes32 operationHash = pool.getAddPoolOperationHash(MP, "pool");
        _approve(operationHash, address(pool));
        _sign(ALICE, operationHash);
        _sign(BOB, operationHash);
        _removeAlice();
        assertFalse(validator.isCustodian(ALICE));
        vm.expectRevert(ConfederationMultiPool.InsufficientCustodianSignatures.selector);
        pool.addPool(MP, "pool", operationHash);
    }

    function testCountExcludesRemovedCustodian() public {
        bytes32 operationHash = keccak256("pending operation");
        _sign(ALICE, operationHash);
        _sign(BOB, operationHash);
        _removeAlice();
        assertEq(validator.countCustodianSignatures(operationHash), 1);
        assertFalse(validator.meetsMinimumCustodians(operationHash));
        assertFalse(validator.validateCustodianThreshold(operationHash));
    }

    function testPreRegisteredCustodianCanNotifyGovernance() public {
        uint256 id = governance.proposeAddPool(MP, "pool", "Add pool");
        bytes32 operationHash = pool.getAddPoolOperationHash(MP, "pool");
        _approve(operationHash, address(pool));
        _sign(ALICE, operationHash);
        _sign(BOB, operationHash);
        vm.prank(ALICE);
        governance.voteAsCustodian(id);
        assertEq(uint256(governance.getProposalState(id)), uint256(ConfederationGovernance.ProposalState.Succeeded));
    }

    function testDirectThresholdApprovalIsNotSubjectToGovernanceWrapperDelay() public {
        uint256 id = governance.proposeAddPool(MP, "pool", "Add pool");
        bytes32 operationHash = pool.getAddPoolOperationHash(MP, "pool");
        _approve(operationHash, address(pool));
        _sign(ALICE, operationHash);
        _sign(BOB, operationHash);
        governance.updateProposalState(id);
        vm.expectRevert(ConfederationGovernance.ExecutionTooEarly.selector);
        governance.execute(id);
        // Document the actual security boundary: direct threshold-authorized calls
        // remain supported. EXECUTION_DELAY is not a protocol-wide timelock.
        pool.addPool(MP, "pool", operationHash);
        assertTrue(pool.getPoolInfo(MP).isActive);
    }

    function testReaddedCustodianMustRegisterFreshApproval() public {
        bytes32 pendingOperation = pool.getAddPoolOperationHash(MP, "pool");
        _approve(pendingOperation, address(pool));
        _sign(ALICE, pendingOperation);
        _sign(BOB, pendingOperation);
        _removeAlice();
        bytes32 addOperation = validator.getAddCustodianOperationHash(ALICE);
        _approve(addOperation, address(validator));
        _sign(BOB, addOperation);
        _sign(CAROL, addOperation);
        validator.addCustodian(ALICE, addOperation);
        assertFalse(validator.hasCustodianSigned(pendingOperation, ALICE));
        assertEq(validator.countCustodianSignatures(pendingOperation), 1);
        vm.expectRevert(ConfederationMultiPool.InsufficientCustodianSignatures.selector);
        pool.addPool(MP, "pool", pendingOperation);
        _sign(ALICE, pendingOperation);
        pool.addPool(MP, "pool", pendingOperation);
        assertTrue(pool.getPoolInfo(MP).isActive);
    }

    function testGovernanceCannotCountUnregisteredCustodianVote() public {
        uint256 id = governance.proposeAddPool(MP, "pool", "Add pool");
        vm.prank(ALICE);
        vm.expectRevert("Custodian signature not registered");
        governance.voteAsCustodian(id);
    }
}
