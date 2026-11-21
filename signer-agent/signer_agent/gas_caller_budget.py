"""Durable local spending reservations for the permissionless gas caller.

The caller lock covers a whole tick. The shared identity lock separately covers
EOA nonce ownership. Neither journal is an on-chain spending restriction.
"""
import json
import re

from .operations import Journal


FIELDS = {
    "period_seconds": (60, 31_536_000),
    "max_period_cost_wei": (1, 2**256 - 1),
    "max_transactions_per_period": (1, 1024),
    "max_attempts_per_payee_epoch": (1, 1024),
    "retry_backoff_seconds": (1, 31_536_000),
    "gas_limit": (21_000, 2**24),
    "max_fee_per_gas_wei": (1, 2**256 - 1),
    "max_priority_fee_per_gas_wei": (0, 2**256 - 1),
    "min_caller_balance_wei": (0, 2**256 - 1),
    "min_claim_to_fee_ratio": (1, 1_000_000),
}


def uint(value, minimum=0, maximum=2**256 - 1):
    if (not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,77}", value)
            or not minimum <= int(value) <= maximum):
        raise ValueError("GAS_CALLER_POLICY")
    return int(value)


def validate_budget(policy):
    if not isinstance(policy, dict) or set(policy) != set(FIELDS):
        raise ValueError("GAS_CALLER_POLICY")
    result = {key: uint(policy[key], *limits) for key, limits in FIELDS.items()}
    if (result["max_priority_fee_per_gas_wei"] > result["max_fee_per_gas_wei"]
            or result["gas_limit"] * result["max_fee_per_gas_wei"] > result["max_period_cost_wei"]):
        raise ValueError("GAS_CALLER_POLICY")
    return result


class CallerBudget:
    def __init__(self, path, policy, chain_id, caller, confirmations):
        self.policy = validate_budget(policy)
        self.maximum = self.policy["gas_limit"] * self.policy["max_fee_per_gas_wei"]
        self.journal = Journal(path)
        # Changing routes/epochs does not create a fresh policy or budget.
        self.journal.put("policy", dict(budget=policy, chain_id=chain_id,
                                       caller=caller, confirmations=confirmations))
        self.journal.put("clock", {})
        with self.journal.db() as db:
            db.executescript("""CREATE INDEX IF NOT EXISTS gas_attempt_status ON operations(status);
                CREATE INDEX IF NOT EXISTS gas_attempt_pair ON operations(json_extract(request, '$.pair'));
                CREATE INDEX IF NOT EXISTS gas_attempt_period ON operations(json_extract(request, '$.period'));
                CREATE INDEX IF NOT EXISTS gas_cost_period ON operations(json_extract(result, '$.cost_period'));""")

    @staticmethod
    def decode(row):
        return dict(id=row["id"], status=row["status"], request=json.loads(row["request"]),
                    result=json.loads(row["result"]))

    def attempts(self):
        with self.journal.db() as db:
            rows = db.execute("SELECT * FROM operations WHERE id LIKE 'attempt:%' ORDER BY rowid").fetchall()
        return [self.decode(row) for row in rows]

    def last(self):
        with self.journal.db() as db:
            row = db.execute("SELECT * FROM operations WHERE id LIKE 'attempt:%' ORDER BY rowid DESC LIMIT 1").fetchone()
        return self.decode(row) if row else None

    def pending(self):
        with self.journal.db() as db:
            rows = db.execute("SELECT * FROM operations WHERE id LIKE 'attempt:%' AND status != 'FINAL' LIMIT 2").fetchall()
        if len(rows) > 1:
            raise ValueError("GAS_CALLER_RECOVERY_REQUIRED")
        return self.decode(rows[0]) if rows else None

    def observe(self, w3, block):
        old = self.journal.get("clock")["result"]
        if old and (int(block.number) < int(old["number"])
                    or int(block.timestamp) < int(old["timestamp"])
                    or w3.eth.get_block(int(old["number"])).hash.hex() != old["hash"]):
            raise ValueError("GAS_CALLER_REORG_RECOVERY_REQUIRED")
        if w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("GAS_CALLER_REORG_RECOVERY_REQUIRED")
        self.journal.update("clock", "OBSERVED", dict(number=str(block.number),
            timestamp=str(block.timestamp), hash=block.hash.hex()))

    def limits(self, pair, timestamp):
        period = timestamp // self.policy["period_seconds"]
        # Only current-period rows and outstanding reservations are loaded;
        # durable lifetime history does not make each poll grow without bound.
        with self.journal.db() as db:
            rows = [self.decode(row) for row in db.execute("""SELECT * FROM operations
                WHERE id LIKE 'attempt:%' AND (status != 'FINAL'
                OR json_extract(request, '$.period')=? OR json_extract(result, '$.cost_period')=?)""",
                (str(period), str(period)))]
            same = db.execute("""SELECT count(*), max(CAST(json_extract(request, '$.timestamp') AS INTEGER))
                FROM operations WHERE json_extract(request, '$.pair')=?""", (pair,)).fetchone()
        held = sum(self.maximum for row in rows if row["status"] != "FINAL")
        spent = sum(int(row["result"]["cost_wei"]) for row in rows
                    if row["status"] == "FINAL" and row["result"].get("cost_period") == str(period))
        count = sum(row["status"] != "FINAL" or row["request"]["period"] == str(period)
                    or row["result"].get("cost_period") == str(period) for row in rows)
        if held + spent + self.maximum > self.policy["max_period_cost_wei"]:
            return "PERIOD_BUDGET"
        if count >= self.policy["max_transactions_per_period"]:
            return "PERIOD_ATTEMPTS"
        if same[0] >= self.policy["max_attempts_per_payee_epoch"]:
            return "EPOCH_ATTEMPTS"
        if same[0] and timestamp < same[1] + self.policy["retry_backoff_seconds"]:
            return "BACKOFF"
        return None

    def reserve(self, pair, timestamp, payload):
        if self.pending() is not None:
            raise ValueError("GAS_CALLER_PENDING")
        if self.limits(pair, timestamp):
            raise ValueError("GAS_CALLER_BUDGET")
        last = self.last()
        key = "attempt:" + str(int(last["id"].split(":")[1]) + 1 if last else 1)
        return self.journal.put(key, dict(pair=pair, timestamp=str(timestamp),
            period=str(timestamp // self.policy["period_seconds"]), payload=payload))

    def finish(self, entry, result):
        self.journal.update(entry["id"], "FINAL", result)
