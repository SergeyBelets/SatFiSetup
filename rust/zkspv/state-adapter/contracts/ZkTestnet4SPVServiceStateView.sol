// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

import "./BitcoinTestnet4TransitionV2.sol";

interface IZkTestnet4SPVServiceState {
    function adapter() external view returns (address);
    function confederationId() external view returns (bytes32);
    function programImageId() external view returns (bytes32);
    function RELAY_PROFILE() external view returns (bytes32);
    function checkpointDescriptor() external view returns (bytes32);
    function maxStateAge() external view returns (uint64);
    function maxTipLag() external view returns (uint64);
    function baselineMaxTipAge() external view returns (uint64);
    function stateVersion() external view returns (uint64);
    function getState() external view returns (BitcoinTestnet4TransitionV2.State memory);
}
interface IZkTestnet4SPVServiceAdapter { function state() external view returns (address); }

/// @notice Read-only service-payment binding for the immutable testnet4 V2 state.
/// @dev Clients additionally authenticate exact state/adapter constructor provenance.
/// Checkpoint initialization permits funding its first update; Fresh is deliberately
/// not a condition of payment. This contract has no authority or state-write path.
contract ZkTestnet4SPVServiceStateView {
    bytes32 public constant POLICY_DOMAIN = keccak256("SATOSHIFI_ZK_SPV_SERVICE_POLICY_V1");
    bytes32 public constant TESTNET4_IMAGE = 0xa6d885a56488897d9b181b5d224652ad67e020d3868060a3e5aae20a4ae82ab8;
    address public immutable state;
    address public immutable adapter;
    bytes32 public immutable policyHash;
    bytes32 public immutable stateCodeHash;
    bytes32 public immutable adapterCodeHash;

    constructor(address state_) {
        require(state_.code.length != 0, "state");
        IZkTestnet4SPVServiceState source = IZkTestnet4SPVServiceState(state_);
        address adapter_ = source.adapter();
        require(adapter_.code.length != 0 && IZkTestnet4SPVServiceAdapter(adapter_).state() == state_, "adapter");
        require(source.programImageId() == TESTNET4_IMAGE && source.RELAY_PROFILE() == bytes32("PROOF_ONLY_V1"), "profile");
        require(source.confederationId() != 0 && source.checkpointDescriptor() != 0, "policy");
        state = state_; adapter = adapter_;
        stateCodeHash = state_.codehash; adapterCodeHash = adapter_.codehash;
        policyHash = keccak256(abi.encode(POLICY_DOMAIN, block.chainid, state_, adapter_,
            source.confederationId(), source.programImageId(), source.RELAY_PROFILE(),
            source.checkpointDescriptor(), source.maxStateAge(), source.maxTipLag(), source.baselineMaxTipAge()));
    }

    function snapshot() external view returns (bytes32 stateHash, uint256 version, bool initialized, bool halted) {
        require(state.codehash == stateCodeHash && adapter.codehash == adapterCodeHash, "code changed");
        BitcoinTestnet4TransitionV2.State memory current = IZkTestnet4SPVServiceState(state).getState();
        stateHash = current.commitment;
        version = IZkTestnet4SPVServiceState(state).stateVersion();
        initialized = current.anchor != 0 && current.commitment != 0 && current.knownCount != 0;
        halted = current.halted;
    }
}
