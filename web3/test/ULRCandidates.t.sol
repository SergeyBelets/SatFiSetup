// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {CustodianDAOToken} from "../contracts/src/tokens/CustodianDAOToken.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";
import {ULRStaking} from "../contracts/src/staking/ULRStaking.sol";
import {ULRConfederationVoting} from "../contracts/src/staking/ULRConfederationVoting.sol";
import {ULRTreasuryVerifierFixture, ULRTreasuryCoordinatorFixture, ULRTreasuryUSD} from "./ULRTreasury.t.sol";

contract ULRCandidatesTest is Test {
    CustodianDAOToken private token;
    ULRTreasuryUSD private usd;
    ULRStaking private staking;
    CustodianValidator private validator;
    FROSTAuthorization private auth;
    ULRTreasuryCoordinatorFixture private coordinator;
    address private alice = address(0xA11CE);
    address private bob = address(0xB0B);
    address private custodian = address(0xC1);
    address private otherCustodian = address(0xC2);
    uint256 private session;
    uint256 private day = 101;

    function setUp() public {
        vm.warp(100 days + 12 hours);
        ULRTreasuryVerifierFixture verifier = new ULRTreasuryVerifierFixture();
        coordinator = new ULRTreasuryCoordinatorFixture(address(verifier), address(this));
        address[] memory signers = new address[](1);
        signers[0] = address(this);
        auth = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), signers, 1);
        address[] memory custodians = new address[](2);
        custodians[0] = custodian;
        custodians[1] = otherCustodian;
        validator = new CustodianValidator(custodians, 1, address(auth));
        token = new CustodianDAOToken("ULR", "ULR", address(this));
        usd = new ULRTreasuryUSD(6);
        staking = new ULRStaking(address(token), address(usd), 200_000 ether, 1);
        token.transfer(alice, 250_000 ether);
        token.transfer(bob, 150_000 ether);
        vm.prank(alice);
        token.approve(address(staking), type(uint256).max);
        vm.prank(bob);
        token.approve(address(staking), type(uint256).max);
    }

    function test_CustomLaunchThresholdBindsWithoutGrantingCandidateAuthority() public {
        staking = new ULRStaking(address(token), address(usd), 500_000 ether, 100 ether);
        vm.prank(alice);token.approve(address(staking),type(uint256).max);
        _stake(alice,100 ether);assertFalse(staking.isCustodianCandidate(alice));
        _bind();assertEq(validator.candidateStaking(),address(staking));
        token.transfer(alice,250_000 ether);assertTrue(staking.isCustodianCandidate(alice));
        assertFalse(validator.isCustodian(alice));
    }

    function test_InvalidCandidateThresholdRejectedBeforeAuthorization() public {
        vm.mockCall(address(staking),abi.encodeWithSignature("CUSTODIAN_THRESHOLD()"),abi.encode(uint256(0)));
        vm.expectRevert(CustodianValidator.InvalidAddress.selector);
        validator.bindCandidateStaking(address(staking),bytes32(0));
        vm.mockCall(address(staking),abi.encodeWithSignature("CUSTODIAN_THRESHOLD()"),abi.encode(uint256(10_000_001 ether)));
        vm.expectRevert(CustodianValidator.InvalidAddress.selector);
        validator.bindCandidateStaking(address(staking),bytes32(0));
    }

    function test_StakingNominatesQualifiedOwnerWithoutGrantingActiveAuthority() public {
        _stake(alice, 50_000 ether);
        assertEq(staking.custodianHoldings(alice), 250_000 ether);
        assertTrue(staking.isCustodianCandidate(alice));
        assertFalse(validator.isCustodian(alice));
        assertEq(validator.getCustodianCount(), 2);
        vm.prank(alice);
        vm.expectRevert(CustodianValidator.NotACustodian.selector);
        validator.registerCustodianSignature(keccak256("operation"));
        _bind();
        (address[] memory candidates, uint256 cursor) = validator.getCustodianCandidates(0, 100);
        assertEq(candidates.length, 1);
        assertEq(candidates[0], alice);
        assertEq(cursor, 1);
    }

    function test_WalletTransfersImmediatelyChangeEligibilityWithoutKeeperOrRestake() public {
        _stake(alice, 50_000 ether);
        vm.prank(alice);
        token.transfer(bob, 50_000 ether + 1);
        assertFalse(staking.isCustodianCandidate(alice));
        (address[] memory candidates,) = staking.getCustodianCandidates(0, 100);
        assertEq(candidates.length, 0);
        vm.prank(bob);
        token.transfer(alice, 1);
        assertTrue(staking.isCustodianCandidate(alice));
        assertEq(staking.candidateNomineeCount(), 1);
    }

    function test_UnstakeKeepsCandidateWhenTokensRemainInOwnersWallet() public {
        _stake(alice, 50_000 ether);
        vm.prank(alice);
        staking.unstake(50_000 ether);
        assertEq(staking.stakedBalance(alice), 0);
        assertTrue(staking.isCustodianCandidate(alice));
        assertEq(staking.custodianHoldings(alice), 250_000 ether);
    }

    function test_CandidatePagesBoundHistoricalScanAndDoNotDuplicateNominees() public {
        _stake(bob, 1 ether);
        _stake(alice, 1 ether);
        _stake(alice, 1 ether);
        assertEq(staking.candidateNomineeCount(), 2);
        (address[] memory first, uint256 next) = staking.getCustodianCandidates(0, 1);
        assertEq(first.length, 0);
        assertEq(next, 1);
        (address[] memory second, uint256 end) = staking.getCustodianCandidates(next, 1);
        assertEq(second.length, 1);
        assertEq(second[0], alice);
        assertEq(end, 2);
        vm.expectRevert(ULRStaking.InvalidCandidatePage.selector);
        staking.getCustodianCandidates(0, 101);
        vm.expectRevert(ULRStaking.InvalidCandidatePage.selector);
        staking.getCustodianCandidates(3, 1);
    }

    function test_BindingRequiresBothAuthoritiesAndCannotBeReplaced() public {
        bytes32 op = validator.getBindCandidateStakingOperationHash(address(staking));
        _authorize(op, address(validator));
        vm.expectRevert(CustodianValidator.ApprovalNotMet.selector);
        validator.bindCandidateStaking(address(staking), op);
        assertFalse(auth.executedOperations(op));
        vm.prank(custodian);
        validator.registerCustodianSignature(op);
        validator.bindCandidateStaking(address(staking), op);
        assertTrue(auth.executedOperations(op));
        vm.expectRevert(CustodianValidator.CandidateSourceAlreadyBound.selector);
        validator.bindCandidateStaking(address(staking), op);
    }

    function test_AdmissionRechecksLiveHoldingsAndPreservesApprovalUntilValid() public {
        _bind();
        _stake(alice, 50_000 ether);
        bytes32 op = validator.getAddCustodianOperationHash(alice);
        _approve(op);
        vm.prank(alice);
        token.transfer(bob, 50_000 ether + 1);
        vm.expectRevert(CustodianValidator.NotEligibleCandidate.selector);
        validator.addCustodian(alice, op);
        assertFalse(auth.executedOperations(op));
        vm.prank(bob);
        token.transfer(alice, 1);
        validator.addCustodian(alice, op);
        assertTrue(validator.isCustodian(alice));
        (address[] memory candidates,) = validator.getCustodianCandidates(0, 100);
        assertEq(candidates.length, 0);
    }

    function test_OnlyWorkingCustodianCanReceiveDelegationAndOwnerCanRevoke() public {
        _bind();
        _stake(alice, 50_000 ether);
        vm.startPrank(alice);
        vm.expectRevert(ULRStaking.InvalidVotingDelegate.selector);
        staking.setVotingDelegate(address(validator), bob);
        staking.setVotingDelegate(address(validator), custodian);
        assertEq(staking.getVotingDelegate(alice, address(validator)), custodian);
        staking.setVotingDelegate(address(validator), address(0));
        assertEq(staking.getVotingDelegate(alice, address(validator)), alice);
        vm.stopPrank();
    }

    function test_DelegateCannotWithdrawStakeOrClaimOwnersReward() public {
        _bind();
        _stake(alice, 50_000 ether);
        _stake(bob, 150_000 ether);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), custodian);
        vm.warp((day + 1) * 1 days);
        usd.mint(address(this), 400e6);
        usd.approve(address(staking), 400e6);
        staking.fundDay(day, 400e6);
        uint256[] memory days_ = new uint256[](1);
        days_[0] = day;
        vm.prank(custodian);
        assertEq(staking.claim(days_), 0);
        vm.prank(custodian);
        vm.expectRevert(ULRStaking.InvalidAmount.selector);
        staking.unstake(1);
        vm.prank(alice);
        assertEq(staking.claim(days_), 100e6);
        assertEq(usd.balanceOf(alice), 100e6);
        assertEq(usd.balanceOf(custodian), 0);
        assertEq(staking.stakedBalance(alice), 50_000 ether);
    }

    function test_OneDelegateVotesForSeveralOwnersOnceEachUsingCompletedDayStake() public {
        _bind();
        _stake(alice, 50_000 ether);
        _stake(bob, 150_000 ether);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), custodian);
        vm.prank(bob);
        staking.setVotingDelegate(address(validator), custodian);
        vm.warp((day + 1) * 1 days);
        ULRConfederationVoting voting = new ULRConfederationVoting(address(staking), address(validator));
        CustodianDAODecisionBoard board = new CustodianDAODecisionBoard(address(voting), address(auth));
        bytes32 op = board.getCreateOperationHash("ipfs://decision", keccak256("content"), 1 days, 5_000);
        _authorize(op, address(board));
        uint256 id = board.createDecision("ipfs://decision", keccak256("content"), 1 days, 5_000, op);
        vm.prank(alice);
        vm.expectRevert(CustodianDAODecisionBoard.UnauthorizedSigner.selector);
        board.vote(id, alice, CustodianDAODecisionBoard.Choice.For);
        vm.prank(custodian);
        board.vote(id, alice, CustodianDAODecisionBoard.Choice.For);
        vm.prank(custodian);
        board.vote(id, bob, CustodianDAODecisionBoard.Choice.For);
        vm.prank(custodian);
        vm.expectRevert(CustodianDAODecisionBoard.AlreadyVoted.selector);
        board.vote(id, alice, CustodianDAODecisionBoard.Choice.Against);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), address(0));
        vm.prank(alice);
        vm.expectRevert(CustodianDAODecisionBoard.AlreadyVoted.selector);
        board.vote(id, alice, CustodianDAODecisionBoard.Choice.Against);
        vm.warp(block.timestamp + 1 days);
        assertTrue(board.finalize(id));
        assertEq(validator.getCustodianCount(), 2);
    }

    function test_RemovedCustodianLosesDelegationButRewardsAndOwnerVotingRemain() public {
        _bind();
        _stake(alice, 50_000 ether);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), custodian);
        bytes32 op = validator.getRemoveCustodianOperationHash(custodian);
        _approve(op);
        validator.removeCustodian(custodian, op);
        assertEq(staking.getVotingDelegate(alice, address(validator)), alice);
        assertEq(staking.stakedBalance(alice), 50_000 ether);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), otherCustodian);
        assertEq(staking.getVotingDelegate(alice, address(validator)), otherCustodian);
    }

    function test_DelegatedStakeDoesNotIncreaseCustodianSignatureCountOrHoldings() public {
        _bind();
        _stake(alice, 50_000 ether);
        _stake(bob, 150_000 ether);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), custodian);
        vm.prank(bob);
        staking.setVotingDelegate(address(validator), custodian);
        bytes32 op = keccak256("threshold");
        vm.prank(custodian);
        validator.registerCustodianSignature(op);
        assertEq(validator.countCustodianSignatures(op), 1);
        assertEq(staking.custodianHoldings(custodian), 0);
        assertFalse(staking.isCustodianCandidate(custodian));
    }

    function test_DelegationIsIndependentForEachConfederationValidator() public {
        _bind();
        _stake(alice, 50_000 ether);
        address[] memory roster = new address[](1);
        roster[0] = otherCustodian;
        CustodianValidator second = new CustodianValidator(roster, 1, address(auth));
        bytes32 op = second.getBindCandidateStakingOperationHash(address(staking));
        _authorize(op, address(second));
        vm.prank(otherCustodian);
        second.registerCustodianSignature(op);
        second.bindCandidateStaking(address(staking), op);
        vm.startPrank(alice);
        staking.setVotingDelegate(address(validator), custodian);
        staking.setVotingDelegate(address(second), otherCustodian);
        vm.expectRevert(ULRStaking.InvalidVotingDelegate.selector);
        staking.setVotingDelegate(address(second), custodian);
        vm.stopPrank();
        assertEq(staking.getVotingDelegate(alice, address(validator)), custodian);
        assertEq(staking.getVotingDelegate(alice, address(second)), otherCustodian);
    }

    function test_RemovingAndReadmittingCustodianDoesNotReviveOldDelegation() public {
        _bind();
        _stake(alice, 50_000 ether);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), custodian);
        bytes32 op = validator.getRemoveCustodianOperationHash(custodian);
        _approve(op);
        validator.removeCustodian(custodian, op);
        token.transfer(custodian, 200_000 ether);
        vm.startPrank(custodian);
        token.approve(address(staking), 1);
        staking.stake(1);
        vm.stopPrank();
        op = validator.getAddCustodianOperationHash(custodian);
        _authorize(op, address(validator));
        vm.prank(otherCustodian);
        validator.registerCustodianSignature(op);
        validator.addCustodian(custodian, op);
        assertTrue(validator.isCustodian(custodian));
        assertEq(staking.getVotingDelegate(alice, address(validator)), alice);
        vm.prank(alice);
        staking.setVotingDelegate(address(validator), custodian);
        assertEq(staking.getVotingDelegate(alice, address(validator)), custodian);
    }

    function _stake(address owner, uint256 amount) private {
        vm.prank(owner);
        staking.stake(amount);
    }

    function _authorize(bytes32 op, address target) private {
        coordinator.set(op, target);
        auth.authorizeOperation(++session, op, target);
    }

    function _approve(bytes32 op) private {
        _authorize(op, address(validator));
        vm.prank(custodian);
        validator.registerCustodianSignature(op);
    }

    function _bind() private {
        bytes32 op = validator.getBindCandidateStakingOperationHash(address(staking));
        _approve(op);
        validator.bindCandidateStaking(address(staking), op);
    }
}
