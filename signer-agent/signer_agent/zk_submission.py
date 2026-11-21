"""Durable EVM attempts for an unchanged, freshly checked ZK proof call."""

import fcntl
import os
import stat

from web3 import Web3

from .operations import Journal


def options(registry, graph, block_hash):
    from .bitcoin_onchain import contract
    from .zk_spv import is_zk

    if not is_zk(graph.get("bitcoin_policy")):
        return {}
    adapter = graph["contracts"]["spv_guard"]
    return {"zk_proof": (contract(registry, adapter["address"], adapter["artifact"]), block_hash)}


def _materialized_root(registry, adapter, header):
    block = registry.w3.eth.get_block("latest")
    canonical, _ = adapter.functions.getBlockStatus(header).call(block_identifier=block.number)
    root, _, _ = adapter.functions.materializedHeaders(header).call(block_identifier=block.number)
    if not canonical or not any(root):
        raise ValueError("ZK_PROOF_ROOT_PENDING")
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return Web3.to_hex(root), block


def submit(registry, profile, adapter, header, send_profile):
    """Advance only after a known failed attempt and a fresh proof simulation."""
    from .bootstrap import Bootstrap
    from .bootstrap_recovery import acknowledge_revert

    journal = Journal(registry.transaction_journal)
    base = profile["deployment_id"]
    marker = "zk-proof-attempts-" + base
    lock = journal.path.with_name(journal.path.name + "." + marker + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("JOURNAL_FILE_PERMISSIONS")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("ZK_PROOF_ATTEMPT_BUSY") from None
        binding = {"adapter": adapter.address, "header": Web3.to_hex(header)}
        saved = journal.put(marker, binding)["result"]
        number = int(saved.get("number", "0"))
        attempt = base if number == 0 else f"{base}-zk-attempt-{number}"
        try:
            previous = journal.get(attempt + ":send")
        except KeyError:
            previous = None
            if saved and number and not saved.get("root"):
                raise ValueError("ZK_PROOF_ATTEMPT_BINDING")

        if previous is not None and previous["status"] == "REVERTED":
            root, block = _materialized_root(registry, adapter, header)
            if root == saved.get("root"):
                raise ValueError("ZK_PROOF_ROOT_UNCHANGED")
            original = Bootstrap(dict(profile, deployment_id=attempt), journal.path,
                                 evm_rpc_override=profile["evm_rpc"])
            step = profile["steps"][0]
            original.fn(step).call({"from": registry.account.address}, block_identifier=block.number)
            if registry.w3.eth.get_block(block.number).hash != block.hash:
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
            tx_hash = previous["result"]["tx_hash"]
            acknowledge_revert(registry.identity_file, tx_hash, profile["evm_rpc"])
            journal.put("zk-proof-reverted-" + attempt, {
                **binding, "tx_hash": tx_hash, "previous_root": saved.get("root"), "next_root": root,
            })
            number += 1
            attempt = f"{base}-zk-attempt-{number}"
            journal.update(marker, "ACTIVE", {"number": str(number), "root": root})
        elif not saved:
            # Pre-upgrade attempts keep their transaction journal. Their old
            # root is unknown, so retry still requires a final revert and simulation.
            root = None if previous else _materialized_root(registry, adapter, header)[0]
            journal.update(marker, "ACTIVE", {"number": "0", "root": root})

        return send_profile(registry, dict(profile, deployment_id=attempt))
    finally:
        os.close(fd)
