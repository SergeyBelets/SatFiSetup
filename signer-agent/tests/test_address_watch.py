"""Real pinned native block codec; synthetic Core chain, no PoW/proof qualification."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from signer_agent import crypto, address_watch as m
from signer_agent.deposit_intent import DepositIntentV1
from signer_agent.bitcoin_proofs import sha256d

ROOT=Path(__file__).resolve().parents[2]
VECTOR=json.loads((ROOT/"rust/zkspv/derivation-qualification/public-vector.json").read_text())


def compact(n):return bytes([n]) if n<253 else b"\xfd"+n.to_bytes(2,"little")


def tx(script,amount=1000,previous=None,tag=1):
    prev=bytes(32) if previous is None else bytes.fromhex(previous.split(":")[0])[::-1]
    index=0xffffffff if previous is None else int(previous.split(":")[1])
    return (bytes.fromhex("0200000001")+prev+index.to_bytes(4,"little")+bytes([2,tag,0])+b"\xff"*4+
        b"\x01"+amount.to_bytes(8,"little")+compact(len(script))+script+bytes(4))


def block(previous,transactions,tag):
    merkle=[sha256d(t) for t in transactions]
    while len(merkle)>1:
        if len(merkle)%2:merkle.append(merkle[-1])
        merkle=[sha256d(merkle[i]+merkle[i+1]) for i in range(0,len(merkle),2)]
    header=(1).to_bytes(4,"little")+bytes.fromhex(previous)[::-1]+merkle[0]+(100+tag).to_bytes(4,"little")+bytes.fromhex("ffff7f20")+tag.to_bytes(4,"little")
    return dict(hash=sha256d(header)[::-1].hex(),raw=(header+compact(len(transactions))+b"".join(transactions)).hex())


@pytest.fixture
def watched(tmp_path):
    genesis=block("00"*32,[tx(b"\x51")],0)
    intent=replace(DepositIntentV1.decode(bytes.fromhex(VECTOR["intent_bytes"])),bitcoin_genesis=bytes.fromhex(genesis["hash"]))
    base=bytes.fromhex(VECTOR["base_group"]);desc=intent.derive(base)
    scope=dict(bitcoin_genesis=genesis["hash"],bitcoin_network="regtest")
    store=m.Store(tmp_path/"address.sqlite",scope)
    accepted=store.accept(intent.encode(),base,"regtest","route",dict(source="explicit test authenticated request"))
    raw=tx(bytes.fromhex(desc["script_pubkey"]),tag=2);outpoint=sha256d(raw)[::-1].hex()+":0"
    first=block(genesis["hash"],[raw],1)
    spend=tx(b"\x51",previous=outpoint,tag=3)
    second=block(first["hash"],[spend],2)
    view=SimpleNamespace(chain=[genesis,first,second],pruned=False,ibd=False,calls=[],move=False,
        filter_index=True,filter_completed=True,filter_anchor=True,filter_hits=[])
    class Core:
        is_loopback=True
        def check_network(self):pass
        def call(self,method,*args):
            view.calls.append((method,args))
            if method=="getblockhash":return view.chain[args[0]]["hash"]
            if method=="getbestblockhash":return view.chain[-1]["hash"]
            if method=="getblockchaininfo":return dict(blocks=len(view.chain)-1,pruned=view.pruned,initialblockdownload=view.ibd)
            if method=="getblockcount":return len(view.chain)-1
            if method in ("getblock","getblockheader"):
                height=next(h for h,b in enumerate(view.chain) if b["hash"]==args[0])
                if method=="getblock" and args[1]==0:
                    return view.chain[height]["raw"]
                return dict(hash=args[0],height=height,tx=["99"*32])
            if method=="getrawtransaction":
                return dict(txid=args[0],blockhash=args[2],vout=[dict(scriptPubKey=dict(hex="51"))])
            if method=="getindexinfo":
                return {"basic block filter index": dict(synced=True,best_block_height=len(view.chain)-1)} if view.filter_index else {}
            if method=="scanblocks":
                _, descriptors, low, high, filter_type = args
                assert descriptors==[f'raw({desc["script_pubkey"]})',"raw(51)"] and filter_type=="basic"
                matches = {h for h in view.filter_hits if low<=h<=high}
                if view.filter_anchor:
                    matches.add(high)
                return dict(completed=view.filter_completed, from_height=low, to_height=high,
                            relevant_blocks=[view.chain[h]["hash"] for h in sorted(matches,reverse=True)])
            raise AssertionError(method)
    return SimpleNamespace(**locals(),bitcoin=Core())


def set_coverage(store, key, tip, best):
    """Seed a consistent synthetic cursor and anchor for consumer tests."""
    with store.journal.db() as db:
        db.execute("UPDATE address_intents SET next_height=? WHERE id=?", (tip+1,key))
        db.execute("INSERT OR REPLACE INTO address_scans VALUES(?,?,?,?)", (key,tip,best,"00"*32))
        store._put(db,"coverage:"+key,dict(status="WATCH_COVERED",tip=str(tip),bestblock=best))


def test_durable_ack_contains_descriptor_and_genesis_rescan_before_any_observation(watched):
    e=watched;assert e.accepted["status"]=="DURABLE" and not e.accepted["ready"]
    reopened=m.Store(e.store.path,e.scope)
    assert reopened.intents()[0][2]==0 and reopened.intents()[0][1]["start_height"]=="0"
    assert e.view.calls==[]


def test_real_block_codec_discovers_early_payment_and_spend_across_restart(watched):
    e=watched;assert e.store.scan(e.bitcoin,max_blocks=1)["status"]=="RESCANNING"
    restarted=m.Store(e.store.path,e.scope);assert restarted.scan(e.bitcoin,max_blocks=1)["status"]=="RESCANNING"
    received=restarted.outputs(e.intent.intent_id.hex())[0]
    assert received["outpoint"]==e.outpoint and received["satoshis"]=="1000" and received["spent_by"] is None
    result=restarted.scan(e.bitcoin,max_blocks=1)
    assert result["status"]=="WATCH_COVERED" and result["ready"] is False
    assert restarted.outputs(e.intent.intent_id.hex())[0]["spent_by"]==sha256d(e.spend)[::-1].hex()


def test_reorg_retains_old_ingress_and_spend_evidence_and_rebuilds_current_view(watched):
    e=watched;e.store.scan(e.bitcoin,max_blocks=3)
    alternate=block(e.first["hash"],[tx(b"\x51",tag=4)],3)
    e.view.chain=[e.genesis,e.first,alternate]
    assert e.store.scan(e.bitcoin,max_blocks=3)["status"]=="WATCH_COVERED"
    assert e.store.outputs(e.intent.intent_id.hex())[0]["spent_by"] is None
    with e.store.journal.db() as db:
        assert db.execute("SELECT COUNT(*) FROM address_observations WHERE kind='SPEND'").fetchone()[0]==1


def test_repeated_and_same_block_payments_remain_separate_protected_outpoints(watched):
    e=watched;a=tx(bytes.fromhex(e.desc["script_pubkey"]),tag=5)
    point=sha256d(a)[::-1].hex()+":0"
    b=tx(bytes.fromhex(e.desc["script_pubkey"]),previous=point,tag=6)
    e.view.chain=[e.genesis,block(e.genesis["hash"],[a,b],4)]
    e.store.scan(e.bitcoin,max_blocks=3);rows=e.store.outputs(e.intent.intent_id.hex())
    assert len(rows)==2 and sum(r["spent_by"] is not None for r in rows)==1


@pytest.mark.parametrize("field",["pruned","ibd"])
def test_unavailable_history_never_becomes_covered_or_deletes_descriptor(watched,field):
    e=watched;setattr(e.view,field,True)
    with pytest.raises(ValueError,match="ADDRESS_ARCHIVAL_CORE_REQUIRED"):e.store.scan(e.bitcoin)
    assert e.store.intents()[0][2]==0


def test_wrong_raw_block_and_corrupted_merkle_root_are_rejected_by_native_codec(watched):
    e=watched
    for raw,expected in [(e.first["raw"],e.genesis["hash"]),
        (e.first["raw"][:72]+("00"*32)+e.first["raw"][136:],e.first["hash"])]:
        with pytest.raises(RuntimeError,match="CRYPTO_VALIDATION_FAILED"):
            crypto.call("bitcoin_watch_block_v1",raw=raw,hash=expected,scripts=[e.desc["script_pubkey"]],outpoints=[])


def test_nonce_rebinding_and_storage_tampering_cannot_change_existing_descriptor(watched):
    e=watched;other=replace(e.intent,recipient=b"z"*20)
    with pytest.raises(ValueError,match="ADDRESS_REQUEST_NONCE_CONFLICT"):
        e.store.accept(other.encode(),e.base,"regtest","other",{})
    with e.store.journal.db() as db:
        row=json.loads(db.execute("SELECT immutable FROM address_intents").fetchone()[0]);row["descriptor"]["script_pubkey"]="5120"+"00"*32
        db.execute("UPDATE address_intents SET immutable=?",(json.dumps(row),))
    with pytest.raises(ValueError,match="ADDRESS_DESCRIPTOR_CONFLICT"):e.store.scan(e.bitcoin)


def test_backup_preserves_watch_evidence_without_any_signer_journal(watched,tmp_path):
    e=watched;e.store.scan(e.bitcoin,max_blocks=3)
    backup=tmp_path/"backup.sqlite";assert e.store.backup(backup)["status"]=="ADDRESS_BACKUP_SAVED"
    restored=m.Store(backup,e.scope)
    assert restored.outputs(e.intent.intent_id.hex())==e.store.outputs(e.intent.intent_id.hex())
    assert not list(tmp_path.glob("*.spent.sqlite"))


def test_library_address_encoding_is_network_explicit_and_p2tr_only(watched):
    e=watched
    for network,prefix in (("mainnet","bc1p"),("testnet4","tb1p"),("regtest","bcrt1p")):
        assert crypto.call("bitcoin_script_address_v1",script=e.desc["script_pubkey"],network=network)["address"].startswith(prefix)
    with pytest.raises(RuntimeError):crypto.call("bitcoin_script_address_v1",script="0014"+"55"*20,network="regtest")


def test_reorg_beyond_configured_bound_retains_all_observations(watched):
    e=watched;e.store.scan(e.bitcoin,max_blocks=3)
    e.view.chain=[e.genesis]
    with pytest.raises(ValueError,match="ADDRESS_RESCAN_RECOVERY_REQUIRED"):e.store.scan(e.bitcoin,max_reorg=1)
    assert e.store.outputs(e.intent.intent_id.hex())[0]["spent_by"] is not None
    assert e.store.intents()[0][2]==3


def test_output_budget_rolls_back_whole_block_and_keeps_previous_cursor(watched,tmp_path):
    e=watched;store=m.Store(tmp_path/"small.sqlite",e.scope,max_outputs=1)
    store.accept(e.intent.encode(),e.base,"regtest","same",{})
    first=tx(bytes.fromhex(e.desc["script_pubkey"]),tag=7);second=tx(bytes.fromhex(e.desc["script_pubkey"]),tag=8)
    e.view.chain=[e.genesis,block(e.genesis["hash"],[first,second],9)]
    with pytest.raises(ValueError,match="ADDRESS_WATCH_OUTPUT_LIMIT"):store.scan(e.bitcoin,max_blocks=3)
    assert store.outputs(e.intent.intent_id.hex())==[] and store.intents()[0][2]==1


def extend(chain, tip):
    while len(chain)<=tip:
        height=len(chain)
        chain.append(block(chain[-1]["hash"],[tx(b"\x51",tag=(height+32)%256)],height+32))


def test_spent_history_does_not_consume_the_live_output_budget(watched,tmp_path):
    e=watched;store=m.Store(tmp_path/"small.sqlite",e.scope,max_outputs=1)
    store.accept(e.intent.encode(),e.base,"regtest","same",{})
    e.view.chain=[e.genesis,e.first]
    previous=e.outpoint
    for height in range(2,20):
        replacement=tx(bytes.fromhex(e.desc["script_pubkey"]),previous=previous,tag=height)
        e.view.chain.append(block(e.view.chain[-1]["hash"],[replacement],height))
        previous=sha256d(replacement)[::-1].hex()+":0"
    assert store.scan(e.bitcoin,max_blocks=32)["status"]=="WATCH_COVERED"
    key=e.intent.intent_id.hex()
    assert len(store.outputs(key))==19
    assert [row["outpoint"] for row in store.unspent_outputs(key)]==[previous]
    with store.journal.db() as db:
        assert db.execute("SELECT COUNT(*) FROM address_observations").fetchone()[0]==37


def test_native_block_with_full_live_budget_can_spend_and_replace_an_outpoint(watched):
    e=watched
    script=e.desc["script_pubkey"]
    replacement=tx(bytes.fromhex(script),previous=e.outpoint,tag=90)
    candidate=block(e.first["hash"],[replacement],90)
    outpoints=[e.outpoint]+[f"{i:064x}:0" for i in range(4095)]
    result=crypto.call("bitcoin_watch_block_v1",raw=candidate["raw"],hash=candidate["hash"],
        scripts=[script],outpoints=outpoints)
    assert result["spends"][0]["outpoint"]==e.outpoint and len(result["outputs"])==1


def test_full_remote_scan_compacts_history_and_rewinds_exactly_100_blocks(watched):
    e=watched;e.bitcoin.is_loopback=False
    payment=tx(bytes.fromhex(e.desc["script_pubkey"]),tag=3)
    outpoint=sha256d(payment)[::-1].hex()+":0"
    e.view.chain.append(block(e.second["hash"],[payment],3))
    extend(e.view.chain,126)
    spend=tx(b"\x51",previous=outpoint,tag=127)
    e.view.chain.append(block(e.view.chain[-1]["hash"],[spend],127))
    extend(e.view.chain,226)
    key=e.intent.intent_id.hex()
    e.store.scan(e.bitcoin,max_blocks=256)
    assert e.store.scan(e.bitcoin)["status"]=="WATCH_COVERED"
    assert e.store.unspent_outputs(key)==[]
    with e.store.journal.db() as db:
        assert tuple(db.execute("SELECT MIN(height),MAX(height),COUNT(*) FROM address_scans").fetchone())==(126,226,101)
    e.view.chain=e.view.chain[:127]
    extend(e.view.chain,226)
    assert e.store.scan(e.bitcoin,max_blocks=100)["status"]=="WATCH_COVERED"
    assert [row["outpoint"] for row in e.store.unspent_outputs(key)]==[outpoint]
    assert e.store.get("reorg:"+key)["last_common_height"]==126
    with e.store.journal.db() as db:
        assert db.execute("SELECT COUNT(*) FROM address_observations").fetchone()[0]==4
    assert not any(method=="scanblocks" for method,_ in e.view.calls)


def test_configured_height_skips_decoding_nonmatching_history_and_resumes(watched):
    e=watched
    e.view.chain=[e.genesis]
    extend(e.view.chain,6)
    options=dict(start_height=5,max_reorg=1)
    assert e.store.scan(e.bitcoin,**options)["next_height"]=="1"
    restarted=m.Store(e.store.path,e.scope)
    assert restarted.scan(e.bitcoin,max_reorg=1)["next_height"]=="5"
    assert restarted.scan(e.bitcoin,max_reorg=1)["status"]=="WATCH_COVERED"
    decoded=[args[0] for method,args in e.view.calls if method=="getblock" and args[1]==0]
    assert decoded==[e.view.chain[h]["hash"] for h in (0,4,5,6)]
    key,immutable,_=restarted.intents()[0]
    assert immutable["start_height"]=="0" and m.digest(immutable)==e.accepted["descriptor_hash"]
    assert restarted.outputs(key)==[]


def test_filtered_history_preserves_early_output_and_spend_in_height_order(watched):
    e=watched
    extend(e.view.chain,5)
    e.view.filter_hits=[1,2]
    options=dict(start_height=4,max_reorg=1)
    e.store.scan(e.bitcoin,**options)
    assert e.store.scan(e.bitcoin,**options)["next_height"]=="4"
    row,=e.store.outputs(e.intent.intent_id.hex())
    assert row["outpoint"]==e.outpoint and row["spent_by"]==sha256d(e.spend)[::-1].hex()
    assert e.store.scan(e.bitcoin,**options)["status"]=="WATCH_COVERED"


@pytest.mark.parametrize("field,code", [
    ("filter_index","ADDRESS_FILTER_INDEX_REQUIRED"),
    ("filter_completed","ADDRESS_FILTER_SCAN_INCOMPLETE"),
    ("filter_anchor","ADDRESS_FILTER_SCAN_INCOMPLETE"),
])
def test_incomplete_filter_page_cannot_advance_cursor(watched,field,code):
    e=watched
    extend(e.view.chain,5)
    options=dict(start_height=4,max_reorg=1)
    e.store.scan(e.bitcoin,**options)
    setattr(e.view,field,False)
    with pytest.raises(ValueError,match=code):
        e.store.scan(e.bitcoin,**options)
    assert e.store.intents()[0][2]==1
    assert e.store.coverage_at(e.intent.intent_id.hex(),e.view.chain[-1]["hash"]) is None


def test_dense_filter_page_continues_with_bounded_full_scan(watched):
    e=watched
    extend(e.view.chain,5)
    e.view.filter_hits=[1,2]
    options=dict(start_height=4,max_reorg=1,max_blocks=1)
    e.store.scan(e.bitcoin,**options)
    assert e.store.scan(e.bitcoin,**options)["next_height"]=="2"
    row,=e.store.outputs(e.intent.intent_id.hex())
    assert row["outpoint"]==e.outpoint


def test_filter_anchor_reorg_rolls_back_outputs_and_cursor(watched, monkeypatch):
    e=watched
    extend(e.view.chain,5)
    e.view.filter_hits=[1,2]
    options=dict(start_height=4,max_reorg=1)
    e.store.scan(e.bitcoin,**options)
    old_anchor=e.view.chain[3]["hash"]
    call=e.bitcoin.call

    def reorg(method,*args):
        value=call(method,*args)
        if method=="getblock" and args==(old_anchor,0):
            e.view.chain[3:]=[block(e.view.chain[2]["hash"],[tx(b"\x51",tag=90)],90)]
            extend(e.view.chain,5)
        return value

    monkeypatch.setattr(e.bitcoin,"call",reorg)
    with pytest.raises(ValueError,match="ADDRESS_CORE_CHANGED"):
        e.store.scan(e.bitcoin,**options)
    key,_,cursor=e.store.intents()[0]
    assert cursor==1 and e.store.outputs(key)==[]


def test_filter_page_checks_combined_disk_budget_before_commit(watched, monkeypatch):
    e=watched
    extend(e.view.chain,5)
    e.view.filter_hits=[1,2]
    options=dict(start_height=4,max_reorg=1)
    e.store.scan(e.bitcoin,**options)
    def limited(additional):
        if additional>=16_384:
            raise ValueError("ADDRESS_WATCH_DISK_BUDGET")
    monkeypatch.setattr(e.store,"space",limited)
    with pytest.raises(ValueError,match="ADDRESS_WATCH_DISK_BUDGET"):
        e.store.scan(e.bitcoin,**options)
    key,_,cursor=e.store.intents()[0]
    assert cursor==1 and e.store.outputs(key)==[]


def test_reorg_in_full_scan_tail_rewinds_without_losing_early_filtered_funding(watched):
    e=watched
    e.view.chain=[e.genesis,e.first]
    extend(e.view.chain,4)
    e.view.chain.append(block(e.view.chain[-1]["hash"],[e.spend],6))
    e.view.filter_hits=[1]
    options=dict(start_height=4,max_reorg=2)
    for _ in range(3):
        result=e.store.scan(e.bitcoin,**options)
    assert result["status"]=="WATCH_COVERED"
    key=e.intent.intent_id.hex()
    assert e.store.outputs(key)[0]["spent_by"] is not None
    e.view.chain[-1]=block(e.view.chain[-2]["hash"],[tx(b"\x51",tag=10)],10)
    assert e.store.scan(e.bitcoin,**options)["status"]=="WATCH_COVERED"
    assert e.store.outputs(key)[0]["spent_by"] is None


def test_configured_height_above_tip_cannot_advance(watched):
    e=watched
    with pytest.raises(ValueError,match="ADDRESS_SCAN_START_HEIGHT"):
        e.store.scan(e.bitcoin,start_height=4)
    assert e.store.intents()[0][2]==0


def test_remote_core_cannot_commit_negative_filter_results(watched):
    e=watched
    e.bitcoin.is_loopback=False
    with pytest.raises(ValueError,match="ADDRESS_FILTER_LOCAL_CORE_REQUIRED"):
        e.store.scan(e.bitcoin,start_height=2)
    assert e.store.intents()[0][2]==0 and not e.view.calls
    assert e.store.scan(e.bitcoin,start_height=0)["status"]=="WATCH_COVERED"


@pytest.mark.parametrize("url,configured_url,opt_in,permitted",[
    ("https://core.invalid","https://core.invalid",True,True),
    ("https://core.invalid","https://other.invalid",True,False),
    ("https://core.invalid","https://core.invalid",False,False),
    ("http://core.invalid","http://core.invalid",True,False),
    ("https://core.invalid","https://core.invalid","true",False),
])
def test_remote_filter_mode_requires_consent_for_the_exact_https_endpoint(watched,url,configured_url,opt_in,permitted):
    from signer_agent.bitcoin_rpc import BitcoinRPC

    e=watched;extend(e.view.chain,5);e.view.filter_hits=[1,2]
    rpc=BitcoinRPC.__new__(BitcoinRPC);rpc.url=url
    rpc.call=e.bitcoin.call;rpc.check_network=e.bitcoin.check_network
    c=dict(bitcoin_rpc=configured_url,trust_remote_watch_filters=opt_in)
    options=dict(start_height=4,max_reorg=1,allow_remote_filters=m.trusted_remote_filters(c,rpc))
    if not permitted:
        with pytest.raises(ValueError,match="ADDRESS_FILTER_LOCAL_CORE_REQUIRED"):
            e.store.scan(rpc,**options)
        assert e.view.calls==[]
        return
    e.store.scan(rpc,**options)
    e.store.scan(rpc,**options)
    assert e.store.scan(rpc,**options)["status"]=="WATCH_COVERED"
    assert any(method=="scanblocks" for method,_ in e.view.calls)
    assert e.store.outputs(e.intent.intent_id.hex())[0]["spent_by"]==sha256d(e.spend)[::-1].hex()


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:8332",True),("http://[::1]:8332",True),
    ("http://localhost:8332",False),("https://core.example.invalid:8332",False),
    ("https://127.0.0.1.example.invalid:8332",False),
])
def test_filter_source_requires_numeric_loopback(url,expected):
    from signer_agent.bitcoin_rpc import BitcoinRPC
    rpc=BitcoinRPC.__new__(BitcoinRPC)
    rpc.url=url
    assert rpc.is_loopback is expected


def test_legacy_sparse_journal_replays_before_coverage_and_keeps_evidence(watched):
    e=watched
    extend(e.view.chain,5)
    e.view.filter_hits=[1]
    options=dict(start_height=4,max_reorg=1)
    for _ in range(3):
        e.store.scan(e.bitcoin,**options)
    key=e.intent.intent_id.hex()
    best=e.view.chain[-1]["hash"]
    assert e.store.coverage_at(key,best)
    assert e.store.outputs(key)[0]["spent_by"] is None
    with e.store.journal.db() as db:
        db.execute("DELETE FROM address_meta WHERE id='watch_coverage_version'")
        observed=db.execute("SELECT COUNT(*) FROM address_observations").fetchone()[0]
    assert e.store.coverage_at(key,best) is None
    assert e.store.scan(e.bitcoin,**options)["next_height"]=="1"
    assert e.store.outputs(key)==[] and e.store.coverage_at(key,best) is None
    with e.store.journal.db() as db:
        assert db.execute("SELECT COUNT(*) FROM address_observations").fetchone()[0]==observed
    e.view.filter_hits=[1,2]
    e.store.scan(e.bitcoin,**options)
    assert e.store.scan(e.bitcoin,**options)["status"]=="WATCH_COVERED"
    assert e.store.outputs(key)[0]["spent_by"]==sha256d(e.spend)[::-1].hex()


def test_legacy_contiguous_journal_refreshes_coverage_without_replay(watched):
    e=watched
    e.store.scan(e.bitcoin)
    key=e.intent.intent_id.hex()
    with e.store.journal.db() as db:
        db.execute("DELETE FROM address_meta WHERE id='watch_coverage_version'")
    e.view.calls.clear()
    assert e.store.scan(e.bitcoin)["status"]=="WATCH_COVERED"
    assert e.store.coverage_at(key,e.view.chain[-1]["hash"])
    assert all(method!="getblock" for method,_ in e.view.calls)


def test_v2_sparse_journal_refreshes_without_replay(watched):
    e=watched;extend(e.view.chain,6);e.view.filter_hits=[1,2]
    for _ in range(3):
        e.store.scan(e.bitcoin,start_height=5,max_reorg=1)
    key=e.intent.intent_id.hex();best=e.view.chain[-1]["hash"]
    before=e.store.outputs(key)
    e.store.put("watch_coverage_version","2")
    assert e.store.coverage_at(key,best) is None
    e.view.calls.clear()
    assert e.store.scan(e.bitcoin,max_reorg=1)["status"]=="WATCH_COVERED"
    assert e.store.coverage_at(key,best)
    assert e.store.outputs(key)==before
    assert all(method!="getblock" for method,_ in e.view.calls)


def test_interrupted_rewind_clears_coverage_even_if_old_tip_returns(watched,monkeypatch):
    e=watched
    e.store.scan(e.bitcoin)
    key=e.intent.intent_id.hex()
    assert e.store.coverage_at(key,e.second["hash"])
    alternative=block(e.first["hash"],[tx(b"\x51",tag=90)],90)
    e.view.chain[-1]=alternative
    call=e.bitcoin.call
    def fail_after_rewind(method,*args):
        if method=="getblock" and args==(alternative["hash"],0):
            e.view.chain[-1]=e.second
            raise ValueError("ADDRESS_CORE_CHANGED")
        return call(method,*args)
    monkeypatch.setattr(e.bitcoin,"call",fail_after_rewind)
    with pytest.raises(ValueError,match="ADDRESS_CORE_CHANGED"):
        e.store.scan(e.bitcoin,max_reorg=1)
    assert e.store.intents()[0][2]==2
    assert e.store.get("coverage:"+key) is None
    assert e.store.coverage_at(key,e.second["hash"]) is None
    monkeypatch.setattr(e.bitcoin,"call",call)
    assert e.store.scan(e.bitcoin,max_reorg=1)["status"]=="WATCH_COVERED"
    assert e.store.outputs(key)[0]["spent_by"]==sha256d(e.spend)[::-1].hex()


def test_coverage_reader_rejects_matching_tip_when_projection_disagrees(watched):
    e=watched
    e.store.scan(e.bitcoin)
    key=e.intent.intent_id.hex()
    best=e.second["hash"]
    with e.store.journal.db() as db:
        db.execute("UPDATE address_intents SET next_height=2 WHERE id=?", (key,))
    assert e.store.get("coverage:"+key)["bestblock"]==best
    assert e.store.coverage_at(key,best) is None
    with e.store.journal.db() as db:
        db.execute("UPDATE address_intents SET next_height=3 WHERE id=?", (key,))
        db.execute("UPDATE address_scans SET hash=? WHERE intent=? AND height=2", ("ff"*32,key))
    assert e.store.coverage_at(key,best) is None
