// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./BitcoinWithdrawalRecovery.sol";

/// @notice Attests that recovery code started with its genuine constructor and empty proof storage.
contract BitcoinWithdrawalRecoveryFactory {
    address public immutable escrow;
    mapping(address => bool) public deployed;
    mapping(bytes32 => address) private policies;

    event RecoveryDeployed(address indexed policy, bytes32 indexed configHash);

    constructor() {
        escrow = msg.sender;
    }

    function policyFor(address reserve, bytes32 sourceScriptHash, bytes32 successorScriptHash,
        bytes32 sponsorScriptHash, uint32 recoveryHeight, uint32 recoveryDepth,
        uint64 maxMinerFeeSats, address pauseGuardian) external view returns (address)
    {
        return policies[_configuration(reserve, sourceScriptHash, successorScriptHash, sponsorScriptHash,
            recoveryHeight, recoveryDepth, maxMinerFeeSats, pauseGuardian)];
    }

    function deployRecovery(address reserve, bytes32 sourceScriptHash, bytes32 successorScriptHash,
        bytes32 sponsorScriptHash, uint32 recoveryHeight, uint32 recoveryDepth,
        uint64 maxMinerFeeSats, address pauseGuardian) external returns (address policy)
    {
        bytes32 configHash = _configuration(reserve, sourceScriptHash, successorScriptHash, sponsorScriptHash,
            recoveryHeight, recoveryDepth, maxMinerFeeSats, pauseGuardian);
        policy = policies[configHash];
        if (policy != address(0)) return policy;
        policy = address(new BitcoinWithdrawalRecovery(escrow, reserve, sourceScriptHash, successorScriptHash,
            sponsorScriptHash, recoveryHeight, recoveryDepth, maxMinerFeeSats, pauseGuardian));
        policies[configHash] = policy;
        deployed[policy] = true;
        emit RecoveryDeployed(policy, configHash);
    }

    function _configuration(address reserve, bytes32 sourceScriptHash, bytes32 successorScriptHash,
        bytes32 sponsorScriptHash, uint32 recoveryHeight, uint32 recoveryDepth, uint64 maxMinerFeeSats,
        address pauseGuardian) private pure returns (bytes32)
    {
        return keccak256(abi.encode(reserve, sourceScriptHash, successorScriptHash, sponsorScriptHash,
            recoveryHeight, recoveryDepth, maxMinerFeeSats, pauseGuardian));
    }
}
