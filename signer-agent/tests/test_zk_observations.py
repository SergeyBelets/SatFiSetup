"""Real SQLite/WAL; linked synthetic Core headers model the observation boundary.

These headers are not consensus/proof qualification and are never submitted.
"""
import json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from signer_agent.zk_observations import Log,validate
from signer_agent.zk_worker import Inbox
from signer_agent.bitcoin_network import header_hash
from test_zk_worker import config


def policy():return dict(version='1',risk_policy='LOCAL_FENCE_RETAIN_MAINNET_GUEST_V1',max_reorg_depth='100',max_branch_headers='4096',max_events='10000',max_bytes='16777216',maintenance_seconds='3600')


def box(tmp_path):
    tmp_path.chmod(0o700);c=config(tmp_path);c['observations']=policy();return Inbox(c)


class Core:
    def __init__(self):self.headers={};self.tip=None;self.missing=set()
    def extend(self,parent,count,label):
        previous=self.headers[parent][0] if parent else -1;h=parent or '00'*32
        for i in range(count):
            raw=(2).to_bytes(4,'little')+bytes.fromhex(h)[::-1]+bytes.fromhex(label*32)+(previous+i+1).to_bytes(4,'little')+bytes.fromhex('ffff7f20')+i.to_bytes(4,'little')
            h=header_hash(raw)[::-1].hex();self.headers[h]=(previous+i+1,raw)
        self.tip=h;return h
    def call(self,method,*args):
        if method=='getbestblockhash':return self.tip
        if method=='getblockheader':
            if args[0] in self.missing:raise ValueError('ARCHIVE_UNAVAILABLE_FIXTURE')
            return self.headers[args[0]][1].hex()
        raise AssertionError(method)
    def info(self):return dict(bestblockhash=self.tip,blocks=self.headers[self.tip][0],chainwork='ff'*32)


def evm(tip='aa'*32,at=100):return dict(number=7,hash='bb'*32,timestamp=at,state=['ANCHOR_FIXTURE',tip])


def test_detect_outbox_crash_before_enqueue_recovers_same_need_and_deadline(tmp_path):
    a=box(tmp_path);log=Log(a);request=dict(kind='deposit',consumer='original',txid='11'*32,vout='0')
    key=log.detect_need(request,'NEEDS_STATE',100,160,True)
    assert not a.needs();a.close()
    b=Inbox(log.box.config);again=Log(b);again.drain()
    assert b.needs()==[(key,request,'NEEDS_STATE')]
    assert again.detect_need(request,'READY',999,9999,True)==key
    again.drain();assert b.get('obligation:'+key)==dict(created_at=100,deadline=160,origins=dict(local=160))
    assert len(again.events('NEED_DETECTED'))==1;b.close()


def test_queue_capacity_keeps_pending_detection_and_no_partial_obligation(tmp_path):
    b=box(tmp_path);b.config['max_pending']='1';log=Log(b);b.enqueue(dict(occupied=True))
    key=log.detect_need(dict(new=True),'NEEDS_STATE',1,60,True)
    with pytest.raises(ValueError,match='INBOX_FULL'):log.drain()
    assert b.get('obligation:'+key) is None
    assert b.db.execute('SELECT status FROM core_observation_outbox WHERE id=?',(key,)).fetchone()[0]=='PENDING'
    b.db.execute('DELETE FROM needs');log.drain();assert b.get('obligation:'+key)['deadline']==60;b.close()


@pytest.mark.parametrize('depth',[0,1,99,100,101,150])
def test_retained_fork_depth_survives_competing_evm_anchor(tmp_path,depth):
    b=box(tmp_path);log=Log(b);core=Core();base=core.extend(None,1851,'11');a=core.extend(base,150,'22')
    log.observe(core,core.info(),evm('OLD_EVM_ANCHOR_FIXTURE'))
    path={height:h for h,(height,_) in core.headers.items() if height<=2000}
    fork=path[2000-depth];core.extend(fork,depth+1,'33')
    # EVM already accepts the competing B branch. Old anchor would hide >100.
    log.observe(core,core.info(),evm(core.tip,at=110))
    processed=log.events('CORE_PROCESSED')[-1][1]['payload']
    if depth<=100:
        log.fence();assert processed['reorg_depth']==depth
    else:
        with pytest.raises(ValueError,match='KNOWN_DEEP_REORG'):log.fence()
        assert processed['reorg_depth']>100
        core.extend(core.tip,1,'44');log.observe(core,core.info(),evm(core.tip,at=120))
        with pytest.raises(ValueError,match='KNOWN_DEEP_REORG'):log.fence()
        assert log.events('INCIDENT')
    assert log.events('CORE_PROCESSED')[0][1]['payload']['tip']==a;b.close()


def test_never_processed_orphan_witness_is_not_silently_skipped(tmp_path):
    b=box(tmp_path);log=Log(b);core=Core();a=core.extend(None,250,'11');core.missing.add(a)
    with pytest.raises(ValueError,match='ARCHIVE_UNAVAILABLE'):log.observe(core,core.info(),evm())
    assert log.events('CORE_DETECTED') and not log.events('CORE_PROCESSED')
    old=a;core.extend(a,1,'22')
    with pytest.raises(ValueError,match='ARCHIVE_UNAVAILABLE'):log.observe(core,core.info(),evm(core.tip))
    with pytest.raises(ValueError,match='WITNESS_REQUIRED'):log.fence()
    core.missing.clear();log.observe(core,core.info(),evm(core.tip));log.fence()
    assert [e['payload']['tip'] for _,e in log.events('CORE_PROCESSED')]==[old,core.tip];b.close()


def test_evm_a_to_b_to_a_and_core_a_to_b_to_a_are_retained(tmp_path):
    b=box(tmp_path);log=Log(b);core=Core();a=core.extend(None,200,'11');log.observe(core,core.info(),evm('A'))
    parent=core.headers[a][1][4:36][::-1].hex();core.extend(parent,2,'22');log.observe(core,core.info(),evm('B'))
    core.tip=a;log.observe(core,core.info(),evm('A'))
    assert len(log.events('CORE_DETECTED'))==3
    assert [e['payload']['state'][1] for _,e in log.events('EVM_OBSERVED')]==['A','B','A'];b.close()


def test_rollback_of_worker_database_is_restored_from_independent_original_lane(tmp_path):
    b=box(tmp_path);cfg=b.config;log=Log(b);log.detect_need(dict(a=1),'READY',10,20,True);log.drain();b.close()
    # A fresh worker directory/old DB cannot erase processed observations.
    cfg=dict(cfg,directory=str(tmp_path/'restored'));c=Inbox(cfg);recovered=Log(c);recovered.drain()
    assert len(c.needs())==1 and c.get('obligation:'+c.needs()[0][0])['deadline']==20
    c.close()


def test_hash_chain_conflict_and_policy_downgrade_fail_closed(tmp_path):
    b=box(tmp_path);cfg=b.config;log=Log(b);log.append('EVM_OBSERVED',evm())
    b.db.execute("UPDATE core_observations SET body='{}' WHERE seq=1")
    with pytest.raises(ValueError,match='INTEGRITY'):Log(b)
    b.close();legacy=dict(cfg);legacy.pop('observations')
    with pytest.raises(ValueError,match='POLICY_CHANGED'):Inbox(legacy)


def test_maintenance_uses_original_fixed_deadline_without_payment_or_heartbeat_reset(tmp_path):
    b=box(tmp_path);log=Log(b);core=Core();core.extend(None,250,'11');log.observe(core,core.info(),evm(at=100))
    a=log.maintenance(NS(timestamp=200),['','','','10'],False)
    c=log.maintenance(NS(timestamp=9999),['','','','10'],False)
    assert a==c and a['deadline']==3700 and a['service_job_id'].startswith('maintenance:')
    assert log.maintenance(NS(timestamp=200),['','','','249'],True) is None
    assert b.get('heartbeat_anchors') is None and not b.get('reservations');b.close()


def test_crash_between_processed_deep_fork_and_incident_marker_never_unfences(tmp_path,monkeypatch):
    b=box(tmp_path);log=Log(b);core=Core();base=core.extend(None,1851,'11');core.extend(base,150,'22')
    log.observe(core,core.info(),evm());core.extend(base,151,'33')
    append=log.append
    def crash(kind,payload):
        if kind=='INCIDENT':raise SystemExit('fixture after durable processed observation')
        return append(kind,payload)
    monkeypatch.setattr(log,'append',crash)
    with pytest.raises(SystemExit):log.observe(core,core.info(),evm(core.tip))
    assert not log.events('INCIDENT')
    recovered=Log(b)
    with pytest.raises(ValueError,match='KNOWN_DEEP_REORG'):recovered.fence()
    b.close()


def test_fresh_directory_cannot_disable_enrolled_observations(tmp_path):
    b=box(tmp_path);Log(b);cfg=dict(b.config,directory=str(tmp_path/'new-directory'));b.close();cfg.pop('observations')
    with pytest.raises(ValueError,match='POLICY_CHANGED'):Inbox(cfg)


def test_two_observers_refresh_the_shared_chain_without_duplicate_queue_deadline(tmp_path):
    a=box(tmp_path);b=Inbox(a.config);one,two=Log(a),Log(b)
    core=Core();core.extend(None,200,'11');one.observe(core,core.info(),evm())
    two.observe(core,core.info(),evm());assert len(two.events('CORE_DETECTED'))==1
    key=one.detect_need(dict(a=1),'READY',1,61,True);two.detect_need(dict(a=1),'READY',99,999,True)
    two.drain();assert a.get('obligation:'+key)['deadline']==61 and len(a.needs())==1
    a.close();b.close()


def test_large_private_backup_extends_history_without_rollback_or_deadline_renewal(tmp_path):
    b=box(tmp_path);log=Log(b);core=Core();core.extend(None,250,'11');log.observe(core,core.info(),evm())
    for i in range(2):core.extend(core.tip,1,'22');log.observe(core,core.info(),evm(at=110+i))
    log.detect_need(dict(a=1),'NEEDS_STATE',1,61,True);out=tmp_path/'observation-backup.json';saved=log.backup(out)
    assert out.stat().st_size>65536
    # A backup does not include any private signing material or EOA envelopes.
    text=out.read_text();assert 'private_key' not in text and 'raw_transaction' not in text
    restored=log.restore(out,saved['sha256']);assert not restored['completeness_proven'] and len(b.needs())==1
    later=log.append('EVM_OBSERVED',evm('later'));log.restore(out,saved['sha256']);assert log.rows[-1][1]==later
    with pytest.raises(FileExistsError):log.backup(out)
    with pytest.raises(ValueError,match='BACKUP_CONFLICT'):log.restore(out,'00'*32)
    b.close()


def test_long_append_witness_is_resumed_in_bounded_durable_chunks(tmp_path):
    b=box(tmp_path);b.config['observations']['max_branch_headers']='102';log=Log(b);core=Core();core.extend(None,200,'11')
    log.observe(core,core.info(),evm());core.extend(core.tip,500,'22')
    attempts=0
    while True:
        try:log.observe(core,core.info(),evm(at=110));break
        except ValueError as exc:
            assert str(exc)=='ZK_OBSERVATION_WITNESS_REQUIRED';attempts+=1
            log=Log(b)  # Crash/restart, including retained partial branch bytes.
            with pytest.raises(ValueError,match='WITNESS_REQUIRED'):log.fence()
    assert attempts==4
    processed=log.events('CORE_PROCESSED')[-1][1]['payload']
    assert processed['reorg_depth']==0 and len(processed['headers'])==102 and len(processed['witness_chunks'])==5
    log.fence();b.close()


def test_full_unpaid_consumer_queue_does_not_veto_observation_maintenance(tmp_path):
    b=box(tmp_path);b.config['max_pending']='1';log=Log(b);b.enqueue(dict(occupied=True))
    log.detect_need(dict(waiting=True),'NEEDS_STATE',1,61,True)
    core=Core();core.extend(None,200,'11');log.observe(core,core.info(),evm())
    assert log.maintenance(NS(timestamp=100),['','','','0'],False)['service_job_id'].startswith('maintenance:')
    assert b.get('observation_outbox_status')['status']=='QUEUE_FULL'
    with pytest.raises(ValueError,match='INBOX_FULL'):log.detect_need(dict(another=True),'NEEDS_STATE',2,62,True)
    assert len(log.events('NEED_DETECTED'))==1;b.close()


def test_completed_need_remains_inspectable_but_new_work_is_fenced(tmp_path):
    from signer_agent.zk_worker import Worker
    b=box(tmp_path);log=Log(b);log.append('INCIDENT',dict(code='ZK_OBSERVATION_KNOWN_DEEP_REORG'))
    w=Worker.__new__(Worker);w.c=b.config;w.inbox=b;w._observations=log
    w.context=lambda **kw:(NS(timestamp=100),None,None,None,{})
    w.stable=lambda b:None;w.observe_need=lambda *a:dict(status='COMPLETED')
    assert w.request(dict(kind='completed'))['status']=='COMPLETED' and not b.needs()
    w.observe_need=lambda *a:dict(status='NEEDS_STATE')
    with pytest.raises(ValueError,match='KNOWN_DEEP_REORG'):w.request(dict(kind='new'))
    b.close()


def test_v7_enrollment_preserves_v6_ids_spending_and_local_deadline(tmp_path):
    from signer_agent.zk_worker import load_config,Worker
    from signer_agent.zk_worker_prover import write
    from test_address_mint import address
    c=config(tmp_path);template=json.loads(Path('config/zkspv-worker.example.json').read_text())
    for k in ('service','heartbeat','transport','batching','archive','automation'):c[k]=template[k]
    pool=next(iter(c['allowed_consumers']));c.update(version='6',transport=None,
        archive=dict(directory=str(tmp_path/'archive'),max_headers='200000',max_bytes='1073741824'),
        address_deposits=dict(version='1',registries={pool:address(4)}),withdrawal_fees=dict(version='1',registries={pool:address(5)}))
    b=Inbox(c);request=dict(kind='deposit',consumer=pool,txid='11'*32,vout='0');key=b.enqueue(request)
    b.reserve('original-transaction',5);b.put('obligation:'+key,dict(created_at=10,deadline=70,origins=dict(local=70)));b.close()
    c.update(version='7',observations=policy());path=tmp_path/'v7.json';write(path,c);assert load_config(path)==c
    w=Worker.__new__(Worker);w.c=c;w.inbox=Inbox(c);w._observations=Log(w.inbox)
    w.context=lambda **kw:(NS(timestamp=999),None,None,None,{})
    w.stable=lambda b:None;w.observe_need=lambda *a:dict(status='NEEDS_STATE')
    assert w.request(request)['local_need_id']==key
    assert w.inbox.get('obligation:'+key)['deadline']==70
    assert w.inbox.db.execute('SELECT cost FROM reservations').fetchone()[0]=='5';w.inbox.close()
