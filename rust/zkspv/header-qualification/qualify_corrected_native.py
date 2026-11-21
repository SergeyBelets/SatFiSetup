#!/usr/bin/env python3
"""Test reviewed corrections in the full native crate, restoring the archive tree.

Only use a dedicated qualification checkout with no other build running. This
changes one verified temporary source file during the run; vendor is untouched.
"""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import prepare
from qualify_native import COMMIT, ARCHIVE_SHA256, digest, verify_source

HERE, ROOT = prepare.HERE, prepare.ROOT


def align_native_tests(source):
    old = """        target[31] = 1;
        let work = target_to_work(&target);
        assert_eq!(work, U256::MAX);"""
    new = """        target[31] = 1;
        let work = target_to_work(&target);
        // Bitcoin Core GetBlockProof: floor(2^256 / (target + 1)).
        assert_eq!(work, U256::from_be_hex(
            "8000000000000000000000000000000000000000000000000000000000000000"));"""
    if source.count(old) != 1:
        raise ValueError("Pinned target-one expectation not found exactly once")
    result = source.replace(old, new) + """

#[cfg(test)]
mod satoshifi_reviewed_work_tests {
    use crypto_bigint::{Encoding, U256};
    use super::{checked_chainwork, ValidationError};

    #[test]
    fn checked_cumulative_work_preserves_representable_sum() {
        let maximum = U256::MAX.to_be_bytes();
        assert_eq!(checked_chainwork(maximum, &U256::ZERO), Ok(maximum));
        assert_eq!(checked_chainwork(U256::MAX.wrapping_sub(&U256::ONE).to_be_bytes(), &U256::ONE), Ok(maximum));
        assert_eq!(checked_chainwork(maximum, &U256::ONE), Err(ValidationError::ChainworkOverflow));
    }
}
"""
    body = (HERE / "src/compact_test_body.rs").read_text().replace(
        'include_str!("../fixtures/compact-core-vectors.tsv")',
        'include_str!(r"' + str(HERE / "fixtures/compact-core-vectors.tsv") + '")')
    return result + """
#[cfg(test)]
mod satoshifi_reviewed_compact_tests {
    fn decode(bits: u32, limit: [u8; 32]) -> Option<[u8; 32]> {
        super::checked_target(bits, crypto_bigint::U256::from_be_bytes(limit)).ok()
    }
""" + body + "\n}\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "archive", "cargo", "rustc", "rustdoc", "cargo-home", "target-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    original_files = verify_source(args.archive, args.source)
    manifest, original, corrected = prepare.checked_sources()
    core = prepare.checked_core_vectors()
    compiler = subprocess.check_output([str(args.rustc), "-Vv"], text=True)
    cargo = subprocess.check_output([str(args.cargo), "--version"], text=True).strip()
    if not compiler.startswith("rustc 1.88.0 ") or not cargo.startswith("cargo 1.88.0 "):
        raise ValueError("Exact upstream Rust/Cargo 1.88.0 required")
    source = args.source / "crates/bitcoin-da/src/verifier.rs"
    if source.read_text() != original:
        raise ValueError("Qualification input mismatch")
    native = align_native_tests(corrected)
    evidence = HERE / "evidence"
    evidence.mkdir(exist_ok=True)
    adjustment = "".join(difflib.unified_diff(corrected.splitlines(True), native.splitlines(True),
        fromfile="a/crates/bitcoin-da/src/verifier.rs", tofile="b/crates/bitcoin-da/src/verifier.rs"))
    (evidence / "native-test-adjustment.patch").write_text(adjustment)
    paths = {ROOT / name for name in manifest["source_sha256"]}
    paths |= {ROOT / name for name in core["source_sha256"]}
    paths |= {Path(__file__), HERE / "prepare.py", HERE / "qualify_native.py", HERE / "series.json",
              HERE / manifest["patch"]["file"], evidence / "native-test-adjustment.patch",
              HERE / "src/compact_test_body.rs", HERE / core["vector_file"], HERE / "core-target-report.json"}
    paths |= {HERE / item["file"] for item in manifest.get("additional_patches", [])}
    sources = {str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)}
    env = dict(os.environ)
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(key, None)
    env.update(CARGO_HOME=str(args.cargo_home.resolve()), CARGO_TARGET_DIR=str(args.target_dir.resolve()),
        RUSTC=str(args.rustc.resolve()), RUSTDOC=str(args.rustdoc.resolve()), CARGO_INCREMENTAL="0",
        CARGO_PROFILE_DEV_DEBUG="0", CARGO_PROFILE_TEST_DEBUG="0", CARGO_BUILD_JOBS="2")
    command = [str(args.cargo), "test", "--locked", "--offline", "-p", "bitcoin-da", "--features", "native",
               "--lib", "verifier::", "--", "--nocapture"]
    start = time.monotonic()
    try:
        source.write_text(native)
        result = subprocess.run(command, cwd=args.source, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for relative, expected in original_files.items():
            actual = args.source / relative
            expected = hashlib.sha256(native.encode()).hexdigest() if actual == source else expected
            if digest(actual) != expected:
                raise ValueError("Unexpected source change during native correction test")
    finally:
        source.write_text(original)
    restored = verify_source(args.archive, args.source) == original_files
    if any(digest(ROOT / name) != value for name, value in sources.items()):
        raise ValueError("Qualification inputs changed during native correction test")
    log = evidence / "corrected-native.log"
    log.write_text(result.stdout)
    count = re.search(r"test result: ok\. (\d+) passed; 0 failed; 0 ignored;", result.stdout)
    status = "PASS" if result.returncode == 0 and count and int(count[1]) == 10 and restored else "FAIL"
    report = dict(schema="satoshifi.corrected-native-header-evidence.v1", status=status, production_ready=False,
        upstream_commit=COMMIT, source_archive_sha256=ARCHIVE_SHA256, original_source_files=len(original_files),
        temporary_checkout_restored=restored, vendor_modified=False, production_modified=False,
        patched_verifier_sha256=manifest["patched_sha256"],
        compiled_verifier_with_tests_sha256=hashlib.sha256(native.encode()).hexdigest(),
        test_adjustment="One target-one expectation aligned with Core; one checked-work and three compact tests added; no original test skipped",
        compact_target_core_comparisons=core["comparisons"],
        compiler=compiler, cargo=cargo, compiler_sha256=digest(args.rustc), cargo_sha256=digest(args.cargo),
        command=command, exit_code=result.returncode, tests_passed=int(count[1]) if count else 0,
        elapsed_seconds=round(time.monotonic() - start, 3), log=str(log.relative_to(HERE)), log_sha256=digest(log),
        source_sha256=sources, sources_unchanged_during_run=True,
        scope="Five reference corrections compiled in actual native bitcoin-da and exercised through its verifier tests",
        limitations=["Native tests are not real ZK proof generation or EVM verification",
            "Other bitcoin-da DA/service tests are outside this run",
            "Compact arithmetic sampling is not exhaustive or full Core-node validation",
            "Authenticated time and fork/LCA/history policy remain open",
            "Independent critical-code review and complete license qualification remain open"])
    (HERE / "corrected-native-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=status, tests=report["tests_passed"], restored=restored, production_ready=False)))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
