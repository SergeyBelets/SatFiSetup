"""Retry a qualified guest with a longer local proving deadline.

The qualified guest, build report and original runner remain byte-identical.
This orchestration layer reuses the original network-denied subprocess runner
and records its own source hash separately from the guest build evidence.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "rust/zkspv/guest-integration"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r0vm", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--height", type=int, choices=(32257, 34272, 36288), default=32257)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 14400:
        raise ValueError("Timeout must be 1..14400 seconds")
    report_path = GUEST / "report.json"
    build = json.loads(report_path.read_text())
    if build["status"] != "LOCAL_GUEST_BUILD_PASS":
        raise ValueError("A qualified local guest build is required")
    sources = dict(build["source_sha256"])
    sources[str(Path(__file__).resolve().relative_to(ROOT))] = sha(__file__)
    for name, expected in sources.items():
        if sha(ROOT / name) != expected:
            raise ValueError("Source changed: " + name)
    if sha(args.r0vm) != build["tools"]["r0vm"]["sha256"]:
        raise ValueError("Runtime changed")
    if sha(args.verifier) != build["receipt_verifier_sha256"]:
        raise ValueError("Receipt verifier changed")
    evidence = GUEST / "evidence"
    for name, expected in build["evidence_sha256"].items():
        if sha(evidence / name) != expected:
            raise ValueError("Build evidence changed: " + name)
    spec = importlib.util.spec_from_file_location("qualified_proof_runner", GUEST / "prove_local.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    args.output.mkdir(parents=True, exist_ok=False)
    free = shutil.disk_usage(args.output).free
    checks = {"resources": dict(free_disk_bytes=free, required_free_disk_bytes=8 * 1024**3,
        status="PASS" if free >= 8 * 1024**3 else "INSUFFICIENT_DISK")}
    (args.output / "resources.json").write_text(json.dumps(checks["resources"], indent=2) + "\n")
    command = [str(args.r0vm), "--elf", str(evidence / "mainnet-transition.r0bf"),
        "--initial-input", str(evidence / f"mainnet-{args.height}.input"),
        "--receipt", str(args.output / "mainnet.receipt"), "--receipt-kind", "composite", "-v"]
    attempt = dict(started_utc=datetime.now(timezone.utc).isoformat(), timeout_seconds=args.timeout,
        command=command, runtime_sha256=sha(args.r0vm), verifier_sha256=sha(args.verifier),
        runner_sha256=sha(__file__), program_image_id=build["program_image_id"],
        program_sha256=sha(evidence / "mainnet-transition.r0bf"),
        input_sha256=sha(evidence / f"mainnet-{args.height}.input"))
    (args.output / "attempt.json").write_text(json.dumps(attempt, indent=2) + "\n")
    if checks["resources"]["status"] == "PASS":
        checks["network_denial"] = runner.isolated([sys.executable, "-c", runner.NETWORK_PROBE],
            args.output / "network-denial.log", 30)
        if checks["network_denial"]["exit_code"] == 0:
            checks["prove"] = runner.isolated(command, args.output / "prove.log", args.timeout)
    receipt = args.output / "mainnet.receipt"
    if checks.get("prove", {}).get("exit_code") == 0 and receipt.is_file():
        checks["verify"] = runner.isolated([args.verifier, evidence / "mainnet-transition.r0bf",
            evidence / f"mainnet-{args.height}.journal", receipt], args.output / "verify.log", 120)
    unchanged = all(sha(ROOT / name) == expected for name, expected in sources.items())
    ok = checks.get("verify", {}).get("exit_code") == 0 and unchanged
    result = dict(schema="satoshifi.isolated-guest-proof.v1", status="LOCAL_STARK_PASS" if ok else "INCOMPLETE",
        production_ready=False, build_report_sha256=sha(report_path), source_sha256=sources,
        source_unchanged=unchanged, program_image_id=build["program_image_id"], height=args.height,
        receipt_kind="composite" if ok else None, timeout_seconds=args.timeout,
        network_profile=runner.PROFILE, dev_mode=False, remote_prover=False, public_transactions=0,
        checks=checks, artifacts={p.name: dict(sha256=sha(p), bytes=p.stat().st_size)
            for p in args.output.iterdir() if p.is_file()}, groth16="NOT_PERFORMED",
        evm_positive_verification="NOT_PERFORMED",
        limitations=["One mainnet append is not full guest branch/retarget/halt qualification",
            "No Groth16 wrapper, EVM positive verification, independent review or production activation"])
    (args.output / "proof-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(status=result["status"], timeout_seconds=args.timeout,
        program_image_id=result["program_image_id"], production_ready=False)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
