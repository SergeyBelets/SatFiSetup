#!/usr/bin/env python3
"""Run original pinned native verifier tests offline from an unchanged archive tree.

Dependency preparation/build is a separate phase. This does not generate a ZK
proof, qualify the whole Bitcoin policy, or modify vendor/production source.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
COMMIT = "2ed54463bb7c4c3f1ff29dcd66d4b28c26fb63c8"
ARCHIVE_SHA256 = "591197b7a10896859d873a757d53ae39edd541414702b17c15a82d3f4d0b12c6"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_source(archive, source):
    if digest(archive) != ARCHIVE_SHA256:
        raise ValueError("Native source archive mismatch")
    prefix = "citrea-" + COMMIT + "/"
    expected = {}
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            if member.isdir():
                continue
            relative = member.name.removeprefix(prefix)
            if not member.isfile() or not member.name.startswith(prefix) or ".." in Path(relative).parts:
                raise ValueError("Unexpected archive member")
            expected[relative] = hashlib.sha256(bundle.extractfile(member).read()).hexdigest()
    actual = {str(path.relative_to(source)) for path in source.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != set(expected):
        raise ValueError("Native source file inventory mismatch")
    for relative, value in expected.items():
        path = source / relative
        if path.is_symlink() or digest(path) != value:
            raise ValueError("Native source changed: " + relative)
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "archive", "cargo", "rustc", "rustdoc", "cargo-home", "target-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    original = verify_source(args.archive, args.source)
    rustc = subprocess.check_output([str(args.rustc), "-Vv"], text=True)
    cargo = subprocess.check_output([str(args.cargo), "--version"], text=True).strip()
    if not rustc.startswith("rustc 1.88.0 ") or not cargo.startswith("cargo 1.88.0 "):
        raise ValueError("Exact upstream Rust/Cargo 1.88.0 required")
    env = dict(os.environ)
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(key, None)
    env.update(CARGO_HOME=str(args.cargo_home.resolve()), CARGO_TARGET_DIR=str(args.target_dir.resolve()),
        RUSTC=str(args.rustc.resolve()), RUSTDOC=str(args.rustdoc.resolve()), CARGO_INCREMENTAL="0",
        CARGO_PROFILE_DEV_DEBUG="0", CARGO_PROFILE_TEST_DEBUG="0", CARGO_BUILD_JOBS="2")
    command = [str(args.cargo), "test", "--locked", "--offline", "-p", "bitcoin-da", "--features", "native",
               "--lib", "verifier::tests", "--", "--nocapture"]
    start = time.monotonic()
    result = subprocess.run(command, cwd=args.source, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    evidence = HERE / "evidence"
    evidence.mkdir(exist_ok=True)
    log = evidence / "original-native.log"
    log.write_text(result.stdout)
    count = re.search(r"test result: ok\. (\d+) passed; 0 failed; 0 ignored;", result.stdout)
    unchanged = verify_source(args.archive, args.source) == original
    status = "PASS" if result.returncode == 0 and count and int(count[1]) == 6 and unchanged else "FAIL"
    report = dict(schema="satoshifi.original-native-header-evidence.v1", status=status, production_ready=False,
        upstream_commit=COMMIT, source_archive_sha256=ARCHIVE_SHA256, original_source_files=len(original),
        source_modified=False, archive_tree_verified_before_and_after=unchanged,
        source_tree_sha256=hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest(),
        compiler=rustc, cargo=cargo, compiler_sha256=digest(args.rustc), cargo_sha256=digest(args.cargo),
        command=command, exit_code=result.returncode, elapsed_seconds=round(time.monotonic() - start, 3),
        tests_passed=int(count[1]) if count else 0, log=str(log.relative_to(HERE)), log_sha256=digest(log),
        environment={key: env[key] for key in ("CARGO_HOME", "CARGO_TARGET_DIR", "RUSTC", "RUSTDOC",
            "CARGO_INCREMENTAL", "CARGO_PROFILE_DEV_DEBUG", "CARGO_PROFILE_TEST_DEBUG", "CARGO_BUILD_JOBS")},
        source_sha256={str(path.relative_to(ROOT)): digest(path) for path in (
            Path(__file__), ROOT / "rust/zkspv/vendor/citrea-2ed54463/crates/bitcoin-da/src/verifier.rs",
            ROOT / "rust/zkspv/vendor/citrea-2ed54463/Cargo.lock")},
        scope="Original bitcoin-da native verifier::tests only, with unchanged source and lockfile",
        limitations=["Six original tests passing does not resolve known consensus/policy incompatibilities",
            "Original work test retains upstream's target-one expectation; this is baseline evidence, not Core compatibility",
            "DA parser/service tests outside verifier::tests were compiled but not executed",
            "Not the corrected source, a guest build, ZK proof, EVM verification or production activation",
            "Dependency retrieval/build preparation was networked; this test command uses Cargo offline",
            "Debug information disabled and incremental compilation disabled to bound disk use"])
    (HERE / "original-native-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=status, tests=report["tests_passed"], source_files=len(original), production_ready=False)))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
