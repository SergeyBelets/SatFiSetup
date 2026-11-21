"""Record the supplied base without overwriting or trusting the working tree."""
import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# The revision is independent of the archive's content hash.
ARCHIVE_SHA256 = "117118f305e8471cb3e2626bd8623a612036afefe37cb4a1d9d94956a99d3868"


def snapshot(archive: Path, revision: str, output: Path):
    with archive.open("rb") as source:
        archive_digest = hashlib.file_digest(source, "sha256").hexdigest()
    if archive_digest != ARCHIVE_SHA256 or archive.stat().st_size != 199976695:
        raise ValueError("BASE_ARCHIVE_MISMATCH")
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", revision + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    committed = subprocess.check_output(["git", "archive", commit], cwd=ROOT)
    tracked, differences = {}, []
    with tarfile.open(fileobj=io.BytesIO(committed)) as tree, zipfile.ZipFile(archive) as base:
        archived = set(base.namelist())
        for entry in tree:
            if not entry.isfile():
                continue
            name = entry.name
            data = tree.extractfile(entry).read()
            digest = hashlib.sha256(data).hexdigest()
            tracked[name] = digest
            if name not in archived:
                differences.append({"path": name, "kind": "absent_from_archive"})
            elif hashlib.sha256(base.read(name)).hexdigest() != digest:
                differences.append({"path": name, "kind": "different_from_archive"})
    result = {
        "schema": "satoshifi.zkspv.baseline.v1",
        "archive": {"path": archive.name, "bytes": archive.stat().st_size,
                    "sha256": archive_digest, "matches_required_archive": True,
                    "spec_filename": "Архив(1).zip", "archive_commit": None},
        "repository_commit": commit,
        "working_tree_at_task_start": "clean (git status --short returned no entries)",
        "tracked_sha256_at_commit": tracked,
        "commit_vs_archive_differences": differences,
        "archive_only_content": "Build/cache files and macOS metadata are not inferred to be source changes.",
        "historical_evidence": {"directory": "ops/artifacts", "new_run": False},
        "known_baseline_findings": [
            "coinbase _mint_proof uses max(depth,100), whereas contract uses exactly 100",
            "escrow accepts callback pool by self-asserted getters without canonical provenance",
            "mint cache operation/session reuse needs exact live pool nonce and attempt history",
        ],
        "inventory": [
            {"area": "ordinary relay/guard", "existing": True, "reuse": "preserve",
             "symbols": ["MainnetHeaderRelay", "MainnetSPVGuard"], "new_scope": "ZK state/adapter and two-bound Fresh"},
            {"area": "SMP1 mint/treasury", "existing": True, "reuse": "preserve monetary semantics",
             "symbols": ["MiningPoolV1", "BitcoinDepositRegistry", "BitcoinWithdrawalEscrow"]},
            {"area": "on-chain messaging", "existing": True, "reuse": "existing DKG/BTC only",
             "new_scope": "SPV request/status envelope and contract admission"},
            {"area": "header keeper", "existing": True, "reuse": "nonce journal/provenance/finality",
             "new_scope": "durable event/heartbeat planner; qualified prover integration"},
            {"area": "address deposits", "existing": False, "new_scope": "derivation, registry, signing, accounting"},
            {"area": "withdrawal subsidies", "existing": False, "new_scope": "BTC/MP/S consent and actual-fee ledger"},
            {"area": "native gas treasury", "existing": False, "new_scope": "paid service jobs and bounded claims"},
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "Архив.zip")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "ops/artifacts/zkspv/baseline-snapshot.json")
    args = parser.parse_args()
    result = snapshot(args.archive, args.revision, args.output)
    print(json.dumps({"commit": result["repository_commit"], "files": len(result["tracked_sha256_at_commit"]),
                      "differences": len(result["commit_vs_archive_differences"])}))
