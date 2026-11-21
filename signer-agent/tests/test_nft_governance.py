"""Independent NFT pins, typed hashes, completion receipts and private-boundary guards."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from web3 import Web3

from signer_agent import nft_governance as n


def addr(i):return Web3.to_checksum_address(f'0x{i:040x}')


def pin():return dict(version='1',chain_id='31337',confirmations='1',board=addr(1),
    role='conf_ceremony',factory=addr(2),fee_manager=addr(3))


@pytest.mark.parametrize('key,value',[('chain_id','0'),('confirmations','0'),('confirmations',1),
    ('role','btc_ceremony'),('fee_manager',n.ZERO),('factory','bad'),('version','2')])
def test_pin_rejects_ambiguous_or_unbound_values(key,value):
    p=pin();p[key]=value
    with pytest.raises(ValueError):n.validate_pin(p)


@pytest.fixture
def env(monkeypatch):
    p=pin();board,manager,factory,auth,nft=[NS(address=addr(i)) for i in (1,3,2,4,5)]
    block=NS(number=7,hash=b'a'*32,timestamp=1000)
    invite=dict(operator=addr(9),evm_confirmations='1',confederation_id='case-conf')
    bundle=dict(role='conf_ceremony',onchain_board=board.address,onboarding=dict(invitation=invite))
    manifest=NS(purpose='EVM_AUTH',chain_id=31337,registry=addr(6),session_id=b's'*32)
    state=NS(manager=manager.address,validator=n.ZERO,creator=invite['operator'],complete=False,
             collection=n.ZERO,managed=True,roles=True,receipt=bytes(32),public=None)
    w3=NS(eth=NS(chain_id=31337,get_block=lambda value:block))
    monkeypatch.setattr('signer_agent.evm_network.read_block',lambda *a:block)
    monkeypatch.setattr(n.Manifest,'from_dict',lambda b:manifest)
    monkeypatch.setattr('signer_agent.onchain_transport._board_and_invitation',lambda *a:(board,invite))
    monkeypatch.setattr('signer_agent.onchain_transport.board_at',lambda *a:board)
    monkeypatch.setattr('signer_agent.contract_provenance.prove_creation',lambda *a:dict(method='direct',sender=state.creator))
    monkeypatch.setattr(n,'proven',lambda w,k,a,at:{factory.address:factory,manager.address:manager}[a])
    monkeypatch.setattr(n,'authority_context',lambda *a:auth)
    monkeypatch.setattr(n,'contract',lambda w,k,a:nft if k=='FederationNFTModifier' else manager)
    monkeypatch.setattr('signer_agent.bitcoin_trust.checked_runtime',lambda *a:'matched')
    def read(target,at,method,*args):
        if method=='getConfederation':
            values=[None]*18;values[7]=auth.address;values[8]=state.validator;values[12]=state.manager;return values
        if method in n.METHODS:return Web3.keccak(text=method+repr(args))
        if method=='hasRole':return state.roles
        return dict(registry=manifest.registry,dkgCoordinator=manifest.registry,dkgId=manifest.session_id,
            frostMultisig=auth.address,frostCoordinator=addr(7),coordinator=addr(7),
            custodianValidator=state.validator,executedOperations=state.complete,nftModifier=state.collection,
            managedNFTCollections=state.managed,nftOperationReceipts=state.receipt,nftCreatedCollections=nft.address,
            DEFAULT_ADMIN_ROLE=b'0'*32,MINTER_ROLE=b'1'*32,USAGE_ROLE=b'2'*32,intents=state.public)[method]
    monkeypatch.setattr(n,'read',read)
    # Argument conversion is covered by actual ABI/CLI/EVM tests; these tests isolate policy decisions.
    monkeypatch.setattr(n,'arguments',lambda helper,args:list(args) if helper in n.METHODS else (_ for _ in ()).throw(ValueError('NFT_INTENT_METHOD')))
    def intent(helper,args):
        return dict(chain_id='31337',coordinator=addr(7),role='conf_ceremony',target=manager.address,
            artifact=n.KIND,hash_method=helper,hash_args=args,
            operation_hash=Web3.to_hex(read(manager,7,helper,*args)))
    return NS(pin=p,w3=w3,bundle=bundle,manager=manager,nft=nft,block=block,state=state,intent=intent)


def test_context_binds_enrolled_factory_and_manager(env):
    e=env;assert n.context(e.w3,e.bundle,e.pin,7)['manager']==e.manager
    e.state.manager=addr(88)
    with pytest.raises(ValueError,match='NFT_MANAGER_BINDING'):n.context(e.w3,e.bundle,e.pin,7)


def test_same_runtime_foreign_creator_is_rejected(env):
    e=env;e.state.creator=addr(88)
    with pytest.raises(ValueError,match='NFT_CREATION_ORIGIN'):n.context(e.w3,e.bundle,e.pin,7)


def test_removing_or_changing_role_and_board_cannot_replace_pin(env):
    e=env
    for change in ({'role':'evm_ceremony'},{'onchain_board':addr(88)}):
        with pytest.raises(ValueError,match='NFT_ENROLLMENT_REQUIRED'):n.context(e.w3,dict(e.bundle,**change),e.pin,7)


def test_create_review_requires_original_hash_and_empty_collection(env):
    e=env;i=e.intent(n.CREATE,['Discount','DISC']);assert not n.review(e.w3,i,e.bundle,7,e.pin)['complete']
    i['hash_args'][0]='Fake'
    with pytest.raises(ValueError,match='NFT_OPERATION_HASH'):n.review(e.w3,i,e.bundle,7,e.pin)
    e.state.collection=e.nft.address
    with pytest.raises(ValueError,match='NFT_COLLECTION_CONFIGURATION'):n.review(e.w3,e.intent(n.CREATE,['D','D']),e.bundle,7,e.pin)


@pytest.mark.parametrize('field,value',[(0,addr(44)),(1,n.ZERO),(2,1000),(3,0),(4,10001)])
def test_mint_rejects_collection_owner_expiry_quota_and_discount_substitution(env,field,value):
    e=env;e.state.collection=e.nft.address;args=[e.nft.address,addr(10),2000,100,2500]
    i=e.intent(n.MINT,args);i['hash_args'][field]=value
    with pytest.raises(ValueError,match='NFT_MINT_CONFIGURATION'):n.review(e.w3,i,e.bundle,7,e.pin)


@pytest.mark.parametrize('attack,error',[('unmanaged','NFT_MINT_CONFIGURATION'),('role','NFT_ISSUER_BINDING'),('hash','NFT_OPERATION_HASH')])
def test_mint_cannot_adopt_foreign_or_reconfigured_issuer(env,attack,error):
    e=env;e.state.collection=e.nft.address;i=e.intent(n.MINT,[e.nft.address,addr(10),2000,100,2500])
    if attack=='unmanaged':e.state.managed=False
    if attack=='role':e.state.roles=False
    if attack=='hash':i['operation_hash']='0x'+'f'*64
    with pytest.raises(ValueError,match=error):n.review(e.w3,i,e.bundle,7,e.pin)


def test_completed_mint_uses_exact_receipt_even_after_collection_change(env,monkeypatch):
    e=env;i=e.intent(n.MINT,[e.nft.address,addr(10),2000,100,2500]);e.state.complete=True;e.state.receipt=b'r'*32
    monkeypatch.setattr(n,'receipt_hash',lambda *a:b'r'*32)
    assert n.review(e.w3,i,e.bundle,7,e.pin)['complete']
    e.state.receipt=bytes(32)
    with pytest.raises(ValueError,match='NFT_OPERATION_RECEIPT'):n.review(e.w3,i,e.bundle,7,e.pin)


def test_raw_fee_manager_signing_cannot_skip_typed_review(env):
    e=env;client=NS(w3=e.w3,_read_snapshot=e.block)
    with pytest.raises(ValueError,match='NFT_TYPED_INTENT_REQUIRED'):
        n.guard_signing_session(client,NS(origin_contract=e.manager.address))


def test_guard_rechecks_public_intent_and_latest_before_private_work(env,monkeypatch):
    e=env;i=e.intent(n.CREATE,['Discount','DISC']);e.state.public=json_bytes(i)
    calls=[];client=NS(w3=e.w3,address=addr(7),_read_snapshot=e.block,nft_intent_context=(i,e.bundle,e.pin),
        check_read_snapshot=lambda:calls.append('anchor'))
    session=NS(origin_contract=e.manager.address,message_hash=bytes.fromhex(i['operation_hash'][2:]),session_id=1)
    n.guard_signing_session(client,session);assert calls==['anchor']
    e.state.public=json_bytes(dict(i,target=addr(88)))
    with pytest.raises(ValueError,match='NFT_PUBLIC_INTENT_REQUIRED'):n.guard_signing_session(client,session)


def json_bytes(value):
    import json
    return json.dumps(value).encode()
