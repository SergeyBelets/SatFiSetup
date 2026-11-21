"""Reproducible, synthetic FUNDING_STRIPPED_100K_V1 format fixtures.

Run ``python tests/zkspv/funding_vectors.py --write --rust`` to regenerate and
cross-check with the pinned local rust-bitcoin inspector; ``--check --rust``
checks existing artifacts. The Rust binary is built separately, never fetched
or built by this module. ``SATOHSI_CRYPTO_BIN`` selects an existing binary.

The records are transaction serialization examples, not evidence of inclusion,
UTXO existence, script execution, monetary validity, or production readiness.
In particular, large scripts exercise a structural parser's byte envelope.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys

from eth_abi import encode
from eth_hash.auto import keccak


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "web3/test/fixtures/funding-stripped-v1.json"
PROFILE = "FUNDING_STRIPPED_100K_V1"
PARSER = "rust-bitcoin-0.32.7"
TARGET_SCRIPT = bytes.fromhex("5120" + "83" * 32)


def compact_size(value: int) -> bytes:
    """Encode the shortest CompactSize representation of an unsigned integer."""
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("CompactSize is an unsigned 64-bit integer")
    if value < 0xFD:
        return bytes([value])
    if value <= 0xFFFF:
        return b"\xfd" + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return b"\xfe" + value.to_bytes(4, "little")
    return b"\xff" + value.to_bytes(8, "little")


@dataclass(frozen=True)
class Input:
    txid_le: bytes
    vout: int
    script_sig: bytes = b""
    sequence: int = 0xFFFFFFFD

    def serialize(self) -> bytes:
        return (self.txid_le + self.vout.to_bytes(4, "little")
                + compact_size(len(self.script_sig)) + self.script_sig
                + self.sequence.to_bytes(4, "little"))


@dataclass(frozen=True)
class Output:
    value: int
    script: bytes

    def serialize(self) -> bytes:
        return self.value.to_bytes(8, "little") + compact_size(len(self.script)) + self.script


@dataclass(frozen=True)
class Vector:
    name: str
    inputs: tuple[Input, ...]
    outputs: tuple[Output, ...]
    target_vout: int
    version: int = 2
    locktime: int = 0

    def serialize(self) -> bytes:
        return (self.version.to_bytes(4, "little")
                + compact_size(len(self.inputs))
                + b"".join(item.serialize() for item in self.inputs)
                + compact_size(len(self.outputs))
                + b"".join(item.serialize() for item in self.outputs)
                + self.locktime.to_bytes(4, "little"))

    def manifest_entry(self) -> dict:
        raw = self.serialize()
        raw_hash = hashlib.sha256(hashlib.sha256(raw).digest()).digest()
        target = self.outputs[self.target_vout]
        outpoints = [(item.txid_le, item.vout) for item in self.inputs]
        return {
            "name": self.name,
            "raw_file": f"test/fixtures/funding-stripped-v1/{self.name}.hex",
            "target_vout": self.target_vout,
            "expected": {
                "txid": raw_hash[::-1].hex(),
                "raw_sha256d": raw_hash.hex(),
                "stripped_bytes": len(raw),
                "input_count": len(self.inputs),
                "output_count": len(self.outputs),
                "target_value": target.value,
                "target_script_hex": target.script.hex(),
                "inputs_abi_keccak256": keccak(encode(["(bytes32,uint32)[]"], [outpoints])).hex(),
                "inputs": [{"txid_le": item.txid_le.hex(), "vout": item.vout}
                           for item in self.inputs],
            },
        }


def known_input(index: int, script: bytes = b"", sequence: int = 0xFFFFFFFD) -> Input:
    # Distinct, non-palindromic hashes and vouts expose accidental byte reversal
    # and truncation while avoiding repeated outpoints in the count fixtures.
    txid_le = hashlib.sha256(b"funding-stripped-v1/input/" + index.to_bytes(4, "little")).digest()
    return Input(txid_le, 0x01020304 + index * 17, script, sequence)


def vectors() -> tuple[Vector, ...]:
    mixed = Vector(
        "mixed_inputs_outputs",
        (
            known_input(0, sequence=0xFFFFFFFF),
            known_input(1, b"\x51", 0x01020304),
            known_input(2, b"\x02\xab\xcd", 0),
            known_input(3, b"\x51" * 253),
        ),
        (
            Output(0, b""),
            Output(1, b"\x51"),
            Output(42_000, bytes.fromhex("0014" + "46" * 20)),
            Output(5_000_000_001, TARGET_SCRIPT),
            Output(123_456_789, b"\x51" * 252),
            Output(987_654_321, b"\x51" * 253),
        ),
        target_vout=3,
        version=1,
        locktime=0x10203040,
    )
    cases = [mixed]
    for count in (33, 256, 257):
        outputs = tuple(Output(1000 + i, bytes([0x51 + i % 4])) for i in range(count - 1))
        cases.append(Vector(f"outputs_{count}", (known_input(4, b"\x51"),),
                            outputs + (Output(5_000_000_001 + count, TARGET_SCRIPT),), count - 1))
    cases.append(Vector("inputs_257", tuple(known_input(i, b"\x51" * (i % 4)) for i in range(257)),
                        (Output(21_000, b"\x51"), Output(5_000_000_258, TARGET_SCRIPT)), 1))
    # With 41-byte inputs / 9-byte outputs these are the maximum nonzero
    # counts that can fit, even before imposing Bitcoin script validity.
    cases.append(Vector("max_inputs_2438", tuple(known_input(i) for i in range(2438)),
                        (Output(1000, b""),), 0))
    cases.append(Vector("max_outputs_11105", (known_input(5),),
                        tuple(Output(i + 1, b"") for i in range(11105)), 11104))
    cases.append(Vector("script_65536", (known_input(6, b"\x51"),),
                        (Output(1234, b"\x51" * 65536), Output(5_000_000_002, TARGET_SCRIPT)), 1))
    # 108 bytes of surrounding framing + 99,892 bytes of script = 100,000.
    cases.append(Vector("stripped_100000", (known_input(7, b"\x51"),),
                        (Output(1234, b"\x51" * 99892), Output(5_000_000_003, TARGET_SCRIPT)), 1))
    assert len(cases[-1].serialize()) == 100_000
    assert all(0 < len(case.serialize()) <= 100_000 for case in cases)
    return tuple(cases)


def manifest() -> dict:
    return {
        "schema_version": 1,
        "profile": PROFILE,
        "reference_parser": PARSER,
        "scope": "Synthetic structural serialization only; no inclusion, UTXO, script or production qualification.",
        "vectors": [case.manifest_entry() for case in vectors()],
    }


def expected_artifacts() -> dict[Path, str]:
    result = {MANIFEST: json.dumps(manifest(), indent=2) + "\n"}
    for case in vectors():
        entry = case.manifest_entry()
        raw_file = ROOT / "web3" / entry["raw_file"]
        result[raw_file] = "0x" + case.serialize().hex()
        # Foundry's JSON cheatcodes ABI-encode the JSON string on every call.
        # Keep the per-case test input small; the ABI digest still binds every
        # outpoint, and the complete inspectable array stays in the manifest.
        del entry["expected"]["inputs"]
        result[raw_file.with_suffix(".json")] = json.dumps(entry, indent=2) + "\n"
    return result


def rust_binary() -> Path:
    return Path(os.environ.get("SATOHSI_CRYPTO_BIN", ROOT / "frost-crypto/target/debug/satohsi-frost-crypto"))


def check_rust(case: Vector) -> None:
    """Compare independent constructor records with the existing public wrapper."""
    sys.path.insert(0, str(ROOT / "signer-agent"))
    from signer_agent.funding_envelope import inspect_funding

    raw = case.serialize()
    expected = case.manifest_entry()["expected"]
    actual = inspect_funding(raw.hex(), expected["txid"])
    assert actual["profile"] == PROFILE
    assert actual["parser"] == PARSER
    assert actual["raw"] == actual["stripped"] == raw.hex()
    assert actual["txid"] == actual["wtxid"] == expected["txid"]
    assert actual["coinbase"] is False
    assert int(actual["raw_bytes"]) == int(actual["stripped_bytes"]) == len(raw)
    assert int(actual["weight"]) == len(raw) * 4
    assert actual["inputs"] == [
        {"outpoint": f"{item.txid_le[::-1].hex()}:{item.vout}",
         "script_sig": item.script_sig.hex(), "sequence": str(item.sequence)}
        for item in case.inputs
    ]
    assert actual["outputs"] == [
        {"vout": str(index), "amount": str(item.value), "script": item.script.hex()}
        for index, item in enumerate(case.outputs)
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="regenerate checked-in artifacts")
    mode.add_argument("--check", action="store_true", help="verify artifacts (default)")
    parser.add_argument("--rust", action="store_true", help="also require the pinned local Rust inspector")
    args = parser.parse_args(argv)
    if args.rust:
        if not rust_binary().is_file():
            parser.error(f"existing Rust binary not found: {rust_binary()}")
        for case in vectors():
            check_rust(case)
    for path, expected in expected_artifacts().items():
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected)
        elif not path.is_file() or path.read_text() != expected:
            parser.error(f"fixture differs: {path}; regenerate with --write")
    action = "wrote" if args.write else "checked"
    print(f"{action} {len(vectors())} structural funding fixtures" + (f" against {PARSER}" if args.rust else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
