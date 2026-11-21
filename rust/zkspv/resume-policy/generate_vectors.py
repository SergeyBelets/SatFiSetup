#!/usr/bin/env python3
import argparse,json,subprocess
from pathlib import Path
HERE=Path(__file__).resolve().parent
def main():
    p=argparse.ArgumentParser();p.add_argument('--binary',type=Path,required=True);p.add_argument('--check',action='store_true');a=p.parse_args()
    data=json.loads(subprocess.check_output([str(a.binary)],text=True));data['authentication']='NONE: test index/codec vectors, not ZK proofs'
    output=json.dumps(data,indent=2)+'\n';path=HERE/'fixtures/index-v2.json'
    if a.check:
        if path.read_text()!=output:raise ValueError('V2 index vectors differ')
    else:path.parent.mkdir(exist_ok=True);path.write_text(output)
    print('V2 index/codec vectors match')
if __name__=='__main__':main()
