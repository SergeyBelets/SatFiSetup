"""Original worker/prover/publication recovery with explicit Core/EVM/proving fixtures."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
import json
import zipfile

import pytest

from ops.zkspv import contour_heartbeat as heartbeat
from ops.zkspv import contour_worker
from ops.zkspv.contour import digest
from signer_agent.zk_worker_prover import read
from test_contour_worker import pipeline


@pytest.fixture
def own(pipeline,monkeypatch):
    e=pipeline;w=e.worker;w.c['heartbeat']=dict(enabled=True)
    _,state,_,_,_=w.context();state.functions.hasUsefulUpdate=lambda:NS(call=lambda **kw:True)
    monkeypatch.setattr('signer_agent.zk_worker_heartbeat.observe',lambda *a:dict(service_job_id='heartbeat:fixture',deadline=1600000000,broadcast_not_before=0))
    def call(action='probe',require_proof=False):
        e.counter+=1;folder=e.tmp_path/('heartbeat-'+str(e.counter));folder.mkdir(mode=0o700)
        context=dict(version='1',run_id='cc'*32,plan_sha256='dd'*32,stage='fresh',capability='state_update',
            attempt_id=digest(['dd'*32,'fresh']),challenge=f'{e.counter:064x}',action=action,predecessors=[],
            directory=str(e.tmp_path),response_file=str(folder/'response.json'),prior={})
        p=heartbeat.Pipeline(w,context,folder,require_proof);return p,p.execute(action)
    e.heartbeat=call;return e


def complete(e,require=False):
    for action in ['run']+['recover']*10:
        p,r=e.heartbeat(action,require)
        if r['status']=='SATISFIED':return p,r
        assert r['status'] in ('ABSENT','WAITING'),r
    raise AssertionError(r)


def test_canonical_existing_fresh_state_is_adopted_without_claiming_proving(own):
    e=own;e.fresh=True;p,r=e.heartbeat()
    assert r['status']=='SATISFIED' and r['observations']['fresh_proving_performed'] is False
    assert not p.lease and e.stages==[] and e.publishes==0


def test_initial_fresh_requires_proof_then_exact_publication_before_completion(own):
    e=own;p,r=complete(e)
    assert r['observations']['fresh_proving_performed'] is True
    assert e.stages==['composite','succinct','groth16'] and e.publishes==1
    assert read(p.base()/'proof-result.json')['cache_hit'] is False
    assert e.heartbeat()[1]['status']=='SATISFIED' and e.publishes==1
    assert e.worker.inbox.get('attempt') is None
    assert e.worker.inbox.get('archive:'+e.after[7])


def test_force_fresh_proof_does_not_adopt_an_existing_root(own):
    e=own;e.fresh=True
    assert e.heartbeat(require_proof=True)[1]['status']=='ABSENT'
    assert complete(e,True)[1]['status']=='SATISFIED'
    assert len(e.stages)==3 and e.publishes==1


def test_completed_local_proof_is_not_a_completed_fresh_job(own):
    e=own;e.heartbeat('run')
    for _ in range(3):p,r=e.heartbeat('recover')
    assert r['status']=='ABSENT' and r['observations']['reason']=='HEARTBEAT_PROOF_VERIFIED_PUBLICATION_REQUIRED'
    assert e.publishes==0
    assert e.heartbeat()[1]['status']=='ABSENT' and e.publishes==0


def test_unsigned_publication_recovery_holds_without_allocating_nonce(own):
    e=own;e.heartbeat('run')
    for _ in range(3):p,_=e.heartbeat('recover')
    p.lease['publish_started']=True;p.save()  # Crash after intent, before original EOA signature.
    assert e.heartbeat('recover')[1]['observations']['reason']=='NO_SIGNED_STATE_TRANSACTION_TO_RECOVER'
    assert e.publishes==0


def test_prover_interruption_resumes_same_chunk_and_enrollment(own):
    e=own;e.heartbeat('run');e.crash=True
    with pytest.raises(RuntimeError,match='stage-interruption'):e.heartbeat('run')
    e.crash=False
    assert complete(e)[1]['status']=='SATISFIED'
    assert e.stages==['composite','succinct','groth16'] and e.publishes==1


def test_receipt_loss_or_freshness_loss_cannot_grant_completion(own):
    e=own;complete(e);e.fresh=False
    historical=e.heartbeat()[1]
    assert historical['status']=='SATISFIED' and historical['observations']['current_fresh'] is False
    e.fresh=True;e.receipts.clear()
    assert e.heartbeat()[1]['status']=='RECOVERY_REQUIRED'


def test_evidence_carries_binary_receipt_inputs_reports_and_exact_signed_envelope(own):
    e=own;p,r=complete(e);contour_worker.emit(p.context,p.invocation,r,p)
    response=json.loads(Path(p.context['response_file']).read_text())
    archive=next(Path(p.context['directory'])/n for n in response['artifacts'] if n.endswith('proof-lineage.zip'))
    with zipfile.ZipFile(archive) as z:
        assert z.read('0/groth16/receipt.bin')==b'groth16'
        assert z.read('0/state.input') and z.read('0/native.json') and z.read('0/preparation.json')
        entry=json.loads(z.read('0/original-state-transaction.json'))
        assert entry['status']=='CONFIRMED' and entry['result']['raw_transaction']


def test_catchup_history_validation_does_not_overwrite_active_enrollment(own):
    e=own;p,_=complete(e);past=deepcopy(p.lease);p.lease['history']=[past];p.save()
    before=deepcopy(e.worker.inbox.get(p.key));p.check_history()
    assert e.worker.inbox.get(p.key)==before and p.lease==before
    past['attempt']['files']['state.input']='00'*32;p.lease['history']=[past]
    with pytest.raises(ValueError,match='ARTIFACT_CHANGED'):p.check_history()
    assert e.worker.inbox.get(p.key)==before


def test_partial_accepted_chunk_is_promoted_and_retained_before_next_preparation(own):
    e=own;original=e.worker.publish
    def partial(*args):
        result=original(*args);e.fresh=False;return result
    e.worker.publish=partial
    e.heartbeat('run')
    for _ in range(3):e.heartbeat('recover')
    e.heartbeat('recover')  # Exact original publication; fixture accepts only a partial chunk.
    assert e.publishes==1
    p,result=e.heartbeat('recover')
    assert result['observations']['reason']=='HEARTBEAT_NEXT_CATCHUP_CHUNK_REQUIRED'
    assert len(p.lease['history'])==1 and 'attempt' not in p.lease
    assert e.worker.inbox.get('attempt') is None
    assert e.worker.inbox.get('archive:'+e.after[7])
    p.check_history();assert e.worker.inbox.get(p.key)==p.lease


def test_catchup_attempt_limit_cannot_reset_original_budget_or_start_another_chunk(own):
    e=own;e.worker.c['max_attempts']='1';original=e.worker.publish
    def partial(*args):
        result=original(*args);e.fresh=False;return result
    e.worker.publish=partial;e.heartbeat('run')
    for _ in range(4):e.heartbeat('recover')
    p,result=e.heartbeat('recover')
    assert result['status']=='RECOVERY_REQUIRED' and result['observations']['reason']=='HEARTBEAT_CATCHUP_ATTEMPT_LIMIT'
    assert e.publishes==1 and len(e.stages)==3 and p.lease['attempt']


def test_completed_heartbeat_can_feed_a_new_consumer_without_external_worker_tick(own):
    from test_zk_worker import fixture_headers
    from signer_agent.bitcoin_network import header_hash
    e=own;complete(e);e.before=list(e.current);e.after=list(e.before)
    header=fixture_headers()[32258];e.after[1]=e.after[3]='32258';e.after[2]=header_hash(header).hex();e.after[7]='66'*32;e.after[11]='3'
    original=e.worker.context
    e.worker.context=lambda **kw:(*original(**kw)[:4],dict(blocks=32258))
    e.worker.observe_need=lambda *args:dict(status='NEEDS_STATE',height=32258,depth=1,header=header.hex())
    assert e.call('consumer_request','run')[1]['status']=='SATISFIED'
    p,r=e.call('proof_preparation','run')
    assert r['status']=='SATISFIED' and p.lease['attempt']['target_height']==32258
    assert e.publishes==1 and len(e.stages)==3
