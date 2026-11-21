"""Original SDKs, HPKE and native Bitcoin codec; explicitly synthetic RPC effects."""
from copy import deepcopy
from types import SimpleNamespace as NS
from pathlib import Path
import json
import pytest
from ops.zkspv import contour_address as address_runner,contour_withdrawal as withdrawal,contour_checks as checks
from ops.zkspv.contour import digest
from signer_agent.bitcoin_input_keys import build
from signer_agent.zk_worker_prover import write
from signer_agent.address_transport import Client
from test_address_transport import delivery
from test_recipient_profiles import matrix
from test_address_mint import address,Functions
from test_contour_withdrawal import selected,personal,signing,native_keys
from test_withdrawal_user import workflow


@pytest.mark.parametrize('mode',['ordinary','wallet-v1','advanced-contract'])
def test_runner_requests_use_real_original_recipient_confirmation_hpke_ack_and_dedup(matrix,monkeypatch,mode):
    e=matrix;choice=e.preview(mode,address(88) if mode=='ordinary' else None);q=choice['selection']
    intent=e.requester.make_intent(None,selection=q,accepted=choice['confirmation_hash'])
    r=address_runner.Runtime.__new__(address_runner.Runtime);r.actors=dict(requester=e.requester,service=e.service)
    r.body=dict(intent=intent.encode().hex(),recipient_selection=q,accepted_recipient_sha256=choice['confirmation_hash']);r.intent=intent
    monkeypatch.setattr(Client,'transmit',lambda self,**kw:e.publish(self))
    first=r.request();assert first['status']=='ADDRESS_ACK_CONFIRMED'
    assert r.request()==first and len(e.service.store.intents())==1
    assert not e.choices  # The opening remains private until real registration.
    e.requester.make_intent=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('never reconstruct a queued request'))
    assert r.request()['route']==first['route']
    r.body['recipient_selection']=dict(q,recipient=address(99))
    with pytest.raises(ValueError,match='RECIPIENT'):r.request()


def replacement(e):
    oldfile=e.tmp_path/'initial.json';write(oldfile,e.c)
    c=deepcopy(e.c);c.update(version='2',previous_config=str(oldfile),accepted_previous_sha256=digest(e.c),payment_file=str(e.tmp_path/'replacement-payment.json'))
    payment=deepcopy(e.payment);plan=payment['plan'];plan['withdrawal_fee']['previous']=e.plan['batch_id'];plan['batch_id']='fe'*32
    plan['fee']=str(int(plan['fee'])+1);plan['outputs'][-1]['amount']=str(int(plan['outputs'][-1]['amount'])-1)
    payment['request']['metadata']['bitcoin_plan']=plan;write(c['payment_file'],payment)
    need=dict(e.need,batch=plan['batch_id'],txid=build(plan)['txid'])
    return c,payment,need


def test_replacement_runner_pins_original_quote_inputs_principal_and_separate_version(selected):
    e=selected;c,payment,need=replacement(e)
    result=withdrawal.inputs(c,e.wc,need)
    assert result[5]['plan']['withdrawal_fee']['previous']==e.plan['batch_id']
    assert need['txid']!=e.need['txid'] and result[5]['plan']['inputs']==e.plan['inputs']


@pytest.mark.parametrize('mutation',['quote','parent','inputs','cycle','mode','equal_fee'])
def test_replacement_cannot_mutate_original_family_or_silently_renew_consent(selected,mutation):
    e=selected;c,payment,need=replacement(e)
    if mutation=='quote':c['quote_file']=str(e.tmp_path/'copied-quote.json');write(c['quote_file'],e.quote)
    if mutation=='parent':payment['plan']['withdrawal_fee']['previous']='cd'*32
    if mutation=='inputs':payment['plan']['inputs'][0]['outpoint']='12'*32+':0'
    if mutation=='cycle':c['previous_config']=str(e.tmp_path/'cycle.json');write(c['previous_config'],c)
    if mutation=='mode':c['mode']='S'
    if mutation=='equal_fee':payment['plan']['fee']=e.plan['fee']
    write(c['payment_file'],payment)
    with pytest.raises(ValueError):withdrawal.inputs(c,e.wc,need)


@pytest.mark.parametrize('state,root_state,allowed',[(1,0,True),(1,1,False),(1,2,False),(1,3,True),(2,2,False),(2,3,True),(3,3,False)])
def test_owner_cancel_uses_original_pending_and_cancelled_family_boundaries(workflow,state,root_state,allowed):
    w,q,s,ctx=workflow;w.submit(q,digest(q));s['request'][7]=state;s['request'][6]=b'b'*32
    root=b'b'*32 if root_state else bytes(32)
    ctx['withdrawal_fees']['payments']=NS(functions=Functions(dict(rootForRequest=lambda _:root)))
    ctx['batch']=NS(functions=Functions(dict(batchState=lambda _:root_state)))
    send=w.owner.send
    def effect(key,kind,target,method,args,guard):
        result=send(key,kind,target,method,args,guard)
        if method=='refund':s['request'][7]=4
        return result
    w.owner.send=effect;before=len(w.owner.sent)
    if allowed:
        result=w.cancel(q['operation_id']);assert result['primary_state']=='REFUNDED'
        assert w.cancel(q['operation_id'])==result and len(w.owner.sent)==before+1
    else:
        with pytest.raises(ValueError,match='SIGNATURE_MAY_EXIST'):w.cancel(q['operation_id'])
        assert len(w.owner.sent)==before
    assert int(s['request'][4])==int(q['cost']['principal_sats'])*10**10


def scenario(tmp_path,expect):
    row=checks.catalog()['SUB-19']
    return dict(version='1',scenario_id='SUB-19',row_sha256=row['row_sha256'],checks=[
        dict(id='old_winner',kind='OWNER_STATUS',config_file=str(tmp_path/'owner.json'),args=dict(operation_id='11'*32),expect=expect)])


def test_scenario_assertions_require_actual_original_sdk_results_and_separate_qualification(tmp_path,monkeypatch):
    c=scenario(tmp_path,dict(fields=[dict(path=['primary_state'],equals='PAID'),dict(path=['fee','charged'],equals='100')]))
    monkeypatch.setattr(checks,'probe',lambda *a:(dict(primary_state='PAID',fee=dict(charged='100')),dict(number='12',hash='aa'*32)))
    r=checks.execute(c,[{}]);assert r['status']=='SATISFIED' and r['observations']['observation_only'] and not r['observations']['e2e_qualified']
    c['checks'][0]['expect']['fields'][1]['equals']='101'
    with pytest.raises(ValueError,match='ASSERTION'):checks.execute(c,[{}])


def test_scenario_denial_never_accepts_transport_error_or_json_success_claim(tmp_path,monkeypatch):
    c=scenario(tmp_path,dict(error='FAMILY_ROOT_PIN_REQUIRED'))
    monkeypatch.setattr(checks,'probe',lambda *a:(_ for _ in ()).throw(ValueError('FAMILY_ROOT_PIN_REQUIRED')))
    assert checks.execute(c,[{}])['observations']['assertions'][0]['error']=='FAMILY_ROOT_PIN_REQUIRED'
    monkeypatch.setattr(checks,'probe',lambda *a:(_ for _ in ()).throw(ConnectionError('provider unavailable')))
    with pytest.raises(ConnectionError):checks.execute(c,[{}])
    monkeypatch.setattr(checks,'probe',lambda *a:(dict(status='SATISFIED'),None))
    with pytest.raises(ValueError,match='EXPECTED_DENIAL'):checks.execute(c,[{}])


def test_catalog_has_every_exact_spec_row_and_config_cannot_relabel_it(tmp_path):
    rows=checks.catalog();assert len(rows)==195 and len([k for k in rows if k.startswith('ADDR-')])==62 and len([k for k in rows if k.startswith('SUB-')])==48
    c=scenario(tmp_path,dict(fields=[dict(path=['primary_state'],equals='PAID')]))
    path=tmp_path/'checks.json';write(path,c);assert checks.load_config(path)==c
    c['scenario_id']='SUB-20';write(path,c)
    with pytest.raises(ValueError,match='CONFIG'):checks.load_config(path)


def mixed_batch(e):
    """Offline native-plan scope fixture, not an on-chain funding acceptance."""
    c=deepcopy(e.c);c.update(version='3',previous_config=None,accepted_previous_sha256=None,additional_owners=[])
    payment=deepcopy(e.payment);plan=payment['plan']
    for index,mode in enumerate(('BTC','S'),1):
        q=deepcopy(e.quote);q['options'].update(fee_mode=mode,asset='MP')
        path=e.tmp_path/('quote-extra-'+str(index)+'.json');write(path,q)
        request=str(index).zfill(2)*32
        c['additional_owners'].append(dict(owner_config=c['owner_config'],quote_file=str(path),accepted_quote_hash=digest(q),request_id=request))
        plan['withdrawal_refs'].append(request);plan['withdrawal_fee']['charges'].append(['0x'+request,'1','0'])
    plan['withdrawal_refs'].sort();plan['withdrawal_fee']['charges'].sort(key=lambda x:x[0]);payment['request']['metadata']['bitcoin_plan']=plan
    write(c['payment_file'],payment);need=dict(e.need,txid=build(plan)['txid'])
    return c,payment,need


def test_batch_scope_pins_all_quotes_and_fee_sources_independently(selected):
    e=selected;c,payment,need=mixed_batch(e);data=withdrawal.inputs(c,e.wc,need)
    assert len(data)==8 and len(data[7])==2
    assert {q['quote']['options']['fee_mode'] for q in data[7]}=={'BTC','S'}
    assert withdrawal.wire_inputs(data)[-1]==data[7]
    changed=deepcopy(c);changed['additional_owners'][0]['accepted_quote_hash']='00'*32
    with pytest.raises(ValueError):withdrawal.inputs(changed,e.wc,need)
    changed=deepcopy(c);changed['additional_owners'][0]['request_id']=c['request_id']
    with pytest.raises(ValueError):withdrawal.inputs(changed,e.wc,need)


def test_direct_mp_principal_can_use_a_separate_s_fee_source(selected):
    e=selected;c=deepcopy(e.c);e.quote['options'].update(fee_mode='S',asset='MP');write(c['quote_file'],e.quote)
    c.update(mode='S',accepted_quote_hash=digest(e.quote))
    assert withdrawal.inputs(c,e.wc,e.need)[4]['options']['asset']=='MP'


def test_batch_primary_uses_every_original_owner_without_changing_principal():
    r=withdrawal.Runtime.__new__(withdrawal.Runtime);r.c=dict(version='3',previous_config=None)
    called=[]
    class Owner:
        def __init__(self,key,principal):self.key,self.principal=key,principal
        def submit(self,q,accepted):called.append((self.key,accepted));return self.status(q['operation_id'])
        def status(self,_):return dict(request=self.key,cost=dict(principal_sats=self.principal))
    r.owner_records=[dict(owner=Owner(str(i),str(i*1000)),quote=dict(operation_id=str(i),cost=dict(principal_sats=str(i*1000))),
        accepted_quote_hash='accepted-'+str(i),request_id=str(i)) for i in (1,2,3)]
    assert r.primary()['request']=='1' and len(called)==3
    r.c['previous_config']='retained-original.json';called.clear();r.primary();assert not called
