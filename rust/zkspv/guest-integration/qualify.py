#!/usr/bin/env python3
"""Build the actual guest offline and record source-bound evidence, not a proof."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib
import prepare

HERE, ZK, ROOT = prepare.HERE, prepare.ZK, prepare.ROOT

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def snapshot():
    parent_path = ZK / "state-adapter/report.json"
    parent = json.loads(parent_path.read_text())
    hashes = dict(parent["source_sha256"])
    hashes[str(parent_path.relative_to(ROOT))] = sha(parent_path)
    for name, value in hashes.items():
        if sha(ROOT / name) != value:
            raise ValueError("Prior source changed: " + name)
    paths = [p for p in HERE.rglob("*") if p.is_file() and p.name not in ("report.json", "proof-report.json")
             and not any(x in ("target", "evidence", "__pycache__") for x in p.relative_to(HERE).parts)]
    paths += [ROOT / "tests/zkspv/test_guest_integration.py"]
    hashes.update({str(p.relative_to(ROOT)): sha(p) for p in paths if p.is_file()})
    return hashes

def dependencies(source):
    result = {}
    for local, original in [("Cargo.lock", "Cargo.lock"), ("guest/Cargo.lock", "guests/risc0/light-client-proof/bitcoin/Cargo.lock"),
                            ("image-tool/Cargo.lock", "Cargo.lock")]:
        upstream = {(p["name"], p["version"], p.get("source")): p.get("checksum")
                    for p in tomllib.loads((source / original).read_text())["package"]}
        external = [p for p in tomllib.loads((HERE / local).read_text())["package"] if "source" in p]
        for p in external:
            key = (p["name"], p["version"], p["source"])
            if key not in upstream or upstream[key] != p.get("checksum"):
                raise ValueError("Dependency differs from pinned upstream: " + str(key))
        result[local] = len(external)
    return result

def run(command, env, log):
    start = time.monotonic()
    with log.open("w") as out:
        result = subprocess.run([str(x) for x in command], env=env, stdout=out, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); see {log}")
    return dict(status="PASS", exit_code=0, elapsed_seconds=round(time.monotonic()-start, 3),
                log=log.name, sha256=sha(log))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "archive", "build-dir", "target-dir", "cargo", "rustc", "rustdoc", "guest-rustc", "cargo-home", "r0vm"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    pins = json.loads((HERE / "toolchain.json").read_text())
    if sha(a.guest_rustc) != pins["guest_rustc_sha256"] or sha(a.r0vm) != pins["r0vm_sha256"]:
        raise ValueError("Pinned macOS guest compiler/runtime required")
    if not subprocess.check_output([a.rustc, "--version"], text=True).startswith("rustc 1.88.0 "):
        raise ValueError("Pinned native compiler required")
    sources = snapshot(); deps = dependencies(a.source)
    prepared = prepare.prepare(a.source, a.archive, a.build_dir)
    evidence = HERE / "evidence"; evidence.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RUST", "CARGO_", "RISC0_", "BONSAI_"))}
    env.update(CARGO_HOME=str(a.cargo_home), RUSTC=str(a.rustc), RUSTDOC=str(a.rustdoc), CARGO_BUILD_JOBS="2")
    manifest = a.build_dir / "guest-integration/Cargo.toml"
    build = {}; test_vectors = []
    for profile in ("debug", "release"):
        vector_dir = a.target_dir / (profile + "-vectors")
        env.update(CARGO_TARGET_DIR=str(a.target_dir / "native"), SATOSHIFI_GUEST_VECTORS=str(vector_dir))
        command = [a.cargo, "test", "--locked", "--offline", "--manifest-path", manifest]
        if profile == "release": command += ["--release"]
        log = evidence / ("native-" + profile + ".log")
        result = run(command, env, log)
        if "test result: ok. 8 passed; 0 failed; 0 ignored;" not in log.read_text():
            raise ValueError("Incomplete native test coverage")
        result["tests_passed"] = 8; build["native_" + profile] = result
        test_vectors.append({x.name: sha(x) for x in vector_dir.iterdir() if x.is_file()})
    if test_vectors[0] != test_vectors[1] or len(test_vectors[0]) != 6:
        raise ValueError("Debug/release vector mismatch")
    for name in test_vectors[0]:
        shutil.copyfile(a.target_dir / "debug-vectors" / name, evidence / name)
    env.pop("SATOSHIFI_GUEST_VECTORS")
    guest_env = dict(env, RUSTC=str(a.guest_rustc), CARGO_TARGET_DIR=str(a.target_dir / "guest"), RISC0_FEATURE_bigint2="")
    # Exact risc0-build 3.0.3 flags plus deterministic path remapping.
    flags = ["-C", "passes=lower-atomic", "-C", "link-arg=-Ttext=0x00200800", "-C", "link-arg=--fatal-warnings",
             "-C", "panic=abort", "--cfg", 'getrandom_backend="custom"',
             "--remap-path-prefix=" + str(a.build_dir) + "=/satoshifi-zkspv",
             "--remap-path-prefix=" + str(a.cargo_home) + "=/cargo",
             "--remap-path-prefix=" + str(a.guest_rustc.parent.parent) + "=/rust"]
    guest_env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    guest_manifest = manifest.parent / "guest/Cargo.toml"
    build["guest"] = run([a.cargo, "build", "--locked", "--offline", "--release", "--target", "riscv32im-risc0-zkvm-elf",
                          "--manifest-path", guest_manifest], guest_env, evidence / "guest-build.log")
    user_elf = evidence / "mainnet-transition.elf"
    shutil.copyfile(a.target_dir / "guest/riscv32im-risc0-zkvm-elf/release/satoshifi-mainnet-transition-guest", user_elf)
    env["CARGO_TARGET_DIR"] = str(a.target_dir / "image-tool")
    build["image_tool"] = run([a.cargo, "build", "--locked", "--offline", "--manifest-path", manifest.parent / "image-tool/Cargo.toml"],
                              env, evidence / "image-tool-build.log")
    program = a.target_dir / "mainnet-transition.r0bf"
    image_id = subprocess.check_output([a.target_dir / "image-tool/debug/satoshifi-guest-image", user_elf, program], text=True).strip()
    runtime_id = subprocess.check_output([a.r0vm, "--elf", program, "--id"], env=env, text=True).strip()
    if not re.fullmatch("[0-9a-f]{64}", image_id) or image_id != runtime_id:
        raise ValueError("Independent image ID mismatch")
    shutil.copyfile(program, evidence / program.name)
    actual = {str(x.relative_to(a.build_dir / "vendor-native")): sha(x)
              for x in (a.build_dir / "vendor-native").rglob("*") if x.is_file()}
    if actual != prepared["upstream_files"] or snapshot() != sources:
        raise ValueError("Source changed during qualification")
    for local in deps:
        if sha(HERE / local) != sha(manifest.parent / local):
            raise ValueError("Build modified locked dependencies")
    tools = {name: dict(version=subprocess.check_output([path, "--version"], text=True).strip(), sha256=sha(path))
             for name, path in [("cargo", a.cargo), ("rustc", a.rustc), ("guest_rustc", a.guest_rustc), ("r0vm", a.r0vm)]}
    report = dict(schema="satoshifi.guest-integration.v1", status="LOCAL_GUEST_BUILD_PASS", production_ready=False,
        source_sha256=sources, upstream_files=len(actual), modified_upstream_files=prepared["modified_upstream_files"],
        prepared_upstream_sha256=hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest(),
        dependencies_matching_upstream=deps, tools=tools, flags=flags, builds=build, vectors=test_vectors[0],
        program_image_id=image_id, journal_bytes=1216, guest_input_max_bytes=2281212,
        receipt_verifier_sha256=sha(a.target_dir / "image-tool/debug/verify_receipt"),
        evidence_sha256={x.name: sha(x) for x in evidence.iterdir() if x.is_file()},
        proof_status="NOT_PERFORMED_BY_THIS_COMMAND", evm_positive_proof="NOT_PERFORMED",
        limitations=["Native tests and a compiled guest are not a cryptographic proof",
                     "See separately verified proof-report.json for any isolated local receipt",
                     "Production image selection, Groth16/EVM positive verification, consumer activation and independent review remain open"])
    (HERE / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], program_image_id=image_id, native_tests=16, production_ready=False)))

if __name__ == "__main__": main()
