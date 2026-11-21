"""Original staged control, EOA envelopes and archives; Core/EVM/proofs are explicit fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import zipfile
from types import SimpleNamespace as NS

import pytest
from web3 import Web3
from web3.datastructures import AttributeDict

from ops.zkspv import contour_worker_cycle as cycle,contour_worker
from ops.zkspv.contour import digest
from signer_agent.operations import Journal
from signer_agent.zk_worker_prover import read,write
from signer_agent.bitcoin_network import header_hash
from test_contour_worker import pipeline
from test_zk_worker import fixture_headers


@pytest.fixture
def chunks(pipeline,request):
    e=pipeline;w=e.worker;headers=fixture_headers();target=32259;w.c['bitcoin_policy']['max_headers_per_sync']='1'
    if hasattr(request,'param'):w.c['max_attempts']=str(request.param)
    previous=w.context;w.context=lambda **kw:(*previous(**kw)[:4],dict(blocks=target))
    w.observe_need=lambda *args:dict(status='NEEDS_STATE',height=32257,depth=3,header=headers[32257].hex())
    native=w.native
    def prepare(archive,new,heights,out):
        end=next(n for n,raw in headers.items() if raw.hex()==new[-1]);e.before=list(e.current);e.after=list(e.before)
        e.after[1]=e.after[3]=str(end);e.after[2]=header_hash(headers[end]).hex();e.after[7]=digest(['root',end]);e.after[11]=str(int(e.before[11])+1)
        result=native(archive,new,heights,out);result['archive']=list(archive)+new;write(out/'native.json',result);return result
    w.native=prepare
    def publish(name,step,records):
        e.publishes+=1;journal=Journal(w.inbox.path/'transactions.sqlite');key=name+':transition';row=journal.put(key,step)
        if not row['result'].get('raw_transaction'):
            state=w.context()[1];data=state.functions.submitTransition(bytes.fromhex(step['args'][0][2:]),bytes.fromhex(step['args'][1][2:]))._encode_transaction_data()
            nonce=len(e.transactions);tx=dict(chainId=31337,nonce=nonce,to=e.c['zk_state'],value=0,gas=2000000,gasPrice=1,data=data)
            raw=e.account.sign_transaction(tx).raw_transaction;txid=Web3.keccak(raw).hex()
            journal.update(key,'SIGNED',dict(raw_transaction=raw.hex(),tx_hash=txid,nonce=nonce))
            e.transactions[txid]=dict(to=e.c['zk_state'],input=bytes.fromhex(data[2:]),nonce=nonce)
            e.receipts[txid]=AttributeDict(dict(status=1,blockNumber=10,blockHash=b'h'*32,
                transactionHash=bytes.fromhex(txid.removeprefix('0x'))))
        row=journal.get(key);journal.update(key,'CONFIRMED',dict(row['result'],block_hash=(b'h'*32).hex(),block_number=10))
        active=w.inbox.get('attempt');e.current=read(w.inbox.path/'attempts'/active['id']/'native.json')['after'];e.fresh=int(e.current[3])==target
        return dict(status='VERIFIED')
    w.publish=publish;e.prepared()
    def call(action='recover',phase='fresh_proof'):
        e.counter+=1;folder=e.tmp_path/('cycle-'+str(e.counter));folder.mkdir(mode=0o700)
        ctx=dict(version='1',run_id='aa'*32,plan_sha256='bb'*32,stage=phase,capability=phase,
            attempt_id=digest(['bb'*32,phase]),challenge=f'{e.counter:064x}',action=action,predecessors=[],
            directory=str(e.tmp_path),response_file=str(folder/'response.json'),prior={})
        p=cycle.Pipeline(w,e.request,ctx,folder);return p,p.execute(action)
    e.cycle=call;return e


def complete(e):
    for _ in range(32):
        p,result=e.cycle()
        if result['status']=='SATISFIED':return p,result
        assert result['status'] in ('ABSENT','WAITING'),result
    raise AssertionError(result)


def test_consumer_catches_up_across_three_proofs_before_membership_and_releases_attempt(chunks):
    e=chunks;p,result=complete(e)
    assert result['observations']['chunks']==3 and e.stages==['composite','succinct','groth16']*3
    assert e.publishes==3 and len(e.transactions)==3 and sorted(t['nonce'] for t in e.transactions.values())==[0,1,2]
    assert e.worker.inbox.get('attempt') is None and len(e.worker.inbox.get('archive:'+e.current[7]))==3
    assert e.cycle('probe','state_update')[1]['status']=='SATISFIED'
    assert e.cycle('probe')[1]['status']=='SATISFIED' and e.publishes==3
    # The preparation predecessor still binds its first chunk, despite later progress.
    assert e.call('proof_preparation')[1]['status']=='SATISFIED'


def test_all_chunks_enrollments_inputs_and_original_envelopes_are_exported(chunks):
    e=chunks;p,result=complete(e);contour_worker.emit(p.context,p.invocation,result,p)
    response=json.loads(Path(p.context['response_file']).read_text())
    archive=next(e.tmp_path/name for name in response['artifacts'] if name.endswith('proof-lineage.zip'))
    with zipfile.ZipFile(archive) as z:
        for i in range(3):
            assert z.read(f'{i}/groth16/receipt.bin')==b'groth16'
            assert json.loads(z.read(f'{i}/original-state-transaction.json'))['status']=='CONFIRMED'
            assert json.loads(z.read(f'{i}/contour-enrollment.json'))['proof_started']


def test_partial_chunk_does_not_complete_state_update(chunks):
    e=chunks
    for _ in range(4):e.cycle()
    assert e.publishes==1 and e.cycle('probe','state_update')[1]['status']=='WAITING'
    assert e.cycle('probe')[1]['status']=='ABSENT'


def test_missing_historical_receipt_waits_without_starting_further_chunks(chunks):
    e=chunks
    for _ in range(6):e.cycle()
    p,_=e.cycle('probe');assert p.cycle['history']
    original=deepcopy(e.worker.inbox.get(p.key));e.receipts.clear()
    result=e.cycle()[1]
    assert result['status']=='WAITING' and result['observations']['reason']=='EVM_RECEIPT_PENDING'
    assert e.worker.inbox.get(p.key)==original and len(e.stages)==3


@pytest.mark.parametrize('chunks',[1],indirect=True)
def test_attempt_limit_preserves_signed_chunk_and_never_resets_spend(chunks):
    e=chunks
    for _ in range(4):e.cycle()
    p,result=e.cycle()
    assert result['status']=='RECOVERY_REQUIRED' and result['observations']['reason']=='CONSUMER_CATCHUP_ATTEMPT_LIMIT'
    assert len(e.stages)==3 and e.publishes==1 and p.lease['proof_started']


def test_crash_before_unsigned_publication_requires_exact_original_recovery(chunks):
    e=chunks
    for _ in range(3):p,_=e.cycle()
    p.lease['publish_started']=True;p.save()
    assert e.cycle()[1]['status']=='RECOVERY_REQUIRED' and e.publishes==0


def test_mid_proof_restart_reuses_original_proving_owner_and_never_cache_substitutes(chunks):
    e=chunks;e.crash=True
    with pytest.raises(RuntimeError,match='stage-interruption'):e.cycle()
    e.crash=False;assert complete(e)[1]['status']=='SATISFIED'
    assert len(e.stages)==9 and e.publishes==3


def test_suite_cli_cycle_joins_original_preparation_and_chunked_publication(chunks,monkeypatch):
    import subprocess
    from ops.zkspv import contour_suite_plan as factory
    from ops.zkspv.contour_suite import Suite
    e=chunks;home=e.tmp_path/'suite-cli';wc=e.tmp_path/'worker.json';need=e.tmp_path/'need.json'
    write(wc,e.worker.c);write(need,e.request)
    spec=dict(role='proof',worker_config=str(wc),request_file=str(need),service_config=None,proof_policy='FRESH')
    profile,child=factory.compile_job(spec,'consumer',home/'jobs'/'consumer')
    # Explicitly partial TEST_ONLY control-chain: membership/real chain effects
    # have their own adapter tests and are not claimed by this fixture.
    child['stages']=[s for s in child['stages'] if s['capability']!='membership']
    plan=dict(version='1',run_id='12'*32,directory=str(home),mode='TEST_ONLY',source_paths=child['source_paths'],input_sha256={},
        jobs=[dict(id='consumer',depends_on=[],spec={},profile=profile,plan=child)],cases={})
    monkeypatch.setattr(contour_worker,'Worker',lambda c:e.worker);monkeypatch.setattr(cycle,'Worker',lambda c:e.worker)
    monkeypatch.setattr(e.worker,'close',lambda:None);original=subprocess.Popen
    def popen(command,*args,**kwargs):
        if command[1:3] in (['-m','ops.zkspv.contour_worker'],['-m','ops.zkspv.contour_worker_cycle']):
            monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',kwargs['env']['SATOHSI_CONTOUR_CONTEXT'])
            (cycle if command[2].endswith('_cycle') else contour_worker).main(command[3:])
            return NS(pid=999999999,wait=lambda **kw:0)
        return original(command,*args,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',popen)
    s=Suite(plan)
    for _ in range(36):
        stages,publishes=len(e.stages),e.publishes;result=s.step()
        assert len(e.stages)-stages<=1 and e.publishes-publishes<=1
        if result['status']=='EXECUTION_COMPLETE_UNQUALIFIED':break
    else:raise AssertionError(result)
    report=s.collect();assert report['jobs']['consumer']=='VERIFIED'
    assert len(e.stages)==9 and e.publishes==3 and report['artifact_sha256']
    assert not report['combined_e2e_accepted'] and not report['typed_scenario_bindings']
