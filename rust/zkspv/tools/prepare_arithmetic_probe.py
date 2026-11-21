#!/usr/bin/env python3
"""Copy pinned upstream functions verbatim into a *test-only* qualification probe.

This is neither the production core nor the complete upstream baseline. No
consensus algorithm is replaced: every probed body is byte-for-byte upstream.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor/citrea-2ed54463/crates/bitcoin-da/src/verifier.rs"
EXPECTED_SHA256 = "a7784140e36beedee14f89b0077d88c98e272b35dab2c7c9971c97ed2b05c444"


def function(source: str, name: str) -> str:
    start = source.index("fn " + name + "(")
    start = source.rfind("\n", 0, start) + 1
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    data = SOURCE.read_bytes()
    if hashlib.sha256(data).hexdigest() != EXPECTED_SHA256:
        raise SystemExit("pinned verifier hash mismatch; refusing to prepare probe")
    functions = ["bits_to_target", "target_to_bits", "target_to_work", "calculate_new_difficulty"]
    generated = "// Generated verbatim by tools/prepare_arithmetic_probe.py; test-only.\n"
    generated += "use crypto_bigint::{Encoding, U256};\n\n"
    generated += "\n\n".join(function(data.decode(), name) for name in functions)
    generated += "\n\ninclude!(\"qualification_tests.rs\");\n"
    target = ROOT / "qualification-probe/src/lib.rs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generated)
    print(f"Prepared {len(functions)} unchanged functions from {EXPECTED_SHA256}")


if __name__ == "__main__":
    main()
