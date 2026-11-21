"""Exact reviewed fee ingress/token-return/BTC-refund consent through original SDKs and journals."""
import argparse
import fcntl
import json
import os
from pathlib import Path

from .contour import digest,private_write,sha
from .contour_calls import Calls,Required,Hold,Progress
from signer_agent.btc_fee_reserve import Inspector,load_config as fee_config,read
from signer_agent.withdrawal_fee_funding import Funding
from signer_agent.withdrawal_fee_client import Owner
from signer_agent.onboarding import read_private
from signer_agent.spv_transport import hex32,uint
from signer_agent.bootstrap import Bootstrap,artifact,encode_args


def fee_mode(mode):
    return 'MP' if mode in ('REFUND_MP','CONSENT_MP') else 'S' if mode in ('REFUND_S','CONSENT_S') else 'BTC'


def load_config(path):
    c=json.loads(read_private(path))
    if (not isinstance(c,dict) or set(c)!={'version','mode','fee_config','request_file','accepted_request_sha256'} or c['version']!='1'
            or c['mode'] not in ('FUNDING','REFUND_MP','REFUND_S','CREATE_BTC_REFUND','RAISE_REFUND_CAP','CANCEL_BTC_REFUND','CONSENT_BTC','CONSENT_MP','CONSENT_S')
            or any(not isinstance(c[k],str) or not Path(c[k]).is_absolute() for k in ('fee_config','request_file'))):raise ValueError('CONTOUR_FEE_CONFIG')
    hex32(c['accepted_request_sha256']);return c


def inputs(c,wc):
    fc=fee_config(c['fee_config']);request=json.loads(read_private(c['request_file']))
    if (digest(request)!=c['accepted_request_sha256'] or fc['bitcoin_network']!=wc['bitcoin_policy']['network']
            or any(fc[k]!=wc[k] for k in ('chain_id','bitcoin_policy','evm_confirmations'))):raise ValueError('CONTOUR_FEE_SCOPE')
    if c['mode']=='FUNDING':
        from signer_agent.withdrawal_fee_spv import scope
        scope(wc,request)
    else:
        if (set(request)!={'method','args','request_id','principal_request','principal_sats','expected_nonce'}
                or request['method']!=dict(CREATE_BTC_REFUND='createRefund',RAISE_REFUND_CAP='raiseRefundCap',CANCEL_BTC_REFUND='cancelRefund',
                    CONSENT_BTC='consent',CONSENT_MP='consent',CONSENT_S='consentFee').get(c['mode'],'refundTokens')
                or not isinstance(request['args'],list)):raise ValueError('CONTOUR_FEE_REQUEST')
        hex32(request['request_id']);hex32(request['principal_request']);uint(request['principal_sats'],0)
        if request['method']=='refundTokens':
            if (request['args']!=['0x'+request['request_id']] or request['expected_nonce'] is not None
                    or request['principal_request']!=request['request_id']):raise ValueError('CONTOUR_FEE_REQUEST')
        elif request['method']=='createRefund':
            if len(request['args'])!=8 or request['args'][1]!='0x'+request['principal_request']:raise ValueError('CONTOUR_FEE_REQUEST')
            uint(request['expected_nonce'])
        elif request['method'] in ('consent','consentFee'):
            kind='SimpleMultiPool' if request['method']=='consentFee' else 'WithdrawalFeeCredits'
            spec=next(x for x in artifact(kind)['abi'] if x.get('name')==request['method'])
            typed=encode_args(spec['inputs'],request['args'])
            if len(typed)!=1 or bytes(typed[0][0]).hex()!=request['request_id'] or request['request_id']!=request['principal_request'] or request['expected_nonce'] is not None:
                raise ValueError('CONTOUR_FEE_REQUEST')
            if typed[0][2]!=('NONE','BTC','MP','S').index(fee_mode(c['mode'])):raise ValueError('CONTOUR_FEE_REQUEST')
        elif request['method']=='raiseRefundCap':
            if len(request['args'])!=5 or request['args'][0]!='0x'+request['request_id'] or request['expected_nonce'] is not None:raise ValueError('CONTOUR_FEE_REQUEST')
        elif request['args']!=['0x'+request['request_id']] or request['expected_nonce'] is not None:raise ValueError('CONTOUR_FEE_REQUEST')
    return fc,request


class Runtime:
    def __init__(self,c,wc,data,*,prepare_only=False):
        self.c,self.wc=c,wc;fc,self.request=data;self.i=Inspector(fc);self.actors=dict(operator=self.i)
        self.journal=self.i.journal;self.prepare_only=prepare_only;self.job=digest([c,data,prepare_only]);self.sdk=Funding(self.i) if c['mode']=='FUNDING' else Owner(self.i)
        self.context()
    def context(self):
        from signer_agent.zk_spv import inspect_binding
        ctx=self.i.context();n=ctx['block'].number
        if self.wc['allowed_consumers'].get(ctx['pool'].address)!='MiningPoolV1':raise ValueError('CONTOUR_FEE_SCOPE')
        inspect_binding(self.i.w3,self.wc['zk_state'],self.wc['bitcoin_policy'],n,adapter_address=read(ctx['common'],n,'spv'))
        if self.c['mode']=='FUNDING':
            from signer_agent.withdrawal_fee_spv import view
            view(self.i.w3,self.wc,self.request,n)
        else:
            req=bytes.fromhex(self.request['principal_request']);primary=read(ctx['escrow'],n,'getRequest',req)
            if int(primary[4])!=int(self.request['principal_sats'])*10**10:raise ValueError('CONTOUR_FEE_PRINCIPAL')
            b=read(ctx['withdrawal_fees']['credits'],n,'getBudget',req)
            from signer_agent.spv_automation import identity
            expected=('NONE','BTC','MP','S').index(fee_mode(self.c['mode']))
            modes=(0,expected) if self.c['mode'].startswith('CONSENT_') else (expected,)
            if primary[3]!=identity(self.i.c['identity_file']) or int(b[3]) not in modes:raise ValueError('CONTOUR_FEE_OWNER')
        return ctx
    def allowed(self,role,profile):
        ctx=self.context();s=profile['steps'][0];t=profile['contracts'][s['contract']];f=ctx['withdrawal_fees']
        targets={'BitcoinDepositRegistry':(ctx['common'].address,{'cacheValue'}),'WithdrawalFeeFunding':(f['funding'].address,{'classify','admit'}),
            'WithdrawalFeeCredits':(f['credits'].address,{self.request.get('method')})}
        if self.c['mode'].startswith('CONSENT_'):
            primary=read(ctx['escrow'],ctx['block'].number,'getRequest',bytes.fromhex(self.request['principal_request']))
            targets['PoolMpToken']=(primary[2],{'approve'})
            if self.c['mode']=='CONSENT_S':targets['SimpleMultiPool']=(self.request['args'][0][3],{'consentFee'})
        a=targets.get(t['artifact'])
        if (role!='operator' or len(profile['steps'])!=1 or s['kind']!='call' or s.get('value','0')!='0' or a is None
                or a[0]!=t['address'] or s['method'] not in a[1]):raise ValueError('CONTOUR_FEE_CALL')
        if s['method']=='approve':
            if s['args'][0]!=f['credits'].address or not 0<=int(s['args'][1])<=int(self.request['args'][0][5]):raise ValueError('CONTOUR_FEE_CALL')
        elif self.c['mode']!='FUNDING' and s['args']!=self.request['args']:raise ValueError('CONTOUR_FEE_CALL')
    def before_send(self,role,profile):
        if profile['steps'][0]['method']!='createRefund':return
        from eth_abi import encode
        from eth_utils import keccak
        from signer_agent.spv_automation import identity
        ctx=self.context();credits=ctx['withdrawal_fees']['credits'];owner=identity(self.i.c['identity_file']);n=ctx['block'].number
        nonce=int(read(credits,'latest','refundNonce',owner))
        expected=keccak(encode(['bytes32','address','uint256','bytes32'],[bytes(read(credits,n,'scope')),owner,nonce,
            bytes.fromhex(self.request['args'][0].removeprefix('0x'))])).hex()
        if nonce!=int(self.request['expected_nonce']) or expected!=self.request['request_id']:raise ValueError('CONTOUR_FEE_REFUND_NONCE')
        if self.i.w3.eth.get_transaction_count(owner,'pending')!=self.i.w3.eth.get_transaction_count(owner,'latest'):
            raise ValueError('CONTOUR_FEE_PENDING_NONCE')
    def advance(self):
        if self.c['mode']=='FUNDING':
            result=self.sdk.credit(self.request['txid'],int(self.request['vout']))
            if result['status']=='WITHDRAWAL_FEE_CREDIT_RECORDED' and not self.calls.has('admit'):self.adopt_admit()
            return result
        result=self.sdk.call(self.request['method'],self.request['args'])
        if self.request['method']=='createRefund' and result['refund_request']!=self.request['request_id']:raise ValueError('CONTOUR_FEE_REFUND_REQUEST')
        ctx=self.context();f=ctx['withdrawal_fees'];n=ctx['block'].number;request=bytes.fromhex(self.request['request_id'])
        budget=read(f['credits'],n,'getBudget',request)
        if self.request['method']=='refundTokens' and not budget[13]:raise ValueError('CONTOUR_FEE_REFUND_RECEIPT')
        if self.request['method']=='cancelRefund' and (not budget[13] or int(read(ctx['escrow'],n,'getRequest',request)[7])!=4):raise ValueError('CONTOUR_FEE_REFUND_RECEIPT')
        if self.request['method']=='raiseRefundCap' and (str(budget[8])!=self.request['args'][1] or str(budget[4])!=self.request['args'][2]):raise ValueError('CONTOUR_FEE_REFUND_RECEIPT')
        if self.request['method'] in ('consent','consentFee'):
            expected=self.request['args'][0]
            if (str(budget[4])!=expected[1] or str(budget[3])!=expected[2] or str(budget[6])!=expected[4]):raise ValueError('CONTOUR_FEE_REFUND_RECEIPT')
        if self.request['method']=='createRefund':
            if (bytes(read(f['credits'],n,'refundOrigin',request)).hex()!=self.request['principal_request']
                    or not read(f['credits'],n,'refundRequest',request)):raise ValueError('CONTOUR_FEE_REFUND_RECEIPT')
        return result
    def adopt_admit(self):
        from signer_agent.operations import Journal
        from signer_agent.funding_envelope import inspect_funding
        from eth_abi import encode
        from eth_utils import keccak
        ctx=self.context();f=ctx['withdrawal_fees'];need=self.request
        identifier=keccak(encode(['bytes32','bytes32','uint32'],[bytes.fromhex(f['scope']),bytes.fromhex(need['txid'])[::-1],int(need['vout'])])).hex()
        key=identifier+':fee-admit';prefix='btc-fee-'+digest([self.i.c['reserve'],key]);path=str(Path(self.i.c['identity_file']).resolve())+'.transactions.sqlite'
        try:entry=Journal(path).get(prefix+':send')
        except KeyError:raise Hold() from None
        s=entry['request']
        if s.get('method')!='admit' or int(s['args'][1])!=int(need['vout']):raise ValueError('CONTOUR_FEE_RECEIPT')
        inspect_funding(s['args'][0].removeprefix('0x'),need['txid'])
        if not entry['result'].get('raw_transaction'):raise Hold()
        self.sdk.send(key,'WithdrawalFeeFunding',f['funding'].address,'admit',s['args'],lambda:(_ for _ in ()).throw(Hold()))


class Pipeline:
    def __init__(self,runtime,c,context):
        expected='bitcoin_funding' if c['mode']=='FUNDING' else 'withdrawal_'+fee_mode(c['mode']).lower()
        if context['capability']!=expected:raise ValueError('CONTOUR_CAPABILITY')
        self.r,self.c=runtime,c;self.journal=runtime.journal;self.key='contour-fee:'+runtime.job
        self.journal.put(self.key,dict(config=c,request=runtime.request))
    def save(self):self.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason):return dict(status=status,observations=dict(reason=reason,mode=self.c['mode'],
        request_id=self.r.request.get('request_id'),ready=False,production_ready=False,
        transactions=Calls(self.r,self.record,self.save,'probe').evidence() if status=='SATISFIED' else []))
    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        fd=os.open(str(self.journal.path)+'.contour.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_FEE_BUSY') from None
            self.record=self.journal.get(self.key)['result']
            if not self.record:
                if action=='probe':return self.result('ABSENT','ORIGINAL_FEE_OPERATION_REQUIRED')
                if action=='recover':return self.result('RECOVERY_REQUIRED','FEE_ENROLLMENT_REQUIRED')
                self.record=dict(calls={});self.save()
            calls=Calls(self.r,self.record,self.save,action);self.r.calls=calls;self.r.i.transaction_controller=calls
            try:
                calls.pending();result=self.r.advance()
                done=(result['status']=='WITHDRAWAL_FEE_CREDIT_RECORDED' or self.r.prepare_only and result['status']=='SPV_REQUIRED'
                    if self.c['mode']=='FUNDING' else result['status']=='WITHDRAWAL_FEE_OWNER_CONFIRMED')
                return self.result('SATISFIED' if done else 'WAITING',result['status'])
            except Required:return self.result('ABSENT','ORIGINAL_FEE_OPERATION_REQUIRED')
            except Hold:return self.result('RECOVERY_REQUIRED','ORIGINAL_FEE_RECOVERY_REQUIRED')
            except Progress:return self.result('ABSENT','ORIGINAL_FEE_OPERATION_PROGRESS')
            finally:self.r.i.transaction_controller=None
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('fee-operation-config','fee-operation-config-sha256','inputs-sha256','worker-config','worker-config-sha256'):p.add_argument('--'+name,required=True)
    p.add_argument('--prepare-only',action='store_true')
    a=p.parse_args(argv)
    from signer_agent.zk_worker import load_config as worker_config
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path));c=load_config(a.fee_operation_config)
    wc=worker_config(a.worker_config);data=inputs(c,wc)
    if digest(c)!=a.fee_operation_config_sha256 or digest(wc)!=a.worker_config_sha256 or digest(data)!=a.inputs_sha256:raise ValueError('CONTOUR_FEE_SCOPE')
    if a.prepare_only and c['mode']!='FUNDING':raise ValueError('CONTOUR_FEE_SCOPE')
    result=Pipeline(Runtime(c,wc,data,prepare_only=a.prepare_only),c,ctx).execute(ctx['action']);artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'fee-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
