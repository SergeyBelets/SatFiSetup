"""Original HPKE/status SDK, EOA/ABI/SQLite and budgets; graph/Core/FROST/EVM effects are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from eth_account import Account
from web3 import Web3

from ops.zkspv import contour_incident as m,contour_plan
from ops.zkspv.contour import digest
from signer_agent import incident_transport as transport,address_status as policy
from signer_agent.address_status_client import Driver
from signer_agent.bootstrap import Bootstrap,artifact
from signer_agent.operations import Journal
from signer_agent.zk_worker_prover import write
from test_incident_status import incident
from test_address_registration import registered_env
from test_address_transport import delivery
from test_contour_withdrawal import envelopes
from test_address_mint import address
from test_zk_worker import config


def values(spec,value):
    if spec['type']=='tuple':return [values(c,value[c['name']]) for c in spec['components']]
    return value


@pytest.fixture(params=[('0','SUSPENDED'),('1','SUSPENDED')])
def controlled(incident,envelopes,monkeypatch,request):
    e=incident;rpc=envelopes;scope,state=request.param
    for client in e.inc_clients:del client.send
    for name in ('get_transaction','get_transaction_receipt','get_transaction_count','send_raw_transaction','wait_for_transaction_receipt'):
        setattr(e.w3.eth,name,getattr(rpc.w3.eth,name))
    e.w3.eth.block_number=200;e.w3.provider=rpc.w3.provider;rpc.w3=e.w3
    report=e.reporter.prepare_report(scope,'ab'*32,dict(summary='private incident note',references=['cd'*32]),incident_id='ef'*32)
    reporter_driver=Driver(e.reporter,e.registration_board.address)
    preview=reporter_driver.preview(transport.report_id(report),state,False,'1600')
    decision=preview['decision'];c=dict(version='1',reporter_config='/fixture/reporter.json',operator_config='/fixture/operator.json',
        auth_bundle_file='/fixture/auth.json',report_file='/fixture/report.json',decision_file='/fixture/decision.json',
        accepted_decision_sha256=preview['confirmation_hash'],delivery='0')
    r=m.Runtime.__new__(m.Runtime);r.c=c;r.wc=dict(zk_state=address(29),bitcoin_policy={});r.bundle=e.bundle;r.report=report;r.decision=decision
    r.actors=dict(reporter=e.reporter,operator=e.operator);r.reporter=e.reporter;r.operator=e.operator;r.driver=e.status_driver
    r.change=policy.parse_change(decision['change']);r.journal=e.operator.costs;r.job=digest(c);r.active_message=None
    e.bindings=[]
    monkeypatch.setattr('signer_agent.zk_spv.inspect_binding',lambda *a,**kw:e.bindings.append(kw['adapter_address']))
    r.contexts();e.raws=rpc.raws;e.receipts=rpc.receipts;e.evm_methods=[]
    def effect(txdata,receipt):
        target=Web3.to_checksum_address(bytes(txdata['to']).hex());sender=Account.recover_transaction(rpc.raws[-1])
        kind={e.inc_board.address:'BitcoinIncidentMessageBoard',e.coordinator.address:'FROSTCoordinator',
            e.registration_board.address:'DKGOnchainBoard',e.auth.address:'DKGAuthorization',e.reg.address:'BitcoinAddressRegistry'}[target]
        fn,args=Web3().eth.contract(address=target,abi=artifact(kind)['abi']).decode_function_input(txdata['data'])
        method=fn.fn_name;args=[values(spec,args[spec['name']]) for spec in fn.abi['inputs']];e.evm_methods.append(method)
        if method in ('request','reply'):
            client=next(c for c in e.inc_clients if c.account.address==sender)
            with client.store.journal.db() as db:rows=db.execute("SELECT id,body FROM address_packets WHERE direction='OUT'").fetchall()
            selected=[(key,json.loads(body)) for key,body in rows if json.loads(body)['ciphertext']==bytes(args[-1]).hex()]
            assert len(selected)==1;key,p=selected[0];h=p['header']
            if h['kind']=='REQUEST':e.inc_routes[h['route']]=(h['sender'],h['recipient'],bytes.fromhex(p['request_id']))
            e.inc_packets[key]=(bytes.fromhex(h['route']),{'REQUEST':0,'ACK':1,'STATUS':2}[h['kind']],int(h['sequence']),
                h['sender'],h['recipient'],int(h['sender_epoch']),int(h['recipient_epoch']),int(h['expires_at']),bytes.fromhex(p['ciphertext']))
            e.inc_inboxes[h['recipient']].append(key)
        elif method=='createSessionFromDKG':
            sid=len(e.sessions)+1;e.sessions[sid]=[sid,e.operator.account.address,b'g'*32,args[1],True,2,2,int(args[3]),True,address(0),1,0,0,0,0,e.reg.address]
            receipt['events']=[dict(args=dict(sessionId=sid))]
        elif method=='publishIntent':e.public[int(args[0])]=args[1]
        elif method=='authorizeOperation':e.approved.add(args[1])
        elif method=='setStatus':
            change=policy.parse_change(Bootstrap.wire(args[0]));policy.pending(e.w3,e.ctx,change,20)
            if change[0]==0:
                e.view.custody=change[4];e.custody_gen+=1;e.custody_nonce+=1;e.closed=e.closed or change[5] or change[4] in (1,3);generation=e.custody_gen
            else:e.view.derivation=change[4];e.derivation_gen+=1;e.derivation_nonce+=1;generation=e.derivation_gen
            e.status_receipts[args[-1]]=generation;e.executed.add(args[-1])
        else:raise AssertionError(method)
    rpc.effect=effect;e.r=r;e.c=c;e.decision=decision;e.report=report;e.rpc=rpc
    e.execute=lambda action='probe':m.Pipeline(r,c,dict(capability='incident_recovery')).execute(action)
    return e


def wait_for_quorum(e):
    assert e.execute()['status']=='ABSENT'
    for n in range(12):
        before=len(e.raws);result=e.execute('run' if n==0 else 'recover');assert len(e.raws)-before<=1
        if result['status']=='WAITING' and result['observations']['reason']=='WAITING_EXTERNAL_INCIDENT_REVIEW_AND_FROST':return
    raise AssertionError(result)


def finish(e):
    wait_for_quorum(e)
    e.sessions[1][10]=2  # External private evidence approvals and threshold signing remain fixtures.
    for _ in range(10):
        before=len(e.raws);result=e.execute('recover');assert len(e.raws)-before<=1
        if result['status']=='SATISFIED':return result
    raise AssertionError(result)


def test_original_encrypted_report_ack_status_and_reviewed_transition(controlled):
    e=controlled;result=finish(e)
    assert e.evm_methods==['request','reply','createSessionFromDKG','publishIntent','authorizeOperation','setStatus','reply']
    assert result['observations']['generation']=='2' and not result['observations']['ready']
    assert all(b'private incident note' not in packet[8] for packet in e.inc_packets.values())
    count=len(e.raws);assert e.execute()['status']=='SATISFIED' and len(e.raws)==count
    assert all(a==address(30) for a in e.bindings)
    for client in e.inc_clients:
        with client.costs.db() as db:assert db.execute('SELECT count(*) FROM address_costs WHERE scope=?',(client.cost_scope,)).fetchone()[0]>0


def test_no_automatic_private_approval_share_finalization_or_session_retry(controlled):
    e=controlled;wait_for_quorum(e)
    for _ in range(3):assert e.execute()['status']=='WAITING'
    assert len(e.sessions)==1
    with e.operator.store.journal.db() as db:
        assert not db.execute("SELECT id FROM address_meta WHERE id LIKE 'incident-approval:%'").fetchall()
    assert 'authorizeOperation' not in e.evm_methods and 'setStatus' not in e.evm_methods


def test_unenrolled_recovery_and_probe_do_not_queue_or_sign(controlled):
    e=controlled
    assert e.execute()['status']=='ABSENT' and e.execute('recover')['status']=='RECOVERY_REQUIRED'
    with e.reporter.store.journal.db() as db:assert not db.execute("SELECT id FROM address_packets WHERE direction='OUT'").fetchall()
    assert not e.raws


def test_stale_generation_does_not_rebase_decision_or_open_new_session(controlled):
    e=controlled;wait_for_quorum(e);e.custody_gen+=1;e.derivation_gen+=1
    count=len(e.raws)
    with pytest.raises(ValueError,match='GENERATION'):e.execute('recover')
    assert len(e.sessions)==1 and len(e.raws)==count and e.r.record()['attempts'][0]['change']==e.decision['change']


@pytest.mark.parametrize('interruption',['unsigned','signed'])
def test_interrupted_transport_uses_original_envelope_or_holds(controlled,monkeypatch,interruption):
    e=controlled;original=Journal.update
    if interruption=='unsigned':e.rpc.crash='unsigned'
    else:
        def update(j,key,status,result):
            if str(j.path)==e.reporter.c['identity_file']+'.transactions.sqlite' and status=='SIGNED':raise RuntimeError('fixture signed interruption')
            return original(j,key,status,result)
        monkeypatch.setattr(Journal,'update',update)
    with pytest.raises(RuntimeError):e.execute('run')
    assert not e.raws;e.rpc.crash=None;monkeypatch.setattr(Journal,'update',original)
    # No new SDK graph/Core prerequisite may run before exact signed recovery.
    e.r.contexts=lambda:(_ for _ in ()).throw(AssertionError('new graph guard before recovery'))
    result=e.execute('recover')
    assert result['status']==('ABSENT' if interruption=='signed' else 'RECOVERY_REQUIRED')
    assert len(e.raws)==(1 if interruption=='signed' else 0)


def test_unrelated_outbox_cannot_be_published_by_incident_runner(controlled):
    e=controlled
    unrelated=e.reporter.prepare_report(str(e.r.change[0]),'99'*32,dict(summary='unrelated note',references=[]))
    queued=e.reporter.send_report(unrelated,e.operator.account.address)
    finish(e)
    assert queued['message_id'] not in e.inc_packets


@pytest.mark.parametrize('mutation',['ciphertext','raw','receipt','anchor'])
def test_completion_requires_original_transport_envelope_and_canonical_receipts(controlled,mutation):
    e=controlled;finish(e)
    p=m.Pipeline(e.r,e.c,dict(capability='incident_recovery'));row=p.journal.get(p.key)['result']
    if mutation=='ciphertext':
        key=row['delivery']['message_id'];packet=list(e.inc_packets[key]);packet[8]=b'x'*64;e.inc_packets[key]=tuple(packet)
    elif mutation=='anchor':e.view.reorg=True
    elif mutation=='receipt':e.receipts.pop(next(iter(e.receipts)))
    else:
        call=next(iter(row['calls'].values()));actor=e.r.actors[call['actor']]
        j=Journal(actor.c['identity_file']+'.transactions.sqlite');key=call['profile']['deployment_id']+':send';saved=j.get(key)
        saved['result']['nonce']='77';j.update(key,'CONFIRMED',saved['result'])
    try:result=e.execute()
    except ValueError:return
    assert result['status']==('WAITING' if mutation=='receipt' else 'RECOVERY_REQUIRED')
    if mutation=='receipt':assert result['observations']['reason']=='EVM_RECEIPT_PENDING'


def test_lost_status_record_write_recovers_same_receipt_after_later_generations(controlled,monkeypatch):
    e=controlled;wait_for_quorum(e);e.sessions[1][10]=2
    e.execute('recover')  # Authorize.
    original=e.r.notice
    e.r.notice=lambda *a:(_ for _ in ()).throw(RuntimeError('fixture after setStatus'))
    with pytest.raises(RuntimeError):e.execute('recover')
    count=e.evm_methods.count('setStatus');e.custody_gen+=3;e.derivation_gen+=3
    e.r.notice=original
    for _ in range(4):
        result=e.execute('recover')
        if result['status']=='SATISFIED':break
    assert result['status']=='SATISFIED' and e.evm_methods.count('setStatus')==count==1


def test_preview_is_read_only_and_exact_without_granting_signer_approval(controlled):
    e=controlled
    preview=Driver(e.reporter,e.registration_board.address).preview(transport.report_id(e.report),'SUSPENDED',False,'1600')
    assert preview['decision']==e.decision and preview['confirmation_hash']==digest(e.decision)
    assert not e.sessions and not e.status_receipts and not e.raws


def resuming(e,monkeypatch):
    from signer_agent import bitcoin_trust,contract_provenance
    monkeypatch.setattr(bitcoin_trust,'checked_runtime',lambda *_:None)
    monkeypatch.setattr(contract_provenance,'prove_creation',lambda *_:None)
    scope=e.r.change[0]
    if scope==0:e.view.custody=2
    else:e.view.derivation=1
    name='ACTIVE' if scope==0 else 'QUALIFIED'
    decision=Driver(e.reporter,e.registration_board.address).preview(transport.report_id(e.report),name,False,'1600')['decision']
    e.r.decision=e.decision=decision;e.r.change=policy.parse_change(decision['change']);e.c['accepted_decision_sha256']=digest(decision)


@pytest.mark.parametrize('failure',['missing_service','unqualified','known_terminal'])
def test_resume_requires_service_qualification_and_retained_observations(controlled,monkeypatch,failure):
    from signer_agent.incident_observations import observe
    e=controlled;resuming(e,monkeypatch)
    if failure=='unqualified' and e.r.change[0]==1:e.view.qualified=False
    if failure=='known_terminal':
        scope=e.r.change[0]
        if scope==0:e.view.custody=3
        else:e.view.derivation=2
        observe(e.operator,e.ctx,scope,20,restrictive=True)
        if scope==0:e.view.custody=2
        else:e.view.derivation=1
    for n in range(8):
        try:result=e.execute('run' if n==0 else 'recover')
        except ValueError as exc:
            assert any(word in str(exc) for word in ('SERVICE_REVIEW','UNQUALIFIED','RECOVERY_REQUIRED'));break
    else:raise AssertionError(result)
    assert not e.sessions and not e.status_receipts and e.evm_methods==['request','reply']


def test_resume_rechecks_real_watch_guard_and_preserves_financial_pause(controlled,monkeypatch,tmp_path):
    from signer_agent import bitcoin_rpc
    from test_address_transport import private
    e=controlled;resuming(e,monkeypatch)
    service_config=dict(e.configs[1],bitcoin_rpc='http://127.0.0.1:18443',bitcoin_cookie=str(tmp_path/'cookie'))
    e.operator.c['recovery_service_config']=private(tmp_path/'recovery.json',service_config)
    best='88'*32
    class Core:
        def __init__(self,*args):pass
        def check_network(self):pass
        def call(self,method,*args):
            if method=='getbestblockhash':return best
            if method=='getblockhash':return e.reg_values['bitcoinGenesis'].hex() if args[0]==0 else best
            raise AssertionError(method)
    monkeypatch.setattr(bitcoin_rpc,'BitcoinRPC',Core)
    e.done[e.intent.intent_id]=e.intent.encode();e.reg_values.update(intentCount=1,intentIds=lambda _:e.intent.intent_id)
    e.batch.functions.values['feeReserve']=transport.ZERO
    from test_address_watch import set_coverage
    set_coverage(e.service.store,e.intent.intent_id.hex(),5,best)
    e.service.store.put('financial-pause-sentinel',dict(retained=True))
    result=finish(e)
    assert result['status']=='SATISFIED' and e.service.store.get('financial-pause-sentinel')==dict(retained=True)


def test_original_gas_cap_failure_holds_before_signing(controlled):
    e=controlled;e.reporter.c['max_transaction_cost_wei']='1'
    with pytest.raises(ValueError,match='GAS_BUDGET'):e.execute('run')
    e.reporter.c['max_transaction_cost_wei']='10000000'
    assert e.execute('recover')['status']=='RECOVERY_REQUIRED' and not e.raws


def test_original_notify_default_still_covers_all_report_routes(incident):
    e=incident;r,first=e.new_report()
    second=e.reporter.send_report(r,e.operator.account.address,'1');e.inc_publish(e.reporter);e.operator.receive_once()
    assert e.operator.notify(transport.report_id(r),route=first['route'])['count']==1
    assert e.operator.notify(transport.report_id(r))['count']==1
    for route in (first['route'],second['route']):assert e.operator.store.get('status:'+route)['sequence']==1


def test_latest_only_status_receipt_waits_without_new_generation_guard(controlled,monkeypatch):
    e=controlled;wait_for_quorum(e);count=len(e.raws)
    monkeypatch.setattr(e.r.driver,'status',lambda *a:dict(status='STATUS_CONFIRMING'))
    monkeypatch.setattr(e.r,'guard',lambda *a:(_ for _ in ()).throw(AssertionError('no new operation during confirmation')))
    result=e.execute()
    assert result['status']=='WAITING' and result['observations']['reason']=='STATUS_CONFIRMING' and len(e.raws)==count


@pytest.mark.parametrize('mutation',['attempt','change'])
def test_changed_original_attempt_requires_explicit_new_review(controlled,mutation):
    e=controlled;wait_for_quorum(e);count=len(e.raws);r=e.r.record()
    if mutation=='attempt':r['attempts'].append(deepcopy(r['attempts'][0]))
    else:r['attempts'][0]['change'][2]='9'
    e.operator.store.put(e.r.driver._key(e.decision['operation_id']),r)
    with pytest.raises(ValueError,match='ATTEMPT'):e.execute('recover')
    assert len(e.raws)==count and len(e.sessions)==1


@pytest.fixture
def offline(controlled,tmp_path):
    e=controlled;wc=config(tmp_path);wc['allowed_consumers']={e.pool.address:'MiningPoolV1'}
    configs=[]
    for role,client in e.r.actors.items():
        cfg=dict(client.c,bitcoin_network='mainnet',bitcoin_policy=wc['bitcoin_policy']);path=tmp_path/(role+'-incident.json')
        write(path,cfg);configs.append(str(path))
    c=dict(e.c,reporter_config=configs[0],operator_config=configs[1],auth_bundle_file=str(tmp_path/'bundle.json'),
        report_file=str(tmp_path/'report.json'),decision_file=str(tmp_path/'decision.json'))
    for name,value in [('bundle.json',e.bundle),('report.json',e.report),('decision.json',e.decision),('incident.json',c),('worker.json',wc),
        ('need.json',dict(kind='deposit',consumer=e.pool.address,txid='22'*32,vout='0'))]:write(tmp_path/name,value)
    return NS(e=e,c=c,wc=wc,tmp_path=tmp_path)


def test_offline_builder_creates_standalone_partial_incident_plan(offline):
    f=offline;data=m.inputs(f.c,f.wc)
    plan=contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'run',incident_config=f.tmp_path/'incident.json')
    assert [s['capability'] for s in plan['stages']]==['incident_recovery']
    assert digest(data) in plan['stages'][0]['run']
    assert plan['stages'][0]['run']==plan['stages'][0]['recover']==plan['stages'][0]['probe']


@pytest.mark.parametrize('extra',[dict(service_config='unused'),dict(reconciliation_config='unused'),dict(withdrawal_config='unused'),
    dict(mint_config='unused'),dict(materialize=True),dict(consumer_only=True),dict(bootstrap_config='unused'),dict(settlement_only=True)])
def test_incident_cannot_depend_on_monetary_or_fresh_stages(offline,extra):
    f=offline
    with pytest.raises(ValueError,match='STANDALONE'):
        contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'run',incident_config=f.tmp_path/'incident.json',**extra)


@pytest.mark.parametrize('mutation',['accepted_hash','generation','report','bundle','scope','actor'])
def test_offline_incident_inputs_reject_substitution(offline,mutation):
    f=offline
    if mutation=='accepted_hash':f.c['accepted_decision_sha256']='00'*32
    if mutation=='generation':
        d=deepcopy(f.e.decision);d['change'][2]='2';write(f.tmp_path/'decision.json',d)
    if mutation=='report':
        r=deepcopy(f.e.report);r['evidence_hash']='01'*32;write(f.tmp_path/'report.json',r)
    if mutation=='bundle':
        b=deepcopy(f.e.bundle);b['purpose']='BTC_SPEND';write(f.tmp_path/'bundle.json',b)
    if mutation=='scope':f.wc['allowed_consumers']={}
    if mutation=='actor':f.c['operator_config']=f.c['reporter_config']
    with pytest.raises(ValueError):m.inputs(f.c,f.wc)


def test_incident_cli_chain_preserves_binding_and_private_evidence(offline,monkeypatch):
    import subprocess
    from ops.zkspv.contour import Contour
    f=offline;e=f.e
    plan=contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'cli',incident_config=f.tmp_path/'incident.json')
    e.r.c=f.c;monkeypatch.setattr(m,'Runtime',lambda *a:e.r)
    original=subprocess.Popen
    def popen(command,*args,**kwargs):
        if command[1:3]==['-m','ops.zkspv.contour_incident']:
            monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',kwargs['env']['SATOHSI_CONTOUR_CONTEXT'])
            m.main(command[3:]);return NS(pid=999999999,wait=lambda **kw:0)
        return original(command,*args,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',popen)
    runner=Contour(plan)
    for _ in range(14):
        before=len(e.raws);result=runner.step();assert len(e.raws)-before<=1
        for session in e.sessions.values():session[10]=2
        if result['status']=='DRIVERS_COMPLETED_UNQUALIFIED':break
    assert result['status']=='DRIVERS_COMPLETED_UNQUALIFIED' and not result['combined_e2e_accepted']
    artifacts=list((f.tmp_path/'cli').rglob('incident-evidence.json'));assert artifacts
    assert 'private incident note' not in artifacts[0].read_text() and 'salt' not in artifacts[0].read_text()


def test_status_preview_cli_exports_reviewable_decision_without_delivery_or_signature(offline,monkeypatch):
    from signer_agent import incident_cli
    f=offline;e=f.e;out=f.tmp_path/'preview.json'
    monkeypatch.setattr(incident_cli,'Client',lambda cfg:e.reporter)
    assert incident_cli.main(['status-preview','--config',f.c['reporter_config'],'--registration-board',e.registration_board.address,
        '--report-id',e.decision['report_id'],'--next-state','SUSPENDED','--deadline','1600','--output',str(out)])==0
    assert json.loads(out.read_text())==e.decision and out.stat().st_mode&0o077==0 and not e.raws and not e.sessions


def test_incident_cli_rejects_changed_private_input_before_rpc(offline,monkeypatch):
    f=offline;plan=contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'run',incident_config=f.tmp_path/'incident.json')
    report=deepcopy(f.e.report);report['details']['summary']='changed private input';write(f.tmp_path/'report.json',report)
    context=f.tmp_path/'context.json';write(context,{})
    monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',str(context))
    monkeypatch.setattr(m,'Runtime',lambda *a:(_ for _ in ()).throw(AssertionError('RPC must not run')))
    with pytest.raises(ValueError,match='SCOPE'):m.main(plan['stages'][0]['run'][3:])
