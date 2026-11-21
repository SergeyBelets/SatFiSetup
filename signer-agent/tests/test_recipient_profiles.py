"""Recipient choice/hash/transport with real ABI, HPKE, native derivation, SQLite.

EVM/provenance boundary fixtures are explicit; production Solidity wallet and
registry/token execution is covered separately by RecipientMatrix.t.sol.
"""
from types import SimpleNamespace
from dataclasses import replace
from copy import deepcopy
import pytest
from eth_utils import keccak
from eth_abi import encode
from eth_account import Account

from signer_agent import recipient_profiles as p,address_transport as transport
from signer_agent.deposit_intent import DepositIntentV1
from signer_agent.btc_fee_reserve import digest
from test_address_mint import Functions,address
from test_address_transport import delivery
from test_smp1_deposit import client as funding_client
from test_address_registration import registered_env


@pytest.fixture
def matrix(delivery,monkeypatch):
    e=delivery
    e.codes={};e.choices={};e.owner=e.accounts[0].address;e.target=address(44);e.wallet=address(45);e.policy_address=address(46)
    e.policy=SimpleNamespace(address=e.policy_address,functions=Functions(dict(creator=e.reg.address,
        wallets=lambda who:e.owner if who==e.wallet else p.ZERO,
        walletHashes=lambda who:keccak(e.codes.get(who,b'')),
        selection=lambda nonce:e.choices.get(nonce,[bytes(32),[0,bytes(32),p.ZERO,bytes(32),bytes(32)]]))))
    e.codes[e.wallet]=b'canonical fixture wallet';e.codes[e.target]=b'unverified arbitrary contract'
    e.reg_values.update(recipientPolicy=e.policy_address,requireRecipientNotSink=lambda *args:None)
    original_contract=e.w3.eth.contract
    def contract(address,**kwargs):
        if address==e.wallet:return SimpleNamespace(address=address,functions=Functions(dict(owner=e.owner)))
        return original_contract(address=address,**kwargs)
    e.w3.eth.contract=contract;e.w3.eth.get_code=lambda who,**kw:e.codes.get(who,b'')
    monkeypatch.setattr(p,'policy_node',lambda w,a,n:e.policy if a==e.policy_address else (_ for _ in ()).throw(ValueError('RECIPIENT_POLICY_ORIGIN')))
    def checked(w3,who,kind,number):
        if kind=='RecipientWalletV1' and who!=e.wallet:raise ValueError('RECIPIENT_WALLET_PROFILE')
    monkeypatch.setattr(p,'checked_runtime',checked)
    def validate(pool,token,requester,recipient,nonce):
        row=e.choices.get(nonce)
        if row is None:
            if e.codes.get(recipient):raise ValueError('ADDRESS_RECIPIENT_PROFILE')
            return 0
        context=p.context_hash(31337,e.policy_address,e.reg.address,pool,token,requester,recipient)
        if row[0]!=context:raise ValueError('RECIPIENT_SELECTION_REQUIRED')
        p.review(e.w3,e.policy,recipient,row[1],context,20)
        return row[1][0]
    e.reg_values['validateRecipient']=validate
    def send(key,method,args,**kwargs):
        if method!='selectFor':raise AssertionError(method)
        q=next(q for q in e.previews if q['request_nonce'] in key)
        c=p.choice_args(q);nonce=bytes.fromhex(q['request_nonce'])
        context=p.context_hash(31337,e.policy_address,*args[:3],args[3],args[4])
        # args are namespace/pool/token/recipient/Choice. Match the full signed
        # context rather than accepting a service-supplied beneficiary label.
        e.choices[nonce]=[context,c]
        return {}
    e.previews=[];e.requester.send=send
    def preview(mode,recipient=None):
        result=p.address_preview(e.requester,recipient or (e.wallet if mode=='wallet-v1' else e.target),mode,seed='11'*32)
        e.previews.append(result['selection']);return result
    e.preview=preview
    return e


@pytest.mark.parametrize('mode',['wallet-v1','advanced-contract'])
def test_profile_choice_survives_unchanged_382_byte_intent_and_encrypted_durable_ack(matrix,mode):
    e=matrix;choice=e.preview(mode);q=choice['selection']
    intent=e.requester.make_intent(None,selection=q,accepted=choice['confirmation_hash'])
    assert len(intent.encode())==382 and intent.request_nonce.hex()==q['request_nonce']
    assert intent.recipient==bytes.fromhex(q['recipient'][2:])
    assert not e.choices  # No public selection transaction during private prepare.
    # Changes to the bound profile necessarily change the nonce and derived BTC address.
    other=deepcopy(q);other['choice'][3]='0x'+'22'*32
    changed=replace(intent,request_nonce=p.nonce_for(bytes.fromhex(q['context']),p.choice_args(other)))
    assert intent.derive(bytes.fromhex(e.requester.scope['base_group']))['script_pubkey']!=changed.derive(bytes.fromhex(e.requester.scope['base_group']))['script_pubkey']
    request=e.requester.request(intent.encode(),e.accounts[1].address)
    e.publish(e.requester);e.service.service_once()
    assert e.service.store.intents()[0][0]==intent.intent_id.hex()
    assert not e.choices  # Encrypted delivery and durable ACK keep the opening private.
    assert e.service.store.get('recipient-choice:'+intent.intent_id.hex())==q
    e.publish(e.service);e.requester.poll()
    assert e.requester.replies()['routes'][request['route']]['acknowledged']
    assert q['requester']!=e.owner or q['mode']=='wallet-v1' or q['recipient']==e.target


def test_advanced_contract_requires_exact_full_user_confirmation(matrix):
    e=matrix;q=e.preview('advanced-contract')['selection']
    with pytest.raises(ValueError,match='CONFIRMATION_REQUIRED'):e.requester.make_intent(None,selection=q,accepted='00'*32)
    for field,value in [('recipient',e.wallet),('token',address(99)),('namespace',address(98)),('risk','safe forever')]:
        bad=dict(q,**{field:value})
        with pytest.raises(ValueError):e.requester.make_intent(None,selection=bad,accepted=digest(bad))
    assert not e.choices


def test_onchain_sink_rejection_is_quarantined_without_blocking_the_service(matrix):
    from web3.exceptions import ContractLogicError
    e=matrix;c=e.preview('advanced-contract')
    intent=e.requester.make_intent(None,selection=c['selection'],accepted=c['confirmation_hash'])
    request=e.requester.request(intent.encode(),e.accounts[1].address);e.publish(e.requester)
    def deny(*args):raise ContractLogicError('execution reverted: recipient sink')
    e.reg_values['requireRecipientNotSink']=deny
    assert e.service.service_once()['status']=='ADDRESS_REQUESTS_SAVED'
    assert e.service.store.get('rejected:'+request['message_id'])['reason']=='RECIPIENT_SINK'
    assert not e.service.store.intents()


def test_contract_and_delegation_cannot_use_ordinary_or_supported_wallet_labels(matrix):
    e=matrix
    with pytest.raises(ValueError):e.requester.make_intent(e.target)
    with pytest.raises(ValueError):e.preview('wallet-v1',e.target)
    e.codes[e.target]=bytes.fromhex('ef0100')+bytes.fromhex(e.wallet[2:])
    with pytest.raises(ValueError,match='DELEGATED_UNSUPPORTED'):e.preview('advanced-contract')
    with pytest.raises(ValueError):e.preview('wallet-v1',address(47))


def test_choice_cannot_be_substituted_after_confirmation_or_registration(matrix):
    e=matrix;c=e.preview('advanced-contract');q=c['selection']
    e.codes[e.target]=b'changed code'
    with pytest.raises(ValueError,match='CODE_CHANGED'):e.requester.make_intent(None,selection=q,accepted=c['confirmation_hash'])
    e.codes[e.target]=b'unverified arbitrary contract'
    intent=e.requester.make_intent(None,selection=q,accepted=c['confirmation_hash'])
    swapped=replace(intent,recipient=bytes.fromhex(e.wallet[2:]))
    with pytest.raises(ValueError):e.requester.validate_intent(swapped,e.ctx,e.block(20),selection=q)


def test_public_registration_can_open_only_the_exact_authenticated_choice(matrix):
    e=matrix;c=e.preview('advanced-contract');q=c['selection']
    intent=e.requester.make_intent(None,selection=q,accepted=c['confirmation_hash'])
    p.open_registration_choices(e.requester,e.ctx,[intent.encode()],[dict(recipient_choice=q)],20)
    assert p.describe_intent(e.w3,e.ctx,intent,20)['mode']=='advanced-contract'
    altered=deepcopy(q);altered['choice'][4]='0x'+'00'*32
    with pytest.raises(ValueError):
        p.open_registration_choices(e.requester,e.ctx,[intent.encode()],[dict(recipient_choice=altered)],20)


def test_registration_driver_keeps_choice_private_until_existing_intent_publication(registered_env,monkeypatch):
    e=registered_env;verifier_code=e.w3.eth.get_code(e.verifier.address,block_identifier=20)
    e=matrix.__wrapped__(e,monkeypatch);e.codes[e.verifier.address]=verifier_code
    choice=e.preview('advanced-contract');q=choice['selection']
    e.intent=e.requester.make_intent(None,selection=q,accepted=choice['confirmation_hash'])
    request=e.requester.request(e.intent.encode(),e.accounts[1].address)
    e.publish(e.requester);e.service.service_once()
    assert not e.choices
    identifier=e.driver.prepare([e.intent.intent_id.hex()],'1600')['batch_id']
    exported=e.driver.export(identifier)
    assert exported['requests'][0]['recipient_choice']==q
    assert not e.choices
    result=e.driver.propose(identifier)
    assert not e.choices
    assert result['intent']['address_registration']['requests'][0]['recipient_choice']==q
    original_send=e.service.send
    def send(key,method,args,**kwargs):
        if method=='selectFor':
            assert e.public  # Existing canonical publication precedes opening.
            e.sent.append((key,method,args,kwargs))
            return e.requester.send(key,method,args,**kwargs)
        return original_send(key,method,args,**kwargs)
    e.service.send=send
    assert e.driver.execute(identifier)['status']=='REGISTERED'
    methods=[call[1] for call in e.sent]
    assert methods.index('publishIntent')<methods.index('selectFor')<methods.index('registerBatch')
    assert e.intent.intent_id in e.done


def test_receiver_code_change_does_not_reassign_or_delete_durable_watch(matrix,tmp_path):
    e=matrix;choice=e.preview('advanced-contract');q=choice['selection']
    intent=e.requester.make_intent(None,selection=q,accepted=choice['confirmation_hash'])
    e.requester.request(intent.encode(),e.accounts[1].address);e.publish(e.requester);e.service.service_once()
    before=e.service.store.intents();e.codes[e.target]=b'changed code'
    with pytest.raises(ValueError):e.requester.validate_intent(intent,e.ctx,e.block(20))
    e.requester.validate_intent(intent,e.ctx,e.block(20),current_recipient=False)
    assert e.service.store.intents()==before
    backup=tmp_path/'backup.sqlite';e.service.store.backup(backup)
    assert backup.is_file() and before[0][1]['intent']==intent.encode().hex()


def test_ordinary_recipient_nonce_and_legacy_draft_are_preserved(matrix):
    e=matrix;recipient=address(99)
    nonce='ab'*32
    intent=e.requester.make_intent(recipient,nonce)
    assert intent.request_nonce.hex()==nonce
    assert p.describe_intent(e.w3,e.ctx,intent,20)['mode']=='ordinary'


@pytest.mark.parametrize('name',['wallet-transfer','wallet-approve','wallet-withdraw'])
def test_typed_wallet_owner_calls_reuse_existing_transaction_lane(matrix,monkeypatch,name):
    from signer_agent import recipient_wallet_client as wallet,crypto
    from signer_agent.bootstrap import artifact
    from web3 import Web3
    e=matrix;e.owner=e.requester.account.address
    original=e.w3.eth.contract
    e.w3.eth.contract=lambda address,**kw: original(address=address,**kw) if address==e.wallet else Web3().eth.contract(address=address,**kw)
    sent=[];e.requester.send=lambda *a,**kw:sent.append((a,kw))
    monkeypatch.setattr(crypto,'call',lambda *a,**kw:dict(script='5120'+'22'*32))
    args=SimpleNamespace(action=name,wallet=e.wallet,amount_units='1000',recipient=address(101),
        spender=e.pool.functions.values['escrow'],bitcoin_destination='native fixture',withdrawal_id='17',wallet_salt='33'*32)
    result=wallet.action(e.requester,args)
    assert result['owner']==e.owner and result['ready'] is False
    positional,kw=sent[0]
    assert positional[1]=='execute' and positional[2][1]=='0' and kw==dict(target=e.wallet,kind='RecipientWalletV1',receipt=True)
    target,value,data=positional[2]
    kind='BitcoinWithdrawalEscrow' if name=='wallet-withdraw' else 'PoolMpToken'
    node=Web3().eth.contract(address=target,abi=artifact(kind)['abi'])
    fn,params=node.decode_function_input(data)
    assert fn.fn_name=={'wallet-transfer':'transfer','wallet-approve':'approve','wallet-withdraw':'requestDirect'}[name]
    assert 1000 in params.values()


def test_wallet_client_rejects_nonowner_before_sending(matrix):
    from signer_agent.recipient_wallet_client import action
    e=matrix;sent=[];e.requester.send=lambda *a,**kw:sent.append(a)
    # This is the same canonical wallet, but requester is not its immutable owner.
    e.owner=address(103)
    with pytest.raises(ValueError,match='WALLET_PROFILE'):
        action(e.requester,SimpleNamespace(action='wallet-transfer',wallet=e.wallet))
    assert not sent


@pytest.mark.parametrize('mode',['wallet-v1','advanced-contract'])
def test_smp1_confirms_profile_before_funding_and_rechecks_saved_choice_before_signing(matrix,funding_client,mode):
    from signer_agent.smp1_deposit import Deposit
    e=matrix;d,_,_,ctx=funding_client
    d.w3=e.w3;ctx['block']=e.block(20);d.stable=lambda _:None
    d.recipient_policy=lambda _:e.policy_address
    d.fence=Deposit.fence.__get__(d)
    recipient=e.wallet if mode=='wallet-v1' else e.target
    g=ctx['identity']
    q=p.preview(e.w3,e.policy_address,g['pool'],g['pool'],g['token'],p.ZERO,recipient,mode,20,seed='22'*32)
    with pytest.raises(ValueError,match='CONFIRMATION_REQUIRED'):
        d.prepare(recipient,'100000','2',max_fee='2000',recipient_choice=q,recipient_accepted='00'*32)
    assert not d.bitcoin.calls and not d.journal.list()
    quoted=d.prepare(recipient,'100000','2',max_fee='2000',recipient_choice=q,recipient_accepted=digest(q))
    assert quoted['quote']['recipient_choice']==q and quoted['quote']['recipient_profile']==mode
    e.codes[recipient]=b'changed after user confirmation'
    with pytest.raises(ValueError,match='CODE_CHANGED'):
        d.sign(quoted['quote']['operation_id'],quoted['quote_hash'])
    assert not any(c[0] in ('walletprocesspsbt','sendrawtransaction','lockunspent') for c in d.bitcoin.calls)


@pytest.mark.parametrize('tamper',[None,'parent-input','child-address','parent-reorg'])
def test_policy_child_requires_exact_canonical_registry_constructor_and_anchor(tamper):
    from test_contract_provenance import History
    from signer_agent.contract_provenance import prove_creation
    from signer_agent.bootstrap import artifact
    from signer_agent.bootstrap_create import create_address
    h=History();parent=address(111);child=create_address(parent,1)
    args=[address(112),b'g'*32,address(113),b'h'*32,b's'*32,b'q'*32,b'p'*32]
    tx,receipt=h.deploy(parent,'BitcoinAddressRegistry',args)
    if tamper=='parent-input':tx['input']=b'custom initcode returning canonical runtime'
    if tamper=='child-address':child=address(114)
    h.contracts[child]=(8,bytes.fromhex(artifact('RecipientPolicy')['deployedBytecode']['object'].removeprefix('0x')))
    nodes={parent:SimpleNamespace(functions=Functions(dict(recipientPolicy=child))),
           child:SimpleNamespace(functions=Functions(dict(creator=parent)))}
    h.w3.eth.contract=lambda address,**kw:nodes[address]
    if tamper in ('parent-input','child-address'):
        with pytest.raises(ValueError,match='CONSTRUCTOR_INPUT_MISMATCH'):prove_creation(h.w3,child,'RecipientPolicy',[],32)
        return
    proof=prove_creation(h.w3,child,'RecipientPolicy',[],32)
    assert proof['method']=='canonical_constructor' and proof['parent']['method']=='direct'
    if tamper=='parent-reorg':
        h.blocks[8]['hash']=keccak(text='replaced block')
        with pytest.raises(ValueError,match='ANCHOR_REORG'):prove_creation(h.w3,child,'RecipientPolicy',[],32)


def test_direct_policy_origin_is_retained_when_creator_later_has_delegated_code():
    from test_contract_provenance import History
    from signer_agent.contract_provenance import prove_creation
    h=History();where=address(115);tx,_=h.deploy(where,'RecipientPolicy',[])
    h.contracts[tx['from']]=(10,bytes.fromhex('ef0100')+bytes.fromhex(address(116)[2:]))
    assert prove_creation(h.w3,where,'RecipientPolicy',[],32)['method']=='direct'
