"""One complete reserve observation and its original typed FROST reconciliation report."""
import argparse
import fcntl
import json
import os
from pathlib import Path

from .contour import digest,private_write,sha
from .contour_calls import Calls,Required,Hold,Progress
from signer_agent.onboarding import read_private,password_file
from signer_agent.btc_fee_reserve import Inspector,load_config as fee_config,read
from signer_agent.btc_fee_client import Driver
from signer_agent.bootstrap import artifact


def load_config(path):
    c=json.loads(read_private(path))
    if (not isinstance(c,dict) or set(c)!={'version','fee_config','bundle_file','state_file','password_file','deadline','report_id'}
            or c['version']!='1' or any(not isinstance(c[k],str) or not Path(c[k]).is_absolute()
                for k in ('fee_config','bundle_file','state_file','password_file'))
            or not isinstance(c['report_id'],str) or len(c['report_id'])!=64 or any(x not in '0123456789abcdef' for x in c['report_id'])):
        raise ValueError('CONTOUR_RECONCILIATION_CONFIG')
    from signer_agent.spv_automation import integer
    integer(c['deadline'],1,2**64-1);return c


def inputs(c,wc):
    from signer_agent.dkg_protocol import Manifest
    fc=fee_config(c['fee_config']);bundle=json.loads(read_private(c['bundle_file']));m=Manifest.from_dict(bundle)
    if (fc['bitcoin_network']!=wc['bitcoin_policy']['network'] or m.purpose!='EVM_AUTH' or bundle.get('role')!='conf_ceremony' or m.chain_id!=int(wc['chain_id'])
            or any(fc[k]!=wc[k] for k in ('chain_id','evm_confirmations','bitcoin_policy'))):raise ValueError('CONTOUR_RECONCILIATION_SCOPE')
    return fc,bundle


class Runtime:
    def __init__(self,c,wc,data):
        from signer_agent.secure_state import SecureState
        self.c=c;fc,self.bundle=data;self.i=Inspector(fc);self.driver=Driver(self.i,self.bundle)
        self.state=SecureState(c['state_file'],password_file(c['password_file']));self.actors=dict(operator=self.i)
        self.job=digest(c)
        ctx=self.i.context();self.driver.authority(ctx)
        dkg=self.i.w3.eth.contract(address=read(ctx['registry'],ctx['block'].number,'dkg'),
            abi=artifact('DKGCoordinator')['abi'])
        completed=read(dkg,ctx['block'].number,'getCompletedResult',bytes(read(ctx['registry'],ctx['block'].number,'walletId')))
        key=self.state.get(bytes(completed[0]).hex())
        if not key or key.get('stage')!='active' or key.get('result_hash')!=bytes(completed[1]).hex():
            raise ValueError('CONTOUR_RECONCILIATION_SIGNER_STATE')
    def allowed(self,role,profile):
        ctx=self.i.context();a=self.driver.authority(ctx);step=profile['steps'][0];target=profile['contracts']['target']
        targets={'FROSTCoordinator':a['coordinator'].address,'DKGOnchainBoard':a['board'].address,
                 'DKGAuthorization':a['authorization'].address,'BitcoinFeeReserve':ctx['fee'].address}
        methods={'FROSTCoordinator':{'createSessionFromDKG'},'DKGOnchainBoard':{'publishIntent'},
                 'DKGAuthorization':{'authorizeOperation'},'BitcoinFeeReserve':{'publishReconciliation'}}
        if (role!='operator' or len(profile['steps'])!=1 or step.get('kind')!='call' or step.get('value','0')!='0'
                or target['address']!=targets.get(target['artifact']) or step['method'] not in methods.get(target['artifact'],set())):raise ValueError('CONTOUR_RECONCILIATION_CALL')
    def observe(self):return self.i.reconcile(full=True,state=self.state)
    def prepare(self,observed):return self.driver.prepare_reconciliation(self.c['deadline'],observation=observed)['request']
    def advance(self,request):
        from signer_agent import btc_fee_policy
        # A complete historical receipt is not re-authorized by today's nonce.
        key='fee-request:'+request['operation_hash'];saved=self.i.journal.get(key)['result']
        if saved:
            reviewed=btc_fee_policy.review(self.i.w3,saved['intent'],self.bundle,'latest',inspector=self.i,executable=False,published=False)
            if reviewed['complete']:return self.driver.execute(saved['intent'])
        result=self.driver.propose(request);intent=result['intent'];ctx=self.i.context();a=self.driver.authority(ctx)
        info=read(a['coordinator'],ctx['block'].number,'getSession',int(intent['session']))
        if int(info[10])!=2:return dict(status='WAITING_RECONCILIATION_AUTHORIZATION')
        return self.driver.execute(intent)


class Pipeline:
    def __init__(self,runtime,c,context):
        if context['capability']!='reconciliation':raise ValueError('CONTOUR_CAPABILITY')
        self.r,self.c=runtime,c;self.journal=runtime.i.journal;self.key='contour-reconciliation:'+digest(c)
        self.journal.put(self.key,c)
    def save(self):self.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason,**facts):return dict(status=status,observations=dict(reason=reason,ready=False,production_ready=False,
        transactions=Calls(self.r,self.record,self.save,'probe').evidence() if status=='SATISFIED' else [],**facts))
    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        fd=os.open(str(self.journal.path)+'.contour.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_RECONCILIATION_BUSY') from None
            self.record=self.journal.get(self.key)['result']
            if not self.record:
                if action=='probe':return self.result('ABSENT','COMPLETE_RECONCILIATION_REQUIRED')
                if action=='recover':return self.result('RECOVERY_REQUIRED','RECONCILIATION_ENROLLMENT_REQUIRED')
                self.record=dict(calls={});self.save()
            calls=Calls(self.r,self.record,self.save,action);self.r.i.transaction_controller=calls
            try:
                calls.pending()
                if 'request' not in self.record:
                    if action=='probe':return self.result('ABSENT','COMPLETE_RECONCILIATION_REQUIRED')
                    if 'observation' not in self.record:
                        self.record['observation']=self.r.observe();self.save()
                    # Inspector persists an incident pause on a critical scan;
                    # negative reports never claim that missing assets exist.
                    self.record['request']=self.r.prepare(self.record['observation']);self.save()
                result=self.r.advance(self.record['request'])
                if result['status']=='BTC_FEE_EXECUTED':
                    if not calls.has('publishReconciliation'):return self.result('RECOVERY_REQUIRED','EXACT_RECONCILIATION_RECEIPT_REQUIRED')
                    healthy=self.record['observation']['status']=='HEALTHY'
                    return self.result('SATISFIED' if healthy else 'RECOVERY_REQUIRED',
                        'CANONICAL_RECONCILIATION_REPORT' if healthy else 'NEGATIVE_RECONCILIATION_REQUIRES_RECOVERY',
                        observation=self.record['observation'])
                return self.result('WAITING',result['status'])
            except Required:return self.result('ABSENT','ORIGINAL_RECONCILIATION_OPERATION_REQUIRED')
            except Hold:return self.result('RECOVERY_REQUIRED','ORIGINAL_RECONCILIATION_RECOVERY_REQUIRED')
            except Progress:return self.result('ABSENT','ORIGINAL_RECONCILIATION_OPERATION_PROGRESS')
            finally:self.r.i.transaction_controller=None
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('reconciliation-config','reconciliation-config-sha256','inputs-sha256','worker-config','worker-config-sha256'):
        p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    from signer_agent.zk_worker import load_config as worker_config
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path))
    c=load_config(a.reconciliation_config);wc=worker_config(a.worker_config);data=inputs(c,wc)
    if digest(c)!=a.reconciliation_config_sha256 or digest(wc)!=a.worker_config_sha256 or digest(data)!=a.inputs_sha256:
        raise ValueError('CONTOUR_RECONCILIATION_SCOPE')
    result=Pipeline(Runtime(c,wc,data),c,ctx).execute(ctx['action']);artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'reconciliation-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
