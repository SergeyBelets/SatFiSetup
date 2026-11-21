"""Authenticated public Bitcoin coordination; secret nonce material never leaves SecureState."""
import json
from web3 import Web3
from . import crypto
from .bootstrap import Bootstrap, artifact
from .bitcoin_onchain_plan import (MAX_PLAN_BYTES, decode_plan, serialize_plan,
                                  onchain_plan_hash, hex_value)
from .bitcoin_trust import (checked_runtime, require_registry, graph_network, bitcoin_for_graph,
                            check_bitcoin_graph, transaction_profile, confirmed_block)
from .contract_provenance import prove_creation, check_proof_anchor
from .operations import canonical
from .bitcoin_network import is_public_network, relay_artifact
from .bitcoin_rpc import (BitcoinRPCError, checked_payment_proof, observation_error,
                          payment_position, retryable_payment_observation)


def contract(registry, address, kind):
    return registry.w3.eth.contract(address=Web3.to_checksum_address(address), abi=artifact(kind)["abi"])


def send(registry, address, kind, method, args, *, graph=None):
    """Idempotent durable submission, with canonical receipt validation on every retry."""
    if not getattr(registry, "identity_file", None):
        raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
    values = Bootstrap.wire(args)
    operation = Web3.keccak(text=canonical([address, kind, method, values])).hex()
    profile = dict(deployment_id="btc-board-" + operation,
                   chain_id=str(registry.w3.eth.chain_id),
                   evm_rpc=registry.w3.provider.endpoint_uri,
                   **transaction_profile(graph if graph is not None else getattr(registry, "bitcoin_graph", {})),
                   contracts={"target": dict(address=address, artifact=kind,
                        runtime_hash=Web3.keccak(registry.w3.eth.get_code(address)).hex())},
                   steps=[dict(id="send", kind="call", contract="target", method=method, args=values)])
    controller = getattr(registry, 'transaction_controller', None)
    if controller is not None:
        return controller.submit(registry, profile)
    runner = Bootstrap(profile, registry.transaction_journal)
    result = runner.apply_confirmed(registry.identity_file)
    if result["status"] != "VERIFIED":
        raise ValueError("BTC_TRANSACTION_UNCONFIRMED")
    saved = runner.journal.get(profile["deployment_id"] + ":send")["result"]
    return runner.verify_receipt(saved)


def snapshot_check(w3, block):
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("BTC_BOARD_SNAPSHOT_REORG")


def checked_board(manifest, key, registry, state, address):
    if manifest.purpose != "BTC_SPEND" or registry.w3.eth.chain_id != manifest.chain_id:
        raise ValueError("BTC_BOARD_CONTEXT")
    board = contract(registry, address, "BitcoinSigningBoard")
    pinned = state.get("btc-registry:" + manifest.context.hex())
    if not pinned:
        raise ValueError("BTC_REGISTRY_NOT_PINNED")
    block = confirmed_block(registry.w3, pinned["graph"])
    checked_runtime(registry.w3, board.address, "BitcoinSigningBoard", block.number)
    def read(name):
        return getattr(board.functions, name)().call(block_identifier=block.number)
    batch_address = read("batchRegistry")
    require_registry(manifest, key, registry, state, batch_address)
    if (read("dkg") != manifest.registry or bytes(read("walletId")) != manifest.session_id
            or bytes(read("walletResult")).hex() != key["result_hash"]
            or read("MAX_PLAN_BYTES") != MAX_PLAN_BYTES or read("maxPacketBytes") < 512):
        raise ValueError("BTC_BOARD_BINDING")
    cache = state.get("btc-board-proof:" + board.address)
    if cache:
        check_proof_anchor(registry.w3, cache)
    proof = prove_creation(registry.w3, board.address, "BitcoinSigningBoard",
                           [batch_address, read("maxPacketBytes")], block.number)
    if cache and proof != cache:
        raise ValueError("BTC_BOARD_PROVENANCE_CHANGED")
    snapshot_check(registry.w3, block)
    board._btc_proof = proof
    return board


def remember_plan(manifest, state, plan):
    serialize_plan(plan, manifest)
    sid = "btc-plan:" + manifest.context.hex() + ":" + plan["batch_id"]
    with state.exclusive():
        previous = state.get(sid)
        if previous and previous != plan:
            raise ValueError("BTC_PLAN_CACHE_CONFLICT")
        state.put(sid, plan)
        state.put("btc-plan-latest:" + manifest.context.hex(), {"batch": plan["batch_id"]})


def cached_plan(manifest, state, batch=None):
    if batch is None:
        latest = state.get("btc-plan-latest:" + manifest.context.hex())
        batch = latest["batch"] if latest else None
    plan = state.get("btc-plan:" + manifest.context.hex() + ":" + str(batch))
    if not plan:
        raise ValueError("BTC_PLAN_NOT_PREPARED")
    serialize_plan(plan, manifest)
    return plan


def load_plan(manifest, key, registry, state, address, batch):
    batch_id = hex_value(batch, 32)
    board = checked_board(manifest, key, registry, state, address)
    block = confirmed_block(registry.w3, getattr(registry, "bitcoin_graph", {}))
    raw = bytes(board.functions.plan(batch_id).call(block_identifier=block.number))
    plan = decode_plan(raw, manifest)
    if (plan["batch_id"] != batch or plan["signing_board"] != board.address
            or plan["wallet_result"] != key["result_hash"]
            or plan["wallet_script"] != "5120" + key["result"]["group"][2:]
            or plan["batch_registry"] != board.functions.batchRegistry().call(block_identifier=block.number)):
        raise ValueError("BTC_PLAN_BINDING")
    batches = contract(registry, plan["batch_registry"], "BitcoinBatchRegistry")
    approved = batches.functions.batches(batch_id).call(block_identifier=block.number)
    h = onchain_plan_hash(manifest, plan)
    if (bytes(approved[0]) != h or bytes(approved[1]).hex() != key["result_hash"]
            or approved[2] != int(plan["deadline"]) or approved[3] == 0
            or bytes(board.functions.getPlanHash(batch_id, raw).call(block_identifier=block.number)) != h):
        raise ValueError("BTC_PLAN_APPROVAL")
    snapshot_check(registry.w3, block)
    proof_key = "btc-plan-anchor:" + manifest.context.hex() + ":" + batch
    old = state.get(proof_key)
    if old and registry.w3.eth.get_block(int(old["number"])).hash.hex() != old["hash"]:
        raise ValueError("BTC_PLAN_ANCHOR_REORG")
    # Anchor a canonical observation, including the approved reservation and immutable plan.
    with state.exclusive():
        state.put("btc-board-proof:" + board.address, board._btc_proof)
        if not old:
            state.put(proof_key, {"number": str(block.number), "hash": block.hash.hex()})
    remember_plan(manifest, state, plan)
    return plan


def packet_payload(plan, input_index, kind, signer, value, manifest):
    # Rust parses and reserializes exact public types. Never expose raw publish from CLI.
    validated = crypto.call("inspect_bitcoin_packet", kind=kind, value=value)["value"]
    return canonical(dict(version="1", batch=plan["batch_id"], input=str(input_index),
                          kind=kind, signer=str(signer), plan_hash=onchain_plan_hash(manifest, plan).hex(),
                          value=validated)).encode()


def collect(manifest, registry, board, plan):
    block = confirmed_block(registry.w3, getattr(registry, "bitcoin_graph", {}))
    commitments, shares = [], []
    missing_commitments, missing_shares = [], []
    seen = set()
    for i in range(len(plan["inputs"])):
        rows = []
        for number, kind in enumerate(("commitment", "share")):
            values = {}
            for signer in plan["signers"]:
                raw = bytes(board.functions.packet(bytes.fromhex(plan["batch_id"]), i, number,
                    manifest.participants[int(signer) - 1]).call(block_identifier=block.number))
                if not raw:
                    (missing_commitments if number == 0 else missing_shares).append(dict(input=str(i), signer=signer))
                    continue
                if len(raw) > 512:
                    raise ValueError("BTC_PACKET_TOO_LARGE")
                try:
                    packet = json.loads(raw)
                    value = packet["value"]
                except (ValueError, KeyError, TypeError):
                    raise ValueError("BTC_PACKET_SCHEMA") from None
                if packet_payload(plan, i, kind, signer, value, manifest) != raw:
                    raise ValueError("BTC_PACKET_BINDING")
                if number == 0:
                    if value in seen:
                        raise ValueError("BTC_DUPLICATE_COMMITMENT")
                    seen.add(value)
                values[signer] = value
            rows.append(values)
        commitments.append(rows[0]); shares.append(rows[1])
    snapshot_check(registry.w3, block)
    return commitments, shares, missing_commitments, missing_shares


def publish_values(manifest, key, registry, board, plan, kind, values):
    if plan.get("version") in ("3", "4"):
        from .bitcoin_input_keys import guard
        guard(manifest,key,plan,registry,registry.bitcoin_graph)
    from .btc_fee_reserve import guard as fee_guard
    fee_guard(manifest,plan,registry,getattr(registry,"bitcoin_graph",{}))
    if len(values) != len(plan["inputs"]) or not 1 <= len(values) <= 16:
        raise ValueError("BTC_PACKET_BINDING")
    payloads, missing = [], False
    block = confirmed_block(registry.w3, getattr(registry, "bitcoin_graph", {}))
    for i, value in enumerate(values):
        number = 0 if kind == "commitment" else 1
        raw = packet_payload(plan, i, kind, str(key["index"]), value, manifest)
        payloads.append(raw)
        previous = bytes(board.functions.packet(bytes.fromhex(plan["batch_id"]), i, number, key["participant"]).call(block_identifier=block.number))
        if previous:
            if previous != raw:
                raise ValueError("BTC_PACKET_CONFLICT")
        else:
            missing = True
    snapshot_check(registry.w3, block)
    if missing:
        # Include the identical full set on retries, including packets already
        # present. Never repartition a pending signed operation or reuse nonces.
        send(registry, board.address, "BitcoinSigningBoard", "publishBatch",
             [bytes.fromhex(plan["batch_id"]), number, payloads])


def cached_transport_step(action, manifest, key, plan, registry, state, bitcoin=None):
    """Replay durable public packets, never create a nonce or compute a new share.

    load_plan and collect must authenticate the current board before this call.
    An incomplete cache goes through the ordinary deadline/UTXO validation path.
    """
    from eth_abi import encode
    from eth_utils import keccak

    serialize_plan(plan, manifest, allow_commitments=True)
    if action not in ("btc-commit", "btc-sign"):
        raise ValueError("BTC_ACTION_UNSUPPORTED")
    with state.exclusive():
        if state.get("btc-payments-recovery:" + manifest.context.hex()):
            raise ValueError("PAYMENTS_PAUSED_FOR_RECOVERY")
        entries = [("btc:" + manifest.context.hex() + ":" + plan["batch_id"] + ":" + str(i))
                   for i in range(len(plan["inputs"]))]
        saved = [state.get(sid) for sid in entries]
        if any(value is not None and value.get("status") not in ("nonce-created", "share-prepared")
               for value in saved):
            raise ValueError("BTC_CACHED_SIGNING_STATE_INVALID")
        if any(value is None for value in saved) or (action == "btc-sign" and
                any(value["status"] != "share-prepared" for value in saved)):
            return None
        pinned = require_registry(manifest, key, registry, state, plan["batch_registry"])
        if plan["network"] != graph_network(pinned["graph"]):
            raise ValueError("BITCOIN_NETWORK_MISMATCH")
        check_bitcoin_graph(registry, pinned["graph"], bitcoin)
        if (manifest.purpose != "BTC_SPEND" or plan["wallet_result"] != key["result_hash"]
                or plan["wallet_script"] != "5120" + key["result"]["group"][2:]
                or str(key["index"]) not in plan["signers"]):
            raise ValueError("BTC_PLAN_BINDING")
        h = onchain_plan_hash(manifest, plan)
        block = confirmed_block(registry.w3, pinned["graph"])
        batches = contract(registry, plan["batch_registry"], "BitcoinBatchRegistry")
        approved = batches.functions.batches(bytes.fromhex(plan["batch_id"])).call(block_identifier=block.number)
        if (bytes(approved[0]) != h or bytes(approved[1]).hex() != key["result_hash"]
                or approved[2] != int(plan["deadline"]) or approved[3] not in (1, 2)):
            raise ValueError("BATCH_APPROVAL_MISMATCH")
        if approved[3] == 1:
            # An orphaned beginSigning must be repeated through normal validation.
            return None
        from .bitcoin_input_keys import build, guard
        guard(manifest,key,plan,registry,pinned["graph"],state)
        from .btc_fee_reserve import guard as fee_guard
        fee_guard(manifest,plan,registry,pinned["graph"],bitcoin=bitcoin,state=state)
        built = build(plan)
        outputs = []
        for i, (sid, stored, message) in enumerate(zip(entries, saved, built["sighashes"])):
            if stored.get("message_hash") != message or stored.get("plan_hash") != h.hex():
                raise ValueError("NONCE_PLAN_MISMATCH")
            commitment = crypto.call("inspect_bitcoin_packet", kind="commitment",
                                     value=stored["commitments"])["value"]
            if stored["status"] == "share-prepared":
                if "nonces" in stored:
                    raise ValueError("BTC_CACHED_SIGNING_STATE_INVALID")
                state.assert_spent(sid, stored["transcript"])
            else:
                state.assert_unspent(sid)
            if action == "btc-commit":
                outputs.append(commitment)
                continue
            commitments = plan["commitments"][i]
            ids = [int(x) for x in plan["signers"]]
            if set(commitments) != set(plan["signers"]):
                raise ValueError("SIGNER_SUBSET_INVALID")
            if commitments[str(key["index"])] != commitment:
                raise ValueError("NONCE_COMMITMENT_MISMATCH")
            transcript = keccak(encode(
                ["bytes32", "bytes32", "uint16[]", "bytes[]"],
                [h, bytes.fromhex(message), ids,
                 [bytes.fromhex(commitments[str(x)]) for x in ids]],
            )).hex()
            if stored["transcript"] != transcript:
                raise ValueError("NONCE_TRANSCRIPT_MISMATCH")
            state.assert_spent(sid, transcript)
            outputs.append(crypto.call("inspect_bitcoin_packet", kind="share", value=stored["share"])["value"])
        guard(manifest,key,plan,registry,pinned["graph"],state)
        snapshot_check(registry.w3, block)
        return dict(status="COMMITTED" if action == "btc-commit" else "SIGNED",
                    participant_index=key["index"], values=outputs, plan_hash=h.hex())


def pending(a, registry):
    from .onchain_transport import board_at
    from .board_intents import decode_intent, intent_rows
    if not a.board:
        raise ValueError("DKG_BOARD_REQUIRED")
    board = board_at(registry.w3, a.board)
    block = confirmed_block(registry.w3, getattr(registry, "bitcoin_graph", {}))
    explicit_session = a.session is not None
    latest = a.session if explicit_session else board.functions.latestSession().call(block_identifier=block.number)
    signing_board = Web3.to_checksum_address(a.signing_board) if a.signing_board else None

    def candidates():
        if latest:
            # A missing nonzero indexed payload or RPC failure remains fatal.
            raw = bytes(board.functions.intents(latest).call(block_identifier=block.number))
            candidate = decode_intent(raw)
            if candidate is None and explicit_session:
                raise ValueError("BTC_OPERATION_INTENT_REQUIRED")
            if candidate is not None:
                yield latest, candidate
        if not explicit_session:
            # Read the append-only index only when latest did not yield a usable
            # payment. Corrupt/unrelated intents cannot hide an older proposal.
            for row in reversed(intent_rows(registry.w3, board, block)):
                if int(row["session"]) != latest:
                    yield int(row["session"]), row["intent"]

    for session, candidate in candidates():
        public_plan = candidate.get("bitcoin_plan")
        if not public_plan:
            continue
        try:
            serialize_plan(public_plan)
        except ValueError:
            if explicit_session:
                raise
            continue
        if a.batch and public_plan["batch_id"] != a.batch.removeprefix("0x"):
            continue
        if signing_board and public_plan["signing_board"] != signing_board:
            continue
        snapshot_check(registry.w3, block)
        return dict(status="REVIEW_REQUIRED", session=str(session), intent=candidate, plan=public_plan,
                    signing_board=public_plan["signing_board"], batch=public_plan["batch_id"])
    raise ValueError("BTC_PAYMENT_INTENT_MISSING")


def deploy_board(a, manifest, key, registry, state):
    pinned = state.get("btc-registry:" + manifest.context.hex())
    if not pinned:
        raise ValueError("BTC_REGISTRY_NOT_PINNED")
    address = a.batch_registry or pinned["graph"]["contracts"]["batches"]["address"]
    pinned = require_registry(manifest, key, registry, state, address)
    if is_public_network(graph_network(pinned["graph"])):
        check_bitcoin_graph(registry, pinned["graph"], bitcoin_for_graph(
            pinned["graph"], a.bitcoin_rpc, a.bitcoin_cookie))
    if a.max_packet_bytes is None or not 512 <= a.max_packet_bytes <= 4096:
        raise ValueError("BTC_PACKET_LIMIT_REQUIRED_512_TO_4096")
    profile = dict(deployment_id="btc-board-deploy-" + Web3.keccak(text=canonical(
                        [manifest.context.hex(), address, str(a.max_packet_bytes)])).hex(),
                   chain_id=str(manifest.chain_id), evm_rpc=registry.w3.provider.endpoint_uri,
                   **transaction_profile(pinned["graph"]),
                   steps=[dict(id="signing_board", kind="deploy", artifact="BitcoinSigningBoard",
                               args=[address, str(a.max_packet_bytes)])])
    runner = Bootstrap(profile, registry.transaction_journal)
    result = runner.apply_confirmed(registry.identity_file)
    if result["status"] != "VERIFIED":
        raise ValueError("BTC_BOARD_DEPLOYMENT_UNCONFIRMED")
    address = runner.contracts["signing_board"]["address"]
    board = checked_board(manifest, key, registry, state, address)
    with state.exclusive():
        state.put("btc-board-proof:" + board.address, board._btc_proof)
    return dict(status="DEPLOYED", signing_board=board.address,
                batch_registry=board.functions.batchRegistry().call(), max_packet_bytes=str(a.max_packet_bytes))


def _pause_status(marker):
    # Legacy markers did not distinguish an outage from a reorg. They require
    # the explicit recovery command, with the same fresh proof as a new pause.
    return "RECOVERY_REQUIRED" if marker.get("requires_review", True) else "OBSERVATION_UNAVAILABLE"


def payment_observation(a, manifest, registry, state, plan, *, recover=False):
    """Retry unavailable observations; clear a genuine reorg only by checked recovery.

    The pause never licenses replacement payments or a nonce reset. Recovery
    requires the exact settled transaction to be canonical again at escrow depth.
    """
    recovery_key = "btc-payments-recovery:" + manifest.context.hex()
    previous = state.get(recovery_key)
    if previous and previous.get("kind")=="BTC_FEE_RESERVE":
        raise ValueError("BTC_FEE_RECONCILIATION_RECOVERY_REQUIRED")
    if previous and previous["batch"] != plan["batch_id"]:
        if recover:
            raise ValueError("BTC_RECOVERY_BATCH_MISMATCH")
        return dict(status=_pause_status(previous), **previous)
    if previous and _pause_status(previous) == "RECOVERY_REQUIRED" and not recover:
        return dict(status="RECOVERY_REQUIRED", **previous)
    pinned = state.get("btc-registry:" + manifest.context.hex())
    if not pinned:
        raise ValueError("BTC_REGISTRY_NOT_PINNED")
    snapshot = confirmed_block(registry.w3, pinned["graph"])
    batches = contract(registry, plan["batch_registry"], "BitcoinBatchRegistry")
    # Both escrow generations expose the same payment and depth read ABI.
    escrow = contract(registry, batches.functions.withdrawalEscrow().call(block_identifier=snapshot.number),
        "BitcoinWithdrawalEscrow")
    payment = escrow.functions.payment(bytes.fromhex(plan["batch_id"])).call(block_identifier=snapshot.number)
    snapshot_check(registry.w3, snapshot)
    if not payment[2]:
        if previous:
            raise ValueError("BTC_RECOVERY_PAYMENT_MISMATCH")
        return None
    txid = bytes(payment[0])[::-1].hex()
    if previous and txid != previous["txid"]:
        raise ValueError("BTC_RECOVERY_PAYMENT_MISMATCH")
    if not a.bitcoin_rpc or not a.bitcoin_cookie:
        if previous or recover:
            raise ValueError("BITCOIN_RPC_REQUIRED")
        return dict(status="PAID", txid=txid, bitcoin_observation="NOT_CHECKED")
    try:
        pinned = state.get("btc-registry:" + manifest.context.hex())
        if not pinned:
            raise ValueError("BTC_REGISTRY_NOT_PINNED")
        bitcoin = bitcoin_for_graph(pinned["graph"], a.bitcoin_rpc, a.bitcoin_cookie)
        if plan["network"] != graph_network(pinned["graph"]):
            raise ValueError("BITCOIN_NETWORK_MISMATCH")
        bitcoin.check_network()
        transaction = bitcoin.call("getrawtransaction", txid, True)
        confirmations, block_hash, height = payment_position(bitcoin, txid, transaction)
        if confirmations < escrow.functions.depth().call(block_identifier=snapshot.number):
            raise ValueError("BITCOIN_PAYMENT_REORG")
        if previous or recover:
            checked_payment_proof(bitcoin, txid, block_hash, height)
        snapshot_check(registry.w3, snapshot)
    except Exception as error:
        # Unavailable observations still block spending, but recover by retry.
        # A positive contradiction remains sticky even if a later RPC is down.
        diagnostic = observation_error(error)
        requires_review = bool(previous and previous.get("requires_review", True)) or (
            diagnostic["error_code"] in ("BITCOIN_PAYMENT_REORG", "INVALID_PAYMENT_PROOF", "BTC_BOARD_SNAPSHOT_REORG"))
        detail = dict(batch=plan["batch_id"], txid=txid,
                      reason="SETTLED_PAYMENT_NOT_INDEPENDENTLY_CONFIRMED",
                      requires_review=requires_review, **diagnostic)
        if not requires_review and not retryable_payment_observation(error):
            detail["retryable"] = False
        with state.exclusive():
            if state.get(recovery_key) != previous:
                raise ValueError("BTC_RECOVERY_CHANGED")
            state.put(recovery_key, detail)
        return dict(status=_pause_status(detail), **detail)
    if previous:
        with state.exclusive():
            if state.get(recovery_key) != previous:
                raise ValueError("BTC_RECOVERY_CHANGED")
            state.put("btc-payments-recovered:" + manifest.context.hex() + ":" + plan["batch_id"],
                      dict(previous=previous, txid=txid, bitcoin_block=block_hash,
                           evm_number=str(snapshot.number), evm_hash=snapshot.hash.hex(),
                           manually_reviewed=bool(recover)))
            state.put(recovery_key, {})
    return dict(status="PAID", txid=txid, bitcoin_observation="CONFIRMED")


def retry_payment_observation(a, manifest, registry, state):
    """Spend guard for retryable pauses, executed before exposing any packets."""
    marker = state.get("btc-payments-recovery:" + manifest.context.hex())
    if not marker:
        return None
    if marker.get("kind")=="BTC_FEE_RESERVE":
        raise ValueError("BTC_FEE_RECONCILIATION_RECOVERY_REQUIRED")
    if _pause_status(marker) == "RECOVERY_REQUIRED":
        raise ValueError("PAYMENTS_PAUSED_FOR_RECOVERY")
    plan = cached_plan(manifest, state, marker["batch"])
    result = payment_observation(a, manifest, registry, state, plan)
    return result if result and result["status"] != "PAID" else None


def _payment_graph_preflight(registry, graph, bitcoin):
    """Wait only on a transient read, before signing or transmitting anything."""
    try:
        if graph_network(graph) == "regtest":
            bitcoin.check_network()
        else:
            check_bitcoin_graph(registry, graph, bitcoin)
    except BitcoinRPCError as error:
        if error.rpc_code != -5 and retryable_payment_observation(error):
            return dict(status="OBSERVATION_UNAVAILABLE", **observation_error(error))
        if error.http_status in (401, 403):
            raise BitcoinRPCError("BITCOIN_RPC_AUTH_REJECTED", http_status=error.http_status) from None
        raise


def dispatch(a, manifest, key, registry, state):
    if a.action.startswith("btc-address-mint-"):
        from .address_mint import dispatch as address_dispatch
        return address_dispatch(a, manifest, key, registry, state)
    if a.action == "btc-deploy-board":
        return deploy_board(a, manifest, key, registry, state)
    if a.action == "btc-pending":
        return pending(a, registry)
    mint_only = bool(getattr(a, "mint_only", False))
    pinned = state.get(("btc-mint-registry:" if mint_only else "btc-registry:") + manifest.context.hex())
    if not pinned:
        raise ValueError("BTC_REGISTRY_NOT_PINNED")
    options = {"mint_only": True} if mint_only else {}
    graph = require_registry(manifest, key, registry, state,
                             pinned["graph"]["contracts"]["batches"]["address"], **options)["graph"]
    registry.bitcoin_graph = graph
    if a.action in ("btc-mint-status", "btc-mint-retire", "btc-mint-transfer-propose",
                    "btc-mint-transfer-execute", "btc-mint-transfer-retire"):
        from .mint_admission_client import management
        return management(a, manifest, key, registry, state, graph)
    if a.action in ("btc-relay", "btc-network-status"):
        return network_action(a, registry, graph)
    bitcoin = None
    if is_public_network(graph_network(graph)) and a.action != "btc-status":
        bitcoin = bitcoin_for_graph(graph, a.bitcoin_rpc, a.bitcoin_cookie)
        if a.action not in ("btc-settle","btc-mint-propose","btc-mint-execute","btc-mint-retry",
                             "btc-treasury-propose","btc-treasury-execute"):
            if a.action in ("btc-commit", "btc-sign", "btc-broadcast", "btc-recover"):
                unavailable = _payment_graph_preflight(registry, graph, bitcoin)
                if unavailable:
                    return unavailable
            else:
                check_bitcoin_graph(registry, graph, bitcoin)
    if a.action=='btc-import-plan':
        from pathlib import Path
        from .bitcoin_onchain_plan import validate_plan,MAX_FEE_PLAN_BYTES
        path=Path(a.plan_file)
        if path.stat().st_size>MAX_FEE_PLAN_BYTES:raise ValueError('BTC_PLAN_TOO_LARGE')
        payload=json.loads(path.read_text());plan=payload.get('plan',payload)
        validate_plan(plan,manifest)
        if (plan["version"] not in ("4", "5") or a.batch and a.batch.removeprefix("0x") != plan["batch_id"]
                or a.signing_board and a.signing_board != plan["signing_board"]):
            raise ValueError("WITHDRAWAL_FEE_PLAN_REQUIRED")
        board=checked_board(manifest,key,registry,state,plan['signing_board'])
        from .bitcoin_onchain_payment import _graph,_terms,_validate_binding
        graph,contracts=_graph(manifest,key,registry,state,board)
        _validate_binding(manifest,key,board,plan)
        bitcoin=bitcoin or bitcoin_for_graph(graph,a.bitcoin_rpc,a.bitcoin_cookie)
        check_bitcoin_graph(registry,graph,bitcoin);_terms(contracts,plan,bitcoin)
        from .bitcoin_input_keys import guard
        guard(manifest, key, plan, registry, graph, state)
        if plan["version"] == "4":
            from .btc_fee_reserve import guard as fee_guard
            fee_guard(manifest,plan,registry,graph,bitcoin=bitcoin,state=state,reserved=False)
        remember_plan(manifest,state,plan)
        return dict(status='BTC_FEE_PLAN_IMPORTED',plan_hash=onchain_plan_hash(manifest,plan).hex(),batch=plan['batch_id'])
    funding_actions = ("btc-mint-propose", "btc-mint-execute", "btc-mint-retry", "btc-treasury-propose", "btc-treasury-execute")
    if a.action in funding_actions:
        if not a.signing_board:
            raise ValueError("BTC_BOARD_BINDING")
        board = checked_board(manifest, key, registry, state, a.signing_board)
        from .bitcoin_funding import dispatch as funding
        return funding(a, manifest, key, registry, state, board)
    if not a.signing_board or not a.batch:
        info = pending(a, registry)
        a.signing_board = a.signing_board or info["signing_board"]
        a.batch = a.batch or info["batch"]
    a.batch = a.batch.removeprefix("0x")
    hex_value(a.batch, 32)
    board = checked_board(manifest, key, registry, state, a.signing_board)
    if a.action == "btc-prepare":
        from .bitcoin_onchain_payment import dispatch as payment
        return payment(a.action, a, manifest, key, registry, state, board)
    if a.action in ("btc-propose", "btc-execute", "btc-publish-plan"):
        try:
            plan = cached_plan(manifest, state, a.batch)
        except ValueError as exc:
            if str(exc) != "BTC_PLAN_NOT_PREPARED":
                raise
            # Reserve intent precedes plan publication. Another participant can execute it
            # using the public DKG intent; payment dispatch verifies its exact authorization.
            plan = pending(a, registry)["plan"]
        if plan["signing_board"] != board.address:
            raise ValueError("BTC_PLAN_BINDING")
    else:
        plan = load_plan(manifest, key, registry, state, board.address, a.batch)
    if plan["network"] != graph_network(graph):
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    if a.action == "btc-recover-inputs":
        from .bitcoin_input_keys import recover
        return recover(manifest,key,plan,registry,graph,state)
    if a.action == "btc-recover":
        marker = state.get("btc-payments-recovery:" + manifest.context.hex())
        if not marker:
            return dict(status="PAYMENTS_NOT_PAUSED")
        observed = payment_observation(a, manifest, registry, state, plan, recover=True)
        if observed and observed["status"] == "PAID":
            return dict(observed, status="PAYMENTS_RESUMED", batch=plan["batch_id"])
        return observed
    if a.action in ("btc-commit", "btc-sign", "btc-broadcast"):
        paused = retry_payment_observation(a, manifest, registry, state)
        if paused:
            return paused
    if a.action == "btc-broadcast":
        # A restarted operator starts in broadcast phase even after settlement.
        # Only the confirmed escrow record for its original batch can finish it.
        batches = contract(registry, plan["batch_registry"], "BitcoinBatchRegistry")
        if batches.functions.batchState(bytes.fromhex(a.batch)).call() != 2:
            observed = payment_observation(a, manifest, registry, state, plan)
            if observed:
                if observed.get("bitcoin_observation") == "NOT_CHECKED":
                    raise ValueError("BITCOIN_RPC_REQUIRED")
                return observed
            raise ValueError("BTC_BATCH_NOT_SIGNING")
    if a.action in ("btc-propose", "btc-execute", "btc-settle"):
        if a.action == "btc-settle":
            observed = payment_observation(a, manifest, registry, state, plan)
            if observed:
                return observed
        from .bitcoin_onchain_payment import dispatch as payment
        return payment(a.action, a, manifest, key, registry, state, board, plan)
    if a.action == "btc-publish-plan":
        raw = serialize_plan(plan, manifest)
        previous = bytes(board.functions.plan(bytes.fromhex(a.batch)).call())
        if previous and previous != raw:
            raise ValueError("BTC_PLAN_CONFLICT")
        if not previous:
            if plan.get('version')=='4':
                from .withdrawal_fee_transport import publish_plan
                def publish(method,args,part):
                    from .bitcoin_input_keys import guard
                    from .btc_fee_reserve import guard as fee_guard
                    guard(manifest,key,plan,registry,graph)
                    fee_guard(manifest,plan,registry,graph,bitcoin=bitcoin,state=state)
                    return send(registry,board.address,'BitcoinSigningBoard',method,args,graph=graph)
                publish_plan(board,bytes.fromhex(a.batch),registry.account.address,raw,publish)
            else:send(registry, board.address, "BitcoinSigningBoard", "publishPlan", [bytes.fromhex(a.batch), raw])
        load_plan(manifest, key, registry, state, board.address, a.batch)
        return dict(status="PLAN_PUBLISHED", plan_hash=onchain_plan_hash(manifest, plan).hex())
    commitments, shares, missing_c, missing_s = collect(manifest, registry, board, plan)
    if a.action == "btc-status":
        batches = contract(registry, plan["batch_registry"], "BitcoinBatchRegistry")
        batch_state = batches.functions.batchState(bytes.fromhex(a.batch)).call()
        escrow = contract(registry, batches.functions.withdrawalEscrow().call(), "BitcoinWithdrawalEscrow")
        observed = payment_observation(a, manifest, registry, state, plan)
        return dict(status=observed["status"] if observed else "CANCELLED" if batch_state == 3 else
                    "READY_TO_BROADCAST" if not missing_s and not missing_c else
                    "READY_TO_SIGN" if not missing_c else "AWAITING_COMMITMENTS", plan=plan,
                    plan_hash=onchain_plan_hash(manifest, plan).hex(),
                    commitments_count=sum(map(len, commitments)), shares_count=sum(map(len, shares)),
                    missing_commitments=missing_c, missing_shares=missing_s,
                    payment=observed)
    from .bitcoin_worker import BitcoinRPC, sign_step, BitcoinWorker
    if a.action in ("btc-commit", "btc-sign"):
        if str(key["index"]) not in plan["signers"] or registry.account.address != key["participant"]:
            raise ValueError("BTC_NOT_SELECTED_SIGNER")
        if a.action == "btc-sign" and missing_c:
            raise ValueError("BTC_COMMITMENTS_MISSING")
        work = dict(plan, commitments=commitments) if a.action == "btc-sign" else plan
        result = cached_transport_step(a.action, manifest, key, work, registry, state, bitcoin)
        if result is None:
            bitcoin = bitcoin or bitcoin_for_graph(graph, a.bitcoin_rpc, a.bitcoin_cookie)
            result = sign_step(a.action, manifest, key, work, registry, state, bitcoin)
        publish_values(manifest, key, registry, board, plan,
                       "commitment" if a.action == "btc-commit" else "share", result["values"])
        return dict(status=result["status"], participant_index=result["participant_index"],
                    plan_hash=result["plan_hash"], published=len(result["values"]))
    if a.action != "btc-broadcast":
        raise ValueError("BTC_ACTION_UNSUPPORTED")
    if missing_c or missing_s:
        raise ValueError("BTC_SIGNATURE_PACKETS_MISSING")
    bitcoin = bitcoin or bitcoin_for_graph(graph, a.bitcoin_rpc, a.bitcoin_cookie)
    unavailable = _payment_graph_preflight(registry, graph, bitcoin)
    if unavailable:
        return unavailable
    from .bitcoin_input_keys import build, guard, versioned
    guard(manifest,key,plan,registry,graph)
    built = build(plan)
    signatures = [crypto.call("bitcoin_aggregate_v1" if versioned(plan) else "aggregate",
                              public_package=key["result"]["public_package"],
                              commitments=commitments[i], shares=shares[i], message=message,
                              **({"input_key": plan["input_keys"][i]} if versioned(plan) else {}))["signature"]
                  for i, message in enumerate(built["sighashes"])]
    package = dict(plan, input_key_version=1) if versioned(plan) else plan
    signed = crypto.call("bitcoin_attach_v1" if versioned(plan) else "bitcoin_attach",
        unsigned=built["unsigned"], package=package, signatures=signatures)
    batches = contract(registry, plan["batch_registry"], "BitcoinBatchRegistry")
    if batches.functions.batchState(bytes.fromhex(a.batch)).call() != 2:
        raise ValueError("BTC_BATCH_NOT_SIGNING")
    worker = BitcoinWorker(a.state + ".bitcoin.sqlite", bitcoin)
    guard(manifest,key,plan,registry,graph)
    from .bitcoin_onchain_payment import _previous
    previous = _previous(plan)
    txid = worker.broadcast(a.batch, signed, **({"replaces": previous.hex()} if any(previous) else {}))
    return dict(status="BROADCAST", txid=txid, wtxid=signed["wtxid"])


def network_action(a, registry, graph):
    """Keep the pinned mainnet relay synchronized without a payment or private nonce."""
    from .bitcoin_network import sync_relay

    bitcoin = bitcoin_for_graph(graph, a.bitcoin_rpc, a.bitcoin_cookie)
    if not is_public_network(graph_network(graph)):
        bitcoin.check_network()
        return dict(status="REGTEST_NETWORK_READY", network="regtest")
    from .zk_spv import is_zk
    if is_zk(graph.get("bitcoin_policy")):
        try:
            return check_bitcoin_graph(registry, graph, bitcoin)
        except ValueError as exc:
            if str(exc) != "MAINNET_RELAY_STALE":
                raise
            return dict(status="AWAITING_ZK_PROOF", backend="zk-state-v2")
    if a.action == "btc-network-status":
        return dict(status="MAINNET_RELAY_READY", network=graph_network(graph),
                    observation=check_bitcoin_graph(registry, graph, bitcoin))
    relay = contract(registry, graph["contracts"]["relay"]["address"], relay_artifact(graph_network(graph)))
    def send_header(raw):
        try:
            return send(registry, relay.address, relay_artifact(graph_network(graph)), "addBlockHeader",
                        [bytes.fromhex(raw)], graph=graph)
        except ValueError as exc:
            if str(exc) != "BTC_TRANSACTION_UNCONFIRMED":
                raise
            return dict(status="PENDING")
    from .relay_batch import DurableRelayBatch
    send_headers = DurableRelayBatch.for_rpc(registry.identity_file, registry.w3, relay.address,
                                             graph["bitcoin_policy"], int(graph["evm_confirmations"]))
    result = sync_relay(bitcoin, relay, graph["bitcoin_policy"], send_header,
                        send_headers=send_headers, evm_confirmations=int(graph["evm_confirmations"]))
    if result["status"] == "MAINNET_RELAY_READY":
        check_bitcoin_graph(registry, graph, bitcoin)
    return result
