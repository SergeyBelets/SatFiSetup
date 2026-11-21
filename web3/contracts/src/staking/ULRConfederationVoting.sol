// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {ULRStaking} from "./ULRStaking.sol";
import {CustodianValidator} from "../federation/CustodianValidator.sol";

/// @notice Scoped voting view for the existing CustodianDAODecisionBoard ABI.
/// @dev No custody, transfers or delegation of the custodian-count authorization threshold.
contract ULRConfederationVoting {
    ULRStaking public immutable staking;
    CustodianValidator public immutable validator;

    constructor(address staking_, address validator_) {
        require(staking_.code.length > 0 && validator_.code.length > 0, "voting binding");
        require(CustodianValidator(validator_).candidateStaking() == staking_, "candidate binding");
        staking = ULRStaking(staking_);
        validator = CustodianValidator(validator_);
    }

    function firstEligibleDay() external view returns (uint256) {
        return staking.firstEligibleDay();
    }

    function totalEligibleStake(uint256 day) external view returns (uint256) {
        return staking.totalEligibleStake(day);
    }

    function eligibleStake(address owner, uint256 day) external view returns (uint256) {
        return staking.eligibleStake(owner, day);
    }

    function getDAOSigner(address owner) external view returns (address) {
        return staking.getVotingDelegate(owner, address(validator));
    }
}
