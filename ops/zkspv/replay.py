"""Reproducible synthetic calendar replay; NEVER proof/gas/latency evidence."""
from pathlib import Path
import tempfile

from .scheduler import Head, Policy, Scheduler


def idle_replay(*, hours=84, days=30):
    if hours not in (1, 6, 24, 84) or type(days) is not int or not 1 <= days <= 180:
        raise ValueError("SUPPORTED_REPLAY_REQUIRED")
    policy = Policy("SIMULATION-confederation", "SIMULATION-state",
                    heartbeat_interval=hours*3600, lead=300)
    receipts = []
    with tempfile.TemporaryDirectory(prefix="zkspv-calendar-") as temporary:
        scheduler = Scheduler(Path(temporary) / "calendar.db", policy, 0, Head("0"*64, 0, 1, 0), "synthetic-activation")
        try:
            for height in range(1, days*144 + 1):
                now = height * 600
                head = Head(f"{height:064x}", height, height+1, now)
                scheduler.observe(head, scanned_from=height, at=now)
                job = scheduler.plan(now)
                if job and now >= job["broadcast_not_before"]:
                    # The synthetic zero-latency acceptance is deliberately
                    # separate from any real blockchain state reader.
                    scheduler.record_finalized_update(head, version=str(height), block_hash=f"SIMULATION-{height}",
                        accepted_at=now, finalized_at=now, target_ancestor_hash=job["target"]["hash"])
                    receipts.append({"service_job_id": job["service_job_id"], "reasons": job["reasons"],
                                     "planned_at": job["heartbeat_due"], "started_at": job["started_at"],
                                     "broadcast_at": now, "accepted_at": now, "finalized_at": now,
                                     "height": height, "lateness_seconds": 0,
                                     "transaction_receipt": None, "gas_used": None})
            state = scheduler.snapshot()
        finally:
            scheduler.close()
    return {"mode": "SIMULATION", "scenario": "healthy_wallet_idle", "days": days,
            "heartbeat_hours": hours, "H_scan": days*144, "H_prove": None, "K_real": None,
            "J_real": None, "M_real": None, "planned_state_updates": len(receipts),
            "modeled_zero_latency": True, "constant_600_second_blocks": True,
            "real_proof": False, "scan_completeness_of_real_bitcoin": "UNKNOWN",
            "missed_intervals": state["missed_intervals"], "status_slots": len(state["statuses"]),
            "updates": receipts}
