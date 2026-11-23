"""Authenticated off-host anti-rollback guard for a participant's FROST nonces."""

from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PATH = "/v1/nonce-anchor"
ERROR = "NONCE_ANCHOR_UNAVAILABLE_RECOVERY_REQUIRED"
LIMIT = 4096
TOKEN = re.compile(r"[0-9a-f]{64}")
_HTTP_STATUSES = {"BOUND", "READY", "UNSPENT", "SPENT", "MATCH", "CONFLICT", "CLAIMED", "UNBOUND", "WRONG_SERVER"}


def _valid_id(value):
    return isinstance(value, str) and TOKEN.fullmatch(value)


def _tagged_hash(tag, value):
    return hashlib.sha256(("satoshifi.nonce-anchor.v1/" + tag + "\n" + value).encode()).hexdigest()


def _private(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > limit):
            raise ValueError("NONCE_ANCHOR_CONFIG")
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError("NONCE_ANCHOR_CONFIG")
        return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise RuntimeError(ERROR)


class NonceAnchor:
    def __init__(self, url, server_id, participant, token):
        try:
            parsed = urlsplit(url)
            if (not isinstance(url, str) or not url.isascii() or any(ch.isspace() for ch in url)
                    or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment or parsed.path != PATH
                    or "?" in url or "#" in url or "\\" in url or "%" in parsed.netloc
                    or parsed.port == 0 or not all(_valid_id(v) for v in (server_id, participant, token))):
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise ValueError("NONCE_ANCHOR_CONFIG") from None
        self.binding = dict(url=url, server_id=server_id, participant=participant)
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_file(cls, config):
        try:
            value = json.loads(_private(config, LIMIT))
            if (not isinstance(value, dict) or set(value) != {"version", "url", "server_id", "participant", "token_file"}
                    or value["version"] != "1" or not Path(value["token_file"]).is_absolute()):
                raise ValueError()
            token = _private(value["token_file"], 65).decode("ascii").removesuffix("\n")
            return cls(value["url"], value["server_id"], value["participant"], token)
        except (OSError, TypeError, ValueError):
            raise ValueError("NONCE_ANCHOR_CONFIG") from None

    def _request(self, operation, ledger, key=None, transcript=None):
        data = dict(version="1", operation=operation, server_id=self.binding["server_id"],
                    participant=self.binding["participant"], ledger=ledger)
        if key is not None:
            data["session"] = _tagged_hash("session", key)
        if transcript is not None:
            data["transcript"] = _tagged_hash("transcript", transcript)
        request = Request(self.binding["url"], json.dumps(data).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self._token})
        try:
            with self._opener.open(request, timeout=10) as response:
                raw = response.read(LIMIT + 1)
            body = json.loads(raw) if len(raw) <= LIMIT else None
            if (not isinstance(body, dict) or set(body) != {"version", "server_id", "status"}
                    or body["version"] != "1" or body["server_id"] != self.binding["server_id"]
                    or body["status"] not in _HTTP_STATUSES):
                raise ValueError()
            return body["status"]
        except (OSError, URLError, HTTPException, TypeError, ValueError, RuntimeError):
            # In particular, never retry a claim after an ambiguous response.
            raise RuntimeError(ERROR) from None

    def bind(self, ledger):
        if self._request("bind", ledger) != "BOUND":
            raise RuntimeError(ERROR)

    def check(self, ledger):
        if self._request("check", ledger) != "READY":
            raise RuntimeError(ERROR)

    def assert_unspent(self, ledger, key):
        status = self._request("lookup", ledger, key)
        if status == "SPENT":
            raise RuntimeError("NONCE_ALREADY_SPENT")
        if status != "UNSPENT":
            raise RuntimeError(ERROR)

    def claim(self, ledger, key, transcript):
        status = self._request("claim", ledger, key, transcript)
        if status in ("MATCH", "CONFLICT"):
            raise RuntimeError("NONCE_ALREADY_SPENT")
        if status != "CLAIMED":
            raise RuntimeError(ERROR)

    def assert_spent(self, ledger, key, transcript):
        status = self._request("lookup", ledger, key, transcript)
        if status == "UNSPENT":
            raise RuntimeError("NONCE_SPEND_PROOF_MISSING_RECOVERY_REQUIRED")
        if status == "CONFLICT":
            raise RuntimeError("NONCE_SPEND_TRANSCRIPT_MISMATCH_RECOVERY_REQUIRED")
        if status != "MATCH":
            raise RuntimeError(ERROR)


def _valid_request(body):
    if not isinstance(body, dict) or body.get("version") != "1":
        return False
    mode = body.get("operation")
    fields = {"version", "operation", "server_id", "participant", "ledger"}
    if mode in ("lookup", "claim"):
        fields.add("session")
        if mode == "claim" or "transcript" in body:
            fields.add("transcript")
    elif mode not in ("bind", "check"):
        return False
    return set(body) == fields and all(_valid_id(body[key]) for key in fields - {"version", "operation"})


class AnchorStore:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        info = self.path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("NONCE_ANCHOR_STORE_PERMISSIONS")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                fd = os.open(str(self.path) + suffix, flags | (os.O_CREAT if not suffix else 0), 0o600)
            except FileNotFoundError:
                if not suffix:
                    raise
                continue
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_nlink != 1):
                    raise ValueError("NONCE_ANCHOR_STORE_PERMISSIONS")
            finally:
                os.close(fd)
        with self._open() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS identity (slot INTEGER PRIMARY KEY CHECK (slot=1), id TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS ledgers (participant TEXT, ledger TEXT, PRIMARY KEY (participant,ledger))")
            db.execute("CREATE TABLE IF NOT EXISTS spent (participant TEXT, ledger TEXT, session TEXT, transcript TEXT NOT NULL, "
                       "PRIMARY KEY (participant,ledger,session))")
            db.execute("INSERT OR IGNORE INTO identity VALUES (1,?)", (os.urandom(32).hex(),))
            self.server_id = db.execute("SELECT id FROM identity WHERE slot=1").fetchone()[0]

    @contextmanager
    def _open(self):
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db

    def process(self, body):
        if not _valid_request(body):
            raise ValueError("NONCE_ANCHOR_CONFIG")
        if body["server_id"] != self.server_id:
            return "WRONG_SERVER"
        op = body["operation"]
        binding = (body["participant"], body["ledger"])
        with self._open() as db:
            if op in ("bind", "claim"):
                db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT id FROM identity WHERE slot=1").fetchone() != (self.server_id,):
                return "WRONG_SERVER"
            if op == "bind":
                db.execute("INSERT OR IGNORE INTO ledgers VALUES (?,?)", binding)
                return "BOUND"
            if db.execute("SELECT 1 FROM ledgers WHERE participant=? AND ledger=?", binding).fetchone() is None:
                return "UNBOUND"
            if op == "check":
                return "READY"
            key = (*binding, body["session"])
            row = db.execute("SELECT transcript FROM spent WHERE participant=? AND ledger=? AND session=?", key).fetchone()
            expected = body.get("transcript")
            if row is not None:
                if expected is None:
                    return "SPENT"
                return "MATCH" if hmac.compare_digest(expected, row[0]) else "CONFLICT"
            if op == "lookup":
                return "UNSPENT"
            db.execute("INSERT INTO spent VALUES (?,?,?,?)", (*key, expected))
            return "CLAIMED"


def handler(store, principals):
    class AnchorHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(10)
            super().setup()

        def log_message(self, *_args):
            pass

        def _reply(self, code, status=None):
            raw = json.dumps(dict(version="1", server_id=store.server_id, status=status)).encode() if status else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path != PATH:
                return self._reply(404)
            lengths = self.headers.get_all("Content-Length", [])
            auth = self.headers.get_all("Authorization", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[1-9][0-9]{0,3}", lengths[0])
                    or int(lengths[0]) > LIMIT or self.headers.get("Transfer-Encoding") is not None):
                return self._reply(400)
            if len(auth) != 1 or not auth[0].startswith("Bearer ") or not _valid_id(auth[0][7:]):
                return self._reply(403)
            try:
                body = json.loads(self.rfile.read(int(lengths[0])))
                if not _valid_request(body):
                    return self._reply(400)
                token_hash = hashlib.sha256(auth[0][7:].encode()).hexdigest()
                if not hmac.compare_digest(token_hash, principals.get(body["participant"], "0" * 64)):
                    return self._reply(403)
                result = store.process(body)
            except (UnicodeError, ValueError):
                return self._reply(400)
            except (OSError, sqlite3.Error):
                return self._reply(503)
            return self._reply(200, result)

    return AnchorHandler


def main(argv):
    from .operational_api import OperationalHTTPServer
    from .safe_errors import DiagnosticArgumentParser

    parser = DiagnosticArgumentParser(prog="frost-signer nonce-anchor")
    parser.add_argument("--state", required=True)
    parser.add_argument("--policy", required=True, help="Private JSON mapping participant namespaces to token SHA-256 hashes")
    parser.add_argument("--port", default=8791, type=int)
    args = parser.parse_args(argv)
    try:
        principals = json.loads(_private(args.policy, 65536))
        if (not isinstance(principals, dict) or not principals or not all(
                _valid_id(k) and _valid_id(v) for k, v in principals.items())
                or len(set(principals.values())) != len(principals)):
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError("NONCE_ANCHOR_CONFIG") from None
    store = AnchorStore(args.state)
    with OperationalHTTPServer(("127.0.0.1", args.port), handler(store, principals)) as server:
        print(json.dumps(dict(status="NONCE_ANCHOR_READY", server_id=store.server_id)), flush=True)
        server.serve_forever()
    return 0
