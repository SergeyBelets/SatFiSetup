"""Platform-specific local network isolation. No network-enabled fallback."""
import json
from functools import lru_cache
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

STRICT='(version 1) (allow default) (deny network*)'
LOOPBACK=STRICT+' (allow network-bind network-inbound (local ip "localhost:*")) (allow network-outbound (remote ip "localhost:*"))'
ROOT=Path(__file__).resolve().parents[2]


def environment(**values):
    result=dict(PATH='/usr/bin:/bin',TMPDIR='/private/tmp' if sys.platform=='darwin' else '/tmp',RISC0_DEV_MODE='0',NO_COLOR='1')
    result.update(values);return result


def docker_endpoint():
    binary=Path(os.environ.get('SATOHSI_DOCKER') or shutil.which('docker') or '/usr/local/bin/docker').resolve()
    if not binary.is_file() or not os.access(binary,os.X_OK):raise ValueError('ZK_DOCKER_REQUIRED')
    explicit=os.environ.get('SATOHSI_DOCKER_SOCKET');host=os.environ.get('DOCKER_HOST')
    if not explicit and host:
        if not host.startswith('unix:///'):raise ValueError('ZK_LOCAL_DOCKER_SOCKET_REQUIRED')
        explicit=host[7:]
    candidates=[Path(explicit)] if explicit else [Path.home()/'.docker/run/docker.sock',Path('/var/run/docker.sock')]
    if not explicit and os.environ.get('XDG_RUNTIME_DIR'):candidates.insert(0,Path(os.environ['XDG_RUNTIME_DIR'])/'docker.sock')
    for path in candidates:
        if path.is_absolute() and path.exists() and stat.S_ISSOCK(path.stat().st_mode):return binary,path.resolve()
    raise ValueError('ZK_LOCAL_DOCKER_SOCKET_REQUIRED')


def broker_profile(profile, broker):
    return (profile + ' (deny file* (subpath ' + json.dumps(str(broker.private)) + '))'
            + ' (deny file-write* (require-not (subpath ' + json.dumps(str(broker.out)) + ')))'
            + ' (allow network-outbound (remote unix-socket (path-literal '
            + json.dumps(str(broker.endpoint)) + ')))')


@lru_cache(maxsize=1)
def macos_python_libraries():
    """Allow the exact shared libraries needed by a host Python or its shim."""
    paths = set()
    pending = [Path(sys.executable).resolve()]
    while pending:
        output = subprocess.check_output(['/usr/bin/otool', '-L', str(pending.pop())], text=True, timeout=10)
        for line in output.splitlines()[1:]:
            path = Path(line.strip().split(' (compatibility version ', 1)[0])
            if not path.is_absolute() or path in paths or str(path).startswith(('/System/', '/usr/lib/')):
                continue
            resolved = path.resolve(strict=True)
            if resolved not in paths:
                pending.append(resolved)
            paths.update((path, resolved))
    return tuple(sorted(paths))


def macos_filesystem(profile, command, base, read_only, broker):
    directories = [base, Path('/System'), Path('/usr/lib'), Path('/usr/share'),
                   Path('/usr/bin'), Path('/bin'), Path(sys.base_prefix).resolve()]
    if directories[-1] in (Path('/'), Path('/Users'), Path.home()):
        raise ValueError('ZK_MACOS_PYTHON_PREFIX')
    files = [Path('/'), Path('/dev/null'), Path('/dev/zero'), Path('/dev/random'), Path('/dev/urandom'),
             Path('/private/etc/localtime'), Path(sys.prefix) / 'pyvenv.cfg',
             Path(command[0]).resolve(strict=True), Path(sys.executable).resolve()]
    files.extend(Path(p).resolve(strict=True) for p in read_only)
    files.extend(macos_python_libraries())
    if broker:
        files.append(broker.endpoint)
    allowed = ' '.join([f'(subpath {json.dumps(str(p))})' for p in directories]
                       + [f'(literal {json.dumps(str(p))})' for p in files])
    writable = json.dumps(str(base))
    return (profile + f' (deny file-read-data (require-not (require-any {allowed})))'
            + f' (deny file-write* (require-not (subpath {writable})))')


def launch(command,base,env,*,loopback=False,broker=None,read_only=(),lock_fd=None,cuda=False):
    if cuda and broker:
        raise ValueError("ZK_CUDA_DOCKER_SOCKET_FORBIDDEN")
    command=[str(x) for x in command]
    if sys.platform=='darwin':
        if cuda:raise ValueError("ZK_CUDA_PLATFORM_REQUIRED")
        base=Path(base).resolve(strict=True)
        command[0]=str(Path(command[0]).resolve(strict=True))
        if command[0]==str(Path(sys.executable).resolve()):
            command[1:1]=['-I']
        profile=macos_filesystem(LOOPBACK if loopback else STRICT, command, base, read_only, broker)
        if broker:
            profile = broker_profile(profile, broker)
        else:
            env=dict(env,TMPDIR=str(base))
        return ['/usr/bin/sandbox-exec','-p',profile,*command],env
    if sys.platform!='linux':raise ValueError('ZK_LOCAL_RUNNER_PLATFORM_NOT_SUPPORTED')
    bwrap=shutil.which('bwrap')
    if not bwrap:raise ValueError('ZK_LINUX_BWRAP_REQUIRED')
    if broker:
        # These directory trees are shared with bwrap. Binding only the
        # broker socket is insufficient if a custom daemon path is under one.
        visible = [Path(folder).resolve() for folder in
                   (base, sys.base_prefix, '/usr', '/lib', '/lib64', '/bin', '/sbin')]
        if any(path.resolve().is_relative_to(folder)
               for path in (broker.daemon, broker.private) for folder in visible):
            raise ValueError("ZK_GROTH16_BROKER_CONFIG")
    entry=Path(__file__).with_name('_zk_linux_entry.py').resolve()
    command[0]=str(Path(command[0]).resolve(strict=True))
    files={str(Path(p).resolve(strict=True)) for p in read_only}
    files.update((str(entry),str(Path(command[0]).resolve(strict=True))))
    spec=dict(bwrap=str(Path(bwrap).resolve()),base=str(Path(base).resolve(strict=True)),command=command,
        strict=not loopback and not cuda,broker_socket=str(broker.endpoint) if broker else None,
        read_only=sorted(files),lock_fd=lock_fd,env={k:str(v) for k,v in env.items()})
    if cuda:spec["cuda"]=True
    return [sys.executable,str(entry),'outside',json.dumps(spec)],env


def probe_code(loopback):
    if not loopback:
        return """import errno,socket
for family,target in [(socket.AF_INET,('127.0.0.1',9)),(socket.AF_INET6,('::1',9))]:
 try: client=socket.socket(family,socket.SOCK_STREAM)
 except OSError as e:
  if e.errno not in (errno.EPERM,errno.EACCES,errno.EAFNOSUPPORT): raise
  continue
 with client:
  try: client.connect(target)
  except OSError as e:
   if e.errno not in (errno.EPERM,errno.EACCES): raise
  else: raise AssertionError('IP connection allowed')
print('Strict IP network denial verified')
"""
    return """import errno,socket
with socket.socket() as server:
 server.bind(('127.0.0.1',0));server.listen(1)
 with socket.create_connection(server.getsockname(),timeout=2):
  accepted,_=server.accept();accepted.close()
for family,target in [(socket.AF_INET,('192.0.2.1',9)),(socket.AF_INET6,('2001:db8::1',9))]:
 try: client=socket.socket(family,socket.SOCK_STREAM)
 except OSError as e:
  if family!=socket.AF_INET6 or e.errno!=errno.EAFNOSUPPORT: raise
  continue
 with client:
  client.settimeout(2)
  try: client.connect(target)
  except OSError as e:
   if e.errno not in (errno.EPERM,errno.EACCES,errno.ENETUNREACH,errno.EHOSTUNREACH): raise
  else: raise AssertionError('External network allowed')
print('Loopback RPC works; external IPv4/IPv6 unavailable')
"""
