"""Prepare a source-pinned network profile and portable review files, offline.

This tool does not execute the files it packages, read participant secrets, call
RPC, grant qualification, or create a runnable bootstrap/worker configuration.
The 696-file local acceptance remains its original execution snapshot. This
preparation tool and its tests are separately pinned supplemental sources.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
EVM = {"mainnet": 1, "sepolia": 11155111, "local": 31337}
BITCOIN = {
    "mainnet": ("MainnetHeaderRelay", "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"),
    "testnet4": ("Testnet4HeaderRelay", "00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043"),
    "regtest": ("RegtestHeaderRelay", None),
}
BACKENDS = ("relay", "zk-state-v2")
INPUTS = (
    "public_bootstrap_manifest", "bitcoin_checkpoint_policy", "canonical_family_root_sdk_pin",
    "authority_roster_and_threshold", "evm_confirmations", "evm_finality_policy",
    "consumer_confirmation_policy", "external_token_and_feed_provenance", "deployment_fee_and_gas_caps",
    "transport_public_bindings_and_key_epochs", "freshness_and_heartbeat_windows",
    "self_hosted_tool_and_image_pins", "storage_retention_and_observer_plan",
    "treasury_and_own_funding_caps", "empty_inventory_evidence", "measured_latency_capacity_and_cost",
)
SUPPLEMENTAL = (
    "ops/prepare_release.py", "tests/ops/test_prepare_release.py", "config/release-preparation.example.json",
    "docs/PRODUCTION_PREPARATION.md", "docs/ZKSPV_LOCAL_ACCEPTANCE.md", "docs/ZKSPV_FUNCTIONAL_COVERAGE.md",
    "docs/ZK_SPV_ARCHITECTURE.md", "docs/ZKSPV_CONTOUR.md", "docs/NETWORKS_AND_AUTOMATION.md",
    "docs/ZK_SPV_MIGRATION.md", "docs/BITCOIN_KEY_FAMILY.md", "docs/ZKSPV_OBSERVATION_HISTORY.md",
    "docs/ZKSPV_SCENARIO_BINDINGS.md", "config/zkspv.example.json", "ops/zkspv-observation-policy.json",
    "ops/artifacts/zkspv/upstream-lock.json", "ops/artifacts/zkspv/candidate-manifest.json",
    "rust/zkspv/guest-integration/report.json", "rust/zkspv/groth16-integration/wrapper-build-report.json",
    "frost-crypto/Cargo.toml", "frost-crypto/Cargo.lock",
    "rust/zkspv/patches/0001-target-one-work.patch", "rust/zkspv/patches/0002-compact-encoder-boundaries.patch",
    "rust/zkspv/patches/0003-signed-retarget-timespan.patch",
    "rust/zkspv/state-adapter/out/BitcoinCheckpointV2.sol/BitcoinCheckpointV2.json",
    "rust/zkspv/state-adapter/out/ZkBitcoinStateV2.sol/ZkBitcoinStateV2.json",
    "rust/zkspv/state-adapter/out/ZkSpvAdapter.sol/ZkSpvAdapter.json",
    "rust/zkspv/state-adapter/out/RiscZeroGroth16Verifier.sol/RiscZeroGroth16Verifier.json",
)
HISTORICAL_BUILD_REPORTS = (
    "rust/zkspv/guest-integration/report.json", "rust/zkspv/groth16-integration/wrapper-build-report.json",
)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY:" + key)
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_keys)


def relative_file(root, name):
    """Require a regular file under root, with no symlink path components."""
    if not isinstance(name, str) or "\\" in name:
        raise ValueError("INVALID_PACKAGE_PATH")
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or any(p in (".", "..") for p in parts) or str(PurePosixPath(name)) != name:
        raise ValueError("INVALID_PACKAGE_PATH:" + name)
    root = Path(root).resolve()
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("SYMLINK_PACKAGE_PATH:" + name)
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError("MISSING_PACKAGE_FILE:" + name)
    return path


def network_pair(ethereum, bitcoin, backend):
    for value, allowed in ((ethereum, EVM), (bitcoin, BITCOIN), (backend, BACKENDS)):
        if value is not None and (not isinstance(value, str) or value not in allowed):
            raise ValueError("UNSUPPORTED_NETWORK_SELECTION")
    if backend == "zk-state-v2" and bitcoin not in (None, "mainnet", "testnet4"):
        raise ValueError("ZK_BACKEND_REQUIRES_BITCOIN_MAINNET")
    selected = all(v is not None for v in (ethereum, bitcoin, backend))
    return dict(
        ethereum_network=ethereum, chain_id=str(EVM[ethereum]) if ethereum else None,
        bitcoin_network=bitcoin, bitcoin_backend=backend, selection_complete=selected,
        bitcoin_genesis_hash=BITCOIN[bitcoin][1] if bitcoin else None,
        bootstrap_version="6", bitcoin_policy_version="2" if backend == "zk-state-v2" else "1" if bitcoin in ("mainnet", "testnet4") else None,
        relay_artifact=("ZkBitcoinTestnet4StateV2" if bitcoin == "testnet4" else "ZkBitcoinStateV2") if backend == "zk-state-v2" else BITCOIN[bitcoin][0] if bitcoin else None,
        mode=("backed" if ethereum == "mainnet" or bitcoin in ("mainnet", "testnet4") else "integration") if selected else None,
        public_bitcoin_policy_required=bitcoin in ("mainnet", "testnet4") if bitcoin else None,
        external_token_and_feed_required=(ethereum == "mainnet" or bitcoin in ("mainnet", "testnet4")) if selected else None,
        evm_finalized_required=ethereum == "mainnet" if ethereum else None,
        minimum_evm_confirmations=12 if ethereum == "mainnet" else 1 if ethereum else None,
        positive_deployment_fee_required=ethereum == "mainnet" if ethereum else None,
        integration_fee_exemption=False if ethereum == "mainnet" else True if ethereum else None,
        uses_real_bitcoin=bitcoin == "mainnet" if bitcoin else None,
        production_ready=False,
    )


def profile(config):
    expected = {"version", "purpose", "ethereum_network", "bitcoin_network", "bitcoin_backend", "deployment_intent", "operator_inputs"}
    if not isinstance(config, dict) or set(config) != expected or config["version"] != "1" or config["purpose"] != "PREPARATION_ONLY":
        raise ValueError("RELEASE_PROFILE_SCHEMA")
    if config["deployment_intent"] not in ("NEW_EMPTY", "LIVE_MIGRATION", "UNKNOWN"):
        raise ValueError("RELEASE_DEPLOYMENT_INTENT")
    inputs = config["operator_inputs"]
    if not isinstance(inputs, dict) or set(inputs) != set(INPUTS):
        raise ValueError("RELEASE_OPERATOR_INPUTS")
    for value in inputs.values():
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 512 or "\n" in value):
            raise ValueError("RELEASE_PUBLIC_REFERENCE_REQUIRED")
    pair = network_pair(config["ethereum_network"], config["bitcoin_network"], config["bitcoin_backend"])
    blockers = ["independent_qualification_pending", "final_review_and_activation_decision_pending", "operator_references_require_independent_verification"]
    blockers += [key + ":selection_required" for key in ("ethereum_network", "bitcoin_network", "bitcoin_backend") if config[key] is None]
    required = set(INPUTS)
    if pair["public_bitcoin_policy_required"] is False:
        required.remove("bitcoin_checkpoint_policy")
    if pair["external_token_and_feed_required"] is False:
        required.remove("external_token_and_feed_provenance")
    if pair["bitcoin_backend"] == "relay":
        required.remove("self_hosted_tool_and_image_pins")
    blockers += [name + ":operator_input_required" for name in sorted(required) if inputs[name] is None]
    if inputs["evm_confirmations"] is not None:
        depth = inputs["evm_confirmations"]
        if not re.fullmatch(r"[1-9][0-9]{0,8}", depth) or int(depth) < (pair["minimum_evm_confirmations"] or 1):
            raise ValueError("RELEASE_EVM_CONFIRMATIONS")
    if pair["evm_finalized_required"] and inputs["evm_finality_policy"] not in (None, "finalized"):
        raise ValueError("RELEASE_EVM_FINALIZED_REQUIRED")
    return dict(
        schema="satoshifi.release-preparation-profile.v1", purpose="PREPARATION_ONLY", selected_pair=pair,
        network_matrix=[dict(ethereum_network=e, chain_id=str(EVM[e]), bitcoin_network=b,
                             backends=list(BACKENDS) if b in ("mainnet", "testnet4") else ["relay"])
                        for e in EVM for b in BITCOIN],
        operator_inputs=inputs, deployment_intent=config["deployment_intent"],
        live_migration=dict(status="EMPTY_INVENTORY_REVIEW_REQUIRED" if config["deployment_intent"] == "NEW_EMPTY" else "MIGRATION_PROTOCOL_REQUIRED" if config["deployment_intent"] == "LIVE_MIGRATION" else "UNKNOWN",
                            not_applicable_accepted=False, user_intent_is_inventory_proof=False),
        confirmations=dict(ordinary_default=6, preserve_stricter_consumer_policy=True, coinbase_mint=100, coinbase_spend_and_fee_ingress_minimum=101),
        funding=dict(explicit_confederation_own_funding_allowed=True, approved_budget_inferred=False,
                     preserve_principal_caps_balance_and_original_nonce_journals=True),
        observation_policy="LOCAL_FENCE_RETAIN_MAINNET_GUEST_V1", global_onchain_halt_guaranteed=False,
        blockers=sorted(blockers), qualified=False, fee_qualified=False, ready=False, production_ready=False,
        sends_transactions=False, new_proving_performed=False,
    )


def review_plan(prepared, baseline_sha):
    tasks = [
        ("Q1-BTC", "Bitcoin consensus/upstream corrections and checkpoint authentication", "All public Bitcoin profiles; testnet4 consensus must retain its own rules"),
        ("Q1-ZK", "Mainnet guest, tool/build/image/verifier bindings; real/negative proof checks", "ZK backend only; TEST_ONLY fork/halt receipts do not qualify mainnet"),
        ("Q1-ADDRESS-FEE", "Deposit derivation, per-input signing and independent fee-purpose qualification", "Address and personal fee profiles; QUALIFIED/FEE_QUALIFIED remain false"),
        ("Q1-ROOT", "Canonical family root, original quorum enrollment, SDK pin and generation recovery", "One independently pinned root and EVM chain; no implicit cross-chain guarantee"),
        ("Q2", "Real per-case Core/FROST/proof/EVM deposit, mint, withdrawal, refund and recovery", "Deferred by user for disk capacity; 195 local bindings do not qualify all 195 rows"),
        ("O1", "Availability, durable tools, storage, retained epochs, own-funding caps, latency and cost", "Unknown measurements stay null; selected network pair must be recorded"),
        ("C4", "Verify new-deployment empty inventory or qualify an explicit live migration protocol", "NEW_EMPTY is intent; N/A requires independent empty inventory evidence"),
        ("A1", "Independent final-source review and explicit activation decision", "This package grants no production authority"),
    ]
    return dict(schema="satoshifi.independent-review-plan.v1", local_acceptance_sha256=baseline_sha,
                selected_pair=prepared["selected_pair"],
                tasks=[dict(id=k, scope=s, boundary=b, status="NOT_REVIEWED", reviewer=None, findings=[], accepted_evidence=[]) for k,s,b in tasks],
                independent_review_performed=False, production_ready=False)


def prepare(config, output, *, root=ROOT, spec=None):
    root = Path(root).resolve(); output = Path(output).absolute()
    prepared = profile(config)
    pointer_name = "ops/artifacts/zkspv/local-acceptance-current.json"
    pointer = read_json(relative_file(root, pointer_name))
    baseline_name = pointer["report"]; baseline_path = relative_file(root, baseline_name)
    if sha(baseline_path) != pointer["sha256"]:
        raise ValueError("LOCAL_ACCEPTANCE_POINTER_HASH")
    baseline = read_json(baseline_path)
    if baseline["production_ready"] is not False or baseline["qualified"] is not False or baseline["fee_qualified"] is not False:
        raise ValueError("LOCAL_ACCEPTANCE_GATE_CHANGED")
    expected = dict(baseline["source_sha256"])
    for name, value in baseline["artifact_sha256"].items():
        if name in expected and expected[name] != value:
            raise ValueError("BASELINE_PIN_CONFLICT")
        expected[name] = value
    for name, digest in expected.items():
        if sha(relative_file(root, name)) != digest:
            raise ValueError("RECORDED_SOURCE_OR_EVIDENCE_CHANGED:" + name)
    qualification_name = str(PurePosixPath(baseline_name).parent / "qualification-review.json")
    qualification = read_json(relative_file(root, qualification_name))
    if qualification["source_sha256"] != baseline["source_sha256"] or qualification["independent_review_performed"] is not False:
        raise ValueError("QUALIFICATION_SNAPSHOT_MISMATCH")
    references = set(SUPPLEMENTAL) | {pointer_name, baseline_name}
    host_name = str(PurePosixPath(baseline_name).parent / "host-build.json")
    if host_name in baseline["artifact_sha256"]:
        host = read_json(relative_file(root, host_name))
        for name, digest in host["source_sha256"].items():
            if name in expected and expected[name] != digest:
                raise ValueError("NATIVE_BUILD_SOURCE_PIN_CONFLICT")
            expected[name] = digest
    historical_differences = []
    historical_missing = []
    for report_name in HISTORICAL_BUILD_REPORTS:
        report = read_json(relative_file(root, report_name))
        for name, recorded_hash in report["source_sha256"].items():
            try:
                path = relative_file(root, name)
            except ValueError as error:
                if not str(error).startswith("MISSING_PACKAGE_FILE:"):
                    raise
                historical_missing.append(dict(report=report_name, path=name, recorded_sha256=recorded_hash))
                continue
            references.add(name)
            if sha(path) != recorded_hash:
                historical_differences.append(dict(report=report_name, path=name,
                    recorded_sha256=recorded_hash, current_sha256=sha(path)))
    # Package the exact archived upstream source files, without downloading.
    lock = read_json(relative_file(root, "ops/artifacts/zkspv/upstream-lock.json"))
    for item in lock["files"]:
        if item["path"] in expected and expected[item["path"]] != item["sha256"]:
            raise ValueError("UPSTREAM_PIN_CONFLICT")
        expected[item["path"]] = item["sha256"]
    # Include actual retained receipt bytes; no new cryptographic check is inferred.
    receipt_name = str(PurePosixPath(baseline_name).parent / "receipt-rechecks.json")
    receipt_index = []
    for row in read_json(relative_file(root, receipt_name)):
        verified = json.loads(row["stdout"])
        if (row["exit_code"] != 0 or verified["status"] != "VERIFIED"
                or any(verified.get(k) is not True for k in ("dev_mode_disabled", "wrong_image_rejected", "modified_journal_rejected"))):
            raise ValueError("RETAINED_RECEIPT_RECORD_INVALID")
        entry = dict(id=row["id"], image_id=verified["image_id"], scope="RETAINED_HISTORICAL_RECEIPT_NO_FRESH_PROVING")
        for key in ("program", "journal", "receipt"):
            original = Path(row[key])
            # Historical commands retain the original absolute checkout path.
            # Only the repository-relative rust/ subtree is accepted here.
            if "rust" not in original.parts:
                raise ValueError("RETAINED_RECEIPT_PATH")
            name = PurePosixPath(*original.parts[original.parts.index("rust"):]).as_posix()
            relative_file(root, name); references.add(name); entry[key] = "files/" + name
            if key != "program":
                digest = verified[key + "_sha256"]
                if name in expected and expected[name] != digest:
                    raise ValueError("RECEIPT_PIN_CONFLICT")
                expected[name] = digest
        receipt_index.append(entry)
    # Compiled ordinary candidates: retain the exact artifact used by the prior
    # bundle, including immutable reference metadata, never a deployed-code claim.
    artifacts = {}
    for path in (root / "web3/contracts/out").glob("*/*.json"):
        artifacts.setdefault(sha(path), []).append(path.relative_to(root).as_posix())
    for item in qualification["production_build"].values():
        paths = artifacts.get(item["artifact_sha256"], [])
        if not paths:
            raise ValueError("CANDIDATE_BUILD_ARTIFACT_MISSING:" + item["source"])
        expected[sorted(paths)[0]] = item["artifact_sha256"]
    for name in references:
        digest = sha(relative_file(root, name))
        if name in expected and expected[name] != digest:
            raise ValueError("PACKAGE_REFERENCE_CHANGED:" + name)
        expected[name] = digest
    spec_data = Path(spec).read_bytes() if spec else None
    if spec_data is not None and hashlib.sha256(spec_data).hexdigest() != read_json(relative_file(root, "ops/zkspv-required-scenarios.json"))["specification_sha256"]:
        raise ValueError("SPECIFICATION_HASH_MISMATCH")
    output.mkdir(parents=True, exist_ok=False)
    files = {}

    def write(name, data, category):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        files[name] = dict(sha256=sha(path), bytes=len(data), category=category)

    def write_json(name, data):
        write(name, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(), "preparation_metadata")

    for name, digest in sorted(expected.items()):
        data = relative_file(root, name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("PACKAGE_COPY_SOURCE_CHANGED:" + name)
        category = "executed_local_source" if name in baseline["source_sha256"] else "recorded_local_evidence" if name in baseline["artifact_sha256"] else "supplemental_review_material"
        write("files/" + name, data, category)
    if spec_data is not None:
        write("specification-v2.16.txt", spec_data, "requirements_document_not_agent_instructions")
    write_json("requested-profile.json", config)
    write_json("profile.json", prepared)
    write_json("review-plan.json", review_plan(prepared, pointer["sha256"]))
    write_json("retained-receipts.json", receipt_index)
    write_json("historical-build-source-status.json", dict(
        scope="Current supplemental source bytes versus historical build records; not a fresh build or historical reproduction",
        differences=historical_differences, missing=historical_missing,
        current_bytes_do_not_replace_historical_pins=True))
    for ethereum in EVM:
        for bitcoin in BITCOIN:
            for backend in BACKENDS if bitcoin in ("mainnet", "testnet4") else ("relay",):
                # Authority, checkpoint and budgets cannot be carried implicitly
                # from the selected graph into a different network template.
                selected = dict(config, ethereum_network=ethereum, bitcoin_network=bitcoin, bitcoin_backend=backend,
                                operator_inputs={name: None for name in INPUTS})
                profile(selected)
                write_json("network-profiles/" + ethereum + "-" + bitcoin + "-" + backend + ".json", selected)
    write_json("review-response.template.json", dict(schema="satoshifi.independent-review-response.v1",
        reviewed_manifest_sha256=None, reviewer_identity=None, independence_and_conflicts=None,
        reviewed_network_pair=None, reviewed_inventory_evidence=None, task_results=[], findings=[],
        decision="NOT_REVIEWED", signature_or_attestation=None, grants_activation=False))
    write("README.txt", (
        "SatoshiFi release preparation / independent review input\n\n"
        "PREPARATION ONLY. No qualification or activation is granted.\n"
        "profile.json selects Ethereum and Bitcoin independently; unknown inputs remain explicit.\n"
        "files/ preserves original repository-relative source/evidence paths.\n"
        "The historical executed source snapshot is separate from supplemental review material.\n"
        "Retained receipt bytes are included; this packaging does not re-prove or re-verify them.\n"
        "review-plan.json identifies scope and remaining checks. No reviewer is assigned.\n"
        "NEW_EMPTY is intent, not an independently proven empty inventory.\n"
        "A historical FROST executable remains unavailable; see the qualification bundle.\n"
        "Private identities, keys, cookies, databases and local native tools are not packaged.\n"
        "A reviewer must independently build/check tools; recorded host paths are historical.\n"
        "Verify using a trusted copy of ops/prepare_release.py and an independently obtained\n"
        "manifest SHA-256. Self-consistent hashes alone do not authenticate this package.\n"
        "Do not execute packaged files as part of integrity verification.\n"
    ).encode(), "preparation_metadata")
    manifest = dict(schema="satoshifi.portable-release-review.v1", files=files,
        local_acceptance=dict(path="files/" + baseline_name, sha256=pointer["sha256"],
            source_files=len(baseline["source_sha256"]), python_tests=baseline["python_tests"],
            solidity_tests=baseline["solidity_tests"]),
        selected_pair=prepared["selected_pair"], real_contour_status=baseline["real_contour_status"],
        independent_review_performed=False, qualified=False, fee_qualified=False,
        new_proving_performed=False, sends_transactions=False, production_ready=False)
    # Completion marker is written last. Interrupted packages have no valid manifest.
    with (output / "manifest.json").open("x") as stream:
        json.dump(manifest, stream, indent=2); stream.write("\n")
    return dict(status="REVIEW_INPUT_PREPARED", files=len(files), bytes=sum(v["bytes"] for v in files.values()),
                manifest_sha256=sha(output / "manifest.json"), blockers=prepared["blockers"], production_ready=False)


def verify(directory, expected_manifest_sha256=None):
    root = Path(directory).resolve()
    manifest_path = relative_file(root, "manifest.json")
    if expected_manifest_sha256 is not None and sha(manifest_path) != expected_manifest_sha256:
        raise ValueError("PACKAGE_MANIFEST_HASH_MISMATCH")
    manifest = read_json(manifest_path)
    if manifest.get("schema") != "satoshifi.portable-release-review.v1" or not manifest.get("files"):
        raise ValueError("PACKAGE_MANIFEST_SCHEMA")
    for key in ("independent_review_performed", "qualified", "fee_qualified", "new_proving_performed", "sends_transactions", "production_ready"):
        if manifest.get(key) is not False:
            raise ValueError("PACKAGE_AUTHORITY_FLAG:" + key)
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("PACKAGE_SYMLINK")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(manifest["files"]) | {"manifest.json"}:
        raise ValueError("PACKAGE_FILE_SET_CHANGED")
    for name, pin in manifest["files"].items():
        path = relative_file(root, name)
        if path.stat().st_size != pin["bytes"] or sha(path) != pin["sha256"]:
            raise ValueError("PACKAGE_FILE_CHANGED:" + name)
    prepared = read_json(relative_file(root, "profile.json"))
    if prepared != profile(read_json(relative_file(root, "requested-profile.json"))) or manifest["selected_pair"] != prepared["selected_pair"]:
        raise ValueError("PACKAGE_PROFILE_CHANGED")
    return dict(status="FILE_INTEGRITY_VERIFIED", files=len(manifest["files"]),
                manifest_sha256=sha(manifest_path), manifest_matches_supplied_hash=expected_manifest_sha256 is not None,
                independent_review_performed=False, production_ready=False, executes_packaged_files=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ('prepare','prepare-review'):
        build = sub.add_parser(name,help='participant distribution without proofs' if name=='prepare' else 'local historical evidence review, never participant delivery')
        build.add_argument("--config", type=Path, required=True)
        build.add_argument("--output", type=Path, required=True)
        build.add_argument("--spec", type=Path)
        build.add_argument("--ethereum-network", choices=EVM)
        build.add_argument("--bitcoin-network", choices=BITCOIN)
        build.add_argument("--bitcoin-backend", choices=BACKENDS)
    for name in ('verify','verify-review'):
        check = sub.add_parser(name)
        check.add_argument("--directory", type=Path, required=True)
        check.add_argument("--manifest-sha256")
    args = parser.parse_args()
    if args.command in ('prepare','prepare-review'):
        config = read_json(args.config)
        for name in ("ethereum_network", "bitcoin_network", "bitcoin_backend"):
            if getattr(args, name) is not None:
                config[name] = getattr(args, name)
        if args.command=='prepare-review':result = prepare(config, args.output, spec=args.spec)
        else:
            if args.spec:raise ValueError('SPEC_ONLY_FOR_LOCAL_REVIEW')
            from ops.package_distribution import build as distribution
            selected=profile(config)
            args.output.mkdir(parents=True,exist_ok=False)
            result=distribution(args.output/'participant-source.tar.gz')
            metadata=dict(schema='satoshifi.participant-delivery.v1',archive='participant-source.tar.gz',
                archive_sha256=result['archive_sha256'],selected_pair=selected['selected_pair'],
                proof_generation='EACH_PARTICIPANT_LOCAL',proofs_included=False,qualified=False,production_ready=False)
            path=args.output/'distribution.json';path.write_text(json.dumps(metadata,indent=2)+'\n')
            result['manifest_sha256']=sha(path)
    elif args.command=='verify-review':result = verify(args.directory, args.manifest_sha256)
    else:
        from ops.package_distribution import verify as verify_distribution
        path=args.directory/'distribution.json'
        if args.manifest_sha256 and sha(path)!=args.manifest_sha256:raise ValueError('DISTRIBUTION_MANIFEST_HASH')
        metadata=read_json(path)
        if (metadata.get('schema')!='satoshifi.participant-delivery.v1' or metadata.get('archive')!='participant-source.tar.gz'
                or metadata.get('proofs_included') is not False or metadata.get('qualified') is not False
                or metadata.get('production_ready') is not False
                or metadata.get('proof_generation')!='EACH_PARTICIPANT_LOCAL'):raise ValueError('DISTRIBUTION_METADATA')
        result=verify_distribution(args.directory/'participant-source.tar.gz',metadata['archive_sha256'])
        result['trusted_delivery_manifest_hash_supplied']=args.manifest_sha256 is not None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
