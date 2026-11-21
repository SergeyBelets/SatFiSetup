"""Fail-closed deployment preparation. This command never sends transactions."""
import hashlib
import json
from pathlib import Path

GATES = (
    "target_network", "security_maturity", "policy_compatibility", "real_proof_verification",
    "self_hosted", "event_driven", "stale_state_safety", "shared_spv_requests", "relay_authorization",
    "gas_funding", "address_deposits", "withdrawal_subsidy", "integration_review", "regression",
    "deployment_limits", "latency", "economic_viability", "live_migration",
)
SUBGATES = {
    "security_maturity": ("audit_version_match", "build_provenance", "runtime_binding",
                          "license_compatibility", "operational_history", "independent_proving_and_data"),
    "withdrawal_subsidy": ("callback_admission", "ingress_classification", "consent_phase_isolation",
                           "bounded_settlement", "btc_mode", "mp_mode", "s_mode", "batch_allocation", "conservation",
                           "owner_consent", "btc_funding", "mp_redemption", "s_redemption", "verified_actual_fee",
                           "replacement_refund_recovery", "deployment_compatibility"),
}


def assess(gates, *, new_deployment_without_obligations=False):
    blockers = []
    for name in GATES:
        record = gates.get(name, {})
        state = record.get("status", "UNKNOWN")
        if state not in {"PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"}:
            raise ValueError("INVALID_GATE_STATUS:" + name)
        if state == "NOT_APPLICABLE":
            if name != "live_migration" or not new_deployment_without_obligations or not record.get("evidence"):
                blockers.append(name + ":invalid_not_applicable")
        elif state != "PASS":
            blockers.append(name + ":" + state)
        elif not record.get("evidence"):
            blockers.append(name + ":evidence_missing")
        if state == "PASS":
            for sub in SUBGATES.get(name, ()):
                item = record.get("subgates", {}).get(sub, {})
                if item.get("status") != "PASS" or not item.get("evidence"):
                    blockers.append(name + "." + sub + ":unverified")
    return {"declared_gates_satisfied": not blockers, "blockers": blockers}


def preflight(config, decision, root: Path):
    result = assess(decision.get("gates", {}), new_deployment_without_obligations=decision.get("new_deployment_without_obligations") is True)
    blockers = result["blockers"]
    if config.get("profile") != "ZK_SPV_V1":
        blockers.append("profile:unsupported")
    if type(config.get("evm_chain_id")) is not int or config.get("evm_chain_id") not in (1, 11155111, 31337):
        blockers.append("evm_chain_id:explicit_supported_target_required")
    if config.get("bitcoin_network") not in ("mainnet", "testnet4"):
        blockers.append("bitcoin_network:explicit_public_profile_required")
    elif config.get("bitcoin_network") == "testnet4":
        from signer_agent.zk_profiles import profile
        if str(config.get("program_id", "")).removeprefix("0x") != profile({"network": "testnet4"})["image"]:
            blockers.append("program_id:testnet4_image_required")
    if config.get("proof_mode") != "REAL" or config.get("remote_proving") is not False:
        blockers.append("proving:real_self_hosted_only")
    if config.get("relay_authorization") not in ("proof-only", "frost"):
        blockers.append("relay_authorization:explicit_policy_required")
    for name in ("program_id", "checkpoint_hash", "verifier_runtime_hash", "state_runtime_hash", "adapter_runtime_hash"):
        value = config.get(name)
        if not isinstance(value, str) or len(value.removeprefix("0x")) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value.removeprefix("0x")) or int(value, 16) == 0:
            blockers.append(name + ":missing_or_placeholder")
    for name in ("verifier", "state", "adapter", "gas_treasury", "request_transport"):
        value = config.get(name)
        if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x") or any(c not in "0123456789abcdefABCDEF" for c in value[2:]) or int(value, 16) == 0:
            blockers.append(name + ":missing_or_placeholder")
    for name in ("heartbeat_interval", "heartbeat_lead", "heartbeat_grace", "heartbeat_safety_margin",
                 "finality_budget", "max_state_age", "max_tip_lag", "baseline_max_tip_age"):
        value = config.get(name)
        if type(value) is not int or value <= 0:
            blockers.append(name + ":positive_explicit_parameter_required")
    if config.get("max_reorg_depth") != 100:
        blockers.append("max_reorg_depth:must_preserve_100")
    tip = config.get("max_tip_lag")
    old_tip = config.get("baseline_max_tip_age")
    if type(tip) is int and type(old_tip) is int and (tip > old_tip or not 1 <= old_tip <= 86400):
        blockers.append("freshness:baseline_weakened_or_invalid")
    if all(type(config.get(n)) is int and config[n] > 0 for n in
           ("heartbeat_interval", "heartbeat_lead", "heartbeat_grace", "heartbeat_safety_margin", "finality_budget", "max_state_age")):
        if config["heartbeat_lead"] >= config["heartbeat_interval"] or config["finality_budget"] > config["heartbeat_grace"]:
            blockers.append("heartbeat:invalid_windows")
        if config.get("continuous_fresh") is True and config["heartbeat_interval"] + config["heartbeat_grace"] + config["heartbeat_safety_margin"] > config["max_state_age"]:
            blockers.append("continuous_fresh:incompatible_interval")
    if type(config.get("continuous_fresh")) is not bool:
        blockers.append("continuous_fresh:explicit_policy_required")
    artifacts = decision.get("artifact_sha256", {})
    if not artifacts:
        blockers.append("artifact_hashes:missing")
    resolved_root = root.resolve()
    for name, digest in artifacts.items():
        path = (resolved_root / name).resolve()
        if not path.is_relative_to(resolved_root) or not path.is_file():
            blockers.append("artifact:" + name + ":missing_or_outside_package")
            continue
        with path.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != digest:
            blockers.append("artifact:" + name + ":hash_mismatch")
    # Evidence identifiers must be backed by the pinned package, not free-form
    # PASS strings. The independent review itself remains an external gate.
    for name, gate in decision.get("gates", {}).items():
        if gate.get("status") in ("PASS", "NOT_APPLICABLE"):
            for evidence in gate.get("evidence", []):
                if evidence not in artifacts:
                    blockers.append(name + ":unpinned_evidence:" + str(evidence))
            for sub, item in gate.get("subgates", {}).items():
                if item.get("status") == "PASS":
                    for evidence in item.get("evidence", []):
                        if evidence not in artifacts:
                            blockers.append(name + "." + sub + ":unpinned_evidence:" + str(evidence))
    # Hashing a file called "review" does not establish its truth. This local
    # preparation tool has no canonical CREATE/runtime/receipt verifier for the
    # proposed ZK graph; arbitrary JSON PASS flags must never enable deployment.
    return {"package_checks_pass": not blockers, "production_ready": False,
            "blockers": sorted(set(blockers)),
            "activation_blockers": ["live_canonical_bindings_and_operational_acceptance_required"],
            "sends_transactions": False}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    result = preflight(json.loads(args.config.read_text()), json.loads(args.decision.read_text()), args.root)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["production_ready"] else 2)
