"""Canonical personal fee graph, snapshots and native Bitcoin checks."""
from types import SimpleNamespace
from hashlib import sha256
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
from .bootstrap import artifact,Bootstrap
from .btc_fee_reserve import read,ZERO
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .withdrawal_fee_intent import WithdrawalFeeIntentV1,PROFILE

def validate_metadata(plan):
    from .bitcoin_onchain_plan import address,hex_value,decimal
    m=plan['withdrawal_fee']
    if not isinstance(m,dict) or set(m)!={'version','payments','previous','treasury','charges','credits'} or m['version']!='1':raise ValueError('WITHDRAWAL_FEE_PLAN_REQUIRED')
    address(m['payments']);hex_value(m['previous'],32);hex_value(m['treasury'],32)
    if not isinstance(m['charges'],list) or not isinstance(m['credits'],list) or len(m['charges'])!=len(plan['withdrawal_refs']) or len(m['credits'])>32:raise ValueError('WITHDRAWAL_FEE_PLAN_SCHEMA')
    if plan['withdrawal_refs']!=sorted(plan['withdrawal_refs']):raise ValueError('WITHDRAWAL_FEE_PLAN_SCHEMA')
    for ref,c in zip(plan['withdrawal_refs'],m['charges']):
        if not isinstance(c,list) or len(c)!=3 or c[0]!='0x'+ref:raise ValueError('WITHDRAWAL_FEE_PLAN_SCHEMA')
        decimal(c[1],2**256-1);decimal(c[2],2100000000000000)
    seen=set()
    for c in m['credits']:
        if not isinstance(c,list) or len(c)!=5:raise ValueError('WITHDRAWAL_FEE_PLAN_SCHEMA')
        if not all(isinstance(v,str) and v.startswith('0x') for v in c[:2]):raise ValueError('WITHDRAWAL_FEE_PLAN_SCHEMA')
        key=hex_value(c[0][2:],32);hex_value(c[1][2:],32)
        if key in seen:raise ValueError('WITHDRAWAL_FEE_PLAN_SCHEMA')
        seen.add(key);decimal(c[2],2100000000000000);decimal(c[3],2100000000000000);decimal(c[4],31)


def for_graph(registry,graph,number):
    w3=registry.w3
    def node(k,kind):return w3.eth.contract(address=graph['contracts'][k]['address'],abi=artifact(kind)['abi'])
    ctx=dict(registry=node('address_registry','BitcoinAddressRegistry'),common=node('deposits','BitcoinDepositRegistry'),
        batch=node('batches','BitcoinBatchRegistry'),escrow=node('escrow','BitcoinWithdrawalEscrow'))
    ctx['withdrawal_fees']=configuration(w3,ctx,number,prove=False)
    if ctx['withdrawal_fees'] is None:raise ValueError('WITHDRAWAL_FEE_GRAPH')
    return ctx


def token_fee(plan,contracts,number=None):
    if plan.get('version')!='4':return 0
    n='latest' if number is None else number;e=contracts['escrow'];w3=e.w3
    root=read(e,n,'withdrawalFees')
    if root==ZERO:raise ValueError('WITHDRAWAL_FEE_GRAPH')
    c=w3.eth.contract(address=root,abi=artifact('WithdrawalFeeCredits')['abi']);total=0
    for request,revision,cost in plan['withdrawal_fee']['charges']:
        b=read(c,n,'getBudget',bytes.fromhex(request[2:]))
        if int(b[3]) in (2,3):total+=int(cost)
    return total

def obligations(plan,contracts,due,number=None):
    """Include named inline returns without counting separate refund principal twice."""
    if plan.get('version')!='4':return due
    n='latest' if number is None else number;e=contracts['escrow'];w3=e.w3
    c=w3.eth.contract(address=read(e,n,'withdrawalFees'),abi=artifact('WithdrawalFeeCredits')['abi']);due=dict(due)
    for identifier,payer,fee,refund,vout in plan['withdrawal_fee']['credits']:
        if int(refund) and int(payer,16) and not read(c,n,'refundRequest',bytes.fromhex(payer[2:])):
            claim=read(c,n,'getClaim',bytes.fromhex(identifier[2:]));b=read(c,n,'getBudget',claim[0]);script=bytes(b[12]).hex()
            due[script]=due.get(script,0)+int(refund)
    return due


def configuration(w3,context,number,*,prove=True,root=None):
    e=context['escrow'];address=root or read(e,number,'withdrawalFees')
    if address==ZERO:return None
    nodes={};args={};proofs={}
    def node(name,where,kind,arguments):
        checked_runtime(w3,where,kind,number);c=w3.eth.contract(address=where,abi=artifact(kind)['abi'])
        nodes[name]=dict(address=where,artifact=kind,runtime_hash=checked_runtime(w3,where,kind,number));args[name]=arguments
        if prove:proofs[name]=prove_creation(w3,where,kind,arguments,number)
        return c
    r=context['registry']
    c=w3.eth.contract(address=address,abi=artifact('WithdrawalFeeCredits')['abi'])
    payment_address=read(c,number,'payments');funding_address=read(c,number,'funding')
    c=node('withdrawal_credits',address,'WithdrawalFeeCredits',[r.address,payment_address,funding_address])
    p=w3.eth.contract(address=payment_address,abi=artifact('WithdrawalFeePayments')['abi']);pv_addr=read(p,number,'verifier')
    pv=node('withdrawal_plan_verifier',pv_addr,'WithdrawalFeePlanVerifier',[])
    p=node('withdrawal_payments',payment_address,'WithdrawalFeePayments',[r.address,c.address,pv.address])
    f=w3.eth.contract(address=funding_address,abi=artifact('WithdrawalFeeFunding')['abi']);fv_addr=read(f,number,'verifier')
    fv=node('withdrawal_derivation',fv_addr,'BitcoinWithdrawalFeeDerivation',[])
    f=node('withdrawal_funding',funding_address,'WithdrawalFeeFunding',[r.address,c.address,fv.address])
    if (read(c,number,'addresses')!=r.address or read(c,number,'escrow')!=e.address or read(p,number,'credits')!=c.address
            or read(p,number,'addresses')!=r.address or read(p,number,'escrow')!=e.address
            or read(f,number,'credits')!=c.address or read(f,number,'addresses')!=r.address or read(f,number,'deposits')!=context['common'].address
            or bytes(read(fv,number,'FEE_PROFILE'))!=keccak(text=PROFILE)
            or bytes(read(f,number,'verifierRuntimeHash'))!=keccak(bytes(w3.eth.get_code(fv.address,block_identifier=number)))):
        raise ValueError('WITHDRAWAL_FEE_GRAPH')
    # Independent preimage matching the constructor exactly (four addresses).
    expected=keccak(encode(['bytes32','uint256']+['address']*4+['bytes32','bytes32','address','bytes32','bytes32','uint64','address','address'],[
        keccak(text='SATOHSI_WITHDRAWAL_FEE_CREDITS_V1'),w3.eth.chain_id,c.address,r.address,e.address,context['batch'].address,
        bytes(read(r,number,'graphId')),bytes(read(r,number,'bitcoinGenesis')),read(r,number,'dkg'),bytes(read(r,number,'walletId')),
        bytes(read(r,number,'walletResult')),int(read(r,number,'keyEpoch')),p.address,f.address]))
    if bytes(read(c,number,'scope'))!=expected:raise ValueError('WITHDRAWAL_FEE_GRAPH')
    payment_scope=keccak(encode(['bytes32','uint256']+['address']*5+['bytes32']*4+['uint64'],[
        keccak(text='SATOHSI_WITHDRAWAL_FEE_PAYMENTS_V1'),w3.eth.chain_id,p.address,r.address,c.address,pv.address,e.address,
        bytes(read(r,number,'graphId')),bytes(read(r,number,'bitcoinGenesis')),bytes(read(r,number,'walletId')),
        bytes(read(r,number,'walletResult')),int(read(r,number,'keyEpoch'))]))
    funding_scope=keccak(encode(['bytes32','uint256']+['address']*4+['bytes32'],[
        keccak(text='SATOHSI_WITHDRAWAL_FEE_FUNDING_V1'),w3.eth.chain_id,f.address,r.address,c.address,fv.address,bytes(read(f,number,'verifierRuntimeHash'))]))
    if bytes(read(p,number,'scope'))!=payment_scope or bytes(read(f,number,'scope'))!=funding_scope:raise ValueError('WITHDRAWAL_FEE_GRAPH')
    return dict(credits=c,payments=p,funding=f,verifier=fv,plan_verifier=pv,nodes=nodes,arguments=args,proofs=proofs,scope=expected.hex())


def funding_binding(context,identifier,number):
    fee=context['withdrawal_fees'];f=fee['funding'];row=read(f,number,'binding',identifier)
    raw=bytes(row[1]);intent=WithdrawalFeeIntentV1.decode(raw);parent=intent.parent;r=context['registry']
    budget=read(fee['credits'],number,'getBudget',row[0]);desc=intent.derive(b'\x02'+bytes(read(r,number,'baseX')))
    if (intent.intent_id!=identifier or bytes(row[0])!=intent.request or intent.credits_scope.hex()!=fee['scope']
            or intent.refund_script_hash!=keccak(bytes(budget[12])) or Web3.to_checksum_address(parent.requester)!=budget[0]
            or Web3.to_checksum_address(parent.recipient)!=budget[0] or Web3.to_checksum_address(parent.deposit_registry)!=r.address
            or parent.bitcoin_genesis!=bytes(read(r,number,'bitcoinGenesis')) or parent.custody_graph_id!=bytes(read(r,number,'graphId'))
            or parent.wallet_id!=bytes(read(r,number,'walletId')) or parent.wallet_result!=bytes(read(r,number,'walletResult'))
            or parent.key_epoch!=int(read(r,number,'keyEpoch')) or parent.evm_chain_id!=r.w3.eth.chain_id
            or parent.dkg_registry!=bytes.fromhex(read(r,number,'dkg')[2:]) or parent.mp_token!=bytes.fromhex(budget[1][2:])
            or parent.pool!=bytes.fromhex(read(context['common'],number,'scriptPools',keccak(b'\x51\x20'+bytes(read(r,number,'baseX'))))[2:])
            or parent.request_nonce!=keccak(encode(['bytes32','bytes32'],[bytes(read(f,number,'scope')),bytes(row[0])]))):raise ValueError('WITHDRAWAL_FEE_BINDING')
    expected=[bytes.fromhex(desc[k]) for k in ('intent_id','merkle_root','tweak','output_key')]+[desc['output_parity_odd'],bytes.fromhex(desc['binding_commitment']),bytes.fromhex(desc['wallet_context'])]
    if list(row[3])!=expected or bytes(row[2])!=keccak(bytes.fromhex(desc['script_pubkey'])):raise ValueError('WITHDRAWAL_FEE_BINDING')
    if bytes(read(context['common'],number,'feeScripts',bytes(row[2])))!=identifier:raise ValueError('WITHDRAWAL_FEE_BINDING')
    if int(row[4])<=0 or bytes(r.w3.eth.get_block(int(row[4])-1).hash)!=bytes(row[5]):raise ValueError('WITHDRAWAL_FEE_BINDING_ANCHOR')
    provenance=keccak(encode(['bytes32','bytes32','bytes32','uint256','bytes32'],[keccak(text='SATOHSI_WITHDRAWAL_FEE_BINDING_RECEIPT_V1'),
        bytes(read(f,number,'scope')),bytes.fromhex(desc['binding_commitment']),int(row[4]),bytes(row[5])]))
    return intent,desc,provenance


def guard(manifest,plan,registry,graph,*,bitcoin=None,state=None,reserved=True):
    from .btc_fee_reserve import configured
    from .bitcoin_input_keys import build
    from .bitcoin_onchain_plan import onchain_plan_hash
    reserve=graph['contracts']['fee_reserve']['address'];i=configured(registry,reserve,bitcoin=bitcoin)
    ctx,observation=i.healthy(state=state);ctx['withdrawal_fees']=configuration(registry.w3,ctx,ctx['block'].number)
    fee=ctx['withdrawal_fees'];n=ctx['block'].number
    if fee is None or plan.get('version')!='4' or plan['withdrawal_fee']['payments']!=fee['payments'].address:raise ValueError('WITHDRAWAL_FEE_PLAN_REQUIRED')
    p=fee['payments'];batch=bytes.fromhex(plan['batch_id']);candidate=read(p,n,'candidate',batch);v=read(p,n,'verification',batch)
    built=build(plan);txid=sha256(sha256(bytes.fromhex(built['unsigned'])).digest()).digest()
    meta=plan['withdrawal_fee']
    if (bytes(candidate[3])!=onchain_plan_hash(manifest,plan) or bytes(candidate[4]).hex()!=built['unsigned']
            or bytes(v[1])!=txid or bytes(candidate[2]).hex()!=meta['previous'] or Bootstrap.wire(candidate[6])!=meta['charges']
            or Bootstrap.wire(candidate[7])!=meta['credits'] or bytes(candidate[8]).hex()!=meta['treasury']
            or int(candidate[0][1])!=int(plan['custody_generation']) or int(v[7])!=int(plan['derivation_generation'])):
        raise ValueError('WITHDRAWAL_FEE_PLAN_BINDING')
    if reserved:
        if not read(p,n,'bound',batch) or bytes(read(p,n,'admissionReceipt',batch))==bytes(32):raise ValueError('WITHDRAWAL_FEE_ADMISSION_REQUIRED')
        read(p,'latest','checkSigning',batch)
    else:
        # Before admission, current candidate revisions and exact owner calls
        # must still match. After admission use the immutable receipt instead.
        if not read(p,n,'bound',batch):
            read(fee['plan_verifier'],n,'verify',p.address,candidate)
            terms=bytes(read(p,n,'termsHash',batch))
            for request,revision,cost in candidate[6]:
                if revision and bytes(read(fee['credits'],n,'admitted',batch,request))!=keccak(encode(['bytes32','uint256'],[terms,revision])):
                    raise ValueError('WITHDRAWAL_FEE_EXACT_OWNER_CONSENT_REQUIRED')
    i.fence(ctx)
    if int(plan['derivation_generation']) and (int(read(ctx['registry'],'latest','derivationGeneration'))!=int(plan['derivation_generation'])
            or int(read(ctx['registry'],'latest','derivationState'))!=0):raise ValueError('WITHDRAWAL_FEE_GENERATION')
    if int(candidate[0][1])!=int(read(ctx['registry'],n,'custodyGeneration')):raise ValueError('WITHDRAWAL_FEE_GENERATION')
    if i.bitcoin.call('getbestblockhash')!=observation['inventory']['tip']:raise ValueError('BTC_INPUT_CORE_CHANGED')
    return observation
