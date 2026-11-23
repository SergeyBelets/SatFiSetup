"""Circular immutable constructors: reserve every CREATE envelope before sending.

The addresses are derived from the signing EOA and its reserved consecutive
nonces. A crash can replay this entire set, never recompute its future addresses.
Contract outputs are published only after every receipt and binding passes.
"""
import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path

import rlp
from web3 import Web3
from web3.exceptions import TransactionNotFound, Web3RPCError

from .operations import Journal
from .evm_network import MAX_TRANSACTION_GAS, _limit, read_block, read_common_block, verify_anchor
from .evm_rpc import evm_web3
from .bootstrap_parallel import (observe, finish_group, operation_binding,
                                 recover_identity, restore_signed)


def create_address(sender, nonce):
    return Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(sender[2:]), nonce]))[-20:])


def creation_groups(steps):
    groups, previous = {}, None
    parallel = {s.get("parallel_group") for s in steps if s.get("parallel_group")}
    for step in steps:
        label = step.get("create_group")
        if label is None:
            previous = None
            continue
        if (not isinstance(label, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", label)
                or label in parallel or label in groups and label != previous):
            raise ValueError("BOOTSTRAP_CREATE_GROUP")
        group = groups.setdefault(label, [])
        group.append(step)
        previous = label
        gas = step.get("gas")
        if (len(group) > 8 or step.get("kind") != "deploy" or step.get("parallel_group")
                or step.get("outputs") or step.get("verify_token_transfer")
                or step.get("value", "0") != "0" or not isinstance(gas, str)
                or not re.fullmatch(r"[1-9][0-9]*", gas) or not 100000 <= int(gas) <= 10000000):
            raise ValueError("BOOTSTRAP_CREATE_STEP")
    return groups


@contextmanager
def predicted(bootstrap, steps, sender, nonce):
    saved = dict(bootstrap.values)
    try:
        for index, step in enumerate(steps):
            address = create_address(sender, nonce + index)
            if step["id"] in bootstrap.contracts and bootstrap.contracts[step["id"]]["address"] != address:
                raise ValueError("BOOTSTRAP_CREATE_ADDRESS")
            bootstrap.values[step["id"]] = address
        yield
    finally:
        bootstrap.values = saved


def simulate_group(w3, transactions, block):
    if any(tx.get("to") not in (None, b"", "0x") for tx in transactions):
        raise ValueError("BOOTSTRAP_CREATE_STEP")
    # Web3 represents a CREATE's empty receiver as b"". The JSON-RPC call
    # object represents CREATE by omitting the receiver altogether.
    calls = [{key: hex(value) if type(value) is int else value
              for key, value in tx.items() if key not in ("chainId", "to")} for tx in transactions]
    try:
        reply = w3.provider.make_request("eth_simulateV1", [
            {"blockStateCalls": [{"calls": [call]} for call in calls], "validation": True}, hex(block.number)])
    except Exception:
        raise ValueError("CREATE_SIMULATION_RPC_REQUIRED") from None
    if "error" in reply:
        error = reply["error"]
        rpc_code = error.get("code") if type(error) is dict else None
        code = "CREATE_SIMULATION_RPC_REQUIRED" if rpc_code == -32601 else "CREATE_SIMULATION_REJECTED"
        if type(rpc_code) is int:
            raise ValueError(code) from Web3RPCError(code, rpc_response={"error": {"code": rpc_code}})
        raise ValueError(code)
    try:
        outcomes = [call for item in reply["result"] for call in item["calls"]]
        if len(outcomes) != len(transactions) or any(item["status"] != "0x1" for item in outcomes):
            raise ValueError()
        used = [int(item["gasUsed"], 16) for item in outcomes]
        if any(not 0 < gas <= tx["gas"] for gas, tx in zip(used, transactions)):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError("CREATE_SIMULATION_REJECTED") from None
    verify_anchor(w3, block.number, block.hash)
    return used


def prepare_transactions(bootstrap, steps, account, nonce):
    w3 = bootstrap.w3
    chain_id = w3.eth.chain_id
    # The withdrawal graph has circular immutable bindings. Simulate ordered
    # CREATEs in one RPC so Credits can inspect both earlier simulated runtimes.
    if steps[0]["create_group"] != "withdrawal-fee-constructors" or chain_id not in (1, 11155111):
        return [bootstrap.build_transaction(step, account, nonce + i) for i, step in enumerate(steps)]
    confirmations = bootstrap.evm_confirmations
    alternate = os.environ.get("SATOSHIFI_CREATE_SIMULATION_RPC")
    simulator = evm_web3(alternate, timeout=60) if alternate else w3
    if alternate:
        try:
            block = read_common_block(w3, simulator, confirmations, chain_id)
        except ValueError as error:
            if str(error) == "EVM_RPC_WITNESS_MISMATCH":
                raise ValueError("CREATE_SIMULATION_RPC_CHAIN_MISMATCH") from None
            raise
    else:
        block = read_block(w3, confirmations)
    if (w3.eth.get_transaction_count(account.address, block.number) != nonce
            or alternate and simulator.eth.get_transaction_count(account.address, block.number) != nonce):
        raise ValueError("CREATE_SIMULATION_WAIT_FOR_PRIOR_NONCE")
    glamsterdam = chain_id == 11155111 and block.timestamp >= 1791294816
    protocol_cap = min(int(block.gasLimit), 2**32 - 1 if glamsterdam else MAX_TRANSACTION_GAS)
    cap = min(protocol_cap, _limit("SATOSHIFI_MAX_TX_GAS")) if glamsterdam or chain_id == 1 else protocol_cap
    transactions = [bootstrap.build_transaction(step, account, nonce + i, simulation_gas=cap)
                    for i, step in enumerate(steps)]
    used = simulate_group(simulator, transactions, block)
    for tx, gas in zip(transactions, used):
        tx["gas"] = max((gas * 130 + 99) // 100, gas + 125_000)
        if tx["gas"] > cap:
            raise ValueError("CREATE_SIMULATION_GAS_BUDGET")
    simulate_group(simulator, transactions, block)
    verify_anchor(w3, block.number, block.hash)
    if w3.eth.get_transaction_count(account.address, "pending") != nonce:
        raise ValueError("CREATE_SIMULATION_WAIT_FOR_PRIOR_NONCE")
    return transactions


def apply_group(bootstrap, steps, account, identity, *, dry_run=False, verify=False, before_submit=None):
    from .bootstrap_recovery import signed_nonce

    entries = [bootstrap.journal.put(bootstrap.prefix + ":" + s["id"], s) for s in steps]
    receipts = [observe(bootstrap, s, e) for s, e in zip(steps, entries)]
    if all(receipts):
        return finish_group(bootstrap, steps, entries, receipts)
    first = next(s for s, r in zip(steps, receipts) if r is None)
    if verify:
        return dict(status="INCOMPLETE", step=first["id"])
    if dry_run:
        return dict(status="DRY_RUN", next_step=first, manifest=bootstrap.manifest())
    if account is None:
        raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
    recover_only = bool(before_submit(steps)) if before_submit is not None else False
    lock = Path(identity).resolve().with_suffix(".evm-nonce.lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        lane = Journal(str(lock) + ".sqlite")
        for step, entry in zip(steps, entries):
            saved = restore_signed(lane, bootstrap, entry["id"], step, entry["result"], require_reserved=True)
            if saved is not None and "raw_transaction" not in entry["result"]:
                entry["result"].update(saved)
                bootstrap.journal.update(entry["id"], "SIGNED" if not entry["result"].get("block_hash") else entry["status"], entry["result"])
            if "raw_transaction" in entry["result"]:
                signed_nonce(entry["result"], account, bootstrap.w3.eth.chain_id)
        signed = ["raw_transaction" in e["result"] for e in entries]
        if any(signed) and not all(signed):
            raise ValueError("BOOTSTRAP_CREATE_RESERVATION_RECOVERY_REQUIRED")
        if recover_only and not all(signed):
            return dict(status="NOT_SIGNED", step=first["id"], create_group=steps[0]["create_group"])
        replacements = {}
        reservations = set()
        floor = recover_identity(lane, bootstrap, account, replacements=replacements, reservations=reservations)
        if not any(signed) or replacements:
            nonce = bootstrap.w3.eth.get_transaction_count(account.address, "pending")
            if nonce < max(floor, max(reservations, default=-1) + 1):
                waiting = next((row for row in replacements.values() if row["status"] == "BROADCAST"), None)
                if waiting is not None:
                    return dict(step=first["id"], create_group=steps[0]["create_group"], **waiting)
                raise ValueError("EVM_RPC_BEHIND")
        if not any(signed):
            results = []
            with predicted(bootstrap, steps, account.address, nonce):
                try:
                    transactions = prepare_transactions(bootstrap, steps, account, nonce)
                except ValueError as error:
                    if str(error) != "CREATE_SIMULATION_WAIT_FOR_PRIOR_NONCE":
                        raise
                    return dict(status="AWAITING_EVM_RPC", step=first["id"], create_group=steps[0]["create_group"],
                                reason=str(error))
                for tx in transactions:
                    raw = bytes(account.sign_transaction(tx).raw_transaction)
                    results.append(dict(tx_hash=Web3.keccak(raw).hex(), raw_transaction=raw.hex(), nonce=str(tx["nonce"])))
            lane.put_many([(r["tx_hash"], dict(chain_id=str(bootstrap.w3.eth.chain_id), **r,
                **operation_binding(bootstrap, e["id"], s))) for s, e, r in zip(steps, entries, results)])
            for entry, result in zip(entries, results):
                entry["result"] = result
                bootstrap.journal.update(entry["id"], "SIGNED", result)
        base = int(entries[0]["result"]["nonce"])
        for index, entry in enumerate(entries):
            r = entry["result"]
            if signed_nonce(r, account, bootstrap.w3.eth.chain_id) != base + index:
                raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        for index, entry in enumerate(entries):
            result = entry["result"]
            try:
                bootstrap.w3.eth.get_transaction(result["tx_hash"])
            except TransactionNotFound:
                # Earlier mined failure/reorg prevents sending later constructors.
                for prior_step, prior_entry in zip(steps[:index], entries[:index]):
                    observe(bootstrap, prior_step, prior_entry)
                if int(result["nonce"]) < floor:
                    raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
                bootstrap.broadcast(result["raw_transaction"])
                bootstrap.journal.update(entry["id"], "BROADCAST", result)
        receipts = [observe(bootstrap, s, e) for s, e in zip(steps, entries)]
        if all(receipts):
            return finish_group(bootstrap, steps, entries, receipts, lane)
    return dict(status="CONFIRMING" if all(e["result"].get("block_hash") for e in entries) else "BROADCAST",
                step=first["id"], create_group=steps[0]["create_group"],
                pending=[s["id"] for s, r in zip(steps, receipts) if r is None])
