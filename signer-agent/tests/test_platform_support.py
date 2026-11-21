import json
from types import SimpleNamespace
import pytest
from signer_agent import platform_support as p
from ops import bootstrap_host as host


@pytest.mark.parametrize('system,release,machine,wsl,prover',[
    ('Linux','6.8.0','x86_64',False,False),
    ('Linux','6.6.87.2-microsoft-standard-WSL2','x86_64',True,False),
    ('Darwin','25.0','arm64',False,True),
    ('Darwin','24.0','x86_64',False,False),
])
def test_posix_signers_share_protocol_but_not_prover_qualification(system,release,machine,wsl,prover):
    c=p.capabilities(system,release,machine)
    assert c['signer'] and c['bootstrap'] and c['relay']
    assert c['wsl'] is wsl and c['qualified_local_prover'] is prover


def test_windows_native_reports_wsl_before_importing_posix_signer(monkeypatch,capsys):
    c=p.capabilities('Windows','11','AMD64')
    monkeypatch.setattr(p,'capabilities',lambda:c)
    assert p.entrypoint()==2
    assert json.loads(capsys.readouterr().out)['code']=='PLATFORM_USE_WSL2'


def test_build_refuses_global_python_before_installing_anything(monkeypatch):
    monkeypatch.setattr(host.sys,'prefix',host.sys.base_prefix)
    monkeypatch.setattr(host.subprocess,'run',lambda *a,**k:pytest.fail('must not install globally'))
    with pytest.raises(ValueError,match='VENV_REQUIRED'):host.build()


def test_wsl_rejects_windows_mounted_private_state(monkeypatch):
    monkeypatch.setattr(host,'capabilities',lambda:p.capabilities('Linux','microsoft-WSL2','x86_64'))
    monkeypatch.setattr(host.shutil,'which',lambda _:None)
    result=host.check(state_dir='/mnt/c/satoshifi-state')
    assert 'state:use WSL Linux filesystem, not /mnt/<drive>' in result['missing']


def test_non_executable_frost_build_is_not_ready(tmp_path, monkeypatch):
    binary=tmp_path/'frost';binary.write_text('not executable');binary.chmod(0o600)
    monkeypatch.setenv('SATOHSI_CRYPTO_BIN',str(binary))
    monkeypatch.setattr(host.shutil,'which',lambda _:None)
    assert 'build:native FROST' in host.check()['missing']


def test_private_regular_file_cannot_be_a_state_directory(tmp_path,monkeypatch):
    state=tmp_path/'state';state.touch();state.chmod(0o700)
    monkeypatch.setattr(host.shutil,'which',lambda _:None)
    assert 'state:owned directory mode 0700 required' in host.check(state_dir=state)['missing']


def test_pinned_rust_is_checked_without_installing_it(monkeypatch):
    commands=[]
    monkeypatch.setattr(host.shutil,'which',lambda n:'/fixture/'+n)
    def local_version(command):
        commands.append(command)
        return None if 'run' in command else 'forge Version: 1.8.5'
    monkeypatch.setattr(host,'version',local_version)
    assert 'toolchain:Rust 1.89.0 required' in host.check()['missing']
    assert ['/fixture/rustup','run','1.89.0','rustc','--version'] in commands


def test_unusable_version_command_does_not_crash_preflight(monkeypatch):
    monkeypatch.setattr(host.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='',stderr=''))
    assert host.version(['broken-tool','--version']) is None


def test_zk_participant_cannot_join_with_verify_only_linux_capability(monkeypatch):
    from test_zk_bootstrap import zk_invite
    original=p.capabilities
    monkeypatch.setattr(p,'capabilities',lambda:original('Linux','6.8','aarch64'))
    with pytest.raises(ValueError,match='PARTICIPANT_LOCAL_PROVER_REQUIRED'):p.require_participant_prover(zk_invite())


@pytest.mark.parametrize('release',['6.8','6.6.87-microsoft-standard-WSL2'])
def test_linux_x86_and_wsl_accept_the_local_prover_implementation(monkeypatch,release):
    from test_zk_bootstrap import zk_invite
    original=p.capabilities;c=original('Linux',release,'x86_64')
    assert c['local_prover_supported'] and not c['qualified_local_prover']
    monkeypatch.setattr(p,'capabilities',lambda:c)
    p.require_participant_prover(zk_invite())


def test_relay_participant_does_not_require_zk_prover(monkeypatch):
    monkeypatch.setattr(p,'capabilities',lambda:{'qualified_local_prover':False})
    p.require_participant_prover({'bitcoin_policy':{'backend':'relay'}})
