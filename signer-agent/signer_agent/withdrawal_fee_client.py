"""Owner fee consent and resumable typed quorum requests on the shared EOA lane."""
from pathlib import Path
from eth_abi import encode
from eth_utils import keccak
from web3.logs import DISCARD
from .btc_fee_client import Driver as ReserveDriver
from .btc_fee_reserve import read,digest,ZERO
from .bootstrap import Bootstrap,artifact,encode_args
from .operations import canonical
from . import withdrawal_fee_policy as policy

def candidate_terms(scope,candidate):
    from eth_utils.abi import collapse_if_tuple
    spec=next(x for x in artifact('WithdrawalFeePayments')['abi'] if x.get('name')=='getPrepareOperationHash')['inputs'][0]
    return keccak(encode(['bytes32',collapse_if_tuple(spec)],[scope,candidate]))

def quote(inspector,batch,request):
    """Independently recompute the exact owner commitment; history is read-only."""
    ctx=inspector.context();fee=ctx['withdrawal_fees'];n=ctx['block'].number;p=fee['payments'];c=fee['credits']
    candidate=read(p,n,'candidate',batch);verified=read(p,n,'verification',batch)
    digest=candidate_terms(bytes(read(p,n,'scope')),candidate)
    if bytes(read(p,n,'termsHash',batch))!=digest or bytes(candidate[1])!=batch:raise ValueError('WITHDRAWAL_FEE_QUOTE_BINDING')
    rows=[row for row in candidate[6] if bytes(row[0])==request]
    if len(rows)!=1:raise ValueError('WITHDRAWAL_FEE_QUOTE_REQUEST')
    charge=rows[0];budget=read(c,n,'getBudget',request);primary=read(ctx['escrow'],n,'getRequest',request)
    mode=int(budget[3]);cost=int(charge[2]);closed=bool(budget[13]);admitted=bool(read(p,n,'bound',batch));winner=bytes(read(p,n,'winner',verified[0]))
    stage='SETTLED' if read(p,n,'settled',batch) else 'SUPERSEDED_BY_CONFIRMED_WINNER' if winner!=bytes(32) else 'VERSION_ADMITTED' if admitted else 'NO_PERSONAL_DEBIT' if mode==0 else 'EXACT_OWNER_CONSENT_REQUIRED'
    return dict(status=stage,winner=winner.hex(),
        batch=batch.hex(),request=request.hex(),owner=primary[3],principal_sats=str(int(primary[4])//10**10),
        mode=('NONE','BTC','MP','S')[mode],revision=str(charge[1]),terms_hash=digest.hex(),plan_hash=bytes(candidate[3]).hex(),
        actual_fee_sats=str(verified[3]),owner_cost_sats=str(cost),treasury_fee_sats=str(verified[5]),current_cap_sats=str(budget[6]),current_budget_revision=str(budget[4]),
        token_fee_debit_units=str(cost*10**10) if mode in (2,3) else '0',
        expected_token_return_units=str(max(0,int(budget[5])-cost*10**10)) if mode in (2,3) and not closed else None,
        refund_script=bytes(budget[12]).hex(),custody_generation=str(candidate[0][1]),derivation_generation=str(verified[7]),
        deadline=str(candidate[0][3]),unsigned_transaction=bytes(candidate[4]).hex(),allocation='EXACT_OWNER_PER_VERSION_V1',ready=False)

class Driver(ReserveDriver):
    def build_payment(self,plan,metadata):
        from types import SimpleNamespace
        from .dkg_protocol import Manifest
        from .bitcoin_onchain_payment import _review_wallet
        from .bitcoin_trust import inspect_registry,graph_options
        from .bitcoin_input_keys import prepare as keys_prepare,build
        from .bitcoin_onchain_plan import onchain_plan_hash
        ctx=self.i.context();r=SimpleNamespace(w3=self.i.w3)
        manifest,key=_review_wallet(Manifest.from_dict(self.bundle),r,ctx['board'],plan,ctx['block'],validate=False)
        options=dict(version='2',bitcoin_network=self.i.c['bitcoin_network'],evm_confirmations=self.i.c['evm_confirmations'])
        if self.i.c['bitcoin_policy']:options['bitcoin_policy']=self.i.c['bitcoin_policy']
        graph=inspect_registry(manifest,key,r,ctx['batch'].address,address_signing=True,**graph_options(options))['graph']
        plan=dict(plan,version='4',withdrawal_fee=metadata)
        keys_prepare(manifest,key,r,graph,plan,ctx['block'].number);built=build(plan)
        fee=ctx['withdrawal_fees'];n=ctx['block'].number
        control=Bootstrap.wire([read(fee['payments'],n,'nonce'),read(ctx['registry'],n,'custodyGeneration'),read(ctx['registry'],n,'derivationGeneration'),int(plan['deadline'])])
        candidate=[control,'0x'+plan['batch_id'],'0x'+metadata['previous'],'0x'+onchain_plan_hash(manifest,plan).hex(),
            '0x'+built['unsigned'],['0x'+x for x in plan['withdrawal_refs']],metadata['charges'],metadata['credits'],'0x'+metadata['treasury']]
        result=self.prepare('prepare',[candidate],plan)
        fn=next(x for x in artifact('WithdrawalFeePayments')['abi'] if x.get('name')=='getPrepareOperationHash')
        typed=encode_args(fn['inputs'],[candidate])[0]
        result.update(plan=plan,candidate=candidate,terms_hash=candidate_terms(bytes(read(fee['payments'],n,'scope')),typed).hex())
        return result
    def prepare(self,method,wire,bitcoin_plan=None):
        if method=='register' and bitcoin_plan is not None:raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
        ctx=self.i.context();a=self.authority(ctx);target,kind,helper,args,c,op=policy.operation(ctx,method,wire,self.i.w3)
        meta=dict(version='1',pool=ctx['pool'].address,registration_board=a['board'].address,reserve=ctx['fee'].address,
            method=method,args=wire,bitcoin_plan=bitcoin_plan)
        policy.validate(self.i,ctx,method,args,c,meta,bundle=self.bundle)
        request=dict(version='1',target=target.address,metadata=meta,operation_hash=op.hex())
        self.i.journal.put('withdrawal-fee-request:'+op.hex(),request)
        return dict(status='WITHDRAWAL_FEE_PREPARED',request=request,ready=False)
    def _intent(self,request,session,ctx,a):
        m=request['metadata']
        if set(m)!={'version','pool','registration_board','reserve','method','args','bitcoin_plan'} or m['version']!='1':raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
        if (m['pool']!=ctx['pool'].address or m['reserve']!=ctx['fee'].address or m['registration_board']!=a['board'].address):raise ValueError('WITHDRAWAL_FEE_GRAPH')
        target,kind,helper,args,c,op=policy.operation(ctx,m['method'],m['args'],self.i.w3)
        if request['version']!='1' or request['target']!=target.address or request['operation_hash']!=op.hex():raise ValueError('WITHDRAWAL_FEE_HASH_MISMATCH')
        result=dict(chain_id=str(self.i.w3.eth.chain_id),coordinator=a['coordinator'].address,role='conf_ceremony',target=target.address,
            artifact=kind,hash_method=helper,hash_args=Bootstrap.wire(args),session=str(session),operation_hash=op.hex(),withdrawal_fee_request=m)
        if len(canonical(result).encode())>policy.LIMIT:raise ValueError('WITHDRAWAL_FEE_INTENT_LIMIT')
        return result
    def propose(self,request):
        if not isinstance(request,dict) or set(request)!={'version','target','metadata','operation_hash'}:raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
        with self.lock():
            ctx=self.i.context();a=self.authority(ctx);self._intent(request,1,ctx,a);m=request['metadata'];op=request['operation_hash']
            from .spv_automation import identity
            sender=identity(self.i.c['identity_file'])
            if sender!=read(a['board'],ctx['block'].number,'operator') and not read(a['board'],ctx['block'].number,'canPublishIntent',sender):raise ValueError('WITHDRAWAL_FEE_OPERATOR_REQUIRED')
            key='withdrawal-fee-request:'+op;self.i.journal.put(key,request);saved=self.i.journal.get(key)['result']
            def check():
                latest=self.i.context();target,kind,helper,args,c,h=policy.operation(latest,m['method'],m['args'],self.i.w3)
                if h.hex()!=op:raise ValueError('WITHDRAWAL_FEE_HASH_MISMATCH')
                policy.validate(self.i,latest,m['method'],args,c,m,bundle=self.bundle)
            if not saved:
                target,kind,helper,args,c,h=policy.operation(ctx,m['method'],m['args'],self.i.w3)
                receipt=self.send(op+':personal-session','FROSTCoordinator',a['coordinator'].address,'createSessionFromDKG',[a['dkg_id'],h,target.address,c[3]],check)
                events=a['coordinator'].events.SessionCreated().process_receipt(receipt,errors=DISCARD)
                if len(events)!=1:raise ValueError('WITHDRAWAL_FEE_RECEIPT_MISMATCH')
                public=self._intent(request,int(events[0]['args']['sessionId']),ctx,a)
                saved=dict(intent=public,anchor=dict(number=str(receipt['blockNumber']),hash=receipt['blockHash'].hex()))
                self.i.journal.update(key,'SESSION_CREATED',saved)
            if self.i.w3.eth.get_block(int(saved['anchor']['number'])).hash.hex()!=saved['anchor']['hash']:raise ValueError('WITHDRAWAL_FEE_SESSION_REORG')
            public=saved['intent'];sid=int(public['session']);raw=canonical(public).encode()
            reviewed=policy.review(self.i.w3,public,self.bundle,'latest',inspector=self.i,published=False)
            if reviewed['complete']:return dict(status='WITHDRAWAL_FEE_EXECUTED',operation_hash=op,ready=False)
            prior=bytes(read(a['board'],'latest','intents',sid))
            if prior and prior!=raw:raise ValueError('WITHDRAWAL_FEE_PUBLIC_INTENT_REQUIRED')
            if not prior:
                from .withdrawal_fee_transport import publish_intent
                publish_intent(a['board'],sid,raw,lambda method,args,part:self.send(op+':personal-'+part,'DKGOnchainBoard',a['board'].address,method,args,check))
            return dict(status='AWAITING_PARTICIPANTS',intent=public,ready=False)
    def execute(self,intent):
        with self.lock():
            r=policy.review(self.i.w3,intent,self.bundle,'latest',inspector=self.i,executable=False)
            if r['complete']:return dict(status='WITHDRAWAL_FEE_EXECUTED',operation_hash=intent['operation_hash'],ready=False)
            def check():return policy.review(self.i.w3,intent,self.bundle,'latest',inspector=self.i)
            r=check();a=r['authority'];op=r['operation_hash'];target=r['target'];sid=r['session'];key=op.hex()
            if not read(a['authorization'],'latest','isOperationApproved',op,target.address):
                result=read(a['coordinator'],'latest','getAuthorizationResult',sid)
                if bytes(result[1])!=op or result[4]!=target.address:raise ValueError('WITHDRAWAL_FEE_SESSION_MISMATCH')
                self.send(key+':personal-authorize','DKGAuthorization',a['authorization'].address,'authorizeOperation',[sid,op,target.address],check)
            self.send(key+':personal-apply',r['kind'],target.address,r['method'],[*r['arguments'],op],check)
            if not policy.review(self.i.w3,intent,self.bundle,'latest',inspector=self.i,executable=False)['complete']:raise ValueError('WITHDRAWAL_FEE_RECEIPT_MISMATCH')
            return dict(status='WITHDRAWAL_FEE_EXECUTED',operation_hash=key,ready=False)

class Owner(ReserveDriver):
    """Uses the configured owner's EOA, never the operator's allowance as consent."""
    def __init__(self,inspector):self.i=inspector;self.bundle=None
    def call(self,method,wire):
        allowed={'consent','approveVersion','refundTokens','createRefund','raiseRefundCap','cancelRefund','consentFee'}
        if method not in allowed:raise ValueError('WITHDRAWAL_FEE_OWNER_ACTION')
        ctx=self.i.context();fee=ctx['withdrawal_fees']
        if fee is None:raise ValueError('WITHDRAWAL_FEE_GRAPH')
        from .spv_automation import identity
        owner=identity(self.i.c['identity_file']);target=fee['credits'];kind='WithdrawalFeeCredits'
        if method=='consentFee':
            target=self.i.w3.eth.contract(address=wire[0][3],abi=artifact('SimpleMultiPool')['abi']);kind='SimpleMultiPool'
            from .callback_admission import review_callback_configuration
            from .bitcoin_trust import checked_runtime
            admission,auth,origins=review_callback_configuration(ctx['escrow'],read(ctx['escrow'],'latest','callbackPoolRegistry'),ctx['block'].number)
            checked_runtime(self.i.w3,target.address,'SimpleMultiPool',ctx['block'].number)
            if not read(admission,ctx['block'].number,'isSimplePool',target.address):raise ValueError('WITHDRAWAL_FEE_OWNING_POOL')
        fn=next(x for x in artifact(kind)['abi'] if x.get('name')==method);args=encode_args(fn['inputs'],wire)
        request=args[0][0] if method in ('consent','consentFee') else args[1] if method in ('approveVersion','createRefund') else args[0]
        primary=read(ctx['escrow'],'latest','getRequest',request);b=read(fee['credits'],'latest','getBudget',request)
        if primary[3]!=owner:raise ValueError('WITHDRAWAL_FEE_OWNER_REQUIRED')
        exact_quote=None
        if method=='approveVersion':
            exact_quote=quote(self.i,args[0],args[1])
            if exact_quote['terms_hash']!=bytes(args[3]).hex() or exact_quote['revision']!=str(args[2]):raise ValueError('WITHDRAWAL_FEE_QUOTE_BINDING')
        def check():
            latest=self.i.context()
            if method not in ('refundTokens','cancelRefund'):self.i.fence(latest)
            if latest['withdrawal_fees']['credits'].address!=fee['credits'].address:raise ValueError('WITHDRAWAL_FEE_GRAPH')
            if method=='approveVersion':
                current=quote(self.i,args[0],args[1])
                if current['terms_hash']!=bytes(args[3]).hex() or current['revision']!=str(args[2]):raise ValueError('WITHDRAWAL_FEE_QUOTE_BINDING')
                if current['status']=='EXACT_OWNER_CONSENT_REQUIRED':
                    node=latest['withdrawal_fees'];candidate=read(node['payments'],'latest','candidate',args[0])
                    if int(candidate[0][1])!=int(read(latest['registry'],'latest','custodyGeneration')) or int(candidate[0][3])<=latest['block'].timestamp:
                        raise ValueError('WITHDRAWAL_FEE_GENERATION_OR_WINDOW')
                    dg=int(current['derivation_generation'])
                    if dg and (dg!=int(read(latest['registry'],'latest','derivationGeneration')) or read(latest['registry'],'latest','derivationState')!=0):
                        raise ValueError('WITHDRAWAL_FEE_GENERATION_OR_WINDOW')
                    read(node['plan_verifier'],'latest','verify',node['payments'].address,candidate)
            # Real eth_call enforces revisions, generations, exact tranche,
            # ownership and signature-family cancellation boundaries.
            getattr(target.functions,method)(*args).call({'from':owner},block_identifier='latest')
        key=digest([fee['scope'],owner,method,Bootstrap.wire(args)])
        with self.lock():
            if method=='consent':
                c=args[0]
                if c[2]==2:
                    extra=(int(c[4])-int(b[6]))*10**10
                    if extra<0:raise ValueError('WITHDRAWAL_FEE_CAP_MONOTONIC')
                    token=self.i.w3.eth.contract(address=primary[2],abi=artifact('PoolMpToken')['abi'])
                    if int(read(token,'latest','allowance',owner,fee['credits'].address))<extra:
                        def allowance_guard():
                            latest=self.i.context();self.i.fence(latest)
                            now=read(fee['credits'],'latest','getBudget',request)
                            if int(now[4])+1!=int(c[1]) or read(ctx['escrow'],'latest','getRequest',request)[3]!=owner:raise ValueError('WITHDRAWAL_FEE_STALE_CONSENT')
                        self.send(key+':exact-allowance','PoolMpToken',token.address,'approve',[fee['credits'].address,extra],allowance_guard)
            # S ownership/allowance escrow is performed by the genuine pool's
            # owner entry point; the client cannot grant synthetic S consent.
            receipt=self.send(key+':owner',kind,target.address,method,args,check)
        result=dict(status='WITHDRAWAL_FEE_OWNER_CONFIRMED',request=bytes(request).hex(),action=method,ready=False)
        if exact_quote is not None:result['quote']=exact_quote
        if method=='createRefund':
            events=fee['credits'].events.Consented().process_receipt(receipt,errors=DISCARD)
            if len(events)!=1:raise ValueError('WITHDRAWAL_FEE_RECEIPT_MISMATCH')
            result['refund_request']=bytes(events[0]['args']['request']).hex()
        return result

def fee_intent(ctx,request,service_policy_hash):
    from .deposit_intent import DepositIntentV1
    from .withdrawal_fee_intent import WithdrawalFeeIntentV1
    fee=ctx['withdrawal_fees'];r=ctx['registry'];n=ctx['block'].number;b=read(fee['credits'],n,'getBudget',request)
    if int(b[3])!=1 or b[13]:raise ValueError('WITHDRAWAL_FEE_BTC_CONSENT_REQUIRED')
    addr=lambda a:bytes.fromhex(a[2:])
    parent=DepositIntentV1(bytes(read(r,n,'bitcoinGenesis')),r.w3.eth.chain_id,addr(r.address),addr(ctx['pool'].address),addr(b[1]),
        bytes(read(r,n,'graphId')),addr(read(r,n,'dkg')),bytes(read(r,n,'walletId')),bytes(read(r,n,'walletResult')),int(read(r,n,'keyEpoch')),
        addr(b[0]),addr(b[0]),keccak(encode(['bytes32','bytes32'],[bytes(read(fee['funding'],n,'scope')),request])),service_policy_hash)
    return WithdrawalFeeIntentV1(parent,request,keccak(bytes(b[12])),bytes.fromhex(fee['scope']))

def bootstrap_plan(registry,payments,funding,credits):
    """Explicit predicted addresses are verified again at deployment/binding."""
    from web3 import Web3
    if any(not Web3.is_checksum_address(a) or a==ZERO for a in (registry,payments,funding,credits)):raise ValueError('WITHDRAWAL_FEE_GRAPH')
    return dict(schema='satoshifi.withdrawal-fee-bootstrap.v1',steps=[
        dict(id='withdrawal_plan_verifier',kind='deploy',artifact='WithdrawalFeePlanVerifier',args=[]),
        dict(id='withdrawal_derivation',kind='deploy',artifact='BitcoinWithdrawalFeeDerivation',args=[]),
        dict(id='withdrawal_payments',kind='deploy',artifact='WithdrawalFeePayments',expected_address=payments,args=[registry,credits,'$withdrawal_plan_verifier']),
        dict(id='withdrawal_funding',kind='deploy',artifact='WithdrawalFeeFunding',expected_address=funding,args=[registry,credits,'$withdrawal_derivation']),
        dict(id='withdrawal_credits',kind='deploy',artifact='WithdrawalFeeCredits',expected_address=credits,args=[registry,payments,funding])],
        binding=dict(artifact='BitcoinWithdrawalEscrow',method='setWithdrawalFees',hash_method='getSetWithdrawalFeesOperationHash',args=[credits],
            requires='Existing enrolled FROST; after reserve binding, before first BTC batch'),ready=False)
