"""Platform pins and namespace policy; real Linux execution is recorded separately."""
import json
from pathlib import Path
import socket
from types import SimpleNamespace
import pytest
from signer_agent import zk_isolation as isolation,zk_prover_platform as platform


def test_linux_uses_its_own_immutable_tools_and_sdk_image(monkeypatch,tmp_path):
    monkeypatch.setattr(platform.sys,'platform','linux');monkeypatch.setattr(platform.platform,'machine',lambda:'x86_64')
    p=platform.profile();assert p['docker_arch']=='amd64' and p['qualified'] is False
    assert p['tools']['r0vm']=='2acdf550f9aca4d05b74072463e4ed307c3824309c754a5cb5f93aa84f3390e6'
    changed=tmp_path/'r0vm';changed.write_bytes(b'not the pinned Linux runtime');changed.chmod(0o755)
    monkeypatch.setenv('SATOHSI_R0VM',str(changed))
    with pytest.raises(ValueError,match='TOOL_DRIFT'):platform.tool('r0vm')


def test_remote_docker_is_never_a_local_proving_backend(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST','tcp://remote.invalid:2375')
    monkeypatch.delenv('SATOHSI_DOCKER_SOCKET',raising=False)
    monkeypatch.setattr(isolation.Path,'is_file',lambda p:True)
    monkeypatch.setattr(isolation.os,'access',lambda *a:True)
    with pytest.raises(ValueError,match='LOCAL_DOCKER_SOCKET_REQUIRED'):isolation.docker_endpoint()


def test_missing_bubblewrap_cannot_fall_back_to_a_networked_process(monkeypatch,tmp_path):
    monkeypatch.setattr(isolation.sys,'platform','linux');monkeypatch.setattr(isolation.shutil,'which',lambda n:None)
    with pytest.raises(ValueError,match='BWRAP_REQUIRED'):isolation.launch(['/bin/true'],tmp_path,isolation.environment())


def test_linux_launch_carries_only_reviewed_mounts_and_worker_lock(monkeypatch,tmp_path):
    monkeypatch.setattr(isolation.sys,'platform','linux');monkeypatch.setattr(isolation.shutil,'which',lambda n:'/usr/bin/bwrap')
    fake=tmp_path/'tool';fake.write_text('test executable')
    command,env=isolation.launch([str(fake),'argument'],tmp_path,isolation.environment(),lock_fd=19)
    spec=json.loads(command[-1])
    assert spec['strict'] is True and spec['lock_fd']==19 and spec['broker_socket'] is None
    assert spec['read_only']==sorted([str(fake),str(Path(isolation.__file__).with_name('_zk_linux_entry.py').resolve())])
    assert env['RISC0_DEV_MODE']=='0' and not any(k in env for k in ('HOME','HTTP_PROXY','AWS_SECRET_ACCESS_KEY'))


def test_linux_broker_does_not_admit_daemon_inside_a_shared_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation.sys, 'platform', 'linux')
    monkeypatch.setattr(isolation.shutil, 'which', lambda n: '/usr/bin/bwrap')
    base = tmp_path / 'attempt'
    base.mkdir()
    tool = tmp_path / 'tool'
    tool.write_text('test executable')
    broker = SimpleNamespace(daemon=base / 'daemon.sock', private=tmp_path / 'private',
                             endpoint=tmp_path / 'broker.sock')
    with pytest.raises(ValueError, match='ZK_GROTH16_BROKER_CONFIG'):
        isolation.launch([tool], base, isolation.environment(), broker=broker)
    broker.daemon = tmp_path / 'daemon.sock'
    command, _ = isolation.launch([tool], base, isolation.environment(), broker=broker)
    spec = json.loads(command[-1])
    assert spec['broker_socket'] == str(broker.endpoint)
    assert str(broker.daemon) not in command[-1] and str(broker.private) not in command[-1]


def test_same_mac_profile_retains_strict_network_denial(monkeypatch,tmp_path):
    monkeypatch.setattr(isolation.sys,'platform','darwin')
    monkeypatch.setattr(isolation,'macos_python_libraries',lambda:())
    command,env=isolation.launch([isolation.sys.executable],tmp_path,isolation.environment())
    assert command[:2]==['/usr/bin/sandbox-exec','-p']
    assert command[2].startswith(isolation.STRICT)
    assert '(deny file-read-data' in command[2] and '(deny file-write*' in command[2]
    assert env['TMPDIR']==str(tmp_path.resolve())


def test_installer_checks_original_rust_and_dependency_locks():
    from ops.zkspv.linux_tools import checked_sources
    source=checked_sources()
    assert 'rust/zkspv/groth16-integration/Cargo.lock' in source
    assert 'rust/zkspv/groth16-integration/src/bin/wrap_groth16.rs' in source


def test_namespace_launcher_has_no_host_network_or_home_mount(monkeypatch,tmp_path):
    from signer_agent import _zk_linux_entry as entry
    binary=tmp_path/'tool';binary.write_bytes(b'public tool')
    calls=[];monkeypatch.setattr(entry.os,'execv',lambda binary,args:calls.append(args))
    readlink=entry.os.readlink
    monkeypatch.setattr(entry.os,'readlink',lambda p:'net:[host]' if str(p)=='/proc/self/ns/net' else readlink(p))
    spec=dict(bwrap='/usr/bin/bwrap',base=str(tmp_path),command=[str(binary)],read_only=[str(binary)],
        env={'PATH':'/usr/bin:/bin','RISC0_DEV_MODE':'0'},strict=False,lock_fd=None,broker_socket=None)
    entry.outside(spec);args=calls[0]
    assert '--unshare-net' in args and '--unshare-pid' in args and '--clearenv' in args
    assert '--share-net' not in args and '--preserve-fds' not in args
    assert all(args[i+1]!='/' for i,v in enumerate(args) if v in ('--bind','--ro-bind'))
    assert '--cap-drop' in args and args[args.index('--cap-drop')+1]=='ALL'
    assert json.loads(args[-1])['parent_netns']=='net:[host]'


def test_unchanged_namespace_stops_before_any_workload(monkeypatch):
    from signer_agent import _zk_linux_entry as entry
    monkeypatch.setattr(entry.os,'readlink',lambda p:'net:[host]')
    monkeypatch.setattr(entry,'filter_network',lambda *a:pytest.fail('must not execute'))
    with pytest.raises(ValueError,match='NETWORK_NAMESPACE_REQUIRED'):entry.inside({'parent_netns':'net:[host]'})


def test_linux_seccomp_load_failure_is_fatal(monkeypatch):
    from signer_agent import _zk_linux_entry as entry
    class Function:
        def __init__(self,value):self.value=value
        def __call__(self,*a):return self.value
    library=SimpleNamespace(seccomp_init=Function(1),seccomp_syscall_resolve_name=Function(10),
        seccomp_rule_add=Function(0),seccomp_load=Function(-1),seccomp_release=Function(None))
    monkeypatch.setattr(entry.ctypes,'CDLL',lambda *a,**k:library)
    with pytest.raises(ValueError,match='SECCOMP_REQUIRED'):entry.filter_network(True)


def test_installer_checks_disk_before_creating_a_builder(monkeypatch,tmp_path):
    from ops.zkspv import linux_tools
    monkeypatch.setattr(linux_tools.shutil,'disk_usage',lambda p:SimpleNamespace(free=1024))
    monkeypatch.setattr(isolation,'docker_endpoint',lambda:pytest.fail('must not start Docker before disk check'))
    with pytest.raises(ValueError,match='DISK_BUDGET'):linux_tools.install(tmp_path/'new-tools')
    assert not (tmp_path/'new-tools').exists()


@pytest.mark.parametrize('mutation',[None,'image','journal','dev_mode','seal'])
def test_receipt_verification_uses_linux_pins_and_still_checks_claim(monkeypatch,tmp_path,mutation):
    from signer_agent import zk_worker_prover as worker
    from signer_agent.zk_spv import MAINNET_IMAGE, VERIFIER_SELECTOR
    root=tmp_path/'repo';base=tmp_path/'attempt';base.mkdir()
    program=root/'rust/zkspv/guest-integration/evidence/mainnet-transition.r0bf'
    program.parent.mkdir(parents=True);program.write_bytes(b'common guest')
    (program.parent.parent/'report.json').write_text(json.dumps(dict(
        tools={'r0vm':{'sha256':'historical Darwin r0vm'}},evidence_sha256={program.name:worker.sha(program)})))
    report=root/'rust/zkspv/groth16-integration/wrapper-build-report.json';report.parent.mkdir()
    report.write_text(json.dumps({'bridge_sha256':'historical Darwin bridge'}))
    tools={}
    for name in ('r0vm','satoshifi-receipt-bridge'):
        path=tmp_path/name;path.write_bytes(('Linux '+name).encode());path.chmod(0o755);tools[name]=path
    monkeypatch.setattr(worker,'ROOT',root)
    from signer_agent import zk_profiles
    monkeypatch.setattr(zk_profiles,'ROOT',root)
    monkeypatch.setattr(platform,'release_tool',lambda name: tools[name])
    (base/'state.journal').write_bytes(b'expected journal');receipt=base/'receipt.bin';receipt.write_bytes(b'receipt')
    monkeypatch.setattr(isolation,'launch',lambda command,*a,**k:(command,{}))
    def inspect(command,**kwargs):
        assert command[0]==tools['satoshifi-receipt-bridge'] and command[2]==program
        result=command[-1];result.mkdir()
        verified=dict(status='VERIFIED',receipt_kind='groth16',image_id=MAINNET_IMAGE,dev_mode_disabled=True,
            journal_sha256=worker.sha(base/'state.journal'),receipt_sha256=worker.sha(receipt))
        if mutation=='image':verified['image_id']='different image'
        if mutation=='journal':verified['journal_sha256']='different journal'
        if mutation=='dev_mode':verified['dev_mode_disabled']=False
        (result/'receipt.json').write_text(json.dumps(verified))
        (result/'seal.bin').write_bytes(VERIFIER_SELECTOR+bytes(255 if mutation=='seal' else 256))
    monkeypatch.setattr(worker.subprocess,'run',inspect)
    prover=worker.LocalProver({})
    if mutation:
        with pytest.raises(ValueError,match='SEAL_LENGTH' if mutation=='seal' else 'RECEIPT_NOT_VERIFIED'):
            prover.verify(base,receipt,base/'verified')
    else:assert prover.verify(base,receipt,base/'verified')==VERIFIER_SELECTOR+bytes(256)
