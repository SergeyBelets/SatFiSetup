"""Reproducibility/provenance of the test-only extracted header harness."""
import importlib.util
import json
from pathlib import Path
import shutil
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "rust/zkspv/header-qualification"


def prepare_module():
    spec = importlib.util.spec_from_file_location("header_qualification_prepare", HERE / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_full_transition_bodies_match_pinned_sources():
    module = prepare_module()
    actual = (HERE / "src/lib.rs").read_text()
    assert module.generate() == actual
    assert "#![cfg(test)]" in actual
    _, original, corrected = module.checked_sources()
    for name in module.METHODS + module.HELPERS:
        assert module.extract.function(original, name) in actual
        assert module.extract.function(corrected, name) in actual


def test_all_resolved_external_dependencies_match_upstream_lock():
    own = tomllib.loads((HERE / "Cargo.lock").read_text())
    upstream = tomllib.loads((ROOT / "rust/zkspv/vendor/citrea-2ed54463/Cargo.lock").read_text())
    exact = {(item["name"], item["version"], item.get("source"), item.get("checksum"))
             for item in upstream["package"]}
    dependencies = [item for item in own["package"] if item.get("source")]
    assert dependencies
    assert all((item["name"], item["version"], item.get("source"), item.get("checksum")) in exact
               for item in dependencies)


def test_changed_patch_is_rejected_before_compilation(tmp_path, monkeypatch):
    module = prepare_module()
    shutil.copyfile(HERE / "series.json", tmp_path / "series.json")
    manifest = json.loads((HERE / "series.json").read_text())
    (tmp_path / manifest["patch"]["file"]).write_text("unreviewed replacement")
    monkeypatch.setattr(module, "HERE", tmp_path)
    with pytest.raises(ValueError, match="HEADER_QUALIFICATION_PATCH_CHANGED"):
        module.checked_sources()


def test_truncated_historical_fixture_is_rejected_before_compilation(tmp_path, monkeypatch):
    module = prepare_module()
    manifest = json.loads((HERE / "series.json").read_text())
    for relative in manifest["source_sha256"]:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    fixture = tmp_path / "rust/zkspv/header-qualification/fixtures/testnet4-40310-42346.txt"
    fixture.write_bytes(b"\n".join(fixture.read_bytes().splitlines()[:-1]))
    monkeypatch.setattr(module, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="HEADER_QUALIFICATION_SOURCE_CHANGED"):
        module.checked_sources()


@pytest.mark.parametrize("change,error", [
    ("bytes", "HEADER_QUALIFICATION_PATCH_CHANGED"),
    ("input", "HEADER_QUALIFICATION_PATCH_INPUT"),
    ("output", "HEADER_QUALIFICATION_PATCH_OUTPUT"),
])
def test_compact_patch_requires_exact_chain_of_sources(tmp_path, monkeypatch, change, error):
    module = prepare_module()
    manifest = json.loads((HERE / "series.json").read_text())
    for patch in [manifest["patch"], *manifest["additional_patches"]]:
        shutil.copyfile(HERE / patch["file"], tmp_path / patch["file"])
    compact = manifest["additional_patches"][0]
    if change == "bytes":
        (tmp_path / compact["file"]).write_text("unreviewed replacement")
    else:
        compact[change + "_sha256"] = "0" * 64
    (tmp_path / "series.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(module, "HERE", tmp_path)
    with pytest.raises(ValueError, match=error):
        module.checked_sources()


def test_core_reference_matches_current_sources_and_vector_count():
    module = prepare_module()
    report = module.checked_core_vectors()
    assert report["compact_inputs"] == 11266
    assert report["comparisons"] == 45064


@pytest.mark.parametrize("change,error", [
    ("source", "CORE_REFERENCE_REPORT_STALE"),
    ("vectors", "CORE_REFERENCE_VECTORS_CHANGED"),
])
def test_stale_core_reference_is_rejected(tmp_path, monkeypatch, change, error):
    module = prepare_module()
    for name in ("core-target-report.json", "core-reference-manifest.json", "fixtures/compact-core-vectors.tsv"):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / name, destination)
    if change == "vectors":
        (tmp_path / "fixtures/compact-core-vectors.tsv").write_text("truncated")
    else:
        report = json.loads((tmp_path / "core-target-report.json").read_text())
        report["source_sha256"]["rust/zkspv/header-qualification/core_target_vectors.py"] = "0" * 64
        (tmp_path / "core-target-report.json").write_text(json.dumps(report))
    monkeypatch.setattr(module, "HERE", tmp_path)
    with pytest.raises(ValueError, match=error):
        module.checked_core_vectors()
