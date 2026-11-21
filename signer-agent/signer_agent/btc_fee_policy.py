"""Typed FROST review for canonical BTC reserve actions; no arbitrary calldata signing."""
from types import SimpleNamespace
from eth_abi import encode
from eth_utils import keccak
from eth_utils.abi import collapse_if_tuple,function_abi_to_4byte_selector
from .bootstrap import artifact,encode_args,Bootstrap
from .operations import canonical
from .btc_fee_reserve import Inspector,configured,read,ZERO

METHODS=frozenset(('publishReconciliation','admitDonation','allocate','raiseCap','release','preparePlan'))


def action(method,wire):
    if method not in METHODS:raise ValueError('BTC_FEE_ACTION')
    fn=next(x for x in artifact('BitcoinFeeReserve')['abi'] if x.get('name')==method)
    specs=fn['inputs'][:-1];args=encode_args(specs,wire)
    data=encode([collapse_if_tuple(s) for s in specs],args)
    if len(data)>200000:raise ValueError('BTC_FEE_INTENT_LIMIT')
    control=args[0][0] if method!='release' else args[0]
    return fn,args,data,control


def operation(ctx,method,wire,w3):
    fn,args,data,c=action(method,wire);f=ctx['fee'];n=ctx['block'].number
    params=keccak(encode(['bytes32','bytes32'],[bytes.fromhex(ctx['scope']),keccak(data)]))
    selector=function_abi_to_4byte_selector(fn)
    auth=read(f,n,'authorization')
    op=keccak(encode(['string','uint256','address','address','bytes4','bytes32','uint256'],[
        'SATOHSI_FROST_AUTHORIZATION_V1',w3.eth.chain_id,auth,f.address,selector,params,c[0]]))
    if bytes(read(f,n,'getOperationHash',selector,data,c[0]))!=op:raise ValueError('BTC_FEE_HASH_MISMATCH')
    return fn,args,data,c,op


def validate(inspector,ctx,method,args,control,meta,*,bundle=None,state=None):
    f=ctx['fee'];r=ctx['registry'];b=ctx['block'];n=b.number;c=control
    if (c[0]!=int(read(f,n,'nonce')) or c[1]!=int(read(r,n,'custodyGeneration'))
            or c[2]!=int(read(r,n,'derivationGeneration')) or not b.timestamp<c[3]<=b.timestamp+3600):
        raise ValueError('BTC_FEE_GENERATION_OR_WINDOW')
    # Negative observations may suspend signing without claiming assets exist.
    # Recovering to HEALTHY always requires fresh complete native Core evidence.
    if method=='publishReconciliation':
        report=args[0]
        if report[1]==1:
            result=inspector.reconcile(full=True,state=state)
            if result['status']!='HEALTHY':raise ValueError('BTC_FEE_'+result['status'])
            body=result['inventory']
            if (str(report[2])!=body['height'] or bytes(report[3]).hex()!=body['tip']
                    or bytes(report[9]).hex()!=result['evidence'] or not b.timestamp<report[4]<=b.timestamp+int(inspector.c['report_seconds'])
                    or [str(x) for x in report[5:9]]!=[body[k] for k in ('wallet','backing','pending','free')]):
                raise ValueError('BTC_FEE_REPORT_MISMATCH')
        elif report[1] not in (2,3):raise ValueError('BTC_FEE_REPORT_MISMATCH')
        return
    inspector.fence(ctx)
    if method=='admitDonation':
        inspector.donation(ctx,args[0][1]);return
    if method=='release':
        a=read(f,n,'allocations',args[1]);root=bytes(a[5])
        if not a[3] or (root!=bytes(32) and int(read(ctx['batch'],n,'batchState',root)) not in (0,3)):
            raise ValueError('BTC_FEE_SIGNATURE_MAY_EXIST')
        # Call the actual method as eth_call with an unapproved operation later;
        # treasury obligations are independently checked here as well.
        if a[2] and int(read(ctx['escrow'],n,'getRequest',args[1])[-1]) not in (0,4):
            raise ValueError('BTC_FEE_TREASURY_OBLIGATION')
        return
    inspector.healthy(state=state)
    if method in ('allocate','raiseCap'):
        g=args[0];cap,max_fee=int(g[2]),int(g[3])
        if (cap<=0 or cap>2100000000000000 or max_fee<=0 or max_fee>cap
                or max_fee>int(read(f,n,'protocolFeeCap'))):raise ValueError('BTC_FEE_CAP')
        a=read(f,n,'allocations',g[1]);available=int(read(f,n,'available'))
        if method=='allocate':
            if a[0]!=0 or cap>available:raise ValueError('BTC_FEE_CAP')
        else:
            root=bytes(a[5]);collateral=None
            if root!=bytes(32):
                collateral=int(read(f,n,'userInputFree',root)) if read(f,n,'userLocked',root) else int(read(f,n,'getPlan',root)[7])
            if (not a[3] or a[2]!=g[4] or cap<int(a[0]) or max_fee<int(a[1])
                    or not a[4] and cap-int(a[0])>available or collateral is not None and cap>collateral):raise ValueError('BTC_FEE_CAP')
        return
    if method=='preparePlan':
        from .bitcoin_onchain_plan import validate_plan,onchain_plan_hash
        from .bitcoin_onchain_payment import _review_wallet
        from .bitcoin_input_keys import build,guard as input_guard
        from .dkg_protocol import Manifest
        from .bitcoin_trust import inspect_registry,graph_options
        if bundle is None:raise ValueError('BTC_FEE_ENROLLMENT_REQUIRED')
        plan=validate_plan(meta['bitcoin_plan']);p=args[0]
        if plan['version']!='3':raise ValueError('BTC_FEE_ADDRESS_PROFILE_REQUIRED')
        registry=SimpleNamespace(w3=inspector.w3)
        manifest,key=_review_wallet(Manifest.from_dict(bundle),registry,ctx['board'],plan,b)
        graph=inspect_registry(manifest,key,registry,ctx['batch'].address,address_signing=True,
            **graph_options(dict(version='2',bitcoin_network=inspector.c['bitcoin_network'],
                evm_confirmations=inspector.c['evm_confirmations'],**({'bitcoin_policy':inspector.c['bitcoin_policy']} if inspector.c['bitcoin_policy'] else {}))))['graph']
        input_guard(manifest,key,plan,registry,graph)
        built=build(plan)
        if (bytes(p[2]).hex()!=plan['batch_id']
                or bytes(p[4])!=onchain_plan_hash(manifest,plan) or bytes(p[5]).hex()!=built['unsigned']
                or [bytes(x).hex() for x in p[6]]!=plan['withdrawal_refs'] or p[0][1]!=int(plan['custody_generation'])
                or int(plan['max_fee'])>int(read(f,n,'protocolFeeCap'))):raise ValueError('BTC_FEE_PLAN_MISMATCH')
        a=read(f,n,'allocations',p[1])
        if int(plan['max_fee'])>int(a[1]):raise ValueError('BTC_FEE_CAP')
        inspector.fence(ctx,derived=int(plan['derivation_generation']))
        for item in plan['inputs']:
            display,vout=item['outpoint'].split(':')
            row,pending=inspector.observed(ctx,display,vout,item['script'],int(item['amount']))
            if row is None:raise ValueError('BTC_FEE_UTXO_UNAVAILABLE')
            actual=inspector.bitcoin.call('gettxout',display,int(vout),bytes(p[3])==bytes(32))
            if not actual or actual.get('coinbase') and int(actual['confirmations'])<101:raise ValueError('BTC_FEE_FUNDING_IMMATURE')
        verifier=inspector.w3.eth.contract(address=read(f,n,'planVerifier'),abi=artifact('BitcoinFeePlanVerifier')['abi'])
        v=read(verifier,n,'verify',f.address,p)
        if int(read(f,n,'familyCount',v[0]))>=int(inspector.c['max_versions']):raise ValueError('BTC_FEE_VERSION_LIMIT')
        if int(v[8])!=int(plan['derivation_generation']):raise ValueError('BTC_FEE_PLAN_MISMATCH')


def review(w3,intent,bundle,number,*,inspector=None,session=None,published=True,executable=True,state=None):
    expected={'chain_id','coordinator','role','target','artifact','hash_method','hash_args','session','operation_hash','btc_fee_reserve'}
    if not isinstance(intent,dict) or set(intent)!=expected or len(canonical(intent))>32768:raise ValueError('BTC_FEE_INTENT_REQUIRED')
    meta=intent['btc_fee_reserve']
    if (not isinstance(meta,dict) or set(meta)!={'version','pool','registration_board','method','args','bitcoin_plan'}
            or meta['version']!='1' or (meta['method']!='preparePlan' and meta['bitcoin_plan'] is not None)):
        raise ValueError('BTC_FEE_INTENT_REQUIRED')
    if inspector is None:
        # Public provenance review, never a substitute for local signing policy.
        inspector=object.__new__(Inspector);inspector.w3=w3
        inspector.c=dict(reserve=intent['target'],chain_id=str(w3.eth.chain_id),evm_confirmations='1')
    ctx=inspector.context(number);n=ctx['block'].number
    from .address_registration import authority
    a=authority(w3,ctx,meta['registration_board'],n,bundle)
    fn,args,data,c,op=operation(ctx,meta['method'],meta['args'],w3)
    wanted=Bootstrap.wire([function_abi_to_4byte_selector(fn),data,c[0]])
    if (intent['chain_id']!=str(w3.eth.chain_id) or intent['role']!='conf_ceremony'
            or intent['artifact']!='BitcoinFeeReserve' or intent['hash_method']!='getOperationHash'
            or intent['target']!=ctx['fee'].address or meta['pool']!=ctx['pool'].address
            or intent['coordinator']!=a['coordinator'].address or intent['hash_args']!=wanted or intent['operation_hash']!=op.hex()):
        raise ValueError('BTC_FEE_HASH_MISMATCH')
    sid=int(intent['session'])
    if str(sid)!=intent['session'] or sid<=0 or session is not None and sid!=int(session):raise ValueError('BTC_FEE_SESSION_MISMATCH')
    if published and bytes(read(a['board'],n,'intents',sid))!=canonical(intent).encode():raise ValueError('BTC_FEE_PUBLIC_INTENT_REQUIRED')
    info=read(a['coordinator'],n,'getSession',sid)
    from .onchain_transport import can_publish_intent
    if (int(info[0])!=sid or not can_publish_intent(a['board'],info[1],n) or bytes(info[3])!=op
            or not info[4] or not info[8] or info[9]!=ZERO or info[15]!=ctx['fee'].address
            or bytes(read(a['coordinator'],n,'sessionDKGId',sid))!=a['dkg_id']
            or bytes(read(a['coordinator'],n,'sessionDKGResult',sid))!=a['result_hash']):raise ValueError('BTC_FEE_SESSION_MISMATCH')
    receipt=bytes(read(ctx['fee'],n,'receipts',op));complete=receipt==keccak(data)
    if receipt!=bytes(32) and not complete:raise ValueError('BTC_FEE_RECEIPT_MISMATCH')
    if executable and not complete:
        if not hasattr(inspector,'journal'):raise ValueError('BTC_FEE_CONFIG_REQUIRED')
        if int(info[10]) not in (1,2) or int(info[10])==1 and int(info[7])!=c[3]:raise ValueError('BTC_FEE_SESSION_MISMATCH')
        validate(inspector,ctx,meta['method'],args,c,meta,bundle=bundle,state=state)
    inspector.stable(ctx['block'])
    return dict(context=ctx,authority=a,method=meta['method'],arguments=args,operation_hash=op,complete=complete,session=sid)


def guard_signing_session(client,session):
    if session.origin_contract==ZERO:return
    from .bitcoin_trust import checked_runtime
    anchor=getattr(client,'_read_snapshot',None) or client.w3.eth.get_block('latest')
    binding=getattr(client,'btc_fee_binding',None)
    if binding is not None and binding['target']==session.origin_contract:
        if bytes.fromhex(binding['operation_hash'].removeprefix('0x'))!=bytes(session.message_hash):raise ValueError('BTC_FEE_HASH_MISMATCH')
        for at in (anchor.number,'latest'):
            review_binding(client.w3,binding,at)
        client.check_read_snapshot();return
    try:checked_runtime(client.w3,session.origin_contract,'BitcoinFeeReserve',anchor.number)
    except ValueError as exc:
        if str(exc)=='BTC_CONTRACT_BUILD_MISMATCH':return
        raise
    payload=getattr(client,'btc_fee_context',None)
    if payload is None:raise ValueError('BTC_FEE_INTENT_REQUIRED')
    intent,bundle,inspector,state=payload
    if intent['operation_hash']!=session.message_hash.hex():raise ValueError('BTC_FEE_HASH_MISMATCH')
    for number in (anchor.number,'latest'):
        reviewed=review(client.w3,intent,bundle,number,inspector=inspector,session=session.session_id,state=state)
        if reviewed['complete']:raise ValueError('BTC_FEE_COMPLETE')
        if reviewed['authority']['coordinator'].address!=client.address:raise ValueError('BTC_FEE_AUTHORITY')
    negative=reviewed['method']=='publishReconciliation' and int(reviewed['arguments'][0][1]) in (2,3)
    client.continuity_context=(reviewed['context'],'READ' if negative else 'BASE_SIGN',session.session_id)
    client.check_read_snapshot()


def review_binding(w3,intent,number):
    args=intent.get('hash_args')
    if not isinstance(args,list) or len(args)!=1:raise ValueError('BTC_FEE_INTENT_REQUIRED')
    i=object.__new__(Inspector);i.w3=w3;i.c=dict(reserve=args[0],chain_id=str(w3.eth.chain_id),evm_confirmations='1')
    ctx=i.context(number,require_bound=False);n=ctx['block'].number
    if ctx['batch'].address!=intent['target'] or read(ctx['batch'],n,'feeReserve')!=ZERO or int(read(ctx['batch'],n,'nonce'))!=1:
        raise ValueError('BTC_FEE_GRAPH_MISMATCH')
    if bytes(read(ctx['batch'],n,'getSetFeeReserveOperationHash',args[0]))!=bytes.fromhex(intent['operation_hash'].removeprefix('0x')):
        raise ValueError('BTC_FEE_HASH_MISMATCH')
    return ctx
