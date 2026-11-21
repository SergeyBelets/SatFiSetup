"""Resumable public fee requests using the existing board, EOA journal and gas budgets."""
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from web3.logs import DISCARD
from .bootstrap import Bootstrap,artifact
from .operations import canonical
from .btc_fee_reserve import read,digest
from . import btc_fee_policy as policy


class Driver:
    def __init__(self,inspector,bundle):
        self.i=inspector;self.bundle=bundle
        if not isinstance(bundle,dict) or not bundle.get('onchain_board'):raise ValueError('BTC_FEE_ENROLLMENT_REQUIRED')
    @contextmanager
    def lock(self):
        fd=os.open(str(self.i.journal.path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX);yield
        finally:os.close(fd)
    def authority(self,ctx):
        from .address_registration import authority
        a=authority(self.i.w3,ctx,self.bundle['onchain_board'],ctx['block'].number,self.bundle)
        from .bitcoin_trust import graph_options
        c=self.i.c;options=graph_options(dict(version='2',bitcoin_network=c['bitcoin_network'],evm_confirmations=c['evm_confirmations'],
            **({'bitcoin_policy':c['bitcoin_policy']} if c['bitcoin_policy'] else {})))
        if (dict(bitcoin_network='regtest',evm_confirmations='1')|a['options']) != (dict(bitcoin_network='regtest',evm_confirmations='1')|options):
            raise ValueError('BTC_FEE_AUTHORITY')
        return a
    def prepare(self,method,wire,bitcoin_plan=None):
        if method!='preparePlan' and bitcoin_plan is not None:raise ValueError('BTC_FEE_INTENT_REQUIRED')
        ctx=self.i.context();a=self.authority(ctx)
        fn,args,data,c,op=policy.operation(ctx,method,wire,self.i.w3)
        meta=dict(version='1',pool=ctx['pool'].address,registration_board=a['board'].address,method=method,
            args=wire,bitcoin_plan=bitcoin_plan)
        policy.validate(self.i,ctx,method,args,c,meta,bundle=self.bundle)
        request=dict(version='1',target=ctx['fee'].address,metadata=meta,operation_hash=op.hex())
        self.i.journal.put('fee-request:'+op.hex(),request)
        return dict(status='BTC_FEE_PREPARED',request=request,ready=False)
    def prepare_reconciliation(self,deadline,*,observation=None):
        result=self.i.reconcile(full=True) if observation is None else observation
        ctx=self.i.context();b=ctx['block'];f=ctx['fee'];r=ctx['registry']
        from .btc_fee_reserve import number
        c=[read(f,b.number,'nonce'),read(r,b.number,'custodyGeneration'),read(r,b.number,'derivationGeneration'),number(deadline,1)]
        until=min(c[-1],b.timestamp+int(self.i.c['report_seconds']));body=result.get('inventory')
        state=1 if result['status']=='HEALTHY' else 3 if result['status']=='RECOVERY_REQUIRED' else 2
        if body:
            height=int(body['height']);tip=bytes.fromhex(body['tip']);values=[int(body[k]) for k in ('wallet','backing','pending','free')]
            evidence=bytes.fromhex(result['evidence'])
        else:
            # UNKNOWN may attest a suspension only. Genesis is an explicit
            # historical reference, never a claim of a current observed tip.
            height=0;tip=bytes(read(r,b.number,'bitcoinGenesis'));values=[0,0,0,0]
            evidence=bytes.fromhex(digest(dict(scope=ctx['scope'],observation=result,evm_number=str(b.number))))
        return self.prepare('publishReconciliation',Bootstrap.wire([[c,state,height,tip,until,*values,evidence]]))
    def _intent(self,request,session,ctx,a):
        m=request['metadata']
        if (not isinstance(m,dict) or set(m)!={'version','pool','registration_board','method','args','bitcoin_plan'}
                or m['version']!='1' or m['pool']!=ctx['pool'].address or m['registration_board']!=a['board'].address
                or m['method']!='preparePlan' and m['bitcoin_plan'] is not None):raise ValueError('BTC_FEE_INTENT_REQUIRED')
        fn,args,data,c,op=policy.operation(ctx,m['method'],m['args'],self.i.w3)
        if op.hex()!=request['operation_hash'] or request['target']!=ctx['fee'].address:raise ValueError('BTC_FEE_HASH_MISMATCH')
        from eth_utils.abi import function_abi_to_4byte_selector
        result=dict(chain_id=str(self.i.w3.eth.chain_id),coordinator=a['coordinator'].address,role='conf_ceremony',
            target=ctx['fee'].address,artifact='BitcoinFeeReserve',hash_method='getOperationHash',
            hash_args=Bootstrap.wire([function_abi_to_4byte_selector(fn),data,c[0]]),session=str(session),operation_hash=op.hex(),btc_fee_reserve=m)
        if len(canonical(result))>32768:raise ValueError('BTC_FEE_INTENT_LIMIT')
        return result
    def send(self,key,kind,target,method,args,guard):
        i=self.i;c=i.c
        from .spv_automation import Budget
        budget=Budget(c['identity_file'],'btc-fee:'+c['reserve'],c,key.split(':')[0],guard=guard)
        class Sender(Bootstrap):
            def build_transaction(self,step,account,nonce):
                tx=super().build_transaction(step,account,nonce);budget.reserve(key,tx);return tx
        profile=dict(deployment_id='btc-fee-'+digest([c['reserve'],key]),chain_id=c['chain_id'],evm_rpc=c['evm_rpc'],
            evm_confirmations=c['evm_confirmations'],bitcoin_network=c['bitcoin_network'],
            mode='backed' if c['chain_id']=='1' or c['bitcoin_network']!='regtest' else 'integration',
            contracts=dict(target=dict(address=target,artifact=kind)),
            steps=[dict(id='send',kind='call',contract='target',method=method,args=Bootstrap.wire(args),value='0')])
        if c['bitcoin_policy'] is not None:profile['bitcoin_policy']=c['bitcoin_policy']
        controller=getattr(i,'transaction_controller',None)
        if controller is not None:return controller.submit_fee(self,key,profile,budget)
        runner=Sender(profile,str(Path(c['identity_file']).resolve())+'.transactions.sqlite')
        if runner.apply_confirmed(c['identity_file'])['status']!='VERIFIED':raise ValueError('BTC_FEE_TRANSACTION_UNCONFIRMED')
        receipt=runner.verify_receipt(runner.journal.get(profile['deployment_id']+':send')['result'])
        if receipt is None:raise ValueError('BTC_FEE_TRANSACTION_UNCONFIRMED')
        return receipt
    def propose(self,request):
        if set(request)!={'version','target','metadata','operation_hash'} or request['version']!='1':raise ValueError('BTC_FEE_INTENT_REQUIRED')
        with self.lock():
            ctx=self.i.context();a=self.authority(ctx);m=request['metadata']
            from .spv_automation import identity
            sender=identity(self.i.c['identity_file'])
            if sender!=read(a['board'],ctx['block'].number,'operator') and not read(a['board'],ctx['block'].number,'canPublishIntent',sender):raise ValueError('BTC_FEE_OPERATOR_REQUIRED')
            expected=self._intent(request,1,ctx,a);op=request['operation_hash'];key='fee-request:'+op
            self.i.journal.put(key,request);saved=self.i.journal.get(key)['result']
            def check():
                latest=self.i.context();fn,args,data,c,d=policy.operation(latest,m['method'],m['args'],self.i.w3)
                if d.hex()!=op:raise ValueError('BTC_FEE_HASH_MISMATCH')
                policy.validate(self.i,latest,m['method'],args,c,m,bundle=self.bundle)
            if not saved:
                fn,args,data,c,h=policy.operation(ctx,m['method'],m['args'],self.i.w3)
                receipt=self.send(op+':session','FROSTCoordinator',a['coordinator'].address,'createSessionFromDKG',
                    [a['dkg_id'],h,ctx['fee'].address,c[3]],check)
                events=a['coordinator'].events.SessionCreated().process_receipt(receipt,errors=DISCARD)
                if len(events)!=1:raise ValueError('BTC_FEE_RECEIPT_MISMATCH')
                sid=int(events[0]['args']['sessionId']);public=self._intent(request,sid,ctx,a)
                saved=dict(intent=public,anchor=dict(number=str(receipt['blockNumber']),hash=receipt['blockHash'].hex()))
                self.i.journal.update(key,'SESSION_CREATED',saved)  # Before publication; exact tx replay recovers a lost receipt.
            if self.i.w3.eth.get_block(int(saved['anchor']['number'])).hash.hex()!=saved['anchor']['hash']:raise ValueError('BTC_FEE_SESSION_REORG')
            public=saved['intent'];sid=int(public['session']);raw=canonical(public).encode()
            if len(raw)>32768:raise ValueError('BTC_FEE_INTENT_LIMIT')
            policy.review(self.i.w3,public,self.bundle,'latest',inspector=self.i,published=False)
            prior=bytes(read(a['board'],'latest','intents',sid))
            if prior and prior!=raw:raise ValueError('BTC_FEE_PUBLIC_INTENT_REQUIRED')
            if not prior:self.send(op+':publish','DKGOnchainBoard',a['board'].address,'publishIntent',[sid,raw],check)
            return dict(status='AWAITING_PARTICIPANTS',intent=public,ready=False)
    def execute(self,intent):
        with self.lock():
            r=policy.review(self.i.w3,intent,self.bundle,'latest',inspector=self.i,executable=False)
            if r['complete']:return dict(status='BTC_FEE_EXECUTED',operation_hash=intent['operation_hash'],ready=False)
            def check():return policy.review(self.i.w3,intent,self.bundle,'latest',inspector=self.i)
            r=check();ctx=r['context'];a=r['authority'];op=r['operation_hash'];sid=r['session'];key=op.hex()
            if not read(a['authorization'],'latest','isOperationApproved',op,ctx['fee'].address):
                result=read(a['coordinator'],'latest','getAuthorizationResult',sid)
                if bytes(result[1])!=op or result[4]!=ctx['fee'].address:raise ValueError('BTC_FEE_SESSION_MISMATCH')
                self.send(key+':authorize','DKGAuthorization',a['authorization'].address,'authorizeOperation',[sid,op,ctx['fee'].address],check)
            self.send(key+':apply','BitcoinFeeReserve',ctx['fee'].address,r['method'],[*r['arguments'],op],check)
            r=policy.review(self.i.w3,intent,self.bundle,'latest',inspector=self.i,executable=False)
            if not r['complete']:raise ValueError('BTC_FEE_RECEIPT_MISMATCH')
            return dict(status='BTC_FEE_EXECUTED',operation_hash=op.hex(),ready=False)


def bootstrap_plan(registry,board,fee_cap,max_vbytes):
    """Deployment-only extension. Binding is a separately reviewed FROST action."""
    from web3 import Web3
    if any(not Web3.is_checksum_address(a) or int(a,16)==0 for a in (registry,board)):raise ValueError('BTC_FEE_GRAPH_MISMATCH')
    from .btc_fee_reserve import number
    number(fee_cap,1,2100000000000000);number(max_vbytes,100,100000)
    return dict(schema='satoshifi.btc-fee-bootstrap.v1',contracts={},steps=[
        dict(id='fee_donor',kind='deploy',artifact='FROSTVerifier',args=[]),
        dict(id='fee_funding',kind='deploy',artifact='BitcoinFeeFundingVerifier',args=[]),
        dict(id='fee_plan',kind='deploy',artifact='BitcoinFeePlanVerifier',args=[]),
        dict(id='fee_reserve',kind='deploy',artifact='BitcoinFeeReserve',args=[registry,board,'$fee_donor','$fee_funding','$fee_plan',fee_cap,max_vbytes])],
        binding=dict(artifact='BitcoinBatchRegistry',hash_method='getSetFeeReserveOperationHash',method='setFeeReserve',reserve='$fee_reserve',
            requires='Existing enrolled confederation FROST authorization; before first Bitcoin batch'),ready=False)
