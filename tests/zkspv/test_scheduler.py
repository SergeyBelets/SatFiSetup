"""Calendar/state-machine simulations, explicitly not Bitcoin or ZK proofs."""
from dataclasses import replace
import pytest

from ops.zkspv.scheduler import Head, Need, Policy, Scheduler, MAX_HEIGHT


def head(height, at, *, work=None, branch=0):
    return Head(f"{height + branch * 1000000:064x}", height, work or height + 1, at)


def open_scheduler(path, policy=None):
    return Scheduler(path, policy or Policy("confederation", "state"), 0, head(0, 0), "activation")


@pytest.fixture
def scheduler(tmp_path):
    obj = open_scheduler(tmp_path / "planner.db")
    yield obj
    obj.close()


def accept(s, h, now, version=None, **kwargs):
    s.record_finalized_update(h, version=version or str(h.height), block_hash="evm-" + str(h.height),
                             accepted_at=now, finalized_at=now, **kwargs)


@pytest.mark.parametrize("hours,expected", [(1, 720), (6, 120), (24, 30), (84, 8)])
def test_30_day_idle_has_useful_heartbeats_without_per_block_updates(tmp_path, hours, expected):
    policy = Policy("confederation", "state", heartbeat_interval=hours*3600, lead=300)
    s = open_scheduler(tmp_path / "idle.db", policy)
    published = 0
    for height in range(1, 4321):
        now = height * 600
        h = head(height, now)
        s.observe(h, scanned_from=height, at=now)
        plan = s.plan(now)
        if plan and now >= plan["broadcast_not_before"]:
            target = plan["target"]["hash"]
            accept(s, h, now, target_ancestor_hash=target)
            assert s.freshness(now)
            published += 1
    assert published == expected
    assert s.snapshot()["h_scan"] == 4320
    assert s.snapshot()["statuses"] == {}
    s.close()


def test_early_preparation_pins_target_and_does_not_shorten_period(scheduler):
    due = scheduler.policy.heartbeat_interval
    scheduler.observe(head(500, due-3500), scanned_from=1, at=due-3500)
    job = scheduler.plan(due-3500)
    scheduler.observe(head(501, due-3000), scanned_from=501, at=due-3000)
    updated = scheduler.plan(due-3000)
    assert job["service_job_id"] == updated["service_job_id"]
    assert updated["target"] == job["target"]
    assert updated["broadcast_not_before"] == due


def test_stale_idle_does_not_publish_or_emit_status_but_payment_wakes(scheduler):
    scheduler.observe(head(1, 600), scanned_from=1, at=600)
    accept(scheduler, head(1, 600), 600)
    scheduler.observe(head(100, 60000), scanned_from=2, at=60000)
    assert not scheduler.freshness(60000)
    assert scheduler.plan(60000) is None
    assert scheduler.status_slot(60000, "PROVER_DELAY") is None
    scheduler.add_need(Need("late", "DEPOSIT", 1, 12, 61000, "registry", head(1, 600).hash))
    plan = scheduler.plan(60000)
    assert plan["broadcast_not_before"] == 60000
    assert plan["deadline"] == 61000
    assert plan["reasons"] == ["DEPOSIT"]


@pytest.mark.parametrize("reason,depth,required", [("DEPOSIT", 12, 111), ("WITHDRAWAL", 18, 117),
                                                   ("COINBASE", 150, 199), ("COINBASE", 6, 199)])
def test_exact_consumer_maturity_without_new_payment(scheduler, reason, depth, required):
    need = Need("one", reason, 100, depth, 500000, "consumer", head(100, 0).hash)
    scheduler.add_need(need)
    scheduler.observe(head(required-1, 600), scanned_from=1, at=600)
    assert scheduler.plan(600) is None
    scheduler.observe(head(required, 1200), scanned_from=required, at=1200)
    assert scheduler.plan(1200)["operations"] == ["one"]


def test_maturity_overflow_rejected():
    with pytest.raises(ValueError, match="OVERFLOW"):
        Need("one", "DEPOSIT", MAX_HEIGHT, 6, 100, "consumer", "0"*64)


def test_joint_heartbeat_event_and_competing_workers(tmp_path):
    path = tmp_path / "shared.db"
    a, b = open_scheduler(path), open_scheduler(path)
    due = a.policy.heartbeat_interval
    h = head(500, due-1000)
    a.observe(h, scanned_from=1, at=due-1000)
    first = a.plan(due-1000)
    b.add_need(Need("payment", "DEPOSIT", 400, 6, due-500, "registry", head(400, 0).hash))
    job = b.plan(due-900)
    assert job["service_job_id"] == first["service_job_id"]
    assert job["reasons"] == ["DEPOSIT", "HEARTBEAT"]
    fence = a.acquire(job["service_job_id"], "a", due-900)
    assert b.acquire(job["service_job_id"], "b", due-899) is None
    attempt = a.begin_attempt(job["service_job_id"], "a", fence, due-899, "program", "inputs")
    assert attempt["service_job_id"] == job["service_job_id"]
    fence2 = b.acquire(job["service_job_id"], "b", due-839)
    with pytest.raises(ValueError, match="FENCED"):
        a.begin_attempt(job["service_job_id"], "a", fence, due-838, "program", "inputs")
    assert fence2 > fence
    accept(b, h, due-800)
    assert a.plan(due) is None
    assert len(a.snapshot()["attempts"]) == 1
    a.close()
    b.close()


def test_restart_does_not_move_anchor_and_policy_cannot_silently_change(tmp_path):
    path = tmp_path / "restart.db"
    a = open_scheduler(path)
    due = a.policy.heartbeat_interval
    a.observe(head(500, due), scanned_from=1, at=due)
    plan = a.plan(due)
    a.close()
    b = open_scheduler(path)
    assert b.plan(due+100)["service_job_id"] == plan["service_job_id"]
    assert b.snapshot()["anchor"]["at"] == 0
    with pytest.raises(ValueError, match="MISMATCH"):
        open_scheduler(path, replace(b.policy, heartbeat_interval=86400))
    b.close()


def test_partial_catchup_does_not_reset_deadline(scheduler):
    due = scheduler.policy.heartbeat_interval
    scheduler.observe(head(500, due), scanned_from=1, at=due)
    original = scheduler.plan(due)
    accept(scheduler, head(200, due-20000), due+10)
    assert not scheduler.freshness(due+10)
    plan = scheduler.plan(due+10)
    assert scheduler.snapshot()["anchor"]["at"] == 0
    assert plan["deadline"] == original["deadline"]
    assert plan["service_job_id"] == original["service_job_id"]
    assert plan["base_version"] == "200"
    accept(scheduler, head(500, due), due+20)
    assert scheduler.snapshot()["anchor"]["at"] == due+20


def test_side_branch_work_alone_cannot_satisfy_pinned_target(scheduler):
    due = scheduler.policy.heartbeat_interval
    scheduler.observe(head(500, due), scanned_from=1, at=due)
    scheduler.plan(due)
    accept(scheduler, head(510, due, branch=2), due+1)
    assert scheduler.snapshot()["anchor"]["at"] == 0


def test_noop_and_replay_do_not_refresh_state(scheduler):
    with pytest.raises(ValueError, match="NO_USEFUL"):
        accept(scheduler, head(0, 0), 500, version="fake")
    scheduler.observe(head(1, 500), scanned_from=1, at=500)
    accept(scheduler, head(1, 500), 500)
    with pytest.raises(ValueError, match="REPLAYED"):
        accept(scheduler, head(2, 600), 600, version="1")
    assert scheduler.snapshot()["published"]["last_useful_at"] == 500


def test_two_freshness_bounds_are_inclusive(scheduler):
    scheduler.observe(head(1, 100), scanned_from=1, at=200)
    accept(scheduler, head(1, 100), 200)
    assert scheduler.freshness(7300)
    assert not scheduler.freshness(7301)
    assert not scheduler.freshness(199)


def test_status_backoff_budget_durable_and_not_proof(scheduler):
    due = scheduler.policy.heartbeat_interval
    assert scheduler.status_slot(due-1, "OBSERVATION_UNAVAILABLE") is None
    slot = scheduler.status_slot(due, "OBSERVATION_UNAVAILABLE")
    assert scheduler.status_slot(due+1, "OBSERVATION_UNAVAILABLE") is None
    assert scheduler.snapshot()["anchor"]["at"] == 0
    scheduler.status_slot(due+60, "OBSERVATION_UNAVAILABLE")
    scheduler.status_slot(due+180, "OBSERVATION_UNAVAILABLE")
    assert scheduler.status_slot(due+10000, "OBSERVATION_UNAVAILABLE") is None
    scheduler.status_delivered(slot["id"], "status-receipt")
    assert not scheduler.freshness(due+180)


def test_no_status_for_normal_inclusion_finality_inside_grace(scheduler):
    due = scheduler.policy.heartbeat_interval
    assert scheduler.status_slot(due+1, "PROVER_DELAY") is None
    assert scheduler.status_slot(due+1801, "PROVER_DELAY") is not None


def test_outage_emits_current_slot_only_and_retains_original_deadline(scheduler):
    due = scheduler.policy.heartbeat_interval
    offline = scheduler.status_slot(due, "OBSERVATION_UNAVAILABLE", evm_available=False)
    assert offline["attempts"] == 0
    recovered = scheduler.status_slot(due*20, "OBSERVATION_UNAVAILABLE")
    assert recovered["slot"] == 19
    assert len(scheduler.snapshot()["statuses"]) == 2
    scheduler.observe(head(9000, due*20), scanned_from=1, at=due*20)
    assert scheduler.plan(due*20)["heartbeat_due"] == due
    accept(scheduler, head(9000, due*20), due*20)
    assert scheduler.snapshot()["missed_intervals"] == 19
    assert scheduler.plan(due*20+1) is None


def test_evm_reorg_restores_canonical_anchor(scheduler):
    scheduler.observe(head(1, 600), scanned_from=1, at=600)
    accept(scheduler, head(1, 600), 600)
    scheduler.observe(head(2, 1200), scanned_from=2, at=1200)
    accept(scheduler, head(2, 1200), 1200)
    scheduler.evm_rewind("evm-1")
    assert scheduler.snapshot()["anchor"]["at"] == 600
    assert scheduler.snapshot()["published"]["version"] == "1"
    with pytest.raises(ValueError, match="RECOVERY_REQUIRED"):
        scheduler.evm_rewind("unknown")


def test_observation_and_detected_need_commit_atomically(tmp_path):
    s = open_scheduler(tmp_path / "atomic.db", Policy("c", "s", max_needs=1))
    n = Need("a", "DEPOSIT", 1, 6, 1000, "registry", head(1, 0).hash)
    with pytest.raises(ValueError, match="ADMISSION_LIMIT"):
        s.observe(head(10, 500), scanned_from=1, at=500, detected_needs=[n, replace(n, operation="b")])
    assert s.snapshot()["scan_through"] == 0
    assert s.snapshot()["active_needs"] == []
    s.observe(head(10, 500), scanned_from=1, at=500, detected_needs=[n])
    assert s.snapshot()["scan_through"] == 10
    assert len(s.snapshot()["active_needs"]) == 1
    s.close()


def test_scan_gap_cannot_be_treated_as_idle(scheduler):
    with pytest.raises(ValueError, match="SCAN_GAP"):
        scheduler.observe(head(100, 500), scanned_from=99, at=500)
    assert scheduler.snapshot()["scan_through"] == 0
    assert scheduler.plan(400000) is None


def test_bitcoin_reorg_revalidates_inclusion_without_cancelling_obligation(scheduler):
    n = Need("a", "DEPOSIT", 5, 6, 10000, "registry", head(5, 0).hash)
    scheduler.observe(head(10, 500), scanned_from=1, at=500, detected_needs=[n])
    scheduler.rewind_scan(3, ["a"])
    assert scheduler.plan(600) is None
    with pytest.raises(ValueError, match="CHANGED_OBLIGATION"):
        scheduler.reinclude_need(replace(n, consumer_depth=7))
    scheduler.reinclude_need(replace(n, inclusion_height=8, inclusion_hash=head(8, 0, branch=1).hash))
    scheduler.observe(head(12, 600, branch=1), scanned_from=4, at=600)
    assert scheduler.plan(600) is None
    scheduler.observe(head(13, 1200, branch=1), scanned_from=13, at=1200)
    assert scheduler.plan(1200)["operations"] == ["a"]


def test_permanent_halt_closes_updates_but_not_obligations(scheduler):
    scheduler.add_need(Need("a", "DEPOSIT", 1, 6, 10000, "registry", head(1, 0).hash))
    accept(scheduler, head(0, 0), 100, version="halt", halted=True)
    assert scheduler.plan(400000) is None
    assert scheduler.status_slot(400000, "HALTED") is not None
    with pytest.raises(ValueError, match="PERMANENT_HALT"):
        accept(scheduler, head(1, 200), 200)
    assert len(scheduler.snapshot()["active_needs"]) == 1


def test_continuous_fresh_incompatible_profile_is_rejected():
    with pytest.raises(ValueError, match="INCOMPATIBLE"):
        Policy("c", "s", continuous_fresh=True)


def test_admission_deduplicates_without_reviving_completed_operations(scheduler):
    n = Need("a", "DEPOSIT", 1, 6, 1000, "registry", head(1, 0).hash)
    assert scheduler.add_need(n)
    assert not scheduler.add_need(n)
    scheduler.complete_need("a", application_receipt={"transaction_hash": "minted exact allocation", "block_hash": "evm-mint", "block_number": 100})
    assert not scheduler.add_need(n)
    assert not scheduler.snapshot()["active_needs"]
    with pytest.raises(ValueError, match="CONFLICT"):
        scheduler.add_need(replace(n, consumer="attacker"))


@pytest.mark.parametrize("canonical", [False, True])
def test_evm_reorg_cannot_drop_completed_application_obligation(scheduler, canonical):
    n = Need("a", "DEPOSIT", 1, 6, 10000, "registry", head(1, 0).hash)
    scheduler.add_need(n)
    scheduler.complete_need("a", application_receipt={"transaction_hash": "mint", "block_hash": "evm-mint", "block_number": 100})
    scheduler.evm_rewind("activation")
    assert scheduler.snapshot()["revalidation_needs"][0]["reason"] == "EVM_COMPLETION"
    assert not scheduler.add_need(n)  # duplicate notification cannot bypass revalidation
    scheduler.revalidate_completion("a", canonical=canonical)
    assert len(scheduler.snapshot()["active_needs"]) == (0 if canonical else 1)
    assert scheduler.snapshot()["revalidation_needs"] == []


def test_partial_stale_target_is_refreshed_without_rebilling_or_moving_deadline(scheduler):
    due = scheduler.policy.heartbeat_interval
    scheduler.observe(head(500, due-3500), scanned_from=1, at=due-3500)
    original = scheduler.plan(due-3500)
    accept(scheduler, head(500, due-3500), due+10000)
    scheduler.observe(head(600, due+10001), scanned_from=501, at=due+10001)
    refreshed = scheduler.plan(due+10001)
    assert refreshed["target"]["height"] == 600
    assert refreshed["service_job_id"] == original["service_job_id"]
    assert refreshed["deadline"] == original["deadline"]
    assert refreshed["base_version"] == "500"
    accept(scheduler, head(600, due+10001), due+10002)
    assert scheduler.snapshot()["anchor"]["at"] == due+10002


@pytest.mark.parametrize("bitcoin_first", [False, True])
def test_both_chains_reorg_cannot_restore_orphaned_inclusion(scheduler, bitcoin_first):
    n = Need("a", "DEPOSIT", 5, 6, 10000, "registry", head(5, 0).hash)
    scheduler.observe(head(10, 500), scanned_from=1, at=500, detected_needs=[n])
    scheduler.complete_need("a", application_receipt={"transaction_hash": "mint", "block_hash": "evm-mint", "block_number": 100})
    if bitcoin_first:
        scheduler.rewind_scan(3, ["a"])
        scheduler.evm_rewind("activation")
    else:
        scheduler.evm_rewind("activation")
        scheduler.rewind_scan(3, ["a"])
    scheduler.revalidate_completion("a", canonical=False)
    assert scheduler.snapshot()["revalidation_needs"][0]["reason"] == "BITCOIN_INCLUSION"
    scheduler.observe(head(12, 600, branch=1), scanned_from=4, at=600)
    assert scheduler.plan(600) is None
    scheduler.reinclude_need(replace(n, inclusion_height=6, inclusion_hash=head(6, 0, branch=1).hash))
    assert scheduler.plan(600)["operations"] == ["a"]
