"""Actual bootstrap/signing/PSBT/inclusion code; EVM/Core execution and graph origins are fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from web3 import Web3

from ops.zkspv import contour_bootstrap as boot,contour_funding as funding
from ops.zkspv.contour import digest
from signer_agent.bootstrap import Bootstrap
from signer_agent.bitcoin_network import header_hash
from signer_agent.bitcoin_rpc import BitcoinRPCError
from signer_agent.zk_worker import Worker,Inbox
from signer_agent.zk_worker_prover import write
from test_bootstrap_parallel import fixture as bootstrap_fixture
from test_smp1_deposit import client
from test_zk_worker import config as worker_config
from test_zk_bootstrap import zk_invite,profile


def bootstrap(tmp_path):
    runner,identity=bootstrap_fixture(tmp_path,2)
    runner.w3.eth.block_number=100
    runner.profile['steps'].append(dict(id='dependent',kind='deploy',artifact='Fixture'))
    # These are explicit graph/provenance fixtures; actual envelopes and original
    # sequential/parallel engines plus global nonce journal remain real.
    wc=worker_config(tmp_path);c=dict(identity_file=identity,max_tx_gas='50000',
        max_transaction_cost_wei='50000',max_total_cost_wei='200000')
    runner.profile.update(bitcoin_network='mainnet',bitcoin_policy=wc['bitcoin_policy'],chain_id=wc['chain_id'])
    request=dict(kind='deposit',consumer='fixture-pool',txid='aa'*32,vout='0')
    runner.contract=lambda name:NS(address=wc['zk_state'] if name=='relay' else 'fixture-pool' if name=='mining' else 'fixture-adapter')
    original=runner.build_transaction
    def build(step,*args):
        tx=original(step,*args);runner.contour.reserve(step,tx);return tx
    runner.build_transaction=build
    return runner,c,wc,request


def call_boot(e,action):
    return boot.Pipeline(*e,dict(capability='bootstrap',action=action)).execute(action)


def test_bootstrap_runner_advances_one_original_group_then_dependent_unit(tmp_path):
    e=bootstrap(tmp_path);runner=e[0]
    assert call_boot(e,'probe')['status']=='ABSENT' and not runner.w3.eth.sent
    assert call_boot(e,'run')['status']=='ABSENT'
    assert len(runner.w3.eth.sent)==2 and runner.prepared==['leaf0','leaf1']
    assert call_boot(e,'recover')['status']=='SATISFIED' and len(runner.w3.eth.sent)==3
    original=list(runner.w3.eth.sent)
    assert call_boot(e,'probe')['status']=='SATISFIED' and runner.w3.eth.sent==original
    assert runner.journal.get(runner.prefix)['status']=='GRAPH_CONFIGURED_FRESHNESS_PENDING'


@pytest.mark.parametrize('group',[False,True])
def test_bootstrap_unsigned_interruption_holds_original_unit(tmp_path,group):
    e=bootstrap(tmp_path);runner=e[0]
    if not group:runner.profile['steps']=[dict(id='single',kind='deploy',artifact='Fixture')]
    original=runner.build_transaction
    runner.build_transaction=lambda *a:(_ for _ in ()).throw(RuntimeError('unsigned fixture interruption'))
    with pytest.raises(RuntimeError,match='unsigned fixture'):call_boot(e,'run')
    runner.build_transaction=original
    assert call_boot(e,'recover')['status']=='RECOVERY_REQUIRED'
    assert not runner.w3.eth.sent and not runner.prepared


def test_bootstrap_signed_group_recovery_reuses_global_bytes(tmp_path,monkeypatch):
    from signer_agent.operations import Journal
    e=bootstrap(tmp_path);runner=e[0];original=Journal.update;crashed=False
    def update(journal,key,status,result):
        nonlocal crashed
        if journal.path==runner.journal.path and status=='SIGNED' and not crashed:
            crashed=True;raise RuntimeError('global signed reservation fixture')
        return original(journal,key,status,result)
    monkeypatch.setattr(Journal,'update',update)
    with pytest.raises(RuntimeError,match='global signed'):call_boot(e,'run')
    monkeypatch.setattr(Journal,'update',original)
    # A parallel group can have only one of its envelopes reserved at a crash.
    # The contour must hold, rather than allocating the missing second nonce.
    assert call_boot(e,'recover')['status']=='RECOVERY_REQUIRED'
    assert not runner.w3.eth.sent


def test_bootstrap_no_enrollment_cannot_gain_first_permission_on_recovery(tmp_path):
    e=bootstrap(tmp_path)
    with pytest.raises(ValueError,match='ENROLLMENT_REQUIRED'):call_boot(e,'recover')
    assert not e[0].w3.eth.sent


@pytest.mark.parametrize('create_group',[False,True])
def test_bootstrap_original_signed_reservation_is_recovered_without_new_build(tmp_path,monkeypatch,create_group):
    from signer_agent.operations import Journal
    e=bootstrap(tmp_path);runner=e[0]
    if create_group:
        runner.profile['steps']=runner.profile['steps'][:2]
        for step in runner.profile['steps']:
            del step['parallel_group'];step.update(create_group='circular',gas='100000')
    else:runner.profile['steps']=[dict(id='single',kind='deploy',artifact='Fixture')]
    original=Journal.update
    def update(journal,key,status,result):
        if journal.path==runner.journal.path and status=='SIGNED':raise RuntimeError('signed bytes retained globally')
        return original(journal,key,status,result)
    monkeypatch.setattr(Journal,'update',update)
    with pytest.raises(RuntimeError,match='signed bytes retained'):call_boot(e,'run')
    prepared=list(runner.prepared);assert not runner.w3.eth.sent
    monkeypatch.setattr(Journal,'update',original)
    assert call_boot(e,'recover')['status']=='SATISFIED'
    assert runner.prepared==prepared and len(runner.w3.eth.sent)==len(prepared)
    assert call_boot(e,'probe')['status']=='SATISFIED'


def test_bootstrap_group_budget_failure_spends_no_prefix(tmp_path):
    e=bootstrap(tmp_path);e[1]['max_total_cost_wei']='21000'
    with pytest.raises(ValueError,match='TOTAL_BUDGET'):call_boot(e,'run')
    assert not e[0].w3.eth.sent
    assert call_boot(e,'recover')['status']=='RECOVERY_REQUIRED'


def test_stale_bootstrap_reader_reloads_the_durable_unsigned_latch(tmp_path):
    e=bootstrap(tmp_path);a=boot.Pipeline(*e,dict(capability='bootstrap',action='run'))
    stale=boot.Pipeline(*e,dict(capability='bootstrap',action='recover'))
    original=e[0].build_transaction
    e[0].build_transaction=lambda *args:(_ for _ in ()).throw(RuntimeError('before signature'))
    with pytest.raises(RuntimeError):a.execute('run')
    e[0].build_transaction=original
    assert stale.execute('recover')['status']=='RECOVERY_REQUIRED'
    assert not e[0].w3.eth.sent


def test_bootstrap_budget_reserves_once_per_original_step_and_survives_plan_retry(tmp_path):
    e=bootstrap(tmp_path);p=boot.Pipeline(*e,dict(capability='bootstrap',action='run'))
    tx=dict(chainId=31337,gas=21000,gasPrice=1,value=0)
    p.reserve(dict(id='a'),tx);p.reserve(dict(id='a'),tx)
    q=boot.Pipeline(*e,dict(capability='bootstrap',action='recover'))
    assert sum(int(v) for v in q.reservations.values())==21000
    with pytest.raises(ValueError,match='TRANSACTION_BUDGET'):q.reserve(dict(id='b'),dict(tx,gas=50001))
    for i in range(8):q.reserve(dict(id=str(i)),tx)
    with pytest.raises(ValueError,match='TOTAL_BUDGET'):q.reserve(dict(id='last'),tx)


def test_structural_readiness_is_deferred_without_confirmed_or_verified_marker(tmp_path):
    e=bootstrap(tmp_path);runner=e[0]
    runner.profile['steps']=[dict(id='bitcoin_relay_ready',kind='bitcoin_ready')]
    runner.bitcoin_binding=lambda:dict(status='ZK_BINDING_VERIFIED_FRESHNESS_DEFERRED',ready=False)
    runner.bitcoin_ready=lambda *a,**kw:dict(status='MAINNET_RELAY_READY')
    assert runner.apply(defer_zk_freshness=True)['status']=='GRAPH_CONFIGURED_FRESHNESS_PENDING'
    assert runner.journal.get(runner.prefix+':bitcoin_relay_ready')['status']=='ZK_FRESHNESS_DEFERRED'
    # The ordinary engine still performs its normal freshness path.
    assert runner.apply()['status']=='VERIFIED'
    assert runner.journal.get(runner.prefix+':bitcoin_relay_ready')['status']=='CONFIRMED'


def test_generated_bootstrap_profile_scope_and_explicit_step_rejection(tmp_path):
    p=tmp_path/'profile.json';cfg=dict(profile_file=str(p))
    raw=profile(zk_invite());write(p,raw)
    wc=dict(chain_id=raw['chain_id'],evm_confirmations=raw['evm_confirmations'],bitcoin_policy=raw['bitcoin_policy'])
    assert boot.profile(cfg,wc)['steps']
    with pytest.raises(ValueError,match='SCOPE'):boot.profile(cfg,dict(wc,chain_id='11155111'))
    write(p,dict(raw,steps=[]))
    with pytest.raises(ValueError,match='GENERATED_PROFILE_REQUIRED'):boot.profile(cfg,wc)


def test_bootstrap_expected_state_address_is_checked_before_signing(tmp_path):
    e=bootstrap(tmp_path);p=boot.Pipeline(*e,dict(capability='bootstrap',action='run'))
    tx=dict(chainId=31337,nonce=9,gas=21000,gasPrice=1,value=0)
    with pytest.raises(ValueError,match='EXPECTED_ADDRESS'):p.reserve(dict(id='relay',kind='deploy'),tx)
    assert not p.reservations and not e[0].w3.eth.sent


def test_binding_only_check_authenticates_state_adapter_core_and_block_without_claiming_freshness(tmp_path,monkeypatch):
    from signer_agent import zk_spv,bitcoin_network
    e=bootstrap(tmp_path);runner=e[0];runner.profile['evm_confirmations']='1'
    runner.bitcoin=NS();seen=[]
    def inspect(w3,state,policy,number,*,adapter_address):
        seen.append((state,adapter_address,number));return NS(address=state),NS(address=adapter_address),{},[]
    monkeypatch.setattr(zk_spv,'inspect_binding',inspect)
    monkeypatch.setattr(bitcoin_network,'check_core',lambda *a:seen.append('core'))
    result=runner.bitcoin_binding()
    assert result['status']=='ZK_BINDING_VERIFIED_FRESHNESS_DEFERRED' and not result['ready']
    assert seen[0][2]==100 and seen[1]=='core'


def test_freshness_deferral_cannot_be_applied_to_regtest_or_legacy_profiles(tmp_path):
    e=bootstrap(tmp_path);e[0].profile['bitcoin_network']='regtest'
    with pytest.raises(ValueError,match='BINDING_PROFILE'):e[0].apply(defer_zk_freshness=True)
    assert not e[0].w3.eth.sent


@pytest.fixture
def funded(client,tmp_path):
    d,quote,values,ctx=client;q=quote();txid=q['quote']['txid'];wc=worker_config(tmp_path)
    wc['allowed_consumers']={q['quote']['graph']['pool']:'MiningPoolV1'}
    w=Worker.__new__(Worker);w.c=wc;w.lock_fd=None;w.inbox=Inbox(wc);w.w3=d.w3
    need=dict(kind='deposit',consumer=q['quote']['graph']['pool'],txid=txid,vout=q['quote']['vout'])
    raw=bytearray(80);raw[:4]=(1).to_bytes(4,'little');raw[36:68]=bytes.fromhex(txid)[::-1];raw[68:72]=(1500000000).to_bytes(4,'little')
    header=raw.hex();blockhash=header_hash(raw)[::-1].hex();tip='99'*32
    env=NS(worker=w,deposit=d,quote=q,need=need,ctx=ctx,core=dict(blocks=32262,bestblockhash=tip),
        header=header,blockhash=blockhash,canonical=blockhash,corrupt_merkle=False,completed=False,visible=True)
    old=d.bitcoin.call
    def call(method,*args):
        if method=='getrawtransaction' and args[1] is True:
            if not env.visible or d.bitcoin.sent is None:raise BitcoinRPCError('BITCOIN_RPC_REJECTED',-5)
            return dict(hex=d.bitcoin.sent,blockhash=env.blockhash,confirmations=1000)
        if method=='getblockheader':return dict(height=32257) if args[1] else env.header
        if method=='getblockhash':return env.canonical if args[0]==32257 else tip
        if method=='getblock':return dict(tx=[env.need['txid']],merkleroot='00'*32 if env.corrupt_merkle else env.need['txid'],confirmations=6)
        return old(method,*args)
    d.bitcoin.call=call;w.bitcoin=d.bitcoin
    w.context=lambda **kw:(ctx['block'],None,ctx['spv'],{},env.core)
    w.observe_need=lambda req,b,a,core:dict(status='COMPLETED') if env.completed else w._observe_transaction(req,b,a,core,6,bytes.fromhex(ctx['identity']['script']))
    env.c=dict(version='1',mode='SMP1_QUOTE',operation_id=q['quote']['operation_id'],accepted_quote_hash=q['quote_hash'],deposit_config='/fixture/smp1.json')
    def execute(action='probe',*,observe=False):
        c=dict(version='1',mode='OBSERVE') if observe else env.c
        return funding.Pipeline(w,env.need,c,dict(capability='bitcoin_funding',action=action),deposit=None if observe else d).execute(action)
    env.execute=execute
    yield env
    w.inbox.close()


def test_funding_runner_uses_original_quote_sign_send_and_true_merkle_codec(funded):
    e=funded
    assert e.execute()['status']=='ABSENT'
    assert e.execute('run')['status']=='ABSENT'
    assert e.deposit.journal.get('deposit:'+e.c['operation_id'])['status']=='SIGNED'
    assert e.execute('recover')['status']=='ABSENT'
    result=e.execute()
    assert result['status']=='SATISFIED' and result['observations']['funding']['amount_sats']=='100000'
    calls=e.deposit.bitcoin.calls
    assert sum(m=='walletprocesspsbt' for m,_ in calls)==1 and sum(m=='sendrawtransaction' for m,_ in calls)==1
    assert not result['observations']['ready']


def test_funding_unsigned_interruption_cannot_request_a_second_signature(funded,monkeypatch):
    e=funded;original=e.deposit.sign
    monkeypatch.setattr(e.deposit,'sign',lambda *a:(_ for _ in ()).throw(RuntimeError('before wallet signature')))
    with pytest.raises(RuntimeError,match='before wallet'):e.execute('run')
    monkeypatch.setattr(e.deposit,'sign',original)
    assert e.execute('recover')['status']=='RECOVERY_REQUIRED'
    assert not e.deposit.bitcoin.sent and not any(m=='walletprocesspsbt' for m,_ in e.deposit.bitcoin.calls)


def test_stale_funding_reader_reloads_original_signing_latch(funded,monkeypatch):
    e=funded;stale=funding.Pipeline(e.worker,e.need,e.c,dict(capability='bitcoin_funding',action='recover'),deposit=e.deposit)
    original=e.deposit.sign
    monkeypatch.setattr(e.deposit,'sign',lambda *a:(_ for _ in ()).throw(RuntimeError('before signature')))
    with pytest.raises(RuntimeError):e.execute('run')
    monkeypatch.setattr(e.deposit,'sign',original)
    assert stale.execute('recover')['status']=='RECOVERY_REQUIRED' and not e.deposit.bitcoin.sent


def test_funding_lost_send_response_adopts_same_raw_without_another_broadcast(funded):
    e=funded;e.execute('run');e.deposit.bitcoin.reply_loss=True
    with pytest.raises(BitcoinRPCError):e.execute('recover')
    assert e.execute()['status']=='SATISFIED'
    assert sum(m=='sendrawtransaction' for m,_ in e.deposit.bitcoin.calls)==1


def test_external_address_funding_is_observed_without_wallet_write(funded):
    e=funded;e.need=dict(e.need,kind='address_deposit',intent_id='ab'*32)
    assert e.execute(observe=True)['status']=='WAITING'
    e.deposit.sign(e.c['operation_id'],e.c['accepted_quote_hash']);e.deposit.broadcast(e.c['operation_id'],e.c['accepted_quote_hash'])
    original=len(e.deposit.bitcoin.calls)
    assert e.execute(observe=True)['status']=='SATISFIED'
    assert not any(m in ('walletprocesspsbt','sendrawtransaction','lockunspent') for m,_ in e.deposit.bitcoin.calls[original:])


def test_funding_depth_uses_chain_height_instead_of_claimed_confirmations(funded):
    e=funded;e.execute('run');e.execute('recover');e.core['blocks']=32261
    assert e.execute()['status']=='WAITING'
    e.core['blocks']=32262
    assert e.execute()['status']=='SATISFIED'


@pytest.mark.parametrize('kind,depth',[('deposit',100),('withdrawal_fee',101)])
def test_funding_retains_actual_coinbase_maturity(funded,kind,depth):
    e=funded;script=bytes.fromhex(e.ctx['identity']['script'])
    raw=(b'\x02\x00\x00\x00\x01'+bytes(32)+b'\xff'*4+b'\x02\x01\x01'+b'\xff'*4+
         b'\x01'+(100000).to_bytes(8,'little')+bytes([len(script)])+script+bytes(4))
    txid=header_hash(raw)[::-1].hex();e.need=dict(e.need,kind=kind,txid=txid,vout='0')
    e.deposit.bitcoin.sent=raw.hex();header=bytearray.fromhex(e.header);header[36:68]=bytes.fromhex(txid)[::-1]
    e.header=header.hex();e.blockhash=header_hash(header)[::-1].hex();e.canonical=e.blockhash
    e.core['blocks']=32257+depth-2
    assert e.execute(observe=True)['status']=='WAITING'
    e.core['blocks']+=1;result=e.execute(observe=True)
    assert result['status']=='SATISFIED' and result['observations']['funding']['depth']==depth


def test_funding_rejects_bad_merkle_and_orphaned_original_completion(funded):
    e=funded;e.execute('run');e.execute('recover');e.corrupt_merkle=True
    with pytest.raises(ValueError,match='MERKLE_ROOT'):e.execute()
    e.corrupt_merkle=False;assert e.execute()['status']=='SATISFIED'
    e.canonical='00'*32
    assert e.execute()['status']=='RECOVERY_REQUIRED'


def test_funding_historical_inclusion_remains_checked_after_mint(funded):
    e=funded;e.execute('run');e.execute('recover');assert e.execute()['status']=='SATISFIED'
    e.completed=True
    assert e.execute()['status']=='SATISFIED'
    e.visible=False
    assert e.execute()['status']=='RECOVERY_REQUIRED'


def test_original_funding_cannot_be_silently_replaced_by_remined_header(funded):
    e=funded;e.execute('run');e.execute('recover');assert e.execute()['status']=='SATISFIED'
    header=bytearray.fromhex(e.header);header[79]^=1;e.header=header.hex()
    e.blockhash=header_hash(header)[::-1].hex();e.canonical=e.blockhash
    assert e.execute()['observations']['reason']=='ORIGINAL_FUNDING_CHANGED'


def test_funding_already_completed_need_without_original_inclusion_is_a_hold(funded):
    e=funded;e.completed=True
    assert e.execute()['status']=='RECOVERY_REQUIRED'
    assert not e.deposit.bitcoin.sent


def test_funding_quote_and_scope_cannot_change(funded):
    e=funded;e.c['accepted_quote_hash']='00'*32
    with pytest.raises(ValueError,match='QUOTE_ACCEPTANCE'):e.execute('run')
    assert not any(m=='walletprocesspsbt' for m,_ in e.deposit.bitcoin.calls)


def test_offline_builder_connects_bootstrap_funding_and_pins_profile(tmp_path):
    from ops.zkspv.contour_plan import build
    wc=worker_config(tmp_path);raw=profile(zk_invite());wc.update(bitcoin_policy=raw['bitcoin_policy'])
    need=dict(kind='deposit',consumer=next(iter(wc['allowed_consumers'])),txid='aa'*32,vout='0')
    bc=json.loads(Path('config/zkspv-contour-bootstrap.example.json').read_text())
    bc['profile_file']=str(tmp_path/'profile.json')
    for name,value in [('worker.json',wc),('need.json',need),('profile.json',raw),('bootstrap.json',bc),
                       ('funding.json',dict(version='1',mode='OBSERVE'))]:write(tmp_path/name,value)
    plan=build(tmp_path/'worker.json',tmp_path/'need.json',tmp_path/'run',worker_only=True,
        bootstrap_config=tmp_path/'bootstrap.json',funding_config=tmp_path/'funding.json')
    assert [s['capability'] for s in plan['stages']][:2]==['bootstrap','bitcoin_funding']
    assert digest(boot.profile(bc,wc)) in plan['stages'][0]['run']
    assert 'ops.zkspv.contour_funding' in plan['stages'][1]['run']


def test_runner_cli_chain_connects_structural_bootstrap_and_bitcoin_inclusion(funded,tmp_path,monkeypatch):
    import subprocess
    from ops.zkspv.contour import Contour
    from ops.zkspv.contour_plan import build
    e=funded;e.deposit.sign(e.c['operation_id'],e.c['accepted_quote_hash']);e.deposit.broadcast(e.c['operation_id'],e.c['accepted_quote_hash'])
    folder=tmp_path/'bootstrap';folder.mkdir();b,bc,wc,_=bootstrap(folder)
    b.contract=lambda name:NS(address=e.worker.c['zk_state'] if name=='relay' else e.need['consumer'] if name=='mining' else 'fixture-adapter')
    c=json.loads(Path('config/zkspv-contour-bootstrap.example.json').read_text());c.update(bc)
    c['profile_file']=str(tmp_path/'profile.json');c['state_file']=str(b.journal.path)
    raw=profile(zk_invite())
    for name,value in [('bootstrap.json',c),('profile.json',raw),('funding.json',dict(version='1',mode='OBSERVE')),
                       ('worker.json',e.worker.c),('need.json',e.need)]:write(tmp_path/name,value)
    plan=build(tmp_path/'worker.json',tmp_path/'need.json',tmp_path/'controller',worker_only=True,
        bootstrap_config=tmp_path/'bootstrap.json',funding_config=tmp_path/'funding.json')
    plan['stages']=plan['stages'][:2]
    monkeypatch.setattr(boot,'BudgetedBootstrap',lambda *a,**kw:b)
    monkeypatch.setattr('signer_agent.bitcoin_rpc.BitcoinRPC',lambda *a:e.worker.bitcoin)
    monkeypatch.setattr(funding,'Worker',lambda c:e.worker);monkeypatch.setattr(e.worker,'close',lambda:None)
    original=subprocess.Popen
    def popen(command,*args,**kw):
        if len(command)>2 and command[1]=='-m' and command[2] in ('ops.zkspv.contour_bootstrap','ops.zkspv.contour_funding'):
            monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',kw['env']['SATOHSI_CONTOUR_CONTEXT'])
            (boot if command[2].endswith('contour_bootstrap') else funding).main(command[3:])
            return NS(pid=999999999,wait=lambda **kw:0)
        return original(command,*args,**kw)
    monkeypatch.setattr(subprocess,'Popen',popen)
    runner=Contour(plan)
    for _ in range(8):
        result=runner.step()
        if result['status']=='DRIVERS_COMPLETED_UNQUALIFIED':break
    assert result['status']=='DRIVERS_COMPLETED_UNQUALIFIED'
    assert not result['ready'] and not result['combined_e2e_accepted'] and 'fresh_proof' in result['missing_capabilities']
    assert len(b.w3.eth.sent)==3
