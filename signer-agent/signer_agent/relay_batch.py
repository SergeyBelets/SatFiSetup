"""Keep an unfinished relay chunk stable while Core's tip continues to grow."""
import fcntl
import hashlib
import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from .operations import Journal, canonical


class DurableRelayBatch:
    @classmethod
    def for_rpc(cls, identity, w3, relay, policy, confirmations):
        """One transaction namespace across bootstrap, CLI and keeper callers.

        A pending header list is not sufficient to identify its signed EVM
        transaction. Every production entry point also shares this canonical
        operation and journal, so switching callers cannot allocate a new nonce.
        """
        from .bitcoin_network import validate_policy, relay_artifact
        from .bootstrap import Bootstrap
        from .bootstrap_parallel import restore_signed
        from .evm_network import read_block
        from web3 import Web3

        if not identity:
            raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
        if (type(confirmations) not in (str, int)
                or isinstance(confirmations, str) and (not confirmations.isdecimal()
                    or str(int(confirmations)) != confirmations)):
            raise ValueError("EVM_CONFIRMATIONS_POLICY")
        policy = validate_policy(policy)
        if policy["version"] != "1":
            raise ValueError("ZK_PROFILE_REQUIRES_PROOF_WORKFLOW")
        relay = Web3.to_checksum_address(relay)
        chain_id = str(w3.eth.chain_id)
        confirmations = int(confirmations)
        read_block(w3, confirmations)  # Validate finality policy before storing a lane.
        identity_path = Path(identity).resolve()
        state = str(identity_path) + ".relay-transactions.sqlite"
        kind = relay_artifact(policy["network"])

        def send(headers):
            operation = "relay-chunk-" + hashlib.sha256(canonical(
                [chain_id, relay.lower(), policy, headers]).encode()).hexdigest()
            step = dict(id="headers", kind="call", contract="relay", method="addBlockHeaders",
                        args=[["0x" + raw for raw in headers]])
            profile = dict(deployment_id=operation, mode="backed", bitcoin_network=policy["network"],
                bitcoin_policy=policy, chain_id=chain_id, evm_rpc=w3.provider.endpoint_uri,
                evm_confirmations=str(confirmations), contracts={"relay": dict(address=relay, artifact=kind)},
                steps=[step])
            runner = Bootstrap(profile, state)
            key = operation + ":headers"
            entry = runner.journal.put(key, step)
            # A reservation may exist only in the shared nonce journal after a
            # crash between its fsync and the per-step journal write.
            identity_journal = Journal(str(identity_path.with_suffix(".evm-nonce.lock")) + ".sqlite")
            reserved = bool(entry["result"].get("tx_hash")) or restore_signed(
                identity_journal, runner, key, step) is not None
            if not reserved and _headers_confirmed(w3, relay, kind, policy, confirmations, headers):
                # Another operator completed the same work before this sender
                # signed. Clear the durable chunk without paying for a duplicate.
                # Leave the unsigned operation PREPARED: a future reorg must be
                # re-observed, never treated as a permanently successful receipt.
                return dict(status="VERIFIED", already_relayed=True)
            return runner.apply(identity_path)

        return cls(identity_path, chain_id, relay, policy, send)

    def __init__(self, identity, chain_id, relay, policy, send):
        self.path = Path(str(Path(identity).resolve()) + ".relay-chunks.sqlite")
        self.journal = Journal(self.path)
        self.binding = dict(chain_id=str(chain_id), relay=relay.lower(), policy=policy)
        self.key = "relay-" + hashlib.sha256(canonical([str(chain_id), relay.lower()]).encode()).hexdigest()
        self.send = send

    def __call__(self, headers):
        if not isinstance(headers, list) or not 1 <= len(headers) <= 64:
            raise ValueError("RELAY_BATCH_LENGTH")
        for raw in headers:
            if not isinstance(raw, str) or len(raw) != 160 or bytes.fromhex(raw).hex() != raw:
                raise ValueError("RELAY_BATCH_HEADER")
        with self._locked():
            entry = self.journal.put(self.key, self.binding)
            saved = entry["result"].get("headers") if entry["status"] == "PENDING" else None
            selected = saved or headers
            result = self._execute(selected)
            if saved and saved != headers:
                # The sync caller requested a different range. Even if recovery
                # finished, force a fresh canonical observation before proceeding.
                return dict(status="PENDING")
            return result

    @contextmanager
    def _locked(self):
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _execute(self, selected):
        self.journal.update(self.key, "PENDING", dict(headers=selected))
        # Persist before the transaction journal is allowed to sign/broadcast.
        result = self.send(selected)
        if isinstance(result, Mapping) and result.get("status") in ("VERIFIED", 1):
            self.journal.update(self.key, "CONFIRMED", dict(headers=selected))
        return result

    def recover_pending(self):
        """Drain a saved chunk even when another relayer already caught up.

        sync_relay invokes this before it can return READY without new headers;
        otherwise a dropped signed transaction could strand the identity nonce.
        """
        with self._locked():
            entry = self.journal.put(self.key, self.binding)
            if entry["status"] != "PENDING":
                return None
            return self._execute(entry["result"]["headers"])


def _headers_confirmed(w3, address, kind, policy, confirmations, headers):
    """Reconcile an unsigned stale chunk against one canonical finalized view."""
    from .bitcoin_network import _header, _relay_snapshot
    from .bitcoin_trust import checked_runtime
    from .bootstrap import artifact
    from .evm_network import read_block

    block = read_block(w3, confirmations)
    checked_runtime(w3, address, kind, block.number)
    relay = w3.eth.contract(address=address, abi=artifact(kind)["abi"])
    if relay.functions.halted().call():
        raise ValueError("MAINNET_RELAY_HALTED")
    _relay_snapshot(relay, policy, block.number)
    present = True
    for encoded in headers:
        raw, digest, work = _header(encoded)
        row = relay.functions.blocks(digest).call(block_identifier=block.number)
        if not row[3]:
            present = False
            continue
        parent = relay.functions.blocks(raw[4:36]).call(block_identifier=block.number)
        if (bytes(row[0]) != raw[4:36] or bytes(row[1]) != raw[36:68]
                or not parent[3] or row[3] != parent[3] + 1 or row[2] != parent[2] + work
                or row[4] != int.from_bytes(raw[68:72], "little")
                or row[5] != int.from_bytes(raw[72:76], "little")):
            raise ValueError("MAINNET_RELAY_WORK_MISMATCH")
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    return present
