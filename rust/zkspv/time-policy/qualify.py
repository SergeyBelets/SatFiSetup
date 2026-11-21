#!/usr/bin/env python3
"""Offline component qualification; no guest, proof, production activation or RPC."""
import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
HEADER = HERE.parent / "header-qualification"
sys.path.insert(0, str(HEADER))
import prepare
from qualify_corrected_native import align_native_tests
from qualify_native import verify_source

FORGE_STD = ROOT / "web3/lib/openzeppelin-contracts/lib/forge-std/src"
SOLIDITY = ["web3/contracts/src/core/BitcoinHeaderTime.sol", "web3/test/BitcoinHeaderTime.t.sol",
            "web3/test/fixtures/header-time-v1.json", "web3/foundry.toml"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot():
    # Prior five-patch evidence is retained unchanged; include its exact inputs.
    parent = json.loads((HEADER / "corrected-native-report.json").read_text())
    paths = {ROOT / name for name in parent["source_sha256"]}
    for name, expected in parent["source_sha256"].items():
        if sha(ROOT / name) != expected:
            raise ValueError("Parent native qualification source is stale: " + name)
    paths |= {HEADER / "corrected-native-report.json"}
    paths |= {path for path in HERE.rglob("*") if path.is_file()
              and not any(part in ("target", "evidence", "__pycache__") for part in path.relative_to(HERE).parts)
              and not path.name.endswith("report.json")}
    paths |= {ROOT / name for name in SOLIDITY}
    paths |= set(FORGE_STD.rglob("*.sol"))
    paths |= {ROOT / "tests/zkspv/test_header_time_policy.py"}
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths)}


def run(command, cwd, env, log):
    started = time.monotonic()
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log.write_text(result.stdout)
    record = dict(command=list(map(str, command)), exit_code=result.returncode,
                  elapsed_seconds=round(time.monotonic() - started, 3),
                  log=str(log.relative_to(HERE)), log_sha256=sha(log))
    return record, result.stdout


def rust_pass(record, output, count):
    match = re.search(r"test result: ok\. (\d+) passed; 0 failed; 0 ignored;", output)
    record["tests_passed"] = int(match[1]) if match else 0
    if record["exit_code"] or record["tests_passed"] != count:
        raise RuntimeError("Rust qualification failed: " + record["log"])


def native_source(original, corrected):
    output = align_native_tests(corrected)
    # Include the application implementation unchanged in this temporary crate.
    output += '\n#[cfg(test)]\n#[path = r"' + str(HERE / "src/lib.rs") + '"]\nmod satoshifi_time_policy;\n'
    output += "#[cfg(test)]\nmod satoshifi_time_policy_tests {\n"
    for short, method, fixture in (
        ("old", "test_header_chain_mainnet_old_blocks", "headers-40310-42346.txt"),
        ("new", "test_header_chain_mainnet_newer_blocks", "headers-872918-874954.txt"),
    ):
        body = prepare.extract.function(original, method)
        preamble = body[body.index("let mut block_hash"):body.index("let file =")]
        output += f"fn initial_{short}() -> LatestDaState {{\n{preamble}\nda_state\n}}\n"
        fixture_path = prepare.VENDOR / "crates/bitcoin-da/test_data/mainnet" / fixture
        output += f'const {short.upper()}: &str = include_str!(r"{fixture_path}");\n'
    return output + (HERE / "native_tests.rs").read_text() + "\n}\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cargo", "rustc", "rustdoc", "cargo-home", "target-dir", "forge",
                 "source", "archive", "native-target-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    rustc = subprocess.check_output([str(args.rustc), "-Vv"], text=True)
    cargo = subprocess.check_output([str(args.cargo), "--version"], text=True)
    if not rustc.startswith("rustc 1.88.0 ") or not cargo.startswith("cargo 1.88.0 "):
        raise ValueError("Exact pinned Rust/Cargo 1.88.0 required")
    manifest, original, corrected = prepare.checked_sources()
    prepare.checked_core_vectors()
    sources = snapshot()
    evidence = HERE / "evidence"
    evidence.mkdir(exist_ok=True)
    env = dict(os.environ)
    for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"):
        env.pop(key, None)
    env.update(CARGO_HOME=str(args.cargo_home.resolve()), CARGO_TARGET_DIR=str(args.target_dir.resolve()),
               RUSTC=str(args.rustc.resolve()), RUSTDOC=str(args.rustdoc.resolve()), CARGO_INCREMENTAL="0")
    runs = {}
    for profile in ("debug", "release"):
        command = [str(args.cargo), "test", "--locked", "--offline", "--manifest-path",
                   str(HERE / "Cargo.toml"), "--test", "policy"]
        if profile == "release":
            command.append("--release")
        record, output = run(command, ROOT, env, evidence / (profile + ".log"))
        rust_pass(record, output, 10)
        runs[profile] = record
        print(profile + ": 10 passed", flush=True)
    build, output = run([str(args.cargo), "build", "--locked", "--offline", "--manifest-path",
                        str(HERE / "Cargo.toml"), "--bin", "encode_times"], ROOT, env, evidence / "encoder-build.log")
    if build["exit_code"]:
        raise RuntimeError("Rust encoder build failed")
    encoder = args.target_dir / "debug/encode_times"
    vectors, output = run([sys.executable, str(HERE / "generate_vectors.py"), "--encoder", str(encoder), "--check"],
                          ROOT, env, evidence / "vectors.log")
    if vectors["exit_code"]:
        raise RuntimeError("Shared Rust/Solidity fixtures changed")
    runs["encoder_build"], runs["shared_vectors"] = build, vectors

    with tempfile.TemporaryDirectory(prefix="satoshifi-header-time-forge-") as folder:
        project = Path(folder)
        for name in SOLIDITY:
            target = project / Path(name).relative_to("web3")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        shutil.copytree(FORGE_STD, project / FORGE_STD.relative_to(ROOT / "web3"))
        results = project / "test-results.json"
        command = [str(args.forge), "test", "--root", str(project), "--offline", "--out", "out", "--cache-path", "cache",
                   "--match-contract", "BitcoinHeaderTimeTest", "--gas-limit", "16777216", "--enable-tx-gas-limit",
                   "--isolate", "--fuzz-seed", "0x01", "--fuzz-runs", "256", "--color", "never", "-vv",
                   "--json-file", str(results)]
        record, output = run(command, project, env, evidence / "solidity.log")
        if not results.is_file():
            raise RuntimeError("Solidity test results unavailable; see solidity.log")
        shutil.copyfile(results, evidence / "solidity-results.json")
        suites = json.loads(results.read_text())
        tests = [test for suite in suites.values() for test in suite.get("test_results", {}).values()]
        record["tests_passed"] = sum(test["status"] == "Success" for test in tests)
        if record["exit_code"] or len(tests) != 9 or record["tests_passed"] != 9:
            raise RuntimeError("Solidity policy qualification failed; see solidity.log")
        artifact = json.loads((project / "out/BitcoinHeaderTime.t.sol/HeaderTimePolicyHarness.json").read_text())
        bytecode = bytes.fromhex(artifact["deployedBytecode"]["object"].removeprefix("0x"))
        record.update(fuzz_runs_per_test=256, shared_vectors=15,
            compiler=artifact["metadata"]["compiler"], compiler_settings=artifact["metadata"]["settings"],
            fixture_runtime_bytes=len(bytecode), fixture_runtime_sha256=hashlib.sha256(bytecode).hexdigest(),
            production_artifacts_untouched=True)
        runs["solidity"] = record
        print("Solidity: 9 passed", flush=True)

    original_files = verify_source(args.archive, args.source)
    source = args.source / "crates/bitcoin-da/src/verifier.rs"
    if source.read_text() != original:
        raise ValueError("Native source does not match the pinned original")
    native = native_source(original, corrected)
    adjustment = "".join(difflib.unified_diff(corrected.splitlines(True), native.splitlines(True),
        fromfile="a/crates/bitcoin-da/src/verifier.rs", tofile="b/crates/bitcoin-da/src/verifier.rs"))
    (evidence / "native-test-adjustment.patch").write_text(adjustment)
    native_env = dict(env, CARGO_TARGET_DIR=str(args.native_target_dir.resolve()), CARGO_PROFILE_DEV_DEBUG="0",
                      CARGO_PROFILE_TEST_DEBUG="0", CARGO_BUILD_JOBS="2")
    command = [str(args.cargo), "test", "--locked", "--offline", "-p", "bitcoin-da", "--features", "native",
               "--lib", "verifier::", "--", "--nocapture"]
    try:
        source.write_text(native)
        record, output = run(command, args.source, native_env, evidence / "native.log")
        for relative, expected in original_files.items():
            path = args.source / relative
            if path == source:
                expected = hashlib.sha256(native.encode()).hexdigest()
            if sha(path) != expected:
                raise ValueError("Unexpected change during native qualification")
    finally:
        source.write_text(original)
    if verify_source(args.archive, args.source) != original_files:
        raise ValueError("Native checkout was not restored")
    rust_pass(record, output, 15)
    record.update(original_archive_files=len(original_files), temporary_checkout_restored=True,
                  compiled_verifier_sha256=hashlib.sha256(native.encode()).hexdigest(),
                  historical_mainnet_headers=4074, native_time_tests=5, prior_verifier_tests=10)
    runs["native"] = record
    print("Native verifier: 15 passed; checkout restored", flush=True)
    if any(sha(ROOT / name) != expected for name, expected in sources.items()):
        raise ValueError("Qualification sources changed during execution")
    report = dict(schema="satoshifi.header-time-policy-evidence.v1", status="LOCAL_COMPONENT_PASS",
        production_ready=False, source_sha256=sources, sources_unchanged_during_run=True,
        compiler=rustc, cargo=cargo, rustc_sha256=sha(args.rustc), cargo_sha256=sha(args.cargo),
        forge=subprocess.check_output([str(args.forge), "--version"], text=True), forge_sha256=sha(args.forge),
        encoder_sha256=sha(encoder), parent_patched_verifier_sha256=manifest["patched_sha256"], runs=runs,
        evidence_sha256={str(path.relative_to(HERE)): sha(path) for path in sorted(evidence.iterdir()) if path.is_file()},
        scope="Nonempty linear verified-header summary plus EVM acceptance-time component; native historical integration and scalar ABI fixtures",
        unavailable=dict(authenticated_proof_binding="NOT_IMPLEMENTED", production_guest="NOT_IMPLEMENTED",
            canonical_fork_history="NOT_IMPLEMENTED", state_adapter="NOT_IMPLEMENTED", independent_review="UNKNOWN"),
        limitations=["Scalar fixtures do not authenticate headers or replace real proof verification",
            "Timestamp callback must bind the same header to the trusted core; integration currently tested only in native qualification",
            "Chunk time-summary merge does not authenticate state/root continuity",
            "Last processed timestamp is not canonical-head selection or Fresh policy",
            "Existing header qualification dependencies are pinned; this no_std component adds none",
            "No contract deployment, RPC, transaction, real proof or production activation"])
    (HERE / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status=report["status"], rust_tests=20, native_tests=15, solidity_tests=9, production_ready=False)))


if __name__ == "__main__":
    main()
