#!/usr/bin/env python3
"""Shared scalar ABI/time fixtures, encoded by the real Rust policy component."""
import argparse
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TARGET = ROOT / "web3/test/fixtures/header-time-v1.json"


def cases():
    return [
        ("past", [100, 150, 125], 200),
        ("now", [100, 200], 200),
        ("future_boundary", [7200], 0),
        ("future_plus_one", [7201], 0),
        ("earlier_maximum_rejected", [100, 7501, 300], 300),
        ("earlier_maximum_boundary", [100, 7500, 300], 300),
        ("later_acceptance", [100, 7501, 300], 301),
        ("zero_timestamp", [0], 0),
        ("max_bitcoin_timestamp", [2**32 - 1, 5], 2**32 - 1),
        ("max_bitcoin_boundary", [2**32 - 1], 2**32 - 1 - 7200),
        ("max_bitcoin_too_early", [2**32 - 1], 2**32 - 2 - 7200),
        ("clock_above_uint32", [2**32 - 1, 0], 2**32),
        ("clock_uint64_max", [2**32 - 1], 2**64 - 1),
        ("clock_uint256_max", [2**32 - 1], 2**256 - 1),
        ("multiple_backsteps", [1000, 800, 1200, 900, 1100, 700], 0),
    ]


def document(encoder):
    rows = cases()
    output = subprocess.check_output([str(encoder)], text=True,
        input="".join(",".join(map(str, times)) + "\n" for _, times, _ in rows))
    encoded = output.splitlines()
    if len(encoded) != len(rows):
        raise ValueError("Rust encoder returned wrong row count")
    vectors = []
    for (name, times, now), raw in zip(rows, encoded):
        expected = b"SATOSHIFI_HEADER_TIME_V1".ljust(32, b"\0") + b"".join(
            number.to_bytes(32, "big") for number in (len(times), max(times), times[-1]))
        if bytes.fromhex(raw) != expected:
            raise ValueError("Rust ABI differs from independent fixed-width encoding")
        vectors.append(dict(name=name, timestamps=times, evm_timestamp=str(now),
            header_count=len(times), maximum_timestamp=max(times), last_timestamp=times[-1],
            accepted=max(times) <= now + 7200, abi="0x" + raw))
    return dict(schema="satoshifi.header-time-fixtures.v1", authentication="NONE: scalar component fixtures",
                vectors=vectors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = json.dumps(document(args.encoder), indent=2) + "\n"
    if args.check:
        if TARGET.read_text() != text:
            raise ValueError("Shared time fixtures are stale")
    else:
        TARGET.write_text(text)
    print("Verified 15 shared Rust/Solidity time fixtures")


if __name__ == "__main__":
    main()
