"""External fee ingress, durable observation and permissionless SPV admission."""
from hashlib import sha256
from eth_abi import encode
from eth_utils import keccak
from .bootstrap import artifact
from .btc_fee_client import Driver as Sender
from .btc_fee_reserve import read,digest,ZERO,outpoint
from .funding_envelope import inspect_funding
from .withdrawal_fee import funding_binding
from .withdrawal_fee_watch import FeeWatch,scope as watch_scope

class Funding(Sender):
    def __init__(self,inspector):self.i=inspector;self.bundle=None
    def fence(self,ctx):
        from .bitcoin_continuity import Catalog,sample
        Catalog(identity_file=self.i.c['identity_file']).authorize(self.i.w3,sample(self.i.w3,ctx,ctx['block'].number),'ADDRESS_MINT')
        self.i.fence(ctx);fee=ctx['withdrawal_fees'];r=ctx['registry'];n=ctx['block'].number
        if (read(fee['verifier'],n,'FEE_QUALIFIED') is not True or int(read(r,n,'derivationState'))!=0
                or bytes(read(fee['funding'],n,'verifierRuntimeHash'))!=keccak(bytes(self.i.w3.eth.get_code(fee['verifier'].address,block_identifier=n)))):
            raise ValueError('WITHDRAWAL_FEE_VERIFIER_UNQUALIFIED')
    def store(self,path):
        if self.i.c.get('version')!='2' or str(path) not in self.i.c['fee_watch_databases']:raise ValueError('WITHDRAWAL_FEE_WATCH_REQUIRED')
        ctx=self.i.context();return FeeWatch(path,watch_scope(self.i,ctx))
    def need(self,ctx,binding_id,txid,vout):
        return dict(kind='withdrawal_fee',consumer=ctx['pool'].address,intent_id=binding_id.hex(),txid=txid,vout=str(vout))
    def request_spv(self,need,*,worker_config=None,transport_config=None,federation=None,service=None):
        from .zk_spv import is_zk
        if not self.i.c['bitcoin_policy'] or not is_zk(self.i.c['bitcoin_policy']):raise ValueError('WITHDRAWAL_FEE_ZK_PROFILE_REQUIRED')
        if bool(worker_config)==bool(transport_config):raise ValueError('WITHDRAWAL_FEE_EXPLICIT_SPV_ROUTE')
        ctx=self.i.context();adapter=read(ctx['common'],ctx['block'].number,'spv')
        from .zk_profiles import profile as zk_profile
        a=self.i.w3.eth.contract(address=adapter,abi=artifact(zk_profile(self.i.c['bitcoin_policy'])['adapter'])['abi'])
        graph=dict(chain_id=self.i.c['chain_id'],evm_confirmations=self.i.c['evm_confirmations'],bitcoin_policy=self.i.c['bitcoin_policy'],
            contracts=dict(relay=dict(address=read(a,ctx['block'].number,'state')),spv_guard=dict(address=adapter),
                withdrawal_credits=dict(address=ctx['withdrawal_fees']['credits'].address)))
        if worker_config:
            from .zk_worker import request_from_consumer
            return request_from_consumer(worker_config,graph,need)
        from .zk_worker_transport import request_from_consumer
        return request_from_consumer(transport_config,graph,need,self.i.bitcoin,federation,service)
    def import_bindings(self,path,*,max_bindings=16):
        if not 1<=max_bindings<=16:raise ValueError('WITHDRAWAL_FEE_IMPORT_LIMIT')
        ctx=self.i.context();fee=ctx['withdrawal_fees'];f=fee['funding'];n=ctx['block'].number;s=self.store(path);known={x[0] for x in s.intents()};added=0
        count=int(read(f,n,'bindingCount'));self.i._bounded(count)
        for index in range(count):
            identifier=bytes(read(f,n,'bindingIds',index))
            if identifier.hex() in known:continue
            intent,desc,provenance=funding_binding(ctx,identifier,n)
            s.accept(intent.encode(),bytes.fromhex(desc['base_group']),self.i.c['bitcoin_network'],identifier.hex(),dict(binding=provenance.hex()))
            from .bitcoin_continuity import Catalog,sample
            Catalog(identity_file=self.i.c['identity_file']).alias(self.i.w3,sample(self.i.w3,ctx,n),bytes.fromhex(desc['output_key']),identifier.hex())
            added+=1
            if added>=max_bindings:break
        return dict(status='WITHDRAWAL_FEE_WATCH_IMPORTED',imported=str(added),bindings=str(count),ready=False)
    def address(self,request,path):
        self.import_bindings(path);ctx=self.i.context();fee=ctx['withdrawal_fees'];n=ctx['block'].number
        identifier=bytes(read(fee['funding'],n,'requestBinding',request))
        if identifier==bytes(32):raise ValueError('WITHDRAWAL_FEE_BINDING_REQUIRED')
        self.fence(ctx);intent,desc,provenance=funding_binding(ctx,identifier,n);s=self.store(path)
        row=next((v for key,v,_ in s.intents() if key==identifier.hex()),None)
        if row is None or s.coverage_at(identifier.hex(),self.i.bitcoin.call('getbestblockhash')) is None:
            return dict(status='RESCANNING',intent_id=identifier.hex(),ready=False)
        return dict(status='QUALIFICATION_REQUIRED',intent_id=identifier.hex(),address=row['address'],script=desc['script_pubkey'],
            owner=read(fee['credits'],n,'getBudget',request)[0],request=request.hex(),refund_script=intent.refund_script_hash.hex(),ready=False)
    def credit(self,txid,vout,*,max_inputs=16):
        if not 1<=max_inputs<=16 or not 0<=vout<2**32:raise ValueError('WITHDRAWAL_FEE_CLASSIFICATION_LIMIT')
        ctx=self.i.context();fee=ctx['withdrawal_fees'];f=fee['funding'];n=ctx['block'].number;common=ctx['common'];core=self.i.bitcoin
        wire=bytes.fromhex(txid)[::-1];identifier=keccak(encode(['bytes32','bytes32','uint32'],[bytes.fromhex(fee['scope']),wire,vout]))
        claim=read(fee['credits'],n,'getClaim',identifier)
        # Recovery observes an exact historical right before current issuance
        # policy or gettxout: a moved/consumed claim must never be minted again.
        if bytes(claim[0])!=bytes(32):
            return dict(status='WITHDRAWAL_FEE_CREDIT_RECORDED',credit_id=identifier.hex(),request=bytes(claim[0]).hex(),
                remaining_sats=str(claim[2]),location=bytes(claim[1]).hex(),ready=False)
        self.fence(ctx);core.check_network();best=core.call('getbestblockhash')
        tx=core.call('getrawtransaction',txid,True);parsed=inspect_funding(tx['hex'],txid)
        if vout>=len(parsed['outputs']):raise ValueError('WITHDRAWAL_FEE_TARGET')
        output=parsed['outputs'][vout];binding_id=bytes(read(common,n,'feeScripts',keccak(bytes.fromhex(output['script']))))
        if binding_id==bytes(32):raise ValueError('WITHDRAWAL_FEE_BINDING_REQUIRED')
        intent,desc,provenance=funding_binding(ctx,binding_id,n)
        if desc['script_pubkey']!=output['script']:raise ValueError('WITHDRAWAL_FEE_BINDING')
        self.i.journal.put('withdrawal-fee-observation:'+digest([fee['scope'],txid,vout,tx]),dict(scope=fee['scope'],txid=txid,vout=str(vout),
            raw=parsed['raw'],stripped=parsed['stripped'],binding=provenance.hex(),block=tx.get('blockhash')))
        utxo=core.call('gettxout',txid,vout,True)
        if utxo is None:return dict(status='WITHDRAWAL_FEE_OUTPUT_SPENT_OR_UNKNOWN',credit_id=identifier.hex(),ready=False)
        from .bitcoin_rpc import satoshis
        if utxo['scriptPubKey']['hex']!=output['script'] or satoshis(utxo['value'])!=int(output['amount']):raise ValueError('WITHDRAWAL_FEE_PREVOUT_VALUE')
        maturity=max(101 if parsed['coinbase'] else 6,int(read(common,n,'depth')))
        if int(utxo['confirmations'])<maturity:return dict(status='WITHDRAWAL_FEE_FUNDING_IMMATURE',required=str(maturity),confirmations=str(utxo['confirmations']),ready=False)
        if read(common,n,'custodyTransactions',wire):raise ValueError('WITHDRAWAL_FEE_INTERNAL_INGRESS')
        raw=bytes.fromhex(parsed['stripped']);next_input=int(read(f,n,'classified',wire));end=min(next_input+max_inputs,len(parsed['inputs']))
        def stable():
            latest=self.i.context();self.fence(latest)
            if (int(read(latest['registry'],'latest','custodyGeneration'))!=int(read(ctx['registry'],n,'custodyGeneration'))
                    or int(read(latest['registry'],'latest','derivationGeneration'))!=int(read(ctx['registry'],n,'derivationGeneration'))
                    or core.call('getbestblockhash')!=best or core.call('gettxout',txid,vout,True)!=utxo):raise ValueError('WITHDRAWAL_FEE_OBSERVATION_CHANGED')
        with self.lock():
            if not parsed['coinbase']:
                protected,_=self.i.watches(ctx,best)
                for source in parsed['inputs'][next_input:end]:
                    display,index=source['outpoint'].split(':');index=int(index)
                    previous=core.call('getrawtransaction',display,True);source_tx=inspect_funding(previous['hex'],display)
                    if index>=len(source_tx['outputs']):raise ValueError('WITHDRAWAL_FEE_SOURCE_CONTEXT')
                    value=source_tx['outputs'][index];script=bytes.fromhex(value['script']);key=outpoint(display,index)
                    if (value['script'] in protected or read(common,'latest','scriptPools',keccak(script))!=ZERO
                            or bytes(read(common,'latest','feeScripts',keccak(script)))!=bytes(32)):raise ValueError('WITHDRAWAL_FEE_INTERNAL_INGRESS')
                    nominal=read(common,'latest','getValue',key)
                    if not nominal[2]:self.send(digest([fee['scope'],display,index])+':fee-prevout','BitcoinDepositRegistry',common.address,'cacheValue',[bytes.fromhex(source_tx['stripped']),index],stable)
                    elif int(nominal[0])!=int(value['amount']) or bytes(nominal[1])!=script:raise ValueError('WITHDRAWAL_FEE_SOURCE_CONTEXT')
            if next_input<len(parsed['inputs']):
                self.send(digest([fee['scope'],txid,vout,next_input])+':fee-classify','WithdrawalFeeFunding',f.address,'classify',[raw,vout,max_inputs],stable)
            if int(read(f,'latest','classified',wire))<len(parsed['inputs']):
                return dict(status='WITHDRAWAL_FEE_CLASSIFYING',next_input=str(read(f,'latest','classified',wire)),inputs=str(len(parsed['inputs'])),ready=False)
            from .bitcoin_proofs import inclusion
            proof=inclusion(core,txid);spv=self.i.w3.eth.contract(address=read(common,'latest','spv'),abi=artifact('MainnetSPVGuard')['abi'])
            if not read(spv,'latest','isFresh'):return dict(status='SPV_REQUIRED',need=self.need(ctx,binding_id,txid,vout),block_hash=proof['block_hash'],confirmations=str(maturity),ready=False)
            block_hash=bytes.fromhex(proof['block_hash']);siblings=[bytes.fromhex(x) for x in proof['siblings']];index=int(proof['index'])
            canonical,depth=read(spv,'latest','getBlockStatus',block_hash)
            if not canonical or depth<maturity:return dict(status='SPV_REQUIRED',need=self.need(ctx,binding_id,txid,vout),block_hash=proof['block_hash'],confirmations=str(maturity),ready=False)
            if not read(spv,'latest','checkTxInclusion',block_hash,wire,siblings,index):raise ValueError('WITHDRAWAL_FEE_INCLUSION')
            def admit_guard():
                stable()
                if not read(spv,'latest','isFresh') or not read(spv,'latest','getBlockStatus',block_hash)[0]:raise ValueError('WITHDRAWAL_FEE_INCLUSION_CHANGED')
            self.send(identifier.hex()+':fee-admit','WithdrawalFeeFunding',f.address,'admit',
                [raw,vout,block_hash,siblings,index,int(read(ctx['registry'],n,'custodyGeneration')),int(read(ctx['registry'],n,'derivationGeneration'))],admit_guard)
        if bytes(read(fee['credits'],'latest','getClaim',identifier)[0])!=intent.request:raise ValueError('WITHDRAWAL_FEE_RECEIPT_MISMATCH')
        return dict(status='WITHDRAWAL_FEE_CREDIT_RECORDED',credit_id=identifier.hex(),request=intent.request.hex(),remaining_sats=output['amount'],ready=False)
