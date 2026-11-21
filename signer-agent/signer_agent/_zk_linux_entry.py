"""Internal Linux namespace entry; receives fixed policy, never arbitrary flags."""
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys


def filter_network(strict):
    try:lib=ctypes.CDLL('libseccomp.so.2',use_errno=True)
    except OSError as error:raise ValueError('ZK_LINUX_SECCOMP_REQUIRED') from error
    lib.seccomp_init.argtypes=[ctypes.c_uint32];lib.seccomp_init.restype=ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p];lib.seccomp_syscall_resolve_name.restype=ctypes.c_int
    lib.seccomp_rule_add.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int,ctypes.c_uint]
    lib.seccomp_load.argtypes=[ctypes.c_void_p];lib.seccomp_release.argtypes=[ctypes.c_void_p]
    ctx=lib.seccomp_init(0x7fff0000)
    if not ctx:raise ValueError('ZK_LINUX_SECCOMP_REQUIRED')
    try:
        denied=['setns','unshare','mount','ptrace','io_uring_setup']
        if strict:denied+=['socket','socketpair']
        for name in denied:
            number=lib.seccomp_syscall_resolve_name(name.encode())
            if number>=0 and lib.seccomp_rule_add(ctx,0x00050000|errno.EPERM,number,0)!=0:
                raise ValueError('ZK_LINUX_SECCOMP_REQUIRED')
        if lib.seccomp_load(ctx)!=0:raise ValueError('ZK_LINUX_SECCOMP_REQUIRED')
    finally:lib.seccomp_release(ctx)


def inside(spec):
    # Namespace failure is fatal before any workload or socket probe runs.
    if os.readlink('/proc/self/ns/net')==spec['parent_netns']:raise ValueError('ZK_LINUX_NETWORK_NAMESPACE_REQUIRED')
    # Some kernels create DOWN tunnel placeholders in every new namespace.
    # Only loopback may be UP; no workload has CAP_NET_ADMIN to activate others.
    names={name for _,name in socket.if_nameindex()}
    if 'lo' not in names:raise ValueError('ZK_LINUX_NETWORK_NAMESPACE_REQUIRED')
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as control:
        for name in names:
            flags=struct.unpack_from('H',fcntl.ioctl(control.fileno(),0x8913,struct.pack('256s',name.encode())),16)[0]
            if bool(flags&1)!=(name=='lo'):raise ValueError('ZK_LINUX_NETWORK_NAMESPACE_REQUIRED')
    filter_network(spec['strict'])
    os.execv(spec['command'][0],spec['command'])


def outside(spec):
    if spec.get("cuda") and spec.get("broker_socket"):
        raise ValueError("ZK_CUDA_DOCKER_SOCKET_FORBIDDEN")
    base=Path(spec['base']).resolve(strict=True)
    args=[spec['bwrap'],'--unshare-user','--unshare-pid','--unshare-net','--unshare-ipc','--unshare-uts',
        '--cap-drop','ALL','--new-session','--die-with-parent','--clearenv',
        '--ro-bind','/usr','/usr','--proc','/proc','--dev','/dev','--tmpfs','/tmp',
        '--dir','/home/prover','--dir','/run','--dir','/var']
    if spec.get("cuda"):
        devices = [path for path in Path("/dev").glob("nvidia*")
                   if re.fullmatch(r"nvidia(?:[0-9]+|ctl|\-uvm|\-uvm\-tools)", path.name)
                   and stat.S_ISCHR(path.lstat().st_mode)]
        if Path("/dev/dxg").exists() and stat.S_ISCHR(Path("/dev/dxg").lstat().st_mode):
            devices.append(Path("/dev/dxg"))
        if not any(path.name == "dxg" or re.fullmatch(r"nvidia[0-9]+", path.name) for path in devices):
            raise ValueError("ZK_CUDA_DEVICE_REQUIRED")
        for path in sorted(devices):
            args += ["--dev-bind", str(path), str(path)]
    for name in ('lib','lib64','bin','sbin'):
        path=Path('/'+name)
        if path.is_symlink():args+=['--symlink',os.readlink(path),str(path)]
        elif path.is_dir():args+=['--ro-bind',str(path),str(path)]
    for name in ('/etc/ld.so.cache','/etc/localtime','/etc/nsswitch.conf','/etc/passwd','/etc/group'):
        if Path(name).exists():args+=['--ro-bind',name,name]
    prefix=Path(sys.base_prefix).resolve()
    if prefix!=Path('/usr'):
        if prefix in (Path('/'),Path('/home'),Path.home()):raise ValueError('ZK_LINUX_PYTHON_PREFIX')
        args+=['--ro-bind',str(prefix),str(prefix)]
    args+=['--bind',str(base),str(base),'--chdir',str(base)]
    for name in spec['read_only']:
        path=Path(name).resolve(strict=True)
        if not path.is_file():raise ValueError('ZK_LINUX_READ_ONLY_FILE')
        args+=['--ro-bind',str(path),str(path)]
    if spec.get('broker_socket'):
        path=Path(spec['broker_socket']).resolve(strict=True)
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise ValueError("ZK_GROTH16_BROKER_CONFIG")
        args+=['--ro-bind',str(path),str(path)]
    for key,value in dict(spec['env'],TMPDIR='/tmp').items():args+=['--setenv',key,value]
    if spec.get('lock_fd') is not None:
        descriptor=spec['lock_fd'];os.fstat(descriptor)
        # bwrap keeps a sync fd open for the lifetime of the sandbox. It need
        # not expose that descriptor to the untrusted/native child itself.
        os.set_inheritable(descriptor,True);args+=['--sync-fd',str(descriptor)]
    spec['parent_netns']=os.readlink('/proc/self/ns/net')
    args += ['--',str(Path(sys.executable).resolve()),str(Path(__file__).resolve()),'inside',json.dumps(spec)]
    os.execv(spec['bwrap'],args)


if __name__=='__main__':
    mode=sys.argv[1];spec=json.loads(sys.argv[2])
    if mode=='outside':outside(spec)
    elif mode=='inside':inside(spec)
    else:raise ValueError('ZK_LINUX_ENTRY_MODE')
