"""Durable planning for §15; no consensus, proof verification or transaction sender.

Only a future qualified state-reader may feed canonical/finalized acceptances in
production. The replay CLI labels all its events SIMULATION. A worker lease is a
coordination fence, never an EOA/FROST nonce lock or an authorization certificate.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import sqlite3
from pathlib import Path

MAX_HEIGHT = (1 << 64) - 1
REASONS = {"DEPOSIT", "COINBASE", "WITHDRAWAL", "TREASURY", "SECURITY"}
STATUS_REASONS = {"NO_PROGRESS", "OBSERVATION_UNAVAILABLE", "PROVER_DELAY",
                  "QUORUM_UNAVAILABLE", "FEE_BUDGET", "HALTED"}


def identifier(domain, value):
    encoded = json.dumps([domain, value], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Policy:
    confederation: str
    state: str
    heartbeat_interval: int = 302400
    lead: int = 3600
    grace: int = 1800
    safety_margin: int = 300
    finality_budget: int = 600
    max_state_age: int = 7200
    max_tip_lag: int = 7200
    continuous_fresh: bool = False
    max_needs: int = 4096
    max_status_attempts: int = 3
    status_backoff: int = 60

    def __post_init__(self):
        if not self.confederation or not self.state:
            raise ValueError("SCOPE_REQUIRED")
        for field in ("heartbeat_interval", "lead", "grace", "safety_margin", "finality_budget",
                      "max_state_age", "max_tip_lag", "max_needs", "max_status_attempts", "status_backoff"):
            if type(getattr(self, field)) is not int or getattr(self, field) <= 0:
                raise ValueError("POSITIVE_POLICY_REQUIRED:" + field)
        if self.lead >= self.heartbeat_interval or self.finality_budget > self.grace:
            raise ValueError("HEARTBEAT_WINDOW_INVALID")
        if self.continuous_fresh and self.heartbeat_interval + self.grace + self.safety_margin > self.max_state_age:
            raise ValueError("CONTINUOUS_FRESH_POLICY_INCOMPATIBLE")


@dataclass(frozen=True)
class Head:
    hash: str
    height: int
    work: int
    timestamp: int

    def __post_init__(self):
        if len(self.hash) != 64 or any(c not in "0123456789abcdef" for c in self.hash):
            raise ValueError("INVALID_HEAD_HASH")
        if (type(self.height) is not int or not 0 <= self.height <= MAX_HEIGHT
                or type(self.work) is not int or not 0 < self.work < 1 << 256
                or type(self.timestamp) is not int or self.timestamp < 0):
            raise ValueError("INVALID_HEAD")


@dataclass(frozen=True)
class Need:
    operation: str
    reason: str
    inclusion_height: int
    consumer_depth: int
    deadline: int
    consumer: str
    inclusion_hash: str

    @property
    def required_height(self):
        # Contract deposit maturity and signer spend maturity are distinct.
        confirmations = 100 if self.reason == "COINBASE" else self.consumer_depth
        if self.reason == "SECURITY":
            return self.inclusion_height
        return self.inclusion_height + confirmations - 1

    def __post_init__(self):
        if self.reason not in REASONS or not self.operation or not self.consumer:
            raise ValueError("INVALID_NEED")
        if (type(self.consumer_depth) is not int or self.consumer_depth < 6
                or type(self.inclusion_height) is not int or self.inclusion_height < 0
                or type(self.deadline) is not int or self.deadline < 0
                or len(self.inclusion_hash) != 64
                or any(c not in "0123456789abcdef" for c in self.inclusion_hash)):
            raise ValueError("INVALID_CONSUMER_OR_INCLUSION")
        if self.required_height > MAX_HEIGHT:
            raise ValueError("REQUIRED_HEIGHT_OVERFLOW")


class Scheduler:
    def __init__(self, path: Path, policy: Policy, activation_at: int, initial_head: Head,
                 activation_block: str, initial_version: str = "activation"):
        if activation_at < 0 or not activation_block or not initial_version:
            raise ValueError("CANONICAL_ACTIVATION_REQUIRED")
        self.policy = policy
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS needs (id TEXT PRIMARY KEY, body TEXT NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS journal (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, job TEXT NOT NULL, body TEXT NOT NULL);
        """)
        with self._transaction() as state:
            if state is None:
                accepted = dict(head=asdict(initial_head), version=initial_version,
                                block=activation_block, accepted_at=activation_at,
                                last_useful_at=None, halted=False)
                state = {"policy": asdict(policy), "activation": accepted,
                         "published": accepted, "observed": asdict(initial_head),
                         "scan_through": initial_head.height, "observation_available": False,
                         "anchor": {"at": activation_at, "version": initial_version, "block": activation_block},
                         "history": [], "job": None, "generation": 0, "statuses": {},
                         "completions": {}, "orphaned_inclusions": [],
                         "h_scan": 0, "missed_intervals": 0}
                self._save(state)
            elif state["policy"] != asdict(policy) or state["activation"] != dict(
                    head=asdict(initial_head), version=initial_version, block=activation_block,
                    accepted_at=activation_at, last_useful_at=None, halted=False):
                raise ValueError("JOURNAL_SCOPE_OR_POLICY_MISMATCH")

    def close(self):
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else None
            yield state
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _save(self, state):
        self.db.execute("INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                        (json.dumps(state, sort_keys=True),))

    def _event(self, kind, data):
        self.db.execute("INSERT INTO journal(kind,body) VALUES(?,?)", (kind, json.dumps(data, sort_keys=True)))

    def snapshot(self):
        state = json.loads(self.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
        state["active_needs"] = [json.loads(row[0]) for row in self.db.execute("SELECT body FROM needs WHERE active=1 ORDER BY id")]
        state["revalidation_needs"] = [{"need": json.loads(row[0]), "reason": "BITCOIN_INCLUSION" if row[1] == -1 else "EVM_COMPLETION"}
                                       for row in self.db.execute("SELECT body,active FROM needs WHERE active<0 ORDER BY id")]
        state["attempts"] = [json.loads(row[0]) for row in self.db.execute("SELECT body FROM attempts ORDER BY rowid")]
        return state

    def _fresh(self, state, now):
        published = state["published"]
        useful = published["last_useful_at"]
        return (not published["halted"] and useful is not None and now >= published["accepted_at"]
                and now <= useful + self.policy.max_state_age
                and now <= published["head"]["timestamp"] + self.policy.max_tip_lag)

    def freshness(self, now):
        with self._transaction() as state:
            return self._fresh(state, now)

    def observe(self, head: Head, *, scanned_from: int, at: int, detected_needs=()):
        """Record a locally verified canonical observation and a contiguous scan.

        This must be fed by the existing Core/Bitcoin validation path, not an RPC
        notification alone. A reorg requires rewind_scan before scanning its branch.
        """
        with self._transaction() as state:
            if scanned_from != state["scan_through"] + 1 or head.height < scanned_from:
                raise ValueError("SCAN_GAP_OR_REORG_REQUIRES_RESCAN")
            state["h_scan"] += head.height - scanned_from + 1
            state["scan_through"] = head.height
            state["observed"] = asdict(head)
            state["observation_available"] = True
            for need in detected_needs:
                self._add_need(need)
            self._event("observation", {"head": asdict(head), "scanned_from": scanned_from, "at": at})
            self._save(state)

    def observation_unavailable(self, reason):
        with self._transaction() as state:
            state["observation_available"] = False
            self._event("observation_unavailable", {"reason": str(reason)})
            self._save(state)

    def reconcile_observation(self, head: Head):
        """A successful full-node reconciliation may establish no new progress."""
        with self._transaction() as state:
            if asdict(head) != state["observed"] or head.height != state["scan_through"]:
                raise ValueError("RECONCILIATION_REQUIRES_COMPLETE_SAME_HEAD")
            state["observation_available"] = True
            self._save(state)

    def add_need(self, need: Need):
        """Input has already passed application/transport authorization upstream."""
        with self._transaction() as state:
            added = self._add_need(need)
            self._save(state)
            return added

    def _add_need(self, need):
        data = json.dumps(asdict(need), sort_keys=True)
        existing = self.db.execute("SELECT body FROM needs WHERE id=?", (need.operation,)).fetchone()
        if existing:
            if existing[0] != data:
                raise ValueError("NEED_ID_CONFLICT")
            return False
        if self.db.execute("SELECT COUNT(*) FROM needs WHERE active!=0").fetchone()[0] >= self.policy.max_needs:
            raise ValueError("NEED_ADMISSION_LIMIT_RETRY_WITHOUT_DROPPING_EVENT")
        self.db.execute("INSERT INTO needs VALUES(?,?,1)", (need.operation, data))
        self._event("need", asdict(need))
        return True

    def complete_need(self, operation, *, application_receipt):
        if (not isinstance(application_receipt, dict)
                or not application_receipt.get("transaction_hash") or not application_receipt.get("block_hash")
                or type(application_receipt.get("block_number")) is not int or application_receipt["block_number"] < 0):
            raise ValueError("APPLICATION_COMPLETION_EVIDENCE_REQUIRED")
        with self._transaction() as state:
            if not self.db.execute("UPDATE needs SET active=0 WHERE id=? AND active=1", (operation,)).rowcount:
                raise ValueError("UNKNOWN_OR_COMPLETED_NEED")
            state["completions"][operation] = {"receipt": application_receipt, "status": "CANONICAL"}
            self._event("completion", {"operation": operation, "receipt": application_receipt})
            self._save(state)

    def revalidate_completion(self, operation, *, canonical: bool):
        """Use a freshly authenticated canonical receipt read after an EVM reorg.

        Reopening queues verification of the original obligation, never a refund
        or automatic second mint. Monetary contracts still enforce exact results.
        """
        if type(canonical) is not bool:
            raise ValueError("CANONICAL_RECEIPT_RESULT_REQUIRED")
        with self._transaction() as state:
            record = state["completions"].get(operation)
            if not record or record["status"] != "REVALIDATION_REQUIRED":
                raise ValueError("NO_PENDING_COMPLETION_REVALIDATION")
            next_state = 0 if canonical else (-1 if operation in state["orphaned_inclusions"] else 1)
            if not self.db.execute("UPDATE needs SET active=? WHERE id=? AND active=-2",
                                   (next_state, operation)).rowcount:
                raise ValueError("COMPLETION_STATE_CONFLICT")
            record["status"] = "CANONICAL" if canonical else "ORPHANED"
            self._event("completion_revalidation", {"operation": operation, "canonical": canonical})
            self._save(state)

    def rewind_scan(self, lca_height: int, orphaned_operations: list[str]):
        """Invalidate local inclusion, preserving obligations and event evidence."""
        with self._transaction() as state:
            if not 0 <= lca_height <= state["scan_through"]:
                raise ValueError("INVALID_SCAN_REWIND")
            for operation in orphaned_operations:
                # -1 denotes revalidation pending, NOT cancellation or refund.
                self.db.execute("UPDATE needs SET active=-1 WHERE id=? AND active=1", (operation,))
            state["orphaned_inclusions"] = sorted(set(state["orphaned_inclusions"]) | set(orphaned_operations))
            state["scan_through"] = lca_height
            state["observation_available"] = False
            state["job"] = None
            state["generation"] += 1
            self._event("scan_reorg", {"lca_height": lca_height, "orphaned": orphaned_operations})
            self._save(state)

    def reinclude_need(self, need: Need):
        with self._transaction() as state:
            row = self.db.execute("SELECT body,active FROM needs WHERE id=?", (need.operation,)).fetchone()
            if not row or row[1] not in (-1, -2) or need.operation not in state["orphaned_inclusions"]:
                raise ValueError("REINCLUSION_REQUIRES_ORPHANED_NEED")
            prior = json.loads(row[0])
            candidate = asdict(need)
            for key in ("operation", "reason", "consumer_depth", "consumer", "deadline"):
                if prior[key] != candidate[key]:
                    raise ValueError("REINCLUSION_CHANGED_OBLIGATION")
            self.db.execute("UPDATE needs SET body=?,active=? WHERE id=?", (json.dumps(candidate, sort_keys=True), -2 if row[1] == -2 else 1, need.operation))
            state["orphaned_inclusions"].remove(need.operation)
            self._event("reinclude", candidate)
            self._save(state)

    def _due(self, state):
        return state["anchor"]["at"] + self.policy.heartbeat_interval

    def plan(self, now: int):
        with self._transaction() as state:
            if state["published"]["halted"] or not state["observation_available"]:
                return None
            observed, published = state["observed"], state["published"]
            if observed["work"] <= published["head"]["work"]:
                return None
            due = self._due(state)
            needs = [Need(**json.loads(row[0])) for row in self.db.execute("SELECT body FROM needs WHERE active=1 ORDER BY id")]
            ready = [n for n in needs if n.required_height <= observed["height"] and
                     (n.reason == "SECURITY" or not self._fresh(state, now) or
                      n.required_height > published["head"]["height"])]
            reasons = {n.reason for n in ready}
            if now >= due - self.policy.lead:
                reasons.add("HEARTBEAT")
            fresh_due = None
            if self.policy.continuous_fresh:
                useful = published["last_useful_at"]
                fresh_due = min((useful or state["activation"]["accepted_at"]) + self.policy.max_state_age,
                                published["head"]["timestamp"] + self.policy.max_tip_lag) - self.policy.safety_margin
                if now >= fresh_due - self.policy.lead:
                    reasons.add("FRESHNESS")
            if not reasons:
                return None
            active = state["job"]
            required = max((n.required_height for n in ready), default=0)
            if (active and active["target"] != observed and
                    (required > active["target"]["height"]
                     or active["target"]["work"] <= published["head"]["work"]
                     or now + self.policy.finality_budget > active["target"]["timestamp"] + self.policy.max_tip_lag)):
                # Stable service job survives re-preparation; attempts get a new
                # target digest. The paid job must never be billed per attempt.
                active["target"] = observed
                active["target_revision"] += 1
                active["lease"] = None
            if active is None:
                job_id = identifier("satoshifi.service-job.v1", [self.policy.confederation, self.policy.state,
                    state["anchor"], published["version"], state["generation"], sorted(n.operation for n in ready)])
                active = {"service_job_id": job_id, "base_version": published["version"],
                          "target": observed, "target_revision": 0, "started_at": now,
                          "heartbeat_due": due, "reasons": [], "operations": [], "lease": None,
                          "lease_generation": 0}
            active["reasons"] = sorted(set(active["reasons"]) | reasons)
            active["operations"] = sorted(set(active["operations"]) | {n.operation for n in ready})
            urgent = bool(set(active["reasons"]) - {"HEARTBEAT", "FRESHNESS"})
            proposed_not_before = now if urgent else min(due, fresh_due if "FRESHNESS" in reasons else due)
            active["broadcast_not_before"] = min(active.get("broadcast_not_before", proposed_not_before), proposed_not_before)
            deadlines = [due + self.policy.grace] if "HEARTBEAT" in active["reasons"] else []
            deadlines.extend(n.deadline for n in ready)
            if "FRESHNESS" in active["reasons"]:
                deadlines.append(fresh_due)
            active["deadline"] = min([active["deadline"]] + deadlines) if "deadline" in active else min(deadlines)
            state["job"] = active
            self._save(state)
            return active

    def acquire(self, job_id, worker, now, lease_seconds=60):
        if not worker or lease_seconds <= 0:
            raise ValueError("INVALID_LEASE")
        with self._transaction() as state:
            job = state["job"]
            if not job or job["service_job_id"] != job_id:
                raise ValueError("STALE_JOB")
            lease = job["lease"]
            if lease and lease["until"] > now:
                return None
            job["lease_generation"] += 1
            job["lease"] = {"worker": worker, "until": now + lease_seconds, "fence": job["lease_generation"]}
            self._save(state)
            return job["lease"]["fence"]

    def begin_attempt(self, job_id, worker, fence, now, program_id, input_digest):
        if not program_id or not input_digest:
            raise ValueError("ATTEMPT_BINDING_REQUIRED")
        with self._transaction() as state:
            job = state["job"]
            lease = job["lease"] if job else None
            if (not job or job["service_job_id"] != job_id or not lease or lease["worker"] != worker
                    or lease["fence"] != fence or now >= lease["until"]):
                raise ValueError("WORKER_FENCED")
            binding = {"service_job_id": job_id, "base_version": job["base_version"],
                       "target": job["target"], "target_revision": job["target_revision"],
                       "program_id": program_id, "input_digest": input_digest}
            attempt_id = identifier("satoshifi.proving-attempt.v1", binding)
            binding["proving_attempt_id"] = attempt_id
            self.db.execute("INSERT OR IGNORE INTO attempts VALUES(?,?,?)", (attempt_id, job_id, json.dumps(binding, sort_keys=True)))
            self._save(state)
            return binding

    def record_finalized_update(self, head: Head, *, version: str, block_hash: str,
                                accepted_at: int, finalized_at: int, halted=False,
                                target_ancestor_hash=None):
        """Record facts AFTER canonical EVM/proof/certificate/finality verification.

        This planner cannot supply that verification. Never call this from a raw
        receipt/ACK without a qualified state reader. A simulation is not evidence.
        """
        if not version or not block_hash or accepted_at < 0 or finalized_at < accepted_at:
            raise ValueError("INVALID_FINALIZED_EVENT")
        with self._transaction() as state:
            prior = state["published"]
            if prior["halted"]:
                raise ValueError("PERMANENT_HALT")
            if any(item["published"]["version"] == version for item in state["history"]) or version == prior["version"]:
                raise ValueError("REPLAYED_STATE")
            if accepted_at < prior["accepted_at"]:
                raise ValueError("NONMONOTONIC_EVM_TIME")
            progress = head.work > prior["head"]["work"] and head.hash != prior["head"]["hash"]
            if not progress and not halted:
                raise ValueError("NO_USEFUL_PROGRESS")
            accepted = {"head": asdict(head), "version": version, "block": block_hash,
                        "accepted_at": accepted_at, "last_useful_at": accepted_at if progress else prior["last_useful_at"],
                        "halted": bool(halted)}
            state["published"] = accepted
            job = state["job"]
            target = job["target"] if job else state["observed"]
            # Chainwork handles shorter/heavier; canonicality/ancestry must be
            # checked by the state-reader. Height alone never proves branch match.
            target_met = (head.work >= target["work"] and
                          (head.hash == target["hash"] or target_ancestor_hash == target["hash"]))
            if target_met and self._fresh(state, finalized_at) and progress:
                due = self._due(state)
                missed = max(0, (finalized_at - due - self.policy.grace + self.policy.heartbeat_interval - 1)
                             // self.policy.heartbeat_interval)
                state["missed_intervals"] += missed
                state["anchor"] = {"at": accepted_at, "version": version, "block": block_hash}
                state["job"] = None
            elif job:
                # A partial useful transition invalidates the old proof attempt
                # but does not move the original heartbeat anchor/deadline.
                job["base_version"] = version
                job["lease"] = None
                job["target_revision"] += 1
            if halted:
                state["job"] = None
            state["generation"] += 1
            state["history"].append({"published": accepted, "anchor": dict(state["anchor"]),
                                     "missed_intervals": state["missed_intervals"]})
            self._event("finalized_state", {"head": asdict(head), "version": version,
                        "accepted_at": accepted_at, "finalized_at": finalized_at, "halted": halted})
            self._save(state)

    def evm_rewind(self, canonical_block_hash):
        with self._transaction() as state:
            candidates = [i for i, item in enumerate(state["history"]) if item["published"]["block"] == canonical_block_hash]
            if candidates:
                i = candidates[-1]
                item = state["history"][i]
                state["published"], state["anchor"] = item["published"], item["anchor"]
                state["missed_intervals"] = item["missed_intervals"]
                state["history"] = state["history"][:i+1]
            elif canonical_block_hash == state["activation"]["block"]:
                state["published"] = dict(state["activation"])
                state["anchor"] = {"at": state["activation"]["accepted_at"], "version": state["activation"]["version"], "block": canonical_block_hash}
                state["history"] = []
                state["missed_intervals"] = 0
            else:
                raise ValueError("CANONICAL_ANCHOR_UNAVAILABLE_RECOVERY_REQUIRED")
            state["job"] = None
            # Keep status delivery records: an EVM reorg is not a fresh budget.
            # A SPV state anchor alone cannot establish that independent monetary
            # receipts survived. Conservatively revalidate every completed need.
            self.db.execute("UPDATE needs SET active=-2 WHERE active=0")
            for completion in state["completions"].values():
                if completion["status"] == "CANONICAL":
                    completion["status"] = "REVALIDATION_REQUIRED"
            state["generation"] += 1
            self._event("evm_rewind", {"canonical_block": canonical_block_hash})
            self._save(state)

    def status_slot(self, now, reason, *, evm_available=True, completion_impossible=False):
        """Reserve a bounded status attempt; transport/encryption are NOT supplied."""
        if reason not in STATUS_REASONS:
            raise ValueError("UNKNOWN_STATUS_REASON")
        with self._transaction() as state:
            due = self._due(state)
            if now < due:
                return None
            if (reason in {"PROVER_DELAY", "QUORUM_UNAVAILABLE", "FEE_BUDGET"}
                    and now <= due + self.policy.grace and not completion_impossible):
                return None
            if reason == "HALTED" and not state["published"]["halted"]:
                raise ValueError("STATUS_IS_NOT_HALT_EVIDENCE")
            if reason == "OBSERVATION_UNAVAILABLE" and state["observation_available"]:
                raise ValueError("OBSERVATION_AVAILABLE")
            if reason == "NO_PROGRESS" and (not state["observation_available"] or state["observed"]["work"] > state["published"]["head"]["work"]):
                raise ValueError("NO_PROGRESS_NOT_ESTABLISHED")
            slot_number = (now - due) // self.policy.heartbeat_interval
            slot_id = identifier("satoshifi.heartbeat-status.v1", [self.policy.confederation, self.policy.state, state["anchor"], slot_number])
            slot = state["statuses"].get(slot_id, {"id": slot_id, "slot": slot_number,
                "anchor": dict(state["anchor"]), "reason": reason, "attempts": 0,
                "delivered": False, "next_attempt_at": due + slot_number * self.policy.heartbeat_interval})
            if slot["delivered"] or slot["attempts"] >= self.policy.max_status_attempts or now < slot["next_attempt_at"]:
                return None
            slot["reason"] = reason
            slot["evm_available"] = evm_available
            if evm_available:
                slot["attempts"] += 1
                slot["next_attempt_at"] = now + self.policy.status_backoff * (2 ** (slot["attempts"]-1))
            state["statuses"][slot_id] = slot
            self._save(state)
            return slot

    def status_delivered(self, slot_id, receipt):
        if not receipt:
            raise ValueError("STATUS_RECEIPT_REQUIRED")
        with self._transaction() as state:
            slot = state["statuses"].get(slot_id)
            if not slot or slot["attempts"] == 0:
                raise ValueError("UNKNOWN_STATUS_ATTEMPT")
            slot["delivered"] = True
            slot["receipt"] = receipt
            self._save(state)
