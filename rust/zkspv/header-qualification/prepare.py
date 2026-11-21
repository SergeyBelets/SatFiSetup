#!/usr/bin/env python3
"""Prepare full extracted header transitions; never a replacement consensus core.

The original and corrected transition/helper bodies are copied verbatim from
hash-checked sources. Small test adapters replace only transport/type plumbing.
The full native crate, DA transactions, guest and proof are outside this harness.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ZK = HERE.parent
ROOT = ZK.parents[1]
VENDOR = ZK / "vendor/citrea-2ed54463"
sys.path.insert(0, str(ZK / "patches"))
from apply_patches import patched_source

module_spec = importlib.util.spec_from_file_location("extract", ZK / "tools/prepare_arithmetic_probe.py")
extract = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(extract)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def braced(source, marker):
    start = source.index(marker)
    opening = source.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def checked_sources():
    manifest = json.loads((HERE / "series.json").read_text())
    sources = {}
    for relative, digest in manifest["source_sha256"].items():
        data = (ROOT / relative).read_bytes()
        if sha(data) != digest:
            raise ValueError("HEADER_QUALIFICATION_SOURCE_CHANGED: " + relative)
        sources[relative] = data
    original = (VENDOR / "crates/bitcoin-da/src/verifier.rs").read_bytes()
    arithmetic = patched_source()
    if sha(arithmetic) != manifest["arithmetic_input_sha256"]:
        raise ValueError("HEADER_QUALIFICATION_ARITHMETIC_INPUT")
    patch = HERE / manifest["patch"]["file"]
    if sha(patch.read_bytes()) != manifest["patch"]["sha256"]:
        raise ValueError("HEADER_QUALIFICATION_PATCH_CHANGED")
    with tempfile.TemporaryDirectory(prefix="satoshifi-header-patch-") as folder:
        path = Path(folder) / "crates/bitcoin-da/src/verifier.rs"
        path.parent.mkdir(parents=True)
        path.write_bytes(arithmetic)
        subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-d", folder, "-i", str(patch)],
                       check=True, capture_output=True)
        if sha(path.read_bytes()) != manifest.get("chainwork_output_sha256", manifest["patched_sha256"]):
            raise ValueError("HEADER_QUALIFICATION_PATCH_OUTPUT")
        for item in manifest.get("additional_patches", []):
            extra = HERE / item["file"]
            if sha(extra.read_bytes()) != item["sha256"]:
                raise ValueError("HEADER_QUALIFICATION_PATCH_CHANGED")
            if sha(path.read_bytes()) != item["input_sha256"]:
                raise ValueError("HEADER_QUALIFICATION_PATCH_INPUT")
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-d", folder, "-i", str(extra)],
                           check=True, capture_output=True)
            if sha(path.read_bytes()) != item["output_sha256"]:
                raise ValueError("HEADER_QUALIFICATION_PATCH_OUTPUT")
        corrected = path.read_bytes()
    if sha(corrected) != manifest["patched_sha256"]:
        raise ValueError("HEADER_QUALIFICATION_PATCH_OUTPUT")
    return manifest, original.decode(), corrected.decode()


def checked_core_vectors():
    """Accept recorded oracle results only for their exact current inputs."""
    report = json.loads((HERE / "core-target-report.json").read_text())
    manifest = json.loads((HERE / "core-reference-manifest.json").read_text())
    if report.get("status") != "PASS" or report.get("core_commit") != manifest["commit"]:
        raise ValueError("CORE_REFERENCE_REPORT_STATUS")
    if not report.get("source_sha256"):
        raise ValueError("CORE_REFERENCE_REPORT_SOURCES")
    for name, expected in report["source_sha256"].items():
        if sha((ROOT / name).read_bytes()) != expected:
            raise ValueError("CORE_REFERENCE_REPORT_STALE: " + name)
    vectors = HERE / "fixtures/compact-core-vectors.tsv"
    if report.get("vector_file") != str(vectors.relative_to(HERE)) or sha(vectors.read_bytes()) != report["vector_sha256"]:
        raise ValueError("CORE_REFERENCE_VECTORS_CHANGED")
    if len(vectors.read_text().splitlines()) != report["compact_inputs"] or report["pow_limits"] != 4 or report["comparisons"] != report["compact_inputs"] * 4:
        raise ValueError("CORE_REFERENCE_VECTOR_COUNTS")
    return report


METHODS = ("verify_header_chain_mainnet", "verify_header_chain_testnet4",
           "verify_header_chain_signet", "verify_header_chain_regtest", "verify_header_chain_common")
HELPERS = ("verify_timestamp", "verify_target_hash", "bits_to_target", "target_to_bits",
           "target_to_work", "calculate_new_difficulty")


def verifier_module(name, source, checked):
    error = "#[derive(Debug, Copy, Clone, PartialEq, Eq)]\n" + braced(source, "pub enum ValidationError")
    constants = source[source.index("const EXPECTED_EPOCH_TIMESPAN:"):source.index("/// The Bitcoin verifier")]
    helpers = HELPERS + (("checked_chainwork", "checked_target") if checked else ())
    return (f"pub mod {name} {{\nuse super::{{LatestDaState, adapters::HeaderWrapper, constants::*}};\n"
            + "use crypto_bigint::{Encoding, U256, CheckedAdd};\n" + error + "\n" + constants
            + "pub struct BitcoinVerifier;\nimpl BitcoinVerifier {\n"
            + "\n\n".join(extract.function(source, method) for method in METHODS)
            + "\n}\n" + "\n\n".join(extract.function(source, helper) for helper in helpers)
            + """
pub fn step(state: &LatestDaState, header: &HeaderWrapper, testnet4: bool)
    -> Result<LatestDaState, ValidationError> {
    if testnet4 { BitcoinVerifier.verify_header_chain_testnet4(state, header) }
    else { BitcoinVerifier.verify_header_chain_mainnet(state, header) }
}
""" + ("""
pub fn checked_work(total: [u8; 32], work: [u8; 32]) -> Result<[u8; 32], ValidationError> {
    checked_chainwork(total, &U256::from_be_bytes(work))
}
pub fn checked_compact(bits: u32, limit: [u8; 32]) -> Option<[u8; 32]> {
    checked_target(bits, U256::from_be_bytes(limit)).ok()
}
""" if checked else "") + "}\n")


def generate():
    manifest, original, corrected = checked_sources()
    state = (VENDOR / "crates/sovereign-sdk/rollup-interface/src/state_machine/da.rs").read_text()
    constants = (VENDOR / "crates/bitcoin-da/src/network_constants.rs").read_text()
    output = "// Generated by prepare.py from pinned sources. TEST ONLY.\n#![cfg(test)]\n#![allow(dead_code, unused_imports, unused_mut)]\nmod adapters;\n"
    output += "#[derive(Clone, Debug, PartialEq, Eq)]\n" + braced(state, "pub struct LatestDaState") + "\n"
    output += "mod constants {\nuse crypto_bigint::U256;\n#[derive(Clone, Copy)]\n"
    output += braced(constants, "pub struct NetworkConstants") + "\n"
    for network in ("MAINNET", "TESTNET4", "SIGNET", "REGTEST"):
        output += braced(constants, "pub const " + network + "_CONSTANTS:") + ";\n"
    output += "}\n" + verifier_module("original", original, False) + verifier_module("corrected", corrected, True)
    # Checkpoint literals are taken from the unchanged upstream fixture tests.
    for short, method in (("old", "test_header_chain_mainnet_old_blocks"),
                          ("new", "test_header_chain_mainnet_newer_blocks"),
                          ("testnet", "test_header_chain_testnet4")):
        body = extract.function(original, method)
        preamble = body[body.index("let mut block_hash"):body.index("let file =")]
        output += f"fn initial_{short}() -> LatestDaState {{\n{preamble}\nda_state\n}}\n"
    output += 'include!("transition_tests.rs");\n'
    output += '''mod compact_target_regressions {
    use super::corrected::checked_compact as decode;
    include!("compact_test_body.rs");
}\n'''
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify reproducibility without writing")
    args = parser.parse_args()
    output = generate()
    target = HERE / "src/lib.rs"
    if args.check:
        if target.read_text() != output:
            raise SystemExit("Generated header qualification source is stale")
    else:
        target.write_text(output)
    print("Verified full extracted transition bodies; SHA-256 " + sha(output.encode()))


if __name__ == "__main__":
    main()
