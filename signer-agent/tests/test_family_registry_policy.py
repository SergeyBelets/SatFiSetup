"""Typed SDK reviews; constructor/DKG/RPC provenance are explicit unit fixtures."""
from copy import deepcopy
from types import SimpleNamespace as NS
import pytest
from signer_agent import family_registry as m
from signer_agent.bootstrap import artifact,encode_args
from signer_agent.bootstrap_withdrawal_fee import genesis
from test_address_mint import Functions,address
from test_address_transport import delivery
from test_address_registration import registered_env
ORIGINAL_INSPECT=m.inspect


@pytest.fixture
def family_policy(registered_env,monkeypatch):
    e=registered_env;e.reg_values['bitcoinGenesis']=bytes.fromhex(genesis('regtest')[2:])
    p=dict(version='1',chain_id='31337',address=address(99),initial_authority=e.auth.address,dkg=e.dkg.address,
        coordinator=e.coordinator.address,runtime_hash='0x'+'11'*32)
    e.invite.update(operator=e.accounts[1].address,withdrawal_fee=dict(version='2',family_root=p));e.bundle['onboarding']['invitation']=e.invite
    values=dict(governance=e.auth.address,registryFamily=lambda _:bytes(32),getEnrollOperationHash=lambda _:b'o'*32,
        receipts=lambda _:False,receiptTarget=lambda _:e.reg.address,receiptKind=lambda _:1)
    root=NS(address=p['address'],functions=Functions(values));old=e.w3.eth.contract
    e.w3.eth.contract=lambda address,**kw:root if address==root.address else old(address=address,**kw)
    monkeypatch.setattr(m,'inspect',lambda *_:root)
    monkeypatch.setattr('signer_agent.contract_provenance.prove_creation',lambda *_args,**_kwargs:dict(method='EXPLICIT_UNIT_PROVENANCE_FIXTURE'))
    intent=dict(chain_id='31337',coordinator=e.coordinator.address,role='conf_ceremony',target=root.address,
        artifact='BitcoinKeyFamilyRegistry',hash_method='getEnrollOperationHash',hash_args=[e.reg.address],operation_hash=(b'o'*32).hex(),session='1')
    return NS(e=e,p=p,root=root,values=values,intent=intent,bundle=e.bundle)


def test_original_root_authority_is_checked_before_private_signing(family_policy):
    f=family_policy
    assert m.review(f.e.w3,f.intent,f.bundle,20,f.p)==dict(complete=False,registry=f.e.reg.address)


def test_create_policy_enrolls_a_new_graph_under_original_root_authority(family_policy):
    f=family_policy
    f.e.invite['withdrawal_fee']['family_root']=dict(version='create-1',creation_code_hash=
        '0x'+m.Web3.keccak(hexstr=artifact('BitcoinKeyFamilyRegistry')['bytecode']['object']).hex().removeprefix('0x'))
    # A second graph has its own authorization; it cannot replace root governance.
    f.e.reg_values['authorization']=address(98)
    assert m.review(f.e.w3,f.intent,f.bundle,20,f.p)['registry']==f.e.reg.address
    resolved=m.policy_for_intent(f.e.w3,f.intent,f.e.invite['withdrawal_fee']['family_root'],20)
    assert resolved['initial_authority']==f.e.auth.address
    assert resolved['dkg']==f.e.dkg.address


def test_install_exact_root_pin_is_idempotent_but_never_replaces_it(family_policy, tmp_path, monkeypatch):
    f=family_policy
    catalog=NS(identity_file=tmp_path/'identity.json')
    retained=[]
    monkeypatch.setattr(m,'retain',lambda catalog,p:retained.append(deepcopy(p)))
    assert m.install(f.e.w3,catalog,f.p,20)['status']=='FAMILY_ROOT_PINNED'
    path=m.pin_path(catalog.identity_file);before=path.read_bytes()
    assert m.install(f.e.w3,catalog,f.p,20)['status']=='FAMILY_ROOT_PINNED'
    assert path.read_bytes()==before and retained==[f.p,f.p]
    with pytest.raises(ValueError,match='FAMILY_ROOT_PIN_CHANGED'):
        m.install(f.e.w3,catalog,dict(f.p,address=address(98)),20)
    assert path.read_bytes()==before


def test_generic_signer_cannot_sign_root_without_pinned_typed_intent(family_policy,monkeypatch):
    f=family_policy
    monkeypatch.setattr('signer_agent.bitcoin_trust.checked_runtime',lambda *_:None)
    client=NS(w3=f.e.w3)
    with pytest.raises(ValueError,match='FAMILY_ROOT_INTENT'):
        m.signing_guard(client,None,NS(origin_contract=f.root.address))


def test_reuse_inspection_resolves_latest_before_constructor_search(family_policy,monkeypatch):
    f=family_policy;runtime=b'explicit unit runtime';seen=[]
    monkeypatch.setattr(f.e.w3.eth,'get_code',lambda *args,**kwargs:runtime)
    f.values.update(chainId=31337,dkg=f.p['dkg'],coordinator=f.p['coordinator'])
    p=dict(f.p,runtime_hash=m.Web3.to_hex(m.Web3.keccak(runtime)))
    monkeypatch.setattr('signer_agent.contract_provenance.prove_creation',lambda w3,address,kind,args,n:seen.append(n))
    assert ORIGINAL_INSPECT(f.e.w3,p,'latest') is f.root
    assert seen==[f.e.w3.eth.get_block('latest').number]


@pytest.mark.parametrize('mutation',['hash','target','coordinator','role','dkg','purpose','roster','root'])
def test_family_review_rejects_cross_scope_or_wrong_governance_before_nonce(family_policy,mutation):
    f=family_policy;intent=deepcopy(f.intent);bundle=deepcopy(f.bundle)
    if mutation=='hash':intent['operation_hash']='ff'*32
    if mutation=='target':intent['target']=address(98)
    if mutation=='coordinator':intent['coordinator']=address(98)
    if mutation=='role':intent['role']='evm_ceremony'
    if mutation=='dkg':bundle['registry']=address(98)
    if mutation=='purpose':bundle['purpose']='BTC_SPEND'
    if mutation=='roster':bundle['participants']=list(reversed(bundle['participants']))
    if mutation=='root':bundle['onboarding']['invitation']['withdrawal_fee']['family_root']=dict(f.p,address=address(98))
    with pytest.raises(ValueError):m.review(f.e.w3,intent,bundle,20,f.p)


def test_exact_historical_receipt_precedes_current_governance_and_nonce(family_policy):
    f=family_policy;f.values['receipts']=lambda _:True
    f.values['governance']=lambda:(_ for _ in ()).throw(AssertionError('historical receipt must not require current governance'))
    f.values['getEnrollOperationHash']=lambda _:(_ for _ in ()).throw(AssertionError('never recompute historical hash with current nonce'))
    assert m.review(f.e.w3,f.intent,f.bundle,20,f.p)['complete']
    f.values['receiptTarget']=lambda _:address(98)
    with pytest.raises(ValueError,match='HASH'):m.review(f.e.w3,f.intent,f.bundle,20,f.p)


def test_standalone_root_enrollment_recipe_preserves_original_governance_and_typed_sdk(family_policy,monkeypatch):
    f=family_policy
    monkeypatch.setattr('signer_agent.onboarding.validate_invite',lambda p:p)
    profile=m.enrollment_plan(f.e.w3,f.p,f.bundle,f.e.reg.address,'2000','http://rpc.invalid','1')
    assert profile['contracts']['authorization']['address']==f.e.auth.address
    assert [s['method'] for s in profile['steps']]==['createSessionFromDKG','publishIntent','getAuthorizationResult','authorizeOperation','enroll']
    public=profile['steps'][1]['args'][1]['$json_hex']
    assert public['target']==f.p['address'] and public['hash_args']==[f.e.reg.address]
    for s in profile['steps']:
        kind=profile['contracts'][s['contract']]['artifact'];abi=next(x for x in artifact(kind)['abi'] if x.get('name')==s['method'])
        def resolve(v):
            if v=='$family_session':return '1'
            if isinstance(v,dict):return '0x00'
            return v
        encode_args(abi['inputs'],[resolve(v) for v in s['args']])
