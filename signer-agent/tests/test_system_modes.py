"""A missing, skipped or altered test run must never appear as coverage."""
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
