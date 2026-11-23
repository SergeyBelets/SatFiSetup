"""ULR issuance is durable while manual distribution waits for operator review."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
from eth_account import Account
import pytest
from signer_agent.bootstrap import Bootstrap
from signer_agent import bootstrap_distribution as d
from test_bootstrap_parallel import fixture
from test_bootstrap_ulr import proposal,profile,build


def setup(tmp_path,monkeypatch):
    b,identity=fixture(tmp_path,1);b.w3.eth.block_number=100
    operator=Account.from_key((1).to_bytes(32,'big')).address
    step=dict(id='ulr_distribution',kind=d.KIND,token='dao_token',staking='staking',participants=[operator])
    b.profile.update(operator=operator,evm_confirmations='1',steps=[step,*b.profile['steps']])
    view=dict(fingerprint='0x'+'11'*32,transfers_confirmed=True,participants=[],
        anchor=dict(number='100',hash=b.w3.eth.get_block(100).hash.hex()))
    monkeypatch.setattr(d,'snapshot',lambda *a:deepcopy(view))
    return NS(b=b,identity=identity,view=view,step=step)


def release(e,**kw):return e.b.apply(e.identity,ulr_distribution_complete=True,accept_ulr_distribution=e.view['fingerprint'],**kw)


def test_pause_survives_restart_verify_and_does_not_sign_following_steps(tmp_path,monkeypatch):
    e=setup(tmp_path,monkeypatch)
    assert e.b.apply(e.identity)['status']==d.STATUS
    assert e.b.w3.eth.sent==e.b.prepared==[]
    other=Bootstrap.__new__(Bootstrap);other.__dict__.update(e.b.__dict__)
    assert other.apply(e.identity)['status']==d.STATUS
    assert other.apply(verify=True)['status']==d.STATUS
    assert release(e)['status']=='VERIFIED'
    assert len(e.b.w3.eth.sent)==1
    assert release(e)['status']=='VERIFIED' and len(e.b.w3.eth.sent)==1
    # Later ordinary owner transfers do not invalidate historical approval.
    monkeypatch.setattr(d,'snapshot',lambda *a:pytest.fail('released distribution re-evaluated'))
    assert e.b.apply(e.identity)['status']=='VERIFIED'


def test_wrong_operator_early_confirmation_and_missing_fingerprint_are_rejected(tmp_path,monkeypatch):
    e=setup(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='ULR_DISTRIBUTION_NOT_PAUSED'):release(e)
    assert e.b.w3.eth.sent==[]
    e.b.apply(e.identity)
    with pytest.raises(ValueError,match='ULR_DISTRIBUTION_FINGERPRINT_REQUIRED'):
        e.b.apply(e.identity,ulr_distribution_complete=True)
    other=tmp_path/'other.json';other.write_text(json.dumps(dict(private_key=f'{2:064x}')));other.chmod(0o600)
    with pytest.raises(ValueError,match='ULR_DISTRIBUTION_OPERATOR_REQUIRED'):
        e.b.apply(other,ulr_distribution_complete=True,accept_ulr_distribution=e.view['fingerprint'])
    assert e.b.w3.eth.sent==[]


def test_stale_distribution_and_pending_transfers_cannot_release(tmp_path,monkeypatch):
    e=setup(tmp_path,monkeypatch);e.b.apply(e.identity);old=e.view['fingerprint']
    e.view['fingerprint']='0x'+'22'*32
    with pytest.raises(ValueError,match='ULR_DISTRIBUTION_CHANGED'):
        e.b.apply(e.identity,ulr_distribution_complete=True,accept_ulr_distribution=old)
    e.view['transfers_confirmed']=False
    assert release(e)['status']==d.STATUS and e.b.w3.eth.sent==[]
    e.view['transfers_confirmed']=True
    assert release(e)['status']=='VERIFIED'


def test_confirmed_distribution_anchor_cannot_survive_reorg(tmp_path,monkeypatch):
    e=setup(tmp_path,monkeypatch);e.b.apply(e.identity);release(e)
    e.b.w3.eth.block_number=101
    original=e.b.w3.eth.get_block
    e.b.w3.eth.get_block=lambda n:NS(number=100,hash=b'x'*32) if n==100 else original(n)
    with pytest.raises(ValueError,match='EVM_REORG_RECOVERY_REQUIRED'):e.b.apply(verify=True)
    assert len(e.b.w3.eth.sent)==1


def test_distribution_waits_for_required_confirmations_without_resetting_journal(tmp_path, monkeypatch):
    e = setup(tmp_path, monkeypatch)
    e.b.apply(e.identity)
    assert release(e)["status"] == "VERIFIED"
    row_id = e.b.prefix + ":ulr_distribution"
    before = e.b.journal.get(row_id)
    e.b._evm_confirmation_floor = 3
    e.b.w3.eth.block_number = 101
    with pytest.raises(ValueError, match="^EVM_RPC_BEHIND$"):
        e.b.apply(e.identity)
    assert e.b.journal.get(row_id) == before
    assert len(e.b.w3.eth.sent) == 1
    e.b.w3.eth.block_number = 102
    assert e.b.apply(e.identity)["status"] == "VERIFIED"
    assert len(e.b.w3.eth.sent) == 1


@pytest.mark.parametrize('mode',['create','reuse'])
@pytest.mark.parametrize('backend,backed',[('relay',False),('relay',True),('zk-state-v2',True)])
def test_generated_pause_is_after_token_staking_before_ulr_authorizations(mode,backend,backed):
    plan=build(profile(proposal(mode,backend,backed),board=True));steps=plan['steps']
    ids=[s['id'] for s in steps];pause=ids.index('ulr_distribution')
    assert steps[pause]['kind']==d.KIND
    assert pause<ids.index('enroll_ulr_treasury_intent')<ids.index('enroll_ulr_treasury_open')
    assert pause<ids.index('bind_ulr_candidates_open')
    assert (ids.index('staking') if mode=='create' else ids.index('shared_ulr_bindings'))<pause
    if mode=='create':assert ids.index('dao_token')<pause
    else:assert 'dao_token' not in ids
