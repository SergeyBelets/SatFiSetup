"""Mainnet doctor readiness is read-only and requires each operator's own Core."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from signer_agent import bitcoin_rpc, bootstrap, bootstrap_plan, doctor, onboarding
from signer_agent.bitcoin_network import MAINNET_GENESIS
from signer_agent.dkg_protocol import PROTOCOL_ID, PROTOCOL_NAME


@pytest.fixture
def diagnostic(monkeypatch, tmp_path, capsys):
    fixture = Path(__file__).resolve().parents[2] / "web3/test/fixtures/mainnet"
    headers = (fixture / "headers-32246-36288.bin").read_bytes()
    records = (fixture / "expected-32246-36288.bin").read_bytes()
    policy = dict(version="1", network="mainnet", genesis_hash=MAINNET_GENESIS,
                  checkpoint_header=headers[800:880].hex(), checkpoint_height="32256",
                  checkpoint_chainwork=records[10 * 68 + 32:10 * 68 + 64].hex(),
                  checkpoint_predecessors=[headers[i * 80:(i + 1) * 80].hex() for i in range(10)],
                  max_reorg_depth="6", max_tip_age_seconds="86400", max_relay_lag_blocks="0",
                  max_headers_per_sync="2")
    profile = dict(chain_id="31337", mode="backed", bitcoin_network="mainnet", bitcoin_policy=policy,
                   protocol=PROTOCOL_NAME, onboarding={}, steps=[{"kind": "omit_readiness"}])
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        profile[role] = {"id": "0x" + "11" * 32}
    state = SimpleNamespace(profile=profile, calls=[],
                            invite={"version": "4", "protocol_id": PROTOCOL_ID.hex()},
                            core_error=None, core_ibd=False, profile_error=None,
                            bootstrap_status="VERIFIED", readiness_status="MAINNET_RELAY_READY",
                            readiness_error=None, result_hash=b"\x11" * 32, manifests={},
                            contracts={"dkg", "coordinator", "conf_pool", "batches", "escrow",
                                       "deposits", "mining", "mp", "fee_manager", "settlement",
                                       "staking", "fee_router", "relay", "spv_guard"})

    def verify_profile(config):
        state.calls.append(("verify_profile", copy.deepcopy(config)))
        if state.profile_error:
            raise state.profile_error
        return state.invite

    def build(config):
        state.calls.append(("build", copy.deepcopy(config)))
        return dict(config, steps=[{"kind": "bitcoin_ready"}])

    class RPC:
        def __init__(self, url, cookie, *, network, policy):
            state.calls.append(("core_init", url, cookie, network, policy))
            state.bitcoin = self

        def check_network(self):
            state.calls.append(("core_check",))
            if state.core_error:
                raise state.core_error
            return {"initialblockdownload": state.core_ibd}

    class Deployment:
        def __init__(self, config, path, *, bitcoin):
            state.calls.append(("bootstrap_init", config, path, bitcoin))
            self.contracts = state.contracts

        def apply(self, *, verify):
            state.calls.append(("apply", verify))
            assert verify is True, "doctor must never submit a transaction"
            return dict(status=state.bootstrap_status,**state.manifests)

        def read_number(self):
            return 100

        def contract(self, name):
            assert name == "dkg"
            return SimpleNamespace(functions=SimpleNamespace(
                getCompletedResult=lambda session: SimpleNamespace(call=lambda **kwargs: (b"", state.result_hash))))

        def bitcoin_ready(self, identity, *, readonly):
            state.calls.append(("bitcoin_ready", identity, readonly))
            assert identity is None and readonly is True
            if state.readiness_error:
                raise state.readiness_error
            return {"status": state.readiness_status}

    monkeypatch.setattr(onboarding, "verify_profile", verify_profile)
    monkeypatch.setattr(bootstrap_plan, "build", build)
    monkeypatch.setattr(bitcoin_rpc, "BitcoinRPC", RPC)
    monkeypatch.setattr(bootstrap, "Bootstrap", Deployment)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/tools/" + name)

    def run(*, core=True):
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(state.profile))
        args = ["--profile", str(path), "--state", str(tmp_path / "state.sqlite"), "--json"]
        if core:
            args += ["--bitcoin-rpc", "http://127.0.0.1:8332", "--bitcoin-cookie", "/private/core.cookie"]
        code = doctor.main(args)
        output = capsys.readouterr().out
        return code, json.loads(output), output

    state.run = run
    return state


def test_mainnet_verified_profile_checks_own_core_and_readonly_full_graph(diagnostic):
    state = diagnostic
    code, result, _ = state.run()
    assert code == 0 and result["status"] == "READY_MAINNET_PROTOCOL"
    assert "not production custody approval" in result["scope"]
    assert all(item["ok"] for item in result["checks"])
    calls = state.calls
    assert next(c for c in calls if c[0] == "core_init")[3:] == ("mainnet", state.profile["bitcoin_policy"])
    deployed = next(c for c in calls if c[0] == "bootstrap_init")
    assert deployed[3] is state.bitcoin
    assert deployed[1]["steps"] == [{"kind": "bitcoin_ready"}]
    assert ("apply", True) in calls and ("bitcoin_ready", None, True) in calls
    assert [c[0] for c in calls].index("verify_profile") < [c[0] for c in calls].index("core_init")


def ulr_graph(state):
    from signer_agent.bootstrap_ulr import ROOTS
    state.profile['ulr']={'version':'1','mode':'create','bitcoin_backend':'relay'}
    state.contracts.remove('settlement')
    state.contracts.update(ROOTS)
    state.contracts.update(('ulr_treasury','ulr_voting','decisions','fee_pipeline','validator'))
    state.manifests['ulr']={'ready':True}


def test_ulr_doctor_requires_current_economy_without_legacy_settlement(diagnostic):
    ulr_graph(diagnostic)
    code,result,_=diagnostic.run()
    assert code==0 and result['status']=='READY_MAINNET_PROTOCOL'
    assert next(c for c in result['checks'] if c['check']=='full_graph')['ok']


@pytest.mark.parametrize('missing',['ulr_treasury','snapshots','ulr_voting','validator'])
def test_ulr_doctor_rejects_incomplete_current_graph(diagnostic,missing):
    ulr_graph(diagnostic);diagnostic.contracts.remove(missing)
    code,result,_=diagnostic.run()
    assert code==2 and not next(c for c in result['checks'] if c['check']=='full_graph')['ok']


def test_ulr_manifest_failure_cannot_be_replaced_by_address_presence(diagnostic):
    ulr_graph(diagnostic);diagnostic.manifests['ulr']['ready']=False
    code,result,_=diagnostic.run()
    assert code==2 and any(c['reason']=='ULR_GRAPH_INCOMPLETE' for c in result['checks'])


def test_full_system_doctor_reports_pending_qualification_after_verified_graph(diagnostic):
    from signer_agent.bootstrap_system import SYSTEM_BINDINGS
    ulr_graph(diagnostic);diagnostic.profile['system_bootstrap']='1'
    diagnostic.contracts.update(SYSTEM_BINDINGS)
    diagnostic.manifests['system']={'graph_configured':True,'ready':False}
    code,result,_=diagnostic.run()
    assert code==2 and result['status']=='NOT_READY'
    assert next(c for c in result['checks'] if c['check']=='full_graph')['ok']
    assert next(c for c in result['checks'] if c['check']=='system_graph')['ok']
    assert any(c['reason']=='SYSTEM_QUALIFICATION_PENDING' for c in result['checks'])


def test_mainnet_without_own_core_cannot_inspect_as_ready(diagnostic):
    code, result, _ = diagnostic.run(core=False)
    assert code == 2 and result["status"] == "NOT_READY"
    assert any(item["reason"] == "BITCOIN_RPC_NOT_CONFIGURED" for item in result["checks"])
    assert not any(c[0] == "bootstrap_init" for c in diagnostic.calls)


@pytest.mark.parametrize("reason", ["MAINNET_TIP_STALE", "MAINNET_CORE_NOT_SYNCED",
                                     "MAINNET_CORE_PRUNED", "MAINNET_TXINDEX_NOT_SYNCED",
                                     "MAINNET_GENESIS_MISMATCH"])
def test_unready_mainnet_core_fails_before_bootstrap(diagnostic, reason):
    diagnostic.core_error = ValueError(reason)
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert any(item["check"] == "bitcoin_rpc" and not item["ok"] for item in result["checks"])
    assert not any(c[0] == "bootstrap_init" for c in diagnostic.calls)


def test_mainnet_ibd_result_cannot_claim_readiness(diagnostic):
    diagnostic.core_ibd = True
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert not any(c[0] == "bootstrap_init" for c in diagnostic.calls)


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_mainnet_requires_v4_verified_invitation(diagnostic, version):
    diagnostic.invite["version"] = version
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert not any(c[0] == "core_init" for c in diagnostic.calls)


def test_mainnet_requires_enrollment_and_policy_binding(diagnostic):
    diagnostic.profile_error = ValueError("ENROLLED_PROFILE_MISMATCH")
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert not any(c[0] == "core_init" for c in diagnostic.calls)


def test_mainnet_profile_without_invitation_is_not_ready(diagnostic):
    del diagnostic.profile["onboarding"]
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert not any(c[0] == "core_init" for c in diagnostic.calls)


def test_mainnet_malformed_policy_fails_before_rpc(diagnostic):
    diagnostic.profile["bitcoin_policy"]["genesis_hash"] = "00" * 32
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert not any(c[0] == "core_init" for c in diagnostic.calls)


@pytest.mark.parametrize("missing", ["relay", "spv_guard", "escrow"])
def test_mainnet_requires_entire_deployed_graph(diagnostic, missing):
    diagnostic.contracts.remove(missing)
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert any(item["check"] == "full_graph" and not item["ok"] for item in result["checks"])


@pytest.mark.parametrize("reason", ["MAINNET_RELAY_STALE", "MAINNET_RELAY_HALTED", "EVM_SNAPSHOT_REORG"])
def test_mainnet_final_readiness_failure_is_not_ready(diagnostic, reason):
    diagnostic.readiness_error = ValueError(reason)
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert ("bitcoin_ready", None, True) in diagnostic.calls


def test_mainnet_pending_relay_cannot_claim_readiness(diagnostic):
    diagnostic.readiness_status = "IN_PROGRESS"
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"


def test_unverified_deployment_cannot_claim_readiness(diagnostic):
    diagnostic.bootstrap_status = "AWAITING_DKG"
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"


def test_zero_dkg_bytes32_is_not_a_completed_ceremony(diagnostic):
    diagnostic.result_hash = bytes(32)
    code, result, _ = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert sum(item["reason"] == "DKG_INCOMPLETE" for item in result["checks"]) == 3


@pytest.mark.parametrize("source", ["core_error", "profile_error", "readiness_error"])
def test_arbitrary_uppercase_secrets_never_enter_diagnostics(diagnostic, source):
    secret = "PRIVATE_KEY_AA11BB22CC33DD44"
    setattr(diagnostic, source, ValueError(secret))
    code, result, output = diagnostic.run()
    assert code == 2 and result["status"] == "NOT_READY"
    assert secret not in output
    assert any(item["reason"] == "ValueError" for item in result["checks"])


def test_regtest_keeps_ready_integration_and_no_mainnet_gate(diagnostic):
    diagnostic.profile.update(mode="integration", bitcoin_network="regtest")
    diagnostic.invite["version"] = "3"
    diagnostic.contracts -= {"relay", "spv_guard"}
    code, result, _ = diagnostic.run()
    assert code == 0 and result["status"] == "READY_INTEGRATION" and "scope" not in result
    assert next(c for c in diagnostic.calls if c[0] == "core_init")[3:] == ("regtest", None)
    assert next(c for c in diagnostic.calls if c[0] == "bootstrap_init")[3] is None
    assert not any(c[0] in ("build", "bitcoin_ready") for c in diagnostic.calls)
