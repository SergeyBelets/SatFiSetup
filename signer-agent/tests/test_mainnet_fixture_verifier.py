"""The secondary Core replay must not weaken or relabel pinned fixture evidence."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "ops"))
    return importlib.import_module("verify_mainnet_headers_current")


def test_committed_fixture_hashes_ranges_and_record_count(verifier):
    prefix, records, manifest = verifier.check_fixture_inputs(verifier.pinned.FIXTURE)
    assert len(prefix) // 80 == 36289
    assert len(records) // 68 == 4043
    assert manifest["bitcoin_core_version"] == "/Satoshi:29.0.0/"


def test_both_core_profiles_disable_peers_and_wallet_but_29_pin_is_preserved(verifier, tmp_path):
    for cls in (verifier.pinned.Core, verifier.Core303):
        node = cls.__new__(cls)
        node.port = 18443
        command = node.command("bitcoind", tmp_path)
        assert {"-listen=0", "-connect=0", "-dnsseed=0", "-discover=0", "-disablewallet=1"} <= set(command)
        assert "-rpcbind=127.0.0.1" in command
        assert f"-datadir={tmp_path}" in command
        assert ("-upnp=0" in command) == (cls is verifier.pinned.Core)
        assert ("-natpmp=0" in command) == (cls is verifier.pinned.Core)
    assert verifier.pinned.Core.version == "/Satoshi:29.0.0/"
    assert verifier.pinned.Core.source_commit == "f490f5562d4b20857ef8d042c050763795fd43da"


@pytest.mark.parametrize("reported_version", ["/Satoshi:29.0.0/", "/Satoshi:30.2.0/", "/Satoshi:31.0.0/"])
def test_current_replay_stops_node_and_rejects_any_other_version(verifier, monkeypatch, tmp_path, reported_version):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def getsockname(self):
            return "127.0.0.1", 18443

    monkeypatch.setattr(verifier.pinned.socket, "socket", FakeSocket)
    monkeypatch.setattr(verifier.pinned.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(verifier.Core303, "batch", lambda *args: [{"subversion": reported_version}])
    stopped = []
    monkeypatch.setattr(verifier.Core303, "stop", lambda self: stopped.append(True))
    (tmp_path / ".cookie").write_text("temporary-test-cookie")
    with pytest.raises(RuntimeError, match="expected Bitcoin Core /Satoshi:30.3.0/"):
        verifier.Core303("unused-test-binary", tmp_path)
    assert stopped == [True]


def test_revalidation_accepts_only_same_records_and_semantics(verifier):
    _, records, original = verifier.check_fixture_inputs(verifier.pinned.FIXTURE)
    actual = dict(original, bitcoin_core_version=verifier.Core303.version, bitcoin_core_commit=None,
                  verification="separate Core30.3 replay")
    verifier.compare_revalidation(records, records, actual, original)
    with pytest.raises(ValueError, match="hash, bits, or chainwork"):
        verifier.compare_revalidation(bytes([records[0] ^ 1]) + records[1:], records, actual, original)
    changed = json.loads(json.dumps(actual))
    changed["anchors"]["32256"]["mediantime"] += 1
    with pytest.raises(ValueError, match="metadata or anchors"):
        verifier.compare_revalidation(records, records, changed, original)
    with pytest.raises(ValueError, match="provenance"):
        verifier.compare_revalidation(records, records, dict(actual, bitcoin_core_version=original["bitcoin_core_version"]), original)


def test_corrupt_fixture_checksum_fails_before_starting_core(verifier, monkeypatch, tmp_path):
    source = verifier.pinned.FIXTURE
    for name in ("headers-genesis-36288.bin.gz", "headers-32246-36288.bin", "expected-32246-36288.bin"):
        (tmp_path / name).write_bytes((source / name).read_bytes())
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["headers_sha256"] = "0" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(verifier.pinned, "verify", lambda *args, **kwargs: pytest.fail("must not start Core"))
    with pytest.raises(ValueError, match="fixture checksum mismatch"):
        verifier.verify_current("unused-test-binary", fixture=tmp_path)


def test_report_is_separate_and_preserves_original_manifest(verifier, monkeypatch, tmp_path):
    fixture = verifier.pinned.FIXTURE
    original_manifest = (fixture / "manifest.json").read_bytes()
    prefix, records, original = verifier.check_fixture_inputs(fixture)
    actual = dict(original, bitcoin_core_version=verifier.Core303.version, bitcoin_core_commit=None,
                  verification="separate Core30.3 replay")

    def fake_replay(binary, data, *, core_type):
        assert data == prefix and core_type is verifier.Core303
        return records, actual

    monkeypatch.setattr(verifier.pinned, "verify", fake_replay)
    binary = tmp_path / "test-binary"
    binary.write_bytes(b"test-only nonexecuted binary")
    report = verifier.verify_current(str(binary))
    assert report["status"] == "verified"
    assert report["headers_submitted"] == 36288
    assert report["original_fixture_provenance"]["bitcoin_core_version"] == "/Satoshi:29.0.0/"
    assert report["independent_revalidation"]["bitcoin_core_version"] == "/Satoshi:30.3.0/"
    assert report["independent_revalidation"]["bitcoin_core_commit"] is None
    assert (fixture / "manifest.json").read_bytes() == original_manifest


def test_cli_cannot_write_report_over_pinned_fixture(verifier, monkeypatch):
    monkeypatch.setattr("sys.argv", ["verify", "--bitcoind", "unused",
                                    "--output", str(verifier.pinned.FIXTURE / "manifest.json")])
    monkeypatch.setattr(verifier, "verify_current", lambda *args: pytest.fail("must fail before replay"))
    with pytest.raises(SystemExit) as failure:
        verifier.main()
    assert failure.value.code == 2
