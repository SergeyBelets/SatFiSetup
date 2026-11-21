"""P2TR key-path worker. Receipt collection is distinct from token settlement."""

import hashlib
import json
from secrets import token_hex
from contextlib import contextmanager
from eth_abi import encode
from eth_utils import keccak
from . import crypto
from .bitcoin_rpc import BitcoinRPC, satoshis
from .bootstrap import artifact


def outpoint_hash(outpoint):
    txid, vout = outpoint.split(":")
    return keccak(encode(["bytes32", "uint32"], [bytes.fromhex(txid), int(vout)]))


def plan_hash(manifest, plan, built):
    if plan.get("version") in ("2", "3", "4", "5"):
        from .bitcoin_onchain_plan import onchain_plan_hash

        return onchain_plan_hash(manifest, plan)
    return keccak(
        encode(
            [
                "bytes32",
                "bytes32",
                "address",
                "bytes32",
                "string",
                "bytes",
                "uint64[]",
                "bytes[]",
                "uint64",
                "uint64",
                "bytes32[]",
            ],
            [
                keccak(text="SATOHSI_BTC_PLAN_V1"),
                manifest.context,
                plan["batch_registry"],
                bytes.fromhex(plan["batch_id"]),
                plan["network"],
                bytes.fromhex(built["unsigned"]),
                [int(i["amount"]) for i in plan["inputs"]],
                [bytes.fromhex(i["script"]) for i in plan["inputs"]],
                int(plan["max_fee"]),
                int(plan["deadline"]),
                [bytes.fromhex(x) for x in plan["withdrawal_refs"]],
            ],
        )
    )


def validate(manifest, key, plan, registry, bitcoin, graph=None):
    from .bitcoin_trust import confirmed_block, graph_network

    graph = graph if graph is not None else {}
    if plan["network"] != graph_network(graph):
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    block = confirmed_block(registry.w3, graph)
    if plan.get("version") in ("2", "3", "4", "5"):
        from .bitcoin_onchain_plan import validate_plan

        validate_plan(plan, manifest, allow_commitments=True)
        if plan["wallet_result"] != key["result_hash"]:
            raise ValueError("BTC_PLAN_WALLET_RESULT")
    if (manifest.purpose != "BTC_SPEND" or plan["network"] not in ("regtest", "mainnet", "testnet4")
            or plan["network"] != getattr(bitcoin, "network", "regtest")):
        raise ValueError("WRONG_KEY_PURPOSE_OR_NETWORK")
    bitcoin.check_network()
    if plan["wallet_script"] != "5120" + key["result"]["group"][2:]:
        raise ValueError("WALLET_SCRIPT_MISMATCH")
    from .bitcoin_input_keys import build, guard
    guard(manifest, key, plan, registry, graph)
    core_anchor = None
    if plan.get("version") in ("3", "4"):
        from .bitcoin_input_keys import wallet
        r, _, _ = wallet(manifest, key, registry, graph, block.number)
        if bitcoin.call("getblockhash",0) != bytes(r.functions.bitcoinGenesis().call(block_identifier=block.number)).hex():
            raise ValueError("BTC_ADDRESS_GENESIS_MISMATCH")
        core_anchor = bitcoin.call("getbestblockhash")
    built = build(plan)
    from .dkg import ROOT

    abi = json.loads(
        (
            ROOT
            / "web3/contracts/out/BitcoinBatchRegistry.sol/BitcoinBatchRegistry.json"
        ).read_text()
    )["abi"]
    contract = registry.w3.eth.contract(address=plan["batch_registry"], abi=abi)
    if (
        contract.functions.dkg().call(block_identifier=block.number) != manifest.registry
        or bytes(contract.functions.walletId().call(block_identifier=block.number)) != manifest.session_id
    ):
        raise ValueError("WALLET_REGISTRY_MISMATCH")
    h = plan_hash(manifest, plan, built)
    batch_id = bytes.fromhex(plan["batch_id"])
    batch = contract.functions.batches(batch_id).call(block_identifier=block.number)
    if (
        bytes(batch[0]) != h
        or bytes(batch[1]).hex() != key["result_hash"]
        or batch[2] != int(plan["deadline"])
        or batch[3] not in (1, 2)
    ):
        raise ValueError("BATCH_APPROVAL_MISMATCH")
    if registry.w3.eth.get_block("latest").timestamp > batch[2]:
        raise ValueError("BATCH_EXPIRED")
    root = bytes(contract.functions.reservationRoot(batch_id).call(block_identifier=block.number))
    replacement = bytes(contract.functions.replacementOf(batch_id).call(block_identifier=block.number)) != bytes(32)
    for item in plan["inputs"]:
        if (
            bytes(
                contract.functions.reservations(outpoint_hash(item["outpoint"])).call(block_identifier=block.number)
            )
            != root
        ):
            raise ValueError("UTXO_NOT_RESERVED")
        txid, vout = item["outpoint"].split(":")
        utxo = bitcoin.call("gettxout", txid, int(vout), not replacement)
        if (
            not utxo
            or utxo["scriptPubKey"]["hex"] != item["script"]
            or satoshis(utxo["value"]) != int(item["amount"])
        ):
            raise ValueError("UTXO_MISMATCH")
        if utxo["confirmations"] < 1 or (
            utxo["coinbase"] and utxo["confirmations"] < 101
        ):
            raise ValueError("UTXO_IMMATURE")
    if plan.get("version") == "5":
        from .bitcoin_onchain_payment import _payment_matches, _terms

        specs = graph["contracts"]
        if specs["escrow"]["artifact"] != "BitcoinWithdrawalEscrowV2":
            raise ValueError("BTC_WITHDRAWAL_FEE_PROFILE")
        contracts = {name: registry.w3.eth.contract(address=specs[name]["address"],
                     abi=artifact(specs[name]["artifact"])["abi"]) for name in ("escrow", "deposits", "batches")}
        if (contracts["escrow"].address != contract.functions.withdrawalEscrow().call(block_identifier=block.number)
                or contracts["batches"].address != contract.address
                or not _payment_matches(contracts, plan, block.number)
                or contracts["escrow"].functions.payment(batch_id).call(block_identifier=block.number)[2]
                or int(contracts["escrow"].functions.minerFee(batch_id).call(block_identifier=block.number))
                   != int(plan["fee"])):
            raise ValueError("ESCROW_PAYMENT_MISMATCH")
        _terms(contracts, plan, bitcoin, block.number, bound_root=root)
    elif plan["withdrawal_refs"]:
        address = contract.functions.withdrawalEscrow().call(block_identifier=block.number)
        if int(address, 16) == 0:
            raise ValueError("ESCROW_NOT_BOUND")
        escrow_abi = json.loads(
            (
                ROOT
                / "web3/contracts/out/BitcoinWithdrawalEscrow.sol/BitcoinWithdrawalEscrow.json"
            ).read_text()
        )["abi"]
        escrow = registry.w3.eth.contract(address=address, abi=escrow_abi)
        payment = escrow.functions.payment(batch_id).call(block_identifier=block.number)
        import hashlib

        expected = hashlib.sha256(
            hashlib.sha256(bytes.fromhex(built["unsigned"])).digest()
        ).digest()
        if (
            bytes(payment[0]) != expected
            or payment[2]
            or [bytes(x).hex() for x in payment[3]] != plan["withdrawal_refs"]
        ):
            raise ValueError("ESCROW_PAYMENT_MISMATCH")
        due, burned, requests = {}, 0, []
        for ref in payment[3]:
            request = escrow.functions.requests(ref).call(block_identifier=block.number)
            if request[-1] != 2 or bytes(request[-2]) != root or request[4] % 10**10:
                raise ValueError("ESCROW_REQUEST_STATE")
            requests.append(request)
            from .callback_admission import validate_callback
            validate_callback(escrow, request, block.number)
            script = bytes(request[5]).hex()
            if plan.get("version") in ("3", "4"):
                common = registry.w3.eth.contract(address=graph["contracts"]["deposits"]["address"],
                    abi=artifact("BitcoinDepositRegistry")["abi"])
                if int(common.functions.scriptPools(keccak(bytes.fromhex(script))).call(block_identifier=block.number),16):
                    raise ValueError("BTC_WITHDRAWAL_TO_WALLET_UNSUPPORTED")
            due[script] = due.get(script, 0) + request[4] // 10**10
            if int(request[2], 16) != 0:
                burned += request[4] // 10**10
        from .bitcoin_onchain_payment import validate_custody_outputs
        validate_custody_outputs(escrow, requests, block.number, plan["outputs"])
        for script, amount in due.items():
            if (
                sum(int(o["amount"]) for o in plan["outputs"] if o["script"] == script)
                < amount
            ):
                raise ValueError("ESCROW_UNDERPAYMENT")
        if plan.get("version")=="4":
            from .withdrawal_fee import token_fee
            burned+=token_fee(plan,dict(escrow=escrow),block.number)
        deposit_address = escrow.functions.depositRegistry().call(block_identifier=block.number)
        deposit_abi = json.loads(
            (
                ROOT
                / "web3/contracts/out/BitcoinDepositRegistry.sol/BitcoinDepositRegistry.json"
            ).read_text()
        )["abi"]
        deposits = registry.w3.eth.contract(address=deposit_address, abi=deposit_abi)
        backing = 0
        from .bitcoin_onchain_payment import protect_unminted_deposit
        for item in plan["inputs"]:
            protect_unminted_deposit(deposits, item, bitcoin, block.number,
                **({"address_mode": True} if plan.get("version") in ("3", "4") else {}))
            txid, vout = item["outpoint"].split(":")
            backing_key = keccak(
                encode(["bytes32", "uint32"], [bytes.fromhex(txid)[::-1], int(vout)])
            )
            backing += deposits.functions.backingSats(backing_key).call(block_identifier=block.number)
        change = sum(
            int(o["amount"])
            for o in plan["outputs"]
            if o["script"] == plan["wallet_script"]
        )
        if backing < burned or change < backing - burned:
            raise ValueError("MINER_FEE_CONSUMES_PRINCIPAL")
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    guard(manifest, key, plan, registry, graph)
    if core_anchor is not None:
        if bitcoin.call("getbestblockhash") != core_anchor: raise ValueError("BTC_INPUT_CORE_CHANGED")
        built["_core_bestblock"] = core_anchor
    from .btc_fee_reserve import guard as fee_guard
    fee_guard(manifest,plan,registry,graph,bitcoin=bitcoin)
    # Irreversible boundary before exposing any nonce commitment or signature material.
    if batch[3] == 1:
        previous = registry.contract
        registry.contract = contract
        try:
            registry.send("beginSigning", batch_id)
        finally:
            registry.contract = previous
    if registry.w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_REGISTRY_READ_REORG")
    return built, h


def sign_step(action, manifest, key, plan, registry, state, bitcoin):
    with state.exclusive():
        from .bitcoin_trust import require_registry, graph_network, check_bitcoin_graph

        if state.get("btc-payments-recovery:" + manifest.context.hex()):
            raise ValueError("PAYMENTS_PAUSED_FOR_RECOVERY")
        pinned = require_registry(manifest, key, registry, state, plan["batch_registry"])
        # A plan cannot change the network approved and pinned for this DKG wallet.
        if graph_network(pinned["graph"]) in ("mainnet", "testnet4") and plan.get("version") not in ("2", "3", "4", "5"):
            raise ValueError("BTC_MAINNET_REQUIRES_ONCHAIN_PLAN")
        if plan["network"] != graph_network(pinned["graph"]):
            raise ValueError("BITCOIN_NETWORK_MISMATCH")
        check_bitcoin_graph(registry, pinned["graph"], bitcoin)
        built, h = validate(manifest, key, plan, registry, bitcoin, pinned["graph"])
        from .bitcoin_input_keys import guard, versioned, key_args
        guard(manifest, key, plan, registry, pinned["graph"], state)
        from .btc_fee_reserve import guard as fee_guard
        fee_guard(manifest,plan,registry,pinned["graph"],bitcoin=bitcoin,state=state)
        outputs = []
        for i, message in enumerate(built["sighashes"]):
            guard(manifest, key, plan, registry, pinned["graph"], state)
            fee_guard(manifest,plan,registry,pinned["graph"],bitcoin=bitcoin,state=state)
            if versioned(plan) and bitcoin.call("getbestblockhash") != built["_core_bestblock"]:
                raise ValueError("BTC_INPUT_CORE_CHANGED")
            sid = (
                "btc:" + manifest.context.hex() + ":" + plan["batch_id"] + ":" + str(i)
            )
            stored = state.get(sid)
            if action == "btc-commit":
                if stored is None:
                    state.assert_unspent(sid)
                    stored = (crypto.call("bitcoin_nonce_v1", key_package=key["result"]["key_package"],
                        **key_args(plan,i,key["result"])) if versioned(plan) else
                        crypto.call("nonce", share=key["result"]["signing_share"]))
                    stored.update(
                        status="nonce-created", message_hash=message, plan_hash=h.hex()
                    )
                    if versioned(plan):
                        stored.update(bitcoin_bestblock=built["_core_bestblock"],
                            generation_anchor=state.get("btc-input-generation:"+manifest.context.hex()))
                    state.put(sid, stored)
                if stored["message_hash"] != message or stored["plan_hash"] != h.hex():
                    raise ValueError("NONCE_PLAN_MISMATCH")
                if stored.get("status") == "share-prepared":
                    if "nonces" in stored: raise ValueError("BTC_CACHED_SIGNING_STATE_INVALID")
                    state.assert_spent(sid, stored["transcript"])
                elif stored.get("status") == "nonce-created":
                    state.assert_unspent(sid)
                else: raise ValueError("BTC_CACHED_SIGNING_STATE_INVALID")
                outputs.append(stored["commitments"])
            else:
                commitments = plan["commitments"][i]
                ids = sorted(int(k) for k in commitments)
                if plan.get("version") in ("2", "3", "4", "5") and ids != [int(x) for x in plan["signers"]]:
                    raise ValueError("SIGNER_SUBSET_INVALID")
                if (
                    len(ids) != manifest.threshold
                    or any(x < 1 or x > len(manifest.participants) for x in ids)
                    or key["index"] not in ids
                ):
                    raise ValueError("SIGNER_SUBSET_INVALID")
                if (
                    not stored
                    or stored.get("message_hash") != message
                    or stored.get("plan_hash") != h.hex()
                    or commitments[str(key["index"])] != stored["commitments"]
                ):
                    raise ValueError("NONCE_COMMITMENT_MISMATCH")
                transcript = keccak(
                    encode(
                        ["bytes32", "bytes32", "uint16[]", "bytes[]"],
                        [
                            h,
                            bytes.fromhex(message),
                            ids,
                            [bytes.fromhex(commitments[str(x)]) for x in ids],
                        ],
                    )
                ).hex()
                if stored["status"] == "share-prepared":
                    if stored["transcript"] != transcript:
                        raise ValueError("NONCE_TRANSCRIPT_MISMATCH")
                    state.assert_spent(sid, transcript)
                    z = stored["share"]
                else:
                    state.assert_unspent(sid)
                    z = crypto.call(
                        "bitcoin_sign_v1" if versioned(plan) else "sign",
                        key_package=key["result"]["key_package"],
                        nonces=stored["nonces"],
                        commitments=commitments,
                        message=message,
                        **(key_args(plan,i,key["result"]) if versioned(plan) else {}),
                    )["share"]
                    # Write journal before publish; discard secret nonce. Reuse only cached z.
                    with state._journal() as db:
                        db.execute("INSERT INTO spent VALUES(?,?)", (sid, transcript))
                    stored.pop("nonces", None)
                    stored.update(
                        status="share-prepared", transcript=transcript, share=z
                    )
                    state.put(sid, stored)
                outputs.append(z)
        guard(manifest, key, plan, registry, pinned["graph"], state)
        fee_guard(manifest,plan,registry,pinned["graph"],bitcoin=bitcoin,state=state)
        return dict(
            status="COMMITTED" if action == "btc-commit" else "SIGNED",
            participant_index=key["index"],
            values=outputs,
            plan_hash=h.hex(),
        )


class BitcoinWorker:
    def __init__(self, path, rpc):
        from .operations import Journal

        self.path = str(path)
        self.rpc = rpc
        self._journal = Journal(path)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, signed TEXT NOT NULL, txid TEXT UNIQUE NOT NULL, wtxid TEXT NOT NULL, status TEXT NOT NULL, proof TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS payment_observations (id TEXT PRIMARY KEY, detail TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS payment_recoveries (id TEXT PRIMARY KEY, detail TEXT NOT NULL)")

    @contextmanager
    def db(self):
        with self._journal.db() as db:
            db.row_factory = None
            yield db

    def _retry_payment_observations(self):
        # A transport failure is retryable, but cannot be ignored by sending a
        # different payment. Recheck every unavailable observation first.
        with self.db() as db:
            pending = db.execute("SELECT id FROM payment_observations").fetchall()
        for (observed_batch,) in pending:
            observed = self.confirmation(observed_batch)
            if observed["status"] == "OBSERVATION_UNAVAILABLE":
                raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")

    def remember_observed_broadcast(self, batch_id, signed, *, replacement=None):
        """Record an already visible, escrow-bound payment for later exact RBF."""
        from .funding_envelope import inspect_funding

        raw, txid, wtxid = (signed[field] for field in ("signed", "txid", "wtxid"))
        decoded = inspect_funding(raw, txid)
        if decoded["wtxid"] != wtxid:
            raise ValueError("TXID_MISMATCH")
        self._retry_payment_observations()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_payment_pauses(db)
            existing = db.execute(
                "SELECT id,signed,txid,wtxid FROM payments WHERE id=? OR txid=?", (batch_id, txid),
            ).fetchall()
            if existing and existing != [(batch_id, raw, txid, wtxid)]:
                raise ValueError("BATCH_VERSION_CONFLICT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY, root TEXT NOT NULL, previous TEXT NOT NULL)"
            )
            ancestry = db.execute("SELECT root,previous FROM versions WHERE id=?", (batch_id,)).fetchone()
            if ancestry and ancestry != replacement:
                raise ValueError("BATCH_VERSION_CONFLICT")
            if replacement:
                # The caller supplies ancestry from the authenticated confirmed batch registry.
                root, previous = replacement
                if batch_id in (root, previous):
                    raise ValueError("BATCH_VERSION_CONFLICT")
                parent = db.execute(
                    "SELECT p.signed,p.txid,v.root FROM payments p LEFT JOIN versions v ON v.id=p.id WHERE p.id=?",
                    (previous,),
                ).fetchone()
                if parent:
                    if (parent[2] or previous) != root:
                        raise ValueError("BATCH_VERSION_CONFLICT")
                    parent_inputs = inspect_funding(parent[0], parent[1])["inputs"]
                    if [item["outpoint"] for item in parent_inputs] != [item["outpoint"] for item in decoded["inputs"]]:
                        raise ValueError("REPLACEMENT_INPUTS_MISMATCH")
                if not ancestry:
                    db.execute("INSERT INTO versions VALUES(?,?,?)", (batch_id, root, previous))
            if existing:
                db.execute("UPDATE payments SET status='BROADCAST' WHERE id=? AND status='SIGNED'", (batch_id,))
            else:
                db.execute("INSERT INTO payments VALUES(?,?,?,?,'BROADCAST',NULL)", (batch_id, raw, txid, wtxid))

    def broadcast(self, batch_id, signed, replaces=None):
        self.rpc.check_network()
        self._retry_payment_observations()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_payment_pauses(db)
            row = db.execute(
                "SELECT signed FROM payments WHERE id=?", (batch_id,)
            ).fetchone()
            if row and row[0] != signed["signed"]:
                raise ValueError("BATCH_VERSION_CONFLICT")
            db.execute(
                "INSERT OR IGNORE INTO payments VALUES(?,?,?,?,?,NULL)",
                (batch_id, signed["signed"], signed["txid"], signed["wtxid"], "SIGNED"),
            )
        if replaces:
            with self.db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY, root TEXT NOT NULL, previous TEXT NOT NULL)"
                )
                parent = db.execute(
                    "SELECT signed FROM payments WHERE id=?", (replaces,)
                ).fetchone()
                if not parent or replaces == batch_id:
                    raise ValueError("UNKNOWN_REPLACED_BATCH")
                old = self.rpc.call("decoderawtransaction", parent[0])
                new = self.rpc.call("decoderawtransaction", signed["signed"])
                if [(v["txid"], v["vout"]) for v in old["vin"]] != [
                    (v["txid"], v["vout"]) for v in new["vin"]
                ]:
                    raise ValueError("REPLACEMENT_INPUTS_MISMATCH")
                root = db.execute(
                    "SELECT root FROM versions WHERE id=?", (replaces,)
                ).fetchone()
                root = root[0] if root else replaces
                existing = db.execute(
                    "SELECT root,previous FROM versions WHERE id=?", (batch_id,)
                ).fetchone()
                if existing and existing != (root, replaces):
                    raise ValueError("BATCH_VERSION_CONFLICT")
                db.execute(
                    "INSERT OR IGNORE INTO versions VALUES(?,?,?)",
                    (batch_id, root, replaces),
                )
        # Crash after storage or broadcast is resumable using the exact same bytes and txid.
        try:
            self.rpc.call("getrawtransaction", signed["txid"])
        except Exception:
            accepted = self.rpc.call("testmempoolaccept", [signed["signed"]])[0]
            if not accepted.get("allowed"):
                raise ValueError("MEMPOOL_REJECTED")
            # Order the send and any concurrently recorded pause in one journal.
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self._check_payment_pauses(db)
                if self.rpc.call("sendrawtransaction", signed["signed"]) != signed["txid"]:
                    raise ValueError("TXID_MISMATCH")
        with self.db() as db:
            db.execute("UPDATE payments SET status='BROADCAST' WHERE id=? AND status='SIGNED'", (batch_id,))
        return signed["txid"]

    @staticmethod
    def _check_payment_pauses(db):
        if db.execute("SELECT 1 FROM payments WHERE status='RECOVERY_REQUIRED'").fetchone():
            raise ValueError("PAYMENTS_PAUSED_FOR_RECOVERY")
        if db.execute("SELECT 1 FROM payment_observations LIMIT 1").fetchone():
            raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")

    @staticmethod
    def _payment_state(db, batch_id):
        row = db.execute(
            "SELECT p.txid,p.status,p.proof,o.detail FROM payments p "
            "LEFT JOIN payment_observations o ON o.id=p.id WHERE p.id=?", (batch_id,),
        ).fetchone()
        if not row:
            raise ValueError("UNKNOWN_BATCH")
        return row

    @staticmethod
    def _observation_detail(row):
        detail = json.loads(row[3]) if row[3] else {"status": row[1]}
        detail.pop("revision", None)
        return detail

    def _paused(self, row):
        return dict(self._observation_detail(row), status="RECOVERY_REQUIRED")

    def _changed(self, db, batch_id, observed):
        current = self._payment_state(db, batch_id)
        if current == observed:
            return None
        if current[1] == "RECOVERY_REQUIRED":
            return self._paused(current)
        return dict(status="OBSERVATION_UNAVAILABLE", error_code="BITCOIN_OBSERVATION_UNAVAILABLE")

    def _confirming(self, batch_id, observed):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = self._changed(db, batch_id, observed)
            if changed:
                return changed
            db.execute("UPDATE payments SET status='CONFIRMING' WHERE id=?", (batch_id,))
            db.execute("DELETE FROM payment_observations WHERE id=?", (batch_id,))
        return dict(status="CONFIRMING")

    def _observation_failure(self, batch_id, observed, error, *, recovery=False):
        from .bitcoin_rpc import observation_error
        detail = dict(status="RECOVERY_REQUIRED" if recovery else "OBSERVATION_UNAVAILABLE",
                      **observation_error(error))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = self._changed(db, batch_id, observed)
            if changed:
                return changed
            # Distinguish two observations with the same public error. A recovery
            # started before a newer pause must recheck against the newer state.
            recorded = dict(detail, revision=token_hex(16))
            db.execute("INSERT OR REPLACE INTO payment_observations VALUES(?,?)", (batch_id, json.dumps(recorded)))
            if recovery:
                db.execute("UPDATE payments SET status='RECOVERY_REQUIRED' WHERE id=?", (batch_id,))
        return detail

    def confirmation(self, batch_id, depth=6, *, recover=False):
        from .bitcoin_rpc import (BitcoinRPCError, check_known_payment_anchor, check_payment_block,
                                  observation_error, payment_block, payment_block_height, verify_payment_proof)

        if depth < 1:
            raise ValueError("CONFIRMATION_DEPTH")
        with self.db() as db:
            row = self._payment_state(db, batch_id)
        if row[1] == "RECOVERY_REQUIRED" and not recover:
            return self._paused(row)
        saved_proof = json.loads(row[2]) if row[2] else None
        if saved_proof:
            # Never recover at a shallower depth than the original observation.
            depth = max(depth, int(saved_proof.get("depth", 6)))
        try:
            self.rpc.check_network()
        except Exception as error:
            return self._observation_failure(batch_id, row, error, recovery=row[1] == "RECOVERY_REQUIRED")
        try:
            tx = self.rpc.call("getrawtransaction", row[0], True)
        except Exception as error:
            # Missing transactions may be an RBF version, an unindexed node or
            # an outage. Only Core's structured -5 can trigger family lookup.
            missing = isinstance(error, BitcoinRPCError) and error.rpc_code == -5
            if missing and saved_proof and saved_proof.get("block_hash"):
                try:
                    check_known_payment_anchor(self.rpc, saved_proof["block_hash"])
                except ValueError as block_error:
                    if str(block_error) == "BITCOIN_PAYMENT_REORG":
                        return self._observation_failure(batch_id, row, block_error, recovery=True)
                except BitcoinRPCError:
                    pass  # The older block could not be checked. Keep the original observation error.
            if not missing or saved_proof or recover:
                return self._observation_failure(batch_id, row, error, recovery=row[1] == "RECOVERY_REQUIRED")
            with self.db() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY, root TEXT NOT NULL, previous TEXT NOT NULL)"
                )
                root = db.execute(
                    "SELECT root FROM versions WHERE id=?", (batch_id,)
                ).fetchone()
                root = root[0] if root else batch_id
                family = db.execute(
                    "SELECT p.id,p.txid FROM payments p WHERE p.id=? OR p.id IN (SELECT id FROM versions WHERE root=?)",
                    (root, root),
                ).fetchall()
            for alternative, txid in family:
                if alternative == batch_id:
                    continue
                try:
                    visible = self.rpc.call("getrawtransaction", txid, True)
                except Exception:
                    continue
                if visible.get("confirmations", 0) >= depth:
                    result = self.confirmation(alternative, depth)
                    return dict(result, confirmed_batch=alternative)
                return dict(
                    status="REPLACED_PENDING", replacement=alternative, txid=txid
                )
            return self._observation_failure(batch_id, row, error)
        try:
            block = payment_block(tx, row[0], depth, bitcoin=self.rpc)
            original_block = saved_proof.get("block_hash") if saved_proof else None
            if original_block and (block is None or (block != original_block and not recover)):
                check_known_payment_anchor(self.rpc, original_block)
            if block is None:
                if row[2] or row[1] == "RECOVERY_REQUIRED":
                    raise ValueError("BITCOIN_PAYMENT_DEPTH_PENDING")
                return self._confirming(batch_id, row)
            height = payment_block_height(self.rpc, block)
            proof = verify_payment_proof(self.rpc, row[0], block)
            check_payment_block(self.rpc, block, height)
        except Exception as error:
            hard = row[1] == "RECOVERY_REQUIRED" or observation_error(error)["error_code"] in (
                "BITCOIN_PAYMENT_REORG", "INVALID_PAYMENT_PROOF")
            return self._observation_failure(batch_id, row, error, recovery=hard)
        result = dict(
            status="CONFIRMED",
            txid=row[0],
            block_hash=block,
            proof=proof,
            raw_transaction=tx["hex"],
            depth=depth,
        )
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = self._changed(db, batch_id, row)
            if changed:
                return changed
            if row[3] or row[1] == "RECOVERY_REQUIRED":
                db.execute("INSERT OR REPLACE INTO payment_recoveries VALUES(?,?)", (batch_id, json.dumps(
                    dict(previous=self._observation_detail(row),
                         proof=result, manually_reviewed=bool(recover)))))
            db.execute(
                "UPDATE payments SET status='CONFIRMED',proof=? WHERE id=?",
                (json.dumps(result), batch_id),
            )
            db.execute("DELETE FROM payment_observations WHERE id=?", (batch_id,))
        return result
