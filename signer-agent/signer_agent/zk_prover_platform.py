"""Immutable platform-specific tools; guest and verifier parameters stay common."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[2]
LINUX_PINS=ROOT/'config/zk-prover-linux-x86_64.json'
RELEASE_PIN=ROOT/'config/zk-prover-3.1.0.json'
RELEASE_CRATE=ROOT/'rust/zkspv/risc0-release'
RELEASE_TOOLS=('r0vm','satoshifi-receipt-bridge','wrap_groth16')


def release_profile():
    machine=platform.machine().lower()
    if sys.platform=='linux' and machine in ('x86_64','amd64'):
        name='linux-x86_64'
    elif sys.platform=='darwin' and machine in ('arm64','aarch64'):
        name='darwin-arm64'
    else:
        raise ValueError('ZK_LOCAL_RUNNER_PLATFORM_NOT_SUPPORTED')
    pins=json.loads(RELEASE_PIN.read_text())
    return dict(pins,platform=name,runtime=pins['platforms'][name])


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def release_sources():
    bridge=ROOT/'rust/zkspv/groth16-integration/src'
    paths=(RELEASE_PIN,RELEASE_CRATE/'Cargo.toml',RELEASE_CRATE/'Cargo.lock',
           bridge/'main.rs',bridge/'bin/wrap_groth16.rs')
    return {str(path.relative_to(ROOT)):file_sha256(path) for path in paths}


def release_tool(name):
    p=release_profile()
    directory=Path(os.environ.get('SATOHSI_RISC0_RELEASE_TOOLS',ROOT/'ops/.local-tools/risc0-3.1.0')).resolve()
    stamp=directory/'build.json'
    if not stamp.is_file() or stamp.is_symlink():
        raise ValueError('ZK_RISC0_RELEASE_TOOLS_REQUIRED')
    info=directory.stat()
    if info.st_uid!=os.getuid() or info.st_mode & 0o022:
        raise ValueError('ZK_RISC0_RELEASE_BUILD_DRIFT')
    build=json.loads(stamp.read_text())
    if (build.get('sdk')!=p['sdk'] or build.get('platform')!=p['platform']
            or build.get('sources')!=release_sources() or set(build.get('tools',{}))!=set(RELEASE_TOOLS)
            or build['tools']['r0vm']!=p['runtime']['r0vm_sha256']):
        raise ValueError('ZK_RISC0_RELEASE_BUILD_DRIFT')
    path=directory/name
    if (name not in RELEASE_TOOLS or path.is_symlink() or not path.is_file()
            or not os.access(path,os.X_OK) or file_sha256(path)!=build['tools'][name]):
        raise ValueError('ZK_PROVER_TOOL_DRIFT:'+name)
    return path


def profile():
    machine=platform.machine().lower()
    if sys.platform=='linux' and machine in ('x86_64','amd64'):
        result=json.loads(LINUX_PINS.read_text())
        if result.get('platform')!='linux-x86_64' or result.get('sdk')!='3.0.3':raise ValueError('ZK_PROVER_PROFILE')
        return result
    if sys.platform=='darwin' and machine in ('arm64','aarch64'):
        guest=json.loads((ROOT/'rust/zkspv/guest-integration/report.json').read_text())
        bridge=json.loads((ROOT/'rust/zkspv/groth16-integration/wrapper-build-report.json').read_text())
        pin=json.loads((ROOT/'rust/zkspv/groth16-integration/container-pin.json').read_text())
        return dict(platform='darwin-arm64',docker_arch='arm64',container_image=pin['repository']+'@'+pin['arm64']['digest'],
            tools={'r0vm':guest['tools']['r0vm']['sha256'],'satoshifi-receipt-bridge':bridge['bridge_sha256'],'wrap_groth16':bridge['wrapper_sha256']})
    raise ValueError('ZK_LOCAL_RUNNER_PLATFORM_NOT_SUPPORTED')


def tool(name,expected=None):
    p=profile();digest=p['tools'][name]
    if p['platform']=='darwin-arm64' and expected is not None and expected!=digest:raise ValueError('ZK_PROVER_TOOL_DRIFT:'+name)
    if not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('ZK_PROVER_TOOL_PIN_REQUIRED')
    variables={'r0vm':'SATOHSI_R0VM','satoshifi-receipt-bridge':'SATOHSI_RECEIPT_BRIDGE','wrap_groth16':'SATOHSI_GROTH16_WRAPPER'}
    directory=ROOT/('ops/.local-tools/zk-linux-x86_64' if p['platform']=='linux-x86_64' else 'ops/.local-tools/acceptance-20261102')
    path=Path(os.environ.get(variables[name]) or shutil.which(name) or directory/name)
    if not path.is_file() or not os.access(path,os.X_OK) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise ValueError('ZK_PROVER_TOOL_DRIFT:'+name)
    return path.resolve()


def check(*,directory=None,full=False):
    """Exercise isolation locally; never infer freshness/proving from OS alone."""
    from .zk_isolation import launch,environment,probe_code,docker_endpoint
    p=release_profile();tools={name:release_tool(name) for name in RELEASE_TOOLS}
    if directory is not None:
        from .platform_support import capabilities
        if capabilities()['wsl'] and str(Path(directory).resolve()).startswith('/mnt/'):
            raise ValueError('ZK_WSL_LINUX_FILESYSTEM_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='zk-isolation-',dir=directory) as folder:
        base=Path(folder)
        for loopback in (False,True):
            command,env=launch([sys.executable,'-c',probe_code(loopback)],base,environment(),loopback=loopback)
            try:subprocess.run(command,env=env,capture_output=True,timeout=30,check=True)
            except subprocess.SubprocessError as exc:
                raise ValueError('ZK_LINUX_ISOLATION_UNAVAILABLE' if sys.platform=='linux' else 'ZK_LOCAL_ISOLATION_UNAVAILABLE') from exc
        if full:
            if shutil.disk_usage(base).free<8*1024**3:raise ValueError('ZK_PROVER_DISK_BUDGET')
            binary,socket=docker_endpoint();config=base/'docker-config';config.mkdir()
            prefix=[str(binary),'--config',str(config),'--host','unix://'+str(socket)]
            inspected=json.loads(subprocess.check_output(prefix+['image','inspect',p['container_image']],timeout=30))[0]
            if inspected['Architecture']!=p['docker_arch'] or inspected['Os']!='linux':raise ValueError('ZK_LOCAL_PROVER_IMAGE_MISMATCH')
    return dict(status='LOCAL_PROVER_PREFLIGHT_PASS' if full else 'TOOLS_AND_ISOLATION_PASS',platform=p['platform'],
        tools={n:dict(path=str(path),sha256=file_sha256(path)) for n,path in tools.items()},
        external_network_denied=True,loopback_rpc=True,docker_image_checked=full,
        fresh_proof_generated=False,qualified=False,production_ready=False)


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--full',action='store_true')
    parser.add_argument('--directory',type=Path)
    a=parser.parse_args(argv)
    try:result=check(directory=a.directory,full=a.full)
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        from .participant_errors import participant_exception_code
        print(json.dumps(dict(status='BLOCKED',reason=participant_exception_code(error),qualified=False)));return 2
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
