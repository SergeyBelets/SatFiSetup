// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "../core/BitcoinTransitionV2.sol";

interface IZkSPVServiceState {
    function adapter() external view returns (address);
    function confederationId() external view returns (bytes32);
    function programImageId() external view returns (bytes32);
    function RELAY_PROFILE() external view returns (bytes32);
    function checkpointDescriptor() external view returns (bytes32);
    function maxStateAge() external view returns (uint64);
    function maxTipLag() external view returns (uint64);
    function baselineMaxTipAge() external view returns (uint64);
    function stateVersion() external view returns (uint64);
    function getState() external view returns (BitcoinTransitionV2.State memory);
}
interface IZkSPVServiceAdapter { function state() external view returns (address); }

/// @notice Read-only service-payment binding for the immutable mainnet V2 state.
/// @dev Clients additionally authenticate exact state/adapter constructor provenance.
/// Checkpoint initialization permits funding its first update; Fresh is deliberately
/// not a condition of payment. This contract has no authority or state-write path.
contract ZkSPVServiceStateView {
    bytes32 public constant POLICY_DOMAIN = keccak256("SATOSHIFI_ZK_SPV_SERVICE_POLICY_V1");
    bytes32 public constant MAINNET_IMAGE = 0xa4c00c90dcc2600acd756df56ae4915e2180d0ebacc20c157382ec184fe0adea;
    address public immutable state;
    address public immutable adapter;
    bytes32 public immutable policyHash;
    bytes32 public immutable stateCodeHash;
    bytes32 public immutable adapterCodeHash;

    constructor(address state_) {
        require(state_.code.length != 0, "state");
        IZkSPVServiceState source = IZkSPVServiceState(state_);
        address adapter_ = source.adapter();
        require(adapter_.code.length != 0 && IZkSPVServiceAdapter(adapter_).state() == state_, "adapter");
        require(source.programImageId() == MAINNET_IMAGE && source.RELAY_PROFILE() == bytes32("PROOF_ONLY_V1"), "profile");
        require(source.confederationId() != 0 && source.checkpointDescriptor() != 0, "policy");
        state = state_; adapter = adapter_;
        stateCodeHash = state_.codehash; adapterCodeHash = adapter_.codehash;
        policyHash = keccak256(abi.encode(POLICY_DOMAIN, block.chainid, state_, adapter_,
            source.confederationId(), source.programImageId(), source.RELAY_PROFILE(),
            source.checkpointDescriptor(), source.maxStateAge(), source.maxTipLag(), source.baselineMaxTipAge()));
    }

    function snapshot() external view returns (bytes32 stateHash, uint256 version, bool initialized, bool halted) {
        require(state.codehash == stateCodeHash && adapter.codehash == adapterCodeHash, "code changed");
        BitcoinTransitionV2.State memory current = IZkSPVServiceState(state).getState();
        stateHash = current.commitment;
        version = IZkSPVServiceState(state).stateVersion();
        initialized = current.anchor != 0 && current.commitment != 0 && current.knownCount != 0;
        halted = current.halted;
    }
}
