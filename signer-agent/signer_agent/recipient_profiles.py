"""Recipient matrix. Immutable selection is separate from current eligibility.

Only RecipientWalletV1 created by a proven RecipientPolicy is a supported wallet.
Advanced contract acceptance promises an exact destination, not extractability.
"""
import secrets
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
from web3.exceptions import ContractLogicError
from .bootstrap import artifact, Bootstrap
from .btc_fee_reserve import read, digest, ZERO
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation

MODES={'ordinary':0,'wallet-v1':1,'advanced-contract':2}
CHOICE_TYPE='(uint8,bytes32,address,bytes32,bytes32)'
RISK='MP may be permanently inaccessible. This contract has no verified MP withdrawal interface; upgrades, proxies and future control are not qualified.'


def policy_node(w3,address,number):
    checked_runtime(w3,address,'RecipientPolicy',number)
    prove_creation(w3,address,'RecipientPolicy',[],number)
    return w3.eth.contract(address=address,abi=artifact('RecipientPolicy')['abi'])


def context_hash(chain,policy,namespace,pool,token,requester,recipient):
    return keccak(encode(['bytes32','uint256']+['address']*6,[keccak(text='SATOHSI_RECIPIENT_CONTEXT_V1'),
        chain,policy,namespace,pool,token,requester,recipient]))


def risk_hash(context,runtime):
    return keccak(encode(['bytes32']*3,[keccak(text='SATOHSI_ADVANCED_MP_MAY_BE_PERMANENTLY_INACCESSIBLE_V1'),context,runtime]))


def nonce_for(context,choice):
    return keccak(encode(['bytes32','bytes32',CHOICE_TYPE],[keccak(text='SATOHSI_RECIPIENT_CHOICE_V1'),context,choice]))


def _code(w3,recipient,number):
    if not Web3.is_checksum_address(recipient) or recipient==ZERO:raise ValueError('RECIPIENT_PROFILE')
    code=bytes(w3.eth.get_code(recipient,block_identifier=number))
    if len(code)==23 and code[:3]==bytes.fromhex('ef0100'):raise ValueError('RECIPIENT_DELEGATED_UNSUPPORTED')
    return code


def review(w3,policy,recipient,choice,context,number):
    mode,runtime,owner,seed,ack=choice;code=_code(w3,recipient,number)
    if mode==0:
        if code:raise ValueError('RECIPIENT_ORDINARY_PROFILE')
        return
    if mode not in (1,2) or not code or keccak(code)!=runtime or seed==bytes(32):raise ValueError('RECIPIENT_CODE_CHANGED')
    if mode==1:
        checked_runtime(w3,recipient,'RecipientWalletV1',number)
        wallet=w3.eth.contract(address=recipient,abi=artifact('RecipientWalletV1')['abi'])
        if (owner==ZERO or _code(w3,owner,number) or read(policy,number,'wallets',recipient)!=owner
                or bytes(read(policy,number,'walletHashes',recipient))!=runtime or read(wallet,number,'owner')!=owner
                or ack!=bytes(32)):
            raise ValueError('RECIPIENT_WALLET_PROFILE')
    elif owner!=ZERO or ack!=risk_hash(context,runtime):raise ValueError('RECIPIENT_ADVANCED_ACK')


def preview(w3,policy_address,namespace,pool,token,requester,recipient,mode,number,*,seed=None,sinks=()):
    if mode not in MODES:raise ValueError('RECIPIENT_PROFILE')
    if recipient in set(sinks)|{ZERO,policy_address,namespace,pool,token}:raise ValueError('RECIPIENT_SINK')
    policy=policy_node(w3,policy_address,number)
    context=context_hash(w3.eth.chain_id,policy.address,namespace,pool,token,requester,recipient)
    code=_code(w3,recipient,number);m=MODES[mode]
    seed=bytes.fromhex(seed) if seed else secrets.token_bytes(32)
    if len(seed)!=32 or seed==bytes(32):raise ValueError('RECIPIENT_PROFILE')
    runtime=keccak(code) if code else bytes(32)
    owner=read(policy,number,'wallets',recipient) if m==1 else ZERO
    choice=[m,runtime,owner,seed,risk_hash(context,runtime) if m==2 else bytes(32)]
    review(w3,policy,recipient,choice,context,number)
    return dict(version='1',chain_id=str(w3.eth.chain_id),policy=policy.address,namespace=namespace,pool=pool,token=token,
        requester=requester,recipient=recipient,mode=mode,choice=Bootstrap.wire(choice),
        context=context.hex(),request_nonce=(nonce_for(context,choice) if m else seed).hex(),
        risk=RISK if m==2 else 'Current profile only; no guarantee of future key availability or third-party ownership.')


def choice_args(q):
    from .bootstrap import encode_args
    fn=next(x for x in artifact('RecipientPolicy')['abi'] if x.get('name')=='nonceFor')
    return encode_args(fn['inputs'],['0x'+q['context'],q['choice']])[1]


def review_quote(w3,q,accepted,number,*,sinks=()):
    if not isinstance(q,dict) or accepted!=digest(q):raise ValueError('RECIPIENT_CONFIRMATION_REQUIRED')
    current=preview(w3,q['policy'],q['namespace'],q['pool'],q['token'],q['requester'],q['recipient'],q['mode'],number,
        seed=q['choice'][3].removeprefix('0x'),sinks=sinks)
    if current!=q:raise ValueError('RECIPIENT_CODE_CHANGED')
    return choice_args(q)


def registry_policy(w3,ctx,number):
    r=ctx['registry'];address=read(r,number,'recipientPolicy')
    node=policy_node(w3,address,number)
    if read(node,number,'creator')!=r.address:raise ValueError('RECIPIENT_POLICY_ORIGIN')
    return node


def require_not_sink(registry,number,pool,token,recipient):
    try:read(registry,number,'requireRecipientNotSink',pool,token,recipient)
    except ContractLogicError:raise ValueError('RECIPIENT_SINK') from None


def describe_intent(w3,ctx,intent,number,*,validate=True,selection=None):
    r=ctx['registry'];recipient=Web3.to_checksum_address(intent.recipient);pool=ctx['pool']
    token=read(pool,number,'mpToken');requester=Web3.to_checksum_address(intent.requester)
    if selection is not None:
        policy=registry_policy(w3,ctx,number);q=selection
        if (q['policy']!=policy.address or q['namespace']!=r.address or q['pool']!=pool.address
                or q['token']!=token or q['requester']!=requester or q['recipient']!=recipient
                or q['request_nonce']!=intent.request_nonce.hex()):raise ValueError('RECIPIENT_SELECTION_REQUIRED')
        choice=review_quote(w3,q,digest(q),number)
        require_not_sink(r,number,pool.address,token,recipient)
        return dict(mode=q['mode'],recipient=recipient,runtime_hash=choice[1].hex(),owner=choice[2],
            acknowledgement=choice[4].hex(),risk=q['risk'],ownership_proven=False)
    try:
        mode=int(read(r,number,'validateRecipient',pool.address,token,requester,recipient,intent.request_nonce)) if validate else None
    except ContractLogicError:
        raise ValueError('ADDRESS_RECIPIENT_PROFILE') from None
    if mode==0:
        if _code(w3,recipient,number):raise ValueError('ADDRESS_RECIPIENT_PROFILE')
        return dict(mode='ordinary',recipient=recipient,ownership_proven=False)
    policy=registry_policy(w3,ctx,number)
    selection=read(policy,number,'selection',intent.request_nonce)
    context=context_hash(w3.eth.chain_id,policy.address,r.address,pool.address,token,requester,recipient)
    choice=list(selection[1]);choice[1]=bytes(choice[1]);choice[3]=bytes(choice[3]);choice[4]=bytes(choice[4])
    if bytes(selection[0])==bytes(32):
        if validate:raise ValueError('RECIPIENT_SELECTION_REQUIRED')
        return dict(mode='ordinary',recipient=recipient,ownership_proven=False)
    if bytes(selection[0])!=context or nonce_for(context,choice)!=intent.request_nonce:
        raise ValueError('RECIPIENT_SELECTION_REQUIRED')
    if validate:
        if mode!=choice[0]:raise ValueError('RECIPIENT_SELECTION_REQUIRED')
        review(w3,policy,recipient,choice,context,number)
    return dict(mode=next(k for k,v in MODES.items() if v==int(choice[0])),recipient=recipient,
        runtime_hash=choice[1].hex(),owner=choice[2],acknowledgement=choice[4].hex(),
        risk=RISK if int(choice[0])==2 else None,ownership_proven=False)


def address_preview(client,recipient,mode,*,seed=None):
    b,pool,ctx,_,_=client.context();policy=registry_policy(client.w3,ctx,b.number)
    require_not_sink(ctx['registry'],b.number,pool.address,client.read(pool,b,'mpToken'),recipient)
    q=preview(client.w3,policy.address,ctx['registry'].address,pool.address,client.read(pool,b,'mpToken'),
        client.account.address,recipient,mode,b.number,seed=seed,
        sinks=[ctx['common'].address,ctx['batch'].address,client.read(pool,b,'authorization'),client.read(pool,b,'escrow')])
    client.stable(b)
    return dict(status='RECIPIENT_REVIEW',selection=q,confirmation_hash=digest(q),ready=False)


def authorize_address(client,q,accepted):
    b,pool,ctx,_,_=client.context();policy=registry_policy(client.w3,ctx,b.number)
    if (q['policy']!=policy.address or q['namespace']!=ctx['registry'].address or q['pool']!=pool.address
            or q['token']!=client.read(pool,b,'mpToken') or q['requester']!=client.account.address):
        raise ValueError('RECIPIENT_SELECTION_REQUIRED')
    review_quote(client.w3,q,accepted,b.number,sinks=[ctx['common'].address,ctx['batch'].address,
        client.read(pool,b,'authorization'),client.read(pool,b,'escrow')])
    require_not_sink(ctx['registry'],b.number,pool.address,q['token'],q['recipient'])
    client.stable(b)
    return q['request_nonce']


def open_registration_choices(client,ctx,raws,proofs,number):
    """Called only after the existing public registration intent is authenticated.

    Before that boundary the opening stays in private state/encrypted REQUEST.
    A permissionless opening is not authorization: the quorum and requester
    commitment still authenticate the exact raw intent containing its hash.
    """
    from .deposit_intent import DepositIntentV1
    for raw,proof in zip(raws,proofs):
        q=proof.get('recipient_choice')
        if not q or q['mode']=='ordinary':continue
        intent=DepositIntentV1.decode(raw)
        if bytes(read(ctx['registry'],number,'getBinding',intent.intent_id)[0]):continue
        describe_intent(client.w3,ctx,intent,number,selection=q)
        client.send('recipient-choice-'+q['request_nonce'],'selectFor',
            [q['namespace'],q['pool'],q['token'],q['requester'],q['recipient'],q['choice']],
            target=q['policy'],kind='RecipientPolicy',receipt=True)
