"""Exact Bitcoin funding observation and optional recovery of an accepted SMP1 quote."""
import argparse
import json
import os
from pathlib import Path

from .contour import digest, private_write, sha
from signer_agent.bitcoin_rpc import BitcoinRPCError
from signer_agent.onboarding import read_private
from signer_agent.zk_worker import Worker,load_config as worker_config


def load_config(path):
    c=json.loads(read_private(path))
    if not isinstance(c,dict) or c.get('version')!='1':raise ValueError('CONTOUR_FUNDING_CONFIG')
    if c.get('mode')=='OBSERVE':
        if set(c)!={'version','mode'}:raise ValueError('CONTOUR_FUNDING_CONFIG')
    elif c.get('mode')=='SMP1_QUOTE':
        if set(c)!={'version','mode','deposit_config','operation_id','accepted_quote_hash'}:
            raise ValueError('CONTOUR_FUNDING_CONFIG')
        if not isinstance(c['deposit_config'],str) or not Path(c['deposit_config']).is_absolute():
            raise ValueError('CONTOUR_FUNDING_CONFIG')
        for name in ('operation_id','accepted_quote_hash'):
            if not isinstance(c[name],str) or len(c[name])!=64 or any(x not in '0123456789abcdef' for x in c[name]):
                raise ValueError('CONTOUR_FUNDING_CONFIG')
    else:raise ValueError('CONTOUR_FUNDING_CONFIG')
    return c


def deposit_config(c,wc,request):
    if c['mode']=='OBSERVE':return None
    from signer_agent.smp1_deposit import load_config
    d=load_config(c['deposit_config'])
    if (request.get('kind')!='deposit' or d['mining_pool']!=request['consumer'] or d['bitcoin_network']!=wc['bitcoin_policy']['network']
            or any(d[k]!=wc[k] for k in ('chain_id','evm_confirmations','bitcoin_policy'))):
        raise ValueError('CONTOUR_FUNDING_SCOPE')
    return d


class Pipeline:
    def __init__(self,worker,request,c,context,*,deposit=None):
        if context['capability']!='bitcoin_funding':raise ValueError('CONTOUR_CAPABILITY')
        if request.get('kind') not in ('deposit','address_deposit','withdrawal_fee'):
            raise ValueError('CONTOUR_FUNDING_NEED')
        self.worker,self.request,self.c,self.context,self.deposit=worker,request,c,context,deposit
        self.key='contour-funding:'+digest([worker.inbox.get('scope'),request])
        self.immutable=dict(config=c,request=request,scope=worker.inbox.get('scope'),
            deposit_config_sha256=digest(deposit.c) if deposit else None)
        self.record=worker.inbox.get(self.key)
        if self.record and self.record['immutable']!=self.immutable:raise ValueError('CONTOUR_FUNDING_SCOPE')

    def save(self):self.worker.inbox.put(self.key,self.record)

    def outcome(self,status,reason,**facts):
        return dict(status=status,observations=dict(reason=reason,mode=self.c['mode'],txid=self.request['txid'],
            vout=self.request['vout'],ready=False,production_ready=False,**facts))

    def quote(self):
        entry,q,_=self.deposit.entry(self.c['operation_id'],self.c['accepted_quote_hash'])
        if (q['txid']!=self.request['txid'] or q['vout']!=self.request['vout']
                or q['graph']['pool']!=self.request['consumer']):raise ValueError('CONTOUR_FUNDING_QUOTE')
        return entry,q

    def wallet_action(self,action):
        entry,q=self.quote();state=entry['status']
        if state in ('SIGNED','BROADCASTING','BROADCAST'):
            result=self.deposit.broadcast(self.c['operation_id'],self.c['accepted_quote_hash'])
            return self.outcome('ABSENT',result['status'])
        if state=='PARTIALLY_SIGNED':
            # Only finalizes the already retained PSBT; no second wallet signature.
            result=self.deposit.sign(self.c['operation_id'],self.c['accepted_quote_hash'])
            return self.outcome('ABSENT',result['status'])
        if state not in ('PREPARED','RESERVED'):return self.outcome('RECOVERY_REQUIRED','ORIGINAL_WALLET_RECOVERY_REQUIRED')
        if self.record.get('sign_started') or action=='recover' and not self.record.get('enrolled'):
            return self.outcome('RECOVERY_REQUIRED','UNSIGNED_FUNDING_RECOVERY')
        self.record['sign_started']=True;self.save()
        result=self.deposit.sign(self.c['operation_id'],self.c['accepted_quote_hash'])
        return self.outcome('ABSENT',result['status'])

    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        with self.worker.exclusive():
            self.record=self.worker.inbox.get(self.key)
            if self.record and self.record['immutable']!=self.immutable:raise ValueError('CONTOUR_FUNDING_SCOPE')
            if self.deposit:self.quote()
            b,state,adapter,_,core=self.worker.context(allow_halt=True)
            if core is None:return self.outcome('WAITING','STATE_HALTED')
            original=(self.record or {}).get('funding')
            try:
                if original:
                    observed=self.worker._observe_transaction(self.request,b,adapter,core,original['depth'],bytes.fromhex(original['script']))
                else:observed=self.worker.observe_need(self.request,b,adapter,core)
            except BitcoinRPCError as error:
                if error.rpc_code!=-5:raise
                observed=dict(status='NOT_OBSERVED')
            self.worker.stable(b)
            if observed['status'] in ('READY','NEEDS_STATE'):
                tx=self.worker.bitcoin.call('getrawtransaction',self.request['txid'],True)
                from signer_agent import crypto
                decoded=crypto.call('bitcoin_funding',raw=tx['hex'],txid=self.request['txid'])
                from signer_agent.bitcoin_network import header_hash
                if (self.worker.bitcoin.call('getblockhash',observed['height'])!=header_hash(bytes.fromhex(observed['header']))[::-1].hex()
                        or self.worker.bitcoin.call('getblockhash',core['blocks'])!=core['bestblockhash']):
                    return self.outcome('RECOVERY_REQUIRED','BITCOIN_FUNDING_REORG')
                self.worker.stable(b)
                index=int(self.request['vout'])
                if index>=len(decoded['outputs']):raise ValueError('CONTOUR_FUNDING_OUTPUT')
                funding=dict(height=observed['height'],header=observed['header'],depth=observed['depth'],
                    script=decoded['outputs'][index]['script'],amount_sats=decoded['outputs'][index]['amount'],
                    raw_sha256=digest(tx['hex']))
                if original and funding!=original:return self.outcome('RECOVERY_REQUIRED','ORIGINAL_FUNDING_CHANGED')
                if not self.record:self.record=dict(immutable=self.immutable)
                self.record['funding']=funding;self.save()
                return self.outcome('SATISFIED','CANONICAL_MATURE_BITCOIN_INCLUSION',funding=funding)
            if original:return self.outcome('RECOVERY_REQUIRED','ORIGINAL_FUNDING_UNAVAILABLE')
            if observed['status']=='COMPLETED':return self.outcome('RECOVERY_REQUIRED','COMPLETED_WITHOUT_FUNDING_ENROLLMENT')
            if action=='probe' or not self.deposit:
                actionable=self.deposit and observed['status']=='NOT_OBSERVED'
                return self.outcome('ABSENT' if actionable else 'WAITING',observed['status'])
            if not self.record:
                if action=='recover':return self.outcome('RECOVERY_REQUIRED','FUNDING_ENROLLMENT_REQUIRED')
                self.record=dict(immutable=self.immutable,enrolled=True);self.save()
            return self.wallet_action(action)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('funding-config','funding-config-sha256','deposit-config-sha256','worker-config',
                 'worker-config-sha256','request-file','request-sha256'):p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs()
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);context=json.loads(read_private(path))
    c=load_config(a.funding_config);wc=worker_config(a.worker_config);request=json.loads(read_private(a.request_file))
    dc=deposit_config(c,wc,request)
    if any(x!=y for x,y in ((digest(c),a.funding_config_sha256),(digest(dc),a.deposit_config_sha256),
                            (digest(wc),a.worker_config_sha256),(digest(request),a.request_sha256))):
        raise ValueError('CONTOUR_FUNDING_SCOPE')
    from signer_agent.smp1_deposit import Deposit
    worker=Worker(wc)
    try:
        result=Pipeline(worker,request,c,context,deposit=Deposit(dc) if dc else None).execute(context['action'])
        artifacts=context['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
        if result['status']=='SATISFIED' and not artifacts:
            out=path.parent/'funding-evidence.json';private_write(out,result['observations'])
            artifacts={str(out.relative_to(context['directory'])):sha(out)}
        binding={k:context[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
        private_write(context['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))
    finally:worker.close()


if __name__=='__main__':main()
