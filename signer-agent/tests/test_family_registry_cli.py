"""Preparation files may contain local RPC credentials; summaries must not."""
import json
from types import SimpleNamespace as NS
from web3 import Web3
from signer_agent import family_registry_cli as cli
from test_family_registry_policy import family_policy,registered_env,delivery


def test_enrollment_profile_is_private_and_stdout_has_no_rpc_credentials(family_policy,tmp_path,monkeypatch,capsys):
    f=family_policy
    class W3:
        HTTPProvider=staticmethod(lambda *args,**kwargs:None)
        to_checksum_address=staticmethod(Web3.to_checksum_address)
        def __new__(cls,*args,**kwargs):return f.e.w3
    monkeypatch.setattr(cli,'Web3',W3)
    monkeypatch.setattr(cli,'origin',lambda *args:(NS(number=20),dict(onboarding=dict(invitation=dict(evm_confirmations='1'))),'fingerprint'))
    monkeypatch.setattr(cli,'Catalog',lambda **kwargs:NS())
    monkeypatch.setattr(cli.policy,'load_pin',lambda *args:f.p)
    private=dict(deployment_id='root-enroll-test',evm_rpc='https://fixture-user:fixture-rpc-secret@localhost/')
    monkeypatch.setattr(cli.policy,'enrollment_plan',lambda *args:private)
    policy=tmp_path/'root.json';policy.write_text(json.dumps(f.p));output=tmp_path/'profile.json'
    assert cli.main(['enroll-prepare','--rpc','local','--board',f.e.registration_board.address,'--policy',str(policy),
        '--identity-file','unused-fixture-key','--registry',f.e.reg.address,'--deadline','2000','--output',str(output)])==0
    summary=capsys.readouterr().out
    assert 'fixture-rpc-secret' not in summary and 'evm_rpc' not in summary
    assert json.loads(summary)['deployment_id']==private['deployment_id']
    assert json.loads(output.read_text())==private and output.stat().st_mode & 0o777==0o600
