"""Authenticate the service payment view against the enrolled mainnet backend."""
from eth_abi import encode
from web3 import Web3
from .bootstrap import artifact
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .zk_spv import inspect_binding, checkpoint_descriptor, constructor_args
from .zk_profiles import profile as zk_profile


def policy_hash(chain_id, state, adapter, policy):
    config = constructor_args(policy)[0]
    return Web3.keccak(encode(
        ["bytes32", "uint256", "address", "address", "bytes32", "bytes32", "bytes32", "bytes32", "uint64", "uint64", "uint64"],
        [Web3.keccak(text="SATOSHIFI_ZK_SPV_SERVICE_POLICY_V1"), int(chain_id), state, adapter,
         config[0], config[1], config[2], checkpoint_descriptor(policy), *config[3:]]))


def inspect_service_view(w3, view_address, policy, block, *, state_address=None):
    kind=zk_profile(policy)["view"]
    checked_runtime(w3, view_address, kind, block)
    view = w3.eth.contract(address=view_address, abi=artifact(kind)["abi"])
    def read(name): return getattr(view.functions, name)().call(block_identifier=block)
    state = read("state")
    if state_address is not None and state != state_address: raise ValueError("GAS_STATE_BINDING")
    _, adapter, records, _ = inspect_binding(w3, state, policy, block)
    prove_creation(w3, view.address, kind, [state], block)
    if (read("adapter") != adapter.address or read("policyHash") != policy_hash(w3.eth.chain_id, state, adapter.address, policy)
            or bytes(read("stateCodeHash")) != bytes.fromhex(records["relay"]["runtime_hash"].removeprefix("0x"))
            or bytes(read("adapterCodeHash")) != bytes.fromhex(records["spv_guard"]["runtime_hash"].removeprefix("0x"))):
        raise ValueError("GAS_STATE_BINDING")
    return view
