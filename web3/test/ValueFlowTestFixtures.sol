// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import {FROSTAuthorization} from "../contracts/src/FROSTAuthorization.sol";
import {BitcoinBatchRegistry} from "../contracts/src/federation/BitcoinBatchRegistry.sol";
import {PoolMpToken} from "../contracts/src/tokens/PoolMpToken.sol";

/// @dev Only the finalized cryptographic result is stubbed. Tests use production
/// FROSTAuthorization, pools, fee manager, NFT usage, escrow, tokens and settlement.
contract ValueFlowVerifier {
    function verify(bytes calldata, bytes32, bytes calldata, string calldata) external pure returns (bool) {
        return true;
    }
}

contract ValueFlowCoordinator {
    address public immutable defaultVerifier = address(new ValueFlowVerifier());
    address private immutable signer;
    bytes32 private message;
    address private target;
    uint256 private session;

    constructor(address signer_) { signer = signer_; }

    function approve(FROSTAuthorization authorization, bytes32 message_, address target_) external {
        message = message_;
        target = target_;
        authorization.authorizeOperation(++session, message_, target_);
    }

    function getSessionParticipants(uint256) external view returns (address[] memory participants) {
        participants = new address[](1);
        participants[0] = signer;
    }

    function getAuthorizationResult(uint256)
        external view returns (bytes memory, bytes32, bool, address, address, uint256, uint256, bytes memory)
    {
        return (abi.encodePacked(bytes32(uint256(1))), message, true, address(0), target, 1, 1, hex"01");
    }
}

/// @dev Deterministic batch/proof boundary. Mainnet proof validity is covered by
/// the separate real-header relay integration; no escrow behavior is stubbed.
contract ValueFlowBatches {
    address public feeReserve;
    FROSTAuthorization public immutable authorization;
    mapping(bytes32 => bytes32) public replacementOf;
    mapping(bytes32 => BitcoinBatchRegistry.State) public batchState;
    mapping(bytes32 => bytes32[]) private inputs;

    constructor(address authorization_) { authorization = FROSTAuthorization(authorization_); }

    function configure(bytes32 batch, bytes32 input, BitcoinBatchRegistry.State state) external {
        delete inputs[batch];
        inputs[batch].push(input);
        batchState[batch] = state;
    }

    function setState(bytes32 batch, BitcoinBatchRegistry.State state) external { batchState[batch] = state; }
    function getInputs(bytes32 batch) external view returns (bytes32[] memory) { return inputs[batch]; }
}

contract ValueFlowProof {
    function testHelpersEnabled() external pure returns (bool) { return false; }
    function getBlockStatus(bytes32) external pure returns (bool, uint64) { return (true, 6); }
    function checkTxInclusion(bytes32, bytes32, bytes32[] calldata, uint256) external pure returns (bool) {
        return true;
    }
}

contract ValueFlowMiningPool {
    PoolMpToken public mpToken = new PoolMpToken("Value Flow MP", "VFMP", address(this), false);
    function mint(address recipient, uint256 amount) external { mpToken.mint(recipient, amount); }
}

contract ValueFlowDeposits {
    address public immutable spendRecorder;
    mapping(address => bytes32) public poolScripts;
    uint256 public burnedSats;

    constructor(address escrow, address miningPool) {
        spendRecorder = escrow;
        poolScripts[miningPool] = keccak256("registered wallet");
    }

    function recordSpend(bytes calldata, uint256 amount) external {
        require(msg.sender == spendRecorder, "recorder");
        burnedSats += amount;
    }
    function recordFamily(bytes32) external view { require(msg.sender == spendRecorder,"recorder"); }
}
