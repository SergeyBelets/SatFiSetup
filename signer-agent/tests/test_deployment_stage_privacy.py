"""Generated Foundry journals must stay out of ordinary git add, at any manifest path."""

from pathlib import Path
import base64
import importlib.util
import logging
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runner():
    specification = importlib.util.spec_from_file_location("stage_privacy_review", ROOT / "ops/deployment_stages.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_actual_gitignore_excludes_stage_cache_and_broadcast_but_keeps_public_files():
    run_id = "0123456789abcdef0123456789abcdef"
    private = []
    public = []
    for parent, stem in (
        ("ops/artifacts", "project-local"),
        ("config/deployments/team A", "sepolia"),
        (".", "custom-manifest"),
    ):
        private.extend(
            [
                f"{parent}/{stem}-private/{run_id}/cache/DeployFederation.s.sol/11155111/run-latest.json",
                f"{parent}/{stem}-private/{run_id}/cache/solidity-files-cache.json",
                f"{parent}/{stem}-broadcast/{run_id}/DeployFederation.s.sol/11155111/run-latest.json",
            ]
        )
        public.append(f"{parent}/{stem}.json")
    public.extend(
        [
            "web3/test/fixtures/mainnet/manifest.json",
            "ops/artifacts/upgrade-mainnet-fixtures-20261018.json",
            "docs/transport-broadcast/README.md",
            "tests/fixtures/signer-private/README.md",
        ]
    )
    # Nonexistent representative paths suffice; no private files are created and
    # no Git index/ref is written. --no-index tests the rules even for tracked paths.
    paths = [str(Path(path)) for path in private + public]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=ROOT,
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        check=True,
    )
    ignored = set(result.stdout.rstrip("\0").split("\0"))
    assert ignored == {str(Path(path)) for path in private}


def test_existing_private_directories_are_tightened_at_every_level(runner, tmp_path):
    run_id = "0123456789abcdef0123456789abcdef"
    manifest = tmp_path / "manifest.json"
    paths = [tmp_path / "manifest-private", tmp_path / "manifest-private" / run_id,
             tmp_path / "manifest-private" / run_id / "cache", tmp_path / "manifest-broadcast",
             tmp_path / "manifest-broadcast" / run_id]
    for path in paths:
        path.mkdir(mode=0o755)
    runner.prepare_paths(manifest, run_id, 31337)
    for path in paths:
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("level", ["private", "run", "cache", "broadcast", "broadcast_run"])
def test_existing_symlinks_cannot_redirect_sensitive_outputs(runner, tmp_path, level):
    run_id = "0123456789abcdef0123456789abcdef"
    outside = tmp_path / "unrelated-public-folder"
    outside.mkdir(mode=0o755)
    (outside / "sentinel").write_text("unchanged")
    paths = {
        "private": tmp_path / "manifest-private",
        "run": tmp_path / "manifest-private" / run_id,
        "cache": tmp_path / "manifest-private" / run_id / "cache",
        "broadcast": tmp_path / "manifest-broadcast",
        "broadcast_run": tmp_path / "manifest-broadcast" / run_id,
    }
    selected = paths[level]
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(runner.DeploymentError, match="UNSAFE_DEPLOYMENT_PRIVATE_DIRECTORY"):
        runner.prepare_paths(tmp_path / "manifest.json", run_id, 31337)
    assert outside.stat().st_mode & 0o777 == 0o755
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert (outside / "sentinel").read_text() == "unchanged"


def test_shared_writable_manifest_parent_is_rejected_before_private_files(runner, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(runner.DeploymentError, match="UNSAFE_DEPLOYMENT_MANIFEST_DIRECTORY"):
        runner.prepare_paths(shared / "manifest.json", "0" * 32, 31337)
    assert list(shared.iterdir()) == []


def test_unowned_directory_is_rejected_without_changing_its_permissions(runner, monkeypatch, tmp_path):
    target = tmp_path / "existing"
    target.mkdir(mode=0o755)
    with monkeypatch.context() as patch:
        patch.setattr(runner.os, "fstat", lambda descriptor: SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755, st_uid=os.getuid() + 1))
        with pytest.raises(runner.DeploymentError, match="UNSAFE_DEPLOYMENT_PRIVATE_DIRECTORY"):
            runner.private_directory(target)
    assert target.stat().st_mode & 0o777 == 0o755


def test_untrusted_value_error_is_not_printed_even_if_rpc_changes_its_spelling(runner, monkeypatch):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    secret = "synthetic-private-token"
    assert runner.failure_message(ValueError("invalid credential " + secret), "https://example.invalid/") == "ValueError"
    assert runner.failure_message(runner.DeploymentError("DEPLOYMENT_CODE_CHANGED: FROSTVerifier"), "") == "DEPLOYMENT_CODE_CHANGED: FROSTVerifier"


def test_forge_output_redacts_key_encodings_and_rpc_fragments(runner, monkeypatch):
    # Artificial test-only credential. No node or signer is invoked.
    number = 123456789012345678901234567890123456789
    encoded = number.to_bytes(32, "big").hex()
    monkeypatch.setenv("PRIVATE_KEY", str(number))
    rpc = "https://local-user:local-pass@example.invalid/private-token?key=test-query-token"
    auth = base64.b64encode(b"local-user:local-pass").decode()
    values = [str(number), encoded, "0x" + encoded, encoded.upper(), rpc,
              "local-pass", "private-token", "test-query-token", auth,
              "https://example.invalid/private-token?key=test-query-token"]
    output = runner.redact("\n".join(values), rpc)
    for secret in values:
        assert secret not in output


def test_transport_retry_warnings_cannot_bypass_output_redaction(runner, monkeypatch, capsys):
    for name in ("urllib3", "requests", "web3", "aiohttp", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", list(logger.handlers))
        monkeypatch.setattr(logger, "propagate", logger.propagate)
    runner.protect_transport_logs()
    logging.getLogger("urllib3.connectionpool").warning("synthetic-private-rpc-token")
    captured = capsys.readouterr()
    assert "synthetic-private-rpc-token" not in captured.err + captured.out


@pytest.mark.parametrize("encoded,decoded", [("/rpc/short", "/rpc/short"), ("/rpc/a%2Fb", "/rpc/a/b")])
def test_relative_request_paths_with_short_credentials_are_redacted(runner, encoded, decoded):
    rpc = "https://example.invalid" + encoded
    output = runner.redact("failed request: " + encoded + " decoded: " + decoded, rpc)
    assert encoded not in output and decoded not in output
    # Do not destroy diagnostics by globally replacing short path segments.
    assert runner.redact("rpc response was short", rpc) == "rpc response was short"
