#!/usr/bin/env python3
"""Freeze executed Rust policy results for Solidity differential tests."""
import argparse
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TARGET = ROOT / "web3/test/fixtures/fork-policy-v1.json"

def document(binary):
    text = subprocess.check_output([str(binary)], text=True)
    proofs, traces = [], {}
    for row in text.splitlines():
        fields = row.split("\t")
        if fields[0] == "PROOF":
            _, name, anchor, anchor_height, head, head_height, root, halted, height, raw, siblings, accepted = fields
            proofs.append(dict(name=name, anchor="0x"+anchor, anchor_height=int(anchor_height), head="0x"+head,
                head_height=int(head_height), canonical_root="0x"+root, halted=halted=="1", height=int(height),
                raw="0x"+raw, siblings=["0x"+value for value in siblings.split(",")], accepted=accepted=="1"))
        elif fields[0] == "TRACE":
            traces.setdefault(fields[1],[]).append(list(map(int,fields[2:])))
        else:
            raise ValueError("Unexpected Rust output")
    assert len(proofs)==9 and {key:len(value) for key,value in traces.items()}=={"depth100":201,"depth101":203,"shorter":101}
    return dict(schema="satoshifi.fork-policy-fixtures.v1", authentication="NONE: synthetic graph policy, not PoW or proof",
        proofs=proofs, traces=traces, trace_columns=["parent_index","work_delta","head_index","head_height","halted","lca_index","rollback_depth"])

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary",type=Path,required=True); parser.add_argument("--check",action="store_true")
    args=parser.parse_args(); data=json.dumps(document(args.binary),indent=2)+"\n"
    if args.check:
        if TARGET.read_text()!=data: raise ValueError("Fork policy fixtures are stale")
    else: TARGET.write_text(data)
    print("Verified 9 membership cases and 505 differential graph transitions")

if __name__ == "__main__": main()
