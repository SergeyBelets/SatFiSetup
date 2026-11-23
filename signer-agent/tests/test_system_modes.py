"""A missing, skipped or altered test run must never appear as coverage."""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from ops import system_modes as m


@pytest.mark.parametrize("body", ["", '<testcase/>', '<testcase name="x"><skipped/></testcase>',
    '<testcase name="x"><failure/></testcase>', '<testcase name="x"><error/></testcase>'])
def test_junit_requires_executed_successful_cases(tmp_path, body):
    path = tmp_path/"junit.xml"
    path.write_text('<testsuites><testsuite>'+body+'</testsuite></testsuites>')
    with pytest.raises(RuntimeError): m.check_junit(path)


def test_junit_counts_only_actual_cases(tmp_path):
    path = tmp_path/"junit.xml"
    path.write_text('<testsuites><testsuite tests="900"><testcase name="x"/></testsuite></testsuites>')
    assert m.check_junit(path) == 1


def test_preflight_missing_core_does_not_block_other_layers(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "tool", lambda _: None)
    monkeypatch.setattr(m.shutil, "disk_usage", lambda _: SimpleNamespace(free=GIB))
    GIB = m.GIB
    rows = m.preflight(["bitcoin", "contracts"], tmp_path)["modes"]
    assert "tool:bitcoind" in rows[0]["missing"]
    assert "disk:"+str(2*GIB) in rows[1]["missing"]
    assert all(r["status"] == "BLOCKED" for r in rows)


def test_t02_preflight_with_built_contracts_and_only_local_recovery_dependencies(tmp_path, monkeypatch):
    artifact = tmp_path / "web3/contracts/out/ULRStaking.sol/ULRStaking.json"
    artifact.parent.mkdir(parents=True)
    artifact.touch()
    (tmp_path / "frost-signer").touch()
    native_tool = tmp_path / "native-tool"
    native_tool.touch()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(m, "tool", lambda _: str(native_tool))
    monkeypatch.setattr(m.shutil, "disk_usage", lambda _: SimpleNamespace(free=128 * 1024**2))
    monkeypatch.setattr(m.importlib.util, "find_spec", lambda name: None if name == "eth_tester" else object())
    monkeypatch.setattr("signer_agent.platform_support.capabilities", lambda: dict(signer=True))
    monkeypatch.setattr(m.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=0, stdout="Bitcoin Core v30.3"))

    recovery, bitcoin = m.preflight(["t02-relay", "bitcoin"], tmp_path)["modes"]
    assert recovery["status"] == "READY_TO_RUN"
    assert recovery["minimum_free_bytes"] == 128 * 1024**2
    assert bitcoin["status"] == "BLOCKED"
    assert bitcoin["minimum_free_bytes"] == m.GIB
    assert any("GasCallerFixture" in item for item in bitcoin["missing"])


@pytest.fixture
def t02_runner(monkeypatch):
    lifecycle = []

    @contextmanager
    def local_nodes(directory, env, *, bitcoin):
        assert bitcoin is True
        assert directory.name == "t02-relay"
        env.update(SATOHSI_TEST_RPC="http://127.0.0.1:12345", SATOHSI_BTC_RPC="http://127.0.0.1:12346")
        lifecycle.append("started")
        try:
            yield
        finally:
            lifecycle.append("stopped")

    def preflight(modes, *_):
        assert modes == ["t02-relay"]
        return dict(tools={}, modes=[dict(mode="t02-relay", status="READY_TO_RUN")])

    monkeypatch.setattr(m, "preflight", preflight)
    monkeypatch.setattr(m, "tool", lambda _: None)
    monkeypatch.setattr(m, "source_hashes", lambda: {})
    monkeypatch.setattr(m, "nodes", local_nodes)
    monkeypatch.setattr(m, "prepare_gas_tests", lambda *_: pytest.fail("T-02 must not copy gas fixtures"))
    return lifecycle


@pytest.mark.parametrize(("override", "expected"), [([], 1800), (["--timeout-seconds", "2700"], 2700)])
def test_t02_run_executes_only_recovery_with_default_or_explicit_budget(tmp_path, monkeypatch, t02_runner, override, expected):
    output = tmp_path / "results"

    def command_run(command, *, env, timeout, **_):
        assert env["SATOHSI_BTC_RPC"] == "http://127.0.0.1:12346"
        assert timeout == expected
        assert command[-1] == "tests/e2e/test_bitcoin_t02_relay.py"
        assert [arg for arg in command if arg.startswith("tests/")] == [command[-1]]
        (output / "t02-relay" / "pytest.xml").write_text('<testsuite><testcase name="payout"/></testsuite>')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(m.subprocess, "run", command_run)
    assert m.main(["run", "--mode", "t02-relay", "--output", str(output), *override]) == 0
    report = json.loads((output / "report.json").read_text())
    assert report["results"]["t02-relay"]["status"] == "PASSED"
    assert report["results"]["t02-relay"]["timeout_seconds"] == expected
    assert t02_runner == ["started", "stopped"]


def test_t02_timeout_reports_failure_and_stops_nodes(tmp_path, monkeypatch, t02_runner):
    output = tmp_path / "results"

    def deadline(command, *, timeout, **_):
        raise m.subprocess.TimeoutExpired(command, timeout - 0.01)

    monkeypatch.setattr(m.subprocess, "run", deadline)
    assert m.main(["run", "--mode", "t02-relay", "--timeout-seconds", "30", "--output", str(output)]) == 1
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "INCOMPLETE"
    assert report["results"]["t02-relay"] == dict(status="FAILED", error="COMMAND_TIMED_OUT; see command.log", timeout_seconds=30)
    assert (output / "t02-relay" / "command.log").is_file()
    assert t02_runner == ["started", "stopped"]


@pytest.mark.parametrize("args", [
    ["run", "--mode", "bitcoin", "--timeout-seconds", "30"],
    ["run", "--mode", "t02-relay", "--timeout-seconds", "0"],
    ["preflight", "--mode", "t02-relay", "--timeout-seconds", "30"],
])
def test_scenario_timeout_override_rejects_invalid_scope_or_budget(args):
    with pytest.raises(SystemExit) as error:
        m.main(args)
    assert error.value.code == 2


def test_fresh_requires_private_operator_binding_and_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "tool", lambda _: None)
    monkeypatch.setattr(m.shutil, "disk_usage", lambda _: SimpleNamespace(free=m.GIB))
    report = m.preflight(["zk-fresh"], tmp_path)
    assert report["qualified"] is False
    assert "operator:--suite-plan required" in report["modes"][0]["missing"]
    assert "disk:"+str(8*m.GIB) in report["modes"][0]["missing"]


def test_source_change_during_successful_command_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "preflight", lambda *args: dict(tools={}, modes=[dict(mode="python",status="READY_TO_RUN")]))
    versions = iter([{"x":"1"}, {"x":"1"}, {"x":"2"}])
    monkeypatch.setattr(m, "source_hashes", lambda: next(versions))
    monkeypatch.setattr(m, "tool", lambda _: None)
    monkeypatch.setattr(m, "execute", lambda *args: dict(status="PASSED", tests=1))
    output = tmp_path/"result"
    assert m.main(["run", "--mode", "python", "--output", str(output)]) == 1
    report = json.loads((output/"report.json").read_text())
    assert report["status"] == "INCOMPLETE"
    assert report["results"]["python"] == dict(status="FAILED",error="SOURCE_CHANGED")


def test_run_refuses_existing_evidence_directory(tmp_path):
    with pytest.raises(FileExistsError): m.main(["run", "--output", str(tmp_path)])


def test_gas_cases_get_owned_rpc_and_separate_fixture_artifacts(tmp_path, monkeypatch):
    source = tmp_path/'source'; output = tmp_path/'run'; output.mkdir()
    monkeypatch.setattr(m, 'ROOT', source)
    for name in ('SPVServicePayment.t.sol', 'GasCallerFixture.sol'):
        folder = source/'web3/contracts/out'/name; folder.mkdir(parents=True)
        (folder/'fixture.json').write_text('{"fixture":true}')
    env = {'SATOHSI_TEST_RPC':'http://127.0.0.1:12345'}
    m.prepare_gas_tests(output, env)
    assert env['SATOSHIFI_GAS_E2E_RPC']==env['SATOHSI_TEST_RPC']
    assert env['SATOSHIFI_GAS_FIXTURE_OUT']==env['SATOSHIFI_GAS_CALLER_FIXTURE_OUT']
    copied = Path(env['SATOSHIFI_GAS_FIXTURE_OUT'])/'SPVServicePayment.t.sol/fixture.json'
    assert json.loads(copied.read_text())=={'fixture':True}
    copied.write_text('changed fixture')
    assert json.loads((source/'web3/contracts/out/SPVServicePayment.t.sol/fixture.json').read_text())=={'fixture':True}


def test_missing_gas_fixture_fails_instead_of_skipping(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'ROOT', tmp_path/'missing-build')
    with pytest.raises(FileNotFoundError):m.prepare_gas_tests(tmp_path, {'SATOHSI_TEST_RPC':'http://127.0.0.1:1'})


@pytest.mark.parametrize('name,executable,variable',[
    ('crypto','satohsi-frost-crypto','SATOHSI_CRYPTO_BIN'),
    ('zk-host','satoshifi-zk-worker-host','SATOHSI_ZK_WORKER_HOST')])
def test_native_tools_are_found_by_their_real_executable_names(tmp_path,monkeypatch,name,executable,variable):
    binary=tmp_path/executable;binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
    monkeypatch.delenv(variable,raising=False)
    monkeypatch.setattr(m.shutil,'which',lambda value:str(binary) if value==executable else None)
    assert m.tool(name)==str(binary)
