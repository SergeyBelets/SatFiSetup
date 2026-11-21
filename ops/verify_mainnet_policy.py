#!/usr/bin/env python3
"""Replay real mainnet headers in isolated Core30.3 and reject it for live signing.

No network peers, wallets, Bitcoin transactions or policy bypasses are enabled.
The unchanged historical fixtures establish consensus coverage, not readiness to
operate with current mainnet funds. The same live check used by the CLI must reject
this header-only, unsynchronized node.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys

import verify_mainnet_headers_current as current

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "signer-agent"))
from signer_agent.bitcoin_network import MAINNET_GENESIS, check_core, validate_policy
from signer_agent.bitcoin_rpc import BitcoinRPC


def verify(bitcoind):
    prefix, expected, original = current.check_fixture_inputs(current.pinned.FIXTURE)
    height = current.pinned.CHECKPOINT
    policy = validate_policy(dict(
        version="1", network="mainnet", genesis_hash=MAINNET_GENESIS,
        checkpoint_height=str(height), checkpoint_header=prefix[height * 80:(height + 1) * 80].hex(),
        checkpoint_chainwork=original["anchors"][str(height)]["chainwork"],
        checkpoint_predecessors=[prefix[h * 80:(h + 1) * 80].hex() for h in range(height - 10, height)],
        max_reorg_depth="6", max_tip_age_seconds="86400", max_relay_lag_blocks="0", max_headers_per_sync="64",
    ))
    observations = []

    class ObservedCore(current.Core303):
        def stop(self):
            try:
                if self.process.poll() is None:
                    info = self.batch([("getblockchaininfo", [])])[0]
                    if info["headers"] == current.pinned.LAST:
                        bitcoin = BitcoinRPC(f"http://127.0.0.1:{self.port}", self.directory / ".cookie",
                                             network="mainnet", policy=policy)
                        try:
                            check_core(bitcoin, "mainnet", policy)
                        except ValueError as error:
                            if str(error) != "MAINNET_CORE_NOT_SYNCED":
                                raise AssertionError("unexpected live policy refusal") from error
                            observations.append(dict(
                                status="REJECTED_AS_REQUIRED", reason=str(error),
                                chain=info["chain"], blocks=info["blocks"], headers=info["headers"],
                                initialblockdownload=info["initialblockdownload"], pruned=info["pruned"],
                                clock_override=False, policy_bypass=False,
                            ))
                        else:
                            raise AssertionError("historical header-only Core passed live mainnet readiness")
            finally:
                super().stop()

    records, actual = current.pinned.verify(bitcoind, prefix, core_type=ObservedCore)
    current.compare_revalidation(records, expected, actual, original)
    if len(observations) != 1:
        raise AssertionError("live Core policy observation was not recorded exactly once")
    return dict(
        status="passed", checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        bitcoin_core_version=current.Core303.version,
        binary_sha256=hashlib.sha256(Path(bitcoind).read_bytes()).hexdigest(),
        headers_checked=len(prefix) // 80, headers_submitted=current.pinned.LAST,
        fixture_records_checked=len(records) // 68, retarget_boundaries=original["retarget_boundaries"],
        fixture_manifest_sha256=hashlib.sha256((current.pinned.FIXTURE / "manifest.json").read_bytes()).hexdigest(),
        historical_consensus="All historical headers, hashes, bits, MTP anchors and chainwork match the pinned Core29 fixtures in Core30.3.",
        live_policy=observations[0],
        isolation=dict(temporary_datadir=True, network="main", peers=False, wallet=False,
                       public_rpc=False, bitcoin_transactions=False, fixture_writes=False),
        scope="Historical consensus replay and real-node live-policy rejection only. No live mainnet spend or current synchronized node is claimed.",
        reproduce="python ops/verify_mainnet_policy.py --bitcoind /absolute/path/to/bitcoin-30.3/bin/bitcoind --output ops/artifacts/mainnet-policy-core-20261018.json",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bitcoind", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(current.pinned.FIXTURE.resolve()):
        parser.error("the report cannot overwrite pinned fixtures")
    result = verify(args.bitcoind)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
