// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Test} from "forge-std/Test.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {CustodianDAOToken} from "../contracts/src/tokens/CustodianDAOToken.sol";
import {CustodianDAOStaking} from "../contracts/src/federation/CustodianDAOStaking.sol";
import {CustodianDAORateController} from "../contracts/src/federation/CustodianDAORateController.sol";
import {CustodianDAODailySettlement} from "../contracts/src/federation/CustodianDAODailySettlement.sol";
import {ChainlinkBTCDailyTWAP} from "../contracts/src/federation/ChainlinkBTCDailyTWAP.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAOClaimAdapter} from "../contracts/src/federation/CustodianDAOClaimAdapter.sol";
import {CustodianDAOConversionAdapter} from "../contracts/src/federation/CustodianDAOConversionAdapter.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";
import {CustodianDAOFeeRouter} from "../contracts/src/federation/CustodianDAOFeeRouter.sol";
import {CustodianDAOSilo4626Adapter} from "../contracts/src/federation/CustodianDAOSilo4626Adapter.sol";
import {CurvePoolLiquidityGuard} from "../contracts/src/federation/CurvePoolLiquidityGuard.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {IConfederationFactory} from "../contracts/src/federation/interfaces/IConfederationFactory.sol";
import {DeployCustodianDAO} from "../script/DeployCustodianDAO.s.sol";
import {DeployCustodianDAOIncomeAdapter} from "../script/DeployCustodianDAOIncomeAdapter.s.sol";
import {SimpleMultiPool} from "../contracts/src/demo/SimpleMultiPool.sol";
import {MultiPoolShareToken} from "../contracts/src/tokens/MultiPoolShareToken.sol";
import {PoolMpToken} from "../contracts/src/tokens/PoolMpToken.sol";
import {FederationFeeManager} from "../contracts/src/federation/FederationFeeManager.sol";
import {IFeeManager} from "../contracts/src/federation/IFeeManager.sol";

import {DAOTestUSD} from "../contracts/src/demo/DAOTestUSD.sol";
import {DAOMockFeed} from "../contracts/src/demo/DAOMockFeed.sol";

contract DAOMockVerifier {
    function verify(bytes calldata, bytes32, bytes calldata, string calldata) external pure returns (bool) {
        return true;
    }
}

contract DAOMockCoordinator {
    address public immutable defaultVerifier;
    bytes32 public messageHash;
    address public origin;
    address public signer;

    constructor(address verifier, address signer_) {
        defaultVerifier = verifier;
        signer = signer_;
    }

    function setResult(bytes32 hash, address target) external {
        messageHash = hash;
        origin = target;
    }

    function getSessionParticipants(uint256) external view returns (address[] memory participants) {
        participants = new address[](1);
        participants[0] = signer;
    }

    function getAuthorizationResult(uint256)
        external
        view
        returns (bytes memory, bytes32, bool, address, address, uint256, uint256, bytes memory)
    {
        return (abi.encodePacked(bytes32(uint256(1))), messageHash, true, address(0), origin, 1, 1, hex"01");
    }
}

contract DAOMockFactory {
    address public immutable frostCoordinator;
    address public immutable authorization;
    address public immutable validator;

    constructor(address coordinator_, address authorization_, address validator_) {
        frostCoordinator = coordinator_;
        authorization = authorization_;
        validator = validator_;
    }

    function getConfederation(string calldata)
        external
        view
        returns (IConfederationFactory.ConfederationConfig memory config)
    {
        config.isActive = true;
        config.frostMultisig = authorization;
        config.custodianValidator = validator;
    }

    function getRegisterCustodianDAOOperationHash(string calldata id, IConfederationFactory.CustodianDAOConfig calldata dao)
        external view returns (bytes32)
    {
        return FROSTAuthorization(authorization).hashOperation(
            address(this), IConfederationFactory.registerCustodianDAO.selector, keccak256(abi.encode(id, dao)), 0
        );
    }

    function getCustodianDAO(string calldata) external pure returns (IConfederationFactory.CustodianDAOConfig memory dao) {}
}

contract DAOMockCurvePool {}

contract DAOMockClaimProtocol {
    DAOTestUSD public immutable rewardToken;

    constructor(address rewardToken_) {
        rewardToken = DAOTestUSD(rewardToken_);
    }

    function claim(uint256 amount, address recipient) external {
        if (amount > 0) rewardToken.mint(recipient, amount);
    }
}

contract DAOMockSiloVault is ERC4626 {
    constructor(address asset_) ERC20("Silo Shares", "ssUSD") ERC4626(IERC20(asset_)) {}
}

contract DAOMockConverter {
    function swap(address input, address output, uint256 amountIn, address recipient, uint256 amountOut) external {
        IERC20(input).transferFrom(msg.sender, address(this), amountIn);
        IERC20(output).transfer(recipient, amountOut);
    }
}

contract CustodianDAOTest is Test {
    CustodianDAOToken token;
    DAOTestUSD usd;
    CustodianValidator validator;
    CustodianDAOStaking staking;
    CustodianDAORateController controller;
    DAOMockFeed feed;
    ChainlinkBTCDailyTWAP twap;
    DAOMockCoordinator coordinator;
    FROSTAuthorization authorization;
    CustodianDAOIncomeVault incomeVault;
    CustodianDAODailySettlement settlement;
    address[] custodians;
    uint256 startDay;

    function setUp() public {
        vm.warp(1000 days + 12 hours);
        startDay = block.timestamp / 1 days;
        token = new CustodianDAOToken("Custodian Governance", "CGOV", address(this));
        usd = new DAOTestUSD();
        custodians = new address[](11);
        for (uint256 i = 0; i < 11; i++) {
            custodians[i] = address(uint160(0x1000 + i));
        }
        validator = new CustodianValidator(custodians, 7, address(0x1234));
        staking = new CustodianDAOStaking(address(token), address(usd), address(validator));
        controller = new CustodianDAORateController(address(validator), address(staking), true);
        feed = new DAOMockFeed();
        twap = new ChainlinkBTCDailyTWAP(address(feed));
        DAOMockVerifier verifier = new DAOMockVerifier();
        coordinator = new DAOMockCoordinator(address(verifier), custodians[0]);
        address[] memory signers = new address[](1);
        signers[0] = custodians[0];
        authorization = new FROSTAuthorization(address(coordinator), abi.encodePacked(bytes32(uint256(1))), signers, 1);
        incomeVault = new CustodianDAOIncomeVault(address(usd), address(authorization));
        settlement = new CustodianDAODailySettlement(
            address(usd),
            address(staking),
            address(controller),
            address(authorization),
            address(twap),
            address(incomeVault)
        , address(0xBEEF));
        staking.setDistributor(address(settlement));
        incomeVault.setSettlement(address(settlement));
        _registerSource(keccak256("YIELD_BASIS"), CustodianDAOIncomeVault.Stream.Yield, 10);
        _registerSource(incomeVault.WRAP_FEE(), CustodianDAOIncomeVault.Stream.WrapFee, 11);
        _registerSource(incomeVault.DEPLOY_FEE(), CustodianDAOIncomeVault.Stream.DeployFee, 12);
    }

    function test_FixedSupplyAndMetadata() public view {
        assertEq(token.totalSupply(), 10_000_000 ether);
        assertEq(token.balanceOf(address(this)), token.totalSupply());
        assertEq(token.name(), "Custodian Governance");
        assertEq(token.symbol(), "CGOV");
    }

    function test_ConfiguredRewardTokenIsSharedAcrossIncomeAndPayout() public view {
        assertEq(address(staking.rewardToken()), address(usd));
        assertEq(address(incomeVault.rewardToken()), address(usd));
        assertEq(address(settlement.rewardToken()), address(usd));
        assertEq(settlement.rewardScale(), 1e6);
    }

    function test_DeployScriptBuildsConfiguredConfederationDAO() public {
        _configureDAODeployment();
        DeployCustodianDAO script = new DeployCustodianDAO();
        _assertDAODeploymentPreflight(script);
        script.run();
        (address daoToken, address daoStaking, address daoController, address daoTwap, address daoVault, address daoBoard, address daoSettlement) = script.deployedDAO();
        assertEq(CustodianDAOStaking(daoStaking).distributor(), daoSettlement);
        assertEq(address(CustodianDAOStaking(daoStaking).stakeToken()), daoToken);
        assertEq(address(CustodianDAODailySettlement(daoSettlement).controller()), daoController);
        assertEq(address(CustodianDAODailySettlement(daoSettlement).twap()), daoTwap);
        assertEq(address(CustodianDAODailySettlement(daoSettlement).incomeVault()), daoVault);
        assertEq(address(CustodianDAODecisionBoard(daoBoard).staking()), daoStaking);
    }

    function _assertDAODeploymentPreflight(DeployCustodianDAO script) private {
        uint256 initialNonce = vm.getNonce(vm.addr(1));
        vm.setEnv("BTC_USD_CHAINLINK_FEED", vm.toString(address(0xBAD)));
        vm.expectRevert("BTC/USD feed missing");
        script.run();
        vm.setEnv("BTC_USD_CHAINLINK_FEED", vm.toString(address(usd)));
        vm.expectRevert("unsupported BTC/USD feed decimals");
        script.run();
        vm.setEnv("BTC_USD_CHAINLINK_FEED", vm.toString(address(feed)));
        vm.setEnv("DAO_TOKEN_RECIPIENT", vm.toString(address(0)));
        vm.expectRevert("token recipient missing");
        script.run();
        assertEq(vm.getNonce(vm.addr(1)), initialNonce);
        vm.setEnv("DAO_TOKEN_RECIPIENT", vm.toString(address(this)));
    }

    function _configureDAODeployment() private {
        vm.setEnv("EXPECTED_CHAIN_ID", vm.toString(block.chainid));
        CustodianValidator deployedValidator = new CustodianValidator(custodians, 7, address(authorization));
        DAOMockFactory factory =
            new DAOMockFactory(address(coordinator), address(authorization), address(deployedValidator));
        vm.setEnv("PRIVATE_KEY", "1");
        vm.setEnv("CONFEDERATION_FACTORY", vm.toString(address(factory)));
        vm.setEnv("CONFEDERATION_ID", "conf-a");
        vm.setEnv("DAO_TOKEN_NAME", "Configured DAO");
        vm.setEnv("DAO_TOKEN_SYMBOL", "CDAO");
        vm.setEnv("DAO_TOKEN_RECIPIENT", vm.toString(address(this)));
        vm.setEnv("DAO_REWARD_TOKEN", vm.toString(address(usd)));
        vm.setEnv("BTC_USD_CHAINLINK_FEED", vm.toString(address(feed)));
        vm.setEnv("DAO_USE_RESERVE", "true");
        vm.setEnv("DAO_TREASURY", vm.toString(address(0xBEEF)));
    }

    function test_IncomeAdapterDeployScriptUsesSelectedMarketAndSource() public {
        vm.setEnv("EXPECTED_CHAIN_ID", vm.toString(block.chainid));
        DAOMockConverter converter = new DAOMockConverter();
        vm.setEnv("PRIVATE_KEY", "1");
        vm.setEnv("DAO_INCOME_VAULT", vm.toString(address(incomeVault)));
        vm.setEnv("DAO_ADAPTER_KIND", "CONVERSION");
        vm.setEnv("DAO_SOURCE_NAME", "WRAP_FEE");
        vm.setEnv("DAO_SOURCE_STREAM", "1");
        vm.setEnv("DAO_INPUT_TOKEN", vm.toString(address(token)));
        vm.setEnv("DAO_CONVERTER", vm.toString(address(converter)));
        vm.setEnv("DAO_LIQUIDITY_GUARD", vm.toString(address(0)));
        vm.setEnv("DAO_MINIMUM_LIQUIDITY", "0");
        DeployCustodianDAOIncomeAdapter script = new DeployCustodianDAOIncomeAdapter();
        address adapter = script.run();
        assertEq(address(CustodianDAOConversionAdapter(adapter).inputToken()), address(token));
        assertEq(address(CustodianDAOConversionAdapter(adapter).rewardToken()), address(usd));
        assertEq(CustodianDAOConversionAdapter(adapter).sourceId(), incomeVault.WRAP_FEE());

        vm.setEnv("DAO_SOURCE_NAME", "UNWRAP_FEE");
        vm.setEnv("DAO_SOURCE_STREAM", "3");
        address unwrapAdapter = script.run();
        assertEq(CustodianDAOConversionAdapter(unwrapAdapter).sourceId(), incomeVault.UNWRAP_FEE());
        vm.setEnv("DAO_SOURCE_STREAM", "1");
        vm.expectRevert("invalid unwrap stream");
        script.run();
        vm.setEnv("DAO_SOURCE_STREAM", "4");
        vm.expectRevert("invalid source stream");
        script.run();

        vm.setEnv("DAO_SOURCE_NAME", "CURVE");
        vm.setEnv("DAO_SOURCE_STREAM", "0");
        vm.expectRevert("invalid Curve source");
        script.run();

        DAOMockClaimProtocol protocol = new DAOMockClaimProtocol(address(usd));
        vm.setEnv("DAO_ADAPTER_KIND", "CLAIM");
        vm.setEnv("DAO_SOURCE_NAME", "YIELD_BASIS");
        vm.setEnv("DAO_CLAIM_PROTOCOL", vm.toString(address(protocol)));
        address claimAdapter = script.run();
        assertEq(CustodianDAOClaimAdapter(claimAdapter).protocol(), address(protocol));

        DAOMockSiloVault silo = new DAOMockSiloVault(address(usd));
        vm.setEnv("DAO_ADAPTER_KIND", "SILO_ERC4626");
        vm.setEnv("DAO_SOURCE_NAME", "SILO");
        vm.setEnv("DAO_SILO_VAULT", vm.toString(address(silo)));
        address siloAdapter = script.run();
        assertEq(address(CustodianDAOSilo4626Adapter(siloAdapter).siloVault()), address(silo));
    }

    function test_StakerDecisionUsesCompletedDaySnapshotAndCurrentDelegatedSigner() public {
        address alice = address(0xAAA1);
        address bob = address(0xBBB1);
        address custodianSigner = custodians[0];
        token.transfer(alice, 100 ether);
        token.transfer(bob, 50 ether);
        vm.startPrank(alice);
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        vm.stopPrank();
        vm.startPrank(bob);
        token.approve(address(staking), 50 ether);
        staking.stake(50 ether);
        staking.setDAOSigner(custodianSigner, "");
        vm.stopPrank();

        CustodianDAODecisionBoard board = new CustodianDAODecisionBoard(address(staking), address(authorization));
        vm.warp((startDay + 2) * 1 days);
        bytes32 contentHash = keccak256("ordinary-staker-decision");
        bytes32 createHash = board.getCreateOperationHash("ipfs://dao-decision", contentHash, 1 days, 5_000);
        coordinator.setResult(createHash, address(board));
        authorization.authorizeOperation(13, createHash, address(board));
        uint256 id = board.createDecision("ipfs://dao-decision", contentHash, 1 days, 5_000, createHash);

        vm.prank(alice);
        staking.unstake(100 ether);
        assertTrue(board.canVote(id, alice));
        vm.prank(alice);
        board.vote(id, alice, CustodianDAODecisionBoard.Choice.For);
        vm.prank(custodianSigner);
        board.vote(id, bob, CustodianDAODecisionBoard.Choice.Against);
        vm.prank(bob);
        staking.setDAOSigner(bob, "");
        vm.prank(bob);
        vm.expectRevert(CustodianDAODecisionBoard.AlreadyVoted.selector);
        board.vote(id, bob, CustodianDAODecisionBoard.Choice.For);

        vm.warp(block.timestamp + 1 days);
        assertTrue(board.finalize(id));
        assertEq(controller.rateForDay(block.timestamp / 1 days), 120);
        assertFalse(board.canVote(id, alice));
        vm.expectRevert(CustodianDAODecisionBoard.AlreadyFinalized.selector);
        board.finalize(id);
    }

    function test_StakerDecisionRejectsUnapprovedAndEmptySnapshot() public {
        CustodianDAODecisionBoard board = new CustodianDAODecisionBoard(address(staking), address(authorization));
        bytes32 contentHash = keccak256("decision");
        vm.expectRevert(CustodianDAODecisionBoard.NoEligibleStake.selector);
        board.createDecision("ipfs://decision", contentHash, 1 days, 0, bytes32(0));

        token.approve(address(staking), 1 ether);
        staking.stake(1 ether);
        vm.warp((startDay + 2) * 1 days);
        bytes32 hash = board.getCreateOperationHash("ipfs://decision", contentHash, 1 days, 10_000);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        board.createDecision("ipfs://decision", contentHash, 1 days, 10_000, hash);
        coordinator.setResult(hash, address(board));
        authorization.authorizeOperation(13, hash, address(board));
        uint256 id = board.createDecision("ipfs://decision", contentHash, 1 days, 10_000, hash);
        vm.expectRevert(CustodianDAODecisionBoard.VotingOpen.selector);
        board.finalize(id);
        vm.prank(address(0xBAD));
        vm.expectRevert(CustodianDAODecisionBoard.UnauthorizedSigner.selector);
        board.vote(id, address(this), CustodianDAODecisionBoard.Choice.For);
        board.vote(id, address(this), CustodianDAODecisionBoard.Choice.For);
        vm.warp(block.timestamp + 1 days);
        assertTrue(board.finalize(id));
    }

    function test_StakerDecisionApprovalExpiresWhenSnapshotDayChanges() public {
        token.approve(address(staking), 1 ether);
        staking.stake(1 ether);
        CustodianDAODecisionBoard board = new CustodianDAODecisionBoard(address(staking), address(authorization));
        vm.warp((startDay + 2) * 1 days);
        bytes32 contentHash = keccak256("time-bound-decision");
        bytes32 hash = board.getCreateOperationHash("ipfs://time-bound", contentHash, 1 days, 5_000);
        coordinator.setResult(hash, address(board));
        authorization.authorizeOperation(13, hash, address(board));
        vm.warp((startDay + 3) * 1 days);
        vm.expectRevert(CustodianDAODecisionBoard.InvalidOperationHash.selector);
        board.createDecision("ipfs://time-bound", contentHash, 1 days, 5_000, hash);
        assertFalse(authorization.executedOperations(hash));
    }

    function test_FeeRouterSplitsRealizedWrapAndDeployFeesIntoSeparateStreams() public {
        address treasury = address(0xFACE);
        CustodianDAOFeeRouter router = new CustodianDAOFeeRouter(address(incomeVault), treasury, 2_500, 5_000);
        bytes32 payerHash = router.getSetPayerOperationHash(address(this), true);
        coordinator.setResult(payerHash, address(router));
        authorization.authorizeOperation(13, payerHash, address(router));
        router.setPayer(address(this), true, payerHash);

        bytes32 wrapId = incomeVault.WRAP_FEE();
        bytes32 wrapHash = incomeVault.getSetSourceOperationHash(
            wrapId, address(router), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0
        );
        coordinator.setResult(wrapHash, address(incomeVault));
        authorization.authorizeOperation(14, wrapHash, address(incomeVault));
        incomeVault.setSource(
            wrapId, address(router), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0, wrapHash
        );
        bytes32 deployId = incomeVault.DEPLOY_FEE();
        bytes32 deployHash = incomeVault.getSetSourceOperationHash(
            deployId, address(router), CustodianDAOIncomeVault.Stream.DeployFee, true, address(0), 0
        );
        coordinator.setResult(deployHash, address(incomeVault));
        authorization.authorizeOperation(15, deployHash, address(incomeVault));
        incomeVault.setSource(
            deployId, address(router), CustodianDAOIncomeVault.Stream.DeployFee, true, address(0), 0, deployHash
        );

        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp(day * 1 days);
        usd.mint(address(this), 8e6);
        usd.approve(address(router), 8e6);
        assertEq(router.depositFee(day, true, 4e6), 1e6);
        assertEq(router.depositFee(day, false, 4e6), 2e6);
        assertEq(incomeVault.dayWrapFeeIncome(day), 1e6);
        assertEq(incomeVault.dayDeployFeeIncome(day), 2e6);
        assertEq(usd.balanceOf(treasury), 5e6);
        assertEq(usd.balanceOf(address(router)), 0);

        vm.warp((day + 1) * 1 days);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        report.stakerWrapFeeIncome = 1e6;
        report.stakerDeployFeeIncome = 2e6;
        bytes32 settleHash = settlement.getSettlementOperationHash(report, _roundIds());
        coordinator.setResult(settleHash, address(settlement));
        authorization.authorizeOperation(day, settleHash, address(settlement));
        settlement.settleDay(report, _roundIds(), settleHash);
        uint256[] memory days_ = new uint256[](1);
        days_[0] = day;
        assertEq(staking.claim(days_), 3e6);
        assertEq(settlement.getSettlement(day).actualIncome, 0);
    }

    function test_FeeRouterRequiresRealTransferAndFROSTGovernedPayer() public {
        CustodianDAOFeeRouter router = new CustodianDAOFeeRouter(address(incomeVault), address(0xFACE), 10_000, 0);
        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        vm.expectRevert(CustodianDAOFeeRouter.UnauthorizedPayer.selector);
        router.depositFee(day, true, 1e6);
        bytes32 payerHash = router.getSetPayerOperationHash(address(this), true);
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        router.setPayer(address(this), true, payerHash);
        coordinator.setResult(payerHash, address(router));
        authorization.authorizeOperation(13, payerHash, address(router));
        router.setPayer(address(this), true, payerHash);
        vm.expectRevert(CustodianDAOFeeRouter.InvalidShare.selector);
        router.setFeeShares(10_001, 0, bytes32(0));
        vm.expectRevert(CustodianDAOFeeRouter.InvalidAddress.selector);
        router.setTreasury(address(router), bytes32(0));
        vm.expectRevert(CustodianDAOFeeRouter.InvalidDay.selector);
        router.depositFee(day + 1, true, 1e6);
        vm.expectRevert();
        router.depositFee(day, true, 1e6);
        assertEq(incomeVault.dayWrapFeeIncome(day), 0);
    }

    function test_StakeEligibilityUsesWholeUTCDaysAndNewStakeFirstWithdrawal() public {
        address alice = address(0xAAA1);
        address bob = address(0xBBB1);
        token.transfer(alice, 200 ether);
        token.transfer(bob, 100 ether);
        vm.startPrank(alice);
        token.approve(address(staking), type(uint256).max);
        staking.stake(100 ether);
        vm.stopPrank();
        vm.startPrank(bob);
        token.approve(address(staking), type(uint256).max);
        vm.stopPrank();

        uint256 day = startDay + 1;
        vm.warp(day * 1 days + 1 hours);
        vm.prank(alice);
        staking.stake(50 ether);
        vm.prank(bob);
        staking.stake(100 ether);
        vm.warp(day * 1 days + 23 hours);
        vm.prank(alice);
        staking.unstake(60 ether);
        vm.prank(bob);
        staking.unstake(30 ether);

        assertEq(staking.eligibleStake(alice, day), 90 ether);
        assertEq(staking.eligibleStake(bob, day), 0);
        assertEq(staking.totalEligibleStake(day), 90 ether);
        vm.warp((day + 1) * 1 days);
        assertEq(staking.eligibleStake(alice, day + 1), 90 ether);
        assertEq(staking.eligibleStake(bob, day + 1), 70 ether);
    }

    function test_StakeAtExactMidnightEarnsCurrentDayButRestakeAfterWithdrawalDoesNot() public {
        address alice = address(0xAAA1);
        token.transfer(alice, 300 ether);
        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        vm.startPrank(alice);
        token.approve(address(staking), 300 ether);
        staking.stake(100 ether);
        assertEq(staking.eligibleStake(alice, day), 100 ether);
        staking.unstake(40 ether);
        staking.stake(40 ether);
        vm.stopPrank();
        assertEq(staking.eligibleStake(alice, day), 60 ether);
        assertEq(staking.totalEligibleStake(day), 60 ether);

        vm.warp(day * 1 days + 1);
        vm.prank(alice);
        staking.stake(20 ether);
        assertEq(staking.eligibleStake(alice, day), 60 ether);
        assertEq(staking.eligibleStake(alice, day + 1), 120 ether);
    }

    function test_DeploymentAtExactMidnightAllowsFirstDayEligibility() public {
        vm.warp((startDay + 1) * 1 days);
        CustodianDAOStaking newStaking = new CustodianDAOStaking(address(token), address(usd), address(validator));
        assertEq(newStaking.firstEligibleDay(), startDay + 1);
        token.approve(address(newStaking), 1 ether);
        newStaking.stake(1 ether);
        assertEq(newStaking.eligibleStake(address(this), startDay + 1), 1 ether);
    }

    function test_DelegationCannotTransferOrUnstakeAndRequiresOtherWalletConsent() public {
        address alice = address(0xAAA1);
        token.transfer(alice, 100 ether);
        vm.startPrank(alice);
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        staking.setDAOSigner(custodians[0], "");
        vm.stopPrank();
        assertEq(staking.getDAOSigner(alice), custodians[0]);
        vm.prank(custodians[0]);
        vm.expectRevert();
        staking.unstake(1 ether);

        uint256 signerKey = 0xBEEF;
        address alternate = vm.addr(signerKey);
        vm.prank(alice);
        vm.expectRevert(CustodianDAOStaking.InvalidSignerConsent.selector);
        staking.setDAOSigner(alternate, "");
        bytes32 digest = staking.getSignerConsentDigest(alice, alternate);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, digest);
        vm.prank(alice);
        staking.setDAOSigner(alternate, abi.encodePacked(r, s, v));
        assertEq(staking.getDAOSigner(alice), alternate);
        assertEq(staking.stakedBalance(alice), 100 ether);
    }

    function test_RateRequiresEightOfElevenAndChangesOnlyNextDay() public {
        vm.prank(custodians[0]);
        uint256 id = controller.propose(CustodianDAORateController.Parameter.BaseRate, 240, custodians[0]);
        (,,,, uint32 required,,) = controller.proposals(id);
        assertEq(required, 8);
        for (uint256 i = 0; i < 7; i++) {
            vm.prank(custodians[i]);
            controller.vote(id, custodians[i]);
        }
        vm.expectRevert(CustodianDAORateController.ThresholdNotMet.selector);
        controller.execute(id);
        vm.prank(custodians[0]);
        vm.expectRevert(CustodianDAORateController.AlreadyVoted.selector);
        controller.vote(id, custodians[0]);
        vm.prank(custodians[7]);
        controller.vote(id, custodians[7]);
        controller.execute(id);
        assertEq(controller.rateForDay(startDay), 120);
        assertEq(controller.rateForDay(startDay + 1), 240);
    }

    function test_TokenHoldingAndStakingDoNotCreateCustodianVote() public {
        address holder = address(0xAAA1);
        token.transfer(holder, 9_000_000 ether);
        vm.startPrank(holder);
        token.approve(address(staking), type(uint256).max);
        staking.stake(9_000_000 ether);
        vm.expectRevert(CustodianDAORateController.NotCustodianSigner.selector);
        controller.propose(CustodianDAORateController.Parameter.BaseRate, 1, holder);
        vm.stopPrank();
    }

    function test_ApprovedRateProposalCannotExecuteAfterDeadline() public {
        uint256 id = _approvedParameter(CustodianDAORateController.Parameter.BaseRate, 240);
        (,, uint64 deadline,,,,) = controller.proposals(id);
        vm.warp(uint256(deadline) + 1);
        vm.expectRevert(CustodianDAORateController.ProposalExpired.selector);
        controller.execute(id);
        assertEq(controller.baseRatePpm(), 120);
    }

    function test_NewerRateExecutionSupersedesOlderApprovalBeforeDeadline() public {
        uint256 older = _approvedParameter(CustodianDAORateController.Parameter.BaseRate, 240);
        uint256 newer = _approvedParameter(CustodianDAORateController.Parameter.BaseRate, 360);
        controller.execute(newer);
        vm.expectRevert(CustodianDAORateController.ProposalSuperseded.selector);
        controller.execute(older);
        assertFalse(controller.canVote(older, custodians[8]));
        vm.prank(custodians[8]);
        vm.expectRevert(CustodianDAORateController.ProposalSuperseded.selector);
        controller.vote(older, custodians[8]);
        assertEq(controller.rateForDay(startDay + 1), 360);
    }

    function test_ReserveProposalOrderingIsIndependentOfRate() public {
        uint256 reserve = _approvedParameter(CustodianDAORateController.Parameter.UseReserve, 0);
        uint256 rate = _approvedParameter(CustodianDAORateController.Parameter.BaseRate, 240);
        controller.execute(rate);
        controller.execute(reserve);
        assertFalse(controller.useReserveForDay(startDay + 1));
        assertEq(controller.rateForDay(startDay + 1), 240);
        uint256 newerReserve = _approvedParameter(CustodianDAORateController.Parameter.UseReserve, 1);
        uint256 newestReserve = _approvedParameter(CustodianDAORateController.Parameter.UseReserve, 0);
        controller.execute(newestReserve);
        vm.expectRevert(CustodianDAORateController.ProposalSuperseded.selector);
        controller.execute(newerReserve);
    }

    function test_ApprovedParameterCanExecuteExactlyAtDeadline() public {
        uint256 id = _approvedParameter(CustodianDAORateController.Parameter.UseReserve, 0);
        (,, uint64 deadline,,,,) = controller.proposals(id);
        vm.warp(deadline);
        controller.execute(id);
        assertFalse(controller.useReserveForDay(block.timestamp / 1 days + 1));
    }

    function _approvedParameter(CustodianDAORateController.Parameter parameter, uint256 value)
        private
        returns (uint256 id)
    {
        vm.prank(custodians[0]);
        id = controller.propose(parameter, value, custodians[0]);
        for (uint256 i = 0; i < 8; i++) {
            vm.prank(custodians[i]);
            controller.vote(id, custodians[i]);
        }
    }

    function test_DailyRewardIsProportionalToFullDayEligibleStake() public {
        address alice = address(0xAAA1);
        address bob = address(0xBBB1);
        token.transfer(alice, 100 ether);
        token.transfer(bob, 300 ether);
        vm.startPrank(alice);
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        vm.stopPrank();
        vm.startPrank(bob);
        token.approve(address(staking), 300 ether);
        staking.stake(300 ether);
        vm.stopPrank();
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        _settle(_report(day, 100e6, 0), _roundIds());
        uint256 stakerIncome = settlement.getSettlement(day).stakerIncome;
        uint256[] memory days_ = new uint256[](1);
        days_[0] = day;
        assertEq(staking.claimableReward(alice, days_), stakerIncome / 4);
        assertEq(staking.claimableReward(bob, days_), stakerIncome * 3 / 4);
        vm.prank(alice);
        staking.claim(days_);
        vm.prank(bob);
        staking.claim(days_);
        assertEq(usd.balanceOf(alice), stakerIncome / 4);
        assertEq(usd.balanceOf(bob), stakerIncome * 3 / 4);
    }

    function test_ReserveFlagHasSameCustodianThreshold() public {
        vm.prank(custodians[0]);
        uint256 id = controller.propose(CustodianDAORateController.Parameter.UseReserve, 0, custodians[0]);
        for (uint256 i = 0; i < 8; i++) {
            vm.prank(custodians[i]);
            controller.vote(id, custodians[i]);
        }
        controller.execute(id);
        assertTrue(controller.useReserveForDay(startDay));
        assertFalse(controller.useReserveForDay(startDay + 1));
    }

    function test_DelegatedCustodianWalletDoesNotGainSecondRateVote() public {
        vm.prank(custodians[0]);
        staking.setDAOSigner(custodians[1], "");
        vm.prank(custodians[1]);
        uint256 id = controller.propose(CustodianDAORateController.Parameter.BaseRate, 200, custodians[0]);
        vm.prank(custodians[1]);
        controller.vote(id, custodians[0]);
        vm.prank(custodians[1]);
        vm.expectRevert(CustodianDAORateController.AlreadyVoted.selector);
        controller.vote(id, custodians[1]);
        (,,,,, uint32 votes,) = controller.proposals(id);
        assertEq(votes, 1);
    }

    function test_ChainlinkTWAPRejectsMissingRounds() public {
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        uint80[] memory ids = _roundIds();
        assertEq(twap.calculate(day, ids), 150 * 1e8);
        ids[1] = 4;
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.calculate(day, ids);
    }

    function test_ChainlinkTWAPAcceptsVerifiedProxyPhaseChange() public {
        uint256 day = startDay + 1;
        uint80 oldRound = uint80((1 << 64) | 1);
        uint80 newRound = uint80((2 << 64) | 1);
        feed.setRound(oldRound, 100e8, day * 1 days - 1 hours);
        feed.setRound(newRound, 200e8, day * 1 days + 12 hours);
        feed.setRound(newRound + 1, 200e8, (day + 1) * 1 days);
        uint80[] memory ids = new uint80[](3);
        ids[0] = oldRound;
        ids[1] = newRound;
        ids[2] = newRound + 1;
        vm.warp((day + 1) * 1 days);
        assertEq(twap.calculate(day, ids), 150e8);
        feed.setRound(oldRound + 1, 100e8, day * 1 days + 2 hours);
        vm.expectRevert(ChainlinkBTCDailyTWAP.MissingRound.selector);
        twap.calculate(day, ids);
    }

    function test_ChainlinkTWAPRejectsStaleOpeningPrice() public {
        uint256 day = startDay + 1;
        feed.setRound(1, 100e8, day * 1 days - 2 days);
        feed.setRound(2, 200e8, day * 1 days + 1 hours);
        feed.setRound(3, 200e8, (day + 1) * 1 days);
        vm.warp((day + 1) * 1 days);
        vm.expectRevert(ChainlinkBTCDailyTWAP.StalePrice.selector);
        twap.calculate(day, _roundIds());
    }

    function test_SettlementSplitsExcessFundsReserveAndPaysFeesIndependently() public {
        address alice = address(0xAAA1);
        token.transfer(alice, 100 ether);
        vm.startPrank(alice);
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        vm.stopPrank();

        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 100e6, 20e6);
        _settle(report, _roundIds());
        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day);
        assertEq(result.btcPrice8, 150e8);
        assertEq(result.tvl, 150e6);
        assertEq(result.baseTarget, 18_000);
        assertEq(result.actualIncome, 100e6);
        assertEq(result.basePaid, 18_000);
        assertEq(result.excess, 100e6 - 18_000);
        assertEq(result.reserveTarget, 7_500_000);
        assertEq(result.reserveBalanceAfter, 7_500_000);
        assertEq(result.stakerIncome + result.lpIncome + result.reserveBalanceAfter, 120e6);
        uint256 before = usd.balanceOf(alice);
        vm.prank(alice);
        uint256[] memory days_ = new uint256[](1);
        days_[0] = day;
        staking.claim(days_);
        assertEq(usd.balanceOf(alice) - before, result.stakerIncome);
    }

    function test_InsufficientIncomeUsesReserveWithoutStakerExcess() public {
        address alice = address(0xAAA1);
        token.transfer(alice, 100 ether);
        vm.startPrank(alice);
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        vm.stopPrank();
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        _settle(_report(day, 100e6, 0), _roundIds());

        feed.setRound(4, 150e8, (day + 2) * 1 days);
        uint80[] memory secondDayRounds = new uint80[](2);
        secondDayRounds[0] = 3;
        secondDayRounds[1] = 4;
        vm.warp((day + 2) * 1 days);
        _settle(_report(day + 1, 0, 1e6), secondDayRounds);
        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day + 1);
        assertEq(result.baseTarget, 18_000);
        assertEq(result.basePaid, 18_000);
        assertEq(result.excess, 0);
        assertEq(result.stakerIncome, 1e6);
        assertEq(result.reserveBalanceAfter, 7_500_000 - 18_000);
    }

    function test_InsufficientIncomeWithReserveDisabledPaysOnlyCashAndIndependentFees() public {
        address alice = address(0xAAA1);
        token.transfer(alice, 100 ether);
        vm.startPrank(alice);
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        vm.stopPrank();
        usd.mint(address(this), 2e6);
        usd.approve(address(settlement), 2e6);
        settlement.fundReserve(2e6);

        vm.prank(custodians[0]);
        uint256 id = controller.propose(CustodianDAORateController.Parameter.UseReserve, 0, custodians[0]);
        for (uint256 i = 0; i < 8; i++) {
            vm.prank(custodians[i]);
            controller.vote(id, custodians[i]);
        }
        controller.execute(id);

        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        _settle(_report(day, 5_000, 1e6), _roundIds());
        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day);
        assertEq(result.baseTarget, 18_000);
        assertEq(result.actualIncome, 5_000);
        assertEq(result.basePaid, 5_000);
        assertEq(result.excess, 0);
        assertEq(result.stakerIncome, 1e6);
        assertEq(result.lpIncome, 5_000);
        assertEq(result.reserveBalanceAfter, 2e6);
    }

    function test_WrapAndDeployFeesAccrueSeparatelyWithoutYield() public {
        address alice = address(0xAAA1);
        token.transfer(alice, 100 ether);
        vm.startPrank(alice);
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        vm.stopPrank();

        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 2e6);
        report.stakerDeployFeeIncome = 3e6;
        _settle(report, _roundIds());

        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day);
        assertEq(result.actualIncome, 0);
        assertEq(result.excess, 0);
        assertEq(result.basePaid, 0);
        assertEq(result.stakerWrapFeeIncome, 2e6);
        assertEq(result.stakerDeployFeeIncome, 3e6);
        assertEq(result.stakerIncome, 5e6);

        uint256[] memory days_ = new uint256[](1);
        days_[0] = day;
        vm.prank(alice);
        staking.claim(days_);
        assertEq(usd.balanceOf(alice), 5e6);
    }

    function test_DailyReportRejectsFeeAsYieldSourceAndDuplicateSources() public {
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        report.income[0].sourceId = settlement.WRAP_FEE();
        bytes32 hash = settlement.getSettlementOperationHash(report, _roundIds());
        vm.expectRevert(CustodianDAODailySettlement.InvalidReport.selector);
        settlement.settleDay(report, _roundIds(), hash);

        report.income = new CustodianDAODailySettlement.IncomeLine[](2);
        report.income[0] = CustodianDAODailySettlement.IncomeLine(keccak256("SILO"), 1);
        report.income[1] = CustodianDAODailySettlement.IncomeLine(keccak256("SILO"), 1);
        hash = settlement.getSettlementOperationHash(report, _roundIds());
        vm.expectRevert(CustodianDAODailySettlement.DuplicateSource.selector);
        settlement.settleDay(report, _roundIds(), hash);
    }

    function test_FeeBreakdownIsBoundToFROSTApproval() public view {
        uint256 day = startDay + 1;
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 2e6);
        bytes32 wrapHash = settlement.getSettlementOperationHash(report, _roundIds());
        report.stakerWrapFeeIncome = 0;
        report.stakerDeployFeeIncome = 2e6;
        bytes32 deployHash = settlement.getSettlementOperationHash(report, _roundIds());
        assertTrue(wrapHash != deployHash);
    }

    function test_EvidenceHashIsRequiredAndBoundToApproval() public {
        uint256 day = startDay + 1;
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        bytes32 originalHash = settlement.getSettlementOperationHash(report, _roundIds());
        report.evidenceHash = keccak256("different evidence");
        assertTrue(originalHash != settlement.getSettlementOperationHash(report, _roundIds()));

        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        report.evidenceHash = bytes32(0);
        bytes32 hash = settlement.getSettlementOperationHash(report, _roundIds());
        vm.expectRevert(CustodianDAODailySettlement.InvalidReport.selector);
        settlement.settleDay(report, _roundIds(), hash);
    }

    function test_OnlyAuthorizedSourceCanDepositAndOnlySettlementCanRelease() public {
        uint256 day = startDay + 1;
        usd.mint(address(0xAAA1), 1e6);
        vm.startPrank(address(0xAAA1));
        usd.approve(address(incomeVault), 1e6);
        vm.expectRevert(CustodianDAOIncomeVault.Unauthorized.selector);
        incomeVault.deposit(day, keccak256("YIELD_BASIS"), 1e6);
        vm.stopPrank();

        bytes32[] memory ids = new bytes32[](0);
        uint256[] memory amounts = new uint256[](0);
        vm.expectRevert(CustodianDAOIncomeVault.Unauthorized.selector);
        incomeVault.releaseDay(day, ids, amounts, 0, 0);
    }

    function test_IncomeVaultRejectsWrongSettlementBinding() public {
        CustodianDAOIncomeVault other = new CustodianDAOIncomeVault(address(usd), address(authorization));
        vm.expectRevert(CustodianDAOIncomeVault.InvalidAddress.selector);
        other.setSettlement(address(settlement));
    }

    function test_SettlementRejectsUnfundedReportWithoutConsumingApproval() public {
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 10e6, 0);
        bytes32 hash = settlement.getSettlementOperationHash(report, _roundIds());
        coordinator.setResult(hash, address(settlement));
        authorization.authorizeOperation(day, hash, address(settlement));

        usd.mint(address(this), 5e6);
        usd.approve(address(incomeVault), 10e6);
        incomeVault.deposit(day, keccak256("YIELD_BASIS"), 5e6);
        vm.expectRevert(CustodianDAOIncomeVault.ReportMismatch.selector);
        settlement.settleDay(report, _roundIds(), hash);
        assertFalse(authorization.executedOperations(hash));

        usd.mint(address(this), 5e6);
        incomeVault.deposit(day, keccak256("YIELD_BASIS"), 5e6);
        settlement.settleDay(report, _roundIds(), hash);
        assertTrue(authorization.executedOperations(hash));
        assertTrue(incomeVault.released(day));
    }

    function test_SettlementCannotReclassifyWrapFeeAsDeployFee() public {
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        usd.mint(address(this), 2e6);
        usd.approve(address(incomeVault), 2e6);
        incomeVault.deposit(day, incomeVault.WRAP_FEE(), 2e6);

        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        report.stakerDeployFeeIncome = 2e6;
        bytes32 hash = settlement.getSettlementOperationHash(report, _roundIds());
        coordinator.setResult(hash, address(settlement));
        authorization.authorizeOperation(day, hash, address(settlement));
        vm.expectRevert(CustodianDAOIncomeVault.ReportMismatch.selector);
        settlement.settleDay(report, _roundIds(), hash);
    }

    function test_CurveSourceRequiresAndEnforcesWrapperLiquidityMinimum() public {
        bytes32 curve = incomeVault.CURVE();
        vm.expectRevert(CustodianDAOIncomeVault.InvalidSource.selector);
        incomeVault.setSource(curve, address(this), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0, 0);

        DAOMockCurvePool pool = new DAOMockCurvePool();
        CurvePoolLiquidityGuard guard = new CurvePoolLiquidityGuard(address(token), address(pool));
        token.transfer(address(pool), 99 ether);
        bytes32 hash = incomeVault.getSetSourceOperationHash(
            curve, address(this), CustodianDAOIncomeVault.Stream.Yield, true, address(guard), 100 ether
        );
        coordinator.setResult(hash, address(incomeVault));
        authorization.authorizeOperation(13, hash, address(incomeVault));
        incomeVault.setSource(
            curve, address(this), CustodianDAOIncomeVault.Stream.Yield, true, address(guard), 100 ether, hash
        );

        usd.mint(address(this), 1e6);
        usd.approve(address(incomeVault), 1e6);
        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        vm.expectRevert(CustodianDAOIncomeVault.InsufficientLiquidity.selector);
        incomeVault.deposit(day, curve, 1e6);
        token.transfer(address(pool), 1 ether);
        incomeVault.deposit(day, curve, 1e6);
        assertEq(incomeVault.dayYieldIncome(day), 1e6);
    }

    function test_IncomeVaultRejectsDaysBeforeDAOStart() public {
        usd.mint(address(this), 1e6);
        usd.approve(address(incomeVault), 1e6);
        vm.expectRevert(CustodianDAOIncomeVault.InvalidDay.selector);
        incomeVault.deposit(startDay, keccak256("YIELD_BASIS"), 1e6);
        assertEq(incomeVault.firstSettlementDay(), startDay + 1);
    }

    function test_FROSTRegisteredTreasuryWalletCanFundIncome() public {
        address treasury = address(0xA011);
        bytes32 silo = keccak256("SILO");
        bytes32 hash = incomeVault.getSetSourceOperationHash(
            silo, treasury, CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0
        );
        coordinator.setResult(hash, address(incomeVault));
        authorization.authorizeOperation(13, hash, address(incomeVault));
        incomeVault.setSource(silo, treasury, CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0, hash);

        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        usd.mint(treasury, 1e6);
        vm.startPrank(treasury);
        usd.approve(address(incomeVault), 1e6);
        incomeVault.deposit(day, silo, 1e6);
        vm.stopPrank();
        assertEq(incomeVault.sourceIncome(day, silo), 1e6);
        assertEq(usd.balanceOf(address(incomeVault)), 1e6);
    }

    function test_ClaimAdapterDepositsOnlyRealizedConfiguredRewardToken() public {
        DAOMockClaimProtocol protocol = new DAOMockClaimProtocol(address(usd));
        bytes32 sourceId = keccak256("YIELD_BASIS");
        CustodianDAOClaimAdapter adapter =
            new CustodianDAOClaimAdapter(address(incomeVault), address(protocol), sourceId);
        bytes32 sourceHash = incomeVault.getSetSourceOperationHash(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0
        );
        coordinator.setResult(sourceHash, address(incomeVault));
        authorization.authorizeOperation(13, sourceHash, address(incomeVault));
        incomeVault.setSource(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0, sourceHash
        );

        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        bytes memory callData = abi.encodeCall(DAOMockClaimProtocol.claim, (2e6, address(adapter)));
        bytes32 claimHash = adapter.getClaimOperationHash(day, callData, 2e6);
        coordinator.setResult(claimHash, address(adapter));
        authorization.authorizeOperation(14, claimHash, address(adapter));
        assertEq(adapter.claimAndDeposit(day, callData, 2e6, claimHash), 2e6);
        assertEq(incomeVault.sourceIncome(day, sourceId), 2e6);
        assertEq(usd.balanceOf(address(adapter)), 0);
        assertEq(usd.balanceOf(address(incomeVault)), 2e6);
    }

    function test_ClaimAdapterRejectsUnrealizedIncomeWithoutConsumingApproval() public {
        DAOMockClaimProtocol protocol = new DAOMockClaimProtocol(address(usd));
        bytes32 sourceId = keccak256("SILO");
        CustodianDAOClaimAdapter adapter =
            new CustodianDAOClaimAdapter(address(incomeVault), address(protocol), sourceId);
        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        bytes memory callData = abi.encodeCall(DAOMockClaimProtocol.claim, (0, address(adapter)));
        bytes32 claimHash = adapter.getClaimOperationHash(day, callData, 1);
        coordinator.setResult(claimHash, address(adapter));
        authorization.authorizeOperation(14, claimHash, address(adapter));
        vm.expectRevert(CustodianDAOClaimAdapter.InsufficientIncome.selector);
        adapter.claimAndDeposit(day, callData, 1, claimHash);
        assertFalse(authorization.executedOperations(claimHash));
    }

    function test_CurveClaimRequiresConfiguredLiquidityAtIncomeDeposit() public {
        DAOMockCurvePool pool = new DAOMockCurvePool();
        CurvePoolLiquidityGuard guard = new CurvePoolLiquidityGuard(address(token), address(pool));
        DAOMockClaimProtocol protocol = new DAOMockClaimProtocol(address(usd));
        bytes32 sourceId = incomeVault.CURVE();
        CustodianDAOClaimAdapter adapter =
            new CustodianDAOClaimAdapter(address(incomeVault), address(protocol), sourceId);
        token.transfer(address(pool), 99 ether);
        bytes32 sourceHash = incomeVault.getSetSourceOperationHash(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.Yield, true, address(guard), 100 ether
        );
        coordinator.setResult(sourceHash, address(incomeVault));
        authorization.authorizeOperation(13, sourceHash, address(incomeVault));
        incomeVault.setSource(
            sourceId,
            address(adapter),
            CustodianDAOIncomeVault.Stream.Yield,
            true,
            address(guard),
            100 ether,
            sourceHash
        );

        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        bytes memory callData = abi.encodeCall(DAOMockClaimProtocol.claim, (1e6, address(adapter)));
        bytes32 claimHash = adapter.getClaimOperationHash(day, callData, 1e6);
        coordinator.setResult(claimHash, address(adapter));
        authorization.authorizeOperation(14, claimHash, address(adapter));
        vm.expectRevert(CustodianDAOIncomeVault.InsufficientLiquidity.selector);
        adapter.claimAndDeposit(day, callData, 1e6, claimHash);
        assertFalse(authorization.executedOperations(claimHash));
        assertEq(usd.balanceOf(address(adapter)), 0);

        token.transfer(address(pool), 1 ether);
        assertEq(adapter.claimAndDeposit(day, callData, 1e6, claimHash), 1e6);
        assertEq(incomeVault.sourceIncome(day, sourceId), 1e6);
    }

    function test_SiloAdapterHarvestsOnlyAppreciationAndPreservesPrincipal() public {
        DAOMockSiloVault silo = new DAOMockSiloVault(address(usd));
        CustodianDAOSilo4626Adapter adapter = new CustodianDAOSilo4626Adapter(address(incomeVault), address(silo));
        bytes32 sourceId = adapter.SOURCE_ID();
        bytes32 sourceHash = incomeVault.getSetSourceOperationHash(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0
        );
        coordinator.setResult(sourceHash, address(incomeVault));
        authorization.authorizeOperation(13, sourceHash, address(incomeVault));
        incomeVault.setSource(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0, sourceHash
        );

        usd.mint(address(this), 100e6);
        usd.approve(address(adapter), 100e6);
        bytes32 supplyHash = adapter.getSupplyOperationHash(address(this), 100e6, 100e6);
        coordinator.setResult(supplyHash, address(adapter));
        authorization.authorizeOperation(14, supplyHash, address(adapter));
        assertEq(adapter.supply(address(this), 100e6, 100e6, supplyHash), 100e6);
        assertEq(adapter.principalAssets(), 100e6);

        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        bytes32 noYieldHash = adapter.getHarvestOperationHash(day, 1e6);
        coordinator.setResult(noYieldHash, address(adapter));
        authorization.authorizeOperation(15, noYieldHash, address(adapter));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InsufficientYield.selector);
        adapter.harvest(day, 1e6, noYieldHash);
        assertFalse(authorization.executedOperations(noYieldHash));

        usd.mint(address(silo), 10e6);
        uint256 harvested = adapter.harvest(day, 1e6, noYieldHash);
        assertGe(harvested, 10e6 - 1);
        assertLe(harvested, 10e6);
        assertEq(incomeVault.sourceIncome(day, sourceId), harvested);
        assertGe(silo.previewRedeem(silo.balanceOf(address(adapter))), 100e6);
        assertEq(adapter.principalAssets(), 100e6);

        bytes32 withdrawHash = adapter.getWithdrawOperationHash(address(this), 100e6);
        coordinator.setResult(withdrawHash, address(adapter));
        authorization.authorizeOperation(16, withdrawHash, address(adapter));
        assertEq(adapter.withdrawPrincipal(address(this), 100e6, withdrawHash), 100e6);
        assertEq(adapter.principalAssets(), 0);
        assertEq(silo.balanceOf(address(adapter)), 0);
    }

    function test_SiloAdapterRejectsWrongAssetAndAllowsLossRecovery() public {
        DAOMockSiloVault wrongAsset = new DAOMockSiloVault(address(token));
        vm.expectRevert(CustodianDAOSilo4626Adapter.InvalidVaultAsset.selector);
        new CustodianDAOSilo4626Adapter(address(incomeVault), address(wrongAsset));

        DAOMockSiloVault silo = new DAOMockSiloVault(address(usd));
        CustodianDAOSilo4626Adapter adapter = new CustodianDAOSilo4626Adapter(address(incomeVault), address(silo));
        usd.mint(address(this), 100e6);
        usd.approve(address(adapter), 100e6);
        bytes32 supplyHash = adapter.getSupplyOperationHash(address(this), 100e6, 100e6);
        coordinator.setResult(supplyHash, address(adapter));
        authorization.authorizeOperation(13, supplyHash, address(adapter));
        adapter.supply(address(this), 100e6, 100e6, supplyHash);

        vm.prank(address(silo));
        usd.transfer(address(0xDEAD), 20e6);
        assertEq(silo.previewRedeem(silo.balanceOf(address(adapter))), 80e6);
        bytes32 recoveryHash = adapter.getEmergencyRedeemOperationHash(address(this), 80e6);
        coordinator.setResult(recoveryHash, address(adapter));
        authorization.authorizeOperation(14, recoveryHash, address(adapter));
        assertEq(adapter.emergencyRedeem(address(this), 80e6, recoveryHash), 80e6);
        assertEq(adapter.principalAssets(), 0);
        assertEq(silo.balanceOf(address(adapter)), 0);
    }

    function test_ConversionAdapterFundsConfiguredTokenFeeStreamAndRefundsUnusedInput() public {
        DAOMockConverter converter = new DAOMockConverter();
        bytes32 sourceId = incomeVault.WRAP_FEE();
        CustodianDAOConversionAdapter adapter =
            new CustodianDAOConversionAdapter(address(incomeVault), address(token), address(converter), sourceId);
        bytes32 sourceHash = incomeVault.getSetSourceOperationHash(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0
        );
        coordinator.setResult(sourceHash, address(incomeVault));
        authorization.authorizeOperation(13, sourceHash, address(incomeVault));
        incomeVault.setSource(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0, sourceHash
        );

        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        usd.mint(address(converter), 2e6);
        token.approve(address(adapter), 100 ether);
        bytes memory converterCall =
            abi.encodeCall(DAOMockConverter.swap, (address(token), address(usd), 50 ether, address(adapter), 2e6));
        bytes32 hash = adapter.getConvertOperationHash(day, address(this), 100 ether, 2e6, converterCall);
        coordinator.setResult(hash, address(adapter));
        authorization.authorizeOperation(14, hash, address(adapter));
        uint256 inputBefore = token.balanceOf(address(this));
        assertEq(adapter.convertAndDeposit(day, address(this), 100 ether, 2e6, converterCall, hash), 2e6);
        assertEq(token.balanceOf(address(this)), inputBefore - 50 ether);
        assertEq(token.balanceOf(address(adapter)), 0);
        assertEq(usd.balanceOf(address(adapter)), 0);
        assertEq(incomeVault.dayWrapFeeIncome(day), 2e6);
        assertEq(incomeVault.dayYieldIncome(day), 0);
    }

    function test_ConversionAdapterRejectsInsufficientOutputWithoutSpendingApproval() public {
        DAOMockConverter converter = new DAOMockConverter();
        CustodianDAOConversionAdapter adapter = new CustodianDAOConversionAdapter(
            address(incomeVault), address(token), address(converter), keccak256("YIELD_BASIS")
        );
        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        usd.mint(address(converter), 1e6);
        token.approve(address(adapter), 100 ether);
        bytes memory converterCall =
            abi.encodeCall(DAOMockConverter.swap, (address(token), address(usd), 100 ether, address(adapter), 1e6));
        bytes32 hash = adapter.getConvertOperationHash(day, address(this), 100 ether, 2e6, converterCall);
        coordinator.setResult(hash, address(adapter));
        authorization.authorizeOperation(13, hash, address(adapter));
        uint256 inputBefore = token.balanceOf(address(this));
        vm.expectRevert(CustodianDAOConversionAdapter.InsufficientOutput.selector);
        adapter.convertAndDeposit(day, address(this), 100 ether, 2e6, converterCall, hash);
        assertFalse(authorization.executedOperations(hash));
        assertEq(token.balanceOf(address(this)), inputBefore);
        assertEq(token.balanceOf(address(converter)), 0);
    }

    function test_ConvertedWrapFeeSettlesAndPaysStakerWithoutYield() public {
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);

        DAOMockConverter converter = new DAOMockConverter();
        bytes32 sourceId = incomeVault.WRAP_FEE();
        CustodianDAOConversionAdapter adapter =
            new CustodianDAOConversionAdapter(address(incomeVault), address(token), address(converter), sourceId);
        bytes32 sourceHash = incomeVault.getSetSourceOperationHash(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0
        );
        coordinator.setResult(sourceHash, address(incomeVault));
        authorization.authorizeOperation(13, sourceHash, address(incomeVault));
        incomeVault.setSource(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0, sourceHash
        );

        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp(day * 1 days);
        usd.mint(address(converter), 2e6);
        token.approve(address(adapter), 10 ether);
        bytes memory converterCall =
            abi.encodeCall(DAOMockConverter.swap, (address(token), address(usd), 10 ether, address(adapter), 2e6));
        bytes32 convertHash = adapter.getConvertOperationHash(day, address(this), 10 ether, 2e6, converterCall);
        coordinator.setResult(convertHash, address(adapter));
        authorization.authorizeOperation(14, convertHash, address(adapter));
        adapter.convertAndDeposit(day, address(this), 10 ether, 2e6, converterCall, convertHash);

        vm.warp((day + 1) * 1 days);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        report.stakerWrapFeeIncome = 2e6;
        bytes32 settleHash = settlement.getSettlementOperationHash(report, _roundIds());
        coordinator.setResult(settleHash, address(settlement));
        authorization.authorizeOperation(day, settleHash, address(settlement));
        settlement.settleDay(report, _roundIds(), settleHash);

        CustodianDAODailySettlement.Settlement memory result = settlement.getSettlement(day);
        assertEq(result.actualIncome, 0);
        assertEq(result.stakerWrapFeeIncome, 2e6);
        assertEq(result.stakerIncome, 2e6);
        uint256[] memory days_ = new uint256[](1);
        days_[0] = day;
        assertEq(staking.claim(days_), 2e6);
        assertEq(usd.balanceOf(address(this)), 2e6);
    }

    function test_SettlementRequiresEveryFundedYieldSource() public {
        _registerSource(keccak256("SILO"), CustodianDAOIncomeVault.Stream.Yield, 13);
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        usd.mint(address(this), 3e6);
        usd.approve(address(incomeVault), 3e6);
        incomeVault.deposit(day, keccak256("YIELD_BASIS"), 1e6);
        incomeVault.deposit(day, keccak256("SILO"), 2e6);

        CustodianDAODailySettlement.DayReport memory report = _report(day, 1e6, 0);
        bytes32 hash = settlement.getSettlementOperationHash(report, _roundIds());
        coordinator.setResult(hash, address(settlement));
        authorization.authorizeOperation(day, hash, address(settlement));
        vm.expectRevert(CustodianDAOIncomeVault.ReportMismatch.selector);
        settlement.settleDay(report, _roundIds(), hash);
    }

    function test_ActualPoolMPFeeCanBeClaimedConvertedSettledAndPaidToStaker() public {
        token.approve(address(staking), 100 ether);
        staking.stake(100 ether);
        PoolMpToken mp = new PoolMpToken("Pool MP", "MP", address(this), false);
        address predictedPool = vm.computeCreateAddress(address(this), vm.getNonce(address(this)) + 1);
        MultiPoolShareToken shares = new MultiPoolShareToken("Share", "S", predictedPool);
        SimpleMultiPool pool = new SimpleMultiPool("fee-source", address(authorization), address(shares));
        IFeeManager.FeeConfig memory config = IFeeManager.FeeConfig(IFeeManager.FeeType.FixedPercentage, 1000, 0, 0);
        FederationFeeManager manager = new FederationFeeManager(address(authorization), address(0), address(this), config, config);
        bytes32 collector = manager.getSetFeeCollectorOperationHash(address(pool), true);
        _authorizeTestOperation(13, collector, address(manager));
        manager.setFeeCollector(address(pool), true, collector);
        bytes32 setup = pool.getSetFeeManagerOperationHash(address(manager));
        _authorizeTestOperation(14, setup, address(pool));
        pool.setFeeManager(address(manager), setup);
        bytes32 add = pool.getAddPoolOperationHash(address(mp), "actual-fee");
        _authorizeTestOperation(15, add, address(pool));
        pool.addPool(address(mp), "actual-fee", add);
        mp.mint(address(this), 100 ether);
        mp.approve(address(pool), 100 ether);
        pool.deposit(address(mp), 100 ether);
        assertEq(shares.balanceOf(address(this)), 90 ether);
        bytes32 claim = pool.getClaimFeesOperationHash(address(mp), true, address(this), 10 ether);
        _authorizeTestOperation(16, claim, address(pool));
        pool.claimFees(address(mp), true, address(this), 10 ether, claim);
        assertEq(mp.balanceOf(address(this)), 10 ether);
        assertEq(mp.balanceOf(address(pool)), 90 ether);
        assertEq(pool.getPoolInfo(address(mp)).totalDeposited, 90 ether);

        DAOMockConverter converter = new DAOMockConverter();
        bytes32 sourceId = incomeVault.WRAP_FEE();
        CustodianDAOConversionAdapter adapter = new CustodianDAOConversionAdapter(
            address(incomeVault), address(mp), address(converter), sourceId
        );
        bytes32 source = incomeVault.getSetSourceOperationHash(
            sourceId, address(adapter), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0
        );
        _authorizeTestOperation(17, source, address(incomeVault));
        incomeVault.setSource(sourceId, address(adapter), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0, source);
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp(day * 1 days);
        usd.mint(address(converter), 2e6);
        mp.approve(address(adapter), 10 ether);
        bytes memory swap = abi.encodeCall(DAOMockConverter.swap, (address(mp), address(usd), 10 ether, address(adapter), 2e6));
        bytes32 conversion = adapter.getConvertOperationHash(day, address(this), 10 ether, 2e6, swap);
        _authorizeTestOperation(18, conversion, address(adapter));
        adapter.convertAndDeposit(day, address(this), 10 ether, 2e6, swap, conversion);
        assertEq(incomeVault.dayWrapFeeIncome(day), 2e6);
        assertEq(mp.balanceOf(address(converter)), 10 ether);
        vm.warp((day + 1) * 1 days);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        report.stakerWrapFeeIncome = 2e6;
        bytes32 settleHash = settlement.getSettlementOperationHash(report, _roundIds());
        _authorizeTestOperation(19, settleHash, address(settlement));
        settlement.settleDay(report, _roundIds(), settleHash);
        uint256[] memory days_ = new uint256[](1);
        days_[0] = day;
        assertEq(staking.claim(days_), 2e6);
        assertEq(settlement.getSettlement(day).actualIncome, 0);
        assertEq(pool.accruedFees(address(mp), true), 0);
    }

    function _authorizeTestOperation(uint256 session, bytes32 hash, address target) private {
        coordinator.setResult(hash, target);
        authorization.authorizeOperation(session, hash, target);
    }

    function test_SourceCannotBeReconfiguredWithoutFreshFROSTApproval() public {
        bytes32 sourceId = keccak256("YIELD_BASIS");
        vm.expectRevert(CustodianDAOIncomeVault.ReportMismatch.selector);
        incomeVault.setSource(
            sourceId, address(0xAAA1), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0, bytes32(0)
        );
        bytes32 hash = incomeVault.getSetSourceOperationHash(
            sourceId, address(0xAAA1), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0
        );
        vm.expectRevert(FROSTAuthorization.OperationNotApproved.selector);
        incomeVault.setSource(
            sourceId, address(0xAAA1), CustodianDAOIncomeVault.Stream.Yield, true, address(0), 0, hash
        );
    }

    function test_ReservedFeeSourceCannotUseWrongStream() public {
        bytes32 wrapFee = incomeVault.WRAP_FEE();
        bytes32 deployFee = incomeVault.DEPLOY_FEE();
        vm.expectRevert(CustodianDAOIncomeVault.InvalidSource.selector);
        incomeVault.setSource(
            wrapFee, address(this), CustodianDAOIncomeVault.Stream.DeployFee, true, address(0), 0, bytes32(0)
        );
        vm.expectRevert(CustodianDAOIncomeVault.InvalidSource.selector);
        incomeVault.setSource(
            deployFee, address(this), CustodianDAOIncomeVault.Stream.WrapFee, true, address(0), 0, bytes32(0)
        );
    }

    function test_YieldSourceLimitRejectsBeforeTransferAndStillSettles() public {
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        usd.mint(address(this), 34);
        usd.approve(address(incomeVault), type(uint256).max);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        report.averageBtcSats = 0;
        report.openingLiquidityReserveSats = 0;
        report.income = new CustodianDAODailySettlement.IncomeLine[](32);
        for (uint256 i = 0; i < 33; i++) {
            bytes32 source = keccak256(abi.encode("bounded-source", i));
            _registerSource(source, CustodianDAOIncomeVault.Stream.Yield, 2000 + i);
            if (i == 32) {
                vm.expectRevert(CustodianDAOIncomeVault.TooManyYieldSources.selector);
                incomeVault.deposit(day, source, 1);
                assertEq(incomeVault.sourceIncome(day, source), 0);
                assertEq(usd.balanceOf(address(this)), 2);
                // The bound is per funded day, not a permanent limit on extensible source registration.
                incomeVault.deposit(day + 1, source, 1);
            } else {
                incomeVault.deposit(day, source, 1);
                report.income[i] = CustodianDAODailySettlement.IncomeLine(source, 1);
            }
        }
        assertEq(incomeVault.getFundedYieldSources(day).length, 32);
        assertEq(incomeVault.getFundedYieldSources(day + 1).length, 1);
        bytes32 hash = settlement.getSettlementOperationHash(report, _roundIds());
        _authorizeTestOperation(2100, hash, address(settlement));
        settlement.settleDay(report, _roundIds(), hash);
        assertEq(settlement.nextSettlementDay(), day + 1);
        assertEq(usd.balanceOf(address(incomeVault)), 1);
    }

    function test_YieldSourceCanTopUpAtLimitAndDeactivationPreservesHistory() public {
        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        usd.mint(address(this), 33);
        usd.approve(address(incomeVault), type(uint256).max);
        bytes32 first;
        for (uint256 i = 0; i < 32; i++) {
            bytes32 source = keccak256(abi.encode("topup-source", i));
            if (i == 0) first = source;
            _registerSource(source, CustodianDAOIncomeVault.Stream.Yield, 2200 + i);
            incomeVault.deposit(day, source, 1);
        }
        incomeVault.deposit(day, first, 1);
        assertEq(incomeVault.sourceIncome(day, first), 2);
        assertEq(incomeVault.getFundedYieldSources(day).length, 32);
        bytes32 hash = incomeVault.getSetSourceOperationHash(
            first, address(this), CustodianDAOIncomeVault.Stream.Yield, false, address(0), 0
        );
        _authorizeTestOperation(2300, hash, address(incomeVault));
        incomeVault.setSource(first, address(this), CustodianDAOIncomeVault.Stream.Yield, false, address(0), 0, hash);
        assertEq(incomeVault.getFundedYieldSources(day).length, 32);
        assertEq(incomeVault.dayYieldIncome(day), 33);
        bytes32[] memory funded = incomeVault.getFundedYieldSources(day);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 0, 0);
        report.averageBtcSats = 0;
        report.openingLiquidityReserveSats = 0;
        report.income = new CustodianDAODailySettlement.IncomeLine[](32);
        for (uint256 i = 0; i < 32; i++) {
            report.income[i] = CustodianDAODailySettlement.IncomeLine(funded[i], incomeVault.sourceIncome(day, funded[i]));
        }
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        hash = settlement.getSettlementOperationHash(report, _roundIds());
        _authorizeTestOperation(2301, hash, address(settlement));
        settlement.settleDay(report, _roundIds(), hash);
        assertTrue(incomeVault.released(day));
        assertEq(settlement.getSettlement(day).actualIncome, 33);
    }

    function test_FeeAliasCannotReplaceEqualSumYieldSources() public {
        uint256 day = startDay + 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        bytes32 aliasId = bytes32(uint256(300));
        _registerSource(aliasId, CustodianDAOIncomeVault.Stream.WrapFee, 2400);
        usd.mint(address(this), 66);
        usd.approve(address(incomeVault), 66);
        incomeVault.deposit(day, keccak256("YIELD_BASIS"), 33);
        incomeVault.deposit(day, aliasId, 33);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 33, 33);
        report.income[0].sourceId = aliasId;
        bytes32 hash = settlement.getSettlementOperationHash(report, _roundIds());
        _authorizeTestOperation(2401, hash, address(settlement));
        vm.expectRevert(CustodianDAOIncomeVault.ReportMismatch.selector);
        settlement.settleDay(report, _roundIds(), hash);
        assertFalse(authorization.executedOperations(hash));
        assertFalse(incomeVault.released(day));
        assertEq(usd.balanceOf(address(incomeVault)), 66);
        report.income[0].sourceId = keccak256("YIELD_BASIS");
        hash = settlement.getSettlementOperationHash(report, _roundIds());
        _authorizeTestOperation(2402, hash, address(settlement));
        settlement.settleDay(report, _roundIds(), hash);
        assertTrue(incomeVault.released(day));
    }

    function test_VaultRejectsDuplicateYieldIDsEvenWhenAggregateMatches() public {
        uint256 day = startDay + 1;
        vm.warp(day * 1 days);
        bytes32 yieldId = keccak256("YIELD_BASIS");
        bytes32 other = keccak256("second-yield");
        _registerSource(other, CustodianDAOIncomeVault.Stream.Yield, 2500);
        usd.mint(address(this), 20);
        usd.approve(address(incomeVault), 20);
        incomeVault.deposit(day, yieldId, 10);
        incomeVault.deposit(day, other, 10);
        bytes32[] memory ids = new bytes32[](2);
        uint256[] memory amounts = new uint256[](2);
        ids[0] = yieldId;
        ids[1] = yieldId;
        amounts[0] = 10;
        amounts[1] = 10;
        vm.prank(address(settlement));
        vm.expectRevert(CustodianDAOIncomeVault.ReportMismatch.selector);
        incomeVault.releaseDay(day, ids, amounts, 0, 0);
        assertFalse(incomeVault.released(day));
    }

    function test_LPRoundingIsOrderIndependentAndConservesIncome() public {
        uint256 day = startDay + 1;
        CustodianDAODailySettlement.DayReport memory report = _report(day, 31, 0);
        report.averageBtcSats = 0;
        report.openingLiquidityReserveSats = 0;
        report.providers = new CustodianDAODailySettlement.LiquidityProvider[](32);
        for (uint256 i = 0; i < 32; i++) {
            report.providers[i] = CustodianDAODailySettlement.LiquidityProvider(address(uint160(0x3000 + i)), 1);
        }
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        _settle(report, _roundIds());
        uint256[] memory firstAllocation = new uint256[](32);
        uint256 total;
        for (uint256 i = 0; i < 32; i++) {
            address provider = address(uint160(0x3000 + i));
            firstAllocation[i] = settlement.lpClaimable(provider);
            assertLe(firstAllocation[i], 1);
            total += firstAllocation[i];
        }
        assertEq(total, 31);
        report.day = day + 1;
        for (uint256 i = 0; i < 32; i++) {
            report.providers[i].account = address(uint160(0x3000 + 31 - i));
        }
        _setRounds(day + 1);
        vm.warp((day + 2) * 1 days);
        _settle(report, _roundIds());
        for (uint256 i = 0; i < 32; i++) {
            assertEq(settlement.lpClaimable(address(uint160(0x3000 + i))), firstAllocation[i] * 2);
        }
    }

    function test_LPRoundingPrioritizesFractionalRemainderNotFirstAddress() public {
        uint256 day = startDay + 1;
        CustodianDAODailySettlement.DayReport memory report = _report(day, 2, 0);
        report.averageBtcSats = 0;
        report.openingLiquidityReserveSats = 0;
        report.providers[0].liquidityWeight = 2;
        report.providers[1].liquidityWeight = 1;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        _settle(report, _roundIds());
        assertEq(settlement.lpClaimable(address(0x2001)), 1);
        assertEq(settlement.lpClaimable(address(0x2002)), 1);
    }

    function test_SettlementUsesFinalizedPaginatedTWAPBeyond512Rounds() public {
        uint256 day = startDay + 1;
        vm.warp((day + 1) * 1 days);
        for (uint80 i = 1; i <= 601; i++) {
            feed.setRound(i, 100e8, day * 1 days + uint256(i - 1) * 144);
        }
        uint80[] memory first = new uint80[](512);
        for (uint80 i = 0; i < 512; i++) first[i] = i + 1;
        twap.append(day, first);
        uint80[] memory second = new uint80[](89);
        for (uint80 i = 0; i < 89; i++) second[i] = i + 513;
        vm.prank(address(0x9876));
        twap.append(day, second);
        CustodianDAODailySettlement.DayReport memory report = _report(day, 2, 0);
        report.averageBtcSats = 0;
        report.openingLiquidityReserveSats = 0;
        _settle(report, new uint80[](0));
        assertEq(settlement.getSettlement(day).btcPrice8, 100e8);
        assertEq(settlement.nextSettlementDay(), day + 1);
    }

    function testFuzz_LPAllocationConservesValueAndIgnoresOrder(uint128 amountSeed, uint128 weightSeed) public {
        uint256 day = startDay + 1;
        uint256 amount = bound(uint256(amountSeed), 1, 1e12);
        CustodianDAODailySettlement.DayReport memory report = _report(day, amount, 0);
        report.averageBtcSats = 0;
        report.openingLiquidityReserveSats = 0;
        report.providers[0].liquidityWeight = bound(uint256(weightSeed), 1, 1e12);
        report.providers[1].liquidityWeight = 7;
        uint256 totalWeight = report.providers[0].liquidityWeight + 7;
        _setRounds(day);
        vm.warp((day + 1) * 1 days);
        _settle(report, _roundIds());
        uint256 first = settlement.lpClaimable(address(0x2001));
        uint256 second = settlement.lpClaimable(address(0x2002));
        assertEq(first + second, amount);
        uint256 floorFirst = amount * report.providers[0].liquidityWeight / totalWeight;
        assertGe(first, floorFirst);
        assertLe(first, floorFirst + 1);
        CustodianDAODailySettlement.LiquidityProvider memory temp = report.providers[0];
        report.providers[0] = report.providers[1];
        report.providers[1] = temp;
        report.day++;
        _setRounds(day + 1);
        vm.warp((day + 2) * 1 days);
        _settle(report, _roundIds());
        assertEq(settlement.lpClaimable(address(0x2001)), first * 2);
        assertEq(settlement.lpClaimable(address(0x2002)), second * 2);
    }

    function _setRounds(uint256 day) internal {
        feed.setRound(1, 100e8, day * 1 days - 1 hours);
        feed.setRound(2, 200e8, day * 1 days + 12 hours);
        feed.setRound(3, 150e8, (day + 1) * 1 days);
    }

    function _roundIds() internal pure returns (uint80[] memory ids) {
        ids = new uint80[](3);
        ids[0] = 1;
        ids[1] = 2;
        ids[2] = 3;
    }

    function _report(uint256 day, uint256 income, uint256 fee)
        internal
        pure
        returns (CustodianDAODailySettlement.DayReport memory report)
    {
        CustodianDAODailySettlement.IncomeLine[] memory sources = new CustodianDAODailySettlement.IncomeLine[](1);
        sources[0] = CustodianDAODailySettlement.IncomeLine(keccak256("YIELD_BASIS"), income);
        CustodianDAODailySettlement.LiquidityProvider[] memory providers =
            new CustodianDAODailySettlement.LiquidityProvider[](2);
        providers[0] = CustodianDAODailySettlement.LiquidityProvider(address(0x2001), 3);
        providers[1] = CustodianDAODailySettlement.LiquidityProvider(address(0x2002), 1);
        report = CustodianDAODailySettlement.DayReport(
            day, 1e8, 0, 1e8, fee, 0, keccak256(abi.encode("evidence", day)), sources, providers
        );
    }

    function _settle(CustodianDAODailySettlement.DayReport memory report, uint80[] memory ids) internal {
        bytes32 hash = settlement.getSettlementOperationHash(report, ids);
        coordinator.setResult(hash, address(settlement));
        authorization.authorizeOperation(report.day, hash, address(settlement));
        uint256 funding = report.stakerWrapFeeIncome + report.stakerDeployFeeIncome;
        for (uint256 i = 0; i < report.income.length; i++) {
            funding += report.income[i].amount;
        }
        usd.mint(address(this), funding);
        usd.approve(address(incomeVault), funding);
        for (uint256 i = 0; i < report.income.length; i++) {
            if (report.income[i].amount > 0) {
                incomeVault.deposit(report.day, report.income[i].sourceId, report.income[i].amount);
            }
        }
        if (report.stakerWrapFeeIncome > 0) {
            incomeVault.deposit(report.day, incomeVault.WRAP_FEE(), report.stakerWrapFeeIncome);
        }
        if (report.stakerDeployFeeIncome > 0) {
            incomeVault.deposit(report.day, incomeVault.DEPLOY_FEE(), report.stakerDeployFeeIncome);
        }
        settlement.settleDay(report, ids, hash);
    }

    function _registerSource(bytes32 sourceId, CustodianDAOIncomeVault.Stream stream, uint256 sessionId) internal {
        bytes32 hash = incomeVault.getSetSourceOperationHash(sourceId, address(this), stream, true, address(0), 0);
        coordinator.setResult(hash, address(incomeVault));
        authorization.authorizeOperation(sessionId, hash, address(incomeVault));
        incomeVault.setSource(sourceId, address(this), stream, true, address(0), 0, hash);
    }
}
