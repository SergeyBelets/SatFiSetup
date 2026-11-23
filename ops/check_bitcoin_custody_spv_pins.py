"""Check the on-chain custody classifier against the two canonical Solidity builds."""

import argparse
import json
from pathlib import Path
import re

from eth_utils import keccak


ROOT = Path(__file__).resolve().parents[1]
RELAY = ("RegtestHeaderRelay", "MainnetSPVGuard", "MainnetHeaderRelay", "Testnet4HeaderRelay")
ZK = ("ZkSpvAdapter", "ZkTestnet4SpvAdapter", "ZkBitcoinStateV2", "ZkBitcoinTestnet4StateV2",
      "RiscZeroGroth16Verifier")


def fingerprint(folder, name):
    path = folder / (name + ".sol") / (name + ".json")
    compiled = json.loads(path.read_text())["deployedBytecode"]
    code = bytearray.fromhex(compiled["object"].removeprefix("0x"))
    if int.from_bytes(code[-2:], "big") != 51:
        raise ValueError(f"Unexpected Solidity metadata length for {name}")
    groups = []
    for immutables in compiled["immutableReferences"].values():
        group = []
        for slot in immutables:
            offset = slot["start"]
            if slot["length"] != 32 or not 0 <= offset <= len(code) - 53 - 32:
                raise ValueError(f"Unexpected immutable slot for {name}")
            code[offset:offset + 32] = bytes(32)
            group.append(offset)
        groups.append(sorted(group))
    layout = b"".join(bytes([len(group)]) + b"".join(slot.to_bytes(2, "big") for slot in group)
                      for group in sorted(groups))
    if name == "RegtestHeaderRelay":
        # Its sole constructor fixes every substituted immutable at zero.
        layout = b""
    return str(len(code)), keccak(bytes(code[:-53])).hex(), layout.hex()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web3-out", type=Path, default=ROOT / "web3/contracts/out")
    parser.add_argument("--zk-out", type=Path, default=ROOT / "rust/zkspv/state-adapter/out")
    args = parser.parse_args()
    path = ROOT / "web3/contracts/src/federation/BitcoinCustodyNetwork.sol"
    enrolled = re.findall(r'_matches\(code, (\d+), 0x([0-9a-f]{64}), hex"([0-9a-f]*)"\)', path.read_text())
    expected = [(name, fingerprint(folder, name)) for names, folder in ((RELAY, args.web3_out), (ZK, args.zk_out))
                for name in names]
    if len(enrolled) != len(expected) or set(enrolled) != {pin for _, pin in expected}:
        for name, pin in expected:
            if pin not in enrolled:
                print(name, *pin)
        raise SystemExit("Bitcoin custody SPV executable pins changed")
    print(f"Checked {len(enrolled)} Bitcoin custody SPV executable pins")


if __name__ == "__main__":
    main()
