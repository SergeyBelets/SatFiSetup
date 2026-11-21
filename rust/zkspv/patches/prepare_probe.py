#!/usr/bin/env python3
"""Prepare test-only original/corrected bodies with non-consensus wrappers."""
import importlib.util
from pathlib import Path

from apply_patches import DEFAULT_SOURCE, ROOT, patched_source

spec = importlib.util.spec_from_file_location(
    "extract", ROOT.parent / "tools/prepare_arithmetic_probe.py")
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def module(name, source):
    functions = ("bits_to_target", "target_to_bits", "target_to_work", "calculate_new_difficulty")
    bodies = "\n\n".join(extract.function(source, name) for name in functions)
    return f"mod {name} {{\nuse crypto_bigint::{{Encoding, U256}};\n" + bodies + """
pub const fn compact(target: &[u8; 32]) -> u32 { target_to_bits(target) }
pub fn target(bits: u32) -> [u8; 32] { bits_to_target(bits) }
pub fn work(target: &[u8; 32]) -> [u8; 32] { target_to_work(target).to_be_bytes() }
pub fn retarget(start: u32, end: u32, bits: u32) -> [u8; 32] {
    calculate_new_difficulty(start, end, bits,
        U256::from_be_bytes(bits_to_target(0x1d00ffff)), 1_209_600)
}
}
"""


def main():
    corrected = patched_source().decode()
    original = DEFAULT_SOURCE.read_text()
    generated = "// Generated from hash-checked original/patch sources; test-only.\n"
    generated += "#![allow(dead_code)]\n" + module("original", original) + module("corrected", corrected)
    generated += '\ninclude!("regression_tests.rs");\n'
    target = ROOT / "probe/src/lib.rs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generated)
    print("Prepared original and corrected bodies without changing vendor")


if __name__ == "__main__":
    main()
