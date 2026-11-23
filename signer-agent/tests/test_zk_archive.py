"""Permanent archive checks with genuine native mainnet replay; EVM boundary faked."""
from pathlib import Path
from types import SimpleNamespace as NS
import shutil
import sqlite3
import pytest
from signer_agent.zk_worker import Worker,Inbox
from signer_agent.zk_archive import ArchiveStore
from signer_agent.zk_archive_ops import backup,restore
from test_zk_worker import config,fixture_headers,fake_state


def worker(tmp_path,directory='worker',archive='archive'):
    c=config(tmp_path);c['directory']=str(tmp_path/directory)
    if not Path(c['host_binary']).exists():pytest.skip('Build worker-host first')
    c['archive']=dict(directory=str(tmp_path/archive),max_headers='200000',max_bytes=str(2**30))
    w=Worker.__new__(Worker);w.c=c;w.inbox=Inbox(c);w.lock_fd=None
    return w


def build(w,tmp_path):
    headers=fixture_headers()
    initial=w.native([],[],[],tmp_path/'initial')['before']
    ref=w.archive_store().ref(initial)
    first=w.native(ref,[headers[32257].hex()],[32257],tmp_path/'first')
    assert not first['resident_cache_hit'] and len(first['delta'])==1
    ref=w.archive_store().promote(initial,first['after'],first['delta'],dict(test=True))
    second=w.native(ref,[headers[32258].hex()],[32257],tmp_path/'second')
    assert second['resident_cache_hit']
    w.archive_store().promote(first['after'],second['after'],second['delta'],dict(test=True))
    return initial,first,second


def test_resident_incremental_archive_matches_legacy_and_cold_replay(tmp_path):
    w=worker(tmp_path);initial,first,second=build(w,tmp_path)
    headers=fixture_headers()
    legacy=w.native([headers[32257].hex()],[headers[32258].hex()],[32257],tmp_path/'legacy')
    assert legacy['after']==second['after']
    assert (tmp_path/'legacy/state.input').read_bytes()==(tmp_path/'second/state.input').read_bytes()
    assert (tmp_path/'legacy/state.journal').read_bytes()==(tmp_path/'second/state.journal').read_bytes()
    w.close();w=worker(tmp_path)
    ref=w.archive_store().ref_existing(second['after'])
    loaded=w.native(ref,[],[32257],tmp_path/'cold')
    assert not loaded['resident_cache_hit'] and loaded['before']==second['after']
    assert loaded['memberships']==second['memberships']
    assert ref.contains(__import__('signer_agent.bitcoin_network',fromlist=['header_hash']).header_hash(headers[32257])[::-1].hex())
    w.close()


def context(w,state):
    target=NS(functions=NS(getState=lambda:NS(call=lambda **kw:fake_state(state))))
    block=NS(number=99,hash=bytes(32));w.context=lambda **kw:(block,target,None,{},None);w.stable=lambda b:None


def test_backup_restore_uses_native_roots_and_keeps_budget_journal(tmp_path):
    w=worker(tmp_path);_,_,last=build(w,tmp_path);context(w,last['after'])
    destination=tmp_path/'backup.sqlite';backup(w,destination);w.close()
    w=worker(tmp_path,'new-worker','new-archive');context(w,last['after'])
    w.inbox.reserve('existing-cost',100)
    assert restore(w,destination)['status']=='RESTORED'
    assert w.archive_store().ref_existing(last['after'])
    assert dict(w.inbox.db.execute('SELECT * FROM reservations'))=={'existing-cost':'100'}
    w.close()


def test_corrupt_backup_is_rejected_without_promoting_or_resetting_costs(tmp_path):
    w=worker(tmp_path);_,_,last=build(w,tmp_path);context(w,last['after'])
    destination=tmp_path/'backup.sqlite';backup(w,destination);w.close()
    db=sqlite3.connect(destination);db.execute("UPDATE chunks SET data=zeroblob(length(data))");db.commit();db.close()
    w=worker(tmp_path,'new-worker','new-archive');context(w,last['after']);w.inbox.reserve('keep',100)
    with pytest.raises(ValueError,match='CHUNK_CORRUPT'):restore(w,destination)
    assert w.archive_store().version(last['after'][7]) is None
    assert dict(w.inbox.db.execute('SELECT * FROM reservations'))=={'keep':'100'}
    w.close()


def test_backup_uses_physical_limit_separate_from_admitted_data(tmp_path):
    w = worker(tmp_path)
    _, _, last = build(w, tmp_path)
    context(w, last["after"])
    w.c["archive"]["max_bytes"] = "160"
    path = tmp_path / "snapshot.sqlite"
    with pytest.raises(ValueError, match="^ZK_ARCHIVE_BACKUP_LIMIT$"):
        backup(w, path)
    assert not path.exists()

    w.c["archive"]["max_backup_bytes"] = "1048576"
    assert backup(w, path)["status"] == "BACKUP_SAVED"
    assert 160 < path.stat().st_size <= 1048576
    w.close()

    w = worker(tmp_path, "new-worker", "new-archive")
    context(w, last["after"])
    w.c["archive"].update(max_bytes="160", max_backup_bytes="1048576")
    w.inbox.reserve("kept-cost", 100)
    assert restore(w, path)["status"] == "RESTORED"
    assert w.archive_store().ref_existing(last["after"])
    assert dict(w.inbox.db.execute("SELECT * FROM reservations")) == {"kept-cost": "100"}
    w.close()


@pytest.mark.parametrize("phase", ["PREPARED", "SENDING"])
def test_archive_capacity_stops_before_publication_and_keeps_prepared_attempt(tmp_path, monkeypatch, phase):
    from test_zk_worker import simulated

    w = worker(tmp_path)
    model = simulated(w, monkeypatch, tmp_path)
    w.inbox.enqueue(dict(kind="deposit", consumer="unit", txid="11" * 32, vout="0"))
    assert w.advance("prepare")["status"] == "PROOF_PREPARED"
    active = dict(w.inbox.get("attempt"), phase=phase)
    w.inbox.put("attempt", active)
    w.archive_store().max_bytes = 79  # One exact 80-byte header must fit before publication.
    w.inbox.reserve("previous-cost", 100)
    with pytest.raises(ValueError, match="^ZK_ARCHIVE_DISK_BUDGET$"):
        w.tick()
    assert w.inbox.get("attempt") == active and not model["sends"]
    assert dict(w.inbox.db.execute("SELECT * FROM reservations")) == {"previous-cost": "100"}
    assert w.archive_store().db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    w.close()


def test_legacy_array_is_migrated_without_changing_financial_records(tmp_path):
    w=worker(tmp_path);headers=fixture_headers()
    raw=[headers[h].hex() for h in (32257,32258)]
    native=w.native([],raw,[],tmp_path/'legacy')
    w.inbox.put('archive:'+native['after'][7],native['archive']);w.inbox.reserve('keep',100)
    ref=w.archive_ref(native['after'])
    assert ref.store.version(ref.commitment)['state']==native['after']
    assert w.inbox.get('archive:'+native['after'][7])==raw
    assert dict(w.inbox.db.execute('SELECT * FROM reservations'))=={'keep':'100'}
    w.close()


def test_v4_upgrade_keeps_v3_need_ids_semantics_and_spending(tmp_path):
    from copy import deepcopy
    from signer_agent.zk_worker import load_config
    from test_zk_worker_service import worker as service_worker
    w=service_worker(tmp_path);c=deepcopy(w.c);w.inbox.close()
    # Start an actual V3 inbox with its original economic scope.
    c['version']='3';c['directory']=str(tmp_path/'v3-worker');c['transport']=None;c['batching']=dict(local_service_seconds='300')
    old=Inbox(c);need=dict(operation='kept');key=old.enqueue(need);scope=old.get('scope');old.reserve('cost',1);old.close()
    c['version']='4';c['archive']=dict(directory=str(tmp_path/'permanent'),max_headers='200000',max_bytes='1073741824');c['automation']=dict(enabled=False)
    from signer_agent.zk_worker_prover import write
    path=tmp_path/'config.json';write(path,c);assert load_config(path)==c
    updated=Inbox(c)
    assert updated.enqueue(need)==key and updated.get('scope')==scope
    assert dict(updated.db.execute('SELECT * FROM reservations'))=={'cost':'1'}
    updated.close()
    c["archive"]["max_backup_bytes"] = "2147483648"
    write(path, c)
    assert load_config(path) == c
    updated = Inbox(c)
    assert updated.get("scope") == scope
    updated.close()


def test_changed_native_summary_stops_before_proving_or_spending(tmp_path,monkeypatch):
    from test_zk_worker import make_worker,simulated
    from signer_agent.zk_worker_prover import read,write
    w=make_worker(tmp_path);model=simulated(w,monkeypatch,tmp_path)
    w.inbox.enqueue(dict(kind='deposit',consumer='unit',txid='11'*32,vout='0'))
    assert w.tick()['status']=='PROOF_PREPARED'
    path=w.inbox.path/'attempts'/w.inbox.get('attempt')['id']/'native.json'
    changed=read(path);changed['after'][7]='ff'*32;write(path,changed)
    with pytest.raises(ValueError,match='SUMMARY_DRIFT'):w.tick()
    assert model['sends']==[] and model['proves']==0;w.inbox.close()
