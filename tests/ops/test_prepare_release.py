"""Offline preparation boundaries; no Core, RPC, Docker, signing or proving."""
import json
import shutil

import pytest

from ops import prepare_release as release


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def config():
    return dict(version="1", purpose="PREPARATION_ONLY", ethereum_network=None,
                bitcoin_network=None, bitcoin_backend=None, deployment_intent="NEW_EMPTY",
                operator_inputs={key: None for key in release.INPUTS})


@pytest.mark.parametrize("ethereum", release.EVM)
@pytest.mark.parametrize("bitcoin", release.BITCOIN)
def test_independent_relay_pairs_follow_existing_bootstrap(ethereum, bitcoin):
    from signer_agent.bitcoin_network import relay_artifact, PUBLIC_NETWORKS
    pair = release.network_pair(ethereum, bitcoin, "relay")
    assert pair["selection_complete"]
    assert pair["chain_id"] == str(release.EVM[ethereum])
    assert pair["relay_artifact"] == relay_artifact(bitcoin)
    if bitcoin in PUBLIC_NETWORKS:
        assert pair["bitcoin_genesis_hash"] == PUBLIC_NETWORKS[bitcoin][1]
    assert pair["mode"] == ("backed" if ethereum == "mainnet" or bitcoin != "regtest" else "integration")
    assert pair["evm_finalized_required"] == (ethereum == "mainnet")
    assert pair["positive_deployment_fee_required"] == (ethereum == "mainnet")
    assert pair["production_ready"] is False


@pytest.mark.parametrize("ethereum", release.EVM)
def test_zk_mainnet_independent_of_evm(ethereum):
    pair = release.network_pair(ethereum, "mainnet", "zk-state-v2")
    assert pair["bitcoin_policy_version"] == "2"
    assert pair["relay_artifact"] == "ZkBitcoinStateV2"
    assert pair["uses_real_bitcoin"] is True
    assert pair["external_token_and_feed_required"] is True


@pytest.mark.parametrize("ethereum", release.EVM)
@pytest.mark.parametrize("bitcoin", ["regtest"])
def test_test_network_cannot_use_mainnet_or_test_only_zk_guest(ethereum, bitcoin):
    with pytest.raises(ValueError, match="ZK_BACKEND_REQUIRES_BITCOIN_MAINNET"):
        release.network_pair(ethereum, bitcoin, "zk-state-v2")


@pytest.mark.parametrize("selection", [(True,"mainnet","relay"), ("goerli","mainnet","relay"),
                                     ("sepolia","testnet3","relay"), ("sepolia","mainnet","auto"),
                                     ([],"mainnet","relay")])
def test_unknown_selection_has_no_fallback(selection):
    with pytest.raises(ValueError, match="UNSUPPORTED_NETWORK_SELECTION"):
        release.network_pair(*selection)


def test_unknown_pair_and_new_empty_intent_do_not_grant_acceptance(config):
    prepared = release.profile(config)
    assert len(prepared["network_matrix"]) == 9
    assert sum(len(row["backends"]) for row in prepared["network_matrix"]) == 15
    assert not prepared["selected_pair"]["selection_complete"]
    assert prepared["live_migration"]["status"] == "EMPTY_INVENTORY_REVIEW_REQUIRED"
    assert prepared["live_migration"]["not_applicable_accepted"] is False
    assert prepared["confirmations"] == dict(ordinary_default=6, preserve_stricter_consumer_policy=True,
        coinbase_mint=100, coinbase_spend_and_fee_ingress_minimum=101)
    assert prepared["funding"]["explicit_confederation_own_funding_allowed"] is True
    assert prepared["funding"]["approved_budget_inferred"] is False
    assert not prepared["production_ready"] and not prepared["global_onchain_halt_guaranteed"]


def test_even_filled_references_and_inventory_path_need_independent_review(config):
    config.update(ethereum_network="mainnet", bitcoin_network="mainnet", bitcoin_backend="zk-state-v2")
    config["operator_inputs"] = {k: "public/review.json" for k in release.INPUTS}
    config["operator_inputs"].update(evm_confirmations="12", evm_finality_policy="finalized")
    prepared = release.profile(config)
    assert "independent_qualification_pending" in prepared["blockers"]
    assert prepared["live_migration"]["not_applicable_accepted"] is False
    assert prepared["ready"] is False


@pytest.mark.parametrize("depth", ["1", "11", "0", "012", "-12", "1e2", True])
def test_mainnet_confirmations_cannot_be_weakened(config, depth):
    config.update(ethereum_network="mainnet", bitcoin_network="testnet4", bitcoin_backend="relay")
    config["operator_inputs"]["evm_confirmations"] = depth
    with pytest.raises(ValueError):
        release.profile(config)


def test_mainnet_finalized_required_even_with_test_bitcoin(config):
    config.update(ethereum_network="mainnet", bitcoin_network="regtest", bitcoin_backend="relay")
    config["operator_inputs"]["evm_finality_policy"] = "latest"
    with pytest.raises(ValueError, match="RELEASE_EVM_FINALIZED_REQUIRED"):
        release.profile(config)


def test_no_private_extra_fields_or_embedded_secrets(config):
    config["operator_inputs"]["identity_file"] = "private/identity.json"
    with pytest.raises(ValueError, match="RELEASE_OPERATOR_INPUTS"):
        release.profile(config)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repository"; root.mkdir()
    base = "ops/artifacts/zkspv/acceptance-test/"
    source = "signer-agent/signer_agent/engine.py"
    path = root / source; path.parent.mkdir(parents=True); path.write_text("# Original tested source\n")
    program = root / "rust/guest/program.r0bf"; program.parent.mkdir(parents=True); program.write_bytes(b"program")
    journal = root / "rust/guest/state.journal"; journal.write_bytes(b"journal")
    receipt = root / "rust/guest/receipt.bin"; receipt.write_bytes(b"receipt")
    rechecks = [dict(id="retained-test-only", program="/old/checkout/rust/guest/program.r0bf",
        journal="/old/checkout/rust/guest/state.journal", receipt="/old/checkout/rust/guest/receipt.bin", exit_code=0,
        stdout=json.dumps(dict(status="VERIFIED", image_id="ab"*32, journal_sha256=release.sha(journal),
            receipt_sha256=release.sha(receipt), dev_mode_disabled=True, wrong_image_rejected=True, modified_journal_rejected=True)))]
    dump(root / (base + "receipt-rechecks.json"), rechecks)
    artifact = root / "web3/contracts/out/Engine.sol/Engine.json"
    dump(artifact, {"bytecode": "compiled fixture"})
    sources = {source: release.sha(path), "rust/guest/program.r0bf": release.sha(program)}
    qualification = dict(source_sha256=sources, independent_review_performed=False,
        production_build={"Engine":dict(source="contracts/src/Engine.sol", artifact_sha256=release.sha(artifact))})
    dump(root / (base + "qualification-review.json"), qualification)
    dump(root / "ops/artifacts/zkspv/upstream-lock.json", {"files": []})
    baseline = dict(source_sha256=sources,
        artifact_sha256={base + n:release.sha(root/(base+n)) for n in ("receipt-rechecks.json", "qualification-review.json")},
        production_ready=False, qualified=False, fee_qualified=False,
        python_tests=dict(tests=2, failures=0, errors=0, skipped=0), solidity_tests={"fixture":1},
        real_contour_status="DEFERRED_BY_USER_FOR_DISK_CAPACITY")
    dump(root / (base + "local-acceptance-report.json"), baseline)
    dump(root / "ops/artifacts/zkspv/local-acceptance-current.json", dict(report=base+"local-acceptance-report.json", sha256=release.sha(root/(base+"local-acceptance-report.json"))))
    monkeypatch.setattr(release, "SUPPLEMENTAL", ("ops/artifacts/zkspv/upstream-lock.json",))
    monkeypatch.setattr(release, "HISTORICAL_BUILD_REPORTS", ())
    return root, path


def test_portable_package_keeps_original_pins_and_never_executes(config, repository, tmp_path):
    root, _ = repository; output = tmp_path / "package"
    result = release.prepare(config, output, root=root)
    moved = tmp_path / "relocated"; output.rename(moved)
    shutil.rmtree(root)  # Verification must not depend on the original checkout/tools.
    checked = release.verify(moved, result["manifest_sha256"])
    assert checked["status"] == "FILE_INTEGRITY_VERIFIED"
    assert checked["manifest_matches_supplied_hash"] is True
    assert not checked["executes_packaged_files"] and not checked["production_ready"]
    index = release.read_json(moved/"retained-receipts.json")
    assert index[0]["receipt"] == "files/rust/guest/receipt.bin"
    assert release.read_json(moved/"review-plan.json")["independent_review_performed"] is False
    assert all(t["status"] == "NOT_REVIEWED" for t in release.read_json(moved/"review-plan.json")["tasks"])
    choices = list((moved/"network-profiles").glob("*.json"))
    assert len(choices) == 15
    assert all(release.profile(release.read_json(p))["selected_pair"]["selection_complete"] for p in choices)


@pytest.mark.parametrize("kind", ["source", "evidence", "compiled", "receipt"])
def test_changed_pinned_inputs_cannot_be_relabelled_current(config, repository, tmp_path, kind):
    root, source = repository
    targets = dict(source=source, evidence=root/"ops/artifacts/zkspv/acceptance-test/qualification-review.json",
                   compiled=root/"web3/contracts/out/Engine.sol/Engine.json", receipt=root/"rust/guest/receipt.bin")
    targets[kind].write_bytes(b"changed")
    with pytest.raises(ValueError, match="CHANGED|MISSING"):
        release.prepare(config, tmp_path/"package", root=root)


@pytest.mark.parametrize("mutation", ["file", "extra", "symlink", "manifest", "authority"])
def test_portable_verification_rejects_tampering(config, repository, tmp_path, mutation):
    root, _ = repository; output = tmp_path/"package"
    result = release.prepare(config, output, root=root)
    if mutation == "file":
        (output/"profile.json").write_text("{}")
    elif mutation == "extra":
        (output/"unlisted.txt").write_text("extra")
    elif mutation == "symlink":
        (output/"profile.json").unlink(); (output/"profile.json").symlink_to(root/"ops/artifacts/zkspv/upstream-lock.json")
    else:
        manifest = release.read_json(output/"manifest.json")
        manifest["production_ready"] = True
        dump(output/"manifest.json", manifest)
    with pytest.raises(ValueError, match="PACKAGE_"):
        release.verify(output, result["manifest_sha256"] if mutation == "manifest" else None)


def test_output_reuse_is_rejected(config, repository, tmp_path):
    root, _ = repository; output = tmp_path/"package"
    release.prepare(config, output, root=root)
    with pytest.raises(FileExistsError):
        release.prepare(config, output, root=root)


def test_other_pair_templates_do_not_reuse_graph_authority_or_budget(config, repository, tmp_path):
    root, _ = repository
    config.update(ethereum_network="sepolia", bitcoin_network="testnet4", bitcoin_backend="relay")
    config["operator_inputs"].update(evm_confirmations="1", authority_roster_and_threshold="selected-roster.json",
                                     treasury_and_own_funding_caps="selected-budget.json")
    output = tmp_path/"package"
    release.prepare(config, output, root=root)
    mainnet = release.read_json(output/"network-profiles/mainnet-mainnet-zk-state-v2.json")
    assert all(value is None for value in mainnet["operator_inputs"].values())
    assert release.read_json(output/"requested-profile.json") == config


def repin_fixture_baseline(root, name, value):
    pointer_path = root/"ops/artifacts/zkspv/local-acceptance-current.json"
    pointer = release.read_json(pointer_path)
    baseline_path = root/pointer["report"]
    baseline = release.read_json(baseline_path)
    dump(root/name, value)
    baseline["artifact_sha256"][name] = release.sha(root/name)
    dump(baseline_path, baseline)
    pointer["sha256"] = release.sha(baseline_path)
    dump(pointer_path, pointer)


def test_wrong_acceptance_pointer_hash_rejected(config, repository, tmp_path):
    root, _ = repository
    path = root/"ops/artifacts/zkspv/local-acceptance-current.json"
    pointer = release.read_json(path); pointer["sha256"] = "00"*32; dump(path, pointer)
    with pytest.raises(ValueError, match="LOCAL_ACCEPTANCE_POINTER_HASH"):
        release.prepare(config, tmp_path/"package", root=root)


def test_native_build_sources_are_included_and_cannot_drift(config, repository, tmp_path):
    root, _ = repository
    native = root/"rust/native.rs"; native.write_text("native source")
    repin_fixture_baseline(root, "ops/artifacts/zkspv/acceptance-test/host-build.json",
                           dict(source_sha256={"rust/native.rs":release.sha(native)}))
    output = tmp_path/"package"
    release.prepare(config, output, root=root)
    assert (output/"files/rust/native.rs").read_bytes() == native.read_bytes()
    native.write_text("changed native source")
    with pytest.raises(ValueError, match="PACKAGE_COPY_SOURCE_CHANGED"):
        release.prepare(config, tmp_path/"changed-package", root=root)
    assert not (tmp_path/"changed-package/manifest.json").exists()


def test_retained_receipt_record_must_keep_negative_and_non_dev_checks(config, repository, tmp_path):
    root, _ = repository
    name = "ops/artifacts/zkspv/acceptance-test/receipt-rechecks.json"
    rows = release.read_json(root/name)
    result = json.loads(rows[0]["stdout"]); result["wrong_image_rejected"] = False
    rows[0]["stdout"] = json.dumps(result)
    repin_fixture_baseline(root, name, rows)
    with pytest.raises(ValueError, match="RETAINED_RECEIPT_RECORD_INVALID"):
        release.prepare(config, tmp_path/"package", root=root)


def test_wrong_specification_not_bundled(config, repository, tmp_path):
    root, _ = repository
    dump(root/"ops/zkspv-required-scenarios.json", {"specification_sha256":"00"*32})
    spec = tmp_path/"requirements.txt"; spec.write_text("wrong specification")
    with pytest.raises(ValueError, match="SPECIFICATION_HASH_MISMATCH"):
        release.prepare(config, tmp_path/"package", root=root, spec=spec)


def test_historical_build_difference_is_disclosed_without_rewriting_old_pin(config, repository, tmp_path, monkeypatch):
    root, source = repository
    report_name = "historical-build.json"
    report = {"source_sha256":{"signer-agent/signer_agent/engine.py":"00"*32}}
    dump(root/report_name, report)
    monkeypatch.setattr(release, "HISTORICAL_BUILD_REPORTS", (report_name,))
    monkeypatch.setattr(release, "SUPPLEMENTAL", (*release.SUPPLEMENTAL, report_name))
    output = tmp_path/"package"
    release.prepare(config, output, root=root)
    status = release.read_json(output/"historical-build-source-status.json")
    assert status["differences"][0]["recorded_sha256"] == "00"*32
    assert status["differences"][0]["current_sha256"] == release.sha(source)
    assert release.read_json(output/"files/historical-build.json") == report


@pytest.mark.parametrize("name", ["../outside", "/outside", "a/../b", "a//b", "a/./b", "a\\b"])
def test_package_paths_cannot_escape_or_alias(tmp_path, name):
    with pytest.raises(ValueError, match="INVALID_PACKAGE_PATH"):
        release.relative_file(tmp_path, name)


def test_duplicate_keys_rejected(tmp_path):
    path = tmp_path/"duplicate.json"; path.write_text('{"production_ready":false,"production_ready":true}')
    with pytest.raises(ValueError, match="DUPLICATE_JSON_KEY"):
        release.read_json(path)


@pytest.mark.parametrize("ethereum", release.EVM)
def test_testnet4_zk_uses_separate_state_artifact(ethereum):
    pair=release.network_pair(ethereum,"testnet4","zk-state-v2")
    assert pair["relay_artifact"]=="ZkBitcoinTestnet4StateV2"
    assert pair["bitcoin_policy_version"]=="2" and pair["mode"]=="backed"
