"""Canonical heartbeat scheduling for the executable worker.

Preparing proofs/statuses does not move the anchor. Only confirmed, Fresh state
covering Core's observed tip may satisfy a heartbeat. No empty transitions.
"""
import re
from web3 import Web3
from .contract_provenance import prove_creation
from .operations import canonical


def validate_heartbeat(config):
    h = config["heartbeat"]
    if (not isinstance(h, dict) or set(h) != {"enabled", "interval_seconds", "lead_seconds", "grace_seconds",
            "safety_margin_seconds", "finality_budget_seconds", "continuous_fresh", "max_status_attempts", "status_backoff_seconds"}
            or type(h["enabled"]) is not bool or type(h["continuous_fresh"]) is not bool):
        raise ValueError("ZK_HEARTBEAT_POLICY")
    for name, value in h.items():
        if isinstance(value, bool): continue
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > 2**32-1:
            raise ValueError("ZK_HEARTBEAT_POLICY")
    if (int(h["lead_seconds"]) >= int(h["interval_seconds"])
            or int(h["finality_budget_seconds"]) > int(h["grace_seconds"])
            or int(h["max_status_attempts"]) > 32):
        raise ValueError("ZK_HEARTBEAT_WINDOW")
    if h["continuous_fresh"] and sum(int(h[k]) for k in ("interval_seconds", "grace_seconds", "safety_margin_seconds")) > int(config["bitcoin_policy"]["zk_spv"]["max_state_age_seconds"]):
        raise ValueError("ZK_HEARTBEAT_FRESH_WINDOW")


def observe(worker, block, state, current, core, aligned):
    h = worker.c["heartbeat"]; box = worker.inbox; w3 = worker.w3
    from .zk_profiles import profile as zk_profile
    creation = prove_creation(w3, state.address, zk_profile(worker.c['bitcoin_policy'])['state'], None, block.number)
    birth = w3.eth.get_block(int(creation["block_number"]))
    initial = dict(at=birth.timestamp, number=birth.number, block_hash=birth.hash.hex(), version="0")
    anchors = box.get("heartbeat_anchors") or [initial]
    # Keep original interval on EVM rollback. Anchor evidence must still be
    # canonical; statuses and cost budgets are not erased by a rollback.
    canonical_anchors = [a for a in anchors if a["number"] <= block.number
                         and w3.eth.get_block(a["number"]).hash.hex() == a["block_hash"]]
    if not canonical_anchors: raise ValueError("ZK_HEARTBEAT_ANCHOR_RECOVERY_REQUIRED")
    anchor = canonical_anchors[-1]
    version = str(state.functions.stateVersion().call(block_identifier=block.number))
    fresh = state.functions.isFresh().call(block_identifier=block.number)
    useful_at = state.functions.lastUsefulAcceptedAt().call(block_identifier=block.number)
    # A state/root version change without useful canonical work never resets
    # a heartbeat, nor does publication of an ACK or a partial catch-up chunk.
    if aligned and fresh and int(current[3]) == core["blocks"] and useful_at > anchor["at"]:
        anchor = dict(at=useful_at, number=block.number, block_hash=block.hash.hex(), version=version)
        canonical_anchors.append(anchor)
        box.put("heartbeat_job", None)
    box.put("heartbeat_anchors", canonical_anchors)
    due = anchor["at"] + int(h["interval_seconds"])
    if not state.functions.hasUsefulUpdate().call(block_identifier=block.number):
        due = min(due, block.timestamp)  # Explicitly enabled worker may activate the initial checkpoint.
    if h["continuous_fresh"]:
        z = worker.c["bitcoin_policy"]["zk_spv"]
        fresh_due = min((useful_at or birth.timestamp) + int(z["max_state_age_seconds"]),
                        int(current[5]) + int(z["max_tip_lag_seconds"])) - int(h["safety_margin_seconds"])
        due = min(due, fresh_due)
    if block.timestamp < due - int(h["lead_seconds"]): return None
    job = box.get("heartbeat_job")
    anchor_key = Web3.keccak(text=canonical(anchor)).hex()
    if not job or job["anchor"] != anchor_key:
        job = dict(service_job_id="heartbeat:"+Web3.keccak(text=canonical([worker.c["zk_state"], anchor, due])).hex(),
                   anchor=anchor_key, started_at=block.timestamp, deadline=due+int(h["grace_seconds"]),
                   broadcast_not_before=due, due=due)
        box.put("heartbeat_job", job)
    box.put("heartbeat_status", dict(status="PREPARE" if int(core["chainwork"],16) > int(current[4],16) else "NO_PROGRESS",
        service_job_id=job["service_job_id"], deadline=job["deadline"], due=job["due"],
        missed=block.timestamp > job["deadline"]))
    return job


def record_status(worker, reason):
    """Durable, bounded local outbox; encrypted delivery is a separate adapter."""
    h = worker.c.get("heartbeat", {})
    job = worker.inbox.get("heartbeat_job")
    if not h.get("enabled") or not job: return
    now = int(__import__('time').time())
    if now < job["due"]: return
    slot = (now - job["due"]) // int(h["interval_seconds"])
    key = "heartbeat_notice:" + job["service_job_id"] + ":" + str(slot)
    old = worker.inbox.get(key) or dict(attempts=0, next_at=job["due"])
    if old["attempts"] >= int(h["max_status_attempts"]) or now < old["next_at"]: return
    count = old["attempts"]+1
    worker.inbox.put(key, dict(service_job_id=job["service_job_id"], reason=reason, deadline=job["deadline"],
        attempts=count, next_at=now+int(h["status_backoff_seconds"])*2**(count-1), delivered=False))
