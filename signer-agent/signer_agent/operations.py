"""Private recovery journal. No key material or signing shares belong here.

RPC credentials and signed transactions may be stored for recovery; expose only
explicit public projections to API clients.
"""

from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def decode_entry(row):
    entry = dict(row)
    entry["request"] = json.loads(entry["request"])
    entry["result"] = json.loads(entry["result"])
    return entry


def public_data(value):
    """Reject secrets and ambiguous monetary numbers before persistence or HTTP output."""
    forbidden = {
        "private_key",
        "secret",
        "signing_share",
        "nonce_secret",
        "password",
        "seed",
        "key_package",
        "transport_secret",
    }
    money = {
        "amount",
        "amountIn",
        "amountOut",
        "minimum",
        "minimumReward",
        "value",
        "fee",
        "max_fee",
        "balance",
        "shareAmount",
        "mpAmount",
    }

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() in forbidden:
                    raise ValueError("SECRET_FIELD_FORBIDDEN")
                if (
                    k in money
                    and not isinstance(v, (dict, list))
                    and (not isinstance(v, str) or not v.isdecimal())
                ):
                    raise ValueError("BASE_UNITS_STRING_REQUIRED")
                walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(value)
    return value


def prepare_operation(policy, journal, key, request):
    from .bootstrap import artifact, encode_args

    public_data(request)
    if set(request) != {"action", "args"}:
        raise ValueError("REQUEST_SCHEMA")
    spec = policy.get("actions", {}).get(request["action"])
    if not spec:
        raise ValueError("ACTION_NOT_ALLOWED")
    if spec.get("deployment") not in policy.get("deployments", []):
        raise ValueError("ACTION_NOT_ALLOWED")
    methods = [
        x
        for x in artifact(spec["artifact"])["abi"]
        if x.get("type") == "function" and x["name"] == spec["method"]
    ]
    if len(methods) != 1:
        raise ValueError("AMBIGUOUS_FUNCTION")
    encode_args(methods[0]["inputs"], request["args"])
    return journal.put(key, dict(request, spec=spec))


class Journal:
    records = "operations"
    events = "events"
    max_id_length = 200

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript(
                f"""CREATE TABLE IF NOT EXISTS {self.records} (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, request TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{{}}');
                CREATE TABLE IF NOT EXISTS {self.events} (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL,status TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"""
            )

    @contextmanager
    def db(self):
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError("JOURNAL_DIRECTORY_PERMISSIONS")
        # Secure the opened inode before SQLite can create WAL/SHM sidecars.
        # A directory writable only by this user prevents replacement races.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise ValueError("JOURNAL_FILE_PERMISSIONS")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                try:
                    fd = os.open(sidecar, os.O_RDWR | os.O_NOFOLLOW)
                except FileNotFoundError:
                    continue  # Another SQLite connection can finish its checkpoint.
                try:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_nlink != 1
                    ):
                        raise ValueError("JOURNAL_FILE_PERMISSIONS")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
        c = sqlite3.connect(str(self.path), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def put(self, key, request):
        if not key or len(key) > self.max_id_length:
            raise ValueError("INVALID_IDEMPOTENCY_KEY")
        encoded = canonical(public_data(request))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(
                f"SELECT * FROM {self.records} WHERE id=?", (key,)
            ).fetchone()
            if existing and existing["digest"] != digest:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            if existing:
                return decode_entry(existing)
            c.execute(
                f"INSERT INTO {self.records}(id,digest,request,status) VALUES (?,?,?,?)",
                (key, digest, encoded, "PREPARED"),
            )
            c.execute(
                f"INSERT INTO {self.events}(operation,status) VALUES (?,?)",
                (key, "PREPARED"),
            )
        return dict(id=key, digest=digest, request=json.loads(encoded), status="PREPARED", result={})

    def get(self, key):
        with self.db() as c:
            row = c.execute(f"SELECT * FROM {self.records} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return decode_entry(row)

    def put_many(self, requests):
        """Reserve a bounded set atomically, before any of its bytes can be sent."""
        if not 1 <= len(requests) <= 8 or len({key for key, _ in requests}) != len(requests):
            raise ValueError("INVALID_IDEMPOTENCY_KEY")
        encoded = []
        for key, request in requests:
            if not key or len(key) > 200:
                raise ValueError("INVALID_IDEMPOTENCY_KEY")
            body = canonical(public_data(request))
            encoded.append((key, hashlib.sha256(body.encode()).hexdigest(), body))
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            for key, digest, body in encoded:
                old = c.execute(f"SELECT digest FROM {self.records} WHERE id=?", (key,)).fetchone()
                if old and old["digest"] != digest:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                if not old:
                    c.execute(f"INSERT INTO {self.records}(id,digest,request,status) VALUES(?,?,?,?)",
                              (key, digest, body, "PREPARED"))
                    c.execute(f"INSERT INTO {self.events}(operation,status) VALUES(?,?)", (key, "PREPARED"))

    def update(self, key, status, result):
        with self.db() as c:
            c.execute(
                f"UPDATE {self.records} SET status=?,result=? WHERE id=?",
                (status, canonical(public_data(result)), key),
            )
            c.execute(
                f"INSERT INTO {self.events}(operation,status) VALUES (?,?)", (key, status)
            )

    def list(self):
        with self.db() as c:
            rows = c.execute(f"SELECT * FROM {self.records} ORDER BY rowid DESC LIMIT 1000").fetchall()
        return [decode_entry(row) for row in rows]

    def steps_for(self, deployment):
        """Read one deployment's journal under a single SQLite snapshot."""
        prefix = deployment + ":"
        with self.db() as c:
            rows = c.execute(f"SELECT * FROM {self.records} WHERE substr(id,1,?)=?",
                             (len(prefix), prefix)).fetchall()
        return {row["id"]: decode_entry(row) for row in rows}


class OperationJournal(Journal):
    """API request IDs occupy their own table, including when sharing a DB."""

    records = "api_operations"
    events = "api_operation_events"
    # Old external keys were at most 200 characters. The fixed-width storage
    # key lives outside their namespace even if a caller guesses its digest.
    max_id_length = 201

    def __init__(self, path):
        super().__init__(path)
        with self.db() as c:
            c.execute("""CREATE INDEX IF NOT EXISTS api_operations_scope
                ON api_operations(json_extract(request,'$.spec.deployment'),
                                  coalesce(json_extract(request,'$.client_id'),id))""")
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='api_operation_migration'").fetchone():
                return
            c.execute("BEGIN IMMEDIATE")
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='api_operation_migration'").fetchone():
                return
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'").fetchone():
                # Only the exact former prepare_operation record shape is moved.
                c.execute(
                    """INSERT INTO api_operations
                    SELECT * FROM operations
                    WHERE json_type(request,'$.action')='text'
                      AND json_type(request,'$.args')='array'
                      AND json_type(request,'$.spec')='object'
                      AND json_type(request,'$.spec.deployment')='text'
                      AND json_type(request,'$.spec.contract')='text'
                      AND json_type(request,'$.spec.artifact')='text'
                      AND json_type(request,'$.spec.method')='text'
                      AND (SELECT count(*) FROM json_each(request))=3
                    ORDER BY rowid"""
                )
                c.execute(
                    """INSERT INTO api_operation_events (operation,status,created)
                    SELECT operation,status,created FROM events
                    WHERE operation IN (SELECT id FROM api_operations) ORDER BY sequence"""
                )
                c.execute("DELETE FROM events WHERE operation IN (SELECT id FROM api_operations)")
                c.execute("DELETE FROM operations WHERE id IN (SELECT id FROM api_operations)")
            c.execute("CREATE TABLE api_operation_migration (version INTEGER PRIMARY KEY CHECK(version=1))")
            c.execute("INSERT INTO api_operation_migration VALUES (1)")

    @staticmethod
    def external(entry):
        entry["id"] = entry["request"].pop("client_id", entry["id"])
        return entry

    def scoped_rows(self, key, deployments):
        if deployments is not None and not deployments:
            return []
        clauses, args = [], []
        if key is not None:
            clauses.append("coalesce(json_extract(request,'$.client_id'),id)=?")
            args.append(key)
        if deployments is not None:
            placeholders = ",".join("?" for _ in deployments)
            clauses.append(f"json_extract(request,'$.spec.deployment') IN ({placeholders})")
            args.extend(deployments)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = " LIMIT 1000" if key is None else ""
        with self.db() as c:
            rows = c.execute(f"SELECT * FROM {self.records}{where} ORDER BY rowid DESC{limit}", args).fetchall()
        return [decode_entry(row) for row in rows]

    def put(self, key, request):
        if not key or len(key) > 200:
            raise ValueError("INVALID_IDEMPOTENCY_KEY")
        deployment = request["spec"]["deployment"]
        previous = self.scoped_rows(key, [deployment])
        if previous and "client_id" not in previous[0]["request"]:
            return super().put(key, request)
        storage = hashlib.sha256(canonical([deployment, key]).encode()).hexdigest().rjust(self.max_id_length, ":")
        return self.external(super().put(storage, dict(request, client_id=key)))

    def find(self, key, deployments, *, visible=None):
        if not key or len(key) > 200:
            raise ValueError("INVALID_IDEMPOTENCY_KEY")
        rows = self.scoped_rows(key, deployments)
        if visible is not None:
            rows = [row for row in rows if visible(row)]
        if len(rows) > 1:
            raise ValueError("API_OPERATION_DEPLOYMENT_REQUIRED")
        if not rows:
            raise KeyError(key)
        return rows[0]

    def get(self, key, *, deployments=None, visible=None):
        return self.external(self.find(key, deployments, visible=visible))

    def update(self, key, status, result, *, deployments=None):
        super().update(self.find(key, deployments)["id"], status, result)

    def list(self, *, deployments=None):
        return [self.external(row) for row in self.scoped_rows(None, deployments)]
