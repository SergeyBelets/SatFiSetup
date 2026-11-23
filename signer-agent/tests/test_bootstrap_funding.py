"""Funding checkpoints cannot be released by polling, restart, or another member."""
import json
from types import SimpleNamespace as NS
import pytest
from eth_account import Account
from signer_agent.bootstrap import Bootstrap
from test_bootstrap_parallel import fixture


def setup(tmp_path):
    b,identity=fixture(tmp_path,1)
    members=[Account.from_key(i.to_bytes(32,'big')).address for i in (1,2,3)]
    b.profile.update(operator=members[0],evm_ceremony=dict(participants=members),evm_confirmations='1')
    balances={m:0 for m in members};pending={m:100 for m in members}
    b.w3.eth.get_balance=lambda a,block_identifier:pending[a] if block_identifier=='pending' else balances[a]
    return b,identity,members,balances,pending


def arm(b,identity):return b.apply(identity,pause_for_funding=True,funding_min_wei='10',funding_operator_min_wei='20')


def test_pause_survives_restart_and_funded_wallets_need_explicit_release(tmp_path):
    b,identity,members,balances,pending=setup(tmp_path)
    assert arm(b,identity)['status']=='PAUSED_FOR_FUNDING'
    assert b.w3.eth.sent==[] and b.prepared==[]
    for m in members:balances[m]=100
    # New executor object, same durable journal, simulates normal process restart.
    restarted=Bootstrap.__new__(Bootstrap);restarted.__dict__.update(b.__dict__)
    assert restarted.apply(identity)['status']=='PAUSED_FOR_FUNDING'
    assert restarted.apply(verify=True)['status']=='PAUSED_FOR_FUNDING'
    assert b.w3.eth.sent==[]
    result=restarted.apply(identity,funding_complete=True)
    assert result['status']=='VERIFIED'
    assert len(b.w3.eth.sent)==1
    assert restarted.apply(identity)['status']=='VERIFIED'
    assert len(b.w3.eth.sent)==1


def test_release_waits_for_every_confirmed_and_pending_balance(tmp_path):
    b,identity,members,balances,pending=setup(tmp_path);arm(b,identity)
    for m in members:balances[m]=100
    balances[members[2]]=0 # Funding visible only in pending state is insufficient.
    assert not b.apply(identity,funding_complete=True)['funding']['ready']
    balances[members[2]]=100;pending[members[1]]=0 # Reserved/spent pending gas is insufficient.
    assert not b.apply(identity,funding_complete=True)['funding']['ready']
    assert b.w3.eth.sent==[]
    pending[members[1]]=100
    assert b.apply(identity,funding_complete=True)['status']=='VERIFIED'


def test_only_operator_can_release_and_policy_cannot_change(tmp_path):
    b,identity,members,balances,pending=setup(tmp_path);arm(b,identity)
    other=tmp_path/'other.json';other.write_text(json.dumps(dict(private_key=f'{2:064x}')));other.chmod(0o600)
    with pytest.raises(ValueError,match='BOOTSTRAP_FUNDING_OPERATOR_REQUIRED'):b.apply(other,funding_complete=True)
    with pytest.raises(ValueError,match='BOOTSTRAP_FUNDING_POLICY_MISMATCH'):
        b.apply(identity,pause_for_funding=True,funding_min_wei='1')
    assert b.w3.eth.sent==[]


@pytest.mark.parametrize('value',[None,'0','-1','1.0','01',str(2**256)])
def test_minimum_is_explicit_positive_base_units(tmp_path,value):
    b,identity,*_=setup(tmp_path)
    with pytest.raises(ValueError,match='BOOTSTRAP_FUNDING_MINIMUM_REQUIRED'):
        b.apply(identity,pause_for_funding=True,funding_min_wei=value)
    assert b.w3.eth.sent==[]


def test_unarmed_release_and_late_pause_are_rejected(tmp_path):
    b,identity,*_=setup(tmp_path)
    with pytest.raises(ValueError,match='BOOTSTRAP_FUNDING_PAUSE_REQUIRED'):b.apply(identity,funding_complete=True)
    b.w3.eth.block_number=100
    assert b.apply(identity)['status']=='VERIFIED'
    with pytest.raises(ValueError,match='BOOTSTRAP_FUNDING_TOO_LATE'):arm(b,identity)


def test_funding_reorg_is_fatal_before_any_send(tmp_path):
    b,identity,*_=setup(tmp_path)
    original=b.w3.eth.get_block;calls=[]
    def get(number):
        result=original(number);calls.append(number)
        if len(calls)>1:return NS(number=result.number,hash=b'x'*32)
        return result
    b.w3.eth.get_block=get
    with pytest.raises(ValueError,match='EVM_SIGNING_SNAPSHOT_REORG'):arm(b,identity)
    assert b.w3.eth.sent==[]


def test_operator_confirmation_floor_is_used_for_funding_snapshot(tmp_path):
    b, identity, *_ = setup(tmp_path)
    b.w3.eth.block_number = 100
    b._evm_confirmation_floor = 5
    result = arm(b, identity)
    assert result["status"] == "PAUSED_FOR_FUNDING"
    assert result["funding"]["anchor"]["number"] == "96"
    assert not b.w3.eth.sent


def test_flow_arms_once_then_polls_without_rearming(monkeypatch):
    from signer_agent import flow_cli
    calls=[]
    def invoke(group,args):
        calls.append(args)
        return dict(status='PAUSED_FOR_FUNDING' if len(calls)==1 else 'VERIFIED')
    monkeypatch.setattr(flow_cli,'invoke',invoke);monkeypatch.setattr(flow_cli.time,'sleep',lambda _:None)
    assert flow_cli.main(['bootstrap','--profile','p','--state','s','--identity-file','i',
        '--pause-for-funding','--funding-min-wei','10'])==0
    assert '--pause-for-funding' in calls[0] and '--funding-min-wei' in calls[0]
    assert '--pause-for-funding' not in calls[1] and '--funding-min-wei' not in calls[1]


@pytest.mark.parametrize('operator_minimum',['','0','9','01'])
def test_explicit_operator_floor_cannot_silently_fall_back_or_drop_below_participants(tmp_path,operator_minimum):
    b,identity,*_=setup(tmp_path)
    with pytest.raises(ValueError,match='BOOTSTRAP_FUNDING_MINIMUM_REQUIRED'):
        b.apply(identity,pause_for_funding=True,funding_min_wei='10',funding_operator_min_wei=operator_minimum)
    assert b.w3.eth.sent==[]
