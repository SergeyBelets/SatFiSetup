"""Assemble an honest local evidence package; absence never becomes PASS."""
import argparse
import csv
import hashlib
import json
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .economics import MONTHLY, tco
from .preflight import GATES, preflight
from .replay import idle_replay

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "ops/artifacts/zkspv"
BASE = "733831ec48e3df96a2356623edf17771f64ba16d"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def load(name, default=None):
    path = OUT / name
    return json.loads(path.read_text()) if path.is_file() else default


def junit(name):
    path = OUT / name
    if not path.is_file():
        return {"status": "UNKNOWN", "reason": "No completed result file", "path": name}
    try:
        tree = ET.parse(path).getroot()
        suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
        totals = {key: sum(int(s.attrib.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")}
        status = "PASS" if totals["tests"] > 0 and not any(totals[k] for k in ("failures", "errors", "skipped")) else "FAIL"
        return {"status": status, "counts": totals, "sha256": sha(path), "path": name}
    except (ET.ParseError, ValueError) as exc:
        return {"status": "UNKNOWN", "reason": type(exc).__name__, "path": name}


def missing(unit, reason):
    return {"value": None, "unit": unit, "kind": "MISSING", "as_of": "2026-10-27",
            "source": reason, "payer": "project; actual allocation not supplied"}


def source_snapshot(report):
    """Preserve old evidence without assigning its PASS to changed source bytes."""
    hashes = report.get("source_sha256", {})
    if not hashes:
        return {"status": "UNKNOWN", "reason": "No source hash map in recorded report"}
    changed = [name for name, expected in hashes.items()
               if not (ROOT / name).is_file() or sha(ROOT / name) != expected]
    return {"status": "MATCH" if not changed else "HISTORICAL", "checked": len(hashes),
            "changed_paths": changed}


def make_reports(spec: Path, rerun_replay=False):
    OUT.mkdir(parents=True, exist_ok=True)
    spec_text = spec.read_text()
    clauses = re.findall(r"^(\d+\.\d+)\.\s", spec_text, re.MULTILINE)
    criteria = re.findall(r"^\| (A\d+) \| (.*?) \|$", spec_text, re.MULTILINE)
    with (OUT / "acceptance-matrix.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["criterion", "requirement", "status", "reason", "evidence"])
        for code, requirement in criteria:
            status = "PASS" if code == "A01" else "UNKNOWN"
            reason = ("Exact required archive and baseline hashes; source preservation manifest"
                      if code == "A01" else "Not accepted end-to-end; see partial evidence and explicit blockers")
            writer.writerow([code, requirement, status, reason,
                             "baseline-snapshot.json;preservation-manifest.json" if code == "A01" else "decision.json"])
    write("requirements-index.json", {"specification_version": "2.16", "source_filename": spec.name,
        "specification_sha256": sha(spec), "numbered_clauses": clauses, "acceptance_criteria": [c[0] for c in criteria],
        "scope": "All requirements remain in scope; this index is not evidence of implementation."})

    events = OUT / "event-driven-report.json"
    if rerun_replay or not events.exists():
        scenarios = [idle_replay(hours=h, days=30) for h in (1, 6, 24, 84)]
        scenarios.append(idle_replay(hours=84, days=180))
        write("event-driven-report.json", {"status": "UNKNOWN", "mode": "SIMULATION",
            "planner_implemented": True, "live_observer_transport_prover_integrated": False,
            "planner_test_evidence": "evidence/planner-tests.xml", "scenarios": scenarios,
            "limits": ["No real proof or transaction receipts", "No measured latency or on-chain encrypted status",
                       "No proof of complete unpublished observation history (§15.11)",
                       "Core event/receipt authentication must precede planner input"]})

    costs = {"schema": "satoshifi.zkspv.cost-inputs.v1", "currency": "USD", "target_network": None,
             "real_local_proof_measured": False, "sla_measured": False, "same_policy_replay": False,
             "modes": {mode: {name: missing("USD/month", "No current complete measured receipts/resource prices")
                                for name in MONTHLY} for mode in ("baseline", "shared_ordinary", "zk")},
             "incremental_investment": missing("USD", "Remaining integration/review/deployment/migration/hardware unknown"),
             "required_payback_months": missing("month", "User acceptance horizon not supplied"),
             "proving_jobs": missing("job/month", "No real local proof workload"),
             "cash_flows": [{"type": "heartbeat_security_reserve", "amount": None},
                            {"type": "gas_treasury_contributions_and_shares", "amount": None},
                            {"type": "btc_fee_cap_refundable_principal", "amount": None}],
             "historical_evidence": "Existing ops/artifacts receipt reports are historical, not current comparative benchmark."}
    if not (OUT / "cost-inputs.json").exists():
        write("cost-inputs.json", costs)
    write("tco.json", tco(load("cost-inputs.json")))
    write("benchmark-environment.json", {"recorded_on": "2026-10-27", "base_commit": BASE,
        "final_commit": None, "python": sys.version, "os": platform.platform(), "machine": platform.machine(),
        "compiler_configuration_sha256": sha(ROOT / "web3/foundry.toml"),
        "source_configuration_changed": False, "evm_version": "osaka", "solc": "0.8.30", "optimizer_runs": 1,
        "via_ir": True, "mode_labels": {"planner": "SIMULATION", "contract_tests": "LOCAL_EVM",
                                          "native_bitcoin": "EXECUTE_ONLY", "production_zk": "NOT_RUN"},
        "exact_tool_versions": ["build-manifest.json", "signer-integration-report.json", "funding-parser-report.json"]})
    write("benchmark-inputs.json", {"status": "UNKNOWN", "production_checkpoint": None,
        "synthetic_calendar": {"block_interval_seconds": 600, "windows_days": [30, 180],
                                "heartbeat_hours": [1, 6, 24, 84], "real_bitcoin_headers": False},
        "mainnet_504_header_real_proof": None,
        "retarget_real_proof": ({"evidence": "rust/zkspv/proof-scenarios/report.json",
            "scope": "Conditional two-header mainnet transition; source/artifact status is reported under mainnet_retarget_proof, not a complete workload benchmark"}
            if (ROOT / "rust/zkspv/proof-scenarios/report.json").is_file() else None),
        "native_fixture_provenance": "upstream-lock.json", "funding_core": "funding-core-comparison.json"})
    summaries = [{k: s[k] for k in ("days", "heartbeat_hours", "planned_state_updates", "H_scan", "H_prove", "K_real", "J_real", "M_real")}
                 for s in load("event-driven-report.json")["scenarios"]]
    write("benchmark-results.json", {"status": "UNKNOWN", "mode": "SIMULATION", "receipt_costs": None,
                                      "real_proving_costs": None, "calendar_results": summaries})
    with (OUT / "benchmark-results.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["days", "heartbeat_hours", "planned_state_updates", "H_scan", "H_prove", "K_real", "J_real", "M_real"])
        writer.writeheader()
        writer.writerows(summaries)

    unavailable = {
        "watch-set-manifest.json": ("Full authenticated production watch set, history and rescan anchors not supplied", {
            "scripts": None, "outpoints": None, "issued_address_intents": None, "start_height": None,
            "old_key_epochs_retained": "REQUIRED", "scan_completeness": "UNKNOWN"}),
        "confederation-spv-manifest.json": ("Qualified ZK state/adapter and encrypted on-chain transport are not implemented", {
            "confederation": None, "state": None, "adapter": None, "request_transport": None,
            "relay_authorization": None, "heartbeat_interval_seconds": 302400, "heartbeat_lead_seconds": None,
            "heartbeat_grace_seconds": None, "heartbeat_safety_margin_seconds": None, "transport_epoch": None}),
        "federation-request-report.json": ("No new encrypted SPV request contract/end-to-end; existing DKG/BTC board not claimed universal", {
            "message_receipts": None, "durable_ack": None, "key_epoch_admission": None, "real_cost": None}),
        "gas-funding-manifest.json": ("Production gas funding bindings/reserves and qualified state profile not supplied", {
            "treasury": None, "epoch": None, "payees": None, "weights": None, "funded_caller": None,
            "separate_heartbeat_reserve": None, "paid_service_job_contract": None, "bootstrap_verified": False}),
        "gas-funding-report.json": ("Complete funding/transport/real SPV publication workflow not qualified", {
            "zero_balance_end_to_end": "UNKNOWN", "claim_receipts": None, "handover": "UNKNOWN"}),
        "address-deposit-manifest.json": ("Qualified derivation/verifier/per-input spend and new address registry not implemented", {
            "ready": False, "derivation_profile": None, "registry": None, "recipient_matrix": None,
            "funding_extractor_profile": "FUNDING_STRIPPED_100K_V1", "funding_contract_compatibility": "UNKNOWN",
            "canonical_mint_admission": None, "incident_generation": None, "btc_fee_reserve": None,
            "reconciliation_period": None, "old_threshold_service": "UNKNOWN"}),
        "address-deposit-test-vectors.json": ("No qualified address derivation vectors; native funding vectors are separate", {
            "derivation": None, "per_input_signatures": None, "funding_vectors": "funding-core-comparison.json"}),
    }
    for filename, (reason, fields) in unavailable.items():
        if not (OUT / filename).exists():
            write(filename, {"status": "UNKNOWN", "production_ready": False, "reason": reason, **fields})

    funding = load("funding-parser-report.json", {})
    gas = load("gas-treasury-component-report.json", {})
    gas_governance = load("gas-governance-report.json", {})
    governance_snapshot = source_snapshot(gas_governance)
    governance_local_status = gas_governance.get("local_e2e", {}).get("status", "UNKNOWN")
    if governance_local_status == "PASS" and governance_snapshot["status"] != "MATCH":
        governance_local_status = "HISTORICAL_PASS"
    gas_caller = load("gas-caller-report.json", {})
    caller_snapshot = source_snapshot(gas_caller)
    caller_local_status = gas_caller.get("local_e2e", {}).get("status", "UNKNOWN")
    if caller_local_status == "PASS" and caller_snapshot["status"] != "MATCH":
        caller_local_status = "HISTORICAL_PASS"
    if gas:
        write("gas-funding-report.json", {
            "status": "UNKNOWN", "production_ready": False,
            "reason": "Native accounting/payment, enrolled governance and bounded local caller components implemented; production state/transport/publication and financial activation remain unqualified",
            "component_evidence": "gas-treasury-component-report.json", "source_snapshot": source_snapshot(gas),
            "zero_balance_native_claim": gas.get("local_receipt_evidence", {}).get("status", "UNKNOWN"),
            "zero_balance_end_to_end": "UNKNOWN", "real_threshold_activation": "UNKNOWN",
            "local_threshold_activation": governance_local_status,
            "local_governance_evidence": "gas-governance-report.json" if gas_governance else None,
            "local_governance_source_snapshot": governance_snapshot,
            "handover": "UNKNOWN", "bounded_automatic_caller": caller_local_status,
            "local_caller_evidence": "gas-caller-report.json" if gas_caller else None,
            "local_caller_source_snapshot": caller_snapshot,
            "production_bootstrap_activation": False})
        gas_manifest = load("gas-funding-manifest.json", {})
        gas_manifest.update(status="UNKNOWN", production_ready=False,
            reason="Component implemented locally; reviewed production bindings, roster/weights, tariff, reserve and qualified state profile absent",
            local_component_evidence="gas-treasury-component-report.json", bootstrap_verified=False)
        write("gas-funding-manifest.json", gas_manifest)
    admission = load("mint-admission-contract-report.json", {})
    admission_client = load("mint-admission-client-report.json", {})
    derivation_path = ROOT / "rust/zkspv/derivation-qualification/qualification-report.json"
    derivation = json.loads(derivation_path.read_text()) if derivation_path.is_file() else {}
    address_input_path = OUT / "worker-integration/address-input-code-report.json"
    address_input = json.loads(address_input_path.read_text()) if address_input_path.is_file() else {}
    address_registry_path = OUT / "worker-integration/address-registry-code-report.json"
    address_registry = json.loads(address_registry_path.read_text()) if address_registry_path.is_file() else {}
    address_mint_path = OUT / "worker-integration/address-mint-client-report.json"
    address_mint = json.loads(address_mint_path.read_text()) if address_mint_path.is_file() else {}
    address_signing_path = OUT / "worker-integration/address-signing-client-report.json"
    address_signing = json.loads(address_signing_path.read_text()) if address_signing_path.is_file() else {}
    address_watch_path = OUT / "worker-integration/address-watch-client-report.json"
    address_watch = json.loads(address_watch_path.read_text()) if address_watch_path.is_file() else {}
    address_registration_path = OUT / "worker-integration/address-registration-client-report.json"
    address_registration = json.loads(address_registration_path.read_text()) if address_registration_path.is_file() else {}
    address_spv_path = OUT / "worker-integration/address-spv-worker-report.json"
    address_spv = json.loads(address_spv_path.read_text()) if address_spv_path.is_file() else {}
    fee_path = OUT / "worker-integration/btc-fee-reserve-report.json"
    fee = json.loads(fee_path.read_text()) if fee_path.is_file() else {}
    withdrawal_path=OUT/'withdrawal-subsidy-report.json'
    withdrawal=json.loads(withdrawal_path.read_text()) if withdrawal_path.is_file() else {}
    withdrawal_user_path = OUT/'worker-integration/withdrawal-bootstrap-user-report.json'
    withdrawal_user = json.loads(withdrawal_user_path.read_text()) if withdrawal_user_path.is_file() else {}
    smp1_path=OUT/'worker-integration/smp1-wallet-preparation-report.json'
    smp1=json.loads(smp1_path.read_text()) if smp1_path.is_file() else {}
    recipient_path=OUT/'worker-integration/recipient-matrix-report.json'
    recipient=json.loads(recipient_path.read_text()) if recipient_path.is_file() else {}
    incident_path=OUT/'worker-integration/incident-status-report.json'
    incident=json.loads(incident_path.read_text()) if incident_path.is_file() else {}
    epoch_path=OUT/'worker-integration/epoch-service-report.json'
    epoch=json.loads(epoch_path.read_text()) if epoch_path.is_file() else {}
    continuity_path=OUT/'worker-integration/continuity-report.json'
    continuity=json.loads(continuity_path.read_text()) if continuity_path.is_file() else {}
    contour_path=OUT/'worker-integration/contour-handover-report.json'
    contour=json.loads(contour_path.read_text()) if contour_path.is_file() else {}
    worker_contour_path=OUT/'worker-integration/contour-worker-report.json'
    worker_contour=json.loads(worker_contour_path.read_text()) if worker_contour_path.is_file() else {}
    service_contour_path=OUT/'worker-integration/contour-service-report.json'
    service_contour=json.loads(service_contour_path.read_text()) if service_contour_path.is_file() else {}
    setup_contour_path=OUT/'worker-integration/contour-setup-report.json'
    setup_contour=json.loads(setup_contour_path.read_text()) if setup_contour_path.is_file() else {}
    mint_contour_path=OUT/'worker-integration/contour-mint-report.json'
    mint_contour=json.loads(mint_contour_path.read_text()) if mint_contour_path.is_file() else {}
    withdrawal_contour_path=OUT/'worker-integration/contour-withdrawal-report.json'
    withdrawal_contour=json.loads(withdrawal_contour_path.read_text()) if withdrawal_contour_path.is_file() else {}
    incident_contour_path=OUT/'worker-integration/contour-incident-report.json'
    incident_contour=json.loads(incident_contour_path.read_text()) if incident_contour_path.is_file() else {}
    epoch_contour_path=OUT/'worker-integration/contour-epoch-report.json'
    epoch_contour=json.loads(epoch_contour_path.read_text()) if epoch_contour_path.is_file() else {}
    suite_contour_path=OUT/'worker-integration/contour-suite-report.json'
    suite_contour=json.loads(suite_contour_path.read_text()) if suite_contour_path.is_file() else {}
    c123_path=OUT/'worker-integration/c123-implementation-report.json'
    c123=json.loads(c123_path.read_text()) if c123_path.is_file() else {}
    local={};local_path=None
    pointer=OUT/'local-acceptance-current.json'
    if pointer.is_file():
        selected=json.loads(pointer.read_text());local_path=(ROOT/selected['report']).resolve()
        if not local_path.is_relative_to(OUT) or sha(local_path)!=selected['sha256']:raise ValueError('LOCAL_ACCEPTANCE_POINTER')
        local=json.loads(local_path.read_text())
    address = load("address-deposit-report.json")
    if address is not None:
        address["gates"]["funding_envelope"] = {
            "status": "UNKNOWN", "profile": "FUNDING_STRIPPED_100K_V1",
            "local_parser_status": funding.get("status", "UNKNOWN"),
            "evidence": "funding-parser-report.json",
            "reason": "Separate EVM parser and shared vectors implemented; custody/mint integration and target-network limits unqualified"}
        address["gates"]["mint_admission"] = {
            "status": "UNKNOWN", "implementation": "Canonical single-slot contract and standard client integration",
            "contract_evidence": "mint-admission-contract-report.json",
            "client_evidence": "mint-admission-client-report.json",
            "reason": "New-pool common nonce component; future address-mode integration, bounded waiting queue and full throughput/latency unqualified"}
        if derivation:
            address["gates"]["custody_derivation"] = {
                "status": "UNKNOWN", "native_profile": derivation.get("profile"),
                "native_evidence": str(derivation_path.relative_to(ROOT)),
                "reason": "Test-only pinned-library derivation/signatures; immutable on-chain verifier and canonical production admission absent"}
        if address_input:
            address["gates"].setdefault("custody_derivation", {"status": "UNKNOWN"}).update(
                code_evidence=str(address_input_path.relative_to(ROOT)),
                code_source_snapshot=source_snapshot(address_input),
                reason="Versioned native BASE/DERIVED transaction signing and candidate EVM arithmetic verifier implemented; curve source/audit qualification, canonical registry, generation fences and live shared-journal/mint integration remain pending")
        if address_registry:
            address.update(implemented_solidity_address_mode=True,
                reason="Candidate immutable address registry, shared accounting/mint and generation fences implemented; live profile/client/watch/recovery and qualification remain pending")
            for gate in ("immutable_binding", "issuance_and_backing", "incident_generation", "recipient_suitability", "deployment_binding"):
                address["gates"].setdefault(gate, {}).update(status="UNKNOWN",
                    local_code_status=address_registry.get("status"),
                    code_evidence=str(address_registry_path.relative_to(ROOT)),
                    code_source_snapshot=source_snapshot(address_registry),
                    reason="Candidate contract logic tested with explicit DKG/approval/SPV/qualification fixtures; production activation, supported clients, cross-graph continuity and full qualification remain pending")
            address["gates"].setdefault("custody_derivation", {"status": "UNKNOWN"})["reason"] = "Native per-input crypto, candidate verifier and registry implemented; independent curve/profile qualification and live derived-signer/journal integration remain pending"
        if address_mint:
            address.update(mint_client_code_implemented=True,
                reason="Mint-only address client/admission and independent funding/generation review implemented; verifier qualification, address transport/watch, live Bitcoin plans/journals and combined E2E remain pending")
            address["gates"]["mint_admission"].update(
                implementation="Common slot for legacy/custody/address modes; immutable economics and generation-bound attempts",
                code_evidence=str(address_mint_path.relative_to(ROOT)),
                code_source_snapshot=source_snapshot(address_mint),
                reason="Focused client/contract checks and shared typed-operation vectors; full Core/proving/FROST E2E and operational queue/latency qualification pending")
        if address_signing:
            address.update(bitcoin_signer_code_implemented=True,
                reason="Explicit V3 BASE/DERIVED signer, generation/provenance/Core guards and common nonce/recovery journal implemented; verifier/profile qualification, address watch/worker, reconciliation and combined E2E remain pending")
            address["gates"].setdefault("custody_derivation", {}).update(status="UNKNOWN",
                signing_code_evidence=str(address_signing_path.relative_to(ROOT)),
                signing_source_snapshot=source_snapshot(address_signing),
                reason="Real native mixed-input signatures and journal crash/restart checks with explicit EVM/Core/qualification fixtures; actual verifier remains unqualified and complete operational contour pending")
        if address_watch:
            address.update(address_request_watch_code_implemented=True,
                reason="Address ciphertext board/client, durable descriptors/ACK and bounded Core watch/rescan/recovery implemented; verifier/operational qualification, registration workflow, fee service/reconciliation and combined E2E remain pending")
            address["gates"].setdefault("lifecycle_recovery", {}).update(status="UNKNOWN",
                code_evidence=str(address_watch_path.relative_to(ROOT)),code_source_snapshot=source_snapshot(address_watch),
                reason="Native HPKE/block codec, private SQLite durability and canonical lookup/reorg component checks; actual Core/service operational qualification and full lifecycle not established")
        if address_registration:
            address.update(address_registration_code_implemented=True,
                reason="V2 requester attestation, typed public FROST registration, generation/deadline checks and bounded explicit recovery implemented; verifier/service qualification, address worker, fee reserve/reconciliation and combined E2E remain pending")
            address["gates"].setdefault("registration_workflow", {}).update(status="UNKNOWN",
                code_evidence=str(address_registration_path.relative_to(ROOT)),code_source_snapshot=source_snapshot(address_registration),
                reason="Component checks use explicit DKG/EVM/provenance/qualification fixtures; actual verifier remains QUALIFIED=false, no live registration qualification")
        if address_spv:
            address.update(address_spv_worker_code_implemented=True,
                reason="Registered-address V5/V3 worker, watch/consumer hooks and guarded admission/quote/payment implemented; verifier/operational qualification, reserve/reconciliation and combined E2E remain pending")
            address["gates"].setdefault("spv_worker",{}).update(status="UNKNOWN",code_evidence=str(address_spv_path.relative_to(ROOT)),
                code_source_snapshot=source_snapshot(address_spv),reason="Fast native/SQLite component checks with explicit Core/EVM/qualification fixtures; no new proving or combined live payment/consumer E2E")
        if fee:
            address.update(btc_fee_reserve_code_implemented=True,reconciliation_code_implemented=True,
                reason="Graph-scoped canonical BTC reserve, principal-preserving settlement, typed FROST and complete native inventory code implemented; verifier/operational qualification, per-owner credits, continuity and combined E2E remain pending")
            address['gates'].setdefault('btc_fee_reserve',{}).update(status='UNKNOWN',code_evidence=str(fee_path.relative_to(ROOT)),
                code_source_snapshot=source_snapshot(fee),reason="Native donor/SQLite and contract monetary checks with explicit Core/EVM/DKG/SPV/qualification fixtures; no live reserve or combined E2E qualification")
            address['gates'].setdefault('reconciliation',{}).update(status='UNKNOWN',code_evidence=str(fee_path.relative_to(ROOT)),
                reason="Full Core inventory, principal/supply checks, mempool/RBF, confirmed settlement waiting and scoped common-journal recovery implemented; actual production watch history and operating qualification remain external")
        if withdrawal.get('personal_fee_code_implemented'):
            address.update(withdrawal_fee_code_implemented=True,ready=False,
                reason='Personal BTC/MP/S owner consent, named credits/refunds, exact version admission, V4 signer and shared SPV code implemented; actual purpose qualification, production bootstrap, independent review and combined live contour remain pending')
            address['gates']['withdrawal_fee_credits']=dict(status='UNKNOWN',code_evidence=str(withdrawal_path.relative_to(ROOT)),
                code_source_snapshot=source_snapshot(withdrawal),reason='Local monetary/gas/native checks with explicit cryptographic/provider boundaries; no live qualification')
        if recipient.get('code_implemented'):
            address['recipient_matrix']=dict(code_implemented=True,status='UNKNOWN',
                profiles=['ordinary','RecipientWalletV1','explicit unverified advanced contract'],
                source_snapshot=source_snapshot(recipient),evidence=str(recipient_path.relative_to(ROOT)))
            address['gates'].setdefault('recipient_profiles',{}).update(status='UNKNOWN',
                code_evidence=str(recipient_path.relative_to(ROOT)),code_source_snapshot=source_snapshot(recipient),
                reason='Recipient code and local MP control path implemented; live combined acceptance and independent qualification remain pending')
        if incident.get('code_implemented'):
            address['reason']='Candidate registry/registration/watch, recipient profiles, SPV worker, reserve/reconciliation, personal fee credits and incident/status client implemented; actual qualification, old-epoch/cross-graph continuity, live operation and combined E2E remain pending'
            address['incident_status']=dict(code_implemented=True,status='UNKNOWN',
                source_snapshot=source_snapshot(incident),evidence=str(incident_path.relative_to(ROOT)))
            address['gates'].setdefault('incident_status',{}).update(status='UNKNOWN',
                code_evidence=str(incident_path.relative_to(ROOT)),code_source_snapshot=source_snapshot(incident),
                reason='Private report, enrolled FROST status client and independent generation/reorg recovery implemented; live combined acceptance and independent qualification pending')
        if epoch.get('code_implemented'):
            address['reason']='Candidate address/recipient/fee, incident/status and old-epoch service clients implemented; actual qualification, canonical cross-graph/key-family continuity, live operation and combined E2E remain pending'
            address['old_epoch_service']=dict(code_implemented=True,status='UNKNOWN',
                source_snapshot=source_snapshot(epoch),evidence=str(epoch_path.relative_to(ROOT)))
            address['gates']['old_epoch_service']=dict(status='UNKNOWN',code_evidence=str(epoch_path.relative_to(ROOT)),
                code_source_snapshot=source_snapshot(epoch),reason='Original graph/archive scheduling, retention claims, generation/nonce and funding guards implemented; single-pool profile, live threshold/restore availability and independent qualification remain pending')
        if continuity.get('code_implemented'):
            address['original_graph_continuity']=dict(code_implemented=True,status='UNKNOWN',
                source_snapshot=source_snapshot(continuity),evidence=str(continuity_path.relative_to(ROOT)),
                profile=continuity['profile'],cross_graph_migration_supported=False,history_complete=False)
            address['gates']['original_graph_continuity']=dict(status='UNKNOWN',code_evidence=str(continuity_path.relative_to(ROOT)),
                code_source_snapshot=source_snapshot(continuity),reason='Original-graph client guards and bounded incomplete inventory implemented; unsupported transitions blocked. Global canonical continuity policy, actual live inventory and combined acceptance remain unqualified')
        if contour.get('code_implemented'):
            address['retained_graph_handover']=dict(status='UNKNOWN',code_implemented=True,
                source_snapshot=source_snapshot(contour),evidence=str(contour_path.relative_to(ROOT)),
                global_onchain_family_registry_implemented=False,cross_graph_migration_supported=False)
        write("address-deposit-report.json", address)
    if address_watch:
        watch_manifest=load("watch-set-manifest.json",{})
        watch_manifest.update(local_code_implemented=True,code_evidence=str(address_watch_path.relative_to(ROOT)),
            code_source_snapshot=source_snapshot(address_watch),profile="ADDRESS_WATCH_BLOCKS_V1",
            initial_scan_height=0,maximum_intents=256,maximum_outputs=4096,maximum_blocks_per_poll=256,
            maximum_reorg_repair=100,production_watch_inventory_supplied=False,
            reason="Bounded durable watcher/recovery code exists; actual production descriptors, completed history/coverage, archive resources and operational reconciliation remain unqualified")
        write("watch-set-manifest.json",watch_manifest)
    address_manifest = load("address-deposit-manifest.json")
    if address_manifest is not None:
        address_manifest["funding_parser"] = "web3/contracts/src/core/BitcoinFundingParser.sol"
        address_manifest["funding_parser_evidence"] = "ops/artifacts/zkspv/funding-parser-report.json"
        address_manifest["funding_contract_compatibility"] = "UNKNOWN"
        address_manifest["canonical_mint_admission"] = "web3/contracts/src/federation/BitcoinMintAdmission.sol"
        address_manifest["mint_admission_qualification"] = "PARTIAL_NEW_POOL_COMPONENT"
        address_manifest["native_derivation_evidence"] = str(derivation_path.relative_to(ROOT)) if derivation else None
        if address_input:
            address_manifest.update(derivation_profile=address_input.get("profile"),
                derivation_profile_qualified=False, address_crypto_code_evidence=str(address_input_path.relative_to(ROOT)),
                reason="Native codec/per-input crypto and candidate EVM verifier implemented; canonical address registry, live custody/mint integration and qualification remain pending")
        if address_registry:
            address_manifest.update(registry_implementation="web3/contracts/src/federation/BitcoinAddressRegistry.sol",
                registry_code_evidence=str(address_registry_path.relative_to(ROOT)),
                incident_generation="CANDIDATE_FIXED_GRAPH_COMPONENT", deployed_registry=None,
                address_client_enabled=False, ready=False,
                reason="Immutable registry, shared address mint and current generation fences implemented; verifier remains unqualified and live clients/watch/recovery/production admission remain pending")
        if address_mint:
            address_manifest.update(mint_client_code_implemented=True,
                mint_client_evidence=str(address_mint_path.relative_to(ROOT)),
                mint_trust_profile="MINT_ONLY_V1", live_bitcoin_signer_enabled=False,
                address_client_enabled=False, ready=False,
                reason="Explicit address mint client/admission code available; actual verifier QUALIFIED=false prevents activation. No address issuance/watch, paid address worker profile or live BTC plans/journal integration")
        if address_signing:
            address_manifest.update(bitcoin_signer_code_implemented=True,
                bitcoin_signing_profile="BTC_INPUT_KEYS_V1", bitcoin_plan_version="3",
                bitcoin_signer_code_evidence=str(address_signing_path.relative_to(ROOT)),
                live_bitcoin_signer_enabled=False,
                reason="Explicit V3 signer code and shared journal available; actual verifier QUALIFIED=false blocks derived private work. Address issuance/watch, reconciliation and combined live qualification pending")
        if address_watch:
            address_manifest.update(address_request_watch_code_implemented=True,
                address_request_watch_evidence=str(address_watch_path.relative_to(ROOT)),
                address_message_board="web3/contracts/src/federation/BitcoinAddressMessageBoard.sol",
                ready=False,address_client_enabled=False,
                reason="Explicit address request/watch component implemented; no Ready/activation until verifier, registration workflow, complete operational watch and fee-service/reconciliation gates qualified")
        if address_registration:
            address_manifest.update(address_registration_code_implemented=True,
                address_registration_evidence=str(address_registration_path.relative_to(ROOT)),
                address_registration_source_snapshot=source_snapshot(address_registration),ready=False,address_client_enabled=False,
                reason="V2 request commitments, public EVM_AUTH registration and bounded recovery implemented; actual verifier QUALIFIED=false blocks activation. Operational watch/registration, fee service/reconciliation and combined E2E remain unqualified")
        if address_spv:
            address_manifest.update(address_spv_worker_code_implemented=True,address_spv_worker_evidence=str(address_spv_path.relative_to(ROOT)),
                address_spv_source_snapshot=source_snapshot(address_spv),ready=False,address_client_enabled=False,
                reason="Registered-address worker and admission/quote/payment code integrated; actual verifier QUALIFIED=false blocks activation. Operational qualification, BTC reserve/reconciliation and combined live E2E remain pending")
        if fee:
            address_manifest.update(btc_fee_reserve_code_implemented=True,reconciliation_code_implemented=True,
                btc_fee_reserve_evidence=str(fee_path.relative_to(ROOT)),btc_fee_source_snapshot=source_snapshot(fee),ready=False,
                reason="BTC reserve/reconciliation code integrated; actual derivation verifier remains QUALIFIED=false, no production activation or combined operational qualification")
        if withdrawal.get('personal_fee_code_implemented'):
            address_manifest.update(withdrawal_fee_code_implemented=True,withdrawal_fee_evidence=str(withdrawal_path.relative_to(ROOT)),
                withdrawal_fee_source_snapshot=source_snapshot(withdrawal),ready=False,address_client_enabled=False)
        write("address-deposit-manifest.json", address_manifest)
    address_vectors = load("address-deposit-test-vectors.json")
    if address_vectors is not None and derivation:
        address_vectors["native_test_only_vectors"] = "rust/zkspv/derivation-qualification/public-vector.json"
        address_vectors["native_test_only_evidence"] = str(derivation_path.relative_to(ROOT))
        address_vectors["reason"] = "Native intent/derivation vector exists; on-chain verifier, per-input Bitcoin signatures and production address qualification remain absent"
        if address_input:
            address_vectors.update(candidate_evm_vectors="web3/test/fixtures/deposit-derivation-v1.json",
                per_input_crypto_evidence=str(address_input_path.relative_to(ROOT)),
                reason="Native mixed BASE/DERIVED Bitcoin sighashes/signatures and 16 matching EVM vectors exist; Core acceptance, canonical registry/live signer integration and production qualification remain pending")
        if address_registry:
            address_vectors.update(registry_state_machine_evidence=str(address_registry_path.relative_to(ROOT)),
                reason="Native/EVM arithmetic vectors and contract registry/mint/status checks exist; real Core/SPV/FROST address contour, cross-language operation vectors and production qualification remain pending")
        if address_mint:
            address_vectors.update(typed_mint_operation_vectors="web3/test/fixtures/address-mint-operations-v1.json",
                mint_client_evidence=str(address_mint_path.relative_to(ROOT)),
                reason="Shared Python/Solidity address and custody operation vectors plus focused state machines; complete address Core/SPV/FROST contour and production qualification pending")
        if address_signing:
            address_vectors.update(bitcoin_signer_evidence=str(address_signing_path.relative_to(ROOT)),
                reason="Native DKG and mixed BASE/DERIVED client signatures independently checked by libsecp256k1, plus journal/recovery and typed mint vectors; Core/EVM/qualification boundaries are fixtures and full production qualification pending")
        write("address-deposit-test-vectors.json", address_vectors)

    baseline = load("baseline-snapshot.json")
    paths = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    original = baseline["tracked_sha256_at_commit"]
    current = {}
    for name in sorted(set(paths)):
        if not name or name.startswith("ops/artifacts/") or "__pycache__" in name or name.endswith(".pyc"):
            continue
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            current[name] = sha(path)
    added = {p: h for p, h in current.items() if p not in original}
    changed = {p: {"before_sha256": original[p], "after_sha256": h} for p, h in current.items() if p in original and original[p] != h}
    deleted = [p for p in original if not p.startswith("ops/artifacts/") and not (ROOT / p).exists()]
    write("preservation-manifest.json", {"base_commit": BASE, "final_commit": None,
        "working_tree_result": True, "modified_sources": changed, "added_sources": added, "deleted_sources": deleted,
        "reason": "Minimal callback safety/client recovery/funding additions plus local ZK qualification/planning/evidence; no independent consensus replacement",
        "source_configuration_sha256": {"before": original["web3/foundry.toml"], "after": sha(ROOT / "web3/foundry.toml")},
        "public_secrets_included": False, "scope": "Source/dependency files; run artifacts hashed separately in decision"})

    runs = {"planner": junit("evidence/planner-tests.xml"), "zkspv_local": junit("evidence/zkspv-local-tests.xml"),
            "signer": junit("signer-client-tests.xml"),
            "selected_bitcoin_e2e": junit("bitcoin-client-e2e.xml")}
    previous_signer_snapshot = source_snapshot(load("signer-integration-report.json", {}))
    for name in ("signer", "selected_bitcoin_e2e"):
        runs[name]["source_snapshot"] = previous_signer_snapshot
        if previous_signer_snapshot["status"] != "MATCH" and runs[name]["status"] == "PASS":
            runs[name]["status"] = "HISTORICAL_PASS"
    solidity = load("solidity-suites.json")
    if solidity is None:
        solidity = load("evidence/solidity-suites.json")
    if solidity is None:
        solidity = load("solidity-suite-results.json")
    runs["solidity"] = {"status": "HISTORICAL_PASS" if solidity and solidity.get("status") == "passed" else "UNKNOWN",
                        "passed": solidity.get("passed") if solidity else None,
                        "scope": "Recorded pre-admission suites; current incremental suites are reported separately"}
    for name in ("tests", "receipts"):
        item = funding.get(name, {})
        runs["funding_parser_" + name] = {
            "status": item.get("status", "UNKNOWN"), "passed": item.get("passed", item.get("cases")),
            "scope": "Local parsing only; no custody/storage/SPV/mint integration or production target qualification",
            "evidence": "funding-parser-report.json", "source_snapshot": source_snapshot(funding)}
        if runs["funding_parser_" + name]["source_snapshot"]["status"] != "MATCH":
            runs["funding_parser_" + name]["status"] = "HISTORICAL_PASS" if item.get("status") == "PASS" else "UNKNOWN"
    runs["mint_admission_contract"] = {
        "status": admission.get("status", "UNKNOWN"), "passed": admission.get("tests", {}).get("passed"),
        "evidence": "mint-admission-contract-report.json", "source_snapshot": source_snapshot(admission),
        "scope": "Contract state-machine checks; synthetic state is not cryptographic FROST evidence"}
    runs["mint_admission_client"] = {
        "status": admission_client.get("evidence", {}).get("python_full", {}).get("status", "UNKNOWN"),
        "passed": admission_client.get("evidence", {}).get("python_full", {}).get("tests"),
        "evidence": "mint-admission-client-report.json", "source_snapshot": source_snapshot(admission_client),
        "scope": "Full current signer unit suite, including canonical admission and bootstrap receipt regression"}
    admission_e2e = admission_client.get("evidence", {}).get("real_bitcoin_e2e", {})
    runs["mint_admission_e2e"] = {
        "status": admission_e2e.get("status", "UNKNOWN"), "passed": admission_e2e.get("tests"),
        "evidence": "mint-admission-client-report.json",
        "scope": "Actual local bootstrap, two mints and operator transfer; later receipt-check metadata correction validated separately on that graph; see exact run source snapshot"}
    native_commands = derivation.get("commands", [])
    derivation_snapshot = source_snapshot({"source_sha256": {
        item["path"]: item["sha256"] for item in derivation.get("artifacts", [])
        if item["path"].startswith("frost-crypto/")}})
    runs["native_derivation"] = {
        "status": ("PASS" if derivation_snapshot["status"] == "MATCH" else "HISTORICAL_PASS")
            if native_commands and all(c.get("exit_code") == 0 for c in native_commands) else "UNKNOWN",
        "source_snapshot": derivation_snapshot,
        "passed": next((c.get("passed") for c in native_commands if c.get("passed")), None),
        "evidence": str(derivation_path.relative_to(ROOT)),
        "scope": "9 existing + 6 test-only native cases; ignored fixture generator, no Bitcoin transaction/on-chain qualification"}
    for name, section in (("gas_treasury_contract", "solidity"), ("gas_treasury_client", "signer_tests"),
                          ("gas_treasury_local_receipts", "local_receipt_evidence")):
        item = gas.get(section, {})
        snapshot = source_snapshot(gas)
        status = item.get("status", "UNKNOWN")
        if status == "PASS" and snapshot["status"] != "MATCH":
            status = "HISTORICAL_PASS"
        runs[name] = {"status": status, "passed": item.get("tests"), "source_snapshot": snapshot,
                      "evidence": "gas-treasury-component-report.json",
                      "scope": "Native accounting/payment/client component; local synthetic authority/state, no real SPV publication"}
    for name, section in (("gas_governance_signer", "signer_tests"), ("gas_governance_threshold_e2e", "local_e2e")):
        item = gas_governance.get(section, {})
        snapshot = source_snapshot(gas_governance)
        status = item.get("status", "UNKNOWN")
        if status == "PASS" and snapshot["status"] != "MATCH":
            status = "HISTORICAL_PASS"
        runs[name] = {"status": status, "passed": item.get("tests"), "source_snapshot": snapshot,
                      "evidence": "gas-governance-report.json",
                      "scope": "Enrolled real DKG/FROST configuration/rotation/pause; SPV state is explicitly synthetic, no production activation"}
    for name, section in (("gas_caller_signer", "signer_tests"), ("gas_caller_local_receipts", "local_e2e")):
        item = gas_caller.get(section, {})
        status = item.get("status", "UNKNOWN")
        if status == "PASS" and caller_snapshot["status"] != "MATCH":
            status = "HISTORICAL_PASS"
        runs[name] = {"status": status, "passed": item.get("tests"), "source_snapshot": caller_snapshot,
                      "evidence": "gas-caller-report.json",
                      "scope": "Bounded local permissionless caller; synthetic authority/state, no SPV publication"}
    patch_path = ROOT / "rust/zkspv/patches/report.json"
    corrections = json.loads(patch_path.read_text()) if patch_path.is_file() else {}
    validation = corrections.get("validation", {})
    runs["reference_arithmetic_patches"] = {
        "status": "PASS" if corrections.get("status") == "LOCAL_ARITHMETIC_REGRESSION_PASS" else "UNKNOWN",
        "passed": sum(validation.get(name, 0) for name in
                      ("debug_tests_passed", "release_tests_passed", "application_tests_passed")) or None,
        "scope": "Corrected extracted functions/application checks; not full upstream or real proof",
        "evidence": "rust/zkspv/patches/report.json"}
    header_path = ROOT / "rust/zkspv/header-qualification/report.json"
    header = json.loads(header_path.read_text()) if header_path.is_file() else {}
    header_snapshot = source_snapshot(header)
    header_commands = header.get("commands", [])
    header_status = ("PASS" if header.get("status") == "LOCAL_HEADER_TRANSITION_PASS"
                     and len(header_commands) == 2 and all(item.get("exit_code") == 0 for item in header_commands)
                     else "UNKNOWN")
    if header_status == "PASS" and header_snapshot["status"] != "MATCH":
        header_status = "HISTORICAL_PASS"
    runs["historical_header_transitions"] = {
        "status": header_status, "passed": sum(item.get("tests_passed", 0) for item in header_commands),
        "source_snapshot": header_snapshot, "evidence": str(header_path.relative_to(ROOT)),
        "scope": "Complete extracted transitions on 6111 historical headers, Rust 1.88.0; not full upstream native suite, fork choice or real proof"}
    runs["header_qualification_local_regression"] = junit("header-qualification-regression.xml")
    core_path = ROOT / "rust/zkspv/header-qualification/core-target-report.json"
    core = json.loads(core_path.read_text()) if core_path.is_file() else {}
    core_snapshot = source_snapshot(core)
    core_status = core.get("status", "UNKNOWN")
    if core_status == "PASS" and core_snapshot["status"] != "MATCH":
        core_status = "HISTORICAL_PASS"
    vector = core_path.parent / core.get("vector_file", "missing")
    if core_status == "PASS" and (not vector.is_file() or sha(vector) != core.get("vector_sha256")):
        core_status = "HISTORICAL_PASS"
    runs["compact_target_core_reference"] = {
        "status": core_status, "compact_inputs": core.get("compact_inputs"),
        "reference_results": core.get("comparisons"), "source_snapshot": core_snapshot,
        "evidence": str(core_path.relative_to(ROOT)),
        "scope": "Executed pinned Core arithmetic oracle; Rust assertions recorded in header/native runs, no full node or exhaustive input coverage"}
    for name in ("original", "corrected"):
        native_path = ROOT / f"rust/zkspv/header-qualification/{name}-native-report.json"
        native = json.loads(native_path.read_text()) if native_path.is_file() else {}
        snapshot = source_snapshot(native)
        status = native.get("status", "UNKNOWN")
        if status == "PASS" and snapshot["status"] != "MATCH":
            status = "HISTORICAL_PASS"
        runs[name + "_native_header_tests"] = {
            "status": status, "passed": native.get("tests_passed"), "source_snapshot": snapshot,
            "evidence": str(native_path.relative_to(ROOT)),
            "scope": "Actual bitcoin-da native verifier tests; no guest/proof or full DA-service suite qualification"}
    time_path = ROOT / "rust/zkspv/time-policy/report.json"
    time_policy = json.loads(time_path.read_text()) if time_path.is_file() else {}
    time_snapshot = source_snapshot(time_policy)
    time_status = "PASS" if time_policy.get("status") == "LOCAL_COMPONENT_PASS" else "UNKNOWN"
    if time_status == "PASS" and time_snapshot["status"] != "MATCH":
        time_status = "HISTORICAL_PASS"
    time_runs = time_policy.get("runs", {})
    for suffix, selected, scope in (
        ("rust", ("debug", "release"), "Time summary/ABI/chunk component tests; callback is not itself a Bitcoin verifier"),
        ("native", ("native",), "Five time-policy tests with actual mainnet verifier plus ten retained verifier tests; no proof binding"),
        ("solidity", ("solidity",), "EVM acceptance-time/ABI component; nine tests including fuzz and Rust fixtures, no production state contract"),
    ):
        selected_runs = [time_runs.get(name, {}) for name in selected]
        status = time_status if all(item.get("exit_code") == 0 for item in selected_runs) else "UNKNOWN"
        runs["header_time_policy_" + suffix] = {
            "status": status, "passed": sum(item.get("tests_passed", 0) for item in selected_runs),
            "source_snapshot": time_snapshot, "evidence": str(time_path.relative_to(ROOT)), "scope": scope}
    runs["header_time_policy_local_regression"] = junit("header-time-policy-regression.xml")
    fork_path = ROOT / "rust/zkspv/fork-policy/report.json"
    fork_policy = json.loads(fork_path.read_text()) if fork_path.is_file() else {}
    fork_snapshot = source_snapshot(fork_policy)
    fork_status = "PASS" if fork_policy.get("status") == "LOCAL_COMPONENT_PASS" else "UNKNOWN"
    if fork_status == "PASS" and fork_snapshot["status"] != "MATCH":
        fork_status = "HISTORICAL_PASS"
    fork_runs = fork_policy.get("runs", {})
    for suffix, selected, scope in (
        ("rust", ("debug", "release"), "Fork/LCA/halt/history/recovery component tests; synthetic graph binding is not PoW qualification"),
        ("native", ("native",), "Four historical mainnet fork/history integrations plus fifteen retained verifier/time tests; no real ZK proof"),
        ("solidity", ("solidity",), "505 selection comparisons, nine membership fixtures and actual relay batch semantics; synthetic graph setup is not gas/proof qualification"),
    ):
        selected_runs = [fork_runs.get(name, {}) for name in selected]
        status = fork_status if all(item.get("exit_code") == 0 for item in selected_runs) else "UNKNOWN"
        runs["fork_policy_" + suffix] = {
            "status": status, "passed": sum(item.get("tests_passed", 0) for item in selected_runs),
            "source_snapshot": fork_snapshot, "evidence": str(fork_path.relative_to(ROOT)), "scope": scope}
    runs["fork_policy_local_regression"] = junit("fork-policy-regression.xml")
    checkpoint_path = ROOT / "rust/zkspv/checkpoint-policy/report.json"
    checkpoint_policy = json.loads(checkpoint_path.read_text()) if checkpoint_path.is_file() else {}
    checkpoint_snapshot = source_snapshot(checkpoint_policy)
    checkpoint_status = "PASS" if checkpoint_policy.get("status") == "LOCAL_COMPONENT_PASS" else "UNKNOWN"
    if checkpoint_status == "PASS" and checkpoint_snapshot["status"] != "MATCH":
        checkpoint_status = "HISTORICAL_PASS"
    checkpoint_runs = checkpoint_policy.get("runs", {})
    for suffix, selected, scope in (
        ("rust", ("debug", "release"), "Validated mainnet checkpoint, linked MTP context and exact native context binding; earlier chainwork remains trusted"),
        ("native", ("native",), "Five checkpoint integrations plus nineteen retained tests; 4032 descendants, two retargets and full replay; no real proof"),
        ("solidity", ("solidity",), "Three shared mainnet contexts and constructor/rejection comparisons with existing relay; no production state activation"),
    ):
        selected_runs = [checkpoint_runs.get(name, {}) for name in selected]
        status = checkpoint_status if all(item.get("exit_code") == 0 for item in selected_runs) else "UNKNOWN"
        runs["checkpoint_policy_" + suffix] = {
            "status": status, "passed": sum(item.get("tests_passed", 0) for item in selected_runs),
            "source_snapshot": checkpoint_snapshot, "evidence": str(checkpoint_path.relative_to(ROOT)), "scope": scope}
    runs["checkpoint_policy_local_regression"] = junit("checkpoint-policy-regression.xml")
    transition_path = ROOT / "rust/zkspv/transition-policy/report.json"
    transition_policy = json.loads(transition_path.read_text()) if transition_path.is_file() else {}
    transition_snapshot = source_snapshot(transition_policy)
    transition_status = "PASS" if transition_policy.get("status") == "LOCAL_COMPONENT_PASS" else "UNKNOWN"
    if transition_status == "PASS" and transition_snapshot["status"] != "MATCH":
        transition_status = "HISTORICAL_PASS"
    transition_runs = transition_policy.get("runs", {})
    for suffix, selected, scope in (
        ("rust", ("debug", "release"), "Integrated checkpoint/history/time kernel and complete journal; synthetic branch tests are not PoW or proof qualification"),
        ("native", ("native",), "Four complete-transition tests plus twenty-four retained tests with actual pinned verifier, including retarget resume; no real proof"),
        ("solidity", ("solidity",), "Six shared journals, strict decoding, EVM domain/current-state/time/halt checks; unauthenticated harness, no production state"),
    ):
        selected_runs = [transition_runs.get(name, {}) for name in selected]
        status = transition_status if all(item.get("exit_code") == 0 for item in selected_runs) else "UNKNOWN"
        runs["transition_policy_" + suffix] = {
            "status": status, "passed": sum(item.get("tests_passed", 0) for item in selected_runs),
            "source_snapshot": transition_snapshot, "evidence": str(transition_path.relative_to(ROOT)), "scope": scope}
    runs["transition_policy_local_regression"] = junit("transition-policy-regression.xml")
    resume_path = ROOT / "rust/zkspv/resume-policy/report.json"
    resume_policy = json.loads(resume_path.read_text()) if resume_path.is_file() else {}
    resume_snapshot = source_snapshot(resume_policy)
    resume_status = "PASS" if resume_policy.get("status") == "LOCAL_COMPONENT_PASS" else "UNKNOWN"
    if resume_status == "PASS" and resume_snapshot["status"] != "MATCH":
        resume_status = "HISTORICAL_PASS"
    resume_runs = resume_policy.get("runs", {})
    for suffix, selected, scope in (
        ("rust", ("debug", "release"), "V2 indexed context, exact LCA, bounded resume/codec and V1 differential; synthetic graph tests are not PoW qualification"),
        ("native", ("native",), "Four bounded resume tests plus twenty-eight retained native tests; actual retarget resume uses one new verification and 31317 witness bytes; no ZK proof"),
    ):
        selected_runs = [resume_runs.get(name, {}) for name in selected]
        status = resume_status if all(item.get("exit_code") == 0 for item in selected_runs) else "UNKNOWN"
        runs["resume_policy_" + suffix] = {
            "status": status, "passed": sum(item.get("tests_passed", 0) for item in selected_runs),
            "source_snapshot": resume_snapshot, "evidence": str(resume_path.relative_to(ROOT)), "scope": scope}
    runs["resume_policy_local_regression"] = junit("resume-policy-regression.xml")
    v2_path = ROOT / "rust/zkspv/resume-transition/report.json"
    v2 = json.loads(v2_path.read_text()) if v2_path.is_file() else {}
    v2_snapshot = source_snapshot(v2)
    v2_status = "PASS" if v2.get("status") == "LOCAL_COMPONENT_PASS" else "UNKNOWN"
    if v2_status == "PASS" and v2_snapshot["status"] != "MATCH":
        v2_status = "HISTORICAL_PASS"
    v2_runs = v2.get("runs", {})
    for suffix, selected, scope in (
        ("rust", ("debug", "release"), "V2 journal and bounded runtime-neutral input codec; no RISC Zero guest or proof"),
        ("native", ("native",), "Four V2 input/journal tests plus thirty-two retained native tests; two real mainnet retargets, one new consensus call each"),
        ("solidity", ("solidity",), "Six shared V2 journals; strict decoding, independent old/new V2 state hashing, deployment/time/replay checks; unauthenticated test harness"),
    ):
        selected_runs = [v2_runs.get(name, {}) for name in selected]
        status = v2_status if all(item.get("exit_code") == 0 for item in selected_runs) else "UNKNOWN"
        runs["resume_transition_" + suffix] = {
            "status": status, "passed": sum(item.get("tests_passed", 0) for item in selected_runs),
            "source_snapshot": v2_snapshot, "evidence": str(v2_path.relative_to(ROOT)), "scope": scope}
    runs["resume_transition_local_regression"] = junit("resume-transition-regression.xml")
    state_path = ROOT / "rust/zkspv/state-adapter/report.json"
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    state_snapshot = source_snapshot(state)
    state_status = "PASS" if state.get("status") == "LOCAL_COMPONENT_PASS" else "UNKNOWN"
    if state_status == "PASS" and state_snapshot["status"] != "MATCH":
        state_status = "HISTORICAL_PASS"
    state_runs = state.get("runs", {})
    for suffix, selected, scope in (
        ("vectors", ("debug_vectors", "release_vectors"), "Three real checkpoint initializations and ten synthetic state/membership vectors; no proof"),
        ("solidity", ("solidity",), "Immutable proof-only state, two-bound Fresh and root-bound monetary adapter; positive state tests mock only the exact proof call"),
        ("ordinary_build", ("ordinary_build",), "Unmodified pinned real verifier plus new state/adapter; separate normal bytecode build and size checks"),
    ):
        selected_runs = [state_runs.get(name, {}) for name in selected]
        status = state_status if all(item.get("exit_code") == 0 for item in selected_runs) else "UNKNOWN"
        runs["state_adapter_" + suffix] = {
            "status": status, "passed": sum(item.get("tests_passed", 0) for item in selected_runs),
            "source_snapshot": state_snapshot, "evidence": str(state_path.relative_to(ROOT)), "scope": scope}
    local_receipts = state.get("local_deployment_receipts", {})
    runs["state_adapter_local_receipts"] = {
        "status": state_status if local_receipts.get("status") == "PASS_LOCAL_EVM" else "UNKNOWN",
        "source_snapshot": state_snapshot, "evidence": "rust/zkspv/state-adapter/evidence/local-receipts.json",
        "deployments": len(local_receipts.get("deployments", [])),
        "scope": "Three isolated Anvil state/verifier/adapter creations and actual-verifier rejection receipts; no positive proof receipt"}
    runs["state_adapter_local_regression"] = junit("state-adapter-regression.xml")
    guest_path = ROOT / "rust/zkspv/guest-integration/report.json"
    guest = json.loads(guest_path.read_text()) if guest_path.is_file() else {}
    guest_snapshot = source_snapshot(guest)
    guest_status = "PASS" if guest.get("status") == "LOCAL_GUEST_BUILD_PASS" else "UNKNOWN"
    if guest_status == "PASS" and guest_snapshot["status"] != "MATCH":
        guest_status = "HISTORICAL_PASS"
    for suffix, selected, scope in (
        ("native", ("native_debug", "native_release"), "Static public mainnet verifier; 4032 real descendants, two retargets, bounded input and exact journal"),
        ("build", ("guest", "image_tool"), "Real RISC-V guest, pinned default kernel and independently matching image ID; build alone is not proof evidence"),
    ):
        selected_runs = [guest.get("builds", {}).get(name, {}) for name in selected]
        runs["guest_" + suffix] = dict(status=guest_status if all(x.get("exit_code") == 0 for x in selected_runs) else "UNKNOWN",
            passed=sum(x.get("tests_passed", 0) for x in selected_runs), source_snapshot=guest_snapshot,
            evidence=str(guest_path.relative_to(ROOT)), scope=scope)
    proof_path = ROOT / "rust/zkspv/guest-integration/evidence/local-proof/proof-report.json"
    proof = json.loads(proof_path.read_text()) if proof_path.is_file() else {}
    proof_bound = guest_path.is_file() and proof.get("build_report_sha256") == sha(guest_path)
    proof_artifacts = proof.get("artifacts", {})
    proof_bound = proof_bound and bool(proof_artifacts) and all((proof_path.parent / name).is_file()
        and sha(proof_path.parent / name) == item.get("sha256") for name, item in proof_artifacts.items())
    proof_snapshot = source_snapshot(proof)
    runs["guest_local_stark"] = dict(status=("PASS" if guest_status == "PASS" and proof_snapshot["status"] == "MATCH" else "HISTORICAL_PASS")
        if proof.get("status") == "LOCAL_STARK_PASS" and proof_bound else "UNKNOWN",
        evidence=str(proof_path.relative_to(ROOT)), source_snapshot=proof_snapshot,
        scope="Separate network-denied composite STARK run; no Groth16/EVM positive verification or production activation")
    runs["guest_local_regression"] = junit("guest-integration-regression.xml")
    groth_path = ROOT / "rust/zkspv/groth16-integration/report.json"
    groth = json.loads(groth_path.read_text()) if groth_path.is_file() else {}
    groth_snapshot = source_snapshot(groth)
    groth_artifacts = groth.get("artifact_sha256", {})
    groth_bound = bool(groth_artifacts) and all((ROOT / name).is_file() and sha(ROOT / name) == digest
                                              for name, digest in groth_artifacts.items())
    for stage, label in (("succinct", "succinct_compression"), ("groth16", "groth16_wrapping"), ("evm_verifier", "groth16_evm_verifier")):
        runs[label] = dict(status=("PASS" if groth_snapshot["status"] == "MATCH" else "HISTORICAL_PASS")
            if groth.get("status") == "LOCAL_GROTH16_EVM_PASS" and groth_bound and groth.get("stages", {}).get(stage) == "PASS" else "UNKNOWN",
            source_snapshot=groth_snapshot, evidence=str(groth_path.relative_to(ROOT)),
            scope="One mainnet append; native ARM64 Docker Groth16 and ordinary local EVM verifier; no actual state transition or production activation")
    runs["groth16_bridge_regression"] = junit("groth16-bridge-regression.xml")
    acceptance_path = ROOT / "rust/zkspv/state-proof-integration/report.json"
    acceptance = json.loads(acceptance_path.read_text()) if acceptance_path.is_file() else {}
    acceptance_snapshot = source_snapshot(acceptance)
    acceptance_artifacts = acceptance.get("artifact_sha256", {})
    acceptance_bound = bool(acceptance_artifacts) and all((ROOT / name).is_file() and sha(ROOT / name) == digest
                                                        for name, digest in acceptance_artifacts.items())
    acceptance_pass = (acceptance.get("status") == "LOCAL_STATE_PROOF_PASS" and acceptance_bound
                       and acceptance.get("actual_state_transition") == "PASS")
    runs["state_proof_acceptance"] = dict(
        status=("PASS" if acceptance_snapshot["status"] == "MATCH" else "HISTORICAL_PASS") if acceptance_pass else "UNKNOWN",
        source_snapshot=acceptance_snapshot, evidence=str(acceptance_path.relative_to(ROOT)),
        scope="One real state-address-bound Groth16 transition, replay/domain rejection, header materialization and Fresh expiry on historical-clock Anvil; no production qualification")
    runs["state_proof_regression"] = junit("state-proof-regression.xml")
    scenario_path = ROOT / "rust/zkspv/proof-scenarios/report.json"
    scenario = json.loads(scenario_path.read_text()) if scenario_path.is_file() else {}
    scenario_snapshot = source_snapshot(scenario)
    scenario_artifacts = scenario.get("artifact_sha256", {})
    scenario_bound = bool(scenario_artifacts) and all((ROOT / name).is_file() and sha(ROOT / name) == digest
                                                    for name, digest in scenario_artifacts.items())
    for name, label in (("retarget-34272", "mainnet_retarget_proof"), ("fork-work-reorg100", "test_model_fork_proof"),
                        ("halt101-prefix", "test_model_halt_proof")):
        entry = scenario.get("scenarios", {}).get(name, {})
        runs[label] = dict(status=("PASS" if scenario_snapshot["status"] == "MATCH" else "HISTORICAL_PASS")
            if scenario.get("status") == "LOCAL_SCENARIO_PROOFS_PASS" and scenario_bound and entry.get("status", "").startswith("PASS_") else "UNKNOWN",
            source_snapshot=scenario_snapshot, evidence=str(scenario_path.relative_to(ROOT)),
            scope=entry.get("starting_state_scope", "Awaiting completed source-bound proof; TEST_ONLY models do not qualify mainnet fork/halt"))
    runs["proof_scenario_regression"] = junit("proof-scenario-regression.xml")
    deposit_path = ROOT / "rust/zkspv/deposit-integration/report.json"
    deposit = json.loads(deposit_path.read_text()) if deposit_path.is_file() else {}
    deposit_snapshot = source_snapshot(deposit)
    deposit_artifacts = deposit.get("artifact_sha256", {})
    deposit_bound = bool(deposit_artifacts) and all((ROOT / name).is_file() and sha(ROOT / name) == digest
                                                  for name, digest in deposit_artifacts.items())
    runs["regtest_zk_deposit"] = dict(
        status=("PASS" if deposit_snapshot["status"] == "MATCH" else "HISTORICAL_PASS")
        if deposit.get("status") == "LOCAL_REGTEST_ZK_DEPOSIT_PASS" and deposit_bound else "UNKNOWN",
        source_snapshot=deposit_snapshot, evidence=str(deposit_path.relative_to(ROOT)),
        scope="Real Core regtest funding, PoW and Groth16; separate TEST_ONLY linear guest/state; ordinary ZkSpvAdapter, BitcoinDepositRegistry, MiningPoolV1 and real DKG/FROST. Not mainnet consumer/bootstrap qualification")
    runs["deposit_integration_regression"] = junit("deposit-integration-regression.xml")
    bootstrap_path = ROOT / "ops/artifacts/zkspv/bootstrap-integration/report.json"
    bootstrap = json.loads(bootstrap_path.read_text()) if bootstrap_path.is_file() else {}
    bootstrap_snapshot = source_snapshot(bootstrap)
    runs["mainnet_zk_bootstrap_consumer"] = dict(
        status=("PASS" if bootstrap_snapshot["status"] == "MATCH" else "HISTORICAL_PASS")
        if bootstrap.get("status") == "LOCAL_MAINNET_ZK_BOOTSTRAP_CONSUMER_PASS" else "UNKNOWN",
        source_snapshot=bootstrap_snapshot, evidence=str(bootstrap_path.relative_to(ROOT)),
        scope="Configurable production-code bootstrap slice and ordinary consumer graph; genuine mainnet Groth16 on historical-clock Anvil, real 3-of-4 DKG/FROST. No live Core, public deployment or mainnet monetary mint")
    runs["zk_bootstrap_client_regression"] = junit("bootstrap-integration/unit-tests.xml")
    runs["zk_bootstrap_client_regression"]["source_snapshot"] = bootstrap_snapshot
    if runs["zk_bootstrap_client_regression"]["status"] == "PASS" and bootstrap_snapshot["status"] != "MATCH":
        runs["zk_bootstrap_client_regression"]["status"] = "HISTORICAL_PASS"
    runs["zk_bootstrap_proof_regression"] = junit("bootstrap-integration/proof-regression.xml")
    worker_path = OUT / "worker-integration/report.json"
    worker = json.loads(worker_path.read_text()) if worker_path.is_file() else {}
    worker_snapshot = source_snapshot(worker)
    runs["consumer_driven_zk_worker"] = dict(
        status=("PASS" if worker_snapshot["status"] == "MATCH" else "HISTORICAL_PASS")
        if worker.get("status") == "LOCAL_AUTOMATED_ZK_STATE_PASS" else "UNKNOWN",
        source_snapshot=worker_snapshot, evidence=str(worker_path.relative_to(ROOT)),
        scope="Native witness and automatic durable EVM update using independently verified genuine cached mainnet receipt; simulated Core/prevalidated inbox. No fresh proving, full catch-up, live consumer or public deployment")
    runs["zk_worker_client_regression"] = junit("worker-integration/unit-tests.xml")
    runs["zk_worker_client_regression"]["source_snapshot"] = worker_snapshot
    if runs["zk_worker_client_regression"]["status"] == "PASS" and worker_snapshot["status"] != "MATCH":
        runs["zk_worker_client_regression"]["status"] = "HISTORICAL_PASS"
    runs["zk_worker_proof_regression"] = junit("worker-integration/proof-regression.xml")
    service_path = OUT / "worker-integration/service-code-report.json"
    service = json.loads(service_path.read_text()) if service_path.is_file() else {}
    runs["zk_worker_service_code"] = dict(status=service.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(service), evidence=str(service_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/service-code-tests.xml"),
        scope="Code for mainnet payment state view, inactive bootstrap entry, issuer admission, paid/own funding and executable heartbeat. Combined proving/payment/consumer E2E deliberately pending")
    transport_path = OUT / "worker-integration/transport-code-report.json"
    transport = json.loads(transport_path.read_text()) if transport_path.is_file() else {}
    runs["encrypted_spv_transport_code"] = dict(status=transport.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(transport), evidence=str(transport_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/transport-code-tests.xml"),
        scope="SPVMessageBoard, real HPKE checks, private request/ACK/status adapters, canonical retry/reorg handling and compatible execution groups. Chain/provenance mocks in unit checks; combined on-chain/proving/consumer E2E pending")
    automation_path = OUT / "worker-integration/automation-archive-report.json"
    automation = json.loads(automation_path.read_text()) if automation_path.is_file() else {}
    runs['service_automation_archive_code'] = dict(status=automation.get('status','UNKNOWN'),
        source_snapshot=source_snapshot(automation),evidence=str(automation_path.relative_to(ROOT)),
        focused_checks=junit('worker-integration/automation-archive-tests.xml'),
        scope='Opt-in admission/quote/payer automation plus permanent archive, resident native preparation and replay restore. Real native fixtures; mocked payment/EVM boundary. Combined real-value/proving/consumer E2E deferred')
    runs["address_input_crypto_code"] = dict(status=address_input.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(address_input), evidence=str(address_input_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/address-input-tests.xml"),
        scope="Native intent codec and mixed BASE/DERIVED actual Bitcoin sighashes/threshold signatures, independent libsecp verification, candidate EVM derivation verifier. No Core acceptance, canonical address registry, live signer integration, address mint or Ready")
    runs["address_registry_contract_code"] = dict(status=address_registry.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(address_registry), evidence=str(address_registry_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/address-registry-client-tests.xml"),
        scope="Immutable candidate registry, global script namespace/shared backing/address mint and current custody/derivation fences, with real contract logic and explicit DKG/approval/SPV/qualification fixtures. Legacy clients refuse unsupported new graph; no Ready or combined E2E")
    runs["address_mint_client_code"] = dict(status=address_mint.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(address_mint), evidence=str(address_mint_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/address-mint-client-tests.xml"),
        scope="Mint-only trust, immutable generation-aware admission, funding/Core review before private work, exact receipt recovery and shared operation vectors. Native codec/derivation and Forge logic are real; positive Core/EVM/provenance and DKG/approval/SPV/qualification boundaries use explicit fixtures. No combined E2E or Ready")
    runs["address_bitcoin_signer_code"] = dict(status=address_signing.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(address_signing), evidence=str(address_signing_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/address-signing-client-tests.xml"),
        scope="Explicit V3 plans, native DKG/mixed-input FROST signatures independently verified by libsecp256k1, prepare/broadcast routing, canonical bindings/generations and shared journal crash/restart/replay checks. EVM/Core/provenance/qualification admission fixtures; no live Core acceptance, combined E2E or Ready")
    runs["address_request_watch_code"] = dict(status=address_watch.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(address_watch),evidence=str(address_watch_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/address-watch-client-tests.xml"),
        scope="Separate retail address ciphertext board/HPKE client, durable ACK, genesis watch plan, pinned raw-block codec and SQLite reorg/recovery/public binding reconstruction. Actual native crypto/codec and contract logic; EVM/Core/DKG/qualification fixtures, no PoW/operational/combined E2E or Ready")
    runs["address_registration_code"] = dict(status=address_registration.get("status", "UNKNOWN"),
        source_snapshot=source_snapshot(address_registration),evidence=str(address_registration_path.relative_to(ROOT)),
        focused_checks=junit("worker-integration/address-registration-client-tests.xml"),
        scope="V2 public requester commitment, native immutable intents, typed DKG-bound registration, before-private-work guards, exact-binding completion and bounded recovery. Real HPKE/SQLite/contract/hash logic; EVM/DKG/provenance/qualification fixtures. No combined live E2E or Ready")
    runs["address_spv_worker_code"] = dict(status=address_spv.get("status","UNKNOWN"),source_snapshot=source_snapshot(address_spv),
        evidence=str(address_spv_path.relative_to(ROOT)),focused_checks=junit("worker-integration/address-spv-worker-tests.xml"),
        scope="V5/V3 registered-address needs, native funding/inclusion, watch/consumer hooks, automatic admission/quote/payment and stable shared paid coverage. Native crypto/SQLite and budgets real; Core/EVM/qualification boundaries explicit fixtures. No fresh proving, live payment or combined E2E")
    runs['btc_fee_reserve_code']=dict(status=fee.get('status','UNKNOWN'),source_snapshot=source_snapshot(fee),
        evidence=str(fee_path.relative_to(ROOT)),focused_checks=junit('worker-integration/btc-fee-reserve-tests.xml'),
        scope="Canonical graph-scoped free/locked/promised credits, donor BIP340, principal-preserving old-winner/RBF settlement, typed FROST intents, complete native watch/Core reconciliation and shared-journal recovery. Native crypto/SQLite/contract logic real; Core/EVM/DKG/SPV/qualification fixtures. No long combined E2E or activation")
    if withdrawal.get('personal_fee_code_implemented'):
        runs['withdrawal_fee_code']=dict(status=withdrawal.get('status','UNKNOWN'),source_snapshot=source_snapshot(withdrawal),
            evidence=str(withdrawal_path.relative_to(ROOT)),focused_checks=junit('worker-integration/withdrawal-subsidy-tests.xml'),
            scope='Real native purpose-separated mixed FROST/SQLite and Solidity money/gas; explicit Core/EVM/provenance/DKG/SPV/qualification fixtures. No live transfer, new proving or combined E2E')
    if withdrawal_user.get('bootstrap_user_code_implemented'):
        runs['withdrawal_bootstrap_user_code'] = dict(status=withdrawal_user['status'],
            source_snapshot=source_snapshot(withdrawal_user), evidence=str(withdrawal_user_path.relative_to(ROOT)),
            focused_checks=junit('worker-integration/withdrawal-bootstrap-user-tests.xml'),
            scope='Enrolled opt-in common bootstrap, atomic circular CREATE nonce recovery, owner MP/S payout/total quotes, durable primary/consent workflow and exact S fee/payout guards. EVM/Core boundaries explicit fixtures; no live deployment, new proof or combined E2E')
    if smp1.get('code_implemented'):
        runs['smp1_wallet_preparation']=dict(status=smp1['status'],source_snapshot=source_snapshot(smp1),
            evidence=str(smp1_path.relative_to(ROOT)),focused_checks=junit('worker-integration/smp1-tests.xml'),
            scope='Real pinned rust-bitcoin PSBT/final transaction codec and SQLite; wallet/Core/EVM fixtures, explicit uncertain-lock/signature recovery. Existing SMP1/coinbase and wallet/RPC regressions retained. No actual Core acceptance, payment or combined E2E')
    if recipient.get('code_implemented'):
        runs['recipient_matrix']=dict(status=recipient['status'],source_snapshot=source_snapshot(recipient),
            evidence=str(recipient_path.relative_to(ROOT)),focused_checks=junit('worker-integration/recipient-tests.xml'),
            scope='Canonical RecipientWalletV1 owner ERC-1271/MP transfer/approve/direct-withdrawal, explicit advanced acknowledgement and immutable nonce binding. Real local Solidity/token/escrow, ABI/HPKE/native/SQLite; explicit Core/EVM provenance histories/DKG/SPV/qualification fixtures. No live value transfer or combined E2E')
    if incident.get('code_implemented'):
        runs['incident_status']=dict(status=incident['status'],source_snapshot=source_snapshot(incident),
            evidence=str(incident_path.relative_to(ROOT)),focused_checks=junit('worker-integration/incident-tests.xml'),
            scope='Actual HPKE/native EVM FROST adapter/libsecp256k1/SQLite and local Solidity; explicit provider/DKG/quorum/qualification fixtures. Dedicated private reports, manual signing approval, generation/finality and independent observation recovery. No live deployment, new proof or combined E2E')
    if epoch.get('code_implemented'):
        runs['old_epoch_service']=dict(status=epoch['status'],source_snapshot=source_snapshot(epoch),
            evidence=str(epoch_path.relative_to(ROOT)),focused_checks=junit('worker-integration/epoch-tests.xml'),
            scope='Actual native codec/share operations, EOA signatures and independent SQLite retention/recovery; explicit chain/DKG/Core/provenance/qualification fixtures. Single-pool epochs; no live quorum/threshold restore, new proof, deployment or combined E2E')
    if continuity.get('code_implemented'):
        runs['original_graph_continuity']=dict(status=continuity['status'],source_snapshot=source_snapshot(continuity),
            evidence=str(continuity_path.relative_to(ROOT)),focused_checks=junit('worker-integration/continuity-tests.xml'),
            scope='Actual native derivation/curve and independent SQLite history retention; explicit chain/DKG/provenance/qualification fixtures. Original-graph only; no live asset migration, full-history proof, global on-chain family registry or combined E2E')
    if contour.get('code_implemented'):
        runs['contour_controller_retained_handover']=dict(status=contour['status'],source_snapshot=source_snapshot(contour),
            evidence=str(contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/contour-tests.xml'),
            scope='Real subprocess/locks/SQLite/native curve identities; explicit non-monetary drivers and canonical chain/inventory/retention fixtures. Remaining actual drivers, global on-chain continuity and combined acceptance pending')
    if worker_contour.get('code_implemented'):
        runs['contour_worker_pipeline']=dict(status=worker_contour['status'],source_snapshot=source_snapshot(worker_contour),
            evidence=str(worker_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/worker-contour-tests.xml'),
            scope='Actual worker/controller/prover-stage control, SQLite, ABI, signatures and child locks; explicit Core/EVM/native preparation/proof fixtures. No fresh real proof or complete combined run')
    if service_contour.get('code_implemented'):
        runs['contour_service_pipeline']=dict(status=service_contour['status'],source_snapshot=source_snapshot(service_contour),
            evidence=str(service_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/service-contour-tests.xml'),
            scope='Real native HPKE, SQLite, original transport/issuer/payer automation, budgets and typed request preparation; CLI chain dispatched in-process with explicit canonical board/Core/EVM/proving fixtures. No actual value transfer or combined acceptance')
    if setup_contour.get('code_implemented'):
        runs['contour_setup_pipeline']=dict(status=setup_contour['status'],source_snapshot=source_snapshot(setup_contour),
            evidence=str(setup_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/setup-contour-tests.xml'),
            scope='Original bounded bootstrap engines, real EOA/nonce journals, native PSBT/funding codecs and Merkle checks; setup CLI chain dispatched in-process with explicit graph/EVM/Core/wallet signature/header fixtures. Structural bootstrap defers freshness; no actual deployment/value transfer or combined acceptance')
    if mint_contour.get('code_implemented'):
        runs['contour_membership_mint']=dict(status=mint_contour['status'],source_snapshot=source_snapshot(mint_contour),
            evidence=str(mint_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/mint-contour-tests.xml'),
            scope='Original SDK/admission and bounded membership/mint control, ABI, real EOA/SQLite/nonce journals; CLI dispatched in-process with explicit graph/funding/header/Core/FROST/EVM effects. No actual new mainnet inclusion, proof, FROST authorization or value transfer')
    if withdrawal_contour.get('code_implemented'):
        runs['contour_withdrawal_reconciliation']=dict(status=withdrawal_contour['status'],source_snapshot=source_snapshot(withdrawal_contour),
            evidence=str(withdrawal_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/withdrawal-contour-tests.xml'),
            scope='Reviewed initial V4 request/terms, original ABI/EOA/budget/nonce/SQLite and native exact Bitcoin raw recovery; original full inventory/typed reconciliation with in-process CLI chain. Core/graph/FROST/EVM monetary effects are fixtures; no actual transfer, new proof or combined acceptance')
    if incident_contour.get('code_implemented'):
        runs['contour_incident_recovery']=dict(status=incident_contour['status'],source_snapshot=source_snapshot(incident_contour),
            evidence=str(incident_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/incident-contour-tests.xml'),
            scope='Standalone accepted exact status decision, original HPKE/report/status/EVM_AUTH policy, ABI/EOA/cost/nonce/SQLite recovery and retained observations; in-process CLI chain. Graph/Core/qualification/FROST/EVM effects are fixtures; no actual on-chain incident, live recovery or combined acceptance')
    if epoch_contour.get('code_implemented'):
        runs['contour_old_epoch_service']=dict(status=epoch_contour['status'],source_snapshot=source_snapshot(epoch_contour),
            evidence=str(epoch_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/epoch-contour-tests.xml'),
            scope='Original bounded Fleet/retention policy, EOA claims, native watch/reorg and SQLite, plus in-process CLI chain. Chain/binding/qualification/reserve/Core/EVM providers are fixtures; no live retained-key availability, transfer, new proof or complete multi-need acceptance')
    if suite_contour.get('code_implemented'):
        runs['contour_multi_request_suite']=dict(status=suite_contour['status'],source_snapshot=source_snapshot(suite_contour),
            evidence=str(suite_contour_path.relative_to(ROOT)),focused_checks=junit('worker-integration/suite-contour-tests.xml'),
            scope='Dependency DAG, original heartbeat, typed 23-case recipes, explicit late input binding, fee/refund SDK paths and read-only binary proof/transaction evidence collection. Public chain/Core/FROST/proving effects are fixtures; actual combined execution and qualification remain pending')
    if c123.get('code_implemented'):
        runs['c123_implementation_checks']=dict(status=c123['status'],source_snapshot=source_snapshot(c123),
            evidence=str(c123_path.relative_to(ROOT)),focused_checks=junit('worker-integration/c123-tests.xml'),
            full_solidity_tests_executed=False,actual_local_evm_unit_execution_performed=True,
            workflow_recipe_count=c123['workflow_recipe_count'],mandatory_observation_slots=c123['mandatory_observation_slots'],
            scope='Current selected Python regression and real compiled family-root bytecode on local PyEVM with explicit UNIT DKG/approval fixtures; Solidity build and test type/ABI compilation. Mainnet guest unchanged, selected local-fence residual risk retained. No real Core/FROST/prover/value transfer or qualified full matrix execution')
    if local.get('local_current_source_checks'):
        runs['full_local_acceptance']=dict(status=local['status'],source_snapshot=source_snapshot(local),
            evidence=str(local_path.relative_to(ROOT)),counts=local['python_tests'],solidity_tests=local['solidity_tests'],
            restored_native_checks=local['restored_native_checks'],
            scope='Complete local Python directories and ordinary/state Solidity tests, with explicit model/fixture boundaries; real contour deferred by user, independent qualification incomplete')
    write("regression-report.json", {"status": "UNKNOWN", "runs": runs,
        "scope": "Current incremental runs and explicitly historical source snapshots; selected e2e are not complete existing E2E regression",
        "full_end_to_end": "UNKNOWN", "new_production_zk_end_to_end": "UNKNOWN", "new_address_subsidy_end_to_end": "UNKNOWN"})

    reasons = {
        "target_network": "Production EVM/checkpoint and infrastructure not supplied",
        "security_maturity": "Candidate provenance retained but baseline tests/review/license/operational acceptance incomplete",
        "policy_compatibility": "Configurable mainnet bootstrap/consumer has local real-proof evidence; full mainnet fork/halt, selected production profile/checkpoint, live operational qualification and independent review remain incomplete",
        "real_proof_verification": "Local mainnet proofs and separate TEST_ONLY regtest monetary E2E have source-bound reports; the production mainnet state/consumer workflow remains unqualified",
        "self_hosted": "Native ARM64 Docker wrapping is supported by the recorded local evidence; complete workload/recovery/operational acceptance remains incomplete",
        "event_driven": "Consumer inbox, Core observer, local prover, heartbeat and encrypted on-chain transport/grouping code implemented. Earlier cached-real-proof update is historical; combined fresh proving/transport/payment/consumer and continuous operation remain unqualified",
        "stale_state_safety": "Existing guard preserved; local two-bound Fresh and real-proof acceptance have separate evidence; production consumer binding remains unqualified",
        "shared_spv_requests": "SPVMessageBoard and encrypted request/ACK/status client/worker plus compatible execution grouping implemented and locally unit-checked; actual chain/proving/payment/consumer integration and operational qualification remain pending",
        "relay_authorization": "Production proof-only/FROST profile and implementation not selected/qualified",
        "gas_funding": "Native treasury/payment/claim, canonical state view, issuer/quote/opt-in payer automation and bounded confederation fallback implemented; combined real-value/transport/prover/consumer E2E and operational qualification remain pending",
        "address_deposits": "Candidate registry/mint/admission, V3 signer/common journal, request/watch/rescan, typed FROST registration, V5/V3 SPV worker and canonical BTC reserve/reconciliation code implemented; verifier/operational qualification, per-owner fee service, cross-graph continuity and combined E2E remain pending",
        "withdrawal_subsidy": "Personal BTC/MP/S consent/credits/refunds, principal conservation, V4 signer and shared SPV code implemented; actual purpose qualification, independent review, live deployment and combined contour unqualified",
        "integration_review": "Internal peer review is not an independent external acceptance review",
        "regression": "Local regression is recorded separately; all required new ZK/address/subsidy E2E absent",
        "deployment_limits": "Local ordinary graph and experimental V2 state/verifier/adapter deployment limits tested; complete production consumer graph unqualified",
        "latency": "Calendar simulation is not measured production latency",
        "economic_viability": "Target, real proving/resource costs, complete receipts and payback horizon unknown",
        "live_migration": "No authenticated inventory of actual live deployments/obligations supplied",
    }
    evidence = {"security_maturity": "candidate-manifest.json", "policy_compatibility": "selection-report.md",
                "self_hosted": "local-proving-report.json", "event_driven": "event-driven-report.json",
                "gas_funding": "gas-funding-report.json",
                "address_deposits": "address-deposit-report.json", "withdrawal_subsidy": "withdrawal-subsidy-report.json",
                "economic_viability": "tco.json", "regression": "regression-report.json", "integration_review": "internal-review.json"}
    gates = {name: {"status": "FAIL" if name == "policy_compatibility" else "UNKNOWN",
                    "reason": reasons[name], "evidence": ["ops/artifacts/zkspv/" + evidence[name]] if name in evidence else []}
             for name in GATES}
    if withdrawal.get('personal_fee_code_implemented'):
        gates['withdrawal_subsidy'].update(code_implemented=True,code_source_snapshot=source_snapshot(withdrawal),
            subgates=withdrawal.get('supporting_gates',{}))
    if withdrawal_user.get('bootstrap_user_code_implemented'):
        gates['withdrawal_subsidy'].update(bootstrap_user_code_implemented=True,
            bootstrap_user_source_snapshot=source_snapshot(withdrawal_user))
        gates['withdrawal_subsidy']['evidence'].append(str(withdrawal_user_path.relative_to(ROOT)))
    if smp1.get('code_implemented'):
        gates['address_deposits'].update(smp1_wallet_preparation_code_implemented=True,
            smp1_wallet_preparation_source_snapshot=source_snapshot(smp1))
        gates['address_deposits']['evidence'].append(str(smp1_path.relative_to(ROOT)))
    if recipient.get('code_implemented'):
        gates['address_deposits'].update(recipient_matrix_code_implemented=True,
            recipient_matrix_source_snapshot=source_snapshot(recipient))
        gates['address_deposits']['evidence'].append(str(recipient_path.relative_to(ROOT)))
    if incident.get('code_implemented'):
        gates['address_deposits'].update(incident_status_code_implemented=True,
            incident_status_source_snapshot=source_snapshot(incident),
            reason='Candidate address/recipient/fee and incident-status client code implemented; verifier/profile qualification, old-epoch/cross-graph continuity and combined live E2E remain pending')
        gates['address_deposits']['evidence'].append(str(incident_path.relative_to(ROOT)))
    if epoch.get('code_implemented'):
        gates['address_deposits'].update(old_epoch_service_code_implemented=True,
            old_epoch_service_source_snapshot=source_snapshot(epoch),
            reason='Candidate address/recipient/fee, incident and old-epoch service clients implemented; verifier/profile qualification, canonical cross-graph/key-family continuity and combined live E2E remain pending')
        gates['address_deposits']['evidence'].append(str(epoch_path.relative_to(ROOT)))
    if continuity.get('code_implemented'):
        gates['address_deposits'].update(original_graph_continuity_code_implemented=True,
            original_graph_continuity_source_snapshot=source_snapshot(continuity),
            cross_graph_migration_supported=False,
            reason='Candidate clients and original-graph continuity guards implemented; actual verifier/profile/global continuity policy and live inventory qualification plus unified combined contour remain pending')
        gates['address_deposits']['evidence'].append(str(continuity_path.relative_to(ROOT)))
    if contour.get('code_implemented'):
        gates['address_deposits'].update(retained_graph_handover_code_implemented=True,
            contour_handover_source_snapshot=source_snapshot(contour),
            reason='Original-graph guards and local retained-source/separate-target service review implemented; actual qualification, global on-chain family/status continuity and combined monetary acceptance pending')
        gates['address_deposits']['evidence'].append(str(contour_path.relative_to(ROOT)))
        gates['event_driven'].update(contour_controller_code_implemented=True,complete_driver_set_implemented=False,
            reason='Durable controller and read-only canonical handover adapter implemented; remaining actual monetary/proving/transport drivers and combined operational acceptance pending')
    if worker_contour.get('code_implemented'):
        gates['event_driven'].update(worker_contour_adapters_code_implemented=True,
            worker_contour_source_snapshot=source_snapshot(worker_contour),
            connected_capabilities=worker_contour['connected_capabilities'],complete_driver_set_implemented=False,
            reason='Consumer/native preparation/fresh local proving/state publication and handover adapters implemented; encrypted transport/payment, monetary consumer/recovery adapters and actual combined acceptance pending')
        gates['event_driven']['evidence'].append(str(worker_contour_path.relative_to(ROOT)))
    if service_contour.get('code_implemented'):
        gates['event_driven'].update(service_contour_adapters_code_implemented=True,
            service_contour_source_snapshot=source_snapshot(service_contour),
            connected_capabilities=service_contour['connected_capabilities'],complete_driver_set_implemented=False,
            reason='Encrypted request/ACK/status, canonical admission/quote/payment and explicit confederation own funding connected to worker pipeline; bootstrap/funding, monetary consumer/recovery adapters, combined collector and actual long acceptance pending')
        gates['event_driven']['evidence'].append(str(service_contour_path.relative_to(ROOT)))
    if setup_contour.get('code_implemented'):
        gates['event_driven'].update(setup_contour_adapters_code_implemented=True,
            setup_contour_source_snapshot=source_snapshot(setup_contour),
            connected_capabilities=setup_contour['connected_capabilities'],complete_driver_set_implemented=False,
            reason='Structural bootstrap and exact Bitcoin funding connected to encrypted service/worker pipeline through original deployment/nonce/wallet journals; membership/mint/withdrawal/recovery adapters, combined collector and actual long acceptance pending')
        gates['event_driven']['evidence'].append(str(setup_contour_path.relative_to(ROOT)))
    if mint_contour.get('code_implemented'):
        gates['event_driven'].update(membership_mint_adapters_code_implemented=True,
            membership_mint_source_snapshot=source_snapshot(mint_contour),connected_capabilities=mint_contour['connected_capabilities'],
            complete_driver_set_implemented=False,
            reason='Membership and SMP1 V1/custody V2/address mint connected through original archive/admission/nonce/budget engines; withdrawal/reconciliation/recovery adapters, multi-need collector and actual combined acceptance pending')
        gates['event_driven']['evidence'].append(str(mint_contour_path.relative_to(ROOT)))
    if withdrawal_contour.get('code_implemented'):
        gates['event_driven'].update(withdrawal_reconciliation_adapters_code_implemented=True,
            withdrawal_reconciliation_source_snapshot=source_snapshot(withdrawal_contour),
            connected_capabilities=withdrawal_contour['connected_capabilities'],complete_driver_set_implemented=False,
            reason='Reviewed initial V4 withdrawal preparation/BTC/MP/S settlement and full inventory reconciliation connected through original owner/operator/Bitcoin journals; incident/old-epoch adapters, multi-need collector and actual combined acceptance pending')
        gates['event_driven']['evidence'].append(str(withdrawal_contour_path.relative_to(ROOT)))
        gates['withdrawal_subsidy'].update(contour_adapters_code_implemented=True,
            contour_source_snapshot=source_snapshot(withdrawal_contour))
        gates['withdrawal_subsidy']['evidence'].append(str(withdrawal_contour_path.relative_to(ROOT)))
    if incident_contour.get('code_implemented'):
        gates['event_driven'].update(incident_recovery_adapter_code_implemented=True,
            incident_recovery_source_snapshot=source_snapshot(incident_contour),
            connected_capabilities=incident_contour['connected_capabilities'],complete_driver_set_implemented=False,
            reason='Standalone reviewed incident/recovery connected through original selected encrypted report/ACK/status, exact EVM_AUTH receipts and nonce/cost/observation journals; old-epoch adapter, multi-need collector and actual combined acceptance pending')
        gates['event_driven']['evidence'].append(str(incident_contour_path.relative_to(ROOT)))
        gates['address_deposits'].update(incident_contour_adapter_code_implemented=True,
            incident_contour_source_snapshot=source_snapshot(incident_contour))
        gates['address_deposits']['evidence'].append(str(incident_contour_path.relative_to(ROOT)))
    if epoch_contour.get('code_implemented'):
        gates['event_driven'].update(old_epoch_service_adapter_code_implemented=True,
            old_epoch_contour_source_snapshot=source_snapshot(epoch_contour),
            connected_capabilities=epoch_contour['connected_capabilities'],all_capability_adapter_types_implemented=True,
            complete_driver_set_implemented=False,multi_need_orchestrator_implemented=False,
            reason='All twenty adapter types available across partial configurations, including original bounded old-epoch observation and fresh threshold/service/funding review; full multi-need scenario bindings/collector and actual combined acceptance pending')
        gates['event_driven']['evidence'].append(str(epoch_contour_path.relative_to(ROOT)))
        gates['address_deposits'].update(old_epoch_contour_adapter_code_implemented=True,
            old_epoch_contour_source_snapshot=source_snapshot(epoch_contour))
        gates['address_deposits']['evidence'].append(str(epoch_contour_path.relative_to(ROOT)))
    if suite_contour.get('code_implemented'):
        gates['event_driven'].update(multi_need_orchestrator_implemented=True,complete_driver_set_implemented=True,
            typed_scenario_matrix_implemented=True,combined_evidence_collector_implemented=True,
            suite_source_snapshot=source_snapshot(suite_contour),complete_private_bindings_supplied=False,
            reason='Multi-request DAG, initial heartbeat/catch-up, original SDK scenario recipes, explicit reviewed late binding and source/input/proof/envelope collector implemented. Actual combined run and independent production qualification pending')
        gates['event_driven']['evidence'].append(str(suite_contour_path.relative_to(ROOT)))
    if c123.get('code_implemented'):
        current=source_snapshot(c123)
        gates['event_driven'].update(c123_source_snapshot=current,
            canonical_family_registry_implemented=True,processed_observation_history_implemented=True,
            recoverable_need_outbox_implemented=True,observation_policy=c123['observation_policy'],
            global_onchain_halt_guaranteed=False,mainnet_guest_changed=False,
            workflow_recipe_count=c123['workflow_recipe_count'],mandatory_observation_slots=c123['mandatory_observation_slots'],
            complete_private_bindings_supplied=False,qualified_scenario_execution_complete=False,
            reason='Canonical family root, durable observation/outbox and expanded lifecycle/assertion adapters implemented. Selected local-fence policy retains mainnet guest and residual competing-submitter risk; private case bindings and actual qualified contour remain pending')
        for name in ('event_driven','address_deposits','withdrawal_subsidy'):
            gates[name]['evidence'].append(str(c123_path.relative_to(ROOT)))
            gates[name]['c123_source_snapshot']=current
    if local.get('local_current_source_checks'):
        snapshot=source_snapshot(local)
        for name in ('event_driven','address_deposits','withdrawal_subsidy','regression'):
            gates[name].update(local_acceptance_source_snapshot=snapshot,local_python_tests=local['python_tests'],
                local_solidity_tests=local['solidity_tests'],independent_qualification=False,
                real_contour_status=local['real_contour_status'])
            gates[name]['evidence'].append(str(local_path.relative_to(ROOT)))
        gates['event_driven'].update(connected_capabilities=local['connected_capabilities'],
            restored_native_checks=local['restored_native_checks'],local_case_bindings=195,
            reason='Full local regression and explicit local programs for 195 rows recorded. Private real-contour bindings and qualified execution deferred by user for disk capacity; unchanged mainnet guest retains selected local-fence residual risk')
        gates['address_deposits']['reason']='Canonical family root, SDK lifecycle and local contract tests implemented; derivation/fee-purpose activation and independent real-contour qualification remain pending'
    artifact_hashes = {str(p.relative_to(ROOT)): sha(p) for p in sorted(OUT.rglob("*"))
                       if p.is_file() and p.name not in ("decision.json", "preflight-report.json")}
    if patch_path.is_file():
        artifact_hashes[str(patch_path.relative_to(ROOT))] = sha(patch_path)
    if derivation_path.is_file():
        artifact_hashes[str(derivation_path.relative_to(ROOT))] = sha(derivation_path)
    header_directory = ROOT / "rust/zkspv/header-qualification"
    if header_directory.is_dir():
        for path in sorted(header_directory.glob("*report.json")):
            artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
        for path in sorted((header_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    time_directory = ROOT / "rust/zkspv/time-policy"
    if (time_directory / "report.json").is_file():
        artifact_hashes[str((time_directory / "report.json").relative_to(ROOT))] = sha(time_directory / "report.json")
        for path in sorted((time_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    fork_directory = ROOT / "rust/zkspv/fork-policy"
    if (fork_directory / "report.json").is_file():
        artifact_hashes[str((fork_directory / "report.json").relative_to(ROOT))] = sha(fork_directory / "report.json")
        for path in sorted((fork_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    checkpoint_directory = ROOT / "rust/zkspv/checkpoint-policy"
    if (checkpoint_directory / "report.json").is_file():
        artifact_hashes[str((checkpoint_directory / "report.json").relative_to(ROOT))] = sha(checkpoint_directory / "report.json")
        for path in sorted((checkpoint_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    transition_directory = ROOT / "rust/zkspv/transition-policy"
    if (transition_directory / "report.json").is_file():
        artifact_hashes[str((transition_directory / "report.json").relative_to(ROOT))] = sha(transition_directory / "report.json")
        for path in sorted((transition_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    resume_directory = ROOT / "rust/zkspv/resume-policy"
    if (resume_directory / "report.json").is_file():
        artifact_hashes[str((resume_directory / "report.json").relative_to(ROOT))] = sha(resume_directory / "report.json")
        for path in sorted((resume_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    v2_directory = ROOT / "rust/zkspv/resume-transition"
    if (v2_directory / "report.json").is_file():
        artifact_hashes[str((v2_directory / "report.json").relative_to(ROOT))] = sha(v2_directory / "report.json")
        for path in sorted((v2_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    state_directory = ROOT / "rust/zkspv/state-adapter"
    for path in sorted((OUT / "worker-integration").rglob("*")):
        if path.is_file():
            artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    for path in sorted((ROOT / "ops/artifacts/zkspv/bootstrap-integration").rglob("*")):
        if path.is_file():
            artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    if (state_directory / "report.json").is_file():
        artifact_hashes[str((state_directory / "report.json").relative_to(ROOT))] = sha(state_directory / "report.json")
        for path in sorted((state_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    guest_directory = ROOT / "rust/zkspv/guest-integration"
    if (guest_directory / "report.json").is_file():
        artifact_hashes[str((guest_directory / "report.json").relative_to(ROOT))] = sha(guest_directory / "report.json")
        for path in sorted((guest_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    groth_directory = ROOT / "rust/zkspv/groth16-integration"
    if (groth_directory / "report.json").is_file():
        for path in sorted(groth_directory.rglob("*")):
            if path.is_file() and ("evidence" in path.relative_to(groth_directory).parts or path.name.endswith("report.json") or path.name == "container-pin.json"):
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    acceptance_directory = ROOT / "rust/zkspv/state-proof-integration"
    if acceptance_path.is_file():
        artifact_hashes[str(acceptance_path.relative_to(ROOT))] = sha(acceptance_path)
        for path in sorted((acceptance_directory / "evidence").rglob("*")):
            if path.is_file():
                artifact_hashes[str(path.relative_to(ROOT))] = sha(path)
    if scenario_path.is_file():
        artifact_hashes[str(scenario_path.relative_to(ROOT))] = sha(scenario_path)
        for name in scenario_artifacts:
            if (ROOT / name).is_file():
                artifact_hashes[name] = sha(ROOT / name)
    if deposit_path.is_file():
        artifact_hashes[str(deposit_path.relative_to(ROOT))] = sha(deposit_path)
        for name in deposit_artifacts:
            if (ROOT / name).is_file():
                artifact_hashes[name] = sha(ROOT / name)
    decision = {"schema": "satoshifi.zkspv.decision.v1", "specification_version": "2.16", "implementation_status": "PARTIAL",
                "production_ready": False, "new_deployment_without_obligations": False, "gates": gates,
                "artifact_sha256": artifact_hashes,
                "next_required": [("Qualify reviewed corrections and full mainnet fork/halt coverage; TEST_ONLY models are separate" if runs["mainnet_retarget_proof"]["status"] == "PASS"
                                   else "Qualify reviewed corrections and full guest scenario coverage"),
                    ("Qualify production state/consumer integration and remaining production proof scenarios" if runs["state_proof_acceptance"]["status"] == "PASS"
                     else "Generate a proof bound to an actually deployed state address and qualify Groth16 state-transition acceptance"),
                    ("Resume the user-deferred real Core/FROST/Groth16/EVM contour with private case bindings; qualify the implemented canonical family root and retain selected local-fence risk policy; live migration remains conditional" if local.get('local_current_source_checks') else
                     "Execute the reviewed full scenario suite with real Core/FROST/Groth16/EVM evidence; global on-chain family/status continuity, live transition and specification 15.11 remain open" if suite_contour.get('code_implemented') else
                     "Implement multi-need orchestrator/full scenario bindings and combined collector/initial heartbeat coordination; global on-chain key-family continuity remains open" if epoch_contour.get('code_implemented') else
                     "Connect old-epoch service adapter plus multi-need combined collector/initial heartbeat coordination; global on-chain key-family continuity remains open" if incident_contour.get('code_implemented') else
                     "Connect incident/recovery and old-epoch service adapters plus multi-need combined collector/initial heartbeat coordination; global on-chain key-family continuity remains open" if withdrawal_contour.get('code_implemented') else
                     "Connect BTC/MP/S withdrawal and recovery adapters plus multi-need combined collector; global on-chain key-family continuity remains open" if mint_contour.get('code_implemented') else
                     "Connect membership materialization, monetary consumer/recovery adapters and combined collector; global on-chain key-family continuity remains open" if setup_contour.get('code_implemented') else
                     "Connect bootstrap/funding, monetary consumer/recovery adapters and combined collector; global on-chain key-family continuity remains open" if service_contour.get('code_implemented') else
                     "Connect transport/admission/payment then monetary/recovery adapters and combined collector; global on-chain key-family continuity remains open" if worker_contour.get('code_implemented') else
                     "Connect remaining actual contour drivers; qualify canonical global key-family continuity and combined old-epoch/monetary flows" if contour.get('code_implemented') else
                     "Complete canonical cross-graph/key-family continuity and unified contour runner; qualify old-epoch service and combined monetary flows" if epoch.get('code_implemented')
                     else "Implement remaining ZK/transport/gas/address/subsidy scope"), "Measure latency/TCO on explicit target",
                    "Obtain independent review and actual live-obligation inventory"]}
    write("decision.json", decision)
    write("preflight-report.json", preflight(json.loads((ROOT / "config/zkspv.example.json").read_text()), decision, ROOT))
    lines = ["# Текущие проверки ТЗ 2.16", "", "Итог: PARTIAL, production_ready=false. Новые результаты не объединяются с архивными PASS.", "",
             "| Прогон | Статус | Число/граница |", "|---|---|---|"]
    for name, run in runs.items():
        count = run.get("counts", {}).get("tests", run.get("passed"))
        lines.append(f"| {name} | {run['status']} | {count if count is not None else 'UNKNOWN'} |")
    lines += ["", "Ранний Citrea library build PASS; native tests тогда остановились из-за ENOSPC. Повторные original/corrected native прогоны вынесены отдельными строками выше.",
              "Диагностические arithmetic probes 5 debug + 5 release воспроизводят несовместимости, не доказывают пригодность.",
              "Отдельные минимальные reference patches: 8 debug + 8 release + 3 application; full upstream qualification открыта.",
              "Historical headers: извлечённые переходы проверены на 6111 Mainnet/Testnet4 headers, включая checked chainwork и шесть epoch resumes; это не полный native suite или fork-choice proof.",
              "Compact target: арифметика закреплённой версии Core дала 45064 reference results; их сравнение с Rust входит в отдельные extracted/native прогоны. Полный перебор compact значений и Core node не заявлены.",
              "Time policy: ранний компонент проверяет максимум новых заголовков и EVM-предел +2 часа; последующие V2 guest/state proof имеют отдельные source-bound статусы выше.",
              "Fork/history: ранний V1 компонент проверяет LCA/reorg 100/101, halt-prefix и canonical membership. Последующие bounded V2 witnesses и TEST_ONLY policy proofs учитываются отдельно и не квалифицируют mainnet fork/halt.",
              "Funding parser: отдельные Forge/Anvil проверки; адресный денежный adapter/suite реализован, настоящий общий денежный прогон и production-квалификация остаются открытыми.",
              "Mint admission: новые contract/client проверки вынесены отдельно; прежние signer/E2E результаты сохраняют свой source snapshot.",
              "Gas treasury: native accounting/payment/CLI и локальные квитанции проверены с synthetic authority/state; полный gas_funding gate остаётся UNKNOWN.",
              "Gas governance: отдельные enrolled DKG/FROST результаты активации/ротации/pause; SPV fixture не заменяет квалифицированный state/adapter.",
              "Gas caller: локальные caps/budget/backoff, failed/zero claims и durable nonce recovery проверены отдельно; production gas_funding остаётся UNKNOWN.",
              "Address crypto: native IPC, BASE/DERIVED подписи настоящих Bitcoin sighashes и кандидат EVM-verifier; Core acceptance, canonical registry/live signer/mint и Ready ещё не закрыты.",
              "Rust funding и реальные Core comparisons описаны в signer-integration-report.json.",
              "Selected Bitcoin E2E не заменяет весь набор существующих E2E, адресные/SUB сценарии или реальный ZK proof.",
              "Guest proof: отдельные STARK/succinct/Groth16, EVM verifier и state-proof acceptance имеют собственные статусы и привязки исходников выше. Полное покрытие guest-сценариев и production-интеграция остаются открытыми.",
              "Proof scenarios: mainnet_retarget_proof относится к условному переходу 34270 → 34272 на неизменённом guest; test_model_fork_proof/test_model_halt_proof — только отдельный TEST_ONLY image с синтетической работой. Эти статусы не закрывают mainnet fork/halt и production-приёмку.",
              "", "Команды воспроизведения: docs/ZK_SPV.md; сырые stdout/JUnit и source hashes сохранены в этом каталоге.",
              "Результат внешнего независимого аудита отсутствует. Неизмеренные gas/latency/стоимость имеют null/UNKNOWN.", ""]
    (OUT / "test-report.md").write_text("\n".join(lines))
    # The report itself was written after hashing; include its final bytes.
    decision["artifact_sha256"]["ops/artifacts/zkspv/test-report.md"] = sha(OUT / "test-report.md")
    write("decision.json", decision)
    write("preflight-report.json", preflight(json.loads((ROOT / "config/zkspv.example.json").read_text()), decision, ROOT))
    print(json.dumps({"criteria": len(criteria), "clauses": len(clauses), "source_files_changed": len(changed),
                      "source_files_added": len(added), "production_ready": False, "runs": runs}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--rerun-replay", action="store_true")
    args = parser.parse_args()
    make_reports(args.spec, args.rerun_replay)
