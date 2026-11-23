"""Local participant IPC. Never log request/response bodies: they contain secrets."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

DEFAULT_BINARY = Path(__file__).resolve().parents[2] / "frost-crypto/target/debug/satohsi-frost-crypto"
pin_required = ContextVar("frost_binary_pin_required", default=False)


@contextmanager
def require_pin(required):
    token = pin_required.set(required or pin_required.get())
    try:
        yield
    finally:
        pin_required.reset(token)


def _owned_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        data = os.fstat(fd)
        if not stat.S_ISREG(data.st_mode) or data.st_uid not in (0, os.getuid()) or data.st_mode & 0o022:
            raise ValueError("FROST_BINARY_UNSAFE_PATH")
        for directory in Path(path).parents:
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise ValueError("FROST_BINARY_UNSAFE_PATH")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _pin(path, override, required):
    expected = os.environ.get("SATOHSI_CRYPTO_SHA256")
    if expected is None and not override:
        try:
            with _owned_file(Path(str(path) + ".sha256")) as pin:
                expected = pin.read(66).decode("ascii").removesuffix("\n")
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError) as error:
            raise ValueError("FROST_BINARY_PIN_INVALID") from error
    if expected is None:
        if required:
            raise ValueError("FROST_BINARY_PIN_REQUIRED")
    elif not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("FROST_BINARY_PIN_INVALID")
    return expected


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_ctime_ns, info.st_mtime_ns


def pin_local_build():
    """Publish the digest of the binary just built from reviewed local sources."""
    with _owned_file(DEFAULT_BINARY) as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", prefix=".frost-pin-",
                                     dir=DEFAULT_BINARY.parent, delete=False) as temporary:
        pending = Path(temporary.name)
        try:
            temporary.write(digest + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(pending, Path(str(DEFAULT_BINARY) + ".sha256"))
        finally:
            pending.unlink(missing_ok=True)
    return digest


@contextmanager
def checked_binary(*, required=False):
    override = os.environ.get("SATOHSI_CRYPTO_BIN")
    binary = Path(override).expanduser() if override else DEFAULT_BINARY
    expected = _pin(binary, bool(override), required)
    if expected is None:
        yield str(binary), None, None
        return
    if not binary.is_absolute() or ".." in binary.parts:
        raise ValueError("FROST_BINARY_UNSAFE_PATH")
    try:
        opened = _owned_file(binary)
    except OSError as error:
        raise ValueError("FROST_BINARY_UNSAFE_PATH") from error
    with opened:
        fd = opened.fileno()
        info = os.fstat(fd)
        if not info.st_mode & 0o111:
            raise ValueError("FROST_BINARY_UNSAFE_PATH")
        actual = hashlib.file_digest(opened, "sha256").hexdigest()
        current = _file_identity(os.fstat(fd))
        try:
            unchanged = current == _file_identity(binary.lstat()) == _file_identity(info)
        except OSError:
            unchanged = False
        if not unchanged or not hmac.compare_digest(expected, actual):
            raise ValueError("FROST_BINARY_HASH_MISMATCH")
        # Linux/WSL2 resolve and execute the verified inode. macOS uses its
        # checked, owner-controlled pathname and does not expose procfs fds.
        proc_fd = Path(f"/proc/self/fd/{fd}")
        by_descriptor = proc_fd.exists()
        yield str(proc_fd if by_descriptor else binary), fd if by_descriptor else None, actual


def call(op, **params):
    with checked_binary(required=pin_required.get()) as (binary, fd, digest):
        options = {"pass_fds": (fd,)} if fd is not None else {}
        if digest is not None:
            options["env"] = {name: value for name, value in os.environ.items()
                              if not name.startswith(("LD_", "DYLD_"))}
        result = subprocess.run([binary, '--ipc'], input=json.dumps(dict(op=op, **params)),
                                capture_output=True, text=True, timeout=30, **options)
    if result.returncode:
        raise RuntimeError('CRYPTO_VALIDATION_FAILED')
    return json.loads(result.stdout)
