"""Reject treasury/verifier substitutions before nonce generation and share export."""
from types import SimpleNamespace as NS

import pytest
import rlp
from web3 import Web3

from signer_agent import contract_provenance, ulr_treasury
from signer_agent.bootstrap import artifact
from signer_agent.coordinator import CoordinatorClient
from signer_agent.service import SigningService
from signer_agent import service as signing
from signer_agent.frost_math import NoncePair, scalar_base_mult, build_transcript
from test_ulr_treasury import env as ulr_env

REAL_TREASURY_CONTEXT=ulr_treasury.treasury_context

ZERO='0x'+'00'*20

def addr(n):return Web3.to_checksum_address(f'0x{n:040x}')

def runtime(kind):return bytes.fromhex(artifact(kind)['deployedBytecode']['object'].removeprefix('0x'))


@pytest.fixture
def verifier_env(monkeypatch):
    coordinator,verifier,clone=addr(1),addr(2),addr(3)
    blocks={7:NS(number=7,hash=b'a'*32),8:NS(number=8,hash=b'b'*32)}
    data=NS(latest=verifier,proofs=[],codes={coordinator:runtime('FROSTCoordinator'),verifier:runtime('FROSTVerifier'),clone:runtime('FROSTVerifier')},strict_constructor=True)
    w3=NS(eth=NS(chain_id=31337,get_block=lambda n:blocks[8 if n=='latest' else n],get_code=lambda a,*args,**kw:data.codes[a]))
    fn=NS(defaultVerifier=lambda:NS(call=lambda **kw:verifier if kw['block_identifier']==7 else data.latest))
    w3.eth.contract=lambda **kw:NS(address=kw['address'],functions=fn)
    def prove(w,a,kind,args,number):
        data.proofs.append((a,kind,args,number))
        if kind=='FROSTCoordinator' and data.strict_constructor and args!=[verifier]:raise ValueError('CONTRACT_CREATION_ARGUMENTS_MISMATCH')
        return dict(method='direct')
    monkeypatch.setattr(contract_provenance,'prove_creation',prove)
    client=NS(w3=w3,chain_id=w3.eth.chain_id,address=coordinator,_read_snapshot=blocks[7],check_read_snapshot=lambda:None)
    client.guard_verifier=lambda session:CoordinatorClient.guard_verifier(client,session)
    session=NS(state=1,purpose=1,verifier_override=ZERO)
    return NS(client=client,session=session,data=data,coordinator=coordinator,verifier=verifier,clone=clone)


def test_canonical_verifier_constructor_and_both_snapshots_are_authenticated(verifier_env):
    e=verifier_env;e.client.guard_verifier(e.session)
    assert [(kind,args,n) for _,kind,args,n in e.data.proofs]==[
        ('FROSTVerifier',[],7),('FROSTCoordinator',[e.verifier],7),
        ('FROSTVerifier',[],8),('FROSTCoordinator',[e.verifier],8)]


@pytest.mark.parametrize('attack,error',[
    ('coordinator_code','BTC_CONTRACT_BUILD_MISMATCH'),('verifier_code','BTC_CONTRACT_BUILD_MISMATCH'),
    ('clone_constructor','CONTRACT_CREATION_ARGUMENTS_MISMATCH'),('clone_latest','FROST_CANONICAL_VERIFIER_REQUIRED'),
    ('session_override','FROST_CANONICAL_VERIFIER_REQUIRED')])
def test_verifier_substitution_is_rejected_before_any_nonce_or_private_math(verifier_env,monkeypatch,attack,error):
    e=verifier_env
    if attack=='coordinator_code':e.data.codes[e.coordinator]=b'not the coordinator'
    if attack=='verifier_code':e.data.codes[e.verifier]=bytes.fromhex('600160005260206000f3') # always-true fixture
    if attack.startswith('clone_'):e.data.latest=e.clone
    if attack=='clone_latest':e.data.strict_constructor=False # even a self-reported valid clone may not change identity
    if attack=='session_override':e.session.verifier_override=e.clone
    service=object.__new__(SigningService);service.client=e.client
    monkeypatch.setattr(signing,'random_nonce_pair',lambda:pytest.fail('nonce created before rejection'))
    monkeypatch.setattr(signing.crypto,'call',lambda *a,**kw:pytest.fail('private scalar arithmetic before rejection'))
    with pytest.raises(ValueError,match=error):service._approve_locked(e.session)


@pytest.mark.parametrize('guard', ['guard_verifier', 'guard_gas_session', 'guard_bitcoin_session',
    'guard_ulr_treasury_session', 'guard_token_metadata_session', 'guard_custody_session', 'guard_nft_session'])
@pytest.mark.parametrize('phase',['initial','new_nonce','recovered_nonce','new_share','cached_share'])
def test_every_private_boundary_rechecks_treasury_and_verifier(monkeypatch,guard,phase):
    sender=addr(10);participants=[sender];nonces=NoncePair(7,11)
    session=NS(session_id=19,state=1,purpose=1,threshold=1,group_pubkey=scalar_base_mult(23).x.to_bytes(32,'big'),
               message_hash=b'm'*32,origin_contract=addr(90))
    stored=dict(status='nonce-created',message_hash=session.message_hash.hex(),hiding=hex(7),binding=hex(11))
    commitments={sender:(nonces.hiding_point,nonces.binding_point)} if phase in ('new_share','cached_share') else {}
    calls=[]
    def reject(_):calls.append(guard);raise ValueError('SUBSTITUTED_SIGNING_TARGET')
    def forbidden(*a,**kw):pytest.fail('private write, generation or export happened before substitution rejection')
    client=NS(sender=sender,address=addr(80),chain_id=31337,has_refused=lambda *a:False,
        nonce_set_status=lambda *a:(False,b'',None,0),submit_nonce_commitments=forbidden,submit_signature_share=forbidden)
    setattr(client,guard,reject)
    if phase=='cached_share':
        stored.update(status='share-prepared',share='01',transcript=build_transcript(31337,client.address,19,
            session.group_pubkey,session.message_hash,participants,commitments).hex())
    state=NS(get=lambda _:None if phase=='new_nonce' else stored,assert_unspent=lambda _:None,assert_spent=lambda *a:None,
        put=forbidden,prepare_share=forbidden,mark_consumed=forbidden)
    service=object.__new__(SigningService);service.client=client;service.state=state;service.signing_share=23
    monkeypatch.setattr(signing,'random_nonce_pair',forbidden);monkeypatch.setattr(signing.crypto,'call',forbidden)
    with pytest.raises(ValueError,match='SUBSTITUTED_SIGNING_TARGET'):
        if phase=='initial':service._approve_locked(session)
        elif phase in ('new_nonce','recovered_nonce'):service._commit(session,participants,commitments)
        else:service._submit_share(session,participants,commitments)
    assert calls==[guard]


@pytest.mark.parametrize('known_kind',['ULRConfederationTreasury','ULRCustodianSnapshots'])
def test_reviewed_ulr_target_cannot_be_skipped_as_unknown_runtime(monkeypatch,known_kind):
    from signer_agent import ulr_treasury as policy,onchain_transport
    target=addr(3);digest=b'h'*32
    intent=dict(artifact=known_kind,target=target,coordinator=addr(80),operation_hash='0x'+digest.hex())
    session=NS(origin_contract=target,message_hash=digest,session_id=19)
    def mismatch(*a,**kw):raise ValueError('BTC_CONTRACT_BUILD_MISMATCH')
    # A known intent must always reach its full review, even if classification
    # would fail. Before the fix, that classification returned without a guard.
    monkeypatch.setattr(policy,'checked_runtime',mismatch);monkeypatch.setattr(policy,'review',mismatch)
    monkeypatch.setattr(policy,'read',lambda *a,**kw:__import__('json').dumps(intent).encode())
    monkeypatch.setattr(onchain_transport,'board_at',lambda *a:NS())
    block=NS(number=7,hash=b'a'*32)
    client=NS(w3=NS(eth=NS(get_block=lambda _:block)),address=addr(80),_read_snapshot=block,
        ulr_intent_context=(intent,{'onchain_board':addr(9)},{}))
    with pytest.raises(ValueError,match='BTC_CONTRACT_BUILD_MISMATCH'):policy.guard_signing_session(client,session)


def test_reviewed_gas_target_cannot_be_skipped_as_unknown_runtime(monkeypatch):
    from signer_agent import gas_governance as gov
    target=addr(3);digest=b'h'*32
    intent=dict(target=target,coordinator=addr(80),operation_hash='0x'+digest.hex())
    block=NS(number=7,hash=b'a'*32)
    client=NS(w3=NS(eth=NS(get_block=lambda _:block)),address=addr(80),_read_snapshot=block,gas_intent_context=(intent,{}))
    monkeypatch.setattr(gov,'checked_runtime',lambda *a:(_ for _ in ()).throw(ValueError('BTC_CONTRACT_BUILD_MISMATCH')))
    with pytest.raises(ValueError,match='BTC_CONTRACT_BUILD_MISMATCH'):
        gov.guard_signing_session(client,NS(origin_contract=target,message_hash=digest))


@pytest.mark.parametrize('attack,error',[
    (None,None),('always_true_code','BTC_CONTRACT_BUILD_MISMATCH'),
    ('canonical_clone','ZK_CHILD_ORIGIN_MISMATCH'),('verifier_hash','ZK_ADAPTER_BINDING_MISMATCH'),
    ('legacy_selector','ZK_VERIFIER_3_1_REQUIRED')])
def test_zk_verifier_must_be_the_pinned_constructor_child(monkeypatch,attack,error):
    from signer_agent.zk_spv import inspect_binding,constructor_args,checkpoint_descriptor,PROFILE,VERIFIER_SELECTOR
    from test_zk_bootstrap import zk_invite
    p=zk_invite()['bitcoin_policy'];state=addr(101)
    child=lambda n:Web3.to_checksum_address(Web3.keccak(rlp.encode([bytes.fromhex(state[2:]),n]))[-20:])
    verifier,adapter,clone=child(1),child(2),addr(102)
    args=constructor_args(p)
    values={state:dict(adapter=adapter,verifier=verifier,confederationId=args[0][0],programImageId=args[0][1],
        RELAY_PROFILE=PROFILE,maxStateAge=args[0][3],maxTipLag=args[0][4],baselineMaxTipAge=args[0][5],checkpointDescriptor=checkpoint_descriptor(p)),
        adapter:dict(state=state,testHelpersEnabled=False),
        verifier:dict(VERSION='3.1.0',SELECTOR=VERIFIER_SELECTOR),
        clone:dict(VERSION='3.1.0',SELECTOR=VERIFIER_SELECTOR)}
    codes={state:runtime('ZkBitcoinStateV2'),adapter:runtime('ZkSpvAdapter'),verifier:runtime('RiscZeroGroth16Verifier'),clone:runtime('RiscZeroGroth16Verifier')}
    values[state]['verifierCodeHash']=Web3.keccak(codes[verifier])
    if attack=='always_true_code':codes[verifier]=bytes.fromhex('600160005260206000f3')
    if attack=='canonical_clone':values[state]['verifier']=clone
    if attack=='verifier_hash':values[state]['verifierCodeHash']=b'x'*32
    if attack=='legacy_selector':values[verifier]['SELECTOR']=bytes.fromhex('73c457ba')
    class Functions:
        def __init__(self,a):self.a=a
        def __getattr__(self,name):return lambda:NS(call=lambda **kw:values[self.a][name])
    w3=NS(eth=NS(contract=lambda **kw:NS(address=kw['address'],functions=Functions(kw['address'])),get_code=lambda a,*a_,**kw:codes[a]))
    monkeypatch.setattr(contract_provenance,'prove_creation',lambda *a,**kw:dict(method='direct'))
    if error:
        with pytest.raises(ValueError,match=error):inspect_binding(w3,state,p,7)
    else:assert inspect_binding(w3,state,p,7)[2]['zk_verifier']['address']==verifier


@pytest.mark.parametrize('replacement',['unpinned_target','redirected_recipient'])
def test_treasury_substitution_with_self_consistent_hash_stops_before_signing(ulr_env,monkeypatch,replacement):
    from signer_agent import ulr_treasury as policy
    e=ulr_env
    if replacement=='unpinned_target':
        target=NS(address=addr(777),abi=artifact('ULRConfederationTreasury')['abi']);e.objects[target.address]=target
        # The real treasury membership check must run, not the graph fixture.
        monkeypatch.setattr(policy,'treasury_context',REAL_TREASURY_CONTEXT)
        helper='getSpendOperationHash';args=[addr(778),1,999999999];kind='ULRConfederationTreasury'
        error='ULR_TREASURY_NOT_PINNED'
    else:
        target=NS(address=addr(70),abi=artifact('CustodianDAOFeeRouter')['abi']);e.objects[target.address]=target
        monkeypatch.setattr(policy,'proven',lambda *a,**kw:target)
        helper='getSetTreasuryOperationHash';args=[addr(777)];kind='CustodianDAOFeeRouter';error='ULR_ZERO_OLD_SPLIT_REQUIRED'
    original_read=policy.read
    def read(target_,at,method,*values):
        if method==helper:
            from eth_utils.abi import collapse_if_tuple
            method_abi=next(f for f in target.abi if f.get('name')==method)
            return Web3.keccak(e.w3.codec.encode([collapse_if_tuple(x) for x in method_abi['inputs']],values))
        if method=='incomeVault':return addr(71)
        if method=='getCustodianDAO':return [addr(1)]*4+[addr(71),addr(1),addr(1)]
        if method=='rewardToken':return addr(72)
        if method in ('wrapStakerBps','deployStakerBps'):return 0
        return original_read(target_,at,method,*values)
    monkeypatch.setattr(policy,'read',read)
    intent=policy.intent_for(e.w3,target,kind,helper,args,e.auth.address,'conf_ceremony',5,ulr_treasury=e.treasury.address)
    session=NS(state=1,purpose=1,origin_contract=target.address,message_hash=policy.digest(intent['operation_hash']))
    client=NS(chain_id=e.w3.eth.chain_id,guard_ulr_treasury_session=lambda _:policy.review(e.w3,intent,e.bundle,5,e.pin))
    service=object.__new__(SigningService);service.client=client
    monkeypatch.setattr(signing,'random_nonce_pair',lambda:pytest.fail('substituted treasury reached nonce generation'))
    monkeypatch.setattr(signing.crypto,'call',lambda *a,**kw:pytest.fail('substituted treasury reached private signing'))
    with pytest.raises(ValueError,match=error):service._approve_locked(session)


@pytest.mark.parametrize('kind',['CustodianDAOFeeRouter','CustodianDAODecisionBoard'])
@pytest.mark.parametrize('label',[False,True])
def test_signed_ulr_enrollment_cannot_be_downgraded_by_stripping_intent_label(kind,label):
    intent=dict(artifact=kind)
    bundle=dict(role='conf_ceremony',onboarding=dict(invitation=dict(ulr={'version':'1','mode':'create'})))
    if label:
        intent['ulr_treasury']=addr(5)
        assert ulr_treasury.requires_review(intent,bundle)
    else:
        with pytest.raises(ValueError,match='ULR_PUBLIC_INTENT_AND_PIN_REQUIRED'):
            ulr_treasury.requires_review(intent,bundle)


def test_project_router_and_legacy_enrollment_are_not_confederation_ulr_scopes():
    intent=dict(artifact='CustodianDAOFeeRouter')
    assert not ulr_treasury.requires_review(intent,dict(role='evm_ceremony',onboarding=dict(invitation=dict(ulr={'version':'1'}))))
    assert not ulr_treasury.requires_review(intent,dict(role='conf_ceremony',onboarding=dict(invitation={})))
    assert ulr_treasury.requires_review(dict(artifact='CustodianValidator'),{})


@pytest.fixture
def derivation_binding_env(monkeypatch):
    """Real runtime policy and registry_identity; only historical RPC is modeled."""
    from signer_agent import address_mint
    from signer_agent.deposit_intent import PROFILE
    registry,common,auth,escrow,batch,dkg,verifier=[addr(i) for i in range(201,208)]
    operation=b'o'*32
    values={registry:dict(deposits=common,authorization=auth,escrow=escrow,batches=batch,dkg=dkg,chainId=31337,
        PROFILE=Web3.keccak(text=PROFILE),verifier=verifier,bitcoinGenesis=b'g'*32,
        verifierRuntimeHash=Web3.keccak(runtime('BitcoinDepositDerivation')),verifierSourceHash=b's'*32,
        qualificationCommitment=b'q'*32,servicePolicyHash=b'p'*32,graphId=b'i'*32,custodyScope=b'c'*32,derivationScope=b'd'*32),
        common:dict(authorization=auth,spendRecorder=escrow,getSetAddressRegistryOperationHash=lambda _:operation),
        auth:{},escrow:dict(batches=batch),batch:dict(dkg=dkg),dkg:{},verifier:{}}
    kinds={registry:'BitcoinAddressRegistry',common:'BitcoinDepositRegistry',auth:'DKGAuthorization',
        escrow:'BitcoinWithdrawalEscrow',batch:'BitcoinBatchRegistry',dkg:'DKGCoordinator',verifier:'BitcoinDepositDerivation'}
    codes={a:runtime(kind) for a,kind in kinds.items()}
    view=NS(attack=None,proofs=[])
    blocks={7:NS(number=7,hash=b'a'*32),8:NS(number=8,hash=b'b'*32)}
    class Functions:
        def __init__(self,a):self.a=a
        def __getattr__(self,name):
            def invoke(*args):
                def call(**kw):
                    value=values[self.a][name]
                    return value(*args) if callable(value) else value
                return NS(call=call)
            return invoke
    def code(a,block_identifier):
        if a==verifier and (view.attack=='always_true' or view.attack=='latest_code' and block_identifier==8):
            return bytes.fromhex('600160005260206000f3')
        return codes[a]
    w3=NS(eth=NS(chain_id=31337,get_code=code,get_block=lambda n:blocks[8 if n=='latest' else n],
        contract=lambda **kw:NS(address=kw['address'],functions=Functions(kw['address']))))
    def prove(w,a,kind,args,at):
        view.proofs.append((a,kind,args,at))
        if kind=='BitcoinDepositDerivation':
            assert args==[]
            if view.attack=='forged_init':raise ValueError('BTC_CONSTRUCTOR_INPUT_MISMATCH')
        return dict(method='direct')
    monkeypatch.setattr(contract_provenance,'prove_creation',prove)
    monkeypatch.setattr(address_mint,'prove_creation',prove)
    intent=dict(artifact='BitcoinDepositRegistry',hash_method='getSetAddressRegistryOperationHash',hash_args=[registry],
        target=common,session='19',operation_hash='0x'+operation.hex())
    return NS(w3=w3,view=view,values=values,registry=registry,verifier=verifier,common=common,intent=intent,blocks=blocks)


def test_initial_bitcoin_binding_authenticates_canonical_derivation_verifier(derivation_binding_env):
    from signer_agent.bitcoin_continuity import binding_context
    e=derivation_binding_env;session=NS(session_id=19,message_hash=b'o'*32,origin_contract=e.common)
    for at in (7,8):assert binding_context(e.w3,e.intent,session,at)['registry'].address==e.registry
    assert [p[3] for p in e.view.proofs if p[1]=='BitcoinDepositDerivation']==[7,8]


@pytest.mark.parametrize('attack,error',[
    ('always_true','BTC_CONTRACT_BUILD_MISMATCH'),('forged_init','BTC_CONSTRUCTOR_INPUT_MISMATCH'),
    ('declared_hash','BTC_ADDRESS_VERIFIER_MISMATCH'),('latest_code','BTC_CONTRACT_BUILD_MISMATCH')])
@pytest.mark.parametrize('phase',['new_nonce','new_share','cached_share'])
def test_initial_bitcoin_derivation_verifier_substitution_blocks_private_boundaries(derivation_binding_env,monkeypatch,attack,error,phase):
    e=derivation_binding_env;e.view.attack=attack
    if attack=='declared_hash':e.values[e.registry]['verifierRuntimeHash']=b'x'*32
    sender=addr(10);participants=[sender];nonces=NoncePair(7,11)
    session=NS(session_id=19,threshold=1,group_pubkey=scalar_base_mult(23).x.to_bytes(32,'big'),
        message_hash=b'o'*32,origin_contract=e.common)
    stored=dict(status='nonce-created',message_hash=session.message_hash.hex(),hiding=hex(7),binding=hex(11))
    commitments={} if phase=='new_nonce' else {sender:(nonces.hiding_point,nonces.binding_point)}
    def forbidden(*a,**kw):pytest.fail('substituted derivation verifier reached private work, state write or export')
    client=NS(w3=e.w3,_read_snapshot=e.blocks[7],continuity_binding=e.intent,sender=sender,address=addr(80),
        chain_id=31337,has_refused=lambda *a:False,nonce_set_status=lambda *a:(False,b'',None,0),
        submit_nonce_commitments=forbidden,submit_signature_share=forbidden)
    if phase=='cached_share':
        stored.update(status='share-prepared',share='01',transcript=build_transcript(31337,client.address,19,
            session.group_pubkey,session.message_hash,participants,commitments).hex())
    state=NS(get=lambda _:None if phase=='new_nonce' else stored,assert_unspent=lambda _:None,assert_spent=lambda *a:None,
        put=forbidden,prepare_share=forbidden,mark_consumed=forbidden)
    service=object.__new__(SigningService);service.client=client;service.state=state;service.signing_share=23
    monkeypatch.setattr(signing,'random_nonce_pair',forbidden);monkeypatch.setattr(signing.crypto,'call',forbidden)
    with pytest.raises(ValueError,match=error):
        if phase=='new_nonce':service._commit(session,participants,commitments)
        else:service._submit_share(session,participants,commitments)
