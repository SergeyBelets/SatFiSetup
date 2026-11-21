"""Real controller/worker/SQLite/codec/signature control; Core/EVM/proving are explicit fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from eth_account import Account
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import TransactionNotFound

from ops.zkspv import contour_worker as adapter
from ops.zkspv.contour import Contour, digest, sha
from signer_agent import zk_worker as worker_module, zk_worker_prover as prover_module
from signer_agent.zk_worker import Inbox, Worker, state_wire
from signer_agent.zk_worker_prover import LocalProver, ROOT, write, read
from signer_agent.zk_spv import MAINNET_IMAGE, VERIFIER_SELECTOR
from signer_agent.operations import Journal
from test_zk_worker import config, fixture_headers, fake_state, stage_report


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    c=config(tmp_path);account=Account.from_key((1).to_bytes(32,'big'))
    write(c['identity_file'],dict(private_key=account.key.hex()))
    headers=fixture_headers()
    from signer_agent.bitcoin_network import header_hash
    before=['00'*32,'32256',header_hash(headers[32256]).hex(),'32256','01'*32,'1262152739','11'*32,'22'*32,False,'33'*32,'44'*32,'1']
    after=list(before);after[1]='32257';after[2]=header_hash(headers[32257]).hex();after[3]='32257';after[7]='55'*32;after[11]='2'
    e=NS(tmp_path=tmp_path,c=c,account=account,before=before,after=after,current=before,fresh=False,
         verifies=0,stages=[],publishes=0,request_status='NEEDS_STATE',transactions={},receipts={},crash=False)
    def block(n):return AttributeDict(dict(number=10 if not isinstance(n,int) else n,hash=b'h'*32,timestamp=1500000000))
    def get_transaction(key):
        if key not in e.transactions:raise TransactionNotFound(key)
        return e.transactions[key]
    def get_receipt(key):
        if key not in e.receipts:raise TransactionNotFound(key)
        return e.receipts[key]
    w3=Web3();w=Worker.__new__(Worker);w.c=c;w.inbox=Inbox(c);w.lock_fd=None
    actual_state=w3.eth.contract(address=c['zk_state'],abi=worker_module.artifact('ZkBitcoinStateV2')['abi'])
    state=NS(address=c['zk_state'],functions=NS(
        getState=lambda:NS(call=lambda **kw:fake_state(e.current)),halted=lambda:NS(call=lambda **kw:False),
        isFresh=lambda:NS(call=lambda **kw:e.fresh),submitTransition=actual_state.functions.submitTransition))
    w.w3=NS(eth=NS(chain_id=31337,block_number=10,get_block=block,get_balance=lambda *a,**kw:10**20,
        get_transaction=get_transaction,get_transaction_receipt=get_receipt))
    def rpc(method,*args):
        if method=='getblockhash':return header_hash(headers[args[0]])[::-1].hex()
        if method=='getblockheader':
            height=next(h for h,raw in headers.items() if header_hash(raw)[::-1].hex()==args[0])
            return dict(height=height,chainwork=e.current[4],time=int(e.current[5])) if args[1] else headers[height].hex()
        raise AssertionError(method)
    w.bitcoin=NS(call=rpc)
    records={'relay':dict(address=c['zk_state'],artifact='ZkBitcoinStateV2')}
    w.context=lambda **kw:(block('latest'),state,None,records,dict(blocks=32257))
    w.observe_need=lambda *args:dict(status=e.request_status,height=32257,depth=6,header=headers[32257].hex())
    original_read=worker_module.read
    # Explicit unit source inventory; the unchanged guest program still has its
    # real artifact pin. This fixture never claims to generate a genuine proof.
    program_pins=original_read(ROOT/'rust/zkspv/guest-integration/report.json')['evidence_sha256']
    monkeypatch.setattr(worker_module,'read',lambda path:dict(source_sha256={},program_image_id=MAINNET_IMAGE,evidence_sha256=program_pins)
        if str(path).endswith('guest-integration/report.json') else original_read(path))
    def native(archive,new,heights,out):
        out.mkdir(parents=True,exist_ok=True)
        reference=ROOT/'rust/zkspv/state-proof-integration/evidence/local-state'
        for name in ('state.input','state.journal'):(out/name).write_bytes((reference/name).read_bytes())
        result=dict(before=e.before,after=e.after,archive=new,memberships=[])
        write(out/'native.json',result);return result
    w.native=native
    def verify(self,base,receipt,output):
        e.verifies+=1
        if Path(receipt).read_bytes()!=b'groth16':raise ValueError('ZK_RECEIPT_NOT_VERIFIED')
        return VERIFIER_SELECTOR + bytes(256)
    monkeypatch.setattr(LocalProver,'verify',verify)
    monkeypatch.setattr(LocalProver,'preflight',lambda *args:None)
    monkeypatch.setattr(prover_module.shutil,'disk_usage',lambda base:NS(free=100*1024**3))
    def run(command,**kw):
        stage=command[3];e.stages.append(stage);base=Path(command[command.index('--output')+1])
        stage_report(base,stage)
        origin=LocalProver(e.c).origins();folder=base/stage
        write(folder/'local-origin.json',origin.sign(origin.payload(base,folder/'receipt.bin',stage=stage,report=folder/'report.json')))
        if e.crash and stage=='composite':raise RuntimeError('explicit-stage-interruption-fixture')
        return NS(returncode=0)
    monkeypatch.setattr(prover_module.subprocess,'run',run)
    def publish(name,step,records):
        e.publishes+=1;journal=Journal(w.inbox.path/'transactions.sqlite');key=name+':transition'
        entry=journal.put(key,step)
        if not entry['result'].get('raw_transaction'):
            data=actual_state.functions.submitTransition(bytes.fromhex(step['args'][0][2:]),bytes.fromhex(step['args'][1][2:]))._encode_transaction_data()
            transaction=dict(chainId=31337,nonce=0,to=c['zk_state'],value=0,gas=2000000,gasPrice=1,data=data)
            signed=account.sign_transaction(transaction);txid=Web3.keccak(signed.raw_transaction).hex()
            journal.update(key,'SIGNED',dict(raw_transaction=signed.raw_transaction.hex(),tx_hash=txid,nonce=0))
            e.transactions[txid]=dict(to=c['zk_state'],input=bytes.fromhex(data[2:]),nonce=0)
            e.receipts[txid]=AttributeDict(dict(status=1,blockNumber=10,blockHash=b'h'*32,
                transactionHash=bytes.fromhex(txid.removeprefix('0x'))))
        entry=journal.get(key);result=dict(entry['result'],block_hash=(b'h'*32).hex(),block_number=10)
        if not e.crash:journal.update(key,'CONFIRMED',result)
        e.current=e.after;e.fresh=True
        if e.crash:raise RuntimeError('explicit-completion-write-interruption-fixture')
        return dict(status='VERIFIED')
    w.publish=publish;e.worker=w
    e.request=dict(kind='deposit',consumer=next(iter(c['allowed_consumers'])),txid='ab'*32,vout='0')
    e.counter=0;e.last={}
    def call(phase,action='probe',**overrides):
        e.counter+=1;invocation=tmp_path/('invocation-'+str(e.counter));invocation.mkdir(mode=0o700)
        context=dict(version='1',run_id='aa'*32,plan_sha256='bb'*32,stage=phase,capability=phase,
            attempt_id=digest(['bb'*32,phase]),challenge=f'{e.counter:064x}',action=action,predecessors=[],
            directory=str(tmp_path),response_file=str(invocation/'response.json'),prior={})
        context.update(overrides)
        p=adapter.Pipeline(w,e.request,context,invocation);result=p.execute(action);e.last[phase]=result
        return p,result
    e.call=call
    def prepared():
        e.call('consumer_request','run');p,result=e.call('proof_preparation','run')
        assert result['status']=='SATISFIED';return p.base()
    e.prepared=prepared
    def proved(start='run'):
        for action in (start,'recover','recover'):
            result=e.call('fresh_proof',action)
            if result[1]['status']=='SATISFIED':return result
            assert result[1]['status']=='ABSENT'
        raise AssertionError('fixture stages incomplete')
    e.proved=proved
    yield e
    w.close()


def test_real_worker_boundaries_register_prepare_prove_publish_and_recover_exact_results(pipeline):
    e=pipeline
    assert e.call('consumer_request')[1]['status']=='ABSENT'
    assert e.call('consumer_request','run')[1]['status']=='SATISFIED'
    assert e.call('consumer_request','recover')[1]['status']=='SATISFIED'
    assert len(e.worker.inbox.needs())==1
    _,result=e.call('proof_preparation','run')
    assert result['status']=='SATISFIED' and e.publishes==0 and e.stages==[]
    assert e.call('fresh_proof')[1]['status']=='ABSENT'
    p,result=e.proved()
    assert result['status']=='SATISFIED' and e.stages==['composite','succinct','groth16'] and e.publishes==0
    assert read(p.base()/'proof-result.json')['cache_hit'] is False
    assert e.call('state_update','run')[1]['status']=='WAITING'
    assert e.publishes==1
    assert e.call('state_update')[1]['status']=='SATISFIED'
    assert e.call('state_update','recover')[1]['status']=='SATISFIED' and e.publishes==1
    # The exact proof remains historical completion even when the state advances.
    e.current=list(e.after);e.current[3]='32258'
    assert e.call('fresh_proof')[1]['status']=='SATISFIED'
    assert e.call('state_update')[1]['status']=='SATISFIED'


def test_existing_cache_cannot_satisfy_a_fresh_proof_stage(pipeline):
    e=pipeline;base=e.prepared();prep=read(base/'preparation.json')
    import hashlib
    cache=Path(e.c['directory'])/'receipt-cache';cache.mkdir()
    key=hashlib.sha256(bytes.fromhex(prep['program_image_id'])+(base/'state.journal').read_bytes()).hexdigest()
    (cache/(key+'.bin')).write_bytes(b'old deliberately unverified cache fixture')
    assert e.proved()[1]['status']=='SATISFIED'
    assert e.stages==['composite','succinct','groth16']


def test_unenrolled_stage_artifacts_are_not_adopted_as_fresh(pipeline):
    e=pipeline;base=e.prepared();stage_report(base,'composite')
    assert e.call('fresh_proof')[1]['status']=='RECOVERY_REQUIRED'
    assert e.call('fresh_proof','run')[1]['status']=='RECOVERY_REQUIRED' and e.stages==[]


def test_crash_after_first_stage_resumes_original_local_proving_enrollment(pipeline):
    e=pipeline;e.prepared();e.crash=True
    with pytest.raises(RuntimeError,match='stage-interruption'):e.call('fresh_proof','run')
    e.crash=False
    assert e.proved('recover')[1]['status']=='SATISFIED'
    assert e.stages==['composite','succinct','groth16']


def test_registered_request_and_attempt_cannot_change_under_the_same_plan(pipeline):
    e=pipeline;e.prepared();e.request=dict(e.request,vout='1')
    with pytest.raises(ValueError,match='CONTOUR_WORKER_SCOPE'):e.call('consumer_request')


def test_no_unnecessary_proof_or_publication_for_sufficient_state(pipeline):
    e=pipeline;e.call('consumer_request','run');e.request_status='READY'
    assert e.call('proof_preparation','run')[1]['observations']['reason']=='NO_NEW_STATE_PROOF_REQUIRED'
    assert e.stages==[] and e.publishes==0


def test_prepare_boundary_cannot_publish_an_existing_prepared_attempt(pipeline):
    e=pipeline;e.prepared()
    assert e.worker.advance('prepare')['status']=='PROOF_PREPARED'
    assert e.publishes==0 and e.stages==[]


def test_normal_worker_tick_retains_its_existing_combined_behavior(pipeline):
    e=pipeline;e.worker.request(e.request)
    assert e.worker.tick()['status']=='PROOF_PREPARED'
    assert e.worker.tick()['status']=='STATE_PROGRESS'
    assert e.stages==['composite','succinct','groth16'] and e.publishes==1


def test_independent_participant_compute_never_publishes(pipeline):
    e=pipeline;e.prepared()
    result=e.worker.advance('prove')
    assert result['status']=='LOCAL_PROOF_COMPUTED' and result['published'] is False
    assert e.stages==['composite','succinct','groth16'] and e.publishes==0
    assert e.worker.advance('publish')['status']=='STATE_PROGRESS' and e.publishes==1


def test_peer_publication_does_not_replace_our_prepared_computation(pipeline):
    e=pipeline;e.prepared();e.current=e.after;e.fresh=True
    result=e.worker.advance('prove')
    assert result['status']=='LOCAL_PROOF_COMPUTED' and result['published'] is False
    assert e.stages==['composite','succinct','groth16'] and e.publishes==0


def test_independent_computation_stops_when_halt_is_observed(pipeline):
    e=pipeline;e.prepared()
    _,state,_,_,_=e.worker.context(allow_halt=True)
    state.functions.halted=lambda:NS(call=lambda:True)
    result=e.worker.advance('prove')
    assert result['status']=='HALT_OBSERVED_UNCONFIRMED'
    assert e.stages==[] and e.publishes==0


def test_prepare_stops_before_membership_publication_for_a_fresh_root(pipeline):
    e=pipeline;e.worker.request(e.request);e.fresh=True
    # Explicit diagnostic observation; the ordinary observer's maturity rule is unchanged.
    e.worker.observe_need=lambda *args:dict(status='NEEDS_STATE',height=32256,depth=1,header='fixture')
    assert e.worker.advance('prepare')['status']=='MATERIALIZATION_REQUIRED'
    assert e.publishes==0 and e.stages==[]


def test_paid_service_gate_is_rechecked_before_starting_proving(pipeline,monkeypatch):
    e=pipeline;e.prepared()
    def hold(*args):raise ValueError('ZK_SERVICE_AWAITING_PAYMENT')
    monkeypatch.setattr('signer_agent.zk_worker_service.preflight',hold)
    with pytest.raises(ValueError,match='ZK_SERVICE_AWAITING_PAYMENT'):e.call('fresh_proof','run')
    assert e.stages==[] and e.publishes==0


def test_publish_boundary_only_verifies_and_never_starts_a_prover(pipeline):
    e=pipeline;e.prepared()
    with pytest.raises(ValueError,match='LOCAL_PROOF_ORIGIN_REQUIRED'):e.worker.advance('publish')
    assert e.stages==[] and e.publishes==0


def test_no_unsigned_publication_is_created_by_recovery(pipeline):
    e=pipeline;e.prepared();e.proved()
    assert e.call('state_update','recover')[1]['observations']['reason']=='NO_SIGNED_STATE_TRANSACTION_TO_RECOVER'
    assert e.publishes==0


def test_next_need_promotes_an_already_accepted_previous_attempt_before_scope_check(pipeline):
    e=pipeline;e.prepared();e.proved();e.call('state_update','run')
    assert e.call('state_update')[1]['status']=='SATISFIED' and e.worker.inbox.get('attempt')
    e.request=dict(e.request,vout='1')
    context=dict(run_id='ef'*32,plan_sha256='ed'*32,capability='consumer_request',attempt_id='ec'*32)
    e.call('consumer_request','run',**context)
    _,result=e.call('proof_preparation','run',**dict(context,capability='proof_preparation'))
    assert result['observations']['reason']!='OTHER_WORKER_ATTEMPT'
    assert e.worker.inbox.get('attempt') is None
    assert e.worker.inbox.get('archive:'+e.after[7])


def test_lost_local_completion_recovers_original_signed_envelope(pipeline):
    e=pipeline;e.prepared();e.proved();e.crash=True
    with pytest.raises(RuntimeError,match='completion-write'):e.call('state_update','run')
    assert e.call('state_update')[1]['status']=='ABSENT'
    record=adapter.Pipeline(e.worker,e.request,dict(run_id='aa'*32,plan_sha256='bb'*32,capability='state_update'),e.tmp_path).transaction()
    original=record['result']['raw_transaction'];e.crash=False
    e.call('state_update','recover')
    assert e.call('state_update')[1]['status']=='SATISFIED'
    assert e.publishes==2 and len(e.transactions)==1
    assert e.stages==['composite','succinct','groth16']
    assert Journal(e.worker.inbox.path/'transactions.sqlite').get(record['id'])['result']['raw_transaction']==original


def test_modified_preparation_or_final_receipt_cannot_be_reused(pipeline):
    e=pipeline;base=e.prepared();e.proved()
    (base/'state.input').write_bytes(b'wrong input')
    with pytest.raises(ValueError,match='CONTOUR_WORKER_ARTIFACT_CHANGED'):e.call('fresh_proof')


def test_bitcoin_reorg_before_proving_blocks_resource_spend(pipeline):
    e=pipeline;e.prepared();e.worker.bitcoin.call=lambda *args:'ff'*32
    assert e.call('fresh_proof','run')[1]['observations']['reason']=='PROVING_BITCOIN_CHANGED'
    assert e.stages==[] and e.publishes==0


def test_other_fresh_stage_cannot_borrow_the_original_proof_enrollment(pipeline):
    e=pipeline;e.prepared();e.proved()
    with pytest.raises(ValueError,match='CONTOUR_WORKER_PROOF_OWNER'):e.call('fresh_proof',attempt_id='ff'*32)


def test_accepted_transaction_reorg_blocks_historical_completion(pipeline):
    e=pipeline;e.prepared();e.proved();e.call('state_update','run')
    assert e.call('state_update')[1]['status']=='SATISFIED'
    for key,receipt in e.receipts.items():e.receipts[key]=AttributeDict(dict(receipt,blockHash=b'z'*32))
    with pytest.raises(ValueError,match='EVM_REORG_RECOVERY_REQUIRED'):e.call('state_update')


def test_wrong_calldata_is_not_accepted_from_a_successful_receipt(pipeline):
    e=pipeline;e.prepared();e.proved();e.call('state_update','run')
    for row in e.transactions.values():row['input']=b'wrong calldata'
    with pytest.raises(ValueError,match='CONTOUR_WORKER_TRANSACTION'):e.call('state_update')


def test_controller_and_real_adapter_evidence_contract_share_exact_capabilities(pipeline,monkeypatch):
    e=pipeline;root=e.tmp_path/'controller';root.mkdir(mode=0o700)
    monkeypatch.setattr(adapter,'Worker',lambda c:e.worker)
    monkeypatch.setattr(adapter,'load_config',lambda path:e.c)
    monkeypatch.setattr(e.worker,'close',lambda:None)
    request=e.tmp_path/'request.json';write(request,e.request)
    # Invoke through the controller's actual binding/artifact/state machine. Only
    # process dispatch is replaced; separate controller tests execute real children.
    stages=[dict(id=phase,capability=phase,timeout_seconds=30,probe=['/usr/bin/true'],run=['/usr/bin/true'],
                 recover=['/usr/bin/true']) for phase in adapter.PHASES]
    plan=dict(version='1',run_id='aa'*32,directory=str(root),source_paths=['ops/zkspv/contour_worker.py'],stages=stages)
    runner=Contour(plan)
    def process(command,**kw):
        context_path=Path(kw['env']['SATOHSI_CONTOUR_CONTEXT']);context=read(context_path)
        monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',str(context_path))
        adapter.main(['--phase',context['capability'],'--worker-config','explicit-worker-fixture','--request-file',str(request),
                     '--worker-config-sha256',digest(e.c),'--request-sha256',digest(e.request)])
        return NS(pid=999999999,wait=lambda **kw:0)
    monkeypatch.setattr('ops.zkspv.contour.subprocess.Popen',process)
    for phase in adapter.PHASES:
        result=runner.step()
        if phase=='fresh_proof':
            while result['status']=='ABSENT':result=runner.step()
        assert result['status']=='STAGE_COMPLETED' and result['stage']==phase
    result=runner.step()
    assert result['status']=='DRIVERS_COMPLETED_UNQUALIFIED' and not result['combined_e2e_accepted']
    assert not result['fresh_proving_verified'] and not result['production_ready']


def test_each_proving_invocation_starts_at_most_one_stage(pipeline):
    e=pipeline;e.prepared()
    assert e.call('fresh_proof','run')[1]['status']=='ABSENT'
    assert e.stages==['composite']
    assert e.call('fresh_proof','recover')[1]['status']=='ABSENT'
    assert e.stages==['composite','succinct']
    assert e.call('fresh_proof','recover')[1]['status']=='SATISFIED'
    assert e.stages==['composite','succinct','groth16']


def test_missing_completed_stage_does_not_start_proving_again(pipeline):
    e=pipeline;base=e.prepared();e.proved();(base/'composite/report.json').unlink()
    with pytest.raises(ValueError,match='CONTOUR_WORKER_PROOF_CHANGED'):e.call('fresh_proof','recover')
    assert e.stages==['composite','succinct','groth16']


def test_proof_inspection_failure_cannot_become_state_publication(pipeline):
    e=pipeline;base=e.prepared();e.proved();(base/'groth16/receipt.bin').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='ZK_STAGE_ARTIFACT_DRIFT'):e.call('state_update','run')
    assert e.publishes==0


def test_missing_confirmed_receipt_waits_without_a_new_transaction(pipeline):
    e=pipeline;e.prepared();e.proved();e.call('state_update','run');e.receipts.clear()
    result=e.call('state_update','recover')[1]
    assert result['status']=='WAITING' and result['observations']['reason']=='EVM_RECEIPT_PENDING'
    assert e.publishes==1


def test_unrelated_valid_signed_envelope_cannot_be_rebroadcast_as_state_recovery(pipeline):
    e=pipeline;e.prepared();e.proved();e.call('state_update','run')
    journal=Journal(e.worker.inbox.path/'transactions.sqlite')
    record=next(r for r in journal.list() if r['id'].endswith(':transition'))
    other=e.account.sign_transaction(dict(chainId=31337,nonce=0,to=e.account.address,value=0,gas=21000,gasPrice=1,data='0x'))
    journal.update(record['id'],'BROADCAST',dict(raw_transaction=other.raw_transaction.hex(),tx_hash=Web3.keccak(other.raw_transaction).hex(),nonce=0))
    with pytest.raises(ValueError,match='CONTOUR_WORKER_TRANSACTION'):e.call('state_update','recover')
    assert e.publishes==1


def test_plan_builder_freezes_private_input_hashes_and_keeps_missing_capabilities(pipeline,monkeypatch):
    from ops.zkspv import contour_plan
    e=pipeline;config=e.tmp_path/'worker.json';request=e.tmp_path/'need.json';write(config,e.c);write(request,e.request)
    plan=contour_plan.build(config,request,e.tmp_path/'full-controller')
    assert [s['capability'] for s in plan['stages'] if s['run']]==list(adapter.PHASES)
    assert any(s['capability']=='admission_payment' and s['run'] is None for s in plan['stages'])
    partial=contour_plan.build(config,request,e.tmp_path/'partial-controller',worker_only=True)
    assert [s['capability'] for s in partial['stages']]==list(adapter.PHASES)
    assert digest(e.c) in partial['stages'][0]['run'] and digest(e.request) in partial['stages'][0]['run']
    assert 'ops/zkspv/contour_worker.py' in partial['source_paths']
    runner=Contour(partial)
    assert 'withdrawal_btc' in runner.status()['missing_capabilities']
    invocation=e.tmp_path/'input-pin-test';invocation.mkdir();context=invocation/'context.json'
    write(context,dict(capability='consumer_request',action='run'))
    monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',str(context))
    write(request,dict(e.request,vout='1'))
    with pytest.raises(ValueError,match='CONTOUR_WORKER_SCOPE'):
        adapter.main(partial['stages'][0]['run'][3:])


def test_capability_mismatch_is_rejected_before_constructing_worker(pipeline,monkeypatch):
    e=pipeline;path=e.tmp_path/'context.json';write(path,dict(capability='state_update'))
    need=e.tmp_path/'need.json';write(need,e.request);monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',str(path))
    with pytest.raises(ValueError,match='CONTOUR_CAPABILITY'):
        adapter.main(['--phase','fresh_proof','--worker-config','unused','--request-file',str(need),
                      '--worker-config-sha256',digest(e.c),'--request-sha256',digest(e.request)])


def test_new_session_prover_inherits_original_worker_lock(tmp_path,monkeypatch):
    import fcntl,os,subprocess,sys
    from ops.zkspv import state_trial_prove
    path=tmp_path/'worker.lock';observed=tmp_path/'observed'
    with path.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);descriptor=lock.fileno()
        monkeypatch.setenv('SATOSHIFI_PROVER_LOCK_FD',str(descriptor))
        # This checks descriptor inheritance with a real child, not sandbox policy.
        monkeypatch.setattr('signer_agent.zk_isolation.launch',lambda command,base,env,**kw:(command,env))
        script='import os,sys;from pathlib import Path;Path(sys.argv[2]).write_text(str(os.fstat(int(sys.argv[1])).st_ino))'
        result=state_trial_prove.run([sys.executable,'-c',script,str(descriptor),str(observed)],tmp_path/'log',
                                     '(version 1) (allow default)',dict(os.environ),5)
        assert result['exit_code']==0 and int(observed.read_text())==os.fstat(descriptor).st_ino
