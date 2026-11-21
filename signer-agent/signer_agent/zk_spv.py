"""Explicit network-pinned proof-only backend; no witness-selected network.

Policy V2 is committed inside the existing signed invitation. V1 remains the
ordinary header relay. Artifact builds stay isolated to preserve verifier pins.
"""
import hashlib

from web3 import Web3

from .zk_profiles import MAINNET_IMAGE, profile as zk_profile
PROFILE = b"PROOF_ONLY_V1".ljust(32, b"\0")
VERIFIER_VERSION = "3.1.0"
VERIFIER_SELECTOR = bytes.fromhex("e71e6620")


def require_patched_build(compiled):
    source = "../vendor/risc0-ethereum-3.1.0/contracts/src/groth16/RiscZeroGroth16Verifier.sol"
    expected = "0x8cb8a742d0683f4dd2390ca32e4fd4e537a105d1ba2d4dcf419afc772e15be94"
    if compiled.get("metadata", {}).get("sources", {}).get(source, {}).get("keccak256") != expected:
        raise ValueError("ZK_VERIFIER_3_1_BUILD_REQUIRED")


def is_zk(policy):
    return isinstance(policy, dict) and policy.get("version") == "2"


def policy_from_checkpoint(policy, identifier, max_state_age_seconds, max_tip_lag_seconds):
    """Public review candidate; the program/network pair comes from source pins."""
    from .bitcoin_network import validate_policy
    from copy import deepcopy
    policy=deepcopy(validate_policy(policy))
    if policy['version']!='1' or not isinstance(identifier,str) or not identifier:
        raise ValueError('ZK_CHECKPOINT_POLICY_REQUIRED')
    selected=zk_profile(policy)
    policy.update(version='2',max_reorg_depth='100',zk_spv=dict(backend='zk-state-v2',
        relay_profile='PROOF_ONLY_V1',program_image_id=selected['image'],
        confederation_id=Web3.keccak(text=identifier).hex(),
        max_state_age_seconds=max_state_age_seconds,max_tip_lag_seconds=max_tip_lag_seconds))
    return validate_policy(policy)


def validate_zk_policy(policy):
    from .bitcoin_network import _decimal, _hex
    z = policy.get("zk_spv")
    if not isinstance(z, dict) or set(z) != {
        "backend", "relay_profile", "program_image_id", "confederation_id",
        "max_state_age_seconds", "max_tip_lag_seconds",
    }:
        raise ValueError("ZK_POLICY_FIELDS")
    selected=zk_profile(policy)
    if (z["backend"] != "zk-state-v2"
            or z["relay_profile"] != "PROOF_ONLY_V1"
            or z["program_image_id"] != selected['image']):
        raise ValueError("ZK_PROFILE_UNSUPPORTED")
    if _hex(z["confederation_id"], 64) == bytes(32):
        raise ValueError("ZK_CONFEDERATION_REQUIRED")
    _decimal(z["max_state_age_seconds"], 1, 86400)
    _decimal(z["max_tip_lag_seconds"], 1, int(policy["max_tip_age_seconds"]))
    # This image has a consensus reorg bound of 100, not a configurable relay cap.
    if policy["max_reorg_depth"] != "100":
        raise ValueError("ZK_REORG_BOUND")


def validate_confederation(policy, identifier):
    if is_zk(policy) and policy["zk_spv"]["confederation_id"] != Web3.keccak(text=identifier).hex():
        raise ValueError("ZK_CONFEDERATION_MISMATCH")


def constructor_args(policy):
    from .bitcoin_network import validate_policy
    policy = validate_policy(policy, policy.get('network'))
    if not is_zk(policy):
        raise ValueError("ZK_POLICY_REQUIRED")
    z = policy["zk_spv"]
    return [
        (bytes.fromhex(z["confederation_id"]), bytes.fromhex(z["program_image_id"]), PROFILE,
         int(z["max_state_age_seconds"]), int(z["max_tip_lag_seconds"]), int(policy["max_tip_age_seconds"])),
        bytes.fromhex(policy["checkpoint_header"]), int(policy["checkpoint_height"]),
        int(policy["checkpoint_chainwork"], 16),
        [bytes.fromhex(h) for h in policy["checkpoint_predecessors"]],
    ]


def checkpoint_descriptor(policy):
    def framed(domain, parts):
        b = b"SATOSHIFI_FORK_V1\0" + len(domain).to_bytes(4, "big") + domain
        return hashlib.sha256(b + b"".join(len(p).to_bytes(8, "big") + p for p in parts)).digest()
    _, raw, height, work, prior = constructor_args(policy)
    network=zk_profile(policy)['network']
    return framed((network+'-checkpoint-v1').encode(), [('bitcoin-'+network).encode(), raw,
        height.to_bytes(8, "big"), work.to_bytes(32, "big"), b"".join(prior)])


def inspect_binding(w3, state_address, policy, block, *, adapter_address=None, prove=True):
    """Authenticate parent initcode, deterministic children and immutable policy."""
    from .bootstrap import artifact
    from .bitcoin_trust import checked_runtime
    from .contract_provenance import prove_creation
    import rlp

    args = constructor_args(policy)
    selected=zk_profile(policy)
    state_address = Web3.to_checksum_address(state_address)
    state = w3.eth.contract(address=state_address, abi=artifact(selected['state'])["abi"])
    def read(c, method):
        return getattr(c.functions, method)().call(block_identifier=block)
    records = {}
    for name, address, kind in [
        ("relay", state_address, selected['state']),
        ("spv_guard", read(state, "adapter"), selected['adapter']),
        ("zk_verifier", read(state, "verifier"), "RiscZeroGroth16Verifier"),
    ]:
        records[name] = dict(address=address, artifact=kind,
            runtime_hash=checked_runtime(w3, address, kind, block))
    adapter = w3.eth.contract(address=records["spv_guard"]["address"], abi=artifact(selected['adapter'])["abi"])
    verifier = w3.eth.contract(address=records["zk_verifier"]["address"], abi=artifact("RiscZeroGroth16Verifier")["abi"])
    if read(verifier, "VERSION") != VERIFIER_VERSION or bytes(read(verifier, "SELECTOR")) != VERIFIER_SELECTOR:
        raise ValueError("ZK_VERIFIER_3_1_REQUIRED")
    expected = dict(confederationId=args[0][0], programImageId=args[0][1], RELAY_PROFILE=PROFILE,
        maxStateAge=args[0][3], maxTipLag=args[0][4], baselineMaxTipAge=args[0][5],
        checkpointDescriptor=checkpoint_descriptor(policy))
    if any(read(state, k) != v for k, v in expected.items()):
        raise ValueError("ZK_STATE_POLICY_MISMATCH")
    if (read(adapter, "state") != state_address or read(adapter, "testHelpersEnabled") is not False
            or adapter_address is not None and Web3.to_checksum_address(adapter_address) != adapter.address
            or bytes(read(state, "verifierCodeHash")) != bytes.fromhex(records["zk_verifier"]["runtime_hash"].removeprefix("0x"))):
        raise ValueError("ZK_ADAPTER_BINDING_MISMATCH")
    for name, nonce in (("zk_verifier", 1), ("spv_guard", 2)):
        expected_address = Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(state_address[2:]), nonce]))[-20:])
        if records[name]["address"] != expected_address:
            raise ValueError("ZK_CHILD_ORIGIN_MISMATCH")
    proofs = None
    if prove:
        parent = prove_creation(w3, state_address, selected['state'], args, block)
        if parent["method"] != "direct":
            raise ValueError("ZK_STATE_DIRECT_CREATION_REQUIRED")
        proofs = {"relay": parent}
        for name in ("spv_guard", "zk_verifier"):
            proofs[name] = dict(parent, **records[name], method="canonical_constructor", parent=parent)
    return state, adapter, records, proofs


def inspect_live(bitcoin, state, policy, *, block_identifier, now=None):
    """Fresh at the confirmed EVM snapshot and consistent with independent Core."""
    from .bitcoin_network import check_core, _core_header, _fresh
    network=zk_profile(policy)['network']
    core = check_core(bitcoin, network, policy, now=now)
    if state.functions.halted().call():
        raise ValueError("MAINNET_RELAY_HALTED")
    s = state.functions.getState().call(block_identifier=block_identifier)
    if s[8]:
        raise ValueError("MAINNET_RELAY_HALTED")
    height = int(s[3])
    if height > core["blocks"]:
        raise ValueError("MAINNET_RELAY_AHEAD_OF_CORE")
    header, _ = _core_header(bitcoin, height)
    if (bytes.fromhex(header["hash"])[::-1] != bytes(s[2])
            or int(header["chainwork"], 16) != s[4] or header["time"] != s[5]):
        raise ValueError("ZK_CORE_STATE_MISMATCH")
    lag = core["blocks"] - height
    import time
    clock = int(time.time()) if now is None else now
    if lag > int(policy["max_relay_lag_blocks"]) or not state.functions.isFresh().call(block_identifier=block_identifier):
        raise ValueError("MAINNET_RELAY_STALE")
    _fresh(s[5], dict(policy, max_tip_age_seconds=policy["zk_spv"]["max_tip_lag_seconds"]), clock)
    if bitcoin.call("getblockhash", core["blocks"]) != core["bestblockhash"]:
        raise ValueError("MAINNET_CORE_REORG_DURING_CHECK")
    return dict(status="MAINNET_RELAY_READY", backend="zk-state-v2", network=network,
        height=str(height), core_height=str(core["blocks"]), head=bytes(s[2]).hex(), lag=str(lag))


def review_consumer(w3, invite, target, kind, intent, block):
    """Check the enrolled SPV choice before Ethereum bootstrap authorization."""
    policy = invite.get("bitcoin_policy")
    from .bootstrap import artifact
    from .bitcoin_trust import checked_runtime
    from .contract_provenance import prove_creation
    def contract(address, name):
        checked_runtime(w3, address, name, block)
        prove_creation(w3, address, name, None, block)
        return w3.eth.contract(address=address, abi=artifact(name)["abi"])
    def read(c, name):
        return getattr(c.functions, name)().call(block_identifier=block)
    from .bootstrap_confederation_dao import enabled
    escrow_kind = "BitcoinWithdrawalEscrowV2" if enabled(invite) else "BitcoinWithdrawalEscrow"
    consumers = []
    if kind in ("BitcoinDepositRegistry", "BitcoinWithdrawalEscrow", "BitcoinWithdrawalEscrowV2"):
        consumers.append(target)
    elif kind in ("MiningPoolV1", "ConfederationBitcoinReserve"):
        consumers.extend([contract(read(target, "deposits"), "BitcoinDepositRegistry"),
                          contract(read(target, "escrow"), escrow_kind)])
    elif kind == "BitcoinAddressRegistry":
        consumers.extend([contract(read(target, "deposits"), "BitcoinDepositRegistry"),
                          contract(read(target, "escrow"), escrow_kind)])
    elif kind == "BitcoinBatchRegistry" and intent.get("hash_method") == "getSetWithdrawalEscrowOperationHash":
        args = intent.get("hash_args")
        if not isinstance(args, list) or len(args) != 1:
            raise ValueError("ZK_CONSUMER_BINDING")
        escrow = contract(args[0], escrow_kind)
        if read(escrow, "batches") != target.address:
            raise ValueError("ZK_CONSUMER_BINDING")
        consumers.append(escrow)
    addresses = {read(c, "spv") for c in consumers}
    if len(addresses) > 1 or any(read(c, "depth") != int(invite["bitcoin_confirmations"]) for c in consumers):
        raise ValueError("ZK_CONSUMER_BINDING")
    for address in addresses:
        if is_zk(policy):
            kind=zk_profile(policy)['adapter']
            checked_runtime(w3, address, kind, block)
            adapter = w3.eth.contract(address=address, abi=artifact(kind)["abi"])
            inspect_binding(w3, read(adapter, "state"), policy, block, adapter_address=address)
        elif policy is not None:
            from .bitcoin_network import relay_artifact, relay_constructor_args, validate_policy
            validate_policy(policy, invite["bitcoin_network"])
            checked_runtime(w3, address, "MainnetSPVGuard", block)
            adapter = w3.eth.contract(address=address, abi=artifact("MainnetSPVGuard")["abi"])
            relay = read(adapter, "relay")
            prove_creation(w3, address, "MainnetSPVGuard", [relay, int(policy["max_tip_age_seconds"])], block)
            prove_creation(w3, relay, relay_artifact(invite["bitcoin_network"]), relay_constructor_args(policy), block)
        else:
            prove_creation(w3, address, "RegtestHeaderRelay", [], block)
