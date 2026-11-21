"""Receipt import must not promote a STARK or changed bytes to Groth16 acceptance."""
import json
from pathlib import Path
import shutil

import pytest

from ops.zkspv.evm_receipt import checked_bundle

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "rust/zkspv/groth16-integration/evidence/evm-preflight"


@pytest.fixture
def bundle(tmp_path):
    target = tmp_path / "bundle"
    shutil.copytree(EVIDENCE / "native-verification", target)
    metadata = json.loads((target / "receipt.json").read_text())
    expected = {key: metadata[key] for key in ("image_id", "program_sha256", "journal_sha256")}
    return target, expected


def test_real_composite_cannot_be_imported_as_groth16(bundle):
    target, expected = bundle
    assert checked_bundle(target, expected, require_groth16=False)["receipt_kind"] == "composite"
    with pytest.raises(ValueError, match="Genuine Groth16 required"):
        checked_bundle(target, expected)


@pytest.mark.parametrize("name", ["receipt.bin", "journal.bin"])
def test_changed_export_bytes_rejected(bundle, name):
    target, expected = bundle
    path = target / name
    data = bytearray(path.read_bytes()); data[0] ^= 1; path.write_bytes(data)
    with pytest.raises(ValueError, match="Export bytes changed"):
        checked_bundle(target, expected, require_groth16=False)


def test_different_guest_binding_rejected(bundle):
    target, expected = bundle
    expected["image_id"] = "00" * 32
    with pytest.raises(ValueError, match="binding mismatch"):
        checked_bundle(target, expected, require_groth16=False)


def test_evm_preflight_does_not_claim_positive_proof():
    report = json.loads((EVIDENCE / "report.json").read_text())
    assert report["status"] == "PREFLIGHT_PASS"
    assert report["positive_proof"] == report["state_transition_acceptance"] == "NOT_PERFORMED"
    assert report["receipt_kind"] == "composite" and report["selector"] == "73c457ba"
    assert report["ordinary_runtime_match"] is True
    assert report["checks"][0]["case"] == "zero_seal_rejected"
    assert report["checks"][0]["status"] == 0


def test_actual_groth16_and_evm_evidence_agree():
    directory = EVIDENCE.parent
    wrap = json.loads((directory / "groth16-local/report.json").read_text())
    export = json.loads((directory / "groth16-local/verified/receipt.json").read_text())
    evm = json.loads((directory / "evm-positive/report.json").read_text())
    assert wrap["status"] == "LOCAL_GROTH16_PASS" and wrap["source_unchanged"]
    assert wrap["container_network_mode"] == "none" and wrap["dev_mode"] is False
    assert export["receipt_kind"] == evm["receipt_kind"] == "groth16"
    for name in ("image_id", "journal_sha256", "receipt_sha256"):
        assert export[name] == evm[name]
    assert len(bytes.fromhex(export["seal_hex"])) == 260
    assert evm["status"] == "LOCAL_GROTH16_EVM_PASS" and evm["positive_proof"] == "PASS"
    assert [r["status"] for r in evm["checks"]] == [1, 0, 0, 0]
    assert evm["state_transition_acceptance"] == "NOT_PERFORMED"
