"""Already owner-created named BTC credit refunds through the original reviewed V4 Bitcoin path."""
import argparse
import json
import os
from pathlib import Path

from . import contour_withdrawal as withdrawal
from .contour import digest,private_write,sha
from signer_agent.withdrawal_user import Withdrawal,Q,ZERO
from signer_agent.btc_fee_reserve import Inspector,load_config as fee_config,read
from signer_agent.onboarding import read_private
from signer_agent.spv_transport import hex32


class RefundOwner(Withdrawal):
    def validate_quote(self,q,accepted):
        if (set(q)!={'kind','version','operation_id','owner','chain_id','bitcoin_network','graph','options','cost',
                'original_request','original_principal_sats','script','anchor'} or q['kind']!='BTC_CREDIT_REFUND'
                or q['version']!='1' or digest(q)!=accepted or q['owner']!=self.identity()
                or any(q[k]!=self.i.c[k] for k in ('chain_id','bitcoin_network'))):raise ValueError('CONTOUR_REFUND_QUOTE')
        ctx=self.i.context()
        if self.graph(ctx)!=q['graph'] or self.i.w3.eth.get_block(int(q['anchor']['number'])).hash.hex()!=q['anchor']['hash']:
            raise ValueError('CONTOUR_REFUND_GRAPH')
        request=hex32(q['operation_id']);r=read(ctx['escrow'],ctx['block'].number,'getRequest',request)
        credits=ctx['withdrawal_fees']['credits'];original=read(ctx['escrow'],ctx['block'].number,'getRequest',hex32(q['original_request']))
        if (not read(credits,ctx['block'].number,'refundRequest',request)
                or bytes(read(credits,ctx['block'].number,'refundOrigin',request)).hex()!=q['original_request']
                or r[0]!=ZERO or r[2]!=ZERO or r[3]!=q['owner'] or int(r[4])!=int(q['cost']['principal_sats'])*Q
                or bytes(r[5]).hex()!=q['script'] or original[3]!=q['owner']
                or int(original[4])!=int(q['original_principal_sats'])*Q):raise ValueError('CONTOUR_REFUND_REQUEST')
        self.q=q;return ctx
    def submit(self,q,accepted):
        self.validate_quote(q,accepted)
        return dict(status='EXISTING_OWNER_REFUND_REQUEST',request=q['operation_id'],cost=q['cost'],ready=False)
    def status(self,operation_id):
        q=self.q
        if operation_id!=q['operation_id']:raise ValueError('CONTOUR_REFUND_REQUEST')
        ctx=self.validate_quote(q,digest(q));r=read(ctx['escrow'],ctx['block'].number,'getRequest',hex32(operation_id))
        return dict(request=operation_id,primary_state=('NONE','PENDING','BOUND','PAID','REFUNDED')[int(r[7])],cost=q['cost'],ready=False)


def quote(inspector,request):
    hex32(request);ctx=inspector.context();n=ctx['block'].number;credits=ctx['withdrawal_fees']['credits'];key=bytes.fromhex(request)
    original=bytes(read(credits,n,'refundOrigin',key)).hex();r=read(ctx['escrow'],n,'getRequest',key)
    old=read(ctx['escrow'],n,'getRequest',hex32(original));budget=read(credits,n,'getBudget',key)
    owner=RefundOwner(inspector)
    q=dict(kind='BTC_CREDIT_REFUND',version='1',operation_id=request,owner=owner.identity(),
        chain_id=inspector.c['chain_id'],bitcoin_network=inspector.c['bitcoin_network'],graph=owner.graph(ctx),
        options=dict(fee_mode='BTC',asset='MP',calculation='refund',pool=None,deadline=str(budget[9])),
        cost=dict(principal_sats=str(int(r[4])//Q)),original_request=original,original_principal_sats=str(int(old[4])//Q),
        script=bytes(r[5]).hex(),anchor=dict(number=str(n),hash=ctx['block'].hash.hex()))
    owner.validate_quote(q,digest(q));return q


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    q=sub.add_parser('quote');q.add_argument('--owner-config',required=True);q.add_argument('--request-id',required=True);q.add_argument('--output',required=True)
    run=sub.add_parser('run')
    for name in ('withdrawal-config','withdrawal-config-sha256','inputs-sha256','worker-config','worker-config-sha256','request-file','request-sha256'):
        run.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs()
    if a.action=='quote':
        result=quote(Inspector(fee_config(a.owner_config)),a.request_id);private_write(Path(a.output),result)
        print(json.dumps(dict(status='BTC_REFUND_QUOTE_REVIEW_REQUIRED',confirmation_hash=digest(result),ready=False)));return
    from signer_agent.zk_worker import load_config as worker_config
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path));c=withdrawal.load_config(a.withdrawal_config)
    wc=worker_config(a.worker_config);need=json.loads(read_private(a.request_file));data=withdrawal.inputs(c,wc,need)
    if (c['mode']!='BTC' or data[4].get('kind')!='BTC_CREDIT_REFUND' or digest(c)!=a.withdrawal_config_sha256
            or digest(withdrawal.wire_inputs(data))!=a.inputs_sha256
            or digest(wc)!=a.worker_config_sha256 or digest(need)!=a.request_sha256):raise ValueError('CONTOUR_REFUND_SCOPE')
    runtime=withdrawal.Runtime(c,wc,need,data,owner_factory=RefundOwner)
    result=withdrawal.Pipeline(runtime,c,need,ctx).execute(ctx['action']);artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'refund-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
