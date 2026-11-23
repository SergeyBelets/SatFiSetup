import base64
import hashlib
import json
import os
import tempfile
import fcntl
import sqlite3
import stat
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken


def requires_independent_nonce_ledger(chain_id, bitcoin_network=None):
    """Enforce the rollback guard where either public ledger carries mainnet funds."""
    chain_id = str(chain_id)
    return chain_id == "1" or (chain_id == "11155111" and bitcoin_network == "mainnet")


class SecureState:
    def __init__(self, path: str, secret: str, *, nonce_ledger: Optional[str] = None, nonce_anchor=None):
        if not secret:
            raise ValueError("SIGNER_STATE_KEY is required to persist FROST nonces")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_directory()
        self.nonce_ledger = self._ledger_path(nonce_ledger) if nonce_ledger is not None else None
        self._nonce_anchor = nonce_anchor
        key_material = secret.removeprefix("0x").encode("utf-8")
        self._fernet = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(key_material).digest())
        )

    def _check_directory(self):
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError("SIGNER_STATE_DIRECTORY_PERMISSIONS")

    def _ledger_path(self, value):
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.name or path.name in (".", ".."):
            raise ValueError("NONCE_LEDGER_PATH")
        path = path.parent.resolve() / path.name
        if path.parent.is_relative_to(self.path.parent.resolve()):
            raise ValueError("NONCE_LEDGER_PATH")
        return path

    def _open_private(self, path, *, create=False, exclusive=False):
        """Keep the lock, encrypted state and nonce journal on owned private inodes."""
        if Path(path).parent == self.path.parent:
            self._check_directory()
        flags = os.O_RDWR if create else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        # O_NONBLOCK lets fstat reject an unexpected FIFO without waiting on it.
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise ValueError("SIGNER_STATE_FILE_PERMISSIONS")
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _load(self) -> Dict[str, Any]:
        try:
            fd = self._open_private(self.path)
        except FileNotFoundError:
            return {"version": 1, "sessions": {}}
        with os.fdopen(fd, "rb") as handle:
            ciphertext = handle.read()
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise RuntimeError(
                "cannot decrypt signer state; check SIGNER_STATE_KEY"
            ) from exc
        data = json.loads(plaintext.decode("utf-8"))
        version = data.get("version")
        if (version not in (1, 2) or not isinstance(data.get("sessions"), dict)
                or (version == 2) != ("nonce_anchor" in data)
                or version == 2 and not all(data.get(key) for key in ("nonce_anchor", "nonce_ledger", "journal_id"))):
            raise RuntimeError("unsupported signer state format")
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        self._check_directory()
        try:
            fd = self._open_private(self.path)
        except FileNotFoundError:
            pass
        else:
            os.close(fd)
        ciphertext = self._fernet.encrypt(
            json.dumps(data, sort_keys=True).encode("utf-8")
        )
        fd, temporary = tempfile.mkstemp(
            prefix=".signer-state-", dir=str(self.path.parent)
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def session_key(chain_id: int, coordinator: str, session_id: int) -> str:
        return "%s:%s:%s" % (chain_id, coordinator.lower(), session_id)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        value = self._load()["sessions"].get(key)
        return dict(value) if value is not None else None

    def put(self, key: str, value: Dict[str, Any]) -> None:
        data = self._load()
        if value.get("status") == "nonce-created":
            self._anchor(data)
            with self._journal():
                pass  # Provision independent journal before storing any nonce.
            data = self._load()  # The compatible local journal may have pinned its identity now.
        data["sessions"][key] = value
        self._save(data)

    @contextmanager
    def exclusive(self):
        # Held across read/compute/persist/send. A second process cannot spend the same nonce.
        fd = self._open_private(str(self.path) + ".lock", create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _check_ledger_directory(path):
        try:
            parent = path.parent.stat()
        except FileNotFoundError:
            raise RuntimeError("NONCE_LEDGER_MISSING_RECOVERY_REQUIRED") from None
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("NONCE_LEDGER_DIRECTORY_PERMISSIONS")

    @contextmanager
    def _ledger_connection(self, path):
        self._check_ledger_directory(path)
        try:
            os.close(self._open_private(path))
        except FileNotFoundError:
            raise RuntimeError("NONCE_LEDGER_MISSING_RECOVERY_REQUIRED") from None
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.close(self._open_private(str(path) + suffix))
            except FileNotFoundError:
                pass
        db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def initialize_nonce_ledger(self, *, evm_signing_state=None):
        """Provision once before creating a signing state. Exact retries only verify."""
        path = self.nonce_ledger
        if path is None:
            raise ValueError("NONCE_LEDGER_PATH")
        companion_lock = nullcontext() if evm_signing_state is None else evm_signing_state.exclusive()
        with self.exclusive(), companion_lock:
            data = self._load()
            if "nonce_ledger" in data:
                if evm_signing_state is None:
                    self.require_nonce_ledger()
                else:
                    self.evm_nonce_ledger(evm_signing_state)
                return
            states = (self,) if evm_signing_state is None else (self, evm_signing_state)
            if any(
                candidate.exists() or candidate.is_symlink()
                for state in states
                for candidate in (state.path, Path(str(state.path) + ".spent.sqlite"))
            ):
                raise RuntimeError("NONCE_LEDGER_FRESH_STATE_REQUIRED")
            self._check_ledger_directory(path)
            if path.exists() or path.is_symlink():
                raise RuntimeError("NONCE_LEDGER_FRESH_STATE_REQUIRED")
            identity = os.urandom(32).hex()
            if self._nonce_anchor is not None:
                self._nonce_anchor.bind(identity)
                # Older SDKs reject version 2 instead of ignoring the remote fence.
                data.update(version=2, nonce_anchor=dict(self._nonce_anchor.binding))
            try:
                os.close(self._open_private(path, create=True, exclusive=True))
            except FileExistsError:
                raise RuntimeError("NONCE_LEDGER_FRESH_STATE_REQUIRED") from None
            with self._ledger_connection(path) as db:
                db.execute("CREATE TABLE spent (session TEXT PRIMARY KEY, transcript TEXT NOT NULL)")
                db.execute("CREATE TABLE identity (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO identity VALUES (?)", (identity,))
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            data.update(journal_id=identity, nonce_ledger=str(path))
            if evm_signing_state is not None:
                # Keep the legacy signing path, but pin its guard to this role's
                # original ledger identity before either state can be enrolled.
                evm_signing_state._save(dict(data, sessions={}))
            self._save(data)

    def evm_nonce_ledger(self, signing_state):
        """Verify the independent guard on the actual EVM signing state."""
        role = self._load()
        signer = signing_state._load()
        if "nonce_ledger" in role or self.nonce_ledger is not None:
            with self._external_ledger(role):
                if not signer.get("nonce_ledger") or not signer.get("journal_id"):
                    raise RuntimeError("EVM_NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED")
                if (signing_state._ledger_path(signer["nonce_ledger"]) != self._ledger_path(role["nonce_ledger"])
                        or signer["journal_id"] != role["journal_id"]
                        or signer.get("nonce_anchor") != role.get("nonce_anchor")):
                    raise RuntimeError("NONCE_LEDGER_MISMATCH_RECOVERY_REQUIRED")
            anchor = self._anchor(role)
            if anchor is not None:
                anchor.check(role["journal_id"])
            return role["nonce_ledger"]
        if "nonce_ledger" in signer:
            # An independent ledger provisioned directly for .signing by
            # earlier releases remains valid when the role itself is unguarded.
            signing_state.require_nonce_ledger()
            return signer["nonce_ledger"]
        return None

    def require_nonce_ledger(self, *, expected_path=None):
        """Validate an external binding without provisioning or falling back to a local journal."""
        data = self._load()
        with self._external_ledger(data, expected_path=expected_path):
            pass
        anchor = self._anchor(data)
        if anchor is not None:
            anchor.check(data["journal_id"])

    def _anchor(self, data):
        """A provisioned binding is mandatory for every subsequent nonce operation."""
        binding = data.get("nonce_anchor")
        anchor = self._nonce_anchor
        if binding is None:
            if anchor is not None:
                raise RuntimeError("NONCE_ANCHOR_FRESH_BINDING_REQUIRED")
            return None
        if anchor is None:
            config = os.environ.get("SATOSHIFI_NONCE_ANCHOR_CONFIG")
            if not config:
                raise RuntimeError("NONCE_ANCHOR_CONFIG_REQUIRED")
            from .nonce_anchor import NonceAnchor
            anchor = NonceAnchor.from_file(config)
            self._nonce_anchor = anchor
        if anchor.binding != binding or not data.get("nonce_ledger") or not data.get("journal_id"):
            raise RuntimeError("NONCE_ANCHOR_BINDING_MISMATCH")
        return anchor

    def has_nonce_journal(self):
        """Find existing or expected durable restrictions without creating a journal."""
        return (self.nonce_ledger is not None or Path(str(self.path) + ".spent.sqlite").exists()
                or "journal_id" in self._load())

    @contextmanager
    def _external_ledger(self, data, *, expected_path=None):
        if not data.get("nonce_ledger") or not data.get("journal_id"):
            raise RuntimeError("NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED")
        path = self._ledger_path(data["nonce_ledger"])
        if ((self.nonce_ledger is not None and self.nonce_ledger != path)
                or (expected_path is not None and self._ledger_path(expected_path) != path)):
            raise RuntimeError("NONCE_LEDGER_MISMATCH_RECOVERY_REQUIRED")
        try:
            with self._ledger_connection(path) as db:
                if db.execute("SELECT id FROM identity").fetchall() != [(data["journal_id"],)]:
                    raise RuntimeError("NONCE_LEDGER_MISMATCH_RECOVERY_REQUIRED")
                yield db
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("NONCE_LEDGER_UNAVAILABLE_RECOVERY_REQUIRED") from exc

    @contextmanager
    def _journal(self):
        data = self._load()
        if "nonce_ledger" in data or self.nonce_ledger is not None:
            with self._external_ledger(data) as db:
                yield db
            return
        path = str(self.path) + ".spent.sqlite"
        identity = data.get("journal_id")
        if identity and not os.path.exists(path):
            raise RuntimeError("NONCE_JOURNAL_MISSING_RECOVERY_REQUIRED")
        if not identity and any(
            v.get("status") in {"nonce-created", "share-prepared", "consumed"}
            for v in data["sessions"].values()
        ):
            raise RuntimeError("NONCE_JOURNAL_UNBOUND_RECOVERY_REQUIRED")
        fd = self._open_private(path, create=True)
        os.close(fd)
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                fd = self._open_private(path + suffix)
            except FileNotFoundError:
                continue
            else:
                os.close(fd)
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS spent (session TEXT PRIMARY KEY, transcript TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS identity (id TEXT PRIMARY KEY)")
            rows = db.execute("SELECT id FROM identity").fetchall()
            if identity:
                if rows != [(identity,)]:
                    raise RuntimeError("NONCE_JOURNAL_MISMATCH_RECOVERY_REQUIRED")
            else:
                if rows or db.execute("SELECT 1 FROM spent LIMIT 1").fetchone():
                    raise RuntimeError("NONCE_STATE_ROLLBACK_RECOVERY_REQUIRED")
                identity = os.urandom(32).hex()
                db.execute("INSERT INTO identity VALUES (?)", (identity,))
                db.commit()
                data["journal_id"] = identity
                self._save(data)
            with db:
                yield db
        finally:
            db.close()

    def assert_unspent(self, key):
        data = self._load()
        anchor = self._anchor(data)
        with self._journal() as db:
            if db.execute("SELECT 1 FROM spent WHERE session=?", (key,)).fetchone():
                raise RuntimeError("NONCE_ALREADY_SPENT")
        if anchor is not None:
            anchor.assert_unspent(data["journal_id"], key)

    def assert_spent(self, key, transcript):
        """Retransmit a cached scalar only with its original durable spend proof."""
        data = self._load()
        anchor = self._anchor(data)
        with self._journal() as db:
            row = db.execute(
                "SELECT transcript FROM spent WHERE session=?", (key,)
            ).fetchone()
            if row is None:
                raise RuntimeError("NONCE_SPEND_PROOF_MISSING_RECOVERY_REQUIRED")
            if row[0] != transcript:
                raise RuntimeError("NONCE_SPEND_TRANSCRIPT_MISMATCH_RECOVERY_REQUIRED")
        if anchor is not None:
            anchor.assert_spent(data["journal_id"], key, transcript)

    def record_spent(self, key, transcript):
        # A UNIQUE insertion commits before any share can leave the process.
        data = self._load()
        anchor = self._anchor(data)
        with self._journal() as db:
            if anchor is not None:
                if db.execute("SELECT 1 FROM spent WHERE session=?", (key,)).fetchone():
                    raise RuntimeError("NONCE_ALREADY_SPENT")
                anchor.claim(data["journal_id"], key, transcript)
            try:
                db.execute("INSERT INTO spent VALUES (?,?)", (key, transcript))
            except sqlite3.IntegrityError:
                raise RuntimeError("NONCE_ALREADY_SPENT") from None

    def prepare_share(self, key, transcript, share):
        self.record_spent(key, transcript)
        previous = self.get(key)
        self.put(
            key,
            {
                "status": "share-prepared",
                "message_hash": previous["message_hash"],
                "transcript": transcript,
                "share": "%064x" % share,
            },
        )

    def mark_consumed(self, key: str, tx_hash: str) -> None:
        previous = self.get(key) or {}
        # Retain only z_i for retransmission on the same transcript after reorg/revert.
        previous.pop("hiding", None)
        previous.pop("binding", None)
        previous.update(status="consumed", share_tx=tx_hash)
        self.put(key, previous)
