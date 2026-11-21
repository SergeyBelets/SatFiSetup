"""Explicit network operations; participant signing is exposed by the DKG CLI."""

import json
from pathlib import Path

from .safe_errors import DiagnosticArgumentParser
from .bitcoin_worker import BitcoinRPC, BitcoinWorker
from .bitcoin_proofs import inclusion
from .bootstrap import Bootstrap, artifact
from .bitcoin_network import (checkpoint_policy, header_hash, inspect_relay,
                              relay_constructor_args, sync_relay, validate_policy, is_public_network, relay_artifact)
from .evm_network import read_block


def _profile(a, contracts, steps, deployment, policy):
    result = dict(deployment_id=deployment, mode="backed" if policy or a.chain_id == "1" else "integration",
                  chain_id=a.chain_id, evm_rpc=a.rpc, bitcoin_network=a.bitcoin_network,
                  contracts=contracts, steps=steps)
    if policy:
        result["bitcoin_policy"] = policy
    if a.evm_confirmations:
        result["evm_confirmations"] = a.evm_confirmations
    return result


def _checked_mainnet_relay(w3, address, policy, *, block=None):
    from .bitcoin_trust import checked_runtime
    from .contract_provenance import prove_creation

    block = block or read_block(w3, 12 if w3.eth.chain_id == 1 else 1)
    from .zk_spv import is_zk, inspect_binding
    if is_zk(policy):
        state, _, _, _ = inspect_binding(w3, address, policy, block.number)
        if w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("BTC_REGISTRY_READ_REORG")
        return state
    kind = relay_artifact(policy["network"])
    checked_runtime(w3, address, kind, block.number)
    prove_creation(w3, address, kind, relay_constructor_args(policy), block.number)
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    return w3.eth.contract(address=address, abi=artifact(kind)["abi"])


def _inspect_mainnet_relay(w3, bitcoin, relay, policy, confirmations):
    block = read_block(w3, int(confirmations))
    from .zk_spv import is_zk, inspect_live
    inspect = inspect_live if is_zk(policy) else inspect_relay
    result = inspect(bitcoin, relay, policy, block_identifier=block.number)
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    return result


def _mainnet_settlement_observation(w3, bitcoin, escrow, policy, evm_confirmations, txid):
    """Validate the explicit operator target and proof at one confirmed EVM snapshot."""
    from .bitcoin_trust import checked_runtime
    from .contract_provenance import prove_creation

    block = read_block(w3, int(evm_confirmations))

    def read(contract, method, *args):
        return getattr(contract.functions, method)(*args).call(block_identifier=block.number)

    checked_runtime(w3, escrow.address, "BitcoinWithdrawalEscrow", block.number)
    depth = int(read(escrow, "depth"))
    minima = [int(read(escrow, "minP2WPKHSats")), int(read(escrow, "minP2TRSats"))]
    if depth < 6 or any(value <= 0 for value in minima):
        raise ValueError("CONFIRMATION_DEPTH")
    batch = read(escrow, "batches")
    guard_address = read(escrow, "spv")
    prove_creation(w3, escrow.address, "BitcoinWithdrawalEscrow",
                   [batch, guard_address, depth, *minima], block.number)
    from .zk_spv import is_zk, inspect_binding, inspect_live
    if is_zk(policy):
        from .zk_profiles import profile as zk_profile
        adapter_kind=zk_profile(policy)["adapter"]
        checked_runtime(w3, guard_address, adapter_kind, block.number)
        guard = w3.eth.contract(address=guard_address, abi=artifact(adapter_kind)["abi"])
        relay, guard, _, _ = inspect_binding(w3, read(guard, "state"), policy, block.number,
                                            adapter_address=guard_address)
        inspect_live(bitcoin, relay, policy, block_identifier=block.number)
    else:
        checked_runtime(w3, guard_address, "MainnetSPVGuard", block.number)
        guard = w3.eth.contract(address=guard_address, abi=artifact("MainnetSPVGuard")["abi"])
        raw_relay = read(guard, "relay")
        if read(guard, "maxTipAgeSeconds") != int(policy["max_tip_age_seconds"]):
            raise ValueError("MAINNET_RELAY_POLICY_MISMATCH")
        prove_creation(w3, guard_address, "MainnetSPVGuard",
                       [raw_relay, int(policy["max_tip_age_seconds"])], block.number)
        relay = _checked_mainnet_relay(w3, raw_relay, policy, block=block)
        inspect_relay(bitcoin, relay, policy, block_identifier=block.number)
    if not read(guard, "isFresh"):
        raise ValueError("MAINNET_RELAY_STALE")
    proof = inclusion(bitcoin, txid)
    if int(proof["confirmations"]) < depth:
        raise ValueError("PAYMENT_DEPTH")
    wire_hash = bytes.fromhex(proof["block_hash"])
    display_hash = wire_hash[::-1].hex()
    header = bitcoin.call("getblockheader", display_hash, True)
    if bitcoin.call("getblockhash", header["height"]) != display_hash:
        raise ValueError("BITCOIN_PAYMENT_REORG")
    canonical, confirmations = read(guard, "getBlockStatus", wire_hash)
    if not canonical or confirmations < depth:
        raise ValueError("PAYMENT_DEPTH")
    if not read(guard, "checkTxInclusion", wire_hash, bytes.fromhex(proof["txid"]),
                [bytes.fromhex(value) for value in proof["siblings"]], int(proof["index"])):
        raise ValueError("INVALID_PAYMENT_PROOF")
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    return proof


def main(argv):
    if argv and argv[0]=='continuity':
        from .continuity_cli import main as continuity_main
        return continuity_main(argv[1:])
    if argv and argv[0]=='epochs':
        from .epoch_cli import main as epochs_main
        return epochs_main(argv[1:])
    if argv and argv[0]=='incident':
        from .incident_cli import main as incident_main
        return incident_main(argv[1:])
    if argv and argv[0] == "deposit":
        from .smp1_deposit_cli import main as deposit_main
        return deposit_main(argv[1:])
    if argv and argv[0] == "withdrawal":
        from .withdrawal_user_cli import main as withdrawal_main
        return withdrawal_main(argv[1:])
    if argv and argv[0]=='withdrawal-fee':
        from .withdrawal_fee_cli import main as personal_main
        return personal_main(argv[1:])
    if argv and argv[0]=="fee-reserve":
        from .btc_fee_cli import main as fee_main
        return fee_main(argv[1:])
    if argv and argv[0] == "inspect-funding":
        from .funding_envelope import main as inspect_funding_main
        return inspect_funding_main(argv[1:])
    if argv and argv[0] == "deposit-commitment":
        from .bitcoin_funding import commitment_main
        return commitment_main(argv[1:])
    p = DiagnosticArgumentParser()
    p.add_argument("action", choices=["checkpoint", "proof", "relay", "settle", "broadcast", "confirm", "recover"])
    p.add_argument("--bitcoin-rpc", required=True)
    p.add_argument("--bitcoin-cookie", required=True)
    p.add_argument("--bitcoin-network", choices=["regtest", "mainnet", "testnet4"], default="regtest")
    p.add_argument("--bitcoin-policy", help="reviewed public Bitcoin policy JSON, identical to the invitation")
    p.add_argument("--rpc")
    p.add_argument("--relay")
    p.add_argument("--escrow")
    p.add_argument("--txid")
    p.add_argument("--batch")
    p.add_argument("--replaces")
    p.add_argument("--signed")
    p.add_argument("--identity-file")
    p.add_argument("--state")
    p.add_argument("--chain-id", default="31337")
    p.add_argument("--evm-confirmations", help="explicit EVM receipt depth for mainnet operations")
    for name in ("checkpoint-height", "max-reorg-depth", "max-tip-age-seconds",
                 "max-relay-lag-blocks", "max-headers-per-sync"):
        p.add_argument("--" + name)
    a = p.parse_args(argv)
    policy_fields = ("checkpoint_height", "max_reorg_depth", "max_tip_age_seconds",
                     "max_relay_lag_blocks", "max_headers_per_sync")
    if a.action == "checkpoint":
        if not is_public_network(a.bitcoin_network) or a.bitcoin_policy or not all(getattr(a, key) for key in policy_fields):
            p.error("checkpoint requires --bitcoin-network mainnet/testnet4 and all five explicit checkpoint/policy parameters")
        bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie, network=a.bitcoin_network)
        result = checkpoint_policy(bitcoin, **{key: getattr(a, key) for key in policy_fields})
        print(json.dumps(result, indent=2))
        return 0
    if any(getattr(a, key) is not None for key in policy_fields):
        p.error("checkpoint parameters apply only to the checkpoint action")
    if not a.state:
        p.error("--state is required for payment and relay actions")
    if is_public_network(a.bitcoin_network) and not a.bitcoin_policy:
        p.error("mainnet requires --bitcoin-policy with the independently reviewed public policy")
    if a.bitcoin_network == "regtest" and a.bitcoin_policy:
        p.error("regtest does not accept a mainnet policy")
    if (is_public_network(a.bitcoin_network) or a.chain_id == "1") and a.action != "proof":
        from .bitcoin_network import _decimal
        try:
            _decimal(a.evm_confirmations, 1, 256)
        except ValueError:
            p.error("mainnet EVM operations require explicit --evm-confirmations in 1..256")
    policy = validate_policy(json.loads(Path(a.bitcoin_policy).read_text()), expected_network=a.bitcoin_network) if a.bitcoin_policy else None
    bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie, network=a.bitcoin_network, policy=policy)
    bitcoin.check_network()
    if a.action == "proof":
        result = inclusion(bitcoin, a.txid)
    elif a.action in ("broadcast", "confirm", "recover"):
        # This is the explicit operator API, not a substitute for participant approval.
        # Mainnet participants use dkg btc-broadcast with the pinned complete graph.
        if policy:
            from web3 import Web3
            if not a.rpc or not a.relay:
                p.error("mainnet broadcast/confirm requires --rpc and --relay")
            w3 = Web3(Web3.HTTPProvider(a.rpc))
            if w3.eth.chain_id != int(a.chain_id):
                raise ValueError("CHAIN_MISMATCH")
            relay = _checked_mainnet_relay(w3, a.relay, policy)
            if a.action != "recover":
                _inspect_mainnet_relay(w3, bitcoin, relay, policy, a.evm_confirmations)
        worker = BitcoinWorker(a.state, bitcoin)
        result = (worker.broadcast(a.batch, json.loads(Path(a.signed).read_text()), a.replaces)
                  if a.action == "broadcast" else worker.confirmation(a.batch, recover=a.action == "recover"))
    else:
        from web3 import Web3

        if not a.rpc or not a.identity_file:
            p.error("relay/settle requires --rpc and --identity-file")
        w3 = Web3(Web3.HTTPProvider(a.rpc))
        if w3.eth.chain_id != int(a.chain_id):
            raise ValueError("CHAIN_MISMATCH")
        if a.action == "settle":
            escrow = w3.eth.contract(address=a.escrow, abi=artifact("BitcoinWithdrawalEscrow")["abi"])
            if policy:
                proof = _mainnet_settlement_observation(w3, bitcoin, escrow, policy, a.evm_confirmations, a.txid)
            else:
                depth = int(escrow.functions.depth().call())
                if depth < 6:
                    raise ValueError("CONFIRMATION_DEPTH")
                proof = inclusion(bitcoin, a.txid)
                if int(proof["confirmations"]) < depth:
                    raise ValueError("PAYMENT_DEPTH")
            contracts = {"escrow": dict(address=a.escrow, artifact="BitcoinWithdrawalEscrow")}
            steps = [dict(id="proof_settlement", kind="call", contract="escrow", method="settle",
                          args=["0x" + a.batch.removeprefix("0x"), "0x" + proof["raw"],
                                "0x" + proof["block_hash"], ["0x" + x for x in proof["siblings"]], proof["index"]])]
            deployment = "settle-" + a.escrow.lower() + "-" + a.batch
        elif policy:
            from .zk_spv import is_zk
            if is_zk(policy):
                raise ValueError("ZK_PROFILE_REQUIRES_PROOF_WORKFLOW")
            relay = _checked_mainnet_relay(w3, a.relay, policy)
            contracts = {"relay": dict(address=a.relay, artifact=relay_artifact(a.bitcoin_network))}

            def send_header(raw):
                digest = header_hash(bytes.fromhex(raw)).hex()
                step = dict(id="header", kind="call", contract="relay", method="addBlockHeader", args=["0x" + raw])
                profile = _profile(a, contracts, [step], "mainnet-header-" + a.relay.lower() + "-" + digest, policy)
                return Bootstrap(profile, a.state).apply(a.identity_file)

            from .relay_batch import DurableRelayBatch
            send_headers = DurableRelayBatch.for_rpc(a.identity_file, w3, relay.address, policy,
                                                     int(a.evm_confirmations))
            result = sync_relay(bitcoin, relay, policy, send_header, send_headers=send_headers,
                                evm_confirmations=int(a.evm_confirmations))
            print(json.dumps(result, indent=2))
            return 0
        else:
            relay = w3.eth.contract(address=a.relay, abi=artifact("RegtestHeaderRelay")["abi"])
            height = bitcoin.call("getblockcount")
            start = 0 if relay.functions.getMainchainHead().call() == bytes(32) else relay.functions.getMainchainHeight().call() + 1
            contracts = {"relay": dict(address=a.relay, artifact="RegtestHeaderRelay")}
            steps = [dict(id="header-" + str(i), kind="call", contract="relay", method="addBlockHeader",
                          args=["0x" + bitcoin.call("getblockheader", bitcoin.call("getblockhash", i), False)])
                     for i in range(start, height + 1)]
            deployment = "headers-" + a.relay.lower() + "-" + str(start) + "-" + str(height)
        runner = Bootstrap(_profile(a, contracts, steps, deployment, policy), a.state)
        if policy and a.action == "settle":
            fresh = _mainnet_settlement_observation(w3, bitcoin, escrow, policy, a.evm_confirmations, a.txid)
            if any(fresh[field] != proof[field] for field in ("raw", "txid", "block_hash", "siblings", "index")):
                raise ValueError("BITCOIN_PAYMENT_REORG")
        result = runner.apply(a.identity_file)
    print(json.dumps(result, indent=2))
    return 0
