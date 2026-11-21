"""Fee-purpose SPV needs reuse state coverage/archive and explicit payer policy."""
import re
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
from .bootstrap import artifact
from .btc_fee_reserve import read,ZERO
KIND='withdrawal_fee'

def validate_profile(profile,allowed=None):
    if (not isinstance(profile,dict) or set(profile)!={'version','registries'} or profile['version']!='1'
            or not isinstance(profile['registries'],dict) or not 1<=len(profile['registries'])<=4096):raise ValueError('SPV_WITHDRAWAL_FEE_PROFILE')
    for pool,credits in profile['registries'].items():
        if (not Web3.is_checksum_address(pool) or not Web3.is_checksum_address(credits) or credits==ZERO
                or allowed is not None and allowed.get(pool)!='MiningPoolV1'):raise ValueError('SPV_WITHDRAWAL_FEE_PROFILE')

def scope(policy,need):
    if (not isinstance(need,dict) or set(need)!={'kind','consumer','intent_id','txid','vout'} or need['kind']!=KIND
            or any(not isinstance(need[k],str) or not re.fullmatch('[0-9a-f]{64}',need[k]) for k in ('intent_id','txid'))
            or not isinstance(need['vout'],str) or not re.fullmatch('0|[1-9][0-9]*',need['vout']) or int(need['vout'])>2**32-1):raise ValueError('SPV_WITHDRAWAL_FEE_NEED')
    profile=policy.get('withdrawal_fees');validate_profile(profile,policy.get('allowed_consumers'))
    pinned=profile['registries'].get(need['consumer'])
    if pinned is None:raise ValueError('SPV_WITHDRAWAL_FEE_CONSUMER')
    return pinned

def view(w3,policy,need,number):
    from .address_mint import configuration as address_context
    from .withdrawal_fee import configuration,funding_binding
    pinned=scope(policy,need);block=w3.eth.get_block(number)
    pool=w3.eth.contract(address=need['consumer'],abi=artifact('MiningPoolV1')['abi']);ac=address_context(w3,pool,block.number)
    r=ac['registry'];escrow=w3.eth.contract(address=read(r,block.number,'escrow'),abi=artifact('BitcoinWithdrawalEscrow')['abi'])
    ctx=dict(registry=r,common=ac['common'],batch=ac['batch'],escrow=escrow,pool=pool,block=block)
    ctx['withdrawal_fees']=configuration(w3,ctx,block.number);fee=ctx['withdrawal_fees']
    if fee is None or fee['credits'].address!=pinned:raise ValueError('SPV_WITHDRAWAL_FEE_GRAPH')
    intent,desc,provenance=funding_binding(ctx,bytes.fromhex(need['intent_id']),block.number)
    key=keccak(encode(['bytes32','bytes32','uint32'],[bytes.fromhex(fee['scope']),bytes.fromhex(need['txid'])[::-1],int(need['vout'])]))
    claim=read(fee['credits'],block.number,'getClaim',key);done=bytes(claim[0])!=bytes(32)
    if done and bytes(claim[0])!=intent.request:raise ValueError('SPV_WITHDRAWAL_FEE_RECEIPT')
    if w3.eth.get_block(block.number).hash!=block.hash:raise ValueError('SPV_WITHDRAWAL_FEE_REORG')
    return ctx,intent,desc,done

def inspect(worker,need,block):
    from .funding_envelope import inspect_funding
    ctx,intent,desc,done=view(worker.w3,worker.c,need,block.number)
    if done:return dict(status='COMPLETED')
    fee=ctx['withdrawal_fees'];r=ctx['registry'];n=block.number
    if (int(read(r,n,'custodyState')) not in (0,1) or int(read(r,n,'derivationState'))!=0
            or read(fee['verifier'],n,'FEE_QUALIFIED') is not True):raise ValueError('WITHDRAWAL_FEE_VERIFIER_UNQUALIFIED')
    raw=worker.bitcoin.call('getrawtransaction',need['txid'],True);parsed=inspect_funding(raw['hex'],need['txid']);index=int(need['vout'])
    if index>=len(parsed['outputs']) or parsed['outputs'][index]['script']!=desc['script_pubkey']:raise ValueError('SPV_WITHDRAWAL_FEE_BINDING')
    wire=bytes.fromhex(need['txid'])[::-1]
    if read(ctx['common'],n,'custodyTransactions',wire) or int(read(fee['funding'],n,'classified',wire))!=len(parsed['inputs']):
        raise ValueError('WITHDRAWAL_FEE_INGRESS_CLASSIFICATION_REQUIRED')
    utxo=worker.bitcoin.call('gettxout',need['txid'],index,True)
    if not utxo:return dict(status='WAITING_BITCOIN')
    from .bitcoin_rpc import satoshis
    if utxo['scriptPubKey']['hex']!=desc['script_pubkey'] or satoshis(utxo['value'])!=int(parsed['outputs'][index]['amount']):raise ValueError('SPV_WITHDRAWAL_FEE_BINDING')
    latest,_,_,done=view(worker.w3,worker.c,need,'latest')
    if done:return dict(status='COMPLETED')
    lr=latest['registry'];ln=latest['block'].number
    if (int(read(lr,ln,'custodyState')) not in (0,1) or int(read(lr,ln,'derivationState'))!=0
            or read(latest['withdrawal_fees']['verifier'],ln,'FEE_QUALIFIED') is not True
            or any(read(lr,ln,k)!=read(r,n,k) for k in ('custodyGeneration','derivationGeneration'))):raise ValueError('WITHDRAWAL_FEE_GENERATION')
    return dict(context=ctx,script=bytes.fromhex(desc['script_pubkey']),depth=max(101 if parsed['coinbase'] else 6,int(read(ctx['common'],n,'depth'))))

def check_graph(policy,graph,need):
    pinned=scope(policy,need)
    if graph.get('contracts',{}).get('withdrawal_credits',{}).get('address')!=pinned:raise ValueError('SPV_WITHDRAWAL_FEE_GRAPH')

def guard(w3,policy,state,need,bitcoin,allowed):
    if need.get('kind')!=KIND:return None
    def check():
        from .spv_automation import observation
        result,_=observation(w3,policy,state,need,bitcoin,allowed)
        if result['status']!='NEEDS_STATE':raise ValueError('SPV_WITHDRAWAL_FEE_WORK_NOT_REQUIRED')
    return check
