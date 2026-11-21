"""Funding and heartbeat invariants; no proof computation or EVM fixture."""
from copy import deepcopy
from types import SimpleNamespace as NS
import pytest
from web3 import Web3
from signer_agent.zk_worker import Inbox, load_config
from signer_agent import zk_worker_service as service, zk_worker_heartbeat as heartbeat
from signer_agent.zk_worker_prover import write
from test_zk_worker import config
from test_zk_worker import make_worker, simulated


def worker(tmp_path, mode="confederation"):
    c = config(tmp_path); c["version"] = "2"
    c["service"] = dict(mode=mode, binding=None, max_self_funded_cost_wei="15", max_self_funded_job_cost_wei="10")
    c["heartbeat"] = dict(enabled=True, interval_seconds="300", lead_seconds="30", grace_seconds="60",
        safety_margin_seconds="10", finality_budget_seconds="30", continuous_fresh=False,
        max_status_attempts="2", status_backoff_seconds="5")
    return NS(c=c, inbox=Inbox(c))


def test_own_funding_budget_atomic_dedup_and_persists(tmp_path):
    w = worker(tmp_path)
    service.reserve(w, "tx1", 8, ["one"])
    service.reserve(w, "tx1", 8, ["one"])
    with pytest.raises(ValueError, match="SELF_FUNDING_JOB_BUDGET"):
        service.reserve(w, "tx2", 3, ["one"])
    assert w.inbox.get("self-cost:tx2") is None
    service.reserve(w, "tx2", 7, ["two"])
    with pytest.raises(ValueError, match="SELF_FUNDING_BUDGET"):
        service.reserve(w, "tx3", 1, ["three"])
    w.inbox.close(); w.inbox = Inbox(w.c)
    assert w.inbox.get("funding:tx1")["source"] == "CONFEDERATION"
    assert w.inbox.get("self-cost:tx1")["cost"] == "8"
    assert dict(w.inbox.db.execute("SELECT * FROM reservations")) == {"tx1": "8", "tx2": "7"}
    w.inbox.close()


def test_global_budget_failure_rolls_back_own_ledger(tmp_path):
    w = worker(tmp_path); w.c["max_total_cost_wei"] = "5"
    with pytest.raises(ValueError, match="WORKER_GAS_BUDGET"):
        service.reserve(w, "tx1", 6, ["one"])
    assert w.inbox.get("self-cost:tx1") is None and w.inbox.get("funding:tx1") is None
    w.inbox.close()


def test_funding_does_not_read_treasury_balance_and_never_refunds_own_reserve(tmp_path, monkeypatch):
    w = worker(tmp_path)
    service.reserve(w, "tx1", 8, ["one"])
    monkeypatch.setattr(service, "source", lambda *a: pytest.fail("own reservation must remain charged"))
    service.reserve(w, "tx1", 9, ["one"])
    assert w.inbox.get("self-cost:tx1")["cost"] == "9"
    w.inbox.close()


def test_paid_reorg_rechecks_before_signing_new_bytes(tmp_path, monkeypatch):
    w = worker(tmp_path)
    w.inbox.put("funding:tx1", dict(source="PAID_SERVICE", jobs=["aa"*32]))
    monkeypatch.setattr(service, "source", lambda *a: dict(source="CONFEDERATION", jobs=[]))
    service.reserve(w, "tx1", 8, ["one"])
    assert w.inbox.get("self-cost:tx1")["cost"] == "8"
    assert w.inbox.get("funding:tx1")["source"] == "CONFEDERATION"
    w.inbox.close()


def test_paid_required_does_not_block_explicit_own_security_work(tmp_path):
    w = worker(tmp_path, "paid")
    with pytest.raises(ValueError, match="AWAITING_PAYMENT"): service.source(w, ["consumer"])
    assert service.source(w, ["heartbeat:job"])["source"] == "CONFEDERATION"
    w.inbox.close()


def test_canonical_paid_terms_are_revalidated_after_reorg(tmp_path, monkeypatch):
    w = worker(tmp_path, "paid_or_confederation"); w.w3 = object()
    record = dict(service_job_id="aa"*32, need_ids=["one"], deadline="100")
    w.inbox.put("service:one", record)
    values = dict(paid=True, semantic=service.semantic_key(w, ["one"]))
    class Client:
        def __init__(self, *a): self.registry = object()
        def _binding(self): return object()
        def service_profile(self, block): return object()
        def _anchor(self, block): pass
        def _call(self, target, name, block, *a):
            if name == "state": return w.c["zk_state"]
            if name == "paid": return values["paid"]
            if name == "jobs": return [bytes.fromhex("aa"*32), values["semantic"], bytes(32), "payer", bytes(32), 100]
            raise AssertionError(name)
    monkeypatch.setattr(service, "GasTreasuryClient", Client)
    assert service.source(w, ["one"])["source"] == "PAID_SERVICE"
    values["paid"] = False
    assert service.source(w, ["one"])["source"] == "CONFEDERATION"
    values.update(paid=True, semantic=bytes(32))
    with pytest.raises(ValueError, match="TERMS_REVALIDATION"): service.source(w, ["one"])
    w.inbox.close()


def test_v2_config_limits_and_scope(tmp_path):
    w = worker(tmp_path); p = tmp_path / "config.json"
    write(p, w.c); p.chmod(0o600)
    assert load_config(p) == w.c
    c = deepcopy(w.c); c["service"]["max_self_funded_cost_wei"] = "0"; write(p, c)
    with pytest.raises(ValueError, match="SERVICE_BUDGET"): load_config(p)
    c = deepcopy(w.c); c["heartbeat"]["lead_seconds"] = "300"; write(p, c)
    with pytest.raises(ValueError, match="HEARTBEAT_WINDOW"): load_config(p)
    w.inbox.close()
    c = deepcopy(w.c); c["heartbeat"]["interval_seconds"] = "600"
    with pytest.raises(ValueError, match="SCOPE_CHANGED"): Inbox(c)


def test_heartbeat_partial_progress_and_no_progress_keep_deadline(tmp_path, monkeypatch):
    w = worker(tmp_path)
    class Hash(bytes):
        pass
    blocks = {0:NS(number=0, timestamp=1000, hash=Hash(bytes(32))),
              1:NS(number=1, timestamp=1275, hash=Hash(bytes([1])*32)),
              2:NS(number=2, timestamp=1320, hash=Hash(bytes([2])*32))}
    w.w3 = NS(eth=NS(get_block=lambda n: blocks[n]))
    monkeypatch.setattr(heartbeat, "prove_creation", lambda *a: dict(block_number="0"))
    values = dict(version=1, fresh=True, useful_at=1260, has_useful=True)
    def call(name): return lambda: NS(call=lambda **k: values[name])
    state = NS(address=w.c["zk_state"], functions=NS(stateVersion=call("version"), isFresh=call("fresh"),
        lastUsefulAcceptedAt=call("useful_at"), hasUsefulUpdate=call("has_useful")))
    current = [None]*12; current[3]="100"; current[4]=f"{100:064x}"; current[5]="1260"
    core = dict(blocks=101, chainwork=f"{101:064x}")
    job = heartbeat.observe(w, blocks[1], state, current, core, True)
    assert job["deadline"] == 1360 and job["broadcast_not_before"] == 1300
    core.update(blocks=100, chainwork=f"{100:064x}"); values["fresh"]=False
    same = heartbeat.observe(w, blocks[2], state, current, core, True)
    assert same == job and w.inbox.get("heartbeat_status")["status"] == "NO_PROGRESS"
    assert w.inbox.get("heartbeat_anchors")[-1]["at"] == 1000
    values.update(fresh=True, useful_at=1310, version=2)
    assert heartbeat.observe(w, blocks[2], state, current, core, True) is None
    assert w.inbox.get("heartbeat_anchors")[-1]["at"] == 1310
    w.inbox.close()


def test_local_status_backoff_is_bounded_and_does_not_advance_anchor(tmp_path, monkeypatch):
    w=worker(tmp_path); w.inbox.put("heartbeat_job", dict(service_job_id="heartbeat:x", due=100, deadline=160))
    monkeypatch.setattr("time.time", lambda: 200)
    heartbeat.record_status(w, "NO_PROGRESS"); heartbeat.record_status(w, "NO_PROGRESS")
    key="heartbeat_notice:heartbeat:x:0"
    assert w.inbox.get(key)["attempts"] == 1
    monkeypatch.setattr("time.time", lambda: 210); heartbeat.record_status(w, "NO_PROGRESS")
    monkeypatch.setattr("time.time", lambda: 240); heartbeat.record_status(w, "NO_PROGRESS")
    assert w.inbox.get(key)["attempts"] == 2 and not w.inbox.get(key)["delivered"]
    assert w.inbox.get("heartbeat_job")["deadline"] == 160
    w.inbox.close()


def test_unpaid_consumer_cannot_veto_own_heartbeat(tmp_path, monkeypatch):
    w = make_worker(tmp_path); simulated(w, monkeypatch, tmp_path)
    w.c["service"] = dict(mode="paid"); w.c["heartbeat"] = dict(enabled=True)
    w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="11"*32, vout="0"))
    monkeypatch.setattr(heartbeat, "observe", lambda *a: dict(service_job_id="heartbeat:x", broadcast_not_before=1, deadline=1000))
    def paid_source(worker, ids):
        if any(not i.startswith("heartbeat:") for i in ids): raise ValueError("ZK_SERVICE_AWAITING_PAYMENT")
        return dict(source="CONFEDERATION", jobs=[])
    monkeypatch.setattr(service, "source", paid_source)
    monkeypatch.setattr(service, "preflight", paid_source)
    assert w.tick()["status"] == "PROOF_PREPARED"
    assert w.inbox.get("attempt")["need_ids"] == ["heartbeat:x"]
    assert w.inbox.needs()[0][2] == "AWAITING_SERVICE_PAYMENT"
    w.inbox.close()


def test_existing_fresh_root_does_not_require_second_service_payment(tmp_path, monkeypatch):
    w = make_worker(tmp_path); m = simulated(w, monkeypatch, tmp_path)
    key = w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="11"*32, vout="0"))
    assert w.tick()["status"] == "PROOF_PREPARED"
    assert w.tick()["status"] == "STATE_PROGRESS"
    w.c["service"] = dict(mode="paid")
    def source(worker, ids):
        if ids != ["maintenance:"+key]: raise ValueError("ZK_SERVICE_AWAITING_PAYMENT")
        return dict(source="CONFEDERATION", jobs=[])
    monkeypatch.setattr(service, "source", source)
    monkeypatch.setattr(service, "preflight", source)
    assert w.tick()["status"] == "HEADER_PROGRESS"
    assert m["proves"] == 1 and m["materialized"]
    w.inbox.close()
