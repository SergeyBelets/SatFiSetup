"""Offline builds of the fixed testnet4 guest and participant witness host.

Guest compilation is a developer build; the resulting R0BF is program code.
No receipt/proof is generated or distributed by this command.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from .testnet4_source import ROOT, ZK, verify_prepared


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def build(a):
    manifest=json.loads(a.source_manifest.read_text())
    verify_prepared(a.prepared,manifest)
    a.output.mkdir(parents=True,exist_ok=False)
    env={k:v for k,v in os.environ.items() if not k.startswith(('RUST','CARGO_','RISC0_','BONSAI_'))}
    env.update(CARGO_HOME=str(a.cargo_home),RUSTC=str(a.rustc),CARGO_BUILD_JOBS='2',CARGO_INCREMENTAL='0')
    started=time.monotonic()
    def run(command,name,environment=env):
        with (a.output/name).open('w') as stream:
            subprocess.run([str(x) for x in command],env=environment,stdout=stream,stderr=subprocess.STDOUT,check=True)
    report=dict(schema='satoshifi.testnet4-build.v1',bitcoin_network='testnet4',
        proofs_generated=False,qualified=False,production_ready=False)
    if a.action=='host':
        env['CARGO_TARGET_DIR']=str(a.target)
        run([a.cargo,'build','--offline','--locked','--release','--manifest-path',
             a.prepared/'testnet4-worker-host/Cargo.toml'],'host-build.log')
        binary=a.target/'release/satoshifi-testnet4-zk-worker-host'
        report.update(status='TESTNET4_HOST_BUILD_PASS',binary=str(binary),binary_sha256=sha(binary))
    else:
        if a.guest_rustc is None or a.r0vm is None:
            raise ValueError('TESTNET4_GUEST_TOOLS_REQUIRED')
        pins=json.loads((ZK/'guest-integration/toolchain.json').read_text())
        if sha(a.guest_rustc)!=pins['guest_rustc_sha256'] or sha(a.r0vm)!=pins['r0vm_sha256']:
            raise ValueError('TESTNET4_GUEST_TOOL_PIN')
        guest_env=dict(env,RUSTC=str(a.guest_rustc),CARGO_TARGET_DIR=str(a.target/'guest'),RISC0_FEATURE_bigint2='')
        flags=['-C','passes=lower-atomic','-C','link-arg=-Ttext=0x00200800','-C','link-arg=--fatal-warnings',
            '-C','panic=abort','--cfg','getrandom_backend="custom"',
            '--remap-path-prefix='+str(a.prepared)+'=/satoshifi-zkspv',
            '--remap-path-prefix='+str(a.cargo_home)+'=/cargo',
            '--remap-path-prefix='+str(a.guest_rustc.parent.parent)+'=/rust']
        guest_env['CARGO_ENCODED_RUSTFLAGS']='\x1f'.join(flags)
        run([a.cargo,'build','--offline','--locked','--release','--target','riscv32im-risc0-zkvm-elf',
            '--manifest-path',a.prepared/'testnet4-integration/guest/Cargo.toml'],'guest-build.log',guest_env)
        env['CARGO_TARGET_DIR']=str(a.target/'image-tool')
        run([a.cargo,'build','--offline','--locked','--manifest-path',
            a.prepared/'testnet4-integration/image-tool/Cargo.toml'],'image-tool-build.log')
        elf=a.target/'guest/riscv32im-risc0-zkvm-elf/release/satoshifi-testnet4-transition-guest'
        program=a.output/'testnet4-transition.r0bf'
        image=subprocess.check_output([a.target/'image-tool/debug/satoshifi-guest-image',elf,program],env=env,text=True).strip()
        runtime=subprocess.check_output([a.r0vm,'--elf',program,'--id'],env=env,text=True).strip()
        if image!=runtime or len(image)!=64 or image=='0'*64:
            raise ValueError('TESTNET4_IMAGE_ID_MISMATCH')
        sources=dict(manifest['source_sha256'])
        for p in (Path(__file__),ROOT/'ops/zkspv/testnet4_source.py'):
            sources[str(p.relative_to(ROOT))]=sha(p)
        report.update(status='TESTNET4_GUEST_BUILD_PASS',program_image_id=image,journal_bytes=1216,
            source_sha256=sources,evidence_sha256={program.name:sha(program)},flags=flags,
            tools={'r0vm':{'sha256':sha(a.r0vm)},'guest_rustc':{'sha256':sha(a.guest_rustc)}})
        # Only pinned program code and its build metadata are promoted.
        dest=ZK/'testnet4-integration/evidence/testnet4-transition.r0bf'
        if dest.exists():raise ValueError('TESTNET4_PROGRAM_ALREADY_EXISTS')
        dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(program,dest)
        (ZK/'testnet4-integration/report.json').write_text(json.dumps(report,indent=2)+'\n')
    verify_prepared(a.prepared,manifest)
    report['seconds']=round(time.monotonic()-started,3)
    (a.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['guest','host'])
    for name in ('prepared','source-manifest','cargo','rustc','cargo-home','target','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--guest-rustc',type=Path);p.add_argument('--r0vm',type=Path)
    a=p.parse_args()
    for name,value in vars(a).items():
        if isinstance(value,Path):setattr(a,name,value.resolve())
    r=build(a);print(json.dumps({k:v for k,v in r.items() if k in ('status','program_image_id','binary_sha256','seconds')}))


if __name__=='__main__':main()
