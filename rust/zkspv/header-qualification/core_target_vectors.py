#!/usr/bin/env python3
"""Run pinned Core SetCompact/DeriveTarget arithmetic as the test-vector oracle.

Only arithmetic and a formatting/input driver are compiled, not a Core node.
No Bitcoin headers or network requests are constructed or submitted here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import prepare

HERE, ROOT = prepare.HERE, prepare.ROOT


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs():
    # Every exponent/sign and mantissa boundary, plus deterministic samples.
    words = (0, 1, 0x7f, 0x80, 0xff, 0x100, 0x7fff, 0x8000, 0xffff,
             0x10000, 0x7ffff, 0x80000, 0x7ffffe, 0x7fffff)
    values = {(size << 24) | sign | word for size in range(256)
              for sign in (0, 0x800000) for word in words}
    for counter in range(4096):
        value = hashlib.sha256(b"satoshifi-compact-v1:" + counter.to_bytes(4, "big")).digest()
        values.add(int.from_bytes(value[:4], "big"))
    values.update((0x1d00ffff, 0x1e0377ae, 0x207fffff, 0x1702c070))
    return sorted(values)


def driver():
    # Copy the exact existing Core predicate, without translating its checks.
    source = ROOT / "rust/zkspv/vendor/bitcoin-core-ee42d59d/src/pow.cpp"
    declaration = prepare.braced(source.read_text(), "std::optional<arith_uint256> DeriveTarget(")
    return """// Generated test driver; pinned Core owns all target arithmetic.
#include <arith_uint256.h>
#include <uint256.h>
#include <array>
#include <iomanip>
#include <iostream>
#include <optional>
#include <string>
""" + declaration + """
std::string format_target(arith_uint256 value) {
    constexpr char digits[] = "0123456789abcdef";
    std::string text(64, '0');
    for (int i = 31; i >= 0; --i) {
        unsigned byte = value.GetLow64() & 0xff;
        text[2*i] = digits[byte >> 4];
        text[2*i+1] = digits[byte & 15];
        value >>= 8;
    }
    return text;
}
int main() {
    std::array<arith_uint256, 4> limits;
    limits[0].SetCompact(0x1d00ffff);
    limits[1].SetCompact(0x1e0377ae);
    limits[2].SetCompact(0x207fffff);
    limits[3] = ~arith_uint256(0);
    uint32_t bits;
    while (std::cin >> std::hex >> bits) {
        std::cout << std::hex << std::setw(8) << std::setfill('0') << bits;
        for (const auto& limit : limits) {
            auto target = DeriveTarget(bits, ArithToUint256(limit));
            std::cout << '\\t' << (target ? format_target(*target) : "-");
        }
        std::cout << '\\n';
    }
    return std::cin.eof() ? 0 : 1;
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", default="/usr/bin/clang++")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((HERE / "core-reference-manifest.json").read_text())
    reference = HERE / "references/bitcoin-core"
    for name, expected in manifest["files_sha256"].items():
        if sha(reference / name) != expected:
            raise ValueError("CORE_REFERENCE_HASH_MISMATCH")
    patches, _, _ = prepare.checked_sources()  # Includes pinned Core pow.cpp and original arith source.
    if (reference / "src/arith_uint256.cpp").read_bytes() != (
            ROOT / "rust/zkspv/vendor/bitcoin-core-ee42d59d/src/arith_uint256.cpp").read_bytes():
        raise ValueError("CORE_ARITHMETIC_REFERENCE_MISMATCH")
    source = driver()
    values = inputs()
    paths = {Path(__file__), HERE / "prepare.py", HERE / "core-reference-manifest.json", HERE / "series.json",
             HERE / patches["patch"]["file"]}
    paths |= {ROOT / name for name in patches["source_sha256"]}
    paths |= {HERE / item["file"] for item in patches.get("additional_patches", [])}
    paths |= {reference / name for name in manifest["files_sha256"]}
    sources = {str(path.relative_to(ROOT)): sha(path) for path in sorted(paths)}
    with tempfile.TemporaryDirectory(prefix="satoshifi-core-target-") as temporary:
        temporary = Path(temporary)
        cpp, executable = temporary / "driver.cpp", temporary / "target-oracle"
        cpp.write_text(source)
        command = [args.compiler, "-std=c++20", "-O2", "-ffunction-sections", "-fdata-sections",
            "-Wl,-dead_strip", "-I", str(reference / "src"), str(cpp),
            str(reference / "src/arith_uint256.cpp"), "-o", str(executable)]
        compiled = subprocess.run(command, capture_output=True, text=True)
        if compiled.returncode:
            raise RuntimeError(compiled.stderr)
        process = subprocess.run([str(executable)], input="".join(f"{value:08x}\n" for value in values),
                                 capture_output=True, text=True, check=True)
        executable_sha = sha(executable)
    rows = process.stdout.splitlines()
    if len(rows) != len(values) or any(row.split("\t")[0] != f"{value:08x}" or len(row.split("\t")) != 5
                                       for row, value in zip(rows, values)):
        raise ValueError("CORE_VECTOR_OUTPUT")
    target = HERE / "fixtures/compact-core-vectors.tsv"
    if args.check:
        if target.read_text() != process.stdout:
            raise ValueError("CORE_VECTORS_CHANGED")
    else:
        target.write_text(process.stdout)
    evidence = HERE / "evidence"
    (evidence / "core-target-driver.cpp").write_text(source)
    (evidence / "core-target-build.log").write_text(compiled.stdout + compiled.stderr)
    if any(sha(ROOT / name) != value for name, value in sources.items()):
        raise ValueError("CORE_REFERENCE_SOURCES_CHANGED_DURING_RUN")
    report = dict(schema="satoshifi.core-target-reference.v1", status="PASS", production_ready=False,
        core_commit=manifest["commit"], compact_inputs=len(values), pow_limits=4,
        comparisons=len(values) * 4, vector_file=str(target.relative_to(HERE)), vector_sha256=sha(target),
        compiler=subprocess.check_output([args.compiler, "--version"], text=True),
        driver_sha256=hashlib.sha256(source.encode()).hexdigest(), executable_sha256=executable_sha,
        command=command, source_sha256=sources, sources_unchanged_during_run=True,
        scope="Actual pinned Core arithmetic and verbatim DeriveTarget; formatting driver only; no complete Core node",
        limits=["Mainnet/Testnet4", "Citrea custom Signet", "Regtest", "uint256 maximum for encoding boundaries"],
        qualification="Reference oracle only; patched Rust assertions run separately")
    (HERE / "core-target-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(status="PASS", inputs=len(values), comparisons=len(values) * 4)))


if __name__ == "__main__":
    main()
