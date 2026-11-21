"""Original Fleet/retention/native/SQLite control; chain, qualification and reserve providers are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from ops.zkspv import contour_epoch as m,contour_plan
from ops.zkspv.contour import digest
from signer_agent import epoch_retention as retention,btc_fee_reserve,crypto
from signer_agent.operations import Journal
from signer_agent.zk_worker_prover import write
from test_epoch_service import epoch,packets
from test_address_transport import delivery,private
from test_address_watch import watched,block,tx
from test_address_mint import address
from test_zk_worker import config

ORIGINAL_FEE_CONFIG=btc_fee_reserve.load_config


def runtime(e,monkeypatch,*,bootstrap_scan=False):
    # The existing fixture supplies explicit chain/reserve providers; use the
    # actual Fleet phases, native keys, original ledgers and retention signatures.
    monkeypatch.setattr(btc_fee_reserve.Inspector,'context',lambda self:dict(registry=e.reg,pool=e.pool,batch=e.batch),raising=False)
    f=m.ScopedFleet(e.policy,client_factory=lambda c:e.service,configuration=e.config_file)
    wc=config(e.tmp_path);wc.update(bitcoin_policy=None,allowed_consumers={e.pool.address:'MiningPoolV1'})
    f.worker=wc;e.bound=[]
    monkeypatch.setattr('signer_agent.zk_spv.inspect_binding',lambda *a,**kw:e.bound.append(kw['adapter_address']))
    if bootstrap_scan:
        f.run_once();f.run_once()
    request=retention.challenge(f,[]);current=f.service_review(e.policy['epochs'][0]);claims=packets(e,request,current)
    r=m.Runtime.__new__(m.Runtime);r.fleet=f;r.request=request;r.packets=claims;r.journal=f.lane
    c=dict(version='1',fleet_config=e.config_file,challenge_file=str(e.tmp_path/'challenge.json'),attestation_files=[])
    write(c['challenge_file'],request)
    for i,p in enumerate(claims):
        path=e.tmp_path/('claim-'+str(i)+'.json');write(path,p);c['attestation_files'].append(str(path))
    r.input_hash=digest([e.policy,f.configs,request,claims]);r.c=c;e.r=r;e.c=c;e.wc=wc;e.request=request;e.claims=claims
    e.execute=lambda action='probe':m.Pipeline(r,c,dict(capability='old_epoch_service')).execute(action)
    return e


@pytest.fixture
def controlled(epoch,monkeypatch):return runtime(epoch,monkeypatch)


def complete(e):
    assert e.execute()['status']=='ABSENT'
    assert e.execute('run')['status']=='ABSENT'
    result=e.execute()
    assert result['status']=='SATISFIED',result
    return result


def test_original_fleet_sweep_and_fresh_signed_threshold_review(controlled):
    e=controlled;result=complete(e)
    assert result['observations']['epochs'][0]['retained']=='2'
    assert not result['observations']['ready'] and result['observations']['evm_operations_performed']==0
    assert e.execute()['status']=='SATISFIED' and e.bound
    with e.r.fleet.journal.db() as db:assert db.execute('SELECT count(*) FROM epoch_runs').fetchone()[0]==1
    with e.r.fleet.lane.db() as db:
        assert db.execute('SELECT count(*) FROM epoch_retention_current').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM epoch_members').fetchone()[0]==1


def test_probe_and_unenrolled_recovery_never_start_observation_or_attestation(controlled,monkeypatch):
    e=controlled;monkeypatch.setattr(e.r,'scan',lambda:(_ for _ in ()).throw(AssertionError()))
    assert e.execute()['status']=='ABSENT'
    assert e.execute('recover')['status']=='RECOVERY_REQUIRED'
    assert not e.r.fleet.journal.get('epoch-fleet')['result']


@pytest.mark.parametrize('mutation',['threshold','signature','expired','departing','generation','qualified','funding','key','reorg','custody','closed','derivation'])
def test_stale_or_unavailable_retention_cannot_satisfy_old_epoch_stage(controlled,mutation):
    e=controlled;e.execute('run')
    if mutation=='threshold':e.r.packets=e.claims[:1]
    if mutation=='signature':e.r.packets[0]['signature']=e.r.packets[1]['signature']
    if mutation=='expired':e.view.timestamp=1600
    if mutation=='departing':
        request=retention.challenge(e.r.fleet,[e.accounts[0].address]);e.r.request=request
        e.r.packets=packets(e,request,e.r.fleet.service_review(e.policy['epochs'][0]))
    if mutation=='generation':e.cg+=1
    if mutation=='qualified':e.verifier.functions.values['QUALIFIED']=False
    if mutation=='funding':e.w3.eth.get_balance=lambda *a,**kw:1
    if mutation=='key':e.service.keys[1]['secret']='01'*32
    if mutation=='reorg':e.view.reorg=True
    if mutation=='custody':e.view.custody=2
    if mutation=='closed':e.closed=False
    if mutation=='derivation':e.view.derivation=1
    result=e.execute()
    assert result['status']=='RECOVERY_REQUIRED' and not result['observations']['ready']


@pytest.mark.parametrize('phase',['transport','watch','funding'])
def test_failed_phase_still_retains_archive_and_runs_other_observation_phases(controlled,monkeypatch,phase):
    e=controlled;seen=[];f=e.r.fleet
    if phase=='transport':monkeypatch.setattr(e.service,'poll',lambda:(_ for _ in ()).throw(RuntimeError('private RPC password must not escape')))
    else:
        original=getattr(f,phase if phase=='watch' else 'fees')
        monkeypatch.setattr(f,phase if phase=='watch' else 'fees',lambda *a:(_ for _ in ()).throw(RuntimeError('private unavailable observation')))
    original_retain=f.retain_archive
    monkeypatch.setattr(f,'retain_archive',lambda *a:seen.append(a[-1]) or original_retain(*a))
    e.execute('run');result=e.execute()
    assert result['status']=='ABSENT' and seen.count('ADDRESS')>=2
    assert 'password' not in json.dumps(result) and 'private unavailable' not in json.dumps(result)


def test_crash_after_scan_repeats_only_original_idempotent_observation(controlled,monkeypatch):
    e=controlled;original=Journal.update
    def update(j,key,status,result):
        if str(j.path)==str(e.r.journal.path) and key.startswith('contour-epoch:') and result.get('observed'):
            raise RuntimeError('fixture crash after original Fleet sweep')
        return original(j,key,status,result)
    monkeypatch.setattr(Journal,'update',update)
    with pytest.raises(RuntimeError):e.execute('run')
    monkeypatch.setattr(Journal,'update',original)
    assert e.execute('recover')['status']=='ABSENT' and e.execute()['status']=='SATISFIED'
    with e.r.fleet.journal.db() as db:assert db.execute('SELECT count(*) FROM epoch_runs').fetchone()[0]==1


def test_original_archive_loss_requires_recovery_even_if_watch_can_recover_canonical_tip(controlled):
    e=controlled;entry=e.policy['epochs'][0]
    with e.r.fleet.lane.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS epoch_archive(fleet TEXT,context TEXT,purpose TEXT,id TEXT,digest TEXT,PRIMARY KEY(fleet,context,purpose,id))')
        db.execute('INSERT INTO epoch_archive VALUES(?,?,?,?,?)',(e.policy['fleet_id'],entry['wallet_context'],'ADDRESS','O:lost-history','lost-history'))
    e.execute('run');result=e.execute()
    assert result['status']=='RECOVERY_REQUIRED' and result['observations']['reason']=='ORIGINAL_EPOCH_ARCHIVE_REQUIRED'


def test_fresh_probe_never_uses_previous_healthy_review_as_authority(controlled):
    e=controlled;complete(e);e.cg+=1
    assert e.execute()['status']=='RECOVERY_REQUIRED'


def test_explicit_fresh_challenge_cannot_reuse_old_contour_enrollment(controlled):
    e=controlled;complete(e)
    request=retention.challenge(e.r.fleet,[]);claims=packets(e,request,e.r.fleet.service_review(e.policy['epochs'][0]))
    e.r.request=request;e.r.packets=claims;e.r.input_hash=digest([e.policy,e.r.fleet.configs,request,claims])
    assert e.execute()['status']=='ABSENT'
    assert e.execute('recover')['status']=='RECOVERY_REQUIRED'
    e.execute('run');assert e.execute()['status']=='SATISFIED'
    with e.r.journal.db() as db:assert db.execute("SELECT count(*) FROM operations WHERE id LIKE 'contour-epoch:%'").fetchone()[0]==2


def test_shared_payer_balance_is_not_reused_across_two_epochs(controlled,monkeypatch):
    from dataclasses import replace
    e=controlled;f=e.r.fleet;second=replace(e.manifest,session_id=b'n'*32)
    f.c=deepcopy(f.c);f.c['epochs'].append(dict(f.c['epochs'][0],wallet_context=second.context.hex()))
    first=f.service_review(f.c['epochs'][0]);first['balance']='15000000';other=dict(first,snapshot=dict(first['snapshot'],manifest=second))
    monkeypatch.setattr(f,'service_review',lambda entry:first if entry['wallet_context']==e.manifest.context.hex() else other)
    request=retention.challenge(f,[]);claims=packets(e,request,first)
    from eth_account import Account
    for p,a in zip(deepcopy(claims),e.accounts):
        p['body']['wallet_context']=second.context.hex();p['signature']=Account.sign_message(retention.message(p['body']),a.key).signature.hex();claims.append(p)
    e.r.request=request;e.r.packets=claims
    sample=dict(wallet_context=e.manifest.context.hex(),archive=dict(status='ARCHIVE_RETAINED'),retained=dict(status='ARCHIVE_RETAINED'),
        bindings=dict(status='BINDINGS_OBSERVED'),transport=dict(status='RECEIVED'),watch=dict(status='IDLE'),
        fee_observations=dict(status='NO_FEE_PURPOSE_ARCHIVE'),funding=dict(reserve=dict(status='SUFFICIENT_QUALIFICATION_REQUIRED')))
    pending=[sample,dict(sample,wallet_context=second.context.hex())]
    monkeypatch.setattr(e.r,'scan',lambda:dict(status='EPOCH_SERVICE_OBSERVED',epochs=[pending.pop(0)]))
    e.execute('run');assert e.execute()['status']=='ABSENT'
    e.execute('recover');result=e.execute()
    assert result['status']=='RECOVERY_REQUIRED' and all(r['reason']=='EPOCH_EVM_FUNDING_REQUIRED' for r in result['observations']['epochs'])


def test_original_spv_scheduling_and_attestations_are_never_implicitly_created(controlled,monkeypatch):
    e=controlled;scan=e.r.fleet.run_once;flags=[]
    monkeypatch.setattr(e.r.fleet,'run_once',lambda **kw:flags.append(kw) or scan(**kw))
    monkeypatch.setattr(retention,'attest',lambda *a:(_ for _ in ()).throw(AssertionError('private attestation must remain external')))
    complete(e);assert flags==[dict(schedule_spv=False)]


def test_unenrolled_challenge_cannot_be_imported_as_authority(controlled,monkeypatch):
    e=controlled;monkeypatch.setattr(m,'ScopedFleet',lambda *a,**kw:e.r.fleet)
    request=dict(e.request,challenge='99'*32)
    with pytest.raises(ValueError,match='RETENTION_REQUEST'):
        m.Runtime(e.c,e.wc,(e.policy,[],request,e.claims))
    with e.r.journal.db() as db:assert not db.execute('SELECT id FROM operations WHERE id=?',('epoch-review:'+request['challenge'],)).fetchone()


def test_native_late_deposit_reorg_catchup_retains_orphan_history_and_financial_pause(epoch,watched,monkeypatch):
    from signer_agent import address_mint,bitcoin_rpc
    e=epoch;w=watched
    # Canonical registry/binding provenance is a fixture; block parsing, scans,
    # UTXO/reorg history and both original journals execute for real.
    e.service.store=w.store;e.service.scope=dict(e.service.scope,bitcoin_genesis=w.intent.bitcoin_genesis.hex())
    e.reg_values.update(bitcoinGenesis=w.intent.bitcoin_genesis,intentCount=1,intentIds=lambda _:w.intent.intent_id,
        getBinding=lambda _:(w.intent.encode(),e.pool.address))
    monkeypatch.setattr(address_mint,'binding',lambda *a:dict(intent=w.intent))
    monkeypatch.setattr(bitcoin_rpc,'BitcoinRPC',lambda *a,**kw:w.bitcoin)
    monkeypatch.setattr(m.ScopedFleet,'import_addresses',lambda *a:dict(status='BINDINGS_OBSERVED',processed='1',registered='1'))
    e=runtime(e,monkeypatch,bootstrap_scan=True)
    e.service.store.put('financial-pause-sentinel',dict(retained=True));complete(e)
    late=tx(bytes.fromhex(w.desc['script_pubkey']),tag=31)
    w.view.chain.append(block(w.view.chain[-1]['hash'],[late],32))
    assert e.execute()['status']=='ABSENT'
    e.execute('recover');assert e.execute()['status']=='SATISFIED'
    assert len(w.store.outputs(w.intent.intent_id.hex()))==2
    orphan=w.view.chain[-1];w.view.chain[-1]=block(w.view.chain[-2]['hash'],[tx(b'\x51',tag=33)],34)
    assert e.execute()['status']=='ABSENT'
    e.execute('recover');assert e.execute()['status']=='SATISFIED'
    assert len(w.store.outputs(w.intent.intent_id.hex()))==1
    with w.store.journal.db() as db:assert db.execute('SELECT id FROM address_observations WHERE hash=?',(orphan['hash'],)).fetchone()
    assert w.store.get('financial-pause-sentinel')==dict(retained=True)


@pytest.fixture
def offline(controlled,monkeypatch):
    e=controlled;wc=config(e.tmp_path);wc['allowed_consumers']={e.pool.address:'MiningPoolV1'}
    ac=dict(e.service.c,bitcoin_network='mainnet',bitcoin_policy=wc['bitcoin_policy'])
    fee=json.loads(Path('config/withdrawal-fee.example.json').read_text());fee.update({k:ac[k] for k in
        ('chain_id','bitcoin_network','bitcoin_policy','evm_confirmations','identity_file')})
    write(ac['fee_reserve_config'],fee);write(e.policy['epochs'][0]['address_config'],ac)
    # Module-level imported loader is also patched explicitly for strict offline parsing.
    monkeypatch.setattr(m,'fee_config',ORIGINAL_FEE_CONFIG)
    write(e.tmp_path/'contour-epoch.json',e.c);write(e.tmp_path/'worker.json',wc)
    write(e.tmp_path/'need.json',dict(kind='deposit',consumer=e.pool.address,txid='22'*32,vout='0'))
    return NS(e=e,c=e.c,wc=wc,ac=ac,fee=fee,tmp_path=e.tmp_path)


def test_offline_builder_pins_original_fleet_threshold_inputs_and_keys(offline):
    f=offline;data=m.inputs(f.c,f.wc)
    plan=contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'run',epoch_config=f.tmp_path/'contour-epoch.json')
    assert [s['capability'] for s in plan['stages']]==['old_epoch_service'] and digest(data) in plan['stages'][0]['run']
    assert plan['stages'][0]['run']==plan['stages'][0]['recover']==plan['stages'][0]['probe']
    assert data[1][0]['key_sha256'][0][0]==f.ac['key_files'][0]


@pytest.mark.parametrize('extra',[dict(incident_config='unused'),dict(service_config='unused'),dict(reconciliation_config='unused'),
    dict(withdrawal_config='unused'),dict(mint_config='unused'),dict(bootstrap_config='unused'),dict(funding_config='unused'),dict(consumer_only=True)])
def test_old_epoch_plan_is_standalone_and_observation_only(offline,extra):
    f=offline
    with pytest.raises(ValueError,match='STANDALONE'):
        contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'run',epoch_config=f.tmp_path/'contour-epoch.json',**extra)


@pytest.mark.parametrize('mutation',['relative','unknown','duplicate','overflow','no_servicing','allowlist','fee_scope','challenge','large_packet'])
def test_strict_private_scope_and_input_limits(offline,mutation):
    f=offline;c=deepcopy(f.c)
    if mutation=='relative':c['fleet_config']='relative'
    if mutation=='unknown':c['unknown']=True
    if mutation=='duplicate':c['attestation_files']*=2
    if mutation=='overflow':c['attestation_files']=['/absolute/'+str(i) for i in range(513)]
    if mutation=='no_servicing':
        p=deepcopy(f.e.policy);p['epochs'][0]['mode']='ISSUING';write(c['fleet_config'],p)
    if mutation=='allowlist':f.wc['allowed_consumers']={}
    if mutation=='fee_scope':f.fee['identity_file']='/other/identity';write(f.ac['fee_reserve_config'],f.fee)
    if mutation=='challenge':
        body=deepcopy(f.e.request);body['wallet_contexts']=[];write(c['challenge_file'],body)
    if mutation=='large_packet':Path(c['attestation_files'][0]).write_text('x'*16385)
    write(f.tmp_path/'contour-epoch.json',c)
    with pytest.raises(ValueError):m.inputs(m.load_config(f.tmp_path/'contour-epoch.json'),f.wc)


def test_changed_claim_is_rejected_by_cli_before_rpc(offline,monkeypatch):
    f=offline;plan=contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'run',epoch_config=f.tmp_path/'contour-epoch.json')
    p=deepcopy(f.e.claims[0]);p['signature']='00'*65;write(f.c['attestation_files'][0],p)
    ctx=f.tmp_path/'context.json';write(ctx,{});monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',str(ctx))
    monkeypatch.setattr(m,'Runtime',lambda *a:(_ for _ in ()).throw(AssertionError('RPC must not run')))
    with pytest.raises(ValueError,match='SCOPE'):m.main(plan['stages'][0]['run'][3:])


def test_epoch_cli_runner_chain_retains_private_source_bound_completion(offline,monkeypatch):
    import subprocess
    from ops.zkspv.contour import Contour
    f=offline;e=f.e
    plan=contour_plan.build(f.tmp_path/'worker.json',f.tmp_path/'need.json',f.tmp_path/'cli',epoch_config=f.tmp_path/'contour-epoch.json')
    # The private inputs are parsed/pinned by the real entrypoint. Runtime chain
    # providers are the original explicit fixture used above.
    monkeypatch.setattr(m,'Runtime',lambda *a:e.r)
    original=subprocess.Popen
    def popen(command,*args,**kwargs):
        if command[1:3]==['-m','ops.zkspv.contour_epoch']:
            monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',kwargs['env']['SATOHSI_CONTOUR_CONTEXT'])
            m.main(command[3:]);return NS(pid=999999999,wait=lambda **kw:0)
        return original(command,*args,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',popen)
    runner=Contour(plan)
    for _ in range(5):
        result=runner.step()
        if result['status']=='DRIVERS_COMPLETED_UNQUALIFIED':break
    assert result['status']=='DRIVERS_COMPLETED_UNQUALIFIED' and not result['combined_e2e_accepted']
    evidence=list((f.tmp_path/'cli').rglob('epoch-evidence.json'));assert evidence
    assert 'secret' not in evidence[0].read_text() and 'private_key' not in evidence[0].read_text()
