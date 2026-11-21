#!/usr/bin/env python3
"""Replay the pinned public testnet4 genesis prefix through isolated Bitcoin Core30.3.

No peers, wallet or broadcast. Historical headers do not meet live signing policy.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import tempfile
from verify_mainnet_headers_current import Core303

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'web3/test/fixtures/testnet4'

class Testnet4Core(Core303):
    def command(self, binary, directory):
        return [arg.replace('-chain=main', '-chain=testnet4') for arg in super().command(binary, directory)] + [f'-rpccookiefile={directory / ".cookie"}']

def verify(binary):
    manifest=json.loads((FIXTURE/'manifest.json').read_text())
    prefix=(FIXTURE/'headers-0-4050.bin').read_bytes()
    headers=(FIXTURE/'headers-2006-4050.bin').read_bytes()
    expected=(FIXTURE/'expected-2006-4050.bin').read_bytes()
    for raw,key in ((prefix,'header_prefix_sha256'),(headers,'headers_sha256'),(expected,'expected_sha256')):
        if hashlib.sha256(raw).hexdigest()!=manifest[key]: raise ValueError('fixture checksum mismatch')
    if headers!=prefix[2006*80:] or len(prefix)!=4051*80: raise ValueError('fixture range mismatch')
    def digest(raw): return hashlib.sha256(hashlib.sha256(raw).digest()).digest()
    actual=bytearray(); anchors={}
    with tempfile.TemporaryDirectory(prefix='satoshifi-testnet4-core-') as path:
        core=Testnet4Core(binary,Path(path))
        try:
            genesis=core.batch([('getblockhash',[0])])[0]
            assert genesis==manifest['genesis']==digest(prefix[:80])[::-1].hex()
            for start in range(1,4051,250):
                core.batch([('submitheader',[prefix[h*80:(h+1)*80].hex()]) for h in range(start,min(start+250,4051))])
            info=core.batch([('getblockchaininfo',[])])[0]
            assert info['chain']=='testnet4' and info['headers']==4050 and info['initialblockdownload'] is True
            for start in range(2006,4051,250):
                hashes=[digest(prefix[h*80:(h+1)*80])[::-1].hex() for h in range(start,min(start+250,4051))]
                for item in core.batch([('getblockheader',[value,True]) for value in hashes]):
                    actual.extend(bytes.fromhex(item['hash'])[::-1]+bytes.fromhex(item['chainwork'])+bytes.fromhex(item['bits']))
                    if item['height'] in (2016,4031,4032,4050): anchors[str(item['height'])]=item
            assert bytes(actual)==expected
        finally: core.stop()
    return dict(status='passed',network='testnet4',bitcoin_core_version=Core303.version,
        checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),headers_checked=4051,
        fixture_records_checked=2045,retarget_boundaries=[2016,4032],anchors=anchors,
        binary_sha256=hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256((FIXTURE/'manifest.json').read_bytes()).hexdigest(),
        isolation=dict(peers=False,wallet=False,public_rpc=False,transactions=False),
        scope='Historical real public testnet4 consensus headers only; no current synchronized node or live spend claim.')

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bitcoind',required=True);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if args.output.resolve().is_relative_to(FIXTURE): parser.error('report cannot overwrite pinned fixtures')
    report=verify(args.bitcoind);args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({key:report[key] for key in ('status','network','bitcoin_core_version','headers_checked')}))
if __name__=='__main__': main()
