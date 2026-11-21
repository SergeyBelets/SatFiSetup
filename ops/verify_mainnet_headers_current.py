#!/usr/bin/env python3
"""Revalidate the pinned Core29 mainnet fixtures with exactly Bitcoin Core30.3.

This is an additional, read-only fixture check. It neither replaces the Core29
manifest nor claims that Core30 produced that original manifest. The verifier
uses a fresh local mainnet data directory with no peers, wallets, or live RPC.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
from pathlib import Path

import verify_mainnet_headers as pinned


class Core303(pinned.Core):
    version = "/Satoshi:30.3.0/"
    # The binary's version is checked at runtime. A source tag is a reference,
    # not an assertion that a locally supplied binary has an attested commit.
    source_commit = None
    release = "30.3"
    # Core30 no longer accepts Core29's -upnp / -natpmp options. The inherited
    # listen/connect/dnsseed/discover settings continue to disable peers.
    nat_options = ()


PROVENANCE_FIELDS = {"bitcoin_core_version", "bitcoin_core_commit", "verification"}


def check_fixture_inputs(fixture: Path) -> tuple[bytes, bytes, dict]:
    manifest = json.loads((fixture / "manifest.json").read_text())
    if (
        manifest.get("bitcoin_core_version") != pinned.CORE_VERSION
        or manifest.get("bitcoin_core_commit") != pinned.CORE_COMMIT
    ):
        raise ValueError("unexpected original fixture Core version or commit")
    prefix = gzip.decompress((fixture / "headers-genesis-36288.bin.gz").read_bytes())
    headers = (fixture / "headers-32246-36288.bin").read_bytes()
    records = (fixture / "expected-32246-36288.bin").read_bytes()
    for data, field in (
        (prefix, "genesis_prefix_sha256"),
        (headers, "headers_sha256"),
        (records, "expected_sha256"),
    ):
        if hashlib.sha256(data).hexdigest() != manifest.get(field):
            raise ValueError(f"fixture checksum mismatch: {field}")
    if len(prefix) != (pinned.LAST + 1) * 80 or headers != prefix[pinned.FIRST * 80 :]:
        raise ValueError("fixture range does not match the genesis prefix")
    if len(records) != (pinned.LAST - pinned.FIRST + 1) * 68:
        raise ValueError("unexpected fixture record count")
    return prefix, records, manifest


def compare_revalidation(records: bytes, original_records: bytes, actual: dict, original: dict) -> None:
    if records != original_records:
        raise ValueError("Core30.3 hash, bits, or chainwork differs from the pinned records")
    if actual.get("bitcoin_core_version") != Core303.version or actual.get("bitcoin_core_commit") is not None:
        raise ValueError("unexpected Core revalidation provenance")
    # Only the verifier's provenance may differ. This includes exact comparison
    # of all MTP/time anchors, input hashes, heights, counts and retargets.
    expected = {k: v for k, v in original.items() if k not in PROVENANCE_FIELDS}
    observed = {k: v for k, v in actual.items() if k not in PROVENANCE_FIELDS}
    if observed != expected:
        raise ValueError("Core30.3 fixture metadata or anchors differ from the pinned manifest")


def verify_current(bitcoind: str, *, fixture: Path = pinned.FIXTURE) -> dict:
    prefix, original_records, original = check_fixture_inputs(fixture)
    records, actual = pinned.verify(bitcoind, prefix, core_type=Core303)
    compare_revalidation(records, original_records, actual, original)
    return {
        "status": "verified",
        "checked_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "headers_checked": len(prefix) // 80,
        "headers_submitted": pinned.LAST,
        "fixture_records_checked": len(records) // 68,
        "original_fixture_provenance": {
            "bitcoin_core_version": original["bitcoin_core_version"],
            "bitcoin_core_commit": original["bitcoin_core_commit"],
            "manifest_sha256": hashlib.sha256((fixture / "manifest.json").read_bytes()).hexdigest(),
        },
        "independent_revalidation": actual,
        "binary_sha256": hashlib.sha256(Path(bitcoind).read_bytes()).hexdigest(),
        "version_difference": (
            "The committed fixtures were generated with Core29.0; this is an additional "
            "Core30.3.0 replay. Core30 removed -upnp/-natpmp, so those two flags are omitted. "
            "The original Core29 manifest and all fixture bytes remain unchanged. "
            "RPC verifies the exact binary version, not the binary's source commit or signature."
        ),
        "isolation": {
            "temporary_datadir": True,
            "network": "main",
            "rpc": "127.0.0.1 only, fresh temporary cookie",
            "peer_networking": False,
            "wallet": False,
            "public_rpc": False,
            "fixture_writes": False,
        },
        "references": {
            "current_release": "https://bitcoincore.org/en/releases/30.3/",
            "current_source_tag": "https://github.com/bitcoin/bitcoin/tree/v30.3",
            "pinned_pow": f"https://github.com/bitcoin/bitcoin/blob/{pinned.CORE_COMMIT}/src/pow.cpp",
            "pinned_compact": f"https://github.com/bitcoin/bitcoin/blob/{pinned.CORE_COMMIT}/src/arith_uint256.cpp",
            "pinned_work": f"https://github.com/bitcoin/bitcoin/blob/{pinned.CORE_COMMIT}/src/chain.cpp",
            "pinned_mtp": f"https://github.com/bitcoin/bitcoin/blob/{pinned.CORE_COMMIT}/src/chain.h",
        },
        "reproduce_from_repository_root": (
            "python ops/verify_mainnet_headers_current.py "
            "--bitcoind /absolute/path/to/bitcoin-30.3/bin/bitcoind "
            "--output ops/artifacts/upgrade-mainnet-fixtures-20261018.json"
        ),
        "scope": (
            "Historical header consensus and fixture consistency only; no current mainnet "
            "synchronization, transaction/block validation, live withdrawal, or agent mainnet readiness."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bitcoind", required=True, help="path to the official Bitcoin Core30.3 bitcoind")
    parser.add_argument("--output", required=True, type=Path, help="separate JSON report; never a fixture path")
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(pinned.FIXTURE.resolve()):
        parser.error("the report cannot overwrite files in the canonical fixture directory")
    report = verify_current(args.bitcoind)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "core": Core303.version,
                      "headers": report["headers_checked"], "records": report["fixture_records_checked"],
                      "report": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
