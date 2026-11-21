// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {Script, console} from "forge-std/Script.sol";
import {CustodianDAOToken} from "../contracts/src/tokens/CustodianDAOToken.sol";
import {CustodianDAOStaking} from "../contracts/src/federation/CustodianDAOStaking.sol";
import {CustodianDAORateController} from "../contracts/src/federation/CustodianDAORateController.sol";
import {CustodianDAODailySettlement} from "../contracts/src/federation/CustodianDAODailySettlement.sol";
import {ChainlinkBTCDailyTWAP} from "../contracts/src/federation/ChainlinkBTCDailyTWAP.sol";
import {CustodianDAOIncomeVault} from "../contracts/src/federation/CustodianDAOIncomeVault.sol";
import {CustodianDAODecisionBoard} from "../contracts/src/federation/CustodianDAODecisionBoard.sol";
import {ConfederationFactory} from "../contracts/src/federation/ConfederationFactory.sol";
import {IConfederationFactory} from "../contracts/src/federation/interfaces/IConfederationFactory.sol";
import {CustodianValidator} from "../contracts/src/federation/CustodianValidator.sol";
import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IChainlinkBTCUSD} from "../contracts/src/federation/ChainlinkBTCDailyTWAP.sol";
import {ScriptArtifactIdentity} from "./ScriptArtifactIdentity.sol";

contract DeployCustodianDAO is ScriptArtifactIdentity {
    IConfederationFactory.CustodianDAOConfig public deployedDAO;

    /// @notice Resume registration of an already deployed graph without creating another token/DAO.
    /// Run only after the matching enforced session and custodian approvals are complete.
    function registerExisting(
        address factoryAddress,
        string calldata confederationId,
        IConfederationFactory.CustodianDAOConfig calldata dao,
        uint256 finalizedSession
    ) external {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        if (block.chainid == 1) _requireArtifact(factoryAddress, "ConfederationFactory");
        ConfederationFactory factory=ConfederationFactory(factoryAddress);
        IConfederationFactory.CustodianDAOConfig memory existing=factory.getCustodianDAO(confederationId);
        if(existing.settlement!=address(0)) {
            require(keccak256(abi.encode(existing))==keccak256(abi.encode(dao)),"different DAO already registered");
            return;
        }
        IConfederationFactory.ConfederationConfig memory config=factory.getConfederation(confederationId);
        bytes32 op=factory.getRegisterCustodianDAOOperationHash(confederationId,dao);
        require(CustodianValidator(config.custodianValidator).meetsMinimumCustodians(op),"AWAITING_CUSTODIAN_APPROVALS");
        FROSTAuthorization auth=FROSTAuthorization(config.frostMultisig);
        vm.startBroadcast();
        if(!auth.isOperationApproved(op,factoryAddress)) auth.authorizeOperation(finalizedSession,op,factoryAddress);
        factory.registerCustodianDAO(confederationId,dao,op);
        vm.stopBroadcast();
        require(factory.getCustodianDAO(confederationId).settlement==dao.settlement,"DAO registration failed");
    }

    function run() external {
        require(block.chainid == vm.envUint("EXPECTED_CHAIN_ID"), "chain mismatch");
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        string memory confederationId = vm.envString("CONFEDERATION_ID");
        ConfederationFactory factory = ConfederationFactory(vm.envAddress("CONFEDERATION_FACTORY"));
        if (block.chainid == 1) _requireArtifact(address(factory), "ConfederationFactory");
        IConfederationFactory.ConfederationConfig memory config = factory.getConfederation(confederationId);
        require(config.isActive, "confederation inactive");
        require(factory.getCustodianDAO(confederationId).settlement == address(0), "DAO already registered");
        require(config.frostMultisig.code.length > 0, "authorization missing");
        require(config.custodianValidator.code.length > 0, "custodian validator missing");
        if (block.chainid == 1) {
            _requireArtifact(config.frostMultisig, "DKGAuthorization");
            _requireArtifact(config.custodianValidator, "CustodianValidator");
        }
        require(
            address(FROSTAuthorization(config.frostMultisig).coordinator()) == factory.frostCoordinator(),
            "coordinator mismatch"
        );
        require(
            CustodianValidator(config.custodianValidator).frostMultisig() == config.frostMultisig, "validator mismatch"
        );

        string memory tokenName = vm.envString("DAO_TOKEN_NAME");
        string memory tokenSymbol = vm.envString("DAO_TOKEN_SYMBOL");
        address tokenRecipient = vm.envAddress("DAO_TOKEN_RECIPIENT");
        address rewardToken = vm.envAddress("DAO_REWARD_TOKEN");
        address btcUsdFeed = vm.envAddress("BTC_USD_CHAINLINK_FEED");
        address treasury = vm.envAddress("DAO_TREASURY");
        bool initialUseReserve = vm.envBool("DAO_USE_RESERVE");
        require(bytes(tokenName).length > 0 && bytes(tokenSymbol).length > 0, "token metadata missing");
        require(tokenRecipient != address(0), "token recipient missing");
        require(treasury != address(0), "treasury missing");
        require(rewardToken.code.length > 0, "reward token missing");
        require(IERC20Metadata(rewardToken).decimals() <= 18, "unsupported reward token decimals");
        require(btcUsdFeed.code.length > 0, "BTC/USD feed missing");
        require(IChainlinkBTCUSD(btcUsdFeed).decimals() == 8, "unsupported BTC/USD feed decimals");
        if (block.chainid == 1) {
            require(rewardToken.codehash == vm.envBytes32("DAO_REWARD_TOKEN_CODE_HASH"), "reward token code hash");
            require(IERC20Metadata(rewardToken).decimals() == vm.envUint("DAO_REWARD_TOKEN_DECIMALS"), "reward token decimals");
            require(btcUsdFeed.codehash == vm.envBytes32("BTC_USD_CHAINLINK_FEED_CODE_HASH"), "BTC/USD feed code hash");
            require(rewardToken.codehash != keccak256(vm.getDeployedCode("DAOTestUSD.sol:DAOTestUSD")), "test reward token forbidden");
            require(btcUsdFeed.codehash != keccak256(vm.getDeployedCode("DAOMockFeed.sol:DAOMockFeed")), "test feed forbidden");
        }

        vm.startBroadcast(deployerKey);
        CustodianDAOToken token = new CustodianDAOToken(tokenName, tokenSymbol, tokenRecipient);
        CustodianDAOStaking staking = new CustodianDAOStaking(address(token), rewardToken, config.custodianValidator);
        CustodianDAORateController controller =
            new CustodianDAORateController(config.custodianValidator, address(staking), initialUseReserve);
        ChainlinkBTCDailyTWAP twap = new ChainlinkBTCDailyTWAP(btcUsdFeed);
        CustodianDAOIncomeVault incomeVault = new CustodianDAOIncomeVault(rewardToken, config.frostMultisig);
        CustodianDAODecisionBoard decisions = new CustodianDAODecisionBoard(address(staking), config.frostMultisig);
        CustodianDAODailySettlement settlement = new CustodianDAODailySettlement(
            rewardToken,
            address(staking),
            address(controller),
            config.frostMultisig,
            address(twap),
            address(incomeVault)
        , treasury);
        staking.setDistributor(address(settlement));
        incomeVault.setSettlement(address(settlement));
        vm.stopBroadcast();

        deployedDAO = IConfederationFactory.CustodianDAOConfig(
            address(token), address(staking), address(controller), address(twap), address(incomeVault),
            address(decisions), address(settlement)
        );

        console.log("CustodianDAOToken:", address(token));
        console.log("CustodianDAOStaking:", address(staking));
        console.log("CustodianDAORateController:", address(controller));
        console.log("ChainlinkBTCDailyTWAP:", address(twap));
        console.log("CustodianDAOIncomeVault:", address(incomeVault));
        console.log("CustodianDAODecisionBoard:", address(decisions));
        console.log("CustodianDAODailySettlement:", address(settlement));
        console.log("First eligible UTC day:", staking.firstEligibleDay());
        bytes32 registrationHash = factory.getRegisterCustodianDAOOperationHash(confederationId, deployedDAO);
        console.log("Pending factory registration operation hash:");
        console.logBytes32(registrationHash);
        console.log("Authorize this hash for the confederation factory and collect custodian approvals, then call:");
        console.logBytes(abi.encodeCall(factory.registerCustodianDAO, (confederationId, deployedDAO, registrationHash)));
    }
}
