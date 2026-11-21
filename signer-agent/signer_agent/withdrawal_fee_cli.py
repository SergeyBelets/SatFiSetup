"""Explicit personal fee actions. JSON contains only PUBLIC arguments/plans."""
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .btc_fee_reserve import Inspector,load_config,read,number,ZERO
from .withdrawal_fee_client import Driver,Owner,fee_intent,bootstrap_plan,quote
from .withdrawal_fee_funding import Funding
from .bootstrap import Bootstrap,artifact

def preview(i,mode,total,cap,*,pool=None,nft=None):
    total=number(total,1,2**256-1);cap=number(cap,0,2100000000000000);q=10**10
    fee_units=cap*q if mode in ('MP','S') else 0
    if total<=fee_units:raise ValueError('WITHDRAWAL_FEE_TOTAL_TOO_SMALL')
    main=total-fee_units;service=0;ctx=i.context();n=ctx['block'].number
    if mode=='S':
        if not pool:raise ValueError('WITHDRAWAL_FEE_OWNING_POOL')
        s=i.w3.eth.contract(address=pool,abi=artifact('SimpleMultiPool')['abi']);manager=read(s,n,'feeManager')
        from .btc_fee_reserve import ZERO
        if manager!=ZERO:
            f=i.w3.eth.contract(address=manager,abi=artifact('FederationFeeManager')['abi'])
            if nft is None:service=int(read(f,n,'calculateFee',False,main))
            else:
                from .spv_automation import identity
                service=int(read(f,n,'quoteFeeWithNFT',False,main,number(nft),identity(i.c['identity_file'])))
    principal=(main-service)//q;rounding=main-service-principal*q
    return dict(status='WITHDRAWAL_FEE_PREVIEW',mode=mode,total_units=str(total),fee_cap_sats=str(cap),fee_tranche_units=str(fee_units),
        main_tranche_units=str(main),ordinary_service_fee_units=str(service),rounding_units=str(rounding),principal_sats=str(principal),
        principal_guaranteed_after_request=True,refund_asset=mode,quote='EXACT_OWNER_PER_VERSION_V1',ready=False)

def status(i,request):
    ctx=i.context();fee=ctx['withdrawal_fees'];n=ctx['block'].number;c=fee['credits'];b=read(c,n,'getBudget',request)
    origin=bytes(read(c,n,'refundOrigin',request));source=origin if origin!=bytes(32) else request
    root=bytes(read(fee['payments'],n,'rootForRequest',request));r=read(ctx['escrow'],n,'getRequest',request)
    if r[3]==ZERO:raise ValueError('WITHDRAWAL_FEE_REQUEST')
    balance=0;available=0;locations=set();claims=0;count=int(read(c,n,'claimCount'));i._bounded(count)
    for index in range(count):
        identifier,claim=read(c,n,'claimAt',index)
        if bytes(claim[0])!=source:continue
        balance+=int(claim[2]);lock=bytes(claim[3])
        if lock==bytes(32) or lock==root or int(read(ctx['batch'],n,'batchState',lock))==3:
            available+=int(claim[2]);locations.add(bytes(claim[1]));claims+=1
    mode=int(b[3]);observations=[];observation='UNKNOWN';required=int(b[6])+(int(r[4])//10**10 if origin!=bytes(32) else 0)
    if mode==1 and locations and len(locations)<=16 and claims<=32:
        observation='MATURE'
        for location in sorted(locations):
            wire,vout=read(ctx['common'],n,'valueLocation',location);display=bytes(wire)[::-1].hex()
            row,pending=i.observed(ctx,display,int(vout))
            if row is None:
                state='SETTLEMENT_REQUIRED' if pending else 'UNKNOWN'
                if state=='UNKNOWN' or observation!='UNKNOWN':observation=state
                observations.append(dict(outpoint=display+':'+str(vout),status=state));continue
            depth=max(101 if row['coinbase'] else 6,int(read(ctx['common'],n,'depth')))
            mature=int(row['confirmations'])>=depth
            if not mature and observation=='MATURE':observation='IMMATURE'
            observations.append(dict(outpoint=display+':'+str(vout),status='MATURE' if mature else 'IMMATURE',
                confirmations=str(row['confirmations']),required=str(depth)))
    if mode==0:stage='CONSENT_REQUIRED'
    elif b[13]:stage='CREDIT_REFUND_AVAILABLE' if mode==1 and available else 'CLOSED'
    elif root!=bytes(32) and int(read(ctx['batch'],n,'batchState',root)) in (1,2):stage='VERSION_ADMITTED'
    elif mode==1:
        stage=('BTC_INPUT_CAPACITY_EXCEEDED' if len(locations)>16 or claims>32 else 'BTC_BUDGET_INSUFFICIENT' if available<required
            else 'BTC_'+observation if observation!='MATURE' else 'INPUTS_AND_EXACT_VERSION_REQUIRED')
    else:stage='TOKEN_BUDGET_INSUFFICIENT' if int(b[5])<int(b[6])*10**10 else 'INPUTS_AND_EXACT_VERSION_REQUIRED'
    if stage=='CREDIT_REFUND_AVAILABLE' and observation!='MATURE':stage='CREDIT_REFUND_'+observation
    qualified=read(fee['verifier'],n,'FEE_QUALIFIED')
    return dict(status=stage,request=request.hex(),owner=b[0],mode=('NONE','BTC','MP','S')[mode],principal_sats=str(int(r[4])//10**10),revision=str(b[4]),
        cap_sats=str(b[6]),locked_token_units=str(b[5]),btc_credit_sats=str(balance),btc_available_sats=str(available),
        btc_input_locations=str(len(locations)),selected_claim_capacity=str(claims),btc_observation=observation,observations=observations,
        refund_cap_sats=str(b[8]),refund_committed_sats=str(read(c,n,'refundCommitted',source)),refund_spent_sats=str(read(c,n,'refundSpent',source)),
        root=root.hex(),deadline=str(b[9]),maximum_rate=str(b[7]),estimate_source='EXACT_PUBLIC_VERSION_REQUIRED',current_fee_estimate_sats=None,
        expected_minimum_return_sats=str(max(0,available-required)) if mode==1 and not b[13] else None,
        funding_qualified=qualified,blocked_by='FEE_PURPOSE_UNQUALIFIED' if mode==1 and not qualified else None,ready=False)

def main(argv=None):
    p=DiagnosticArgumentParser(prog='frost-signer bitcoin withdrawal-fee')
    p.add_argument('action',choices=['bootstrap-plan','status','quote','preview','owner','build','prepare','propose','execute','funding-intent','import-watch','scan','address','credit','backup','release-cancelled'])
    p.add_argument('--config');p.add_argument('--bundle');p.add_argument('--file');p.add_argument('--method');p.add_argument('--bitcoin-plan')
    p.add_argument('--launch-config',type=Path,help='use the Bitcoin-watch start height for scan')
    p.add_argument('--request');p.add_argument('--deadline');p.add_argument('--watch');p.add_argument('--destination');p.add_argument('--txid');p.add_argument('--vout')
    p.add_argument('--batch')
    p.add_argument('--max-blocks',default='16');p.add_argument('--max-inputs',default='16');p.add_argument('--mode',choices=['BTC','MP','S']);p.add_argument('--total');p.add_argument('--cap');p.add_argument('--pool');p.add_argument('--nft')
    p.add_argument('--registry');p.add_argument('--payments');p.add_argument('--funding');p.add_argument('--credits');p.add_argument('--service-policy-hash')
    p.add_argument('--spv-worker-config');p.add_argument('--spv-transport-config');p.add_argument('--federation');p.add_argument('--service')
    a=p.parse_args(argv)
    if a.launch_config is not None and a.action!='scan':p.error('--launch-config requires scan')
    if a.action=='bootstrap-plan':result=bootstrap_plan(a.registry,a.payments,a.funding,a.credits)
    else:
        if not a.config:raise ValueError('WITHDRAWAL_FEE_CONFIG_REQUIRED')
        config=load_config(a.config)
        from .launch_configurator import watch_start_height
        start_height=watch_start_height(a.launch_config,config)
        i=Inspector(config);ctx=i.context();fee=ctx['withdrawal_fees']
        if fee is None:raise ValueError('WITHDRAWAL_FEE_GRAPH')
        request=bytes.fromhex(a.request.removeprefix('0x')) if a.request else None
        if request is not None and len(request)!=32:raise ValueError('WITHDRAWAL_FEE_REQUEST')
        if a.action=='status':result=status(i,request)
        elif a.action=='quote':result=quote(i,bytes.fromhex(a.batch.removeprefix('0x')),request)
        elif a.action=='preview':result=preview(i,a.mode,a.total,a.cap,pool=a.pool,nft=a.nft)
        elif a.action=='funding-intent':
            intent=fee_intent(ctx,request,bytes.fromhex(a.service_policy_hash.removeprefix('0x')))
            result=dict(status='WITHDRAWAL_FEE_REGISTRATION_REQUIRED',request=request.hex(),raw='0x'+intent.encode().hex(),intent_id=intent.intent_id.hex(),ready=False)
        elif a.action in ('import-watch','scan','address','credit','backup'):
            f=Funding(i)
            if a.action=='import-watch':result=f.import_bindings(a.watch)
            elif a.action=='scan':
                from .address_watch import trusted_remote_filters
                result=f.store(a.watch).scan(i.bitcoin,max_blocks=number(a.max_blocks,1,256),start_height=start_height,
                    allow_remote_filters=trusted_remote_filters(config,i.bitcoin))
            elif a.action=='backup':result=f.store(a.watch).backup(a.destination)
            elif a.action=='address':result=f.address(request,a.watch)
            else:
                result=f.credit(a.txid,number(a.vout,0,2**32-1),max_inputs=number(a.max_inputs,1,16))
                if result['status']=='SPV_REQUIRED' and (a.spv_worker_config or a.spv_transport_config):
                    result['spv']=f.request_spv(result['need'],worker_config=a.spv_worker_config,transport_config=a.spv_transport_config,federation=a.federation,service=a.service)
        elif a.action=='release-cancelled':
            root=request
            def check():
                latest=i.context()
                if read(latest['batch'],'latest','batchState',root)!=3:raise ValueError('WITHDRAWAL_FEE_SIGNATURE_MAY_EXIST')
            Owner(i).send(root.hex()+':release-personal','BitcoinFeeReserve',ctx['fee'].address,'releaseUserFamily',[root],check)
            result=dict(status='WITHDRAWAL_FEE_CANCELLED_FAMILY_RELEASED',root=root.hex(),ready=False)
        else:
            if not a.file or Path(a.file).stat().st_size>131072:raise ValueError('WITHDRAWAL_FEE_INTENT_LIMIT')
            body=json.loads(Path(a.file).read_text())
            if a.action=='owner':result=Owner(i).call(a.method,body)
            else:
                if not a.bundle:raise ValueError('WITHDRAWAL_FEE_ENROLLMENT_REQUIRED')
                d=Driver(i,json.loads(Path(a.bundle).read_text()))
                if a.action=='build':
                    if not isinstance(body,dict) or set(body)!={'plan','withdrawal_fee'}:raise ValueError('WITHDRAWAL_FEE_PLAN_REQUIRED')
                    result=d.build_payment(body['plan'],body['withdrawal_fee'])
                elif a.action=='prepare':
                    if a.method not in ('prepare','register') or not a.deadline or not isinstance(body,list):raise ValueError('WITHDRAWAL_FEE_INTENT_REQUIRED')
                    target=fee['payments' if a.method=='prepare' else 'funding'];n=ctx['block'].number;r=ctx['registry']
                    control=Bootstrap.wire([read(target,n,'nonce'),read(r,n,'custodyGeneration'),read(r,n,'derivationGeneration'),number(a.deadline,1)])
                    wire=[[control,*body]] if a.method=='prepare' else [*body,control]
                    plan=json.loads(Path(a.bitcoin_plan).read_text()) if a.bitcoin_plan else None
                    result=d.prepare(a.method,wire,plan)
                elif a.action=='propose':result=d.propose(body.get('request',body))
                else:result=d.execute(body.get('intent',body))
    print(json.dumps(result,sort_keys=True));return 0
