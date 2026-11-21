"""The acceptance runner must isolate environment users without dropping tests."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("local_acceptance_under_test", ROOT / "ops/local_acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def test_environment_isolation_follows_local_fixture_and_script_imports(tmp_path):
    (tmp_path / "suite.sol").write_text('import {Fixture} from "./fixture.sol"; contract Suite is Fixture {}')
    (tmp_path / "fixture.sol").write_text('import "./script.sol"; contract Fixture {}')
    (tmp_path / "script.sol").write_text('contract Script { function run() external { vm.envAddress("TARGET"); } }')
    assert acceptance.uses_process_environment(tmp_path / "suite.sol")


def test_ordinary_suite_with_cyclic_imports_needs_only_suite_isolation(tmp_path):
    (tmp_path / "a.sol").write_text('import "./b.sol"; contract A {}')
    (tmp_path / "b.sol").write_text('import "./a.sol"; contract B {}')
    assert not acceptance.uses_process_environment(tmp_path / "a.sol")


def test_missing_local_import_fails_instead_of_assuming_safe_isolation(tmp_path):
    (tmp_path / "suite.sol").write_text('import "./missing.sol";')
    with pytest.raises(FileNotFoundError):
        acceptance.uses_process_environment(tmp_path / "suite.sol")


def output(names, *, passed=None, failed=0, skipped=0, total=None):
    passed = len(names) if passed is None else passed
    total = passed + failed + skipped if total is None else total
    lines = "".join(f"[PASS] {name}(uint32) (gas: 1)\n" for name in names)
    return lines + f"Ran 1 test suite in 0.1s: {passed} tests passed, {failed} failed, {skipped} skipped ({total} total tests)\n"


def test_coverage_accepts_every_discovered_test_once():
    expected = ["testPlain", "testFuzz"]
    assert acceptance.check_solidity_coverage(output(expected), expected, "Suite") == (2, 0, 0)


@pytest.mark.parametrize("actual", [
    output(["testPlain"]),
    output(["testPlain", "testUnexpected"]),
    output(["testPlain", "testPlain"]),
    output(["testPlain"], failed=1),
    output(["testPlain"], skipped=1),
    output(["testPlain", "testFuzz"], passed=3, total=3),
    "no tests matched\n",
])
def test_coverage_rejects_omissions_duplicates_failures_and_skips(actual):
    with pytest.raises(RuntimeError, match="SOLIDITY_TEST_(COVERAGE_MISMATCH|SUMMARY_MISSING)"):
        acceptance.check_solidity_coverage(actual, ["testPlain", "testFuzz"], "Suite")


@pytest.mark.parametrize("fails", [False, True])
def test_all_forge_processes_use_private_artifacts_and_leave_pins_unchanged(tmp_path, monkeypatch, fails):
    project = tmp_path / "project"
    source = project / "web3/test/Suite.t.sol"
    source.parent.mkdir(parents=True)
    source.write_text("contract Suite { function testPlain() external {} }")
    production_out = project / "web3/contracts/out"
    production_cache = project / "web3/cache"
    production_out.mkdir(parents=True)
    production_cache.mkdir()
    pin = production_out / "pinned-contract.json"
    cache = production_cache / "solidity-files-cache.json"
    pin.write_text("production bytecode and immutable references")
    cache.write_text("production compiler cache")
    monkeypatch.setattr(acceptance, "ROOT", project)
    monkeypatch.setenv("FOUNDRY_OUT", str(production_out))
    monkeypatch.setenv("FOUNDRY_CACHE_PATH", str(production_cache))
    monkeypatch.setenv("FOUNDRY_BUILD_INFO_PATH", str(production_out / "build-info"))
    environments = []

    def checked_environment(kwargs):
        env = kwargs["env"]
        assert env["FOUNDRY_OUT"] != str(production_out)
        assert env["FOUNDRY_CACHE_PATH"] != str(production_cache)
        assert Path(env["FOUNDRY_OUT"]).parent == Path(env["FOUNDRY_CACHE_PATH"]).parent
        assert Path(env["FOUNDRY_BUILD_INFO_PATH"]).parent == Path(env["FOUNDRY_OUT"]).parent
        environments.append(env)
        return env

    def check_output(command, **kwargs):
        env = checked_environment(kwargs)
        if command[1] == "config":
            if "--json" not in command:
                return ('[profile.default]\noptimizer = true\nvia_ir = true\n'
                        'remappings = ["lib/=lib/"]\n'
                        '[[profile.default.fs_permissions]]\naccess = "read"\npath = "./test/fixtures"\n'
                        '[[profile.default.fs_permissions]]\naccess = "read"\npath = "./contracts/out"\n')
            config = Path(env["FOUNDRY_CONFIG"])
            assert config.stat().st_mode & 0o777 == 0o600
            values = tomllib.loads(config.read_text())["profile"]["default"]
            assert values["optimizer"] and values["via_ir"]
            assert values["remappings"] == ["lib/=lib/"]
            assert values["fs_permissions"] == [
                {"access": "read", "path": "./test/fixtures"},
                {"access": "read", "path": "./contracts/out"},
                {"access": "read", "path": str(Path(env["FOUNDRY_OUT"]).resolve())},
            ]
            return json.dumps(dict(out=env["FOUNDRY_OUT"], fs_permissions=values["fs_permissions"]))
        assert command[1] == "test" and "--list" in command
        artifact = Path(env["FOUNDRY_OUT"]) / "Suite.t.sol/Suite.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text(json.dumps(dict(bytecode=dict(object="0x6000"))))
        return json.dumps({"test/Suite.t.sol": {"Suite": ["testPlain"]}})

    def run(command, **kwargs):
        env = checked_environment(kwargs)
        if command[1] == "build":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        # Simulate dynamic linking overwriting the compiled contract and cache.
        (Path(env["FOUNDRY_OUT"]) / "pinned-contract.json").write_text("test-linked bytecode")
        Path(env["FOUNDRY_CACHE_PATH"]).mkdir()
        (Path(env["FOUNDRY_CACHE_PATH"]) / "solidity-files-cache.json").write_text("test cache")
        return subprocess.CompletedProcess(command, 0, stdout="no tests matched\n" if fails else output(["testPlain"]), stderr="")

    monkeypatch.setattr(acceptance.subprocess, "check_output", check_output)
    monkeypatch.setattr(acceptance.subprocess, "run", run)
    report = tmp_path / "report.json"
    if fails:
        with pytest.raises(RuntimeError, match="SOLIDITY_TEST_SUMMARY_MISSING"):
            acceptance.run_solidity_suites(report)
    else:
        acceptance.run_solidity_suites(report)
    assert len(environments) == 6
    assert all(env is environments[0] for env in environments)
    assert not Path(environments[0]["FOUNDRY_OUT"]).parent.exists()
    assert pin.read_text() == "production bytecode and immutable references"
    assert cache.read_text() == "production compiler cache"
    assert os.environ["FOUNDRY_OUT"] == str(production_out)
    saved = json.loads(report.read_text())
    assert saved["status"] == ("failed" if fails else "passed")
    assert saved["artifact_isolation"] == "private_temporary_out_cache_build_info"
