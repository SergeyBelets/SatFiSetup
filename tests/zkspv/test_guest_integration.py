"""Cross-check actual guest build evidence without promoting it to proof readiness."""
import hashlib
import json
from pathlib import Path
import tomllib
from ops.zkspv.historical_sources import resolve, audit

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "rust/zkspv/guest-integration"
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def report(): return json.loads((HERE / "report.json").read_text())

def test_guest_build_and_source_evidence_are_bound():
    r = report()
    assert r["status"] == "LOCAL_GUEST_BUILD_PASS" and r["production_ready"] is False
    assert r["upstream_files"] == 887
    assert r["modified_upstream_files"] == ["crates/bitcoin-da/src/verifier.rs"]
    # Original proof/build sources remain authenticated after ordinary contracts evolve.
    # Archived bytes do not qualify the current contracts or alter the old report.
    for name, digest in r["source_sha256"].items(): assert sha(resolve(name,digest)) == digest, name
    scope=audit(r['source_sha256'])
    assert scope['historical_reproduction_complete'] and not scope['current_source_qualification']
    for name, digest in r["evidence_sha256"].items(): assert sha(HERE / "evidence" / name) == digest, name
    for p in ("native_debug", "native_release"): assert r["builds"][p]["tests_passed"] == 8
    assert r["builds"]["guest"]["exit_code"] == 0
    assert len(bytes.fromhex(r["program_image_id"])) == 32
    assert (HERE / "evidence/mainnet-transition.elf").read_bytes().startswith(b"\x7fELF")

def test_actual_mainnet_journals_bind_headers_work_and_prior_state():
    headers = (ROOT / "web3/test/fixtures/mainnet/headers-32246-36288.bin").read_bytes()
    expected = (ROOT / "web3/test/fixtures/mainnet/expected-32246-36288.bin").read_bytes()
    for height in (32257,34272,36288):
        frame = (HERE / f"evidence/mainnet-{height}.input").read_bytes()
        assert int.from_bytes(frame[:4], "little") == len(frame)-4 == 31805
        journal = (HERE / f"evidence/mainnet-{height}.journal").read_bytes()
        assert len(journal) == 1216
        w = [journal[i:i+32] for i in range(0,1216,32)]
        i = (height-32246)*80; raw = headers[i:i+80]
        assert w[15] == hashlib.sha256(hashlib.sha256(raw).digest()).digest()
        assert int.from_bytes(w[16], "big") == height
        j = (height-32246)*68
        assert w[17] == expected[j+32:j+64]
        assert int.from_bytes(w[12], "big") == height-1
        assert int.from_bytes(w[23], "big") == 1
        assert int.from_bytes(w[37], "big") == int.from_bytes(w[34], "big")+1

def test_guest_sdk_is_pinned_without_remote_or_development_features():
    manifest = tomllib.loads((HERE / "guest/Cargo.toml").read_text())
    sdk = manifest["dependencies"]["risc0-zkvm"]
    assert sdk["version"] == "=3.0.3" and sdk["default-features"] is False
    assert set(sdk["features"]) == {"std", "unstable"}
    verifier = tomllib.loads((HERE / "image-tool/Cargo.toml").read_text())["dependencies"]["risc0-zkvm"]
    assert verifier["default-features"] is False and "disable-dev-mode" in verifier["features"]
    assert report()["proof_status"] == "NOT_PERFORMED_BY_THIS_COMMAND"
    assert report()["evm_positive_proof"] == "NOT_PERFORMED"
