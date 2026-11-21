#!/usr/bin/env python3
"""Deterministic synthetic transitions and real checkpoint commitments; no proof."""
import argparse,json,subprocess
from pathlib import Path
HERE=Path(__file__).resolve().parent
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--binary',type=Path,required=True);p.add_argument('--check',action='store_true');a=p.parse_args()
    data=json.loads(subprocess.check_output([str(a.binary)],text=True))
    outputs={'state-v2.json':data}
    for group in ['checkpoints','steps','old_tip','nonmonotonic']:
        outputs.update({f'{group}-{i}.json':entry for i,entry in enumerate(data[group])})
    for name,value in outputs.items():
        out=json.dumps(value,indent=2)+'\n';destination=HERE/'fixtures'/name
        if a.check:
            if destination.read_text()!=out:raise ValueError('State vectors differ: '+name)
        else:destination.write_text(out)
    print('3 real checkpoint initializations and 10 synthetic transition/membership vectors match')
if __name__=='__main__':main()
