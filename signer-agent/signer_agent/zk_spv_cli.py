"""Permissionless, durable proof submission and header materialization.

Run with python -m signer_agent.zk_spv_cli. Proof submission is always a separate
transaction; no monetary operation can roll back an accepted permanent halt.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from .evm_rpc import evm_web3

from .bootstrap import Bootstrap
from .bitcoin_network import validate_policy, check_core
from .evm_network import read_block
from .operations import canonical
from .zk_spv import VERIFIER_SELECTOR, inspect_binding, inspect_live, checkpoint_descriptor
from .zk_profiles import journal_network


def _validate_journal_scope(chain_id, state_address, policy, journal):
    if (len(journal) != 1216
            or journal[:32] != b"SATOSHIFI_TRANSITION_V2".ljust(32, b"\0")
            or journal[32:64] != journal_network(policy)
            or int.from_bytes(journal[64:96], "big") != int(chain_id)
            or journal[96:128] != bytes(12) + bytes.fromhex(state_address[2:])
            or journal[128:160] != checkpoint_descriptor(policy)):
        raise ValueError("ZK_PROOF_DEPLOYMENT_MISMATCH")


def _validate_journal_batch(policy, journal):
    if not 1 <= int.from_bytes(journal[640:672], "big") <= int(policy["max_headers_per_sync"]):
        raise ValueError("ZK_BATCH_POLICY_LIMIT")


def validate_transition_journal(chain_id, state_address, policy, journal):
    _validate_journal_scope(chain_id, state_address, policy, journal)
    _validate_journal_batch(policy, journal)


def transition_step(chain_id, state_address, policy, journal, seal):
    if len(seal) != 260:
        raise ValueError("ZK_PROOF_DEPLOYMENT_MISMATCH")
    _validate_journal_scope(chain_id, state_address, policy, journal)
    if seal[:4] != VERIFIER_SELECTOR:
        raise ValueError("ZK_VERIFIER_3_1_REQUIRED")
    _validate_journal_batch(policy, journal)
    return dict(id="transition", kind="call", contract="relay", method="submitTransition",
                args=["0x" + journal.hex(), "0x" + seal.hex()])


def materialization_step(value):
    from .bitcoin_network import _decimal, _hex
    if not isinstance(value, dict) or set(value) != {"height", "header", "siblings"}:
        raise ValueError("ZK_HEADER_MEMBERSHIP_SCHEMA")
    _decimal(value["height"], 2016, 2**64 - 1)
    _hex(value["header"], 160)
    if not isinstance(value["siblings"], list) or len(value["siblings"]) != 64:
        raise ValueError("ZK_HEADER_MEMBERSHIP_SCHEMA")
    for sibling in value["siblings"]:
        _hex(sibling, 64)
    return dict(id="header", kind="call", contract="spv_guard", method="materializeHeader",
                args=[value["height"], "0x" + value["header"], ["0x" + s for s in value["siblings"]]])


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if argv and argv[0]=='policy':
        parser=argparse.ArgumentParser(description='Create a pinned mainnet/testnet4 ZK review policy; no RPC or transactions.')
        parser.add_argument('--source-policy',type=Path,required=True)
        parser.add_argument('--confederation-id',required=True)
        parser.add_argument('--max-state-age-seconds',required=True)
        parser.add_argument('--max-tip-lag-seconds',required=True)
        parser.add_argument('--output',type=Path)
        a=parser.parse_args(argv[1:])
        from .zk_spv import policy_from_checkpoint
        value=policy_from_checkpoint(json.loads(a.source_policy.read_text()),a.confederation_id,
            a.max_state_age_seconds,a.max_tip_lag_seconds)
        encoded=json.dumps(value,indent=2)+'\n'
        if a.output:
            with a.output.open('x') as stream:stream.write(encoded)
        else:print(encoded,end='')
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["policy", "status", "submit", "materialize"])
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--chain-id", required=True, choices=["1", "31337", "11155111"])
    parser.add_argument("--evm-confirmations", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--zk-state", required=True)
    parser.add_argument("--bitcoin-rpc", required=True)
    parser.add_argument("--bitcoin-cookie", required=True)
    parser.add_argument("--identity-file")
    parser.add_argument("--journal", help="Exact 1216-byte public transition journal")
    parser.add_argument("--seal", help="Exact 260-byte Groth16 seal")
    parser.add_argument("--header-proof", help="JSON with decimal height, raw header and 64 history siblings")
    a = parser.parse_args(argv)
    policy = validate_policy(json.loads(Path(a.policy).read_text()))
    network=policy['network']
    w3 = evm_web3(a.rpc, timeout=30)
    if w3.eth.chain_id != int(a.chain_id):
        raise ValueError("CHAIN_MISMATCH")
    from .bootstrap_policy import validate_confirmations
    validate_confirmations(dict(chain_id=a.chain_id, evm_confirmations=a.evm_confirmations), "ZK_")
    block = read_block(w3, int(a.evm_confirmations))
    state, adapter, records, _ = inspect_binding(w3, a.zk_state, policy, block.number)
    from .bitcoin_rpc import BitcoinRPC
    bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie, network, policy)
    check_core(bitcoin, network, policy)
    if a.action == "status":
        result = inspect_live(bitcoin, state, policy, block_identifier=block.number)
        if w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("BTC_REGISTRY_READ_REORG")
        print(json.dumps(result))
        return
    if not a.identity_file:
        raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
    if a.action == "submit":
        if not a.journal or not a.seal:
            raise ValueError("ZK_PROOF_FILES_REQUIRED")
        step = transition_step(a.chain_id, state.address, policy,
                               Path(a.journal).read_bytes(), Path(a.seal).read_bytes())
    else:
        if not a.header_proof:
            raise ValueError("ZK_HEADER_MEMBERSHIP_REQUIRED")
        inspect_live(bitcoin, state, policy, block_identifier=block.number)
        step = materialization_step(json.loads(Path(a.header_proof).read_text()))
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    # Stable across callers/restarts; Bootstrap shares the identity's nonce lane.
    operation = "zk-spv-" + hashlib.sha256(canonical([a.chain_id, state.address, policy, step]).encode()).hexdigest()
    profile = dict(deployment_id=operation, mode="backed", bitcoin_network=network, bitcoin_policy=policy,
        chain_id=a.chain_id, evm_rpc=a.rpc, evm_confirmations=a.evm_confirmations,
        contracts=records, steps=[step])
    result = Bootstrap(profile, str(Path(a.identity_file).resolve()) + ".zk-transactions.sqlite").apply(a.identity_file)
    result["halted_latest"] = state.functions.halted().call()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
