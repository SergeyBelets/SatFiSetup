#!/usr/bin/env python3
"""Rebuild/verify pinned mainnet fixtures with Bitcoin Core, without a live Bitcoin peer or RPC.

Use --source-json-gz only to rebuild from the checksum-pinned DFINITY Core export.
Normal verification reads the committed compressed genesis prefix, starts an isolated local
Bitcoin Core mainnet process, submits every header in order, and checks every stored hash,
bits and chainwork value. No wallets, network peers, or public RPC are used.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import http.client
import json
import pathlib
import socket
import struct
import subprocess
import tempfile
import time

FIRST, CHECKPOINT, LAST = 32246, 32256, 36288
CORE_VERSION = "/Satoshi:29.0.0/"
CORE_COMMIT = "f490f5562d4b20857ef8d042c050763795fd43da"
SOURCE_URL = "https://download.dfinity.systems/testdata/mainnet_headers_800k.json.gz"
SOURCE_SHA256 = "fb58cd3c0e2efe298eeb96751ae2276e938b39e2f1d9760ea47a9f3a8288d214"
ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "web3/test/fixtures/mainnet"


def sha256d(raw: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def extract(path: pathlib.Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != SOURCE_SHA256:
        raise ValueError("source archive checksum does not match the pinned Core-export manifest")
    decoder = json.JSONDecoder()
    headers = bytearray()
    with gzip.open(path, "rt") as stream:
        text = ""
        offset = 0
        for height in range(LAST + 1):
            while True:
                while offset < len(text) and text[offset] in " \n\r\t,[":
                    offset += 1
                try:
                    item, end = decoder.raw_decode(text, offset)
                    offset = end
                    break
                except json.JSONDecodeError:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        raise ValueError(f"source ends before height {height}")
                    text = text[offset:] + chunk
                    offset = 0
            headers.extend(struct.pack("<i", item["version"]))
            headers.extend(bytes.fromhex(item["prev_blockhash"])[::-1])
            headers.extend(bytes.fromhex(item["merkle_root"])[::-1])
            headers.extend(struct.pack("<III", item["time"], item["bits"], item["nonce"]))
    return bytes(headers)


class Core:
    version = CORE_VERSION
    source_commit = CORE_COMMIT
    release = "29.0"
    nat_options = ("-upnp=0", "-natpmp=0")

    def command(self, binary: str, directory: pathlib.Path) -> list[str]:
        return [
            binary, f"-datadir={directory}", "-chain=main", "-server=1", "-listen=0",
            "-connect=0", "-dnsseed=0", "-discover=0", *self.nat_options,
            "-disablewallet=1", "-rpcbind=127.0.0.1", f"-rpcport={self.port}",
            "-printtoconsole=0", "-logips=0", "-dbcache=64",
        ]

    def __init__(self, binary: str, directory: pathlib.Path):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.directory = directory
        self.process = subprocess.Popen(
            self.command(binary, directory), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        for _ in range(200):
            if self.process.poll() is not None:
                raise RuntimeError(self.process.stderr.read().decode())
            cookie = directory / ".cookie"
            if cookie.exists():
                self.auth = base64.b64encode(cookie.read_bytes().strip()).decode()
                try:
                    info = self.batch([("getnetworkinfo", [])])[0]
                except (ConnectionError, OSError, RuntimeError):
                    pass
                else:
                    if info["subversion"] != self.version:
                        self.stop()
                        raise RuntimeError(f"expected Bitcoin Core {self.version}; got {info['subversion']}")
                    return
            time.sleep(0.05)
        self.stop()
        raise RuntimeError("isolated Bitcoin Core RPC did not become ready")

    def batch(self, operations: list[tuple[str, list]]) -> list:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        body = json.dumps([{"jsonrpc": "2.0", "id": i, "method": method, "params": params}
                           for i, (method, params) in enumerate(operations)])
        connection.request("POST", "/", body, {"Authorization": "Basic " + self.auth,
                                                "Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        if response.status != 200:
            raise RuntimeError(f"Core RPC HTTP {response.status}: {payload[:500]!r}")
        replies = sorted(json.loads(payload), key=lambda item: item["id"])
        for reply in replies:
            if reply.get("error"):
                raise RuntimeError(f"Core RPC rejected operation {reply['id']}: {reply['error']}")
        return [reply["result"] for reply in replies]

    def stop(self):
        if self.process.poll() is None:
            try:
                self.batch([("stop", [])])
                self.process.wait(timeout=15)
            except Exception:
                self.process.terminate()
                self.process.wait(timeout=15)


def verify(bitcoind: str, prefix: bytes, *, core_type: type[Core] = Core) -> tuple[bytes, dict]:
    if len(prefix) != (LAST + 1) * 80:
        raise ValueError("unexpected genesis-prefix length")
    records = bytearray()
    anchors = {}
    with tempfile.TemporaryDirectory(prefix="satoshifi-mainnet-core-") as path:
        core = core_type(bitcoind, pathlib.Path(path))
        try:
            genesis = core.batch([("getblockhash", [0])])[0]
            assert sha256d(prefix[:80])[::-1].hex() == genesis
            assert core.batch([("getblockheader", [genesis, False])])[0] == prefix[:80].hex()
            for start in range(1, LAST + 1, 500):
                stop = min(start + 500, LAST + 1)
                core.batch([("submitheader", [prefix[h * 80:(h + 1) * 80].hex()]) for h in range(start, stop)])
            info = core.batch([("getblockchaininfo", [])])[0]
            assert info["chain"] == "main" and info["headers"] == LAST
            for start in range(FIRST, LAST + 1, 250):
                stop = min(start + 250, LAST + 1)
                hashes = [sha256d(prefix[h * 80:(h + 1) * 80])[::-1].hex() for h in range(start, stop)]
                results = core.batch([("getblockheader", [hash_, True]) for hash_ in hashes])
                for height, expected, result in zip(range(start, stop), hashes, results):
                    header = prefix[height * 80:(height + 1) * 80]
                    assert result["height"] == height and result["hash"] == expected
                    assert int(result["bits"], 16) == int.from_bytes(header[72:76], "little")
                    # Display-order block hash, unsigned big-endian chainwork, big-endian nBits.
                    records.extend(bytes.fromhex(result["hash"]))
                    records.extend(bytes.fromhex(result["chainwork"]))
                    records.extend(bytes.fromhex(result["bits"]))
                    if height in (FIRST, CHECKPOINT, CHECKPOINT + 2015, CHECKPOINT + 2016, LAST - 1, LAST):
                        anchors[str(height)] = {key: result[key] for key in ("hash", "bits", "chainwork", "time", "mediantime")}
        finally:
            core.stop()
    manifest = {
        "network": "bitcoin-mainnet", "first_height": FIRST, "checkpoint_height": CHECKPOINT,
        "last_height": LAST, "checkpoint_included_in_chainwork": True,
        "header_count": LAST - FIRST + 1, "retarget_boundaries": [CHECKPOINT + 2016, LAST],
        "bitcoin_core_version": core_type.version, "bitcoin_core_commit": core_type.source_commit,
        "source_url": SOURCE_URL, "source_sha256": SOURCE_SHA256,
        "source_manifest": "https://github.com/dfinity/ic/blob/master/MODULE.bazel",
        "verification": f"All headers 1..36288 accepted by isolated Bitcoin Core{core_type.release} submitheader on mainnet; each fixture record checked by getblockheader(hash,true). No peers or live RPC.",
        "anchors": anchors,
        "headers_sha256": hashlib.sha256(prefix[FIRST * 80:]).hexdigest(),
        "expected_sha256": hashlib.sha256(records).hexdigest(),
        "genesis_prefix_sha256": hashlib.sha256(prefix).hexdigest(),
    }
    return bytes(records), manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bitcoind", required=True, help="path to the official Bitcoin Core29.0 bitcoind")
    parser.add_argument("--source-json-gz", type=pathlib.Path, help="rebuild from the pinned historical source archive")
    parser.add_argument("--write", action="store_true", help="write fixtures after successful Core verification")
    args = parser.parse_args()
    prefix = extract(args.source_json_gz) if args.source_json_gz else gzip.decompress(
        (FIXTURE / "headers-genesis-36288.bin.gz").read_bytes())
    records, manifest = verify(args.bitcoind, prefix)
    if args.write:
        FIXTURE.mkdir(parents=True, exist_ok=True)
        (FIXTURE / "headers-32246-36288.bin").write_bytes(prefix[FIRST * 80:])
        (FIXTURE / "expected-32246-36288.bin").write_bytes(records)
        (FIXTURE / "headers-genesis-36288.bin.gz").write_bytes(gzip.compress(prefix, mtime=0))
        (FIXTURE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    else:
        assert (FIXTURE / "headers-32246-36288.bin").read_bytes() == prefix[FIRST * 80:]
        assert (FIXTURE / "expected-32246-36288.bin").read_bytes() == records
        assert json.loads((FIXTURE / "manifest.json").read_text()) == manifest
    print(json.dumps({"status": "verified", "core": CORE_VERSION, "headers": LAST + 1,
                      "fixture_records": LAST - FIRST + 1, "anchors": manifest["anchors"]}, indent=2))


if __name__ == "__main__":
    main()
