// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {IConfederationFactory} from "../contracts/src/federation/interfaces/IConfederationFactory.sol";
import {CustodianDAOToken} from "../contracts/src/tokens/CustodianDAOToken.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAOFeeRouter} from "../contracts/src/federation/CustodianDAOFeeRouter.sol";
import {ULRStaking} from "../contracts/src/staking/ULRStaking.sol";
import {ULRCustodianSnapshots} from "../contracts/src/staking/ULRCustodianSnapshots.sol";
import {ULRConfederationTreasury} from "../contracts/src/staking/ULRConfederationTreasury.sol";
import {DeployULRCustodianSnapshots, DeployULRTreasury} from "../script/DeployULRTreasury.s.sol";
import {DeployULRVoting} from "../script/DeployULRVoting.s.sol";
import {ULRConfederationVoting} from "../contracts/src/staking/ULRConfederationVoting.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";

// Crypto/session-finalization fixtures; authorization consumption and validator logic are real.
contract ULRTreasuryVerifierFixture {
    function verify(bytes calldata, bytes32, bytes calldata, string calldata) external pure returns (bool) {
        return true;
    }
}

contract ULRTreasuryCoordinatorFixture {
    address public immutable defaultVerifier;
    address private signer;
    bytes32 private message;
    address private target;

    constructor(address verifier, address signer_) {
        defaultVerifier = verifier;
        signer = signer_;
    }

    function set(bytes32 message_, address target_) external {
        message = message_;
        target = target_;
    }

    function getSessionParticipants(uint256) external view returns (address[] memory owners) {
        owners = new address[](1);
        owners[0] = signer;
    }

    function getAuthorizationResult(uint256)
        external
        view
        returns (bytes memory, bytes32, bool, address, address, uint256, uint256, bytes memory)
    {
        return (abi.encodePacked(bytes32(uint256(1))), message, true, address(0), target, 1, 1, hex"01");
    }
}

contract ULRTreasuryFactoryFixture {
    mapping(string => IConfederationFactory.ConfederationConfig) private configs;

    function configure(string calldata id, address auth, address validator) external {
        configs[id].isActive = true;
        configs[id].frostMultisig = auth;
        configs[id].custodianValidator = validator;
    }

    function setActive(string calldata id, bool active) external {
        configs[id].isActive = active;
    }

    function getConfederation(string calldata id)
        external
        view
        returns (IConfederationFactory.ConfederationConfig memory)
    {
        return configs[id];
    }
}

contract ULRTreasuryUSD is ERC20 {
    bool public tax;
    uint8 private immutable precision;

    constructor(uint8 decimals_) ERC20("USD fixture", "USD") {
        precision = decimals_;
    }

    function decimals() public view override returns (uint8) {
        return precision;
    }

    function mint(address owner, uint256 amount) external {
        _mint(owner, amount);
    }

    function setTax(bool active) external {
        tax = active;
    }

    function _update(address from, address to, uint256 amount) internal override {
        if (tax && from != address(0) && to != address(0) && amount > 0) {
            super._update(from, address(0), 1);
            amount--;
        }
        super._update(from, to, amount);
    }
}

contract ULRTreasuryTest is Test {
    CustodianDAOToken private ulr;
    ULRTreasuryUSD private usd;
    ULRStaking private staking;
    ULRCustodianSnapshots private snapshots;
    ULRConfederationTreasury private treasuryA;
    ULRConfederationTreasury private treasuryB;
    ULRTreasuryFactoryFixture private factory;
    FROSTAuthorization private auth;
    FROSTAuthorization private systemAuth;
    ULRTreasuryCoordinatorFixture private coordinator;
    CustodianValidator private validatorA;
    CustodianValidator private validatorB;
    address private alice = address(0xA11CE);
    address private bob = address(0xB0B);
    uint256 private day = 101;
    uint256 private session;

    function setUp() public {
        vm.warp(100 days + 12 hours);
        vm.roll(500);
        ULRTreasuryVerifierFixture verifier = new ULRTreasuryVerifierFixture();
        coordinator = new ULRTreasuryCoordinatorFixture(address(verifier), address(this));
        address[] memory signer = new address[](1);
        signer[0] = address(this);
        auth = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), signer, 1);
        systemAuth = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), signer, 1);
        signer[0] = alice;
        validatorA = new CustodianValidator(signer, 1, address(auth));
        signer[0] = bob;
        validatorB = new CustodianValidator(signer, 1, address(auth));
        factory = new ULRTreasuryFactoryFixture();
        factory.configure("A", address(auth), address(validatorA));
        factory.configure("B", address(auth), address(validatorB));
        ulr = new CustodianDAOToken("ULR", "ULR", address(this));
        usd = new ULRTreasuryUSD(6);
        staking = new ULRStaking(address(ulr), address(usd), 200_000 ether, 1);
        snapshots = new ULRCustodianSnapshots(address(staking), address(systemAuth));
        treasuryA = new ULRConfederationTreasury(address(factory), "A", address(snapshots), 1000 * 10**6, 9000);
        treasuryB = new ULRConfederationTreasury(address(factory), "B", address(snapshots), 1000 * 10**6, 9000);
        _enroll(treasuryA, "A", validatorA, alice);
        _enroll(treasuryB, "B", validatorB, bob);
        ulr.transfer(alice, 600_000 ether);
        ulr.transfer(bob, 400_000 ether);
        vm.startPrank(alice);
        ulr.approve(address(staking), type(uint256).max);
        staking.stake(100 ether);
        vm.stopPrank();
        vm.startPrank(bob);
        ulr.approve(address(staking), type(uint256).max);
        staking.stake(300 ether);
        vm.stopPrank();
        vm.warp((day + 1) * 1 days);
    }

    function test_TwoTreasuriesPayWeightedExcessAndLateClaim() public {
        _publish();
        usd.mint(address(treasuryA), 11_000e6);
        usd.mint(address(treasuryB), 6_000e6);
        assertEq(treasuryA.payDay(day), 5_400e6); // 10,000 * .9 * .6
        assertEq(_claim(alice), 1_350e6);
        assertEq(treasuryB.payDay(day), 1_800e6); // 5,000 * .9 * .4
        assertEq(_claim(alice), 450e6);
        assertEq(_claim(bob), 5_400e6);
        assertEq(usd.balanceOf(address(treasuryA)), 5_600e6);
        assertEq(usd.balanceOf(address(treasuryB)), 4_200e6);
        assertEq(staking.sourceDayFunding(day, address(treasuryA)), 5_400e6);
        assertEq(staking.sourceDayFunding(day, address(treasuryB)), 1_800e6);
        assertEq(usd.allowance(address(treasuryA), address(staking)), 0);
    }

    function test_DeployScriptsPinSharedGraphAndRefuseDuplicateEnrollment() public {
        vm.setEnv("EXPECTED_CHAIN_ID", vm.toString(block.chainid));
        vm.setEnv("PRIVATE_KEY", vm.toString(uint256(0x1234)));
        vm.setEnv("ULR_STAKING", vm.toString(address(staking)));
        vm.setEnv("ULR_STAKING_CODE_HASH", vm.toString(address(staking).codehash));
        vm.setEnv("ULR_SNAPSHOT_AUTHORIZATION", vm.toString(address(systemAuth)));
        vm.setEnv("ULR_SNAPSHOT_AUTHORIZATION_CODE_HASH", vm.toString(address(systemAuth).codehash));
        ULRCustodianSnapshots fresh = new DeployULRCustodianSnapshots().run();
        assertEq(address(fresh.staking()), address(staking));
        assertEq(address(fresh.authorization()), address(systemAuth));
        vm.setEnv("CONFEDERATION_FACTORY", vm.toString(address(factory)));
        vm.setEnv("CONFEDERATION_FACTORY_CODE_HASH", vm.toString(address(factory).codehash));
        vm.setEnv("CONFEDERATION_ID", "A");
        vm.setEnv("ULR_SNAPSHOTS", vm.toString(address(fresh)));
        vm.setEnv("ULR_SNAPSHOTS_CODE_HASH", vm.toString(address(fresh).codehash));
        ULRConfederationTreasury deployed = new DeployULRTreasury().run();
        assertEq(address(deployed.snapshots()), address(fresh));
        assertEq(address(deployed.staking()), address(staking));
        vm.setEnv("ULR_SNAPSHOTS", vm.toString(address(snapshots)));
        vm.setEnv("ULR_SNAPSHOTS_CODE_HASH", vm.toString(address(snapshots).codehash));
        DeployULRTreasury duplicateScript = new DeployULRTreasury();
        vm.expectRevert("treasury already enrolled");
        duplicateScript.run();
    }

    function test_VotingDeployScriptUsesEnrolledTreasuryAndBoundCandidates() public {
        bytes32 op = validatorA.getBindCandidateStakingOperationHash(address(staking));
        _authorize(auth, op, address(validatorA));
        vm.prank(alice);
        validatorA.registerCustodianSignature(op);
        validatorA.bindCandidateStaking(address(staking), op);
        vm.setEnv("EXPECTED_CHAIN_ID", vm.toString(block.chainid));
        vm.setEnv("PRIVATE_KEY", vm.toString(uint256(0x1234)));
        vm.setEnv("ULR_TREASURY", vm.toString(address(treasuryA)));
        vm.setEnv("ULR_TREASURY_CODE_HASH", vm.toString(address(treasuryA).codehash));
        (ULRConfederationVoting voting, CustodianDAODecisionBoard board) = new DeployULRVoting().run();
        assertEq(address(voting.staking()), address(staking));
        assertEq(address(voting.validator()), address(validatorA));
        assertEq(address(board.staking()), address(voting));
        assertEq(address(board.authorization()), address(auth));
    }

    function test_RepeatPaymentReturnsReceiptWithoutSecondTransferIncludingAfterDayChange() public {
        _publish();
        usd.mint(address(treasuryA), 11_000e6);
        treasuryA.payDay(day);
        usd.mint(address(treasuryA), 1_000e6);
        assertEq(treasuryA.payDay(day), 5_400e6);
        vm.warp(block.timestamp + 1 days);
        assertEq(treasuryA.payDay(day), 5_400e6);
        assertEq(staking.dayReward(day), 5_400e6);
        assertEq(usd.balanceOf(address(treasuryA)), 6_600e6);
    }

    function test_NextDayRecalculatesRemainingTreasuryAndDoesNotCatchUpMissedDays() public {
        _publish();
        usd.mint(address(treasuryA), 11_000e6);
        treasuryA.payDay(day);
        ++day;
        vm.warp((day + 1) * 1 days);
        _publish();
        assertEq(treasuryA.payDay(day), 2_484e6); // (5600 - 1000) * .9 * .6
        vm.warp(block.timestamp + 2 days);
        vm.expectRevert(ULRConfederationTreasury.InvalidDay.selector);
        treasuryB.payDay(day);
    }

    function test_FloorAndNoStakersLeaveFundsInTreasury() public {
        _publish();
        usd.mint(address(treasuryA), 1_000e6);
        assertEq(treasuryA.payDay(day), 0);
        assertEq(usd.balanceOf(address(treasuryA)), 1_000e6);
        vm.prank(alice);
        staking.unstake(100 ether);
        vm.prank(bob);
        staking.unstake(300 ether);
        ++day;
        vm.warp((day + 1) * 1 days);
        _publish();
        usd.mint(address(treasuryA), 10_000e6);
        assertEq(treasuryA.payDay(day), 0);
        (, ULRConfederationTreasury.Outcome outcome,,,,,) = treasuryA.settlements(day);
        assertEq(uint256(outcome), uint256(ULRConfederationTreasury.Outcome.NoEligibleStake));
        assertEq(usd.balanceOf(address(treasuryA)), 11_000e6);
    }

    function test_RoundedZeroCanBeRetriedWithinWindowButCannotCatchUp() public {
        _publish();
        usd.mint(address(treasuryA), 1_000e6 + 1);
        assertEq(uint256(treasuryA.previewDay(day).outcome), uint256(ULRConfederationTreasury.Outcome.RoundedToZero));
        vm.prank(address(0xBAD));
        assertEq(treasuryA.payDay(day), 0);
        (bool processed,,,,,,) = treasuryA.settlements(day);
        assertFalse(processed);
        usd.mint(address(treasuryA), 1);
        assertEq(treasuryA.payDay(day), 1);
        assertEq(treasuryA.payDay(day), 1);
        assertEq(staking.dayReward(day), 1);

        usd.mint(address(treasuryB), 1_000e6 + 1);
        assertEq(treasuryB.payDay(day), 0);
        vm.warp(block.timestamp + 1 days);
        usd.mint(address(treasuryB), 1_000e6);
        vm.expectRevert(ULRConfederationTreasury.InvalidDay.selector);
        treasuryB.payDay(day);
    }

    function test_MissingSnapshotAndFutureDayDoNotConsumePayment() public {
        usd.mint(address(treasuryA), 11_000e6);
        vm.expectRevert(ULRConfederationTreasury.SnapshotRequired.selector);
        treasuryA.payDay(day);
        vm.expectRevert(ULRConfederationTreasury.InvalidDay.selector);
        treasuryA.payDay(day + 1);
        vm.expectRevert(ULRConfederationTreasury.InvalidDay.selector);
        treasuryA.payDay(type(uint256).max);
        _publish();
        assertEq(treasuryA.payDay(day), 5_400e6);
    }

    function test_CanonicalEnrollmentRequiresBothApprovalsAndCannotBeDuplicated() public {
        factory.configure("C", address(auth), address(validatorA));
        ULRConfederationTreasury other = new ULRConfederationTreasury(address(factory), "C", address(snapshots), 1000 * 10**6, 9000);
        bytes32 op = snapshots.getEnrollTreasuryOperationHash(address(factory), "C", address(other));
        _authorize(auth, op, address(snapshots));
        vm.expectRevert(ULRCustodianSnapshots.CustodianApprovalRequired.selector);
        snapshots.enrollTreasury(address(factory), "C", address(other), op);
        assertFalse(auth.executedOperations(op));
        vm.prank(alice);
        validatorA.registerCustodianSignature(op);
        snapshots.enrollTreasury(address(factory), "C", address(other), op);
        assertTrue(auth.executedOperations(op));
        vm.expectRevert(ULRCustodianSnapshots.AlreadyEnrolled.selector);
        snapshots.enrollTreasury(address(factory), "C", address(other), op);
        ULRConfederationTreasury clone = new ULRConfederationTreasury(address(factory), "A", address(snapshots), 1000 * 10**6, 9000);
        vm.expectRevert(ULRConfederationTreasury.NotEnrolled.selector);
        clone.payDay(day);
    }

    function test_SnapshotCannotBePublishedWithoutSystemAuthorizationOrReplaced() public {
        (
            ULRCustodianSnapshots.Report memory report,
            address[] memory owners,
            uint256[] memory weights,
            ULRCustodianSnapshots.Allocation[] memory allocations
        ) = _report();
        bytes32 op = snapshots.getPublishDayOperationHash(report, owners, weights, allocations);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        snapshots.publishDay(report, owners, weights, allocations, op);
        _publish();
        vm.expectRevert(ULRCustodianSnapshots.AlreadyPublished.selector);
        snapshots.publishDay(report, owners, weights, allocations, op);
    }

    function test_SnapshotRequiresEveryTreasuryActivatedByThatUtcDay() public {
        (
            ULRCustodianSnapshots.Report memory report,
            address[] memory owners,
            uint256[] memory weights,
            ULRCustodianSnapshots.Allocation[] memory allocations
        ) = _report();
        factory.configure("C", address(auth), address(validatorA));
        ULRConfederationTreasury treasuryC = new ULRConfederationTreasury(
            address(factory), "C", address(snapshots), 1000e6, 9000
        );
        _enroll(treasuryC, "C", validatorA, alice);
        assertEq(snapshots.snapshotTreasuries(day).length, 2);
        assertEq(snapshots.snapshotTreasuries(day + 1).length, 3);

        ULRCustodianSnapshots.Allocation[] memory incomplete = new ULRCustodianSnapshots.Allocation[](1);
        incomplete[0] = allocations[0];
        bytes32 incompleteOp = snapshots.getPublishDayOperationHash(report, owners, weights, incomplete);
        _authorize(systemAuth, incompleteOp, address(snapshots));
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, incomplete, incompleteOp);

        bytes32 late = allocations[1].scope;
        allocations[1].scope = treasuryC.scope();
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, bytes32(0));
        allocations[1].scope = late;
        _publish();
        (bool included,,) = snapshots.confederations(day, treasuryC.scope());
        assertFalse(included);
    }

    function test_SnapshotRejectsDuplicateOwnersSubthresholdWeightsInvalidIndicesAndExcessSupply() public {
        (
            ULRCustodianSnapshots.Report memory report,
            address[] memory owners,
            uint256[] memory weights,
            ULRCustodianSnapshots.Allocation[] memory allocations
        ) = _report();
        owners[1] = owners[0];
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
        owners[1] = alice;
        weights[0] = 199_999 ether;
        bytes memory firstIndices = allocations[0].custodianIndices;
        bytes memory secondIndices = allocations[1].custodianIndices;
        allocations[0].custodianIndices = new bytes(0);
        allocations[1].custodianIndices = new bytes(0);
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
        allocations[0].custodianIndices = firstIndices;
        allocations[1].custodianIndices = secondIndices;
        weights[0] = 9_900_000 ether;
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
        weights[0] = 400_000 ether;
        allocations[0].custodianIndices = hex"00";
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
        allocations[0].custodianIndices = _index(2);
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
    }

    function test_MoreThanFiftyEligibleOwnersIncludingActiveResiduals() public {
        (ULRCustodianSnapshots.Report memory report,,,) = _report();
        address[] memory owners = new address[](51);
        uint256[] memory weights = new uint256[](51);
        for (uint256 i; i < owners.length; ++i) {
            owners[i] = address(uint160(1000 + i));
            weights[i] = i < 49 ? 200_000 ether : 100_000 ether;
        }
        uint256[] memory indices = new uint256[](2);
        indices[0] = 49;
        indices[1] = 50;
        (ULRCustodianSnapshots.Allocation[] memory allocations,) = _allocatedToA(_pack(indices));
        bytes32 op = snapshots.getPublishDayOperationHash(report, owners, weights, allocations);
        _authorize(systemAuth, op, address(snapshots));
        snapshots.publishDay(report, owners, weights, allocations, op);
        (, uint256 total,,,,) = snapshots.daySnapshots(day);
        (, uint256 weight,) = snapshots.confederations(day, treasuryA.scope());
        assertEq(total, 10_000_000 ether);
        assertEq(weight, 200_000 ether);
        usd.mint(address(treasuryA), 11_000e6);
        assertEq(treasuryA.payDay(day), 180e6);
    }

    function test_CustodianIndicesBeyond64And256AndDuplicateProtection() public {
        (ULRCustodianSnapshots.Report memory report,,,) = _report();
        address[] memory owners = new address[](300);
        uint256[] memory weights = new uint256[](300);
        uint256[] memory indices = new uint256[](300);
        for (uint256 i; i < owners.length; ++i) {
            owners[i] = address(uint160(1000 + i));
            weights[i] = 1 ether;
            indices[i] = i;
        }
        (ULRCustodianSnapshots.Allocation[] memory allocations, uint256 treasuryIndex) = _allocatedToA(_pack(indices));
        indices[299] = 298;
        allocations[treasuryIndex].custodianIndices = _pack(indices);
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
        indices[299] = 297;
        allocations[treasuryIndex].custodianIndices = _pack(indices);
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
        indices[299] = 300;
        allocations[treasuryIndex].custodianIndices = _pack(indices);
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, owners, weights, allocations, 0);
        indices[299] = 299;
        allocations[treasuryIndex].custodianIndices = _pack(indices);
        bytes32 op = snapshots.getPublishDayOperationHash(report, owners, weights, allocations);
        _authorize(systemAuth, op, address(snapshots));
        snapshots.publishDay(report, owners, weights, allocations, op);
        (, uint256 total,,,,) = snapshots.daySnapshots(day);
        (, uint256 weight,) = snapshots.confederations(day, treasuryA.scope());
        assertEq(total, 300 ether);
        assertEq(weight, total);
    }

    function test_MaximumHolderInventoryIsBoundedAndFitsTransactionGas() public {
        (ULRCustodianSnapshots.Report memory report,,,) = _report();
        address[] memory oversized = new address[](4097);
        uint256[] memory tooManyWeights = new uint256[](4097);
        (ULRCustodianSnapshots.Allocation[] memory allocations, uint256 treasuryIndex) = _allocatedToA("");
        vm.expectRevert(ULRCustodianSnapshots.InvalidReport.selector);
        snapshots.publishDay(report, oversized, tooManyWeights, allocations, 0);
        uint256 count = snapshots.MAX_HOLDERS();
        address[] memory owners = new address[](count);
        uint256[] memory weights = new uint256[](count);
        uint256[] memory indices = new uint256[](count);
        for (uint256 i; i < count; ++i) {
            owners[i] = address(uint160(1000 + i));
            weights[i] = 1 ether;
            indices[i] = i;
        }
        allocations[treasuryIndex].custodianIndices = _pack(indices);
        bytes32 op = snapshots.getPublishDayOperationHash(report, owners, weights, allocations);
        _authorize(systemAuth, op, address(snapshots));
        bytes memory txData = abi.encodeCall(snapshots.publishDay, (report, owners, weights, allocations, op));
        uint256 intrinsic = 21_000;
        for (uint256 i; i < txData.length; ++i) intrinsic += txData[i] == 0 ? 4 : 16;
        uint256 gasBefore = gasleft();
        (bool success,) = address(snapshots).call(txData);
        uint256 used = gasBefore - gasleft();
        assertTrue(success);
        emit log_named_uint("publishDay4096 call plus intrinsic gas", used + intrinsic);
        assertLt(used + intrinsic, 16_777_216);
        (, uint256 total,,,,) = snapshots.daySnapshots(day);
        assertEq(total, count * 1 ether);
    }

    function test_EmptySystemSnapshotRecordsZeroPayoutWithoutDivision() public {
        (ULRCustodianSnapshots.Report memory report,,, ULRCustodianSnapshots.Allocation[] memory allocations) =
            _report();
        address[] memory owners = new address[](0);
        uint256[] memory weights = new uint256[](0);
        allocations[0].custodianIndices = new bytes(0);
        allocations[1].custodianIndices = new bytes(0);
        bytes32 op = snapshots.getPublishDayOperationHash(report, owners, weights, allocations);
        _authorize(systemAuth, op, address(snapshots));
        snapshots.publishDay(report, owners, weights, allocations, op);
        usd.mint(address(treasuryA), 11_000e6);
        assertEq(treasuryA.payDay(day), 0);
        assertEq(usd.balanceOf(address(treasuryA)), 11_000e6);
    }

    function test_ActiveCustodianBelowThresholdRetainsActualIncomeWeight() public {
        (
            ULRCustodianSnapshots.Report memory report,
            address[] memory owners,
            uint256[] memory weights,
            ULRCustodianSnapshots.Allocation[] memory allocations
        ) = _report();
        weights[0] = 50_000 ether;
        bytes32 op = snapshots.getPublishDayOperationHash(report, owners, weights, allocations);
        _authorize(systemAuth, op, address(snapshots));
        snapshots.publishDay(report, owners, weights, allocations, op);
        usd.mint(address(treasuryB), 11_000e6);
        assertEq(treasuryB.payDay(day), uint256(10_000e6) * 9 * 50_000 / (10 * 650_000));
        (, uint256 weight,) = snapshots.confederations(day, treasuryB.scope());
        assertEq(weight, 50_000 ether);
    }

    function test_TaxedPayoutRevertsAllStateAndCanBeRetried() public {
        _publish();
        usd.mint(address(treasuryA), 11_000e6);
        usd.setTax(true);
        vm.expectRevert(ULRStaking.InvalidTransfer.selector);
        treasuryA.payDay(day);
        (bool processed,,,,,,) = treasuryA.settlements(day);
        assertFalse(processed);
        assertEq(staking.dayReward(day), 0);
        assertEq(usd.balanceOf(address(treasuryA)), 11_000e6);
        usd.setTax(false);
        assertEq(treasuryA.payDay(day), 5_400e6);
    }

    function test_OperationsRequireExactDualApprovalAndCannotBypassDailyRoute() public {
        usd.mint(address(treasuryA), 1_000e6);
        uint256 deadline = block.timestamp + 1 hours;
        bytes32 op = treasuryA.getSpendOperationHash(alice, 100e6, deadline);
        _authorize(auth, op, address(treasuryA));
        vm.expectRevert(ULRConfederationTreasury.ApprovalRequired.selector);
        treasuryA.spend(alice, 100e6, deadline, op);
        vm.prank(alice);
        validatorA.registerCustodianSignature(op);
        treasuryA.spend(alice, 100e6, deadline, op);
        assertEq(usd.balanceOf(alice), 100e6);
        assertEq(usd.balanceOf(address(treasuryA)), 900e6);
        vm.expectRevert(ULRConfederationTreasury.InvalidBinding.selector);
        treasuryA.spend(address(staking), 1, deadline, 0);
        vm.expectRevert(ULRConfederationTreasury.InvalidOperation.selector);
        treasuryA.spend(alice, 100e6, deadline, op);
    }

    function test_InactiveConfederationCannotPay() public {
        _publish();
        usd.mint(address(treasuryA), 11_000e6);
        factory.setActive("A", false);
        vm.expectRevert(ULRConfederationTreasury.InvalidBinding.selector);
        treasuryA.payDay(day);
    }

    function test_FeeRouterCanSendGrossRealizedFeesToTreasuryAndStakers() public {
        CustodianDAOIncomeVault vault = new CustodianDAOIncomeVault(address(usd), address(auth));
        CustodianDAOFeeRouter router = new CustodianDAOFeeRouter(address(vault), alice, 5_000, 0);
        bytes32 op = router.getSetFeeSharesOperationHash(0, 0);
        _authorize(auth, op, address(router));
        router.setFeeShares(0, 0, op);
        op = router.getSetTreasuryOperationHash(address(treasuryA));
        _authorize(auth, op, address(router));
        router.setTreasury(address(treasuryA), op);
        op = router.getSetPayerOperationHash(address(this), true);
        _authorize(auth, op, address(router));
        router.setPayer(address(this), true, op);
        usd.mint(address(this), 11_000e6);
        usd.approve(address(router), 11_000e6);
        router.depositFeeV1(day, CustodianDAOFeeRouter.FeeStream.Wrap, 6_000e6);
        router.depositFeeV1(day, CustodianDAOFeeRouter.FeeStream.Unwrap, 5_000e6);
        assertEq(usd.balanceOf(address(treasuryA)), 11_000e6);
        assertEq(usd.balanceOf(address(vault)), 0);
        _publish();
        assertEq(treasuryA.payDay(day), 5_400e6);
        assertEq(_claim(alice), 1_350e6);
    }

    function testFuzz_FormulaConservesBalanceAndProtectsReserve(uint96 excessSeed) public {
        uint256 excess = bound(uint256(excessSeed), 1, 1e24);
        _publish();
        usd.mint(address(treasuryA), 1_000e6 + excess);
        uint256 paid = treasuryA.payDay(day);
        assertEq(paid, excess * 54 / 100);
        assertEq(usd.balanceOf(address(treasuryA)) + paid, 1_000e6 + excess);
        assertGe(usd.balanceOf(address(treasuryA)), 1_000e6);
        assertEq(staking.rewardLiability(), paid);
    }

    function _enroll(ULRConfederationTreasury treasury, string memory id, CustodianValidator validator, address owner)
        private
    {
        bytes32 op = snapshots.getEnrollTreasuryOperationHash(address(factory), id, address(treasury));
        _authorize(auth, op, address(snapshots));
        vm.prank(owner);
        validator.registerCustodianSignature(op);
        snapshots.enrollTreasury(address(factory), id, address(treasury), op);
        op = snapshots.getActivateTreasuryOperationHash(address(treasury));
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        snapshots.activateTreasury(address(treasury), op);
        _authorize(systemAuth, op, address(snapshots));
        snapshots.activateTreasury(address(treasury), op);
    }

    function _publish() private {
        (
            ULRCustodianSnapshots.Report memory report,
            address[] memory owners,
            uint256[] memory weights,
            ULRCustodianSnapshots.Allocation[] memory allocations
        ) = _report();
        bytes32 op = snapshots.getPublishDayOperationHash(report, owners, weights, allocations);
        _authorize(systemAuth, op, address(snapshots));
        snapshots.publishDay(report, owners, weights, allocations, op);
    }

    function _report()
        private
        view
        returns (
            ULRCustodianSnapshots.Report memory report,
            address[] memory owners,
            uint256[] memory weights,
            ULRCustodianSnapshots.Allocation[] memory allocations
        )
    {
        report = ULRCustodianSnapshots.Report(
            day, 1, keccak256("historical anchor fixture"), keccak256(abi.encode(day))
        );
        owners = new address[](2);
        weights = new uint256[](2);
        owners[0] = bob; // ascending address order
        owners[1] = alice;
        weights[0] = 400_000 ether;
        weights[1] = 600_000 ether;
        allocations = new ULRCustodianSnapshots.Allocation[](2);
        allocations[0] = ULRCustodianSnapshots.Allocation(treasuryA.scope(), _index(1), keccak256(abi.encode(alice)));
        allocations[1] = ULRCustodianSnapshots.Allocation(treasuryB.scope(), _index(0), keccak256(abi.encode(bob)));
        if (allocations[0].scope > allocations[1].scope) {
            ULRCustodianSnapshots.Allocation memory swap = allocations[0];
            allocations[0] = allocations[1];
            allocations[1] = swap;
        }
    }

    function _index(uint256 value) private pure returns (bytes memory) {
        return abi.encodePacked(uint16(value));
    }

    function _allocatedToA(bytes memory indices)
        private
        view
        returns (ULRCustodianSnapshots.Allocation[] memory allocations, uint256 treasuryIndex)
    {
        (,,, allocations) = _report();
        treasuryIndex = allocations[0].scope == treasuryA.scope() ? 0 : 1;
        allocations[treasuryIndex].custodianIndices = indices;
        allocations[1 - treasuryIndex].custodianIndices = new bytes(0);
    }

    function _pack(uint256[] memory indices) private pure returns (bytes memory packed) {
        packed = new bytes(indices.length * 2);
        for (uint256 i; i < indices.length; ++i) {
            packed[2 * i] = bytes1(uint8(indices[i] >> 8));
            packed[2 * i + 1] = bytes1(uint8(indices[i]));
        }
    }

    function _authorize(FROSTAuthorization authorization, bytes32 op, address target) private {
        coordinator.set(op, target);
        authorization.authorizeOperation(++session, op, target);
    }

    function _claim(address owner) private returns (uint256) {
        uint256[] memory list = new uint256[](1);
        list[0] = day;
        vm.prank(owner);
        return staking.claim(list);
    }
}
