import hashlib
from ops.zkspv.preflight import GATES, SUBGATES, assess, preflight


def all_pass():
    return {name: {"status": "PASS", "evidence": ["evidence.json"],
                   "subgates": {s: {"status": "PASS", "evidence": ["evidence.json"]}
                                for s in SUBGATES.get(name, ())}} for name in GATES}


def config():
    return dict(profile="ZK_SPV_V1", evm_chain_id=31337, bitcoin_network="mainnet", proof_mode="REAL",
                remote_proving=False, relay_authorization="proof-only", max_reorg_depth=100,
                heartbeat_interval=302400, heartbeat_lead=3600, heartbeat_grace=1800,
                heartbeat_safety_margin=300, finality_budget=600, max_state_age=7200,
                max_tip_lag=7200, baseline_max_tip_age=7200, continuous_fresh=False,
                **{key: "0x"+"1"*64 for key in ("program_id", "checkpoint_hash", "verifier_runtime_hash", "state_runtime_hash", "adapter_runtime_hash")},
                **{key: "0x"+"1"*40 for key in ("verifier", "state", "adapter", "gas_treasury", "request_transport")})


def test_unknown_or_one_failed_gate_blocks_activation():
    assert not assess({})["declared_gates_satisfied"]
    for name in GATES:
        gates = all_pass()
        gates[name]["status"] = "UNKNOWN"
        assert not assess(gates)["declared_gates_satisfied"]
        gates[name]["status"] = "FAIL"
        assert not assess(gates)["declared_gates_satisfied"]


def test_missing_submode_not_inapplicable():
    gates = all_pass()
    gates["withdrawal_subsidy"]["subgates"]["s_mode"]["status"] = "NOT_APPLICABLE"
    assert not assess(gates)["declared_gates_satisfied"]
    gates = all_pass()
    gates["address_deposits"]["status"] = "NOT_APPLICABLE"
    assert not assess(gates, new_deployment_without_obligations=True)["declared_gates_satisfied"]


def test_live_migration_needs_proven_new_graph():
    gates = all_pass()
    gates["live_migration"]["status"] = "NOT_APPLICABLE"
    assert not assess(gates)["declared_gates_satisfied"]
    assert assess(gates, new_deployment_without_obligations=True)["declared_gates_satisfied"]


def test_pass_strings_without_pinned_artifacts_cannot_activate(tmp_path):
    assert not preflight(config(), {"gates": all_pass()}, tmp_path)["production_ready"]


def test_preflight_checks_hashes_placeholders_modes_and_existing_fresh_policy(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text("synthetic unit fixture; NOT a qualification report")
    decision = {"gates": all_pass(), "artifact_sha256": {"evidence.json": hashlib.sha256(evidence.read_bytes()).hexdigest()}}
    cfg = config()
    assert preflight(cfg, decision, tmp_path)["package_checks_pass"]  # schema-only fixture
    assert not preflight(cfg, decision, tmp_path)["production_ready"]
    for key, bad in (("proof_mode", "MOCK"), ("remote_proving", True), ("max_tip_lag", 86400),
                     ("continuous_fresh", True), ("program_id", "0x"+"0"*64), ("evm_chain_id", True),
                     ("heartbeat_interval", 0), ("state", None)):
        altered = dict(cfg, **{key: bad})
        assert not preflight(altered, decision, tmp_path)["package_checks_pass"], key
    evidence.write_text("changed")
    assert not preflight(cfg, decision, tmp_path)["package_checks_pass"]


def test_evidence_must_stay_inside_review_package(tmp_path):
    decision = {"gates": all_pass(), "artifact_sha256": {"../outside.json": "0"*64}}
    result = preflight(config(), decision, tmp_path)
    assert not result["production_ready"]
    assert any("outside_package" in message for message in result["blockers"])
