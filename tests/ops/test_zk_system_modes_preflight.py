import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from web3 import Web3

from ops import system_modes


@pytest.fixture
def zk_preflight(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    tool = tmp_path / "fixture-tool"
    tool.touch()
    monkeypatch.setattr(system_modes, "ROOT", root)
    monkeypatch.setattr(system_modes, "tool", lambda _: str(tool))
    monkeypatch.setattr(system_modes, "GIB", 1)
    monkeypatch.setattr(system_modes.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr("signer_agent.platform_support.capabilities",
        lambda: dict(signer=True, local_prover_supported=True))
    monkeypatch.delenv(system_modes.CACHED_ZK_EVIDENCE_ENV, raising=False)
    source = "../vendor/risc0-ethereum-3.1.0/contracts/src/groth16/RiscZeroGroth16Verifier.sol"
    vendor = Path(__file__).resolve().parents[2] / "rust/zkspv/state-adapter" / source
    metadata = dict(metadata=dict(sources={source: dict(keccak256=Web3.to_hex(Web3.keccak(vendor.read_bytes())))}))

    def write(relative, content=""):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def artifact(name, current=True):
        return write(f"rust/zkspv/state-adapter/out/{name}.sol/{name}.json", json.dumps(metadata if current else {}))

    write("web3/contracts/out/ULRStaking.sol/ULRStaking.json")
    for name in ("ZkBitcoinStateV2", "ZkSpvAdapter", "RiscZeroGroth16Verifier",
            "ZkBitcoinTestnet4StateV2", "ZkTestnet4SpvAdapter", "ZkTestnet4SPVServiceStateView"):
        artifact(name)
    return SimpleNamespace(root=root, write=write, artifact=artifact,
        run=lambda mode: system_modes.preflight([mode], tmp_path)["modes"][0])


@pytest.mark.parametrize(("mode", "state"), [
    ("zk-cached", "ZkBitcoinStateV2"),
    ("zk-testnet4", "ZkBitcoinTestnet4StateV2"),
])
def test_legacy_artifacts_block_zk_readiness(zk_preflight, mode, state):
    f = zk_preflight
    for path in ("groth16/receipt.bin", "groth16/verified/seal.bin", "state.journal", "state.input"):
        f.write(f"{system_modes.CACHED_ZK_EVIDENCE}/{path}")
    assert f.run(mode)["status"] == "READY_TO_RUN"
    for name in [state, "RiscZeroGroth16Verifier"]:
        f.artifact(name, current=False)
    result = f.run(mode)
    assert result["status"] == "BLOCKED"
    assert [entry.rpartition("/")[-1] for entry in result["missing"]] == [state + ".json", "RiscZeroGroth16Verifier.json"]
    assert all("ZK_VERIFIER_3_1_BUILD_REQUIRED" in entry for entry in result["missing"])


def test_supplied_zk_proof_runs_without_historical_evidence(zk_preflight, tmp_path, monkeypatch):
    f = zk_preflight
    supplied = tmp_path / "participant"
    seal = supplied / "groth16/verified/seal.bin"
    seal.parent.mkdir(parents=True)
    seal.touch()
    (supplied / "state.journal").touch()
    monkeypatch.setenv(system_modes.CACHED_ZK_EVIDENCE_ENV, str(supplied))
    monkeypatch.setattr(system_modes.importlib.util, "find_spec",
        lambda name: None if name == "eth_tester" else object())
    assert f.run("zk-cached")["status"] == "READY_TO_RUN"
    run = tmp_path / "run"
    run.mkdir()

    def pytest_command(command, *, env, **_kwargs):
        assert env[system_modes.CACHED_ZK_EVIDENCE_ENV] == str(supplied)
        assert command[-1] == "tests/e2e/test_zk_bootstrap_e2e.py"
        assert "tests/e2e/test_zk_worker_e2e.py" not in command
        (run / "pytest.xml").write_text('<testsuite><testcase name="verify"/></testsuite>')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(system_modes.subprocess, "run", pytest_command)
    result = system_modes.execute("zk-cached", run, {system_modes.CACHED_ZK_EVIDENCE_ENV: str(supplied)})
    assert result["tests"] == 1
    assert "no fresh proving" in result["scope"]
    f.write(f"{system_modes.CACHED_ZK_EVIDENCE}/state.journal")
    (supplied / "state.journal").unlink()
    assert f.run("zk-cached")["missing"] == ["receipt:state.journal"]


def test_zk_source_fingerprints_include_release_and_contract_inputs(zk_preflight):
    paths = (
        "config/zk-prover-3.1.0.json",
        "rust/zkspv/risc0-release/Cargo.lock",
        "rust/zkspv/groth16-integration/src/bin/wrap_groth16.rs",
        "rust/zkspv/state-adapter/foundry.toml",
        "rust/zkspv/state-adapter/contracts/ZkBitcoinStateV2.sol",
        "rust/zkspv/state-adapter/test/StateAdapter.t.sol",
        "rust/zkspv/vendor/risc0-ethereum-3.1.0/pin.json",
        "rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/IRiscZeroVerifier.sol",
    )
    for name in paths:
        zk_preflight.write(name, name)
    before = system_modes.source_hashes()
    assert set(before) == set(paths)
    zk_preflight.write(paths[-1], "different verifier")
    after = system_modes.source_hashes()
    assert [name for name in paths if before[name] != after[name]] == [paths[-1]]
