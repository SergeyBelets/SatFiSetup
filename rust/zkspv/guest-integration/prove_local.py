#!/usr/bin/env python3
"""Bounded local macOS STARK run with enforced network denial and offline verification.

Preparation/downloads must finish first. No remote prover, dev mode, Groth16,
public transaction, or production activation is performed by this command.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
PROFILE = "(version 1) (allow default) (deny network*)"
NETWORK_PROBE = '''import errno,socket
for family,address in [(socket.AF_INET, ('127.0.0.1',9)),(socket.AF_INET6, ('::1',9))]:
 s=socket.socket(family,socket.SOCK_STREAM)
 try: s.connect(address)
 except OSError as e: assert e.errno in (errno.EPERM,errno.EACCES), repr(e)
 else: raise AssertionError('network access allowed')
 finally: s.close()
print('IPv4 and IPv6 network operations denied by OS')
'''

def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def isolated(command, log, timeout):
    env = dict(PATH="/usr/bin:/bin", TMPDIR="/private/tmp", RISC0_DEV_MODE="0", RUST_LOG="info", NO_COLOR="1")
    command = ["/usr/bin/sandbox-exec", "-p", PROFILE] + [str(x) for x in command]
    start = time.monotonic()
    with log.open("w") as out:
        process = subprocess.Popen(command, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); process.wait()
            code = -signal.SIGKILL
    return dict(exit_code=code, elapsed_seconds=round(time.monotonic()-start,3), log=log.name, sha256=sha(log))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--r0vm", type=Path, required=True)
    p.add_argument("--verifier", type=Path, required=True)
    p.add_argument("--height", type=int, choices=(32257,34272,36288), default=32257)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if not 1 <= a.timeout <= 1800: raise ValueError("Timeout must be 1..1800 seconds")
    report_path = HERE / "report.json"; build = json.loads(report_path.read_text())
    root = HERE.parents[2]
    for name, expected in build["source_sha256"].items():
        if sha(root / name) != expected: raise ValueError("Source changed since guest build: " + name)
    if sha(a.r0vm) != build["tools"]["r0vm"]["sha256"]: raise ValueError("Runtime changed")
    if sha(a.verifier) != build["receipt_verifier_sha256"]: raise ValueError("Receipt verifier changed")
    evidence = HERE / "evidence"
    for name, expected in build["evidence_sha256"].items():
        if sha(evidence / name) != expected: raise ValueError("Build evidence changed: " + name)
    a.output.mkdir(parents=True, exist_ok=False)
    checks = {}
    free = shutil.disk_usage(a.output).free
    checks["resources"] = dict(free_disk_bytes=free, required_free_disk_bytes=8 * 1024**3,
                               status="PASS" if free >= 8 * 1024**3 else "INSUFFICIENT_DISK")
    (a.output / "resources.json").write_text(json.dumps(checks["resources"], indent=2) + "\n")
    receipt = a.output / "mainnet.receipt"
    if checks["resources"]["status"] == "PASS":
        checks["network_denial"] = isolated([sys.executable, "-c", NETWORK_PROBE], a.output / "network-denial.log", 30)
        if checks["network_denial"]["exit_code"] == 0:
            checks["prove"] = isolated([a.r0vm, "--elf", evidence / "mainnet-transition.r0bf", "--initial-input",
                evidence / f"mainnet-{a.height}.input", "--receipt", receipt, "--receipt-kind", "composite", "-v"],
                a.output / "prove.log", a.timeout)
    if checks.get("prove", {}).get("exit_code") == 0 and receipt.is_file():
        checks["verify"] = isolated([a.verifier, evidence / "mainnet-transition.r0bf",
            evidence / f"mainnet-{a.height}.journal", receipt], a.output / "verify.log", 120)
    ok = checks.get("verify", {}).get("exit_code") == 0
    result = dict(schema="satoshifi.isolated-guest-proof.v1", status="LOCAL_STARK_PASS" if ok else "INCOMPLETE",
        production_ready=False, build_report_sha256=sha(report_path), source_sha256=build["source_sha256"],
        program_image_id=build["program_image_id"], height=a.height, receipt_kind="composite" if ok else None,
        network_profile=PROFILE, dev_mode=False, remote_prover=False, public_transactions=0,
        checks=checks, artifacts={x.name:dict(sha256=sha(x),bytes=x.stat().st_size) for x in a.output.iterdir() if x.is_file()},
        groth16="NOT_PERFORMED", evm_positive_verification="NOT_PERFORMED",
        limitations=["One mainnet append is not full guest branch/retarget/halt qualification",
                     "No Groth16 wrapper, EVM positive verification, independent review or production activation"])
    (a.output / "proof-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(dict(status=result["status"], program_image_id=result["program_image_id"], production_ready=False)))
    return 0 if ok else 1

if __name__ == "__main__": raise SystemExit(main())
