"""Provenance and cross-language fixture checks for the time-policy component."""
import importlib.util
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "rust/zkspv/time-policy"


def module(name):
    spec = importlib.util.spec_from_file_location("time_policy_" + name, HERE / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_shared_fixture_matrix_has_exact_abi_and_independent_time_results():
    document = json.loads((ROOT / "web3/test/fixtures/header-time-v1.json").read_text())
    assert document["authentication"].startswith("NONE")
    cases = module("generate_vectors").cases()
    assert len(document["vectors"]) == len(cases) == 15
    for vector, (name, timestamps, evm_time) in zip(document["vectors"], cases):
        assert vector["name"] == name
        assert vector["timestamps"] == timestamps
        assert int(vector["evm_timestamp"]) == evm_time
        assert vector["accepted"] == (max(timestamps) <= evm_time + 7200)
        expected = b"SATOSHIFI_HEADER_TIME_V1".ljust(32, b"\0") + b"".join(
            value.to_bytes(32, "big") for value in (len(timestamps), max(timestamps), timestamps[-1]))
        assert bytes.fromhex(vector["abi"][2:]) == expected


def test_native_qualification_reuses_exact_pinned_checkpoint_context_and_policy():
    qualifier = module("qualify")
    _, original, corrected = qualifier.prepare.checked_sources()
    source = qualifier.native_source(original, corrected)
    assert source.startswith(qualifier.align_native_tests(corrected))
    assert (HERE / "native_tests.rs").read_text() in source
    assert str(HERE / "src/lib.rs") in source
    for method in ("test_header_chain_mainnet_old_blocks", "test_header_chain_mainnet_newer_blocks"):
        body = qualifier.prepare.extract.function(original, method)
        context = body[body.index("let mut block_hash"):body.index("let file =")]
        assert context in source


def test_policy_adds_no_dependency_or_substitute_bitcoin_core():
    lock = tomllib.loads((HERE / "Cargo.lock").read_text())
    assert len(lock["package"]) == 1
    assert lock["package"][0]["name"] == "satoshifi-header-time-policy"
    assert not lock["package"][0].get("dependencies")
