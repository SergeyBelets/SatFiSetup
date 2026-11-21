"""Prove an explicitly pinned mainnet or TEST_ONLY scenario in isolated stages."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

from .compress_local import PROFILE, PROBE
from .state_trial import read, write, ROOT, GUEST, GROTH
from .wrap_local import sha

R0VM=Path('/private/tmp/satoshifi-risc0-3.0.3/r0vm')
BRIDGE=Path('/private/tmp/satoshifi-proof-tools/satoshifi-receipt-bridge')
WRAPPER=Path('/private/tmp/satoshifi-proof-tools/wrap_groth16')


def run(command, log, profile, env, timeout):
    started=time.monotonic();process=None
    try:
        with log.open('w') as stream:
            process=subprocess.Popen(['/usr/bin/sandbox-exec','-p',profile]+[str(x) for x in command],
                env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            while process.poll() is None:
                if time.monotonic()-started>=timeout:
                    os.killpg(process.pid,signal.SIGKILL);process.wait();break
                time.sleep(1)
            return dict(exit_code=process.wait(),elapsed_seconds=round(time.monotonic()-started,3),log=log.name)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.wait()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('stage',choices=['composite','succinct','groth16'])
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--timeout',type=int,default=7200)
    a=parser.parse_args();base=a.output.resolve();prep=read(base/'preparation.json')
    if not 1<=a.timeout<=14400:raise ValueError('Timeout 1..14400 required')
    for p,digest in prep['source_sha256'].items():assert sha(ROOT/p)==digest,p
    assert sha(base/'state.input')==prep['input_sha256'] and sha(base/'state.journal')==prep['journal_sha256']
    build=read(GUEST/'report.json');tools=read(GROTH/'wrapper-build-report.json')
    assert sha(R0VM)==build['tools']['r0vm']['sha256'] and sha(BRIDGE)==tools['bridge_sha256'] and sha(WRAPPER)==tools['wrapper_sha256']
    program=ROOT/prep['program'];assert sha(program)==prep['program_sha256']
    actual_image=subprocess.check_output([R0VM,'--elf',program,'--id'],text=True).strip()
    assert actual_image==prep['program_image_id']
    out=base/a.stage;out.mkdir(exist_ok=False)
    env=dict(PATH='/usr/bin:/bin',TMPDIR='/private/tmp',RISC0_DEV_MODE='0',RUST_LOG='info,risc0_zkvm::host::server::prove=debug',NO_COLOR='1')
    profile=PROFILE;broker=nullcontext();observed={};checks={};prior=None
    if a.stage=='composite':
        free=shutil.disk_usage(base).free
        write(out/'resources.json',dict(free_disk_bytes=free,required_free_disk_bytes=8*1024**3))
        if free<8*1024**3:raise ValueError('At least 8 GiB free required for STARK launch')
        profile='(version 1) (allow default) (deny network*)'
        # Strict no-network probe already qualified by the original runner.
        probe='''import errno,socket
for family,address in [(socket.AF_INET,('127.0.0.1',9)),(socket.AF_INET6,('::1',9))]:
 with socket.socket(family,socket.SOCK_STREAM) as sock:
  try:sock.connect(address)
  except OSError as e:assert e.errno in (errno.EPERM,errno.EACCES),repr(e)
  else:raise AssertionError('network allowed')
print('IPv4/IPv6 denied by OS')
'''
        command=[R0VM,'--elf',program,'--initial-input',base/'state.input','--receipt',out/'receipt.bin','--receipt-kind','composite','-vv']
    else:
        previous=base/('composite' if a.stage=='succinct' else 'succinct')
        prior=read(previous/'report.json');assert prior['status']=='VERIFIED'
        for file,item in prior['artifacts'].items():assert sha(previous/file)==item['sha256'],file
        probe=PROBE
        if a.stage=='succinct':
            command=[BRIDGE,'compress','succinct',R0VM,program,base/'state.journal',previous/'receipt.bin',out/'result']
        else:
            from signer_agent.zk_isolation import docker_endpoint
            from .groth16_docker import Broker
            binary, socket = docker_endpoint()
            pin = read(GROTH/'container-pin.json')
            broker = Broker(out, binary, socket, pin['repository']+'@'+pin['arm64']['digest'], 'arm64', a.timeout)
            command=[WRAPPER,R0VM,program,base/'state.journal',previous/'receipt.bin',out/'receipt.bin']
    write(out/'attempt.json',dict(started_unix=time.time(),timeout_seconds=a.timeout,command=[str(x) for x in command],preparation_sha256=sha(base/'preparation.json')))
    checks['network']=run([sys.executable,'-c',probe],out/'network.log',profile,env,30)
    if checks['network']['exit_code']==0:
        from signer_agent.zk_isolation import broker_profile
        with broker as job:
            prove_profile = broker_profile(profile, job) if job else profile
            checks['prove']=run(command,out/'prove.log',prove_profile,env | job.env if job else env,a.timeout)
        if job:
            observed.update(job.observed)
            checks['prove']['broker'] = job.diagnostics()
        if checks['prove']['exit_code']==0:
            if a.stage=='succinct':shutil.copy2(out/'result/receipt.bin',out/'receipt.bin')
            checks['verify']=run([BRIDGE,'inspect',program,base/'state.journal',out/'receipt.bin',out/'verified'],out/'verify.log',profile,env,180)
    unchanged=all(sha(ROOT/p)==digest for p,digest in prep['source_sha256'].items())
    verified=read(out/'verified/receipt.json') if (out/'verified/receipt.json').exists() else {}
    ok=checks.get('verify',{}).get('exit_code')==0 and unchanged and verified.get('receipt_kind')==a.stage
    if a.stage=='groth16':ok=ok and bool(observed)
    report=dict(schema='satoshifi.scenario-proof-stage.v1',status='VERIFIED' if ok else 'INCOMPLETE',stage=a.stage,
        production_ready=False,source_sha256=prep['source_sha256'],source_unchanged=unchanged,checks=checks,network_profile=profile + ('; single-job Docker broker' if a.stage=='groth16' else ''),
        container=observed,input_sha256=prep['input_sha256'],journal_sha256=prep['journal_sha256'],preparation_sha256=sha(base/'preparation.json'),
        prior_report_sha256=sha(previous/'report.json') if prior else None,dev_mode=False,public_transactions=0,
        artifacts={str(p.relative_to(out)):dict(sha256=sha(p),bytes=p.stat().st_size) for p in out.rglob('*') if p.is_file()})
    write(out/'report.json',report);print(json.dumps(dict(status=report['status'],stage=a.stage,checks=checks)))
    return 0 if ok else 1


if __name__=='__main__':raise SystemExit(main())
