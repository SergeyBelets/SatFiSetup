"""Exact typed quorum actions. Owner consent remains a separate EVM call."""
from types import SimpleNamespace
from hashlib import sha256
from eth_abi import encode
from eth_utils import keccak
from eth_utils.abi import collapse_if_tuple,function_abi_to_4byte_selector
from .bootstrap import artifact,encode_args,Bootstrap
from .btc_fee_reserve import read,ZERO,Inspector,configured
from .operations import canonical
from .withdrawal_fee import configuration

METHODS={'prepare':('WithdrawalFeePayments','payments','getPrepareOperationHash'),
         'register':('WithdrawalFeeFunding','funding','getRegisterOperationHash')}
LIMIT=131072

def operation(ctx,method,wire,w3):
    if method not in METHODS or ctx.get('withdrawal_fees') is None:raise ValueError('WITHDRAWAL_FEE_ACTION')
    kind,key,helper=METHODS[method];target=ctx['withdrawal_fees'][key]
    fn=next(x for x in artifact(kind)['abi'] if x.get('name')==method)
    specs=fn['inputs'][:-1];args=encode_args(specs,wire);types=[collapse_if_tuple(s) for s in specs]
    c=args[0][0] if method=='prepare' else args[2]
    scope=bytes(read(target,ctx['block'].number,'scope'))
    params=keccak(encode(['bytes32']+types,[scope,*args]))
    op=keccak(encode(['string','uint256','address','address','bytes4','bytes32','uint256'],[
        'SATOHSI_FROST_AUTHORIZATION_V1',w3.eth.chain_id,read(ctx['fee'],ctx['block'].number,'authorization'),
        target.address,function_abi_to_4byte_selector(fn),params,c[0]]))
    if bytes(read(target,ctx['block'].number,helper,*args))!=op:raise ValueError('WITHDRAWAL_FEE_HASH_MISMATCH')
    return target,kind,helper,args,c,op

def validate(i,ctx,method,args,c,meta,*,bundle=None,state=None):
    target,_,_,_,_,_=operation(ctx,method,meta['args'],i.w3);n=ctx['block'].number;r=ctx['registry'];fee=ctx['withdrawal_fees']
    if (c[0]!=int(read(target,n,'nonce')) or c[1]!=int(read(r,n,'custodyGeneration')) or c[2]!=int(read(r,n,'derivationGeneration'))
            or not ctx['block'].timestamp<c[3]<=ctx['block'].timestamp+3600):raise ValueError('WITHDRAWAL_FEE_GENERATION_OR_WINDOW')
    i.fence(ctx)
    if method=='register':
        from .withdrawal_fee_intent import WithdrawalFeeIntentV1,PROFILE
        request,raw,_=args;b=read(fee['credits'],n,'getBudget',request);original=read(ctx['escrow'],n,'getRequest',request)
        intent=WithdrawalFeeIntentV1.decode(raw);p=intent.parent;v=fee['verifier']
        if (read(v,n,'FEE_QUALIFIED') is not True or bytes(read(v,n,'FEE_PROFILE'))!=keccak(text=PROFILE)
                or int(read(r,n,'derivationState'))!=0):raise ValueError('WITHDRAWAL_FEE_VERIFIER_UNQUALIFIED')
        if int(b[3])!=1 or b[13] or b[9]<ctx['block'].timestamp or int(original[-1]) not in (1,2):raise ValueError('WITHDRAWAL_FEE_CONSENT_REQUIRED')
        from web3 import Web3
        wanted=(bytes(read(r,n,'bitcoinGenesis')),i.w3.eth.chain_id,bytes.fromhex(r.address[2:]),bytes.fromhex(ctx['pool'].address[2:]),
            bytes.fromhex(b[1][2:]),bytes(read(r,n,'graphId')),bytes.fromhex(read(r,n,'dkg')[2:]),bytes(read(r,n,'walletId')),
            bytes(read(r,n,'walletResult')),int(read(r,n,'keyEpoch')),bytes.fromhex(b[0][2:]),bytes.fromhex(b[0][2:]),
            keccak(encode(['bytes32','bytes32'],[bytes(read(fee['funding'],n,'scope')),request])))
        actual=(p.bitcoin_genesis,p.evm_chain_id,p.deposit_registry,p.pool,p.mp_token,p.custody_graph_id,p.dkg_registry,p.wallet_id,
            p.wallet_result,p.key_epoch,p.requester,p.recipient,p.request_nonce)
        if (actual!=wanted or intent.request!=request or intent.credits_scope.hex()!=fee['scope'] or intent.refund_script_hash!=keccak(bytes(b[12]))
                or bytes(read(fee['funding'],n,'requestBinding',request))!=bytes(32)):
            raise ValueError('WITHDRAWAL_FEE_BINDING')
        script=bytes(b[12]);supported=len(script)==22 and script[:2]==b'\0\x14' or len(script)==34 and script[:2]==b'\x51\x20'
        if (not supported or read(ctx['common'],n,'scriptPools',keccak(script))!=ZERO
                or bytes(read(ctx['common'],n,'feeScripts',keccak(script)))!=bytes(32)):raise ValueError('WITHDRAWAL_FEE_REFUND_EXTERNAL_REQUIRED')
        desc=intent.derive(b'\x02'+bytes(read(r,n,'baseX')))
        onchain=read(v,n,'deriveFee',raw,b'\x02'+bytes(read(r,n,'baseX')))
        expected=[bytes.fromhex(desc[k]) for k in ('intent_id','merkle_root','tweak','output_key')]+[desc['output_parity_odd'],bytes.fromhex(desc['binding_commitment']),bytes.fromhex(desc['wallet_context'])]
        if list(onchain)!=expected:raise ValueError('WITHDRAWAL_FEE_BINDING')
        return
    from .bitcoin_onchain_plan import validate_plan,onchain_plan_hash
    from .bitcoin_onchain_payment import _review_wallet
    from .bitcoin_input_keys import build,guard as input_guard
    from .bitcoin_trust import inspect_registry,graph_options
    from .dkg_protocol import Manifest
    if bundle is None:raise ValueError('WITHDRAWAL_FEE_ENROLLMENT_REQUIRED')
    plan=validate_plan(meta['bitcoin_plan']);p=args[0]
    if plan['version']!='4':raise ValueError('WITHDRAWAL_FEE_PLAN_REQUIRED')
    if int(plan['max_fee'])>int(read(ctx['fee'],n,'protocolFeeCap')):raise ValueError('WITHDRAWAL_FEE_PROTOCOL_CAP')
    registry=SimpleNamespace(w3=i.w3);manifest,key=_review_wallet(Manifest.from_dict(bundle),registry,ctx['board'],plan,ctx['block'])
    options=dict(version='2',bitcoin_network=i.c['bitcoin_network'],evm_confirmations=i.c['evm_confirmations'])
    if i.c['bitcoin_policy']:options['bitcoin_policy']=i.c['bitcoin_policy']
    graph=inspect_registry(manifest,key,registry,ctx['batch'].address,address_signing=True,**graph_options(options))['graph']
    input_guard(manifest,key,plan,registry,graph,state);built=build(plan);m=plan['withdrawal_fee']
    if (bytes(p[1]).hex()!=plan['batch_id'] or bytes(p[2]).hex()!=m['previous'] or bytes(p[3])!=onchain_plan_hash(manifest,plan)
            or bytes(p[4]).hex()!=built['unsigned'] or [bytes(x).hex() for x in p[5]]!=plan['withdrawal_refs']
            or Bootstrap.wire(p[6])!=m['charges'] or Bootstrap.wire(p[7])!=m['credits'] or bytes(p[8]).hex()!=m['treasury']
            or m['payments']!=target.address or p[0][1]!=int(plan['custody_generation'])):raise ValueError('WITHDRAWAL_FEE_PLAN_BINDING')
    i.healthy(state=state)
    for item in plan['inputs']:
        txid,vout=item['outpoint'].split(':');row,_=i.observed(ctx,txid,vout,item['script'],int(item['amount']))
        if row is None:raise ValueError('WITHDRAWAL_FEE_INPUT_UNAVAILABLE')
        actual=i.bitcoin.call('gettxout',txid,int(vout),bytes(p[2])==bytes(32))
        if not actual or int(actual['confirmations'])<max(101 if actual.get('coinbase') else 6,int(read(ctx['common'],n,'depth'))):
            raise ValueError('WITHDRAWAL_FEE_FUNDING_IMMATURE')
        nominal=read(ctx['common'],n,'getValue',keccak(encode(['bytes32','uint32'],[bytes.fromhex(txid)[::-1],int(vout)])))
        if not nominal[2] or int(nominal[0])!=int(item['amount']) or bytes(nominal[1]).hex()!=item['script']:raise ValueError('WITHDRAWAL_FEE_PREVOUT_VALUE')
    verified=read(fee['plan_verifier'],n,'verify',target.address,p)
    if (int(verified[7])!=int(plan['derivation_generation']) or int(verified[3])!=int(plan['fee'])
            or int(read(target,n,'versionCount',verified[0]))>=int(i.c['max_versions'])):raise ValueError('WITHDRAWAL_FEE_PLAN_BINDING')
    treasury=bytes(p[8]);debit=int(verified[5])
    if treasury==bytes(32):
        if debit:raise ValueError('WITHDRAWAL_FEE_TREASURY_SOURCE_REQUIRED')
    else:
        grant=read(ctx['fee'],n,'allocations',treasury)
        if (not grant[3] or grant[2] or debit>int(grant[0]) or debit>int(grant[1]) or int(grant[0])>int(verified[6])
                or grant[4] and bytes(grant[5])!=bytes(verified[0])):raise ValueError('WITHDRAWAL_FEE_TREASURY_CAP')
    i.fence(ctx)
    if int(plan['derivation_generation']) and (int(read(r,'latest','derivationGeneration'))!=int(plan['derivation_generation'])
            or int(read(r,'latest','derivationState'))!=0):raise ValueError('WITHDRAWAL_FEE_GENERATION')

def review(w3,intent,bundle,number,*,inspector=None,session=None,published=True,executable=True,state=None):
    keys={'chain_id','coordinator','role','target','artifact','hash_method','hash_args','session','operation_hash','withdrawal_fee_request'}
    if not isinstance(intent,dict) or set(intent)!=keys or len(canonical(intent).encode())>LIMIT:raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
    m=intent['withdrawal_fee_request']
    if (not isinstance(m,dict) or set(m)!={'version','pool','registration_board','reserve','method','args','bitcoin_plan'} or m['version']!='1'
            or m['method'] not in METHODS or m['method']=='register' and m['bitcoin_plan'] is not None):raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
    if inspector is None:
        inspector=object.__new__(Inspector);inspector.w3=w3
        inspector.c=dict(reserve=m['reserve'],chain_id=str(w3.eth.chain_id),evm_confirmations='1')
    if inspector.c['reserve']!=m['reserve']:raise ValueError('WITHDRAWAL_FEE_GRAPH')
    ctx=inspector.context(number);n=ctx['block'].number
    from .address_registration import authority
    a=authority(w3,ctx,m['registration_board'],n,bundle)
    target,kind,helper,args,c,op=operation(ctx,m['method'],m['args'],w3)
    if (intent['chain_id']!=str(w3.eth.chain_id) or intent['role']!='conf_ceremony' or intent['artifact']!=kind or intent['hash_method']!=helper
            or intent['target']!=target.address or intent['coordinator']!=a['coordinator'].address or intent['operation_hash']!=op.hex()
            or intent['hash_args']!=Bootstrap.wire(args) or m['pool']!=ctx['pool'].address):raise ValueError('WITHDRAWAL_FEE_HASH_MISMATCH')
    sid=int(intent['session'])
    if str(sid)!=intent['session'] or sid<=0 or session is not None and sid!=int(session):raise ValueError('WITHDRAWAL_FEE_SESSION_MISMATCH')
    if published and bytes(read(a['board'],n,'intents',sid))!=canonical(intent).encode():raise ValueError('WITHDRAWAL_FEE_PUBLIC_INTENT_REQUIRED')
    info=read(a['coordinator'],n,'getSession',sid)
    from .onchain_transport import can_publish_intent
    if (int(info[0])!=sid or not can_publish_intent(a['board'],info[1],n) or bytes(info[3])!=op or not info[4] or not info[8]
            or info[9]!=ZERO or info[15]!=target.address or bytes(read(a['coordinator'],n,'sessionDKGId',sid))!=a['dkg_id']
            or bytes(read(a['coordinator'],n,'sessionDKGResult',sid))!=a['result_hash']):raise ValueError('WITHDRAWAL_FEE_SESSION_MISMATCH')
    complete=False
    if read(a['authorization'],n,'executedOperations',op):
        if m['method']=='prepare':complete=bytes(read(target,n,'receipts',op))==keccak(encode(['bytes32',collapse_if_tuple(next(x for x in artifact(kind)['abi'] if x.get('name')=='prepare')['inputs'][0])],[bytes(read(target,n,'scope')),args[0]]))
        else:
            identifier=sha256(args[1]).digest();row=read(target,n,'binding',identifier)
            complete=bytes(row[0])==args[0] and bytes(row[1])==args[1] and bytes(read(target,n,'requestBinding',args[0]))==identifier
        if not complete or read(a['authorization'],n,'operationTargets',op)!=target.address:raise ValueError('WITHDRAWAL_FEE_RECEIPT_MISMATCH')
    if executable and not complete:
        if not hasattr(inspector,'journal'):raise ValueError('WITHDRAWAL_FEE_CONFIG_REQUIRED')
        if int(info[10]) not in (1,2) or int(info[10])==1 and int(info[7])!=c[3]:raise ValueError('WITHDRAWAL_FEE_SESSION_MISMATCH')
        validate(inspector,ctx,m['method'],args,c,m,bundle=bundle,state=state)
    inspector.stable(ctx['block'])
    return dict(context=ctx,authority=a,target=target,kind=kind,method=m['method'],arguments=args,operation_hash=op,complete=complete,session=sid)

def guard_signing_session(client,session):
    if session.origin_contract==ZERO:return
    anchor=getattr(client,'_read_snapshot',None) or client.w3.eth.get_block('latest')
    binding=getattr(client,'withdrawal_fee_binding',None)
    if binding is not None and binding['target']==session.origin_contract:
        if bytes.fromhex(binding['operation_hash'].removeprefix('0x'))!=bytes(session.message_hash):raise ValueError('WITHDRAWAL_FEE_HASH_MISMATCH')
        for n in (anchor.number,'latest'):review_binding(client.w3,binding,n)
        client.check_read_snapshot();return
    from .bitcoin_trust import checked_runtime
    matched=False
    for kind,_,_ in METHODS.values():
        try:checked_runtime(client.w3,session.origin_contract,kind,anchor.number);matched=True;break
        except ValueError as exc:
            if str(exc)!='BTC_CONTRACT_BUILD_MISMATCH':raise
    if not matched:return
    payload=getattr(client,'withdrawal_fee_context',None)
    if payload is None:raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
    intent,bundle,i,state=payload
    if intent['operation_hash']!=session.message_hash.hex():raise ValueError('WITHDRAWAL_FEE_HASH_MISMATCH')
    for n in (anchor.number,'latest'):
        r=review(client.w3,intent,bundle,n,inspector=i,session=session.session_id,state=state)
        if r['complete'] or r['authority']['coordinator'].address!=client.address:raise ValueError('WITHDRAWAL_FEE_SESSION_MISMATCH')
    client.continuity_context=(r['context'],'DERIVED_SIGN' if r['method']=='register' else 'BASE_SIGN',session.session_id)
    client.check_read_snapshot()

def review_binding(w3,intent,n):
    n=w3.eth.get_block(n).number
    args=intent.get('hash_args')
    if not isinstance(args,list) or len(args)!=1:raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
    c=w3.eth.contract(address=args[0],abi=artifact('WithdrawalFeeCredits')['abi'])
    r=w3.eth.contract(address=read(c,n,'addresses'),abi=artifact('BitcoinAddressRegistry')['abi'])
    b=w3.eth.contract(address=read(r,n,'batches'),abi=artifact('BitcoinBatchRegistry')['abi'])
    i=object.__new__(Inspector);i.w3=w3;i.c=dict(reserve=read(b,n,'feeReserve'),chain_id=str(w3.eth.chain_id),evm_confirmations='1')
    ctx=i.context(n);e=ctx['escrow'];configuration(w3,ctx,n,root=c.address)
    if (e.address!=intent['target'] or read(e,n,'withdrawalFees')!=ZERO or int(read(b,n,'nonce'))!=2
            or bytes(read(e,n,'getSetWithdrawalFeesOperationHash',c.address))!=bytes.fromhex(intent['operation_hash'].removeprefix('0x'))):raise ValueError('WITHDRAWAL_FEE_GRAPH')
    return ctx
