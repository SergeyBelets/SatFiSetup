"""Safe owner cancellation using the original withdrawal/fee SDK and EOA lane."""
import argparse
import fcntl
import json
import os
from pathlib import Path
from .contour import digest,private_write,sha
from .contour_calls import Calls,Required,Progress,Hold
from signer_agent.onboarding import read_private
from signer_agent.btc_fee_reserve import Inspector,load_config as fee_config
from signer_agent.withdrawal_user import Withdrawal


def load_config(path):
    c=json.loads(read_private(path))
    if not isinstance(c,dict) or set(c)!={'version','owner_config','quote_file','accepted_quote_hash','request_id'} or c.get('version')!='1':raise ValueError('CONTOUR_OWNER_CONFIG')
    from signer_agent.spv_transport import hex32
    hex32(c['accepted_quote_hash']);hex32(c['request_id'])
    if any(not isinstance(c[k],str) or not Path(c[k]).is_absolute() for k in ('owner_config','quote_file')):raise ValueError('CONTOUR_OWNER_CONFIG')
    return c


def inputs(c,wc):
    fc=fee_config(c['owner_config']);body=json.loads(read_private(c['quote_file']));q=body.get('quote',body)
    if digest(q)!=c['accepted_quote_hash'] or fc['bitcoin_network']!=wc['bitcoin_policy']['network'] or any(fc[k]!=wc[k] for k in ('chain_id','bitcoin_policy','evm_confirmations')):
        raise ValueError('CONTOUR_OWNER_SCOPE')
    return fc,q


class Runtime:
    def __init__(self,c,wc,data):
        self.c,self.wc=c,wc;fc,self.quote=data;self.i=Inspector(fc);self.owner=Withdrawal(self.i)
        self.actors=dict(owner=self.i);self.journal=self.i.journal;self.job=digest([c,data])
        self.owner.validate_quote(self.quote,c['accepted_quote_hash'])
    def status(self):
        r=self.owner.status(self.quote['operation_id'])
        if r.get('request')!=self.c['request_id'] or r['cost']['principal_sats']!=self.quote['cost']['principal_sats']:raise ValueError('CONTOUR_OWNER_SCOPE')
        return r
    def allowed(self,role,profile):
        step=profile['steps'][0];target=profile['contracts'][step['contract']];ctx=self.i.context()
        expected=(self.quote['options']['pool'],'SimpleMultiPool','cancelWithdrawal') if self.quote['options']['asset']=='S' else (ctx['escrow'].address,'BitcoinWithdrawalEscrow','refund')
        if role!='owner' or len(profile['steps'])!=1 or step['kind']!='call' or step.get('value','0')!='0' or (target['address'],target['artifact'],step['method'])!=expected:
            raise ValueError('CONTOUR_OWNER_CALL')


class Pipeline:
    def __init__(self,r,c,context):
        if context['capability']!='withdrawal_cancel':raise ValueError('CONTOUR_CAPABILITY')
        self.r,self.c=r,c;self.journal=r.journal;self.key='contour-owner-cancel:'+r.job
        self.journal.put(self.key,dict(config=c,quote=r.quote))
    def save(self):self.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason,**facts):return dict(status=status,observations=dict(reason=reason,request_id=self.c['request_id'],
        principal_sats=self.r.quote['cost']['principal_sats'],ready=False,production_ready=False,**facts))
    def execute(self,action):
        fd=os.open(str(self.journal.path)+'.contour-cancel.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_OWNER_BUSY') from None
            self.record=self.journal.get(self.key)['result']
            if not self.record:
                if action=='recover':return self.result('RECOVERY_REQUIRED','OWNER_ENROLLMENT_REQUIRED')
                if action=='probe':return self.result('ABSENT','OWNER_ENROLLMENT_REQUIRED')
                self.record=dict(calls={});self.save()
            calls=Calls(self.r,self.record,self.save,action);self.r.i.transaction_controller=calls
            try:
                calls.pending();status=self.r.status()
                if status['primary_state']=='REFUNDED':
                    if not any(calls.has(m) for m in ('refund','cancelWithdrawal')):return self.result('RECOVERY_REQUIRED','EXACT_CANCELLATION_RECEIPT_REQUIRED')
                    return self.result('SATISFIED','PRIMARY_CANCELLED_FEE_CREDITS_RETAINED',withdrawal=status,transactions=calls.evidence())
                if action=='probe':return self.result('ABSENT','OWNER_CANCELLATION_REQUIRED')
                self.r.owner.cancel(self.r.quote['operation_id']);return self.result('WAITING','OWNER_CANCELLATION_CONFIRMING')
            except Required:return self.result('ABSENT','ORIGINAL_OWNER_OPERATION_REQUIRED')
            except Progress:return self.result('ABSENT','ORIGINAL_OWNER_OPERATION_PROGRESS')
            except Hold:return self.result('RECOVERY_REQUIRED','ORIGINAL_OWNER_RECOVERY_REQUIRED')
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('owner-operation-config','owner-operation-config-sha256','inputs-sha256','worker-config','worker-config-sha256'):p.add_argument('--'+k,required=True)
    a=p.parse_args(argv)
    from signer_agent.zk_worker import load_config as wc_load
    wc=wc_load(a.worker_config);c=load_config(a.owner_operation_config);data=inputs(c,wc)
    if digest(wc)!=a.worker_config_sha256 or digest(c)!=a.owner_operation_config_sha256 or digest(data)!=a.inputs_sha256:raise ValueError('CONTOUR_OWNER_SCOPE')
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path));result=Pipeline(Runtime(c,wc,data),c,ctx).execute(ctx['action'])
    artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'cancel-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
