"""Resumable participant. Secrets never enter the mailbox or operator database."""

import json
import os
from contextlib import nullcontext
from pathlib import Path
from eth_account import Account
from eth_abi import decode
from web3 import Web3
from . import crypto, dkg_protocol as protocol
from .dkg_transport import Mailbox, sign, recover
from .secure_state import SecureState

ROOT = Path(__file__).resolve().parents[2]

REVIEWABLE_DKG_FAILURES = frozenset({
    "ROUND1_HASH_MISMATCH", "PACKAGE_COMMITMENT_MISMATCH", "COMMITMENT_MISMATCH",
    "SHARE_HASH_MISMATCH", "PUBLIC_SHARE_MISMATCH", "RESULT_MISMATCH",
    "CRYPTO_VALIDATION_FAILED", "CEREMONY_TIMEOUT",
})


def atomic_private(path, data):
    import tempfile

    path = Path(path)
    if path.exists():
        raise ValueError("OUTPUT_EXISTS")
    fd, tmp = tempfile.mkstemp(prefix=".key-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # no overwrite, including symlinks
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(tmp)


class Registry:
    def __init__(self, rpc, address, private_key=None):
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        self.account = Account.from_key(private_key) if private_key else None
        abi = json.loads(
            (
                ROOT / "web3/contracts/out/DKGCoordinator.sol/DKGCoordinator.json"
            ).read_text()
        )["abi"]
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=abi
        )
        if not self.w3.eth.get_code(address):
            raise ValueError("REGISTRY_NO_CODE")

    def read(self, method, *args):
        fn = getattr(self.contract.functions, method)(*args)
        pinned = getattr(self, "read_block_identifier", None)
        if pinned is not None:
            return fn.call(block_identifier=pinned)
        graph = getattr(self, "bitcoin_graph", {})
        if self.w3.eth.chain_id == 1 or "evm_confirmations" in graph:
            from .evm_network import read_block
            depth = int(graph.get("evm_confirmations", "12" if self.w3.eth.chain_id == 1 else "1"))
            return fn.call(block_identifier=read_block(self.w3, depth)["number"])
        return fn.call()

    def dkg_block(self):
        from .evm_network import read_block

        graph = getattr(self, "bitcoin_graph", {})
        depth = int(graph.get("evm_confirmations", "12" if self.w3.eth.chain_id == 1 else "1"))
        return read_block(self.w3, depth)

    def check_dkg_block(self, block):
        if bytes(self.w3.eth.get_block(block.number).hash) != bytes(block.hash):
            raise ValueError("DKG_ANCHOR_REORG")

    def send(self, method, *args):
        if getattr(self, "identity_file", None):
            from .bootstrap import Bootstrap
            from .bitcoin_trust import transaction_profile
            from .operations import canonical

            address = self.contract.address
            artifact_name = "DKGCoordinator"
            # Bitcoin validation temporarily targets the already verified batch registry.
            if any(x.get("name") == "beginSigning" for x in self.contract.abi):
                artifact_name = "BitcoinBatchRegistry"
            values = Bootstrap.wire(list(args))
            operation = Web3.keccak(text=canonical([address, method, values])).hex()
            profile = dict(
                deployment_id="participant-" + operation,
                chain_id=str(self.w3.eth.chain_id),
                evm_rpc=self.w3.provider.endpoint_uri,
                **transaction_profile(getattr(self, "bitcoin_graph", {})),
                contracts={
                    "target": dict(
                        address=address,
                        artifact=artifact_name,
                        runtime_hash=Web3.keccak(self.w3.eth.get_code(address)).hex(),
                    )
                },
                steps=[
                    dict(
                        id="send",
                        kind="call",
                        contract="target",
                        method=method,
                        args=values,
                    )
                ],
            )
            b = Bootstrap(profile, self.transaction_journal, evm_rpc_override=self.w3.provider.endpoint_uri)
            result = b.apply_confirmed(self.identity_file)
            if result["status"] != "VERIFIED":
                raise ValueError("TRANSACTION_UNCONFIRMED")
            return b.journal.get(profile["deployment_id"] + ":send")["result"][
                "tx_hash"
            ]
        if not self.account:
            raise ValueError("IDENTITY_REQUIRED")
        if self.w3.eth.chain_id == 1:
            raise ValueError("ETHEREUM_DURABLE_IDENTITY_REQUIRED")
        f = getattr(self.contract.functions, method)(*args)
        tx = f.build_transaction(
            {
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(
                    self.account.address, "pending"
                ),
                "chainId": self.w3.eth.chain_id,
            }
        )
        signed = self.account.sign_transaction(tx)
        receipt = self.w3.eth.wait_for_transaction_receipt(
            self.w3.eth.send_raw_transaction(signed.raw_transaction)
        )
        if receipt.status != 1:
            raise RuntimeError("EVM_REVERT")
        return receipt.transactionHash.hex()

    def check(self, manifest):
        if self.w3.eth.chain_id != manifest.chain_id:
            raise ValueError("CHAIN_MISMATCH")
        info = self.read("getSessionInfo", manifest.session_id)
        c = self.read("ceremonies", manifest.session_id)
        if bytes(c[0]) != manifest.context or self.read(
            "getSessionParticipants", manifest.session_id
        ) != list(manifest.participants):
            raise ValueError("MANIFEST_MISMATCH")
        if info[3] != manifest.threshold:
            raise ValueError("THRESHOLD_MISMATCH")
        if info[4] in (5, 6):
            raise ValueError("CEREMONY_TERMINAL_FAILURE")
        return info, c


class Participant:
    def __init__(self, manifest, registry, state, mailbox):
        self.m = manifest
        self.r = registry
        self.s = state
        self.box = mailbox
        self.address = registry.account.address
        self.index = manifest.index(self.address)
        self.key = manifest.context.hex()

    def run_once(self, confirm_abort=None):
        from .transaction_wait import observation_pending

        try:
            return self._anchored_round(confirm_abort)
        except Exception as error:
            if observation_pending(error) or (type(error) is ValueError and str(error) in (
                    "DKG_ANCHOR_UNAVAILABLE", "EVM_CONFIRMATIONS_PENDING", "ETHEREUM_FINALITY_UNAVAILABLE")):
                return dict(status="WAITING_DKG_RPC", context=self.key)
            raise

    def _anchored_round(self, confirm_abort):
        with self.s.exclusive():
            r = self.r
            anchor_key = "chain:" + self.key
            guard = self.s.get(anchor_key)
            block = r.dkg_block()
            if guard and guard.get("version") == "2":
                if block.number < int(guard["number"]):
                    raise ValueError("DKG_ANCHOR_UNAVAILABLE")
                if r.w3.eth.get_block(int(guard["number"])).hash.hex() != guard["hash"]:
                    raise ValueError("DKG_ANCHOR_REORG")
            previous = getattr(r, "read_block_identifier", None)
            r.read_block_identifier = block.number
            self._block = block
            try:
                mailbox = self.box.at_block(self.m, block) if hasattr(self.box, "at_block") else nullcontext()
                with mailbox:
                    if not any(r.read("ceremonies", self.m.session_id)[0]):
                        raise ValueError("EVM_CONFIRMATIONS_PENDING")
                    info, ceremony = r.check(self.m)
                    if guard and guard.get("version") != "2":
                        self._verify_legacy_progress()
                    r.check_dkg_block(block)
                    checkpoint = dict(version="2", number=str(block.number), hash=block.hash.hex())
                    if checkpoint != guard:
                        self.s.put(anchor_key, checkpoint)
                    abort_key = "abort:" + self.key
                    review = self.s.get(abort_key)
                    if confirm_abort is not None:
                        if not review or review["fingerprint"] != confirm_abort or review["phase"] != info[4]:
                            raise ValueError("DKG_ABORT_CONFIRMATION_MISMATCH")
                        if r.w3.eth.get_block(int(review["number"])).hash.hex() != review["hash"]:
                            raise ValueError("DKG_ANCHOR_REORG")
                        code = review["reason"]
                        if code == "CEREMONY_TIMEOUT":
                            if block.timestamp <= info[5]:
                                raise ValueError("DKG_ABORT_CONFIRMATION_MISMATCH")
                            self._send("advancePhase", self.m.session_id)
                        else:
                            self._send("abortCeremony", self.m.session_id, Web3.keccak(text=code))
                        return dict(status="DKG_ABORTED", context=self.key, reason=code)
                    try:
                        result = self._run_once(info, ceremony)
                    except (ValueError, RuntimeError) as exc:
                        code = str(exc)
                        if code not in REVIEWABLE_DKG_FAILURES:
                            raise
                        r.check_dkg_block(block)
                        if review and review["reason"] == code and review["phase"] == info[4]:
                            if r.w3.eth.get_block(int(review["number"])).hash.hex() != review["hash"]:
                                raise ValueError("DKG_ANCHOR_REORG") from None
                            fingerprint = review["fingerprint"]
                        else:
                            fingerprint = Web3.keccak(text=f"{self.key}:{code}:{block.hash.hex()}").hex()
                            self.s.put(abort_key, dict(fingerprint=fingerprint, reason=code, phase=info[4],
                                                      number=str(block.number), hash=block.hash.hex()))
                        return dict(status="DKG_REVIEW_REQUIRED", context=self.key, reason=code,
                                    confirm_abort=fingerprint)
                    if review:
                        self.s.put(abort_key, {})
            finally:
                r.read_block_identifier = previous
            updated = r.dkg_block()
            if updated.number > block.number:
                r.check_dkg_block(block)
                r.check_dkg_block(updated)
                self.s.put(anchor_key, dict(version="2", number=str(updated.number), hash=updated.hash.hex()))
            return result

    def _verify_legacy_progress(self):
        """Migrate an old tip anchor only when confirmed transcript evidence agrees."""
        state = self.s.get(self.key)
        if state is None:
            return
        m, r = self.m, self.r
        package = bytes.fromhex(state["round1"]["package"])
        own = bytes(r.read("round1Hashes", m.session_id, self.address))
        if any(own) and own != protocol.round1(m.context, self.address, package):
            raise ValueError("DKG_ANCHOR_REORG")
        if "round1_digest" not in state:
            return
        round1 = [bytes(r.read("round1Hashes", m.session_id, peer)) for peer in m.participants]
        if not all(any(item) for item in round1):
            raise ValueError("DKG_ANCHOR_UNAVAILABLE")
        if protocol.digest(["bytes32[]"], [round1]).hex() != state["round1_digest"]:
            raise ValueError("DKG_ANCHOR_REORG")
        for recipient, envelope in state["envelopes"].items():
            saved = bytes(r.read("getShareHash", m.session_id, self.address, recipient))
            if any(saved) and saved.hex() != envelope["hash"]:
                raise ValueError("DKG_ANCHOR_REORG")
        if "result_hash" in state:
            round2 = [bytes(r.read("getShareHash", m.session_id, peer, recipient))
                      for peer in m.participants for recipient in m.participants if peer != recipient]
            if not all(any(item) for item in round2):
                raise ValueError("DKG_ANCHOR_UNAVAILABLE")
            result = state["result"]
            points = [bytes.fromhex(result["public_shares"]["%064x" % index])
                      for index in range(1, len(m.participants) + 1)]
            if (protocol.result(m, round1, round2, result["normalization_negated"],
                    bytes.fromhex(result["group"])[1:], points).hex() != state["result_hash"]):
                raise ValueError("DKG_ANCHOR_REORG")

    def _send(self, method, *args):
        self.r.check_dkg_block(self._block)
        return self.r.send(method, *args)

    def _run_once(self, info, c):
        m, r, s = self.m, self.r, self.s
        state = s.get(self.key)
        if info[4] != 4 and self._block.timestamp > info[5]:
            raise ValueError("CEREMONY_TIMEOUT")
        if state is None:
            transport = s.get("transport")
            if (
                not transport
                or bytes.fromhex(transport["public"])
                != m.transport_keys[self.index - 1]
            ):
                raise ValueError("TRANSPORT_KEY_MISMATCH")
            signature = sign(
                m,
                "DKGTransportKey",
                r.account.key,
                participant=self.address,
                transportPublicKey=m.transport_keys[self.index - 1],
            )
            out = crypto.call(
                "part1", index=self.index, n=len(m.participants), t=m.threshold
            )
            state = dict(stage="round1", round1=out, transport_binding=dict(signature=signature))
            s.put(self.key, state)
        if info[4] == 4:
            if state.get("result_hash") != bytes(c[3]).hex():
                raise ValueError("RESULT_MISMATCH")
            r.check_dkg_block(self._block)
            state["stage"] = "active"
            state["round1"].pop("secret", None)
            s.put(self.key, state)
            return dict(status="ACTIVE", context=self.key, result=state["result_hash"])
        package = bytes.fromhex(state["round1"]["package"])
        expected = protocol.round1(m.context, self.address, package)
        if "transport_binding" not in state:
            state["transport_binding"] = dict(signature=sign(
                m, "DKGTransportKey", r.account.key, participant=self.address,
                transportPublicKey=m.transport_keys[self.index - 1]))
            s.put(self.key, state)
        self.box.put_many(m, [
            ("key", self.address, "", state["transport_binding"]),
            ("round1", self.address, "", dict(package=package.hex(), commitments=state["round1"]["commitments"])),
        ])
        current = bytes(r.read("round1Hashes", m.session_id, self.address))
        if current == bytes(32):
            self._send(
                "submitRound1",
                m.session_id,
                expected,
                [
                    protocol.commitment(m.context, self.address, i, bytes.fromhex(p))
                    for i, p in enumerate(state["round1"]["commitments"])
                ],
            )
            return dict(status="ROUND1_PUBLISHED")
        if current != expected:
            raise ValueError("ROUND1_CONFLICT")
        if info[4] == 1:
            return dict(status="WAITING_ROUND1")
        public = {}
        r1_hashes = []
        for i, p in enumerate(m.participants, 1):
            binding = self.box.get(m, "key", p)
            item = self.box.get(m, "round1", p)
            if not binding or not item:
                return dict(status="WAITING_PACKAGE")
            if (
                recover(
                    m,
                    "DKGTransportKey",
                    binding["signature"],
                    participant=p,
                    transportPublicKey=m.transport_keys[i - 1],
                )
                != p
            ):
                raise ValueError("TRANSPORT_IDENTITY")
            actual = crypto.call("inspect_round1", package=item["package"])[
                "commitments"
            ]
            if actual != item["commitments"]:
                raise ValueError("PACKAGE_COMMITMENT_MISMATCH")
            h = protocol.round1(m.context, p, bytes.fromhex(item["package"]))
            observed = bytes(r.read("round1Hashes", m.session_id, p))
            if not any(observed):
                return dict(status="WAITING_DKG_RPC", context=self.key)
            if h != observed:
                raise ValueError("ROUND1_HASH_MISMATCH")
            if [
                protocol.commitment(m.context, p, j, bytes.fromhex(point))
                for j, point in enumerate(item["commitments"])
            ] != [bytes(x) for x in r.read("getVSSCommitments", m.session_id, p)]:
                raise ValueError("COMMITMENT_MISMATCH")
            r1_hashes.append(h)
            if p != self.address:
                public[str(i)] = item["package"]
        broadcast_digest = protocol.digest(["bytes32[]"], [r1_hashes]).hex()
        if state.get("round1_digest", broadcast_digest) != broadcast_digest:
            raise ValueError("ROUND1_TRANSCRIPT_MISMATCH")
        if "envelopes" not in state:
            r.check_dkg_block(self._block)
            state["round1_digest"] = broadcast_digest
            state["round2"] = crypto.call(
                "part2", secret=state["round1"]["secret"], round1=public
            )
            state["round1"].pop("secret", None)
            state["envelopes"] = {}
            for encoded_index, plain in state["round2"]["packages"].items():
                i = int(encoded_index, 16)
                recipient = m.participants[i - 1]
                hpke_info, aad = protocol.hpke_context(
                    m.context, self.address, recipient
                )
                out = crypto.call(
                    "seal",
                    public=m.transport_keys[i - 1].hex(),
                    plaintext=plain,
                    info=hpke_info.hex(),
                    aad=aad.hex(),
                )
                envelope = protocol.envelope(
                    m.context,
                    self.address,
                    recipient,
                    bytes.fromhex(out["enc"]),
                    bytes.fromhex(out["ciphertext"]),
                )
                h = protocol.share(m.context, self.address, recipient, envelope)
                state["envelopes"][recipient] = dict(
                    envelope=envelope.hex(),
                    hash=h.hex(),
                    signature=sign(
                        m,
                        "DKGEnvelope",
                        r.account.key,
                        sender=self.address,
                        recipient=recipient,
                        shareHash=h,
                    ),
                )
            state["round2"].pop("packages")
            s.put(
                self.key, state
            )  # ciphertext fixed durably before hashes are published
        recipients = [p for p in m.participants if p != self.address]
        self.box.put_many(m, [("envelope", self.address, recipient, state["envelopes"][recipient])
                             for recipient in recipients])
        missing_hashes = False
        for recipient in recipients:
            item = state["envelopes"][recipient]
            current = bytes(
                r.read("getShareHash", m.session_id, self.address, recipient)
            )
            if current == bytes(32):
                missing_hashes = True
            elif current.hex() != item["hash"]:
                raise ValueError("ENVELOPE_CONFLICT")
        if missing_hashes:
            self._send("submitShareHashes", m.session_id, recipients,
                   [bytes.fromhex(state["envelopes"][recipient]["hash"]) for recipient in recipients])
        if info[4] == 2:
            return dict(status="WAITING_ROUND2")
        hashes = [
            bytes(r.read("getShareHash", m.session_id, p, q))
            for p in m.participants
            for q in m.participants
            if p != q
        ]
        if not all(any(item) for item in hashes):
            return dict(status="WAITING_DKG_RPC", context=self.key)
        if "result" not in state:
            received = {}
            for i, p in enumerate(m.participants, 1):
                if p == self.address:
                    continue
                item = self.box.get(m, "envelope", p, self.address)
                if item is None:
                    return dict(status="WAITING_ENVELOPE")
                wire = bytes.fromhex(item["envelope"])
                h = protocol.share(m.context, p, self.address, wire)
                observed = bytes(r.read("getShareHash", m.session_id, p, self.address))
                if not any(observed):
                    return dict(status="WAITING_DKG_RPC", context=self.key)
                if h.hex() != item["hash"] or h != observed:
                    raise ValueError("SHARE_HASH_MISMATCH")
                if (
                    recover(
                        m,
                        "DKGEnvelope",
                        item["signature"],
                        sender=p,
                        recipient=self.address,
                        shareHash=h,
                    )
                    != p
                ):
                    raise ValueError("SENDER_MISMATCH")
                version, ctx, round_, sender, recipient, seq, enc, ct = decode(
                    [
                        "uint16",
                        "bytes32",
                        "uint8",
                        "address",
                        "address",
                        "uint64",
                        "bytes",
                        "bytes",
                    ],
                    wire,
                )
                if (version, ctx, round_, sender.lower(), recipient.lower(), seq) != (
                    1,
                    m.context,
                    2,
                    p.lower(),
                    self.address.lower(),
                    0,
                ):
                    raise ValueError("ENVELOPE_CONTEXT")
                if protocol.envelope(ctx, p, self.address, enc, ct) != wire:
                    raise ValueError("NONCANONICAL_ENVELOPE")
                hpke_info, aad = protocol.hpke_context(ctx, p, self.address)
                received[str(i)] = crypto.call(
                    "open",
                    secret=s.get("transport")["secret"],
                    enc=enc.hex(),
                    ciphertext=ct.hex(),
                    info=hpke_info.hex(),
                    aad=aad.hex(),
                )["plaintext"]
            r.check_dkg_block(self._block)
            out = crypto.call(
                "part3",
                secret=state["round2"]["secret"],
                round1=public,
                round2=received,
            )
            state["result"] = out
            state.pop("round2", None)
            points = [
                bytes.fromhex(out["public_shares"]["%064x" % i])
                for i in range(1, len(m.participants) + 1)
            ]
            state["result_hash"] = protocol.result(
                m,
                r1_hashes,
                hashes,
                out["normalization_negated"],
                bytes.fromhex(out["group"])[1:],
                points,
            ).hex()
            state["manifest"] = m.to_dict()
            state["participant"] = self.address
            state["index"] = self.index
            s.put(self.key, state)
        if (any(bytes(r.read("receiptHashes", m.session_id, p, self.address)) == bytes(32)
                for p in recipients) or not r.read("hasSubmittedPKShare", m.session_id, self.address)):
            self._send("submitReceiptsAndPublicKeyShare", m.session_id, recipients,
                   [r.read("getShareHash", m.session_id, p, self.address) for p in recipients],
                   bytes.fromhex(state["result"]["public_share"]))
        if info[8] != len(m.participants) or c[5] != len(m.participants) * (
            len(m.participants) - 1
        ):
            return dict(status="WAITING_ATTESTATIONS")
        # Compare EVERY published share with the locally verified library result, including odd Y.
        for i, p in enumerate(m.participants, 1):
            observed = bytes(r.read("getPublicKeyShare", m.session_id, p))
            if not observed:
                return dict(status="WAITING_DKG_RPC", context=self.key)
            if observed.hex() != state["result"]["public_shares"]["%064x" % i]:
                raise ValueError("PUBLIC_SHARE_MISMATCH")
        if bytes(r.read("resultApprovals", m.session_id, self.address)) == bytes(32):
            self._send(
                "attestResult",
                m.session_id,
                state["result"]["normalization_negated"],
                bytes.fromhex(state["result"]["group"])[1:],
                bytes.fromhex(state["result_hash"]),
            )
        return dict(status="RESULT_ATTESTED", result=state["result_hash"])
