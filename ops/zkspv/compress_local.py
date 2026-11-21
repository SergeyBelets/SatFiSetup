"""Compress the qualified composite STARK on macOS with loopback-only RPC.

RISC Zero ExternalProver uses TCP on 127.0.0.1 between two local processes.
All external IPv4/IPv6 networking remains denied by the OS sandbox.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "rust/zkspv/groth16-integration"
GUEST = ROOT / "rust/zkspv/guest-integration"
PROFILE = '(version 1) (allow default) (deny network*) (allow network-bind network-inbound (local ip "localhost:*")) (allow network-outbound (remote ip "localhost:*"))'
PROBE = '''import errno,socket
with socket.socket() as listener:
 listener.bind(('127.0.0.1',0));listener.listen(1)
 with socket.create_connection(listener.getsockname(),timeout=2):
  accepted,_=listener.accept();accepted.close()
for family,address in [(socket.AF_INET,('192.0.2.1',9)),(socket.AF_INET6,('2001:db8::1',9))]:
 with socket.socket(family,socket.SOCK_STREAM) as sock:
  sock.settimeout(2)
  try:sock.connect(address)
  except OSError as e:assert e.errno in (errno.EPERM,errno.EACCES),repr(e)
  else:raise AssertionError('external networking allowed')
print('Local IPv4 RPC allowed; external IPv4 and IPv6 denied by OS')
'''


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def isolated(command, log, timeout):
    env = dict(PATH="/usr/bin:/bin", TMPDIR="/private/tmp", RISC0_DEV_MODE="0", RUST_LOG="info", NO_COLOR="1")
    started = time.monotonic()
    with log.open("w") as stream:
        process = subprocess.Popen(["/usr/bin/sandbox-exec", "-p", PROFILE] + [str(x) for x in command],
            env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL); process.wait(); code = -signal.SIGKILL
    return dict(exit_code=code, elapsed_seconds=round(time.monotonic() - started, 3), log=log.name, sha256=sha(log))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bridge", "r0vm", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    if platform.system() != "Darwin" or not 1 <= args.timeout <= 14400:
        raise ValueError("Requires macOS and timeout 1..14400 seconds")
    qualified_path = GUEST / "evidence/local-proof/proof-report.json"
    qualified = json.loads(qualified_path.read_text())
    guest = json.loads((GUEST / "report.json").read_text())
    build_path = HERE / "build-report.json"
    build = json.loads(build_path.read_text())
    if qualified["status"] != "LOCAL_STARK_PASS" or qualified["build_report_sha256"] != sha(GUEST / "report.json"):
        raise ValueError("Qualified composite proof required")
    for name, item in qualified["artifacts"].items():
        if sha(qualified_path.parent / name) != item["sha256"]:
            raise ValueError("Qualified proof artifact changed: " + name)
    sources = dict(qualified["source_sha256"], **build["source_sha256"])
    sources[str(Path(__file__).resolve().relative_to(ROOT))] = sha(__file__)
    for name, digest in sources.items():
        if sha(ROOT / name) != digest:
            raise ValueError("Source changed: " + name)
    if sha(args.bridge) != build["bridge_sha256"] or sha(args.r0vm) != guest["tools"]["r0vm"]["sha256"]:
        raise ValueError("Qualified tool changed")
    args.output.mkdir(parents=True, exist_ok=False)
    free = shutil.disk_usage(args.output).free
    checks = {"resources": dict(free_disk_bytes=free, required_free_disk_bytes=8 * 1024**3,
        status="PASS" if free >= 8 * 1024**3 else "INSUFFICIENT_DISK")}
    receipt = qualified_path.parent / "mainnet.receipt"
    command = [args.bridge, "compress", "succinct", args.r0vm, GUEST / "evidence/mainnet-transition.r0bf",
        GUEST / "evidence/mainnet-32257.journal", receipt, args.output / "result"]
    (args.output / "attempt.json").write_text(json.dumps(dict(started_unix=time.time(),
        timeout_seconds=args.timeout, command=[str(x) for x in command], input_receipt_sha256=sha(receipt)), indent=2) + "\n")
    if checks["resources"]["status"] == "PASS":
        checks["network_denial"] = isolated([sys.executable, "-c", PROBE], args.output / "network-denial.log", 30)
        if checks["network_denial"]["exit_code"] == 0:
            checks["compress"] = isolated(command, args.output / "compress.log", args.timeout)
    exported = args.output / "result/receipt.json"
    summary = json.loads(exported.read_text()) if exported.exists() else {}
    unchanged = all(sha(ROOT / name) == digest for name, digest in sources.items())
    ok = checks.get("compress", {}).get("exit_code") == 0 and unchanged and summary.get("receipt_kind") == "succinct" and summary.get("status") == "VERIFIED"
    report = dict(schema="satoshifi.local-compression.v1", status="LOCAL_SUCCINCT_PASS" if ok else "INCOMPLETE",
        production_ready=False, source_sha256=sources, source_unchanged=unchanged, checks=checks,
        input_proof_report_sha256=sha(qualified_path), build_report_sha256=sha(build_path),
        bridge_sha256=sha(args.bridge), r0vm_sha256=sha(args.r0vm), network_profile=PROFILE,
        allowed_internal_hosts=["127.0.0.1", "::1"], external_network_denied=checks.get("network_denial", {}).get("exit_code") == 0,
        dev_mode=False, groth16="NOT_PERFORMED", evm_positive_proof="NOT_PERFORMED", public_transactions=0,
        artifacts={str(p.relative_to(args.output)): dict(sha256=sha(p), bytes=p.stat().st_size)
            for p in args.output.rglob("*") if p.is_file()})
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], checks=checks)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
