// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IConfederationFactory} from "./interfaces/IConfederationFactory.sol";
import {CustodianDAOStaking} from "./CustodianDAOStaking.sol";
import {CustodianDAORateController} from "./CustodianDAORateController.sol";
import {CustodianDAODailySettlement} from "./CustodianDAODailySettlement.sol";
import {CustodianDAODecisionBoard} from "./CustodianDAODecisionBoard.sol";
import {CustodianDAOIncomeVault} from "./CustodianDAOIncomeVault.sol";

/// @dev Deployed by the factory constructor so checking the DAO graph requires no external deployment trust.
contract CustodianDAOConfigValidator {
    error InvalidCustodianDAO();

    function validate(
        IConfederationFactory.CustodianDAOConfig calldata dao, address custodianValidator, address authorization
    ) external view {
        if (
            dao.token.code.length == 0 || dao.staking.code.length == 0 || dao.controller.code.length == 0
                || dao.twap.code.length == 0 || dao.incomeVault.code.length == 0 || dao.decisions.code.length == 0
                || dao.settlement.code.length == 0
        ) revert InvalidCustodianDAO();
        CustodianDAOStaking staking = CustodianDAOStaking(dao.staking);
        CustodianDAORateController controller = CustodianDAORateController(dao.controller);
        CustodianDAODailySettlement settlement = CustodianDAODailySettlement(dao.settlement);
        CustodianDAOIncomeVault vault = CustodianDAOIncomeVault(dao.incomeVault);
        CustodianDAODecisionBoard decisions = CustodianDAODecisionBoard(dao.decisions);
        if (
            address(staking.stakeToken()) != dao.token || address(staking.custodianValidator()) != custodianValidator
                || staking.distributor() != dao.settlement || address(controller.staking()) != dao.staking
                || address(controller.validator()) != custodianValidator
                || address(settlement.staking()) != dao.staking || address(settlement.controller()) != dao.controller
                || address(settlement.twap()) != dao.twap || address(settlement.incomeVault()) != dao.incomeVault
                || address(settlement.authorization()) != authorization
                || address(staking.rewardToken()) != address(settlement.rewardToken())
                || address(vault.rewardToken()) != address(settlement.rewardToken())
                || address(vault.authorization()) != authorization || vault.settlement() != dao.settlement
                || address(decisions.staking()) != dao.staking || address(decisions.authorization()) != authorization
        ) revert InvalidCustodianDAO();
    }
}
