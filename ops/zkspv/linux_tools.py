"""Install reproducible Linux x86-64 tools; never download or distribute proofs."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid
import sys
import platform

ROOT=Path(__file__).resolve().parents[2]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_sources():
    folder=ROOT/'rust/zkspv/groth16-integration'
    pins={}
    for report in ('build-report.json','wrapper-build-report.json'):pins.update(json.loads((folder/report).read_text())['source_sha256'])
    for name,digest in pins.items():
        if sha(ROOT/name)!=digest:raise ValueError('ZK_LINUX_BUILD_SOURCE_DRIFT:'+name)
    return pins


def install(directory):
    from signer_agent.zk_prover_platform import LINUX_PINS
    from signer_agent.zk_isolation import docker_endpoint
    pins=json.loads(LINUX_PINS.read_text());sources=checked_sources();directory=Path(directory).resolve()
    directory.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(directory.parent).free<8*1024**3:raise ValueError('ZK_PROVER_DISK_BUDGET')
    directory.mkdir(mode=0o700,parents=True,exist_ok=False)
    binary,socket=docker_endpoint()
    config=directory/'docker-config';config.mkdir()
    docker=[str(binary),'--config',str(config),'--host','unix://'+str(socket)]
    name='satoshifi-zk-build-'+uuid.uuid4().hex
    def run(args,**kwargs):return subprocess.run(docker+list(map(str,args)),check=True,**kwargs)
    with tempfile.TemporaryDirectory(prefix='zk-tools-',dir=directory) as temporary:
        temp=Path(temporary);archive=temp/'runtime.tgz'
        with urllib.request.urlopen(pins['runtime_archive_url'],timeout=120) as incoming,archive.open('wb') as output:
            shutil.copyfileobj(incoming,output)
        if sha(archive)!=pins['runtime_archive_sha256']:raise ValueError('ZK_LINUX_RUNTIME_ARCHIVE_DRIFT')
        with tarfile.open(archive) as bundle:
            members=[m for m in bundle.getmembers() if m.name=='r0vm' and m.isfile()]
            if len(members)!=1:raise ValueError('ZK_LINUX_RUNTIME_ARCHIVE')
            with bundle.extractfile(members[0]) as source,(directory/'r0vm').open('xb') as output:shutil.copyfileobj(source,output)
        source=temp/'source';source.mkdir()
        for relative in sources:
            path=ROOT/relative;target=source/path.relative_to(ROOT/'rust/zkspv/groth16-integration')
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
        # Source paths, Cargo paths, compiler/linker and environment are fixed.
        run(['pull','--platform','linux/amd64',pins['builder_image']])
        try:
            run(['run','-d','--name',name,'--platform','linux/amd64','--cpus','2','--memory','4g',
                '-e','CARGO_HOME=/cargo','-e','CARGO_TARGET_DIR=/target','-e','CARGO_BUILD_JOBS=2','-e','SOURCE_DATE_EPOCH=0',
                pins['builder_image'],'sleep','infinity'],stdout=subprocess.DEVNULL)
            run(['cp',str(source),name+':/workspace'])
            with (directory/'build.log').open('w') as log:
                run(['exec','-w','/workspace',name,'cargo','fetch','--locked'],stdout=log,stderr=subprocess.STDOUT)
                info=json.loads(subprocess.check_output(docker+['inspect',name]))[0]
                for network in info['NetworkSettings']['Networks']:run(['network','disconnect',network,name])
                info=json.loads(subprocess.check_output(docker+['inspect',name]))[0]
                if info['NetworkSettings']['Networks']:raise ValueError('ZK_LINUX_OFFLINE_BUILD_REQUIRED')
                run(['exec','-w','/workspace',name,'cargo','build','--offline','--locked','--bins'],stdout=log,stderr=subprocess.STDOUT)
            for tool in ('satoshifi-receipt-bridge','wrap_groth16'):run(['cp',name+':/target/debug/'+tool,str(directory/tool)])
        finally:subprocess.run(docker+['rm','-f',name],capture_output=True,timeout=60)
    if checked_sources()!=sources:raise ValueError('ZK_LINUX_BUILD_SOURCE_DRIFT')
    for name,digest in pins['tools'].items():
        if sha(directory/name)!=digest:raise ValueError('ZK_PROVER_TOOL_DRIFT:'+name)
        (directory/name).chmod(0o755)
    native=sys.platform=='linux' and platform.machine().lower() in ('x86_64','amd64')
    if native:
        version=subprocess.run([str(directory/'r0vm'),'--version'],capture_output=True,text=True,timeout=30)
        if version.returncode or '3.0.3' not in version.stdout:raise ValueError('ZK_LINUX_TOOL_SMOKE_FAILED')
        for name in ('satoshifi-receipt-bridge','wrap_groth16'):
            rejected=subprocess.run([str(directory/name)],capture_output=True,timeout=30)
            if rejected.returncode!=101:raise ValueError('ZK_LINUX_TOOL_SMOKE_FAILED')
    result=dict(schema='satoshifi.linux-prover-installation.v1',status='PINNED_TOOLS_INSTALLED',
        source_sha256=sources,tool_sha256=pins['tools'],profile_sha256=sha(LINUX_PINS),
        builder_image=pins['builder_image'],native_tool_startup_checked=native,
        proofs_generated=False,qualified=False,production_ready=False)
    (directory/'installation.json').write_text(json.dumps(result,indent=2)+'\n')
    variables={'r0vm':'SATOHSI_R0VM','satoshifi-receipt-bridge':'SATOHSI_RECEIPT_BRIDGE','wrap_groth16':'SATOHSI_GROTH16_WRAPPER'}
    (directory/'activate.sh').write_text(''.join('export '+variables[n]+'='+shlex.quote(str(directory/n))+'\n' for n in variables))
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,required=True)
    a=parser.parse_args(argv);print(json.dumps(install(a.directory),indent=2))


if __name__=='__main__':main()
