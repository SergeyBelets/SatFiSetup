"""Real SQLite/native derivation/curve checks; canonical EVM/provenance are explicit fixtures."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from eth_account import Account
from eth_utils import keccak
from eth_abi import encode
from signer_agent import bitcoin_continuity as m,crypto,address_mint
from signer_agent.secure_state import SecureState
from signer_agent.operations import Journal,canonical
from signer_agent.continuity_cli import compare
from test_address_transport import delivery,private
from test_address_mint import Functions,address
from test_bitcoin_input_keys import signing,native_keys


@pytest.fixture
def continuity(delivery,monkeypatch):
    e=delivery;e.view.derivation=0;e.cg=1;e.dg=1;e.cseq=0;e.dseq=0;e.closed=False
    e.reg_values.update(custodyGeneration=lambda:e.cg,derivationGeneration=lambda:e.dg,
        custodyStatusNonce=lambda:e.cseq,derivationStatusNonce=lambda:e.dseq,issuanceClosed=lambda:e.closed,
        intentCount=0)
    e.common.functions.values.update(backingCount=0,totalBackingSats=0)
    e.batch.functions.values.update(feeReserve=address(0))
    escrow=SimpleNamespace(address=address(8),functions=Functions(dict(withdrawalFees=address(0),requests=lambda _:[0]*8,
        payment=lambda _:[bytes(32),0,False,[]])))
    original=e.w3.eth.contract
    e.w3.eth.contract=lambda address,**kw:escrow if address==escrow.address else original(address=address,**kw)
    e.source=e.service;e.sample=lambda:m.sample(e.w3,e.ctx,20)
    e.catalog=m.Catalog(identity_file=e.source.c['identity_file'])
    return e


@pytest.mark.parametrize('field',['registry','deposits','escrow','batches','authorization','dkg','spv','graph_id',
    'wallet_id','wallet_result','key_epoch','custody_scope','derivation_scope','verifier','verifier_runtime',
    'verifier_source','qualification','derivation_profile','service_policy','chain_id'])
def test_same_key_cannot_reset_accounting_or_status_with_new_graph_labels(continuity,field):
    e=continuity;old=e.sample();e.catalog.authorize(e.w3,old)
    candidate=deepcopy(old);candidate['identity'][field]='different canonical graph fixture'
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH_CHANGE'):e.catalog.authorize(e.w3,candidate)
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH_CHANGE'):e.catalog.observe(e.w3,candidate,recover=True)
    # The original graph and obligations remain retained after rejection.
    assert e.catalog.authorize(e.w3,e.sample())['status']=='ORIGINAL_GRAPH_RETAINED'


def test_sample_rechecks_actual_common_binding_and_final_anchors(continuity):
    e=continuity;e.common.functions.values['addressRegistry']=address(99)
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH'):e.sample()


def test_network_or_genesis_change_cannot_reset_a_known_key_or_child(continuity):
    e=continuity;s=e.sample();e.catalog.authorize(e.w3,s)
    other=deepcopy(s);other['identity']['bitcoin_genesis']='ef'*32
    other['family']=m.family(bytes.fromhex('ef'*32),bytes.fromhex(other['identity']['base_x']))
    with pytest.raises(ValueError,match='CONTINUITY_NETWORK_KEY_REUSE'):e.catalog.authorize(e.w3,other)
    desc=e.intent.derive(bytes.fromhex('02'+s['identity']['base_x']));key=bytes.fromhex(desc['output_key'])
    e.catalog.alias(e.w3,s,key,e.intent.intent_id.hex());other['identity']['base_x']=key.hex()
    other['family']=m.family(bytes.fromhex('ef'*32),key)
    with pytest.raises(ValueError,match='CONTINUITY_DERIVED_KEY_REUSE'):e.catalog.authorize(e.w3,other)


def test_unchanged_graph_accepts_new_generations_and_uses_one_accounting_domain(continuity):
    e=continuity;first=e.sample();e.catalog.authorize(e.w3,first,'ISSUE')
    e.cg+=1;e.cseq+=1;e.view.custody=1;e.closed=True
    assert e.catalog.authorize(e.w3,e.sample(),'ADDRESS_MINT')['status']=='ORIGINAL_GRAPH_RETAINED'
    with pytest.raises(ValueError,match='CONTINUITY_ISSUANCE_CLOSED'):e.catalog.authorize(e.w3,e.sample(),'ISSUE')
    e.cg+=1;e.cseq+=1;e.view.custody=2
    with pytest.raises(ValueError,match='CONTINUITY_CUSTODY_RESTRICTED'):e.catalog.authorize(e.w3,e.sample(),'BASE_MINT')
    e.cg+=1;e.cseq+=1;e.view.custody=1
    e.catalog.authorize(e.w3,e.sample(),'BASE_SIGN')
    assert first['identity']==e.sample()['identity']


@pytest.mark.parametrize('scope,terminal,mode',[('custody',3,'BASE_MINT'),('derivation',2,'ADDRESS_MINT')])
def test_terminal_and_reorg_evidence_survive_encrypted_state_backup(continuity,scope,terminal,mode):
    e=continuity;state=SecureState(str(e.tmp_path/'signer'/'state'),'test password')
    with state._journal():pass
    before=state.path.read_bytes();catalog=m.Catalog(identity_file=e.source.c['identity_file'],state=state)
    initial=e.sample();catalog.authorize(e.w3,initial)
    if scope=='custody':e.cg=2;e.cseq=1;e.view.custody=terminal;e.closed=True
    else:e.dg=2;e.dseq=1;e.view.derivation=terminal
    catalog.observe(e.w3,e.sample());state.path.write_bytes(before)
    e.cg=1;e.cseq=0;e.dg=1;e.dseq=0;e.closed=False;e.view.custody=0;e.view.derivation=0
    with pytest.raises(ValueError,match='CONTINUITY_'):catalog.authorize(e.w3,e.sample(),mode)
    with pytest.raises(ValueError,match='CONTINUITY_'):catalog.observe(e.w3,e.sample(),recover=True)
    # Exact historical inspection does not claim permission for a new monetary action.
    assert catalog.authorize(e.w3,e.sample(),'READ')['status']=='CONTINUITY_REQUIRED'


def test_revoked_derivation_cannot_block_unrelated_base_mint_but_blocks_address_mint(continuity):
    e=continuity;e.view.derivation=2;e.dg=2;e.dseq=1
    e.catalog.authorize(e.w3,e.sample(),'BASE_MINT')
    with pytest.raises(ValueError,match='CONTINUITY_DERIVATION_RESTRICTED'):e.catalog.authorize(e.w3,e.sample(),'ADDRESS_MINT')


def test_known_public_child_key_cannot_be_reenrolled_as_an_independent_parent(continuity):
    e=continuity;s=e.sample();desc=e.intent.derive(bytes.fromhex('02'+s['identity']['base_x']))
    output=bytes.fromhex(desc['output_key']);alias=e.catalog.alias(e.w3,s,output,e.intent.intent_id.hex())
    candidate=deepcopy(s);candidate['family']=alias;candidate['identity'].update(base_x=output.hex(),registry=address(97))
    with pytest.raises(ValueError,match='CONTINUITY_DERIVED_KEY_REUSE'):e.catalog.authorize(e.w3,candidate)
    # The relationship is derived from canonical intent bytes, never a supplied tweak.
    assert m.family(bytes.fromhex(s['identity']['bitcoin_genesis']),output)==alias


def test_alias_discovered_after_bad_parent_enrollment_still_blocks_that_parent(continuity):
    e=continuity;s=e.sample();key=bytes.fromhex(crypto.call('public_share',share=(13).to_bytes(32,'big').hex())['public'])[1:]
    candidate=deepcopy(s);candidate['family']=m.family(bytes.fromhex(s['identity']['bitcoin_genesis']),key)
    candidate['identity'].update(base_x=key.hex(),registry=address(97));e.catalog.observe(e.w3,candidate)
    e.catalog.alias(e.w3,s,key,'canonical binding fixture')
    with pytest.raises(ValueError,match='CONTINUITY_DERIVED_KEY_REUSE'):e.catalog.authorize(e.w3,candidate)


def test_orphan_anchor_requires_explicit_safe_reanchor_and_does_not_reset_spent_journal(continuity):
    e=continuity;state=SecureState(str(e.tmp_path/'signer'/'state'),'test password')
    catalog=m.Catalog(identity_file=e.source.c['identity_file'],state=state);catalog.authorize(e.w3,e.sample())
    state.put('original-signing-record',dict(status='consumed',public_result='retained'))
    e.view.reorg=True
    with pytest.raises(ValueError,match='CONTINUITY_REANCHOR_REQUIRED'):catalog.authorize(e.w3,e.sample())
    assert catalog.observe(e.w3,e.sample(),recover=True)['status']=='ORIGINAL_GRAPH_RETAINED'
    assert state.get('original-signing-record')['status']=='consumed'


def test_inventory_zero_is_never_an_empty_history_or_migration_proof(continuity):
    e=continuity;result=m.inventory(e.source)
    assert result['total_backing_sats']=='0' and not result['history_complete'] and not result['activation_allowed']
    assert result['known_requests']=={} and result['fees']['status']=='OBSERVED'
    assert 'all consumed/walletOutput keys' in result['non_enumerable']


def test_inventory_keeps_exact_principal_reservation_and_named_pending_obligations(continuity):
    e=continuity;txid=b't'*32;key=keccak(encode(['bytes32','uint32'],[txid,2]))
    e.common.functions.values.update(backingCount=1,backingAt=lambda _:(txid,2,1000),totalBackingSats=1000,
        backingScripts=lambda _:b's'*32,consumed=lambda _:True,walletOutput=lambda _:False)
    e.batch.functions.values.update(reservations=lambda _:b'r'*32,batches=lambda _:[b'p'*32,b'w'*32,1000,2],
        reservationRoot=lambda _:b'r'*32,replacementOf=lambda _:bytes(32),versionCount=lambda _:2)
    result=m.inventory(e.source,requests=['11'*32],batches=['22'*32])
    assert result['backing'][0]['satoshis']=='1000' and result['backing'][0]['reservation']=='72'*32
    assert result['known_batches']['22'*32]['versions']=='2' and not result['history_complete']
    e.common.functions.values['totalBackingSats']=999
    with pytest.raises(ValueError,match='CONTINUITY_ACCOUNTING'):m.inventory(e.source)


def test_bounded_inventory_and_broken_fee_component_never_disappear_as_empty(continuity):
    e=continuity;e.reg_values['intentCount']=257
    with pytest.raises(ValueError,match='CONTINUITY_INVENTORY_LIMIT'):m.inventory(e.source)
    e.reg_values['intentCount']=0;e.batch.functions.values['feeReserve']=address(66)
    result=m.inventory(e.source)
    assert result['fees']['status']=='OBSERVATION_UNAVAILABLE' and not result['activation_allowed']


def test_state_only_catalog_retains_key_family_across_different_contexts(continuity):
    e=continuity;state=SecureState(str(e.tmp_path/'signer'/'state'),'test password');catalog=m.Catalog(state=state)
    old=e.sample();catalog.authorize(e.w3,old)
    other=deepcopy(old);other['identity'].update(wallet_id='ff'*32,wallet_result='ee'*32)
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH_CHANGE'):catalog.authorize(e.w3,other)


def test_canonical_comparison_has_no_zero_balance_or_new_key_migration_exception(continuity,monkeypatch):
    e=continuity;assert compare(e.source,e.source)['status']=='SUPPORTED_ORIGINAL_GRAPH'
    original=m.sample;other=deepcopy(e.sample());other['identity']['registry']=address(97)
    candidate=SimpleNamespace(c=e.source.c,w3=e.w3,context=lambda:(e.block(20),None,dict(e.ctx,candidate=True),None,None))
    monkeypatch.setattr('signer_agent.continuity_cli.sample',lambda w3,ctx,n:other if ctx.get('candidate') else original(w3,ctx,n))
    assert compare(e.source,candidate)['status']=='BLOCKED'
    key=bytes.fromhex(crypto.call('public_share',share=(15).to_bytes(32,'big').hex())['public'])[1:]
    other['identity']['base_x']=key.hex();other['family']=m.family(bytes.fromhex(other['identity']['bitcoin_genesis']),key)
    assert compare(e.source,candidate)['reason']=='CONTINUITY_CUSTODY_MIGRATION_UNSUPPORTED'


def test_pinned_native_signer_checks_continuity_before_revealing_nonce(signing,monkeypatch):
    e=signing;state=e.states[0]
    state.put('btc-registry:'+e.manifest.context.hex(),dict(graph=e.graph))
    calls=[]
    def block(*args,**kw):calls.append(kw['mode']);raise ValueError('CONTINUITY_GRAPH_CHANGE')
    monkeypatch.setattr(m,'trust_guard',block)
    from signer_agent.bitcoin_worker import sign_step
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH_CHANGE'):
        sign_step('btc-commit',e.manifest,e.key,e.plan,e.registry,state,e.bitcoin)
    assert calls==['DERIVED_SIGN']
    assert state.get('btc:'+e.manifest.context.hex()+':'+e.plan['batch_id']+':0') is None


def test_evm_private_work_uses_existing_signer_journal_and_exact_session(continuity):
    e=continuity;state=SecureState(str(e.tmp_path/'evm-signer'/'state'),'test password')
    client=SimpleNamespace(w3=e.w3,_read_snapshot=e.block(20),continuity_context=(e.ctx,'ISSUE',9),
        continuity_identity_file=e.source.c['identity_file'])
    assert m.evm_guard(client,state,SimpleNamespace(session_id=9,origin_contract=address(0)))['status']=='ORIGINAL_GRAPH_RETAINED'
    with pytest.raises(ValueError,match='CONTINUITY_SESSION'):m.evm_guard(client,state,SimpleNamespace(session_id=10,origin_contract=address(0)))
    e.view.custody=3;e.cg+=1;e.cseq+=1;e.closed=True
    with pytest.raises(ValueError,match='CONTINUITY_CUSTODY_RESTRICTED'):m.evm_guard(client,state,SimpleNamespace(session_id=9,origin_contract=address(0)))


def test_one_time_bootstrap_binding_checks_original_family_even_with_empty_new_ledger(continuity,monkeypatch):
    e=continuity;operation=b'o'*32;session=SimpleNamespace(session_id=7,message_hash=operation,origin_contract=e.common.address)
    intent=dict(artifact='BitcoinDepositRegistry',hash_method='getSetAddressRegistryOperationHash',hash_args=[e.reg.address],
        target=e.common.address,session='7',operation_hash='0x'+operation.hex())
    e.catalog.authorize(e.w3,e.sample())
    e.common.functions.values.update(addressRegistry=address(0),nonce=0,getSetAddressRegistryOperationHash=lambda _:operation)
    e.batch.functions.values['dkg']=e.reg_values['dkg']
    escrow=e.w3.eth.contract(address=address(8));escrow.functions.values['batches']=e.batch.address
    nodes={n.address:n for n in (e.common,e.reg,e.batch,escrow)}
    for name in ('authorization','dkg'):
        where=e.reg_values[name];nodes[where]=SimpleNamespace(address=where,functions=Functions({}))
    e.w3.eth.contract=lambda address,**kw:nodes[address]
    def fixture_runtime(w3,where,kind,at):
        if kind=='BitcoinKeyFamilyRegistry':raise ValueError('BTC_CONTRACT_BUILD_MISMATCH')
    monkeypatch.setattr('signer_agent.bitcoin_trust.checked_runtime',fixture_runtime)
    monkeypatch.setattr('signer_agent.contract_provenance.prove_creation',lambda *a,**kw:None)
    monkeypatch.setattr(address_mint,'registry_identity',lambda *a,**kw:(e.reg,{},[],{}))
    client=SimpleNamespace(w3=e.w3,_read_snapshot=e.block(20),continuity_binding=intent,continuity_identity_file=e.source.c['identity_file'])
    state=SecureState(str(e.tmp_path/'bootstrap-signer'/'state'),'test password')
    assert m.evm_guard(client,state,session)['status']=='ORIGINAL_GRAPH_RETAINED'
    e.reg_values['verifier']=address(96)
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH_CHANGE'):m.evm_guard(client,state,session)
    assert not any(v.get('status')=='nonce-created' for v in state._load()['sessions'].values())
    e.common.functions.values['nonce']=1
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH'):m.sample(e.w3,e.ctx,20,allow_unbound=True)


def test_mint_policy_is_reconstructed_at_each_private_guard_not_reused_from_another_session(continuity):
    e=continuity;state=SecureState(str(e.tmp_path/'service-signer'/'state'),'test password')
    from signer_agent.service import SigningService
    service=SigningService.__new__(SigningService);service.state=state
    client=SimpleNamespace(w3=e.w3,_read_snapshot=e.block(20),continuity_identity_file=e.source.c['identity_file'],
        continuity_context=(e.ctx,'ISSUE',999))
    seen=[]
    def refresh(session):
        seen.append(session.session_id);client.continuity_context=(e.ctx,'ADDRESS_MINT',session.session_id)
    client.guard_mint_session=refresh;service.client=client
    service._guard_bitcoin(SimpleNamespace(session_id=9,origin_contract=address(0)))
    assert seen==[9]
    e.cg+=1;e.cseq+=1;e.view.custody=3;e.closed=True
    with pytest.raises(ValueError,match='CONTINUITY_CUSTODY_RESTRICTED'):service._guard_bitcoin(SimpleNamespace(session_id=9,origin_contract=address(0)))
    assert seen==[9,9]
