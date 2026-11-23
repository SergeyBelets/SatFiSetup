"""Prove the prepared real state deployment without modifying qualified runners."""
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

from .compress_local import PROFILE
from .state_trial import read, write, ROOT
from .wrap_local import sha


def commit_completion(base,out,report):
    """Authenticate the completed child output before committing its report."""
    encoded=(json.dumps(report,indent=2)+'\n').encode()
    local=os.environ.get('SATOSHIFI_LOCAL_PROOF_CONFIG')
    if report['status']=='VERIFIED' and local:
        import hashlib
        from signer_agent.zk_local_proofs import LocalProofs
        from signer_agent.zk_worker_prover import write as private_json
        config=json.loads(local)
        if set(config)!={'directory','chain_id','zk_state','identity_file'}:raise ValueError('ZK_LOCAL_PROOF_CONFIG')
        origin=LocalProofs(config)
        private_json(out/'local-origin.json',origin.sign(origin.payload(base,out/'receipt.bin',stage=report['stage'],
            report_sha256=hashlib.sha256(encoded).hexdigest())))
    from signer_agent.dkg import atomic_private
    atomic_private(out/'report.json',encoded)


def stage_runtime(stage, cuda):
    from signer_agent.zk_worker_prover import cuda_tool, pinned_tool

    if stage == "groth16" or cuda is None:
        return pinned_tool("r0vm"), None
    return cuda_tool(cuda), cuda


def run(command, log, profile, env, timeout, *, base=None, read_only=(), broker=None, cuda=False):
    started=time.monotonic();process=None
    # A prover starts a new process group. Keep the original owning worker lock
    # alive even if its controller is interrupted before runner cleanup.
    inherited=os.environ.get('SATOSHIFI_PROVER_LOCK_FD')
    descriptors=() if inherited is None else (int(inherited),)
    for descriptor in descriptors:os.fstat(descriptor)
    from signer_agent.zk_isolation import launch
    isolated,env=launch(command,base or log.parent,env,loopback='localhost' in profile,
        broker=broker,read_only=read_only,lock_fd=int(inherited) if inherited else None,
        **({"cuda": True} if cuda else {}))
    try:
        with log.open('w') as stream:
            process=subprocess.Popen(isolated,
                env=env,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=descriptors)
            while process.poll() is None:
                if time.monotonic()-started>=timeout:
                    os.killpg(process.pid,signal.SIGKILL);process.wait();break
                time.sleep(1)
            return dict(exit_code=process.wait(),elapsed_seconds=round(time.monotonic()-started,3),log=log.name,
                isolation_backend='linux-bwrap-namespaces-seccomp' if sys.platform=='linux' else 'macos-sandbox-exec')
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid,signal.SIGKILL);process.wait()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('stage',choices=['composite','succinct','groth16'])
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--timeout',type=int,default=7200)
    a=parser.parse_args();base=a.output.resolve();prep=read(base/'preparation.json')
    from signer_agent.zk_worker_prover import MAX_STAGE_TIMEOUT_SECONDS, pinned_tool
    if not 1<=a.timeout<=MAX_STAGE_TIMEOUT_SECONDS:raise ValueError('Invalid stage timeout')
    if any(sha(ROOT / name) != digest for name, digest in prep['source_sha256'].items()):
        raise ValueError("ZK_PROVING_SOURCE_DRIFT")
    def input_matches():
        return (sha(base / "state.input") == prep["input_sha256"]
                and sha(base / "state.journal") == prep["journal_sha256"])
    if not input_matches():
        raise ValueError("ZK_PROVING_INPUT_DRIFT")
    from signer_agent.zk_profiles import for_image
    selected=for_image(prep['program_image_id'])
    build=read(selected['report'])
    r0vm, cuda = stage_runtime(a.stage, json.loads(os.environ.get("SATOSHIFI_LOCAL_PROOF_CUDA", "null")))
    bridge=pinned_tool('satoshifi-receipt-bridge')
    wrapper=pinned_tool('wrap_groth16')
    program=selected['program']
    if sha(program) != build['evidence_sha256'][program.name]:
        raise ValueError("ZK_GUEST_PROGRAM_DRIFT")
    out=base/a.stage;out.mkdir(exist_ok=False)
    from signer_agent.zk_isolation import environment,probe_code,docker_endpoint
    from signer_agent.zk_prover_platform import release_profile
    env=environment(RUST_LOG='info')
    if cuda is not None and Path("/dev/dxg").exists():
        env["LD_LIBRARY_PATH"] = "/usr/lib/wsl/lib"
    profile=PROFILE;broker=nullcontext();observed={};checks={};prior=None
    ro=[r0vm,bridge,wrapper,program,base/'state.input',base/'state.journal']
    if a.stage=='composite':
        free=shutil.disk_usage(base).free
        write(out/'resources.json',dict(free_disk_bytes=free,required_free_disk_bytes=8*1024**3))
        if free<8*1024**3:raise ValueError('At least 8 GiB free required for STARK launch')
        profile='(version 1) (allow default) (deny network*)'
        # Strict no-network probe already qualified by the original runner.
        # CUDA runs without the socket filter (the driver needs sockets), so the
        # sandbox is loopback-only there: verify exactly that, not strict denial.
        probe=probe_code(cuda is not None)
        command=[r0vm,'--elf',program,'--initial-input',base/'state.input','--receipt',out/'receipt.bin','--receipt-kind','composite','-v']
        if cuda is not None:
            command += ["--po2", cuda["segment_limit_po2"]]
    else:
        previous=base/('composite' if a.stage=='succinct' else 'succinct')
        prior=read(previous/'report.json')
        if prior['status'] != 'VERIFIED':
            raise ValueError("ZK_STAGE_NOT_VERIFIED")
        if any(sha(previous / file) != item["sha256"] for file,item in prior['artifacts'].items()):
            raise ValueError("ZK_STAGE_ARTIFACT_DRIFT")
        ro.append(previous/'receipt.bin')
        probe=probe_code(True)
        if a.stage=='succinct':
            command=[bridge,'compress','succinct',r0vm,program,base/'state.journal',previous/'receipt.bin',out/'result']
        else:
            from .groth16_docker import Broker
            binary, socket = docker_endpoint()
            pin = release_profile()
            broker = Broker(out, binary, socket, pin['container_image'], pin['docker_arch'], a.timeout,
                            sdk_image=pin['sdk_image'])
            command=[wrapper,r0vm,program,base/'state.journal',previous/'receipt.bin',out/'receipt.bin']
    write(out/'attempt.json',dict(started_unix=time.time(),timeout_seconds=a.timeout,command=[str(x) for x in command],preparation_sha256=sha(base/'preparation.json')))
    kwargs=dict(base=base,read_only=ro)
    if cuda is not None:kwargs["cuda"]=True
    checks['network']=run([sys.executable,'-c',probe],out/'network.log',profile,env,30,**kwargs)
    cuda_ready = True
    if checks["network"]["exit_code"] == 0 and cuda is not None and a.stage == "composite":
        version = run([r0vm, "--version"], out / "cuda-version.log", profile, env, 30, **kwargs)
        help_log = out / "cuda-runtime.log"
        checks["cuda_runtime"] = run([r0vm, "--help"], help_log, profile, env, 30, **kwargs)
        cuda_ready = (version["exit_code"] == 0 and "risc0-r0vm 3.1.0" in (out / "cuda-version.log").read_text()
                      and checks["cuda_runtime"]["exit_code"] == 0 and "SatoshiFi CUDA segment limit" in help_log.read_text())
        checks["cuda_runtime"]["segment_flag_supported"] = cuda_ready
    if checks['network']['exit_code']==0 and cuda_ready:
        with broker as job:
            prove_env = env | job.env if job else env
            options = dict(kwargs, broker=job, read_only=[*ro, *job.read_only]) if job else kwargs
            checks['prove'] = run(command, out/'prove.log', profile, prove_env, a.timeout, **options)
        if job:
            observed.update(job.observed)
            checks['prove']['broker'] = job.diagnostics()
        if checks['prove']['exit_code']==0:
            if a.stage=='succinct':shutil.copy2(out/'result/receipt.bin',out/'receipt.bin')
            checks['verify']=run([bridge,'inspect',program,base/'state.journal',out/'receipt.bin',out/'verified'],out/'verify.log',profile,env,int(a.timeout),**kwargs)
    unchanged=all(sha(ROOT/p)==digest for p,digest in prep['source_sha256'].items())
    verified=read(out/'verified/receipt.json') if (out/'verified/receipt.json').exists() else {}
    ok=(checks.get('verify',{}).get('exit_code')==0 and unchanged and input_matches()
        and verified.get('receipt_kind')==a.stage)
    if ok and cuda is not None and a.stage == "composite":
        with (out / "prove.log").open() as log:
            ok = any(line.strip() == "SatoshiFi segment_limit_po2=" + cuda["segment_limit_po2"] for line in log)
    if a.stage=='groth16':ok=ok and bool(observed)
    effective_profile=('linux: private user/pid/net namespaces; '+('strict socket denial' if a.stage=='composite' and cuda is None else 'loopback-only RPC')
        if sys.platform=='linux' else profile)
    if a.stage == 'groth16':
        effective_profile += '; single-job Docker broker'
    report=dict(schema='satoshifi.state-proof-stage.v1',status='VERIFIED' if ok else 'INCOMPLETE',stage=a.stage,
        production_ready=False,source_sha256=prep['source_sha256'],source_unchanged=unchanged,checks=checks,network_profile=effective_profile,
        container=observed,input_sha256=prep['input_sha256'],journal_sha256=prep['journal_sha256'],preparation_sha256=sha(base/'preparation.json'),
        prior_report_sha256=sha(previous/'report.json') if prior else None,dev_mode=False,public_transactions=0,
        artifacts={str(p.relative_to(out)):dict(sha256=sha(p),bytes=p.stat().st_size) for p in out.rglob('*') if p.is_file()})
    if cuda is not None:
        report["cuda"] = {"r0vm_sha256": cuda["r0vm_sha256"], "segment_limit_po2": cuda["segment_limit_po2"]}
    # Commit authenticated completion in the child, not its controller. A
    # controller crash after this process exits must not lose provenance.
    # The receipt/report body is never accepted solely because it was copied.
    commit_completion(base,out,report)
    print(json.dumps(dict(status=report['status'],stage=a.stage,checks=checks)))
    return 0 if ok else 1


if __name__=='__main__':raise SystemExit(main())
