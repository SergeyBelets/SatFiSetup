import hashlib
from types import SimpleNamespace

import pytest

from signer_agent import crypto


@pytest.fixture
def executable(tmp_path, monkeypatch):
    path = (tmp_path.resolve() / "frost-fixture")
    path.write_text('#!/bin/sh\nprintf \'{"status":"ok"}\\n\'\n')
    path.chmod(0o700)
    monkeypatch.setattr(crypto, "DEFAULT_BINARY", path)
    monkeypatch.delenv("SATOHSI_CRYPTO_BIN", raising=False)
    monkeypatch.delenv("SATOHSI_CRYPTO_SHA256", raising=False)
    return path


def test_pinned_local_artifact_runs_and_rebuilt_pin_is_explicit(executable):
    with crypto.require_pin(True):
        with pytest.raises(ValueError, match="FROST_BINARY_PIN_REQUIRED"):
            crypto.call("test")
        assert crypto.pin_local_build() == hashlib.sha256(executable.read_bytes()).hexdigest()
        assert crypto.call("test") == {"status": "ok"}
        executable.write_text('#!/bin/sh\nexit 99\n')
        with pytest.raises(ValueError, match="FROST_BINARY_HASH_MISMATCH"):
            crypto.call("test", fixture_secret="unused")


def test_override_requires_explicit_digest_and_rejects_a_symlink(executable, monkeypatch):
    digest = crypto.pin_local_build()
    monkeypatch.setenv("SATOHSI_CRYPTO_BIN", str(executable))
    with crypto.require_pin(True):
        with pytest.raises(ValueError, match="FROST_BINARY_PIN_REQUIRED"):
            crypto.call("test")
        monkeypatch.setenv("SATOHSI_CRYPTO_SHA256", digest)
        assert crypto.call("test") == {"status": "ok"}
        link = executable.with_name("redirected")
        link.symlink_to(executable)
        monkeypatch.setenv("SATOHSI_CRYPTO_BIN", str(link))
        with pytest.raises(ValueError, match="FROST_BINARY_UNSAFE_PATH"):
            crypto.call("test")


def test_pinned_process_does_not_inherit_loader_injections(executable, monkeypatch):
    crypto.pin_local_build()
    monkeypatch.setenv("LD_PRELOAD", "/test-only")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/test-only")

    def run(*args, **options):
        assert "LD_PRELOAD" not in options["env"]
        assert "DYLD_INSERT_LIBRARIES" not in options["env"]
        return SimpleNamespace(returncode=0, stdout='{"status":"ok"}')

    monkeypatch.setattr(crypto.subprocess, "run", run)
    assert crypto.call("test") == {"status": "ok"}
