"""Use the separate exact-regtest SDK bridge with the existing isolated launcher.

An existing genuine STARK may be independently revalidated after the legacy
1216-byte inspector rejects its journal length. Preserve that failed attempt and
its receipt; never change guest/input/journal or pretend a new STARK was computed.
"""
import argparse
import os
from pathlib import Path
import shutil
import sys
from . import scenario_prove as launcher
from .state_trial import ROOT, read, write
from .wrap_local import sha
from .deposit_build import HERE

def toolchain():
    path=HERE/'bridge-build-report.json';p=read(path);assert p['status']=='BUILD_PASS' and p['journal_bytes']==512 and p['disable_dev_mode']
    sources=dict(p['source_sha256']);sources[str(path.relative_to(ROOT))]=sha(path)
    sources[str(Path(__file__).resolve().relative_to(ROOT))]=sha(Path(__file__))
    for name,digest in sources.items():assert sha(ROOT/name)==digest,name
    launcher.BRIDGE=Path(p['bridge_binary']);launcher.WRAPPER=Path(p['wrapper_binary']);launcher.GROTH=HERE/'evidence/bridge-tools'
    assert sha(launcher.BRIDGE)==p['bridge_sha256'] and sha(launcher.WRAPPER)==p['wrapper_sha256']
    return sources

def recover(base,sources):
    old=base/'composite';report=read(old/'report.json');prep=read(base/'preparation.json')
    assert report['status']=='INCOMPLETE' and report['checks']['prove']['exit_code']==0 and report['checks']['network']['exit_code']==0
    assert report['checks']['verify']['exit_code']!=0 and 'exact V2 journal required' in (old/'verify.log').read_text()
    assert report['preparation_sha256']==sha(base/'preparation.json') and report['source_unchanged']
    for p,digest in prep['source_sha256'].items():assert sha(ROOT/p)==digest,p
    for p,item in report['artifacts'].items():assert sha(old/p)==item['sha256'],p
    assert sha(base/'state.journal')==prep['journal_sha256'] and len((base/'state.journal').read_bytes())==512
    assert sha(base/'state.input')==prep['input_sha256'] and sha(ROOT/prep['program'])==prep['program_sha256']
    preserved=base/'composite-legacy-inspector';assert not preserved.exists()
    old.rename(preserved);old.mkdir()
    for name in ['receipt.bin','prove.log','network.log']:shutil.copyfile(preserved/name,old/name)
    env=dict(PATH='/usr/bin:/bin',TMPDIR='/private/tmp',RISC0_DEV_MODE='0',NO_COLOR='1')
    profile='(version 1) (allow default) (deny network*)'
    check=launcher.run([launcher.BRIDGE,'inspect',ROOT/prep['program'],base/'state.journal',old/'receipt.bin',old/'verified'],old/'verify.log',profile,env,180)
    receipt=read(old/'verified/receipt.json') if (old/'verified/receipt.json').exists() else {}
    ok=check['exit_code']==0 and receipt.get('status')=='VERIFIED' and receipt.get('receipt_kind')=='composite'
    assert sha(old/'receipt.bin')==sha(preserved/'receipt.bin')
    for p,digest in {**prep['source_sha256'],**sources}.items():assert sha(ROOT/p)==digest,p
    result=dict(report,status='VERIFIED' if ok else 'INCOMPLETE',checks=dict(report['checks'],verify=check),
        supplemental_source_sha256=sources,recovery=dict(reason='Legacy inspector requires 1216-byte journal; independent regtest inspector requires exact 512-byte domain',
            original_report=str((preserved/'report.json').relative_to(ROOT)),original_report_sha256=sha(preserved/'report.json'),
            original_receipt_sha256=sha(preserved/'receipt.bin'),new_stark_computed=False),
        artifacts={str(p.relative_to(old)):dict(sha256=sha(p),bytes=p.stat().st_size) for p in old.rglob('*') if p.is_file()})
    write(old/'report.json',result)
    if not ok:raise RuntimeError('Regtest receipt verification failed')
    print('VERIFIED: exact original STARK receipt independently revalidated; failed legacy inspector evidence preserved')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['recover-composite','composite','succinct','groth16'])
    p.add_argument('--output',type=Path,required=True);p.add_argument('--timeout',type=int,default=14400)
    a=p.parse_args();base=a.output.resolve();sources=toolchain()
    if a.stage=='recover-composite':recover(base,sources);return
    sys.argv=[sys.argv[0],a.stage,'--output',str(base),'--timeout',str(a.timeout)]
    result=launcher.main()
    path=base/a.stage/'report.json';report=read(path)
    for name,digest in sources.items():assert sha(ROOT/name)==digest,name
    report['supplemental_source_sha256']=sources;write(path,report)
    if result:raise SystemExit(result)

if __name__=='__main__':main()
