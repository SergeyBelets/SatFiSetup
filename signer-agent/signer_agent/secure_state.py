import base64
import hashlib
import json
import os
import tempfile
import fcntl
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken


class SecureState:
    def __init__(self, path: str, secret: str):
        if not secret:
            raise ValueError("SIGNER_STATE_KEY is required to persist FROST nonces")
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check_directory()
        key_material = secret.removeprefix("0x").encode("utf-8")
        self._fernet = Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(key_material).digest())
        )

    def _check_directory(self):
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError("SIGNER_STATE_DIRECTORY_PERMISSIONS")

    def _open_private(self, path, *, create=False):
        """Keep the lock, encrypted state and nonce journal on owned private inodes."""
        self._check_directory()
        flags = os.O_RDWR if create else os.O_RDONLY
        if create:
            flags |= os.O_CREAT
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
        if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
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
        if value.get("status") == "nonce-created":
            with self._journal():
                pass  # Provision independent journal before storing any nonce.
        data = self._load()
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

    @contextmanager
    def _journal(self):
        # An old state without its current independent journal must fail closed.
        path = str(self.path) + ".spent.sqlite"
        data = self._load()
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
        with self._journal() as db:
            if db.execute("SELECT 1 FROM spent WHERE session=?", (key,)).fetchone():
                raise RuntimeError(
                    "NONCE_ALREADY_SPENT: stale state or interrupted write; start a new session"
                )

    def assert_spent(self, key, transcript):
        """Retransmit a cached scalar only with its original durable spend proof."""
        with self._journal() as db:
            row = db.execute(
                "SELECT transcript FROM spent WHERE session=?", (key,)
            ).fetchone()
            if row is None:
                raise RuntimeError("NONCE_SPEND_PROOF_MISSING_RECOVERY_REQUIRED")
            if row[0] != transcript:
                raise RuntimeError("NONCE_SPEND_TRANSCRIPT_MISMATCH_RECOVERY_REQUIRED")

    def prepare_share(self, key, transcript, share):
        # Journal is durable before a scalar can leave this process. Interrupted writes fail closed.
        with self._journal() as db:
            db.execute("INSERT INTO spent VALUES (?,?)", (key, transcript))
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
