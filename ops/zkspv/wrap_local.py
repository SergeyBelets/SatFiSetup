"""Local Docker Groth16 wrapping with a pinned image and enforced network isolation."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys

from .compress_local import GUEST, HERE, ROOT, PROFILE, PROBE


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("wrapper", "bridge", "r0vm", "docker", "docker-socket", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not 1 <= args.timeout <= 14400:
        raise ValueError("This launcher is qualified only for local macOS ARM64 and 1..14400 seconds")
    input_path = HERE / "evidence/succinct-local/report.json"
    previous = json.loads(input_path.read_text())
    if previous["status"] != "LOCAL_SUCCINCT_PASS":
        raise ValueError("A verified succinct receipt is required")
    for name, item in previous["artifacts"].items():
        if sha(input_path.parent / name) != item["sha256"]:
            raise ValueError("Succinct artifact changed: " + name)
    build_path = HERE / "wrapper-build-report.json"
    build = json.loads(build_path.read_text())
    sources = dict(previous["source_sha256"], **build["source_sha256"])
    for file in (Path(__file__).resolve(), ROOT / "ops/zkspv/groth16_docker.py", HERE / "container-pin.json"):
        sources[str(file.relative_to(ROOT))] = sha(file)
    for name, digest in sources.items():
        if sha(ROOT / name) != digest:
            raise ValueError("Source changed: " + name)
    for arg, key in ((args.wrapper, "wrapper_sha256"), (args.bridge, "bridge_sha256")):
        if sha(arg) != build[key]:
            raise ValueError("Tool changed: " + key)
    if sha(args.r0vm) != previous["r0vm_sha256"]:
        raise ValueError("Runtime changed")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    from signer_agent.zk_isolation import environment
    from .groth16_docker import Broker
    from .state_trial_prove import run

    pin = json.loads((HERE / "container-pin.json").read_text())
    image = pin["repository"] + "@" + pin["arm64"]["digest"]
    broker = Broker(args.output, args.docker.resolve(strict=True), args.docker_socket.resolve(strict=True),
                    image, "arm64", args.timeout)
    env = environment(RUST_LOG="info")
    commands = dict(
        network_denial=[sys.executable, "-c", PROBE],
        wrap=[args.wrapper, args.r0vm, GUEST / "evidence/mainnet-transition.r0bf",
              GUEST / "evidence/mainnet-32257.journal", input_path.parent / "result/receipt.bin", args.output / "groth16.receipt"],
        verify=[args.bridge, "inspect", GUEST / "evidence/mainnet-transition.r0bf",
                GUEST / "evidence/mainnet-32257.journal", args.output / "groth16.receipt", args.output / "verified"])
    checks = {}
    checks["network_denial"] = run(commands["network_denial"], args.output / "network_denial.log", PROFILE, env, 180)
    if checks["network_denial"]["exit_code"] == 0:
        with broker as job:
            checks["wrap"] = run(commands["wrap"], args.output / "wrap.log", PROFILE,
                                 env | job.env, args.timeout, broker=job)
        checks["wrap"]["broker"] = job.diagnostics()
        if checks["wrap"]["exit_code"] == 0:
            checks["verify"] = run(commands["verify"], args.output / "verify.log", PROFILE, env, 180)
    observed = broker.observed
    unchanged = all(sha(ROOT / path) == digest for path, digest in sources.items())
    ok = checks.get("verify", {}).get("exit_code") == 0 and unchanged and bool(observed)
    report = dict(schema="satoshifi.local-groth16.v1", status="LOCAL_GROTH16_PASS" if ok else "INCOMPLETE",
        production_ready=False, source_sha256=sources, source_unchanged=unchanged, checks=checks,
        input_report_sha256=sha(input_path), build_report_sha256=sha(build_path), image=image,
        image_id=observed.get("image"), container_network_mode=observed.get("network_mode"),
        network_profile=PROFILE + "; single-job Docker broker", allowed_internal_hosts=["127.0.0.1", "::1"],
        dev_mode=False, public_transactions=0, evm_positive_proof="NOT_PERFORMED",
        artifacts={str(p.relative_to(args.output)): dict(sha256=sha(p), bytes=p.stat().st_size)
                   for p in args.output.rglob("*") if p.is_file()})
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], checks=checks)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
