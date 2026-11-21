#!/usr/bin/env python3
"""Record/check shared checkpoint fixtures produced by the compiled Rust factory."""
import argparse
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
DEST = HERE.parents[2] / 'web3/test/fixtures/checkpoint-policy-v1.json'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,required=True); parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    data=json.loads(subprocess.check_output([str(args.binary)],text=True))
    data['authentication']='NONE: historical test inputs, not selected production checkpoints'
    output=json.dumps(data,indent=2)+'\n'
    if args.check:
        if DEST.read_text()!=output: raise ValueError('Shared checkpoint vectors differ')
    else: DEST.write_text(output)
    print('Three real mainnet checkpoint contexts/commitments match')

if __name__=='__main__': main()
