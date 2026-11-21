"""Typed issuance-status policy. Private evidence approval never lowers EVM_AUTH."""
from eth_abi import encode
from eth_utils import keccak
from eth_utils.abi import function_abi_to_4byte_selector
from .bootstrap import artifact
from .operations import canonical
from .spv_transport import hex32,uint
from .address_registration import read,authority,ZERO
from .incident_transport import public_report,validate_report,delivery_proof

CHANGE_TYPE='(uint8,uint8,uint256,uint256,uint8,bool,uint64,(bytes32,bytes32,bytes32,bytes32,bytes32,bytes32))'
STATES=(('ACTIVE','DRAINING','SUSPENDED','COMPROMISED'),('QUALIFIED','SUSPENDED','REVOKED'))


def snapshot(ctx,number,scope):
    r=ctx['registry'];prefix='custody' if scope==0 else 'derivation'
    return dict(state=int(read(r,number,prefix+'State')),generation=int(read(r,number,prefix+'Generation')),
        sequence=int(read(r,number,prefix+'StatusNonce')),issuance_closed=bool(read(r,number,'issuanceClosed')))


def parse_change(raw):
    try:
        if not isinstance(raw,list) or len(raw)!=8 or type(raw[5]) is not bool or not isinstance(raw[7],list) or len(raw[7])!=6:
            raise ValueError('INCIDENT_STATUS_INTENT')
        c=[uint(raw[0],0,1),uint(raw[1],0,3),uint(raw[2],1,2**256-2),uint(raw[3],0,2**256-2),
            uint(raw[4],0,3),raw[5],uint(raw[6],1,2**64-1),[hex32(v[2:]) if isinstance(v,str) and v.startswith('0x') else hex32(None) for v in raw[7]]]
        if c[1]>=len(STATES[c[0]]) or c[4]>=len(STATES[c[0]]):raise ValueError('INCIDENT_STATUS_INTENT')
        return c
    except (ValueError,TypeError,KeyError):raise ValueError('INCIDENT_STATUS_INTENT') from None


def operation_hash(w3,ctx,auth,c,number):
    scope=bytes(read(ctx['registry'],number,'custodyScope' if c[0]==0 else 'derivationScope'))
    params=keccak(encode(['bytes32','bytes32',CHANGE_TYPE],[keccak(text='SATOHSI_ISSUANCE_STATUS_V1'),scope,c]))
    method=next(x for x in artifact('BitcoinAddressRegistry')['abi'] if x.get('name')=='setStatus')
    return keccak(encode(['string','uint256','address','address','bytes4','bytes32','uint256'],[
        'SATOHSI_FROST_AUTHORIZATION_V1',w3.eth.chain_id,auth.address,ctx['registry'].address,
        function_abi_to_4byte_selector(method),params,c[3]]))


def pending(w3,ctx,c,number):
    current=snapshot(ctx,number,c[0])
    if (current['state']!=c[1] or current['generation']!=c[2] or current['sequence']!=c[3]):raise ValueError('INCIDENT_GENERATION_CHANGED')
    if w3.eth.get_block(number).timestamp>c[6]:raise ValueError('INCIDENT_STATUS_EXPIRED')
    if (c[1]==len(STATES[c[0]])-1 or c[0]==1 and c[5]
            or c[0]==0 and c[4]==0 and (current['issuance_closed'] or c[5])):raise ValueError('INCIDENT_STATUS_TERMINAL')
    if c[0]==1 and c[4]==0:
        from .bitcoin_trust import checked_runtime
        from .contract_provenance import prove_creation
        r=ctx['registry'];address=read(r,number,'verifier')
        checked_runtime(w3,address,'BitcoinDepositDerivation',number)
        prove_creation(w3,address,'BitcoinDepositDerivation',[],number)
        v=w3.eth.contract(address=address,abi=artifact('BitcoinDepositDerivation')['abi'])
        if (keccak(bytes(w3.eth.get_code(address,block_identifier=number)))!=bytes(read(r,number,'verifierRuntimeHash'))
                or read(v,number,'QUALIFIED') is not True):raise ValueError('INCIDENT_DERIVATION_UNQUALIFIED')
    return current


def review(w3,intent,bundle,number,*,session=None,executable=True,published=True):
    from .address_mint import configuration
    expected={'chain_id','coordinator','role','target','artifact','hash_method','hash_args','session','operation_hash','incident_status'}
    if not isinstance(intent,dict) or set(intent)!=expected:raise ValueError('INCIDENT_STATUS_INTENT')
    m=intent['incident_status']
    if (not isinstance(m,dict) or set(m)!={'version','pool','registration_board','incident_board','report_id','report','delivery'}
            or m['version']!='1' or not isinstance(m['delivery'],dict) or set(m['delivery']) not in ({'message_id'},{'message_id','attempt'})
            or not isinstance(intent['hash_args'],list) or len(intent['hash_args'])!=1):raise ValueError('INCIDENT_STATUS_INTENT')
    c=parse_change(intent['hash_args'][0]);block=w3.eth.get_block(number)
    pool=w3.eth.contract(address=m['pool'],abi=artifact('MiningPoolV1')['abi'])
    ctx=configuration(w3,pool,block.number,verify_derivation=False);r=ctx['registry']
    a=authority(w3,ctx,m['registration_board'],block.number,bundle)
    identifier=validate_report(m['report'],w3,ctx,block.number,private=False)
    p=m['report'];report=[bytes.fromhex(p[k]) for k in ('incident_id','reason','evidence_hash','bitcoin_block','bitcoin_tx','evm_anchor')]
    digest=operation_hash(w3,ctx,a['authorization'],c,block.number)
    if (m['report_id']!=identifier or c[0]!=int(p['scope']) or c[7]!=report
            or intent['chain_id']!=str(w3.eth.chain_id) or intent['role']!='conf_ceremony'
            or intent['artifact']!='BitcoinAddressRegistry' or intent['hash_method']!='getStatusOperationHash'
            or intent['target']!=r.address or intent['coordinator']!=a['coordinator'].address
            or intent['operation_hash']!=digest.hex()
            or bytes(read(r,block.number,'getStatusOperationHash',c))!=digest):raise ValueError('INCIDENT_STATUS_HASH')
    sid=uint(intent['session'],1,2**256-1)
    if session is not None and int(session)!=sid:raise ValueError('INCIDENT_STATUS_SESSION')
    info=read(a['coordinator'],block.number,'getSession',sid)
    from .onchain_transport import can_publish_intent
    if (int(info[0])!=sid or not can_publish_intent(a['board'],info[1],block.number) or bytes(info[3])!=digest
            or not info[4] or not info[8] or info[9]!=ZERO or info[15]!=r.address
            or bytes(read(a['coordinator'],block.number,'sessionDKGId',sid))!=a['dkg_id']
            or bytes(read(a['coordinator'],block.number,'sessionDKGResult',sid))!=a['result_hash']):raise ValueError('INCIDENT_STATUS_SESSION')
    receipt=int(read(r,block.number,'statusReceipts',digest));complete=receipt!=0
    if published and bytes(read(a['board'],block.number,'intents',sid))!=canonical(intent).encode():raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED')
    if complete:
        if receipt!=c[2]+1:raise ValueError('INCIDENT_STATUS_RECEIPT')
    else:
        delivery_proof(w3,ctx,m['incident_board'],p,m['delivery']['message_id'],block.number,attempt=m['delivery'].get('attempt','0'))
        if executable:
            if int(info[10]) not in (1,2) or int(info[10])==1 and int(info[7])!=c[6]:raise ValueError('INCIDENT_STATUS_SESSION')
            pending(w3,ctx,c,block.number)
    if w3.eth.get_block(block.number).hash!=block.hash:raise ValueError('INCIDENT_EVM_REORG')
    return dict(context=ctx,authority=a,arguments=c,operation_hash=digest,session=sid,state=int(info[10]),
        complete=complete,generation=receipt,anchor=block,current=snapshot(ctx,block.number,c[0]))


def resume_service(client,ctx,c,number):
    """Revalidate the bounded existing watch/reserve; never clear a signing pause."""
    resume=c[1]==(2 if c[0]==0 else 1) and c[4] in ((0,1) if c[0]==0 else (0,))
    if not resume:return dict(required=False)
    filename=client.c.get('recovery_service_config')
    if not filename:raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
    from .address_transport import Client,load_config
    from .bitcoin_rpc import BitcoinRPC
    cfg=load_config(filename)
    if any(cfg[k]!=client.c[k] for k in ('chain_id','registry','pool','bitcoin_network','bitcoin_policy','evm_confirmations')):
        raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
    service=Client(cfg,client.w3)
    if not cfg['bitcoin_rpc'] or not cfg['bitcoin_cookie']:raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
    core=BitcoinRPC(cfg['bitcoin_rpc'],cfg['bitcoin_cookie'],cfg['bitcoin_network'],cfg['bitcoin_policy']);core.check_network()
    if core.call('getblockhash',0)!=bytes(read(ctx['registry'],number,'bitcoinGenesis')).hex():raise ValueError('BTC_ADDRESS_GENESIS_MISMATCH')
    best=core.call('getbestblockhash');count=int(read(ctx['registry'],number,'intentCount'))
    if count>int(cfg['max_intents']):raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
    known={key for key,_,_ in service.store.intents()}
    for index in range(count):
        identifier=bytes(read(ctx['registry'],number,'intentIds',index)).hex()
        if identifier not in known or service.status(identifier,core)['watch']!='WATCH_COVERED':raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
    fee=read(ctx['batch'],number,'feeReserve');evidence=None
    if fee!=ZERO:
        if cfg['version']!='2':raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
        from .btc_fee_reserve import Inspector,load_config as fee_config
        inspector=Inspector(fee_config(cfg['fee_reserve_config']),client.w3,core)
        if inspector.c['reserve']!=fee:raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
        result=inspector.reconcile(full=False)
        if result['status']!='HEALTHY':raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
        evidence=result['evidence']
    if core.call('getbestblockhash')!=best:raise ValueError('INCIDENT_SERVICE_REVIEW_REQUIRED')
    return dict(required=True,bitcoin_tip=best,registered_watches=str(count),reconciliation=evidence)


def private_review(client,intent,bundle,number,*,session=None,approval=False):
    reviewed=review(client.w3,intent,bundle,number,session=session)
    if reviewed['complete']:
        from .incident_observations import observe
        observe(client,reviewed['context'],reviewed['arguments'][0],reviewed['anchor'].number,restrictive=True)
        return reviewed
    m=intent['incident_status'];ctx=reviewed['context'];r=client.report(m['report_id'])
    if (client.c['board']!=m['incident_board'] or client.c['registry']!=intent['target']
            or client.c['pool']!=m['pool'] or public_report(r)!=m['report']):raise ValueError('INCIDENT_PRIVATE_REPORT_REQUIRED')
    validate_report(r,client.w3,ctx,reviewed['anchor'].number)
    from .incident_observations import observe
    change=reviewed['arguments']
    restrictive=change[4] in ((2,3) if change[0]==0 else (1,2))
    reviewed['observations']=observe(client,ctx,change[0],reviewed['anchor'].number,restrictive=restrictive)
    service=resume_service(client,ctx,reviewed['arguments'],reviewed['anchor'].number)
    if approval:
        saved=client.store.get('incident-approval:'+intent['operation_hash'])
        if (not saved or saved['identity']!=client.account.address or saved['evidence_hash']!=r['evidence_hash']
                or client.w3.eth.get_block(int(saved['anchor']['number'])).hash.hex()!=saved['anchor']['hash']):raise ValueError('INCIDENT_LOCAL_APPROVAL_REQUIRED')
    reviewed['service']=service
    return reviewed


def guard_signing_session(coordinator,session,payload):
    intent,bundle,client=payload
    if coordinator.sender!=client.account.address:raise ValueError('INCIDENT_SIGNER_IDENTITY')
    anchor=getattr(coordinator,'_read_snapshot',None) or coordinator.w3.eth.get_block('latest')
    for number in (anchor.number,'latest'):
        r=private_review(client,intent,bundle,number,session=session.session_id,approval=True)
        if r['complete']:raise ValueError('INCIDENT_STATUS_COMPLETE')
        if r['authority']['coordinator'].address!=coordinator.address or r['operation_hash']!=session.message_hash:raise ValueError('INCIDENT_STATUS_SESSION')
    coordinator.check_read_snapshot()
