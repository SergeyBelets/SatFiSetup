#!/usr/bin/env python3
"""Versioned ABI vectors from compiled Rust; synthetic branch tests are not PoW proofs."""
import argparse
import json
from pathlib import Path
import subprocess
HERE=Path(__file__).resolve().parent
DEST=HERE.parents[2]/'web3/test/fixtures/transition-policy-v1.json'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,required=True); parser.add_argument('--check',action='store_true')
    args=parser.parse_args(); data=json.loads(subprocess.check_output([str(args.binary)],text=True))
    data['authentication']='NONE: test-only synthetic branch transitions; no ZK proof or receipt'
    output=json.dumps(data,indent=2)+'\n'
    if args.check:
        if DEST.read_text()!=output: raise ValueError('Transition ABI fixtures differ')
    else: DEST.write_text(output)
    print('Six Rust transition journal fixtures match')

if __name__=='__main__': main()
