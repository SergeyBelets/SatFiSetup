#!/usr/bin/env python3
"""Run the pinned test-only header harness and record exact source/run evidence.

Fetch dependencies separately. Qualification runs Cargo offline; this is not
proof-process network isolation or the full upstream native feature test suite.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib

import prepare

HERE, ROOT = prepare.HERE, prepare.ROOT


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependencies():
    upstream = tomllib.loads((prepare.VENDOR / "Cargo.lock").read_text())
    expected = {(item["name"], item["version"], item.get("source"), item.get("checksum"))
                for item in upstream["package"]}
    own = tomllib.loads((HERE / "Cargo.lock").read_text())
    entries = [item for item in own["package"] if item.get("source")]
    if not entries or any((item["name"], item["version"], item.get("source"), item.get("checksum"))
                          not in expected for item in entries):
        raise ValueError("Qualification dependency differs from pinned upstream lock")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--rustdoc", type=Path, required=True)
    parser.add_argument("--cargo-home", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()
    compiler = subprocess.check_output([str(args.rustc), "-Vv"], text=True)
    cargo = subprocess.check_output([str(args.cargo), "--version"], text=True).strip()
    if not compiler.startswith("rustc 1.88.0 ") or not cargo.startswith("cargo 1.88.0 "):
        raise ValueError("Exact upstream Rust/Cargo 1.88.0 required")
    manifest, _, _ = prepare.checked_sources()
    if (HERE / "src/lib.rs").read_text() != prepare.generate():
        raise ValueError("Generated source is stale; run prepare.py and review changes")
    resolved = dependencies()
    paths = {ROOT / name for name in manifest["source_sha256"]}
    paths |= {path for path in HERE.rglob("*") if path.is_file()
              and not any(part in ("target", "evidence", "__pycache__") for part in path.relative_to(HERE).parts)
              and path.name != "report.json"}
    paths |= {prepare.VENDOR / "Cargo.lock", ROOT / "tests/zkspv/test_header_qualification.py"}
    sources = {str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)}
    env = dict(os.environ)
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(key, None)
    env.update(CARGO_HOME=str(args.cargo_home.resolve()), CARGO_TARGET_DIR=str(args.target_dir.resolve()),
               RUSTC=str(args.rustc.resolve()), RUSTDOC=str(args.rustdoc.resolve()), CARGO_INCREMENTAL="0")
    evidence = HERE / "evidence"
    evidence.mkdir(exist_ok=True)
    commands = []
    for profile in ("debug", "release"):
        command = [str(args.cargo), "test", "--locked", "--offline", "--manifest-path",
                   str(HERE / "Cargo.toml"), "--lib"]
        if profile == "release":
            command.append("--release")
        command += ["--", "--nocapture"]
        start = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        log = evidence / (profile + ".log")
        log.write_text(result.stdout)
        count = re.search(r"test result: ok\. (\d+) passed; 0 failed; 0 ignored;", result.stdout)
        commands.append(dict(profile=profile, command=command, exit_code=result.returncode,
            tests_passed=int(count[1]) if count else 0, elapsed_seconds=round(time.monotonic() - start, 3),
            log=str(log.relative_to(HERE)), log_sha256=digest(log)))
        if result.returncode or not count or int(count[1]) != 9:
            raise RuntimeError("Header qualification did not pass; see " + str(log))
    if any(digest(ROOT / name) != value for name, value in sources.items()):
        raise ValueError("Sources changed during qualification")
    report = dict(schema="satoshifi.header-transition-evidence.v1", recorded_on=str(datetime.date.today()),
        status="LOCAL_HEADER_TRANSITION_PASS", production_ready=False,
        scope="Complete extracted Citrea header transition bodies, not the full original native crate or a proof",
        upstream_commit=manifest["upstream_commit"], arithmetic_parent=manifest["parent_series"],
        checked_chainwork_patch=manifest["patch"], patched_verifier_sha256=manifest["patched_sha256"],
        vendor_changed=False, production_changed=False, compiler=compiler, cargo=cargo,
        compiler_sha256=digest(args.rustc), cargo_sha256=digest(args.cargo),
        dependency_lock_sha256=digest(HERE / "Cargo.lock"), upstream_matching_dependencies=resolved,
        source_sha256=sources, sources_unchanged_during_run=True, commands=commands,
        validation=dict(unique_historical_headers=6111, mainnet_headers=4074, testnet4_headers=2037,
            historical_retarget_boundaries=6, boundary_resume_cases=6,
            exact_all_field_original_vs_corrected_comparison=True,
            independent_rust_bitcoin_pow_work_and_mainnet_retarget=True,
            checked_work_sum_and_overflow=True, explicit_checkpoint_and_header_order=True),
        unavailable=dict(full_upstream_native_suite="UNKNOWN", compact_decoding_validation="UNKNOWN",
            authenticated_time_bound="NOT_IMPLEMENTED", fork_choice_lca_history="NOT_IMPLEMENTED",
            real_proof="UNKNOWN", evm_verifier="UNKNOWN", independent_review="UNKNOWN"),
        limitations=["Only Mainnet/Testnet4 historical transitions exercised; no Signet/Regtest qualification",
            "Adapter decodes/hashes with pinned rust-bitcoin; cached-hash and Borsh wire behavior not covered",
            "Snapshot replay uses already-known full states; it is not authenticated LCA discovery or fork choice",
            "Relative work begins at the upstream fixture's synthetic zero-work checkpoint",
            "Cargo offline enforces dependency resolution without downloads, not proof-process egress isolation",
            "Dependency fetching/toolchain installation happened separately with networking",
            "No production guest, state/adapter, custody activation or independent external review"])
    (HERE / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], tests=[item["tests_passed"] for item in commands],
        unique_historical_headers=6111, exact_upstream_dependencies=len(resolved), production_ready=False)))


if __name__ == "__main__":
    main()
