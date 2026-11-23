"""An exact bound replacement can move to another broadcaster without signing again."""

import json
from stat import S_IMODE
from types import SimpleNamespace

import pytest

from signer_agent import bitcoin_onchain, bitcoin_onchain_payment as payment, bitcoin_worker
from signer_agent.bitcoin_proofs import sha256d
from signer_agent.bitcoin_rpc import BitcoinRPCError


ROOT = b"p" * 32
BATCH = b"b" * 32
REQUEST = b"r" * 32


def signed_payment(value, prevout="ab" * 32):
    inputs = "01" + prevout + "0000000000fdffffff"
    outputs = "01" + value.to_bytes(8, "little").hex() + "0151"
    version, locktime = "02000000", "00000000"
    stripped = version + inputs + outputs + locktime
    raw = version + "0001" + inputs + outputs + "010101" + locktime
    return dict(signed=raw, txid=sha256d(bytes.fromhex(stripped))[::-1].hex(),
                wtxid=sha256d(bytes.fromhex(raw))[::-1].hex())


@pytest.fixture
def bound_plan(monkeypatch):
    manifest = SimpleNamespace(context=b"c" * 32)
    signed = signed_payment(900)
    plan = dict(version="5", batch_id=BATCH.hex(), previous=ROOT.hex(), batch_registry="batches",
                withdrawal_refs=[REQUEST.hex()], inputs=[dict(outpoint="ab" * 32 + ":0")])
    graph = {"contracts": {
        "batches": dict(address="batches", artifact="BitcoinBatchRegistry"),
        "escrow": dict(address="escrow", artifact="BitcoinWithdrawalEscrowV2"),
    }}
    confirmed = SimpleNamespace(number=12, hash=b"safe")
    latest = SimpleNamespace(number=15, hash=b"latest")
    view = SimpleNamespace(bound=True, latest_bound=True, anchor=b"safe", root=ROOT,
                           parent_refs=[REQUEST], parent_inputs=[bitcoin_worker.outpoint_hash(plan["inputs"][0]["outpoint"])],
                           paid=False, approved=True)
    eth = SimpleNamespace(get_block=lambda n:
                          latest if n in ("latest", 15) else SimpleNamespace(number=n, hash=view.anchor),
                          block_number=15)
    registry = SimpleNamespace(w3=SimpleNamespace(eth=eth))
    monkeypatch.setattr(payment, "confirmed_block", lambda *_: confirmed)
    def expected_plan(manifest_, contracts, plan_, block, *, allowed_states):
        assert (manifest_, plan_, block, allowed_states) == (manifest, plan, 12, (2,))
        return view.approved
    monkeypatch.setattr(payment, "_batch_matches", expected_plan)

    def call(value, *args):
        return SimpleNamespace(call=lambda *, block_identifier: value(block_identifier, *args))

    batches = SimpleNamespace(address="batches", functions=SimpleNamespace(
        withdrawalEscrow=lambda: call(lambda _: "escrow"),
        reservationRoot=lambda batch: call(lambda *_: view.root, batch),
        getInputs=lambda batch: call(lambda *_: view.parent_inputs, batch),
        batchState=lambda batch: call(lambda *_: 2, batch),
    ))
    escrow = SimpleNamespace(address="escrow", functions=SimpleNamespace(
        isBound=lambda batch: call(lambda n, _: view.bound if n == 12 else view.latest_bound, batch),
        payment=lambda batch: call(lambda _, bid:
            (b"x" * 32, b"", view.paid, view.parent_refs) if bid == ROOT else
            (bytes.fromhex(signed["txid"])[::-1], b"", False, [REQUEST]), batch),
    ))
    monkeypatch.setattr(payment, "_contract", lambda _, addr, kind: {"batches": batches, "escrow": escrow}[addr])
    return SimpleNamespace(manifest=manifest, signed=signed, plan=plan, graph=graph, view=view, registry=registry,
        check=lambda txid=None: payment.confirmed_v5_broadcast(manifest, registry, graph, plan, txid or signed["txid"]))


def test_conflicting_version_uses_confirmed_lineage_and_rechecks_live_binding(bound_plan):
    ancestry, recheck = bound_plan.check()
    assert ancestry == (ROOT.hex(), ROOT.hex())
    recheck()
    bound_plan.view.latest_bound = False
    with pytest.raises(ValueError, match="ESCROW_PAYMENT_MISMATCH"):
        recheck()
    bound_plan.view.latest_bound = True
    bound_plan.view.anchor = b"reorg"
    with pytest.raises(ValueError, match="BTC_BOARD_SNAPSHOT_REORG"):
        recheck()


@pytest.mark.parametrize("change,error", [
    ({"approved": False}, "ESCROW_PAYMENT_MISMATCH"),
    ({"bound": False}, "ESCROW_PAYMENT_MISMATCH"),
    ({"parent_refs": [b"x" * 32]}, "BTC_REPLACEMENT_PLAN"),
    ({"parent_inputs": [b"x" * 32]}, "BTC_REPLACEMENT_PLAN"),
    ({"root": bytes(32)}, "BTC_REPLACEMENT_PLAN"),
    ({"paid": True}, "BTC_REPLACEMENT_PLAN"),
])
def test_conflicting_or_resolved_binding_stops_before_broadcast(bound_plan, change, error):
    for key, value in change.items():
        setattr(bound_plan.view, key, value)
    with pytest.raises(ValueError, match=error):
        bound_plan.check()


def test_cold_broadcaster_recovers_after_lost_send_reply(tmp_path, bound_plan):
    signed, observed = bound_plan.signed, {}
    sends, reads = [], []

    def call(method, *args):
        reads.append(method)
        if method == "getrawtransaction":
            if not observed:
                raise BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-5)
            return observed["raw"]
        if method == "testmempoolaccept":
            assert args == ([signed["signed"]],)
            with worker.db() as db:
                assert db.execute("SELECT status FROM payments").fetchall() == [("SIGNED",)]
                assert db.execute("SELECT id,root,previous FROM versions").fetchall() == [
                    (BATCH.hex(), ROOT.hex(), ROOT.hex())]
            return [{"allowed": True}]
        assert method == "sendrawtransaction"
        sends.append(args[0])
        observed["raw"] = args[0]
        raise BitcoinRPCError("BITCOIN_RPC_UNAVAILABLE")

    rpc = SimpleNamespace(check_network=lambda: None, call=call)
    path = tmp_path / "payments.sqlite"
    worker = bitcoin_worker.BitcoinWorker(path, rpc)
    ancestry, recheck = bound_plan.check()
    with pytest.raises(BitcoinRPCError, match="BITCOIN_RPC_UNAVAILABLE"):
        worker.broadcast_confirmed_replacement(BATCH.hex(), signed, ancestry, recheck)
    second = bitcoin_worker.BitcoinWorker(path, rpc)
    assert second.broadcast_confirmed_replacement(BATCH.hex(), signed, ancestry, recheck) == signed["txid"]
    assert sends == [signed["signed"]] and reads == ["getrawtransaction", "testmempoolaccept",
                                                     "sendrawtransaction", "getrawtransaction"]
    with second.db() as db:
        assert db.execute("SELECT signed,txid,status FROM payments").fetchall() == [
            (signed["signed"], signed["txid"], "BROADCAST")]


def test_late_resolution_retains_exact_bytes_and_never_sends(tmp_path, bound_plan):
    def call(method, *args):
        if method == "getrawtransaction":
            raise BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-5)
        assert method == "testmempoolaccept"
        bound_plan.view.latest_bound = False
        return [{"allowed": True}]

    worker = bitcoin_worker.BitcoinWorker(tmp_path / "payments.sqlite",
                    SimpleNamespace(check_network=lambda: None, call=call))
    ancestry, recheck = bound_plan.check()
    with pytest.raises(ValueError, match="ESCROW_PAYMENT_MISMATCH"):
        worker.broadcast_confirmed_replacement(BATCH.hex(), bound_plan.signed, ancestry, recheck)
    with worker.db() as db:
        assert db.execute("SELECT status FROM payments").fetchall() == [("SIGNED",)]


@pytest.mark.parametrize("case", ["evicted", "outage", "prior-proof", "unrelated", "review-required"])
def test_same_worker_replaces_only_an_unproved_missing_predecessor(tmp_path, bound_plan, case):
    def call(method, *args):
        if method == "getrawtransaction":
            raise BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-28 if case == "outage" else -5)
        if method == "testmempoolaccept":
            return [{"allowed": True}]
        assert method == "sendrawtransaction" and case == "evicted"
        sends.append(args[0])
        return bound_plan.signed["txid"]

    sends = []
    rpc = SimpleNamespace(check_network=lambda: None, call=call)
    worker = bitcoin_worker.BitcoinWorker(tmp_path / "payments.sqlite", rpc)
    parent = signed_payment(1000)
    local_batch = "unrelated" if case == "unrelated" else ROOT.hex()
    worker.remember_observed_broadcast(local_batch, parent)
    assert worker.confirmation(local_batch)["status"] == "OBSERVATION_UNAVAILABLE"
    with worker.db() as db:
        if case == "prior-proof":
            db.execute("UPDATE payments SET proof='{\"depth\":6}' WHERE id=?", (local_batch,))
        elif case == "review-required":
            db.execute("UPDATE payments SET status='RECOVERY_REQUIRED' WHERE id=?", (local_batch,))
    ancestry, recheck = bound_plan.check()
    if case == "evicted":
        assert worker.broadcast_confirmed_replacement(BATCH.hex(), bound_plan.signed,
                                                      ancestry, recheck) == bound_plan.signed["txid"]
        assert sends == [bound_plan.signed["signed"]]
        with worker.db() as db:
            assert db.execute("SELECT id FROM payment_observations").fetchall() == []
            audit = json.loads(db.execute("SELECT detail FROM payment_recoveries WHERE id=?", (local_batch,)).fetchone()[0])
            assert audit["replacement"] == BATCH.hex() and audit["previous"]["rpc_error_code"] == -5
    else:
        with pytest.raises(ValueError, match="BITCOIN_OBSERVATION_UNAVAILABLE|PAYMENTS_PAUSED_FOR_RECOVERY"):
            worker.broadcast_confirmed_replacement(BATCH.hex(), bound_plan.signed, ancestry, recheck)
        with worker.db() as db:
            assert db.execute("SELECT id FROM payments").fetchall() == [(local_batch,)]
            assert db.execute("SELECT id FROM payment_observations").fetchall() == [(local_batch,)]
        assert sends == []


def test_signed_export_keeps_exact_raw_private_and_does_not_broadcast(tmp_path, bound_plan, monkeypatch):
    from signer_agent import bitcoin_input_keys

    e = bound_plan
    e.plan.update(signing_board="board", network="regtest")
    board = SimpleNamespace(address="board")
    key = {"result": {"public_package": "existing-public-package"}}
    state = SimpleNamespace(get=lambda name: dict(graph=e.graph) if name.startswith("btc-registry:") else None)
    rpc = SimpleNamespace(check_network=lambda: None)
    monkeypatch.setattr(bitcoin_onchain, "require_registry", lambda *_, **__: dict(graph=e.graph))
    monkeypatch.setattr(bitcoin_onchain, "checked_board", lambda *_: board)
    monkeypatch.setattr(bitcoin_onchain, "load_plan", lambda *_: e.plan)
    monkeypatch.setattr(bitcoin_onchain, "collect", lambda *_: ([{}], [{}], [], []))
    monkeypatch.setattr(bitcoin_onchain, "graph_network", lambda _: "regtest")
    monkeypatch.setattr(bitcoin_onchain, "bitcoin_for_graph", lambda *_: rpc)
    monkeypatch.setattr(bitcoin_onchain, "check_bitcoin_graph", lambda *_: None)
    monkeypatch.setattr(bitcoin_onchain, "confirmed_block", lambda *_: SimpleNamespace(number=12))
    monkeypatch.setattr(bitcoin_onchain, "contract", lambda *_: SimpleNamespace(
        functions=SimpleNamespace(batchState=lambda _: SimpleNamespace(call=lambda: 2))))
    monkeypatch.setattr(payment, "review_custody_outputs", lambda *_: None)
    monkeypatch.setattr(bitcoin_input_keys, "guard", lambda *_: None)
    monkeypatch.setattr(bitcoin_input_keys, "build", lambda _: dict(unsigned="00", sighashes=["digest"]))
    monkeypatch.setattr(bitcoin_input_keys, "versioned", lambda _: False)
    monkeypatch.setattr(bitcoin_onchain.crypto, "call", lambda op, **kw:
                        {"aggregate": {"signature": "existing"}, "bitcoin_attach": e.signed}[op])
    monkeypatch.setattr(bitcoin_worker, "BitcoinWorker", lambda *_: pytest.fail("export sent to payment worker"))
    out = tmp_path / "signed.json"
    args = SimpleNamespace(action="btc-broadcast", signing_board="board", batch=BATCH.hex(),
                           bitcoin_rpc="rpc", bitcoin_cookie="cookie", signed_out=out)
    assert bitcoin_onchain.dispatch(args, e.manifest, key, e.registry, state) == dict(
        status="BTC_SIGNED_EXPORTED", txid=e.signed["txid"], wtxid=e.signed["wtxid"])
    assert json.loads(out.read_text()) == e.signed
    assert S_IMODE(out.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="OUTPUT_EXISTS"):
        bitcoin_onchain.dispatch(args, e.manifest, key, e.registry, state)
