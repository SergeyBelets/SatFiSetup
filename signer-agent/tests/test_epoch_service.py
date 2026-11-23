"""Real native share checks, identity signatures, block codec and SQLite; explicit chain fixtures."""
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_utils import keccak
from signer_agent import epoch_service as m,epoch_retention as retention,crypto,address_mint
from signer_agent.address_watch import Store,digest
from signer_agent.dkg_protocol import Manifest
from signer_agent.operations import Journal,canonical
from signer_agent.secure_state import SecureState
from test_address_transport import delivery,private,VECTOR
from test_address_mint import Functions,address
from test_address_watch import watched,block,tx
from test_bitcoin_input_keys import signing,native_keys


@pytest.fixture
def epoch(delivery,monkeypatch):
    e=delivery;e.view.custody=1;e.view.derivation=0;e.closed=True;e.cg=1;e.dg=1;e.nonce=0
    manifest=Manifest(31337,address(10),e.template.wallet_id,e.template.key_epoch,'BTC_SPEND',tuple(a.address for a in e.accounts),
        tuple(bytes.fromhex(k['public']) for k in e.keys),2)
    shares={a.address:crypto.call('public_share',share=(i+11).to_bytes(32,'big').hex())['public'] for i,a in enumerate(e.accounts)}
    dkg=SimpleNamespace(address=manifest.registry,functions=Functions(dict(
        getCompletedResult=(manifest.context,e.template.wallet_result,manifest.epoch,keccak(text='BTC_SPEND'),bytes.fromhex(VECTOR['base_group'])[1:],list(manifest.participants),2),
        getSessionInfo=[0]*9+[True],transportKeys=lambda _,who:manifest.transport_keys[manifest.participants.index(who)],
        PROTOCOL_V1=manifest.protocol_id,getPublicKeyShare=lambda _,who:bytes.fromhex(shares[who]))))
    signing=SimpleNamespace(address=address(40),functions=Functions(dict(batchRegistry=e.batch.address,dkg=dkg.address,
        walletId=manifest.session_id,walletResult=e.template.wallet_result,maxPacketBytes=4096)))
    verifier=SimpleNamespace(address=address(41),functions=Functions(dict(QUALIFIED=True)))
    e.reg_values.update(dkg=dkg.address,custodyGeneration=lambda:e.cg,derivationGeneration=lambda:e.dg,
        custodyStatusNonce=lambda:e.nonce,derivationStatusNonce=0,issuanceClosed=lambda:e.closed,
        custodyScope=b'c'*32,derivationScope=b'd'*32,intentCount=0,verifier=verifier.address)
    e.w3.eth.contract=lambda address,**kw:{dkg.address:dkg,signing.address:signing,verifier.address:verifier}[address]
    e.w3.eth.get_balance=lambda *a,**kw:1000000000
    monkeypatch.setattr('signer_agent.bitcoin_trust.checked_runtime',lambda *a,**kw:None)
    monkeypatch.setattr('signer_agent.contract_provenance.prove_creation',lambda *a,**kw:{'explicit':'chain/provenance fixture'})
    monkeypatch.setattr(address_mint,'configuration',lambda *a,**kw:e.ctx)
    c=dict(e.configs[1],version='2',fee_reserve_config=private(e.tmp_path/'fee.json',{}))
    e.service.c=c
    filename=private(e.tmp_path/'old-address.json',c)
    policy=dict(version='1',fleet_id='aa'*32,identity_file=c['identity_file'],directory=str(e.tmp_path/'fleet'),
        max_epochs_per_poll='1',max_bindings_per_poll='2',max_archive_records='1024',attestation_ttl_seconds='600',
        epochs=[dict(wallet_context=manifest.context.hex(),mode='SERVICING',address_config=filename,
            signing_board=signing.address,spv_route=None,minimum_evm_balance_wei='10000000')])
    config_file=private(e.tmp_path/'epochs.json',policy)
    fleet=m.Fleet(m.load_config(config_file),client_factory=lambda c:e.service)
    class Core:
        def check_network(self):pass
        def call(self,method,*args):
            if method in ('getblockhash','getbestblockhash'):return e.template.bitcoin_genesis.hex()
            if method=='getblockchaininfo':return dict(blocks=0,pruned=False,initialblockdownload=False)
            raise AssertionError(method)
    core=Core();monkeypatch.setattr('signer_agent.bitcoin_rpc.BitcoinRPC',lambda *a,**kw:core)
    class Inspector:
        def __init__(self,*a,**kw):self.c=dict(watch_databases=[str(e.service.store.path)])
        def healthy(self,**kw):return dict(registry=e.reg,batch=e.batch),dict(evidence='bb'*32)
        def status(self,**kw):return dict(status='SUFFICIENT_QUALIFICATION_REQUIRED')
    monkeypatch.setattr('signer_agent.btc_fee_reserve.load_config',lambda *_:{})
    monkeypatch.setattr('signer_agent.btc_fee_reserve.Inspector',Inspector)
    e.fleet=fleet;e.policy=policy;e.manifest=manifest;e.dkg=dkg;e.signing=signing;e.verifier=verifier;e.shares=shares;e.config_file=config_file
    return e


@pytest.mark.parametrize('mutation',[
    lambda c:c.update(unknown=True),lambda c:c.update(max_epochs_per_poll='9'),
    lambda c:c.update(attestation_ttl_seconds='0'),lambda c:c.update(max_archive_records='65537'),
    lambda c:c.update(epochs=[]),lambda c:c['epochs'].append(deepcopy(c['epochs'][0])),
    lambda c:c['epochs'][0].update(mode='CLOSED'),lambda c:c['epochs'][0].update(spv_route={}),
])
def test_strict_bounded_configuration(epoch,mutation):
    e=epoch;c=deepcopy(e.policy);mutation(c);path=private(e.tmp_path/'invalid.json',c)
    with pytest.raises(ValueError):m.load_config(path)


def test_snapshot_binds_exact_completed_wallet_and_dedicated_board(epoch):
    e=epoch;s=e.fleet.snapshot(e.policy['epochs'][0],e.service)
    assert s['manifest']==e.manifest and s['identity']['signing_board']==e.signing.address
    assert not hasattr(s,'private_key')
    e.signing.functions.values['walletResult']=b'wrong'.ljust(32,b'\0')
    with pytest.raises(ValueError,match='EPOCH_BOARD_BINDING'):e.fleet.snapshot(e.policy['epochs'][0],e.service)


def test_wrong_wallet_context_and_same_key_new_family_are_rejected(epoch):
    e=epoch;entry=dict(e.policy['epochs'][0],wallet_context='ef'*32)
    with pytest.raises(ValueError,match='EPOCH_WALLET_BINDING'):e.fleet.snapshot(entry,e.service)
    # An existing local family association cannot be bypassed by another completed context/label.
    e.fleet.snapshot(e.policy['epochs'][0],e.service)
    with e.fleet.lane.db() as db:db.execute('UPDATE epoch_key_families SET context=?',('ef'*32,))
    with pytest.raises(ValueError,match='EPOCH_KEY_FAMILY_UNSUPPORTED'):e.fleet.snapshot(e.policy['epochs'][0],e.service)


@pytest.mark.parametrize('change',['remove','reopen','board','archive','key'])
def test_old_epoch_membership_and_transport_history_cannot_be_erased(epoch,change):
    e=epoch;c=deepcopy(e.policy)
    if change=='remove':c['epochs']=[]
    elif change=='reopen':c['epochs'][0]['mode']='ISSUING'
    elif change=='board':c['epochs'][0]['signing_board']=address(99)
    else:
        ac=deepcopy(e.service.c)
        if change=='archive':ac['directory']=str(e.tmp_path/'other-archive')
        else:ac['key_files']=[private(e.tmp_path/'other-key.json',dict(epoch='2',**crypto.call('transport_key')))]
        private(Path(c['epochs'][0]['address_config']),ac)
    with pytest.raises(ValueError,match='EPOCH_RETENTION_REQUIRED|EPOCH_BINDING_CHANGED'):m.Fleet(c)


def test_retains_old_transport_keys_when_adding_new_one(epoch):
    e=epoch;ac=deepcopy(e.service.c)
    ac['key_files'].append(private(e.tmp_path/'new-key.json',dict(epoch='2',**crypto.call('transport_key'))))
    private(Path(e.policy['epochs'][0]['address_config']),ac)
    restarted=m.Fleet(e.policy)
    assert len(restarted.configs[e.manifest.context.hex()]['key_files'])==2


def test_missing_fleet_journal_is_recovery_failure_not_new_empty_fleet(epoch):
    e=epoch;e.fleet.path.unlink()
    with pytest.raises(ValueError,match='EPOCH_JOURNAL_RECOVERY_REQUIRED'):m.Fleet(e.policy)


def test_archive_witness_survives_watch_backup_and_history_loss(epoch,watched):
    e=epoch;w=watched;entry=e.policy['epochs'][0]
    backup=w.store.path.parent/'early.sqlite';w.store.backup(backup)
    w.store.scan(w.bitcoin,max_blocks=3)
    assert e.fleet.retain_archive(entry,w.store,'ADDRESS')['status']=='ARCHIVE_RETAINED'
    old=Store(backup,w.scope)
    lost=e.fleet.retain_archive(entry,old,'ADDRESS')
    assert lost['status']=='ARCHIVE_RECOVERY_REQUIRED' and int(lost['missing'])>0
    assert e.fleet.retain_archive(entry,w.store,'ADDRESS')['status']=='ARCHIVE_RETAINED'
    with w.store.journal.db() as db:
        value=json.loads(db.execute('SELECT immutable FROM address_intents').fetchone()[0]);value['address']='tampered'
        db.execute('UPDATE address_intents SET immutable=?',(canonical(value),))
    with pytest.raises(ValueError,match='EPOCH_ARCHIVE_CONFLICT'):e.fleet.retain_archive(entry,w.store,'ADDRESS')


def test_late_old_epoch_deposit_and_reorg_are_watched_after_restart(epoch,watched,monkeypatch):
    e=epoch;w=watched;entry=e.policy['epochs'][0]
    e.service.store=w.store;e.service.poll=lambda:(_ for _ in ()).throw(RuntimeError('RPC password must not escape'))
    monkeypatch.setattr(e.fleet,'snapshot',lambda *_:dict(block=e.block(20)))
    monkeypatch.setattr(e.fleet,'import_addresses',lambda *_:dict(status='fixture existing registered descriptor'))
    monkeypatch.setattr(e.fleet,'watch',lambda _e,client:(w.bitcoin,client.store.scan(w.bitcoin,max_blocks=3)))
    monkeypatch.setattr(e.fleet,'fees',lambda *_:dict(status='fixture no monetary acceptance'))
    first=e.fleet.run_epoch(entry)
    assert first['watch']['status']=='WATCH_COVERED' and first['transport']['reason']=='RuntimeError'
    assert 'password' not in canonical(first)
    late=tx(bytes.fromhex(w.desc['script_pubkey']),tag=19)
    w.view.chain.append(block(w.view.chain[-1]['hash'],[late],20))
    e.service.store=Store(w.store.path,w.scope)
    second=e.fleet.run_epoch(entry)
    assert second['watch']['status']=='WATCH_COVERED' and len(e.service.store.outputs(w.intent.intent_id.hex()))==2
    old=w.view.chain[-1];w.view.chain[-1]=block(w.view.chain[-2]['hash'],[tx(b'\x51',tag=21)],22)
    e.fleet.run_epoch(entry)
    assert len(e.service.store.outputs(w.intent.intent_id.hex()))==1
    with w.store.journal.db() as db:assert db.execute('SELECT 1 FROM address_observations WHERE hash=?',(old['hash'],)).fetchone()


def test_fair_bounded_scheduling_continues_after_bad_epoch(epoch,monkeypatch):
    e=epoch;e.fleet.c=deepcopy(e.policy)
    e.fleet.c['epochs'].append(dict(e.policy['epochs'][0],wallet_context='ee'*32))
    seen=[]
    def run(entry,**kw):
        seen.append(entry['wallet_context'])
        if len(seen)==1:raise RuntimeError('private remote details')
        return dict(status='OBSERVED',ready=False)
    monkeypatch.setattr(e.fleet,'run_epoch',run)
    assert e.fleet.run_once()['epochs'][0]['reason']=='RuntimeError'
    assert e.fleet.run_once()['epochs'][0]['status']=='OBSERVED'
    assert seen==[e.manifest.context.hex(),'ee'*32]


def test_readonly_service_review_requires_closed_draining_and_qualification(epoch):
    e=epoch;entry=e.policy['epochs'][0]
    review=e.fleet.service_review(entry)
    assert review['custody']['state']==1 and review['custody']['issuance_closed']
    e.verifier.functions.values['QUALIFIED']=False
    with pytest.raises(ValueError,match='EPOCH_PROFILE_UNQUALIFIED'):e.fleet.service_review(entry)


@pytest.mark.parametrize('state,closed',[(0,False),(0,True),(1,False),(2,True),(3,True)])
def test_unsafe_retired_state_never_passes_handover(epoch,state,closed):
    e=epoch;e.view.custody=state;e.closed=closed
    with pytest.raises(ValueError,match='EPOCH_SERVICE_RESTRICTED'):e.fleet.service_review(e.policy['epochs'][0])


def packets(e,request,review):
    result=[]
    for a in e.accounts:
        body=dict(version='1',fleet_id=e.policy['fleet_id'],challenge=request['challenge'],request_hash=digest(request),wallet_context=e.manifest.context.hex(),
            participant=a.address,service_fingerprint=review['service_fingerprint'],custody=review['custody'],derivation=review['derivation'],
            anchor=dict(number='20',hash=e.block(20).hash.hex()),issued_at='1000',expires_at='1600',public_share=e.shares[a.address])
        result.append(dict(body=body,signature=Account.sign_message(retention.message(body),a.key).signature.hex()))
    return result


def test_distinct_original_threshold_and_expiry_enforced_with_real_identity_signatures(epoch):
    e=epoch;request=retention.challenge(e.fleet,[]);current=e.fleet.service_review(e.policy['epochs'][0]);p=packets(e,request,current)
    assert retention.review(e.fleet,request,p[:1])['status']=='BLOCKED'
    assert retention.review(e.fleet,request,p)['status']=='RETENTION_CHECKED'
    assert retention.review(e.fleet,request,[p[0],p[0]])['status']=='BLOCKED'
    e.view.timestamp=1600
    assert retention.review(e.fleet,request,p)['status']=='BLOCKED'


def test_departing_participant_never_counts_and_new_challenge_blocks_replay(epoch):
    e=epoch;request=retention.challenge(e.fleet,[e.accounts[0].address]);current=e.fleet.service_review(e.policy['epochs'][0]);p=packets(e,request,current)
    assert retention.review(e.fleet,request,p)['status']=='BLOCKED'
    fresh=retention.challenge(e.fleet,[])
    assert retention.review(e.fleet,fresh,p)['status']=='BLOCKED'
    changed=dict(request,departing=[])
    with pytest.raises(ValueError,match='EPOCH_ATTESTATION'):retention.verify(e.fleet,changed,p[0],current)


@pytest.mark.parametrize('mutation',['generation','signature','participant','anchor','fingerprint'])
def test_stale_or_forged_retention_claims_never_count(epoch,mutation):
    e=epoch;request=retention.challenge(e.fleet,[]);current=e.fleet.service_review(e.policy['epochs'][0]);p=packets(e,request,current)
    if mutation=='generation':e.cg+=1
    elif mutation=='signature':p[0]['signature']=p[1]['signature']
    elif mutation=='participant':p[0]['body']['participant']=address(100)
    elif mutation=='anchor':e.view.reorg=True
    else:p[0]['body']['service_fingerprint']='ff'*32
    assert retention.review(e.fleet,request,p)['status']=='BLOCKED'


def test_attestation_checks_real_local_scalar_and_existing_nonce_journal(epoch,monkeypatch):
    e=epoch;mfest=e.manifest;account=e.accounts[0];request=retention.challenge(e.fleet,[])
    state=SecureState(str(e.tmp_path/'signer'/'state'),'local test password')
    key=dict(stage='active',manifest=mfest.to_dict(),participant=account.address,index=1,result_hash=e.template.wallet_result.hex(),
        result=dict(group=VECTOR['base_group'],signing_share=(11).to_bytes(32,'big').hex(),public_share=e.shares[account.address],
            key_package='01'*128,public_package='02'*200))  # Package serialization is an explicit fixture here.
    state.put(mfest.context.hex(),key)
    graph=dict(usage='BTC_INPUT_KEYS_V1',contracts=dict(address_registry=dict(address=e.reg.address),batches=dict(address=e.batch.address)))
    monkeypatch.setattr('signer_agent.bitcoin_trust.require_registry',lambda *a,**kw:dict(graph=graph))
    monkeypatch.setattr('signer_agent.bitcoin_onchain.checked_board',lambda *a:e.signing)
    packet=retention.attest(e.fleet,request,mfest,SimpleNamespace(),state,account.key,e.config_file)
    assert retention.verify(e.fleet,request,packet,e.fleet.service_review(e.policy['epochs'][0]))==account.address
    assert Path(str(state.path)+'.spent.sqlite').exists()
    assert not any(v.get('status')=='nonce-created' for v in state._load()['sessions'].values())
    assert retention.attest(e.fleet,request,mfest,SimpleNamespace(),state,account.key,e.config_file)==packet
    key['result']['signing_share']=(12).to_bytes(32,'big').hex();state.put(mfest.context.hex(),key)
    with pytest.raises(ValueError,match='EPOCH_SIGNER_UNAVAILABLE'):
        retention.attest(e.fleet,request,mfest,SimpleNamespace(),state,account.key,e.config_file)


def test_retired_issuance_enrollment_survives_dropping_cli_option(epoch):
    e=epoch;e.fleet.configuration=e.config_file
    e.fleet.snapshot(e.policy['epochs'][0],e.service)
    with pytest.raises(ValueError,match='EPOCH_ISSUANCE_CLOSED'):
        m.issuance_guard(e.service)


def test_observation_client_cannot_sign_or_issue_and_skips_only_derivation_execution(monkeypatch):
    seen=[]
    monkeypatch.setattr('signer_agent.address_transport.Client.context',lambda self,**kw:seen.append(kw))
    client=m.ObservationClient.__new__(m.ObservationClient);client.context()
    assert seen==[dict(verify_derivation=False)]
    for method in ('make_intent','request','send'):
        with pytest.raises(ValueError,match='EPOCH_OBSERVATION_ONLY'):getattr(client,method)()


def test_fee_refund_watch_survives_verifier_quarantine(epoch,watched,monkeypatch):
    e=epoch;w=watched
    from signer_agent.withdrawal_fee_intent import WithdrawalFeeIntentV1
    from signer_agent.withdrawal_fee_watch import FeeWatch
    intent=WithdrawalFeeIntentV1(w.intent,b'f'*32,b'r'*32,b's'*32);desc=intent.derive(w.base)
    scope=dict(purpose='WITHDRAWAL_FEE',chain_id='31337',bitcoin_genesis=w.intent.bitcoin_genesis.hex(),
        bitcoin_network='regtest',base_group=w.base.hex(),ledger_scope=intent.credits_scope.hex())
    store=FeeWatch(e.tmp_path/'fee-watch.sqlite',scope)
    store.accept(intent.encode(),w.base,'regtest','old-refund',{})
    w.view.chain=[w.genesis,block(w.genesis['hash'],[tx(bytes.fromhex(desc['script_pubkey']),tag=24)],25)]
    monkeypatch.setattr('signer_agent.btc_fee_reserve.load_config',lambda _:dict(version='2',fee_watch_databases=[str(store.path)]))
    client=SimpleNamespace(c=e.service.c,scope=scope)
    observed=e.fleet.observe_fee_archives(e.policy['epochs'][0],client,w.bitcoin)
    assert observed['archives'][0]['observation']['authority']=='OBSERVATION_ONLY'
    assert observed['archives'][0]['observation']['watch']['status']=='WATCH_COVERED'
    assert len(store.outputs(intent.intent_id.hex()))==1


def test_shared_evm_payer_cannot_count_same_balance_for_two_epochs(epoch,monkeypatch):
    e=epoch;e.fleet.c=deepcopy(e.policy)
    second=replace(e.manifest,session_id=b'n'*32)
    e.fleet.c['epochs'].append(dict(e.policy['epochs'][0],wallet_context=second.context.hex()))
    first=e.fleet.service_review(e.policy['epochs'][0]);first['balance']='15000000'
    other=dict(first,snapshot=dict(first['snapshot'],manifest=second))
    monkeypatch.setattr(e.fleet,'service_review',lambda entry:first if entry['wallet_context']==e.manifest.context.hex() else other)
    request=retention.challenge(e.fleet,[]);p=packets(e,request,first)
    for original,a in zip(deepcopy(p),e.accounts):
        original['body']['wallet_context']=second.context.hex()
        original['signature']=Account.sign_message(retention.message(original['body']),a.key).signature.hex();p.append(original)
    result=retention.review(e.fleet,request,p)
    assert result['status']=='BLOCKED' and all(r['reason']=='EPOCH_EVM_FUNDING_REQUIRED' for r in result['epochs'])


def test_transport_secret_and_unfunded_payer_block_retention(epoch):
    e=epoch;entry=e.policy['epochs'][0];e.w3.eth.get_balance=lambda *a,**kw:1
    with pytest.raises(ValueError,match='EPOCH_EVM_FUNDING_REQUIRED'):e.fleet.service_review(entry)
    e.w3.eth.get_balance=lambda *a,**kw:1000000000
    e.service.keys[1]['secret']='01'*32
    with pytest.raises(ValueError,match='ADDRESS_LOCAL_KEY_REQUIRED'):e.fleet.service_review(entry)


def test_unknown_signed_evm_obligation_blocks_until_exact_source_receipt(epoch):
    e=epoch;entry=e.policy['epochs'][0]
    shared=Path(e.service.c['identity_file']).resolve().with_suffix('.evm-nonce.lock.sqlite')
    identity=Journal(shared);source=Journal(e.tmp_path/'source.sqlite')
    account=e.accounts[1]
    signed=account.sign_transaction(dict(chainId=31337,nonce=0,to=address(100),value=0,gas=21000,gasPrice=1))
    raw=bytes(signed.raw_transaction);txid=keccak(raw).hex()
    source.put('source-op',dict(id='exact-original-step'))
    identity.put(txid,dict(chain_id='31337',source_journal=str(source.path),operation='source-op',tx_hash=txid,raw_transaction=raw.hex(),nonce='0'))
    with pytest.raises(ValueError,match='EPOCH_EVM_RECOVERY_REQUIRED'):e.fleet.service_review(entry)
    source.update('source-op','CONFIRMED',dict(block_hash=e.block(19).hash.hex()))
    e.w3.eth.get_transaction_receipt=lambda _:SimpleNamespace(status=1,blockNumber=19,blockHash=e.block(19).hash)
    assert e.fleet.service_review(entry)['balance']=='1000000000'
    # Inspection neither resends the envelope nor rewrites its nonce ownership.
    assert identity.get(txid)['status']=='PREPARED'
    e.w3.eth.get_transaction_receipt=lambda _:SimpleNamespace(status=1,blockNumber=19,blockHash=b'x'*32)
    with pytest.raises(ValueError,match='EPOCH_EVM_RECOVERY_REQUIRED'):e.fleet.service_review(entry)


def test_enrolled_epoch_is_checked_before_actual_bitcoin_nonce_creation(signing,monkeypatch,tmp_path):
    e=signing;state=e.states[0]
    marker=dict(fleet_id='aa'*32,configuration=str(tmp_path/'epochs.json'))
    m.signer_enrollment(state,e.manifest.context.hex(),binding=marker)
    monkeypatch.setattr(m,'load_config',lambda _:dict(fleet_id=marker['fleet_id']))
    fleet=SimpleNamespace(c=dict(fleet_id=marker['fleet_id']),entry=lambda _:dict(wallet_context=e.manifest.context.hex(),signing_board=e.plan['signing_board']),
        configs={e.manifest.context.hex():dict(registry=e.reg.address)},service_review=lambda _:(_ for _ in ()).throw(ValueError('EPOCH_ARCHIVE_REQUIRED')))
    monkeypatch.setattr(m,'Fleet',lambda *a,**kw:fleet)
    from signer_agent.bitcoin_worker import sign_step
    with pytest.raises(ValueError,match='EPOCH_ARCHIVE_REQUIRED'):
        sign_step('btc-commit',e.manifest,e.key,e.plan,e.registry,state,e.bitcoin)
    assert state.get('btc:'+e.manifest.context.hex()+':'+e.plan['batch_id']+':0') is None
    assert state.get('btc-epoch-service:'+e.manifest.context.hex())==marker


@pytest.mark.parametrize("guarded", [False, True])
def test_stale_encrypted_signer_backup_cannot_remove_independent_epoch_enrollment(epoch, guarded):
    e=epoch; path = e.tmp_path / 'retained-signer' / 'state'
    if guarded:
        volume = e.tmp_path / 'external'
        volume.mkdir(mode=0o700)
        state = SecureState(path, 'test password', nonce_ledger=volume / 'spent.sqlite')
        state.initialize_nonce_ledger()
    else:
        state = SecureState(path, 'test password')
        with state._journal():pass
    before=state.path.read_bytes();marker=dict(fleet_id=e.policy['fleet_id'],configuration=e.config_file)
    m.signer_enrollment(state,e.manifest.context.hex(),binding=marker)
    state.path.write_bytes(before)
    state = SecureState(path, 'test password')
    assert state.get('btc-epoch-service:'+e.manifest.context.hex()) is None
    assert m.signer_enrollment(state,e.manifest.context.hex())==marker
    with pytest.raises(ValueError,match='EPOCH_BINDING_CHANGED'):
        m.signer_enrollment(state,e.manifest.context.hex(),binding=dict(marker,fleet_id='ff'*32))


def test_funded_new_epoch_requires_fresh_old_epoch_retention_before_issuance(epoch,monkeypatch):
    e=epoch;entry=e.policy['epochs'][0]
    # A distinct new graph is reviewed through its normal canonical snapshot;
    # this fixture isolates the durable client routing gate.
    e.fleet.c=deepcopy(e.policy);e.fleet.c['epochs'][0]['mode']='ISSUING'
    e.fleet.c['epochs'].append(dict(entry,wallet_context='ee'*32,mode='SERVICING'))
    e.fleet.configs['ee'*32]=dict(e.service.c,registry=address(90),directory=str(e.tmp_path/'old-replica'))
    monkeypatch.setattr(m,'load_config',lambda _:e.fleet.c)
    monkeypatch.setattr(m,'Fleet',lambda *a,**kw:e.fleet)
    monkeypatch.setattr(e.fleet,'snapshot',lambda *_:None)
    with pytest.raises(ValueError,match='EPOCH_RETENTION_REQUIRED'):m.issuance_guard(e.service,e.config_file)


def test_final_generation_change_while_collecting_epochs_invalidates_review(epoch,monkeypatch):
    e=epoch;request=retention.challenge(e.fleet,[]);current=e.fleet.service_review(e.policy['epochs'][0]);p=packets(e,request,current)
    old_verify=retention.verify;calls=[]
    def verify(*args):
        who=old_verify(*args);calls.append(who)
        if len(calls)==2:e.cg+=1
        return who
    monkeypatch.setattr(retention,'verify',verify)
    result=retention.review(e.fleet,request,p)
    assert result['status']=='BLOCKED' and result['epochs'][0]['reason']=='EPOCH_GENERATION_CHANGED'
