"""Bounded structural bootstrap through the original deployment and nonce journals."""
import argparse
import fcntl
import json
import os
from pathlib import Path

from eth_account import Account

from .contour import digest, private_write, sha
from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_plan import build
from signer_agent.btc_fee_reserve import number
from signer_agent.onboarding import read_private
from signer_agent.zk_worker import load_config as worker_config


class Boundary(Exception):
    pass


def load_config(path):
    c=json.loads(read_private(path))
    fields={'version','profile_file','state_file','identity_file','bitcoin_rpc','bitcoin_cookie',
            'max_tx_gas','max_transaction_cost_wei','max_total_cost_wei'}
    if (not isinstance(c,dict) or set(c)!=fields or c['version']!='1'
            or any(not isinstance(c[k],str) or not Path(c[k]).is_absolute()
                   for k in ('profile_file','state_file','identity_file','bitcoin_cookie'))):
        raise ValueError('CONTOUR_BOOTSTRAP_CONFIG')
    for key in ('max_tx_gas','max_transaction_cost_wei','max_total_cost_wei'):number(c[key],1,2**256-1)
    if int(c['max_transaction_cost_wei'])>int(c['max_total_cost_wei']):raise ValueError('CONTOUR_BOOTSTRAP_BUDGET')
    return c


def profile(c,worker):
    raw=json.loads(read_private(c['profile_file']))
    # The generated plan authenticates ceremonies, financial policy and all
    # dependencies. An arbitrary explicit-step file is not a graph bootstrap.
    if not isinstance(raw,dict) or 'steps' in raw:raise ValueError('CONTOUR_BOOTSTRAP_GENERATED_PROFILE_REQUIRED')
    p=build(raw)
    from signer_agent.zk_spv import is_zk
    if (p.get('bitcoin_network')!=worker['bitcoin_policy']['network'] or not is_zk(p.get('bitcoin_policy'))
            or any(p.get(k)!=worker[k] for k in ('chain_id','evm_confirmations','bitcoin_policy'))):
        raise ValueError('CONTOUR_BOOTSTRAP_SCOPE')
    return p


class BudgetedBootstrap(Bootstrap):
    def build_transaction(self,step,account,nonce):
        tx=super().build_transaction(step,account,nonce)
        self.contour.reserve(step,tx)
        return tx


class Pipeline:
    def __init__(self,runner,c,worker,request,context):
        if context['capability']!='bootstrap':raise ValueError('CONTOUR_CAPABILITY')
        self.runner,self.c,self.worker,self.request,self.context=runner,c,worker,request,context
        self.key='contour-bootstrap:'+digest(runner.prefix)
        binding=dict(config=c,profile_sha256=digest(runner.profile),worker_scope={k:worker[k]
            for k in ('chain_id','evm_confirmations','bitcoin_policy','zk_state')})
        self.record=runner.journal.put(self.key,binding)
        self.actions=self.record['result'].get('actions',{});self.reservations=self.record['result'].get('reservations',{})
        self.used=False

    def save(self):
        self.runner.journal.update(self.key,'ENROLLED',dict(actions=self.actions,reservations=self.reservations))

    def before_submit(self,steps):
        if self.used:raise Boundary()
        self.used=True;key=digest(steps)
        if key in self.actions:
            if self.actions[key]!=steps:raise ValueError('CONTOUR_BOOTSTRAP_OPERATION')
            return True
        # A process that died before its first enrollment has no spend authority
        # on recovery. Distinct later units are allowed after canonical progress.
        if self.context['action']=='recover' and not self.actions:
            raise ValueError('CONTOUR_BOOTSTRAP_ENROLLMENT_REQUIRED')
        self.actions[key]=steps;self.save();return False

    def reserve(self,step,tx):
        from signer_agent.bootstrap_create import create_address
        consumer='mining' if self.request['kind'] in ('deposit','address_deposit','withdrawal_fee') else 'escrow'
        expected=self.worker['zk_state'] if step['id']=='relay' else self.request['consumer'] if step['id']==consumer else None
        if step.get('kind')=='deploy' and expected is not None:
            sender=Account.from_key(json.loads(read_private(self.c['identity_file']))['private_key']).address
            if create_address(sender,int(tx['nonce']))!=expected:raise ValueError('CONTOUR_BOOTSTRAP_EXPECTED_ADDRESS')
        fee=int(tx.get('maxFeePerGas',tx.get('gasPrice',0)));cost=int(tx['gas'])*fee+int(tx.get('value',0))
        if (int(tx.get('chainId',0))!=int(self.worker['chain_id']) or fee<=0
                or int(tx['gas'])>int(self.c['max_tx_gas']) or cost>int(self.c['max_transaction_cost_wei'])):
            raise ValueError('CONTOUR_BOOTSTRAP_TRANSACTION_BUDGET')
        key=self.runner.prefix+':'+step['id'];cost=max(cost,int(self.reservations.get(key,'0')))
        if sum(int(v) for k,v in self.reservations.items() if k!=key)+cost>int(self.c['max_total_cost_wei']):
            raise ValueError('CONTOUR_BOOTSTRAP_TOTAL_BUDGET')
        self.reservations[key]=str(cost);self.save()

    def binding(self):
        r=self.runner
        consumer='mining' if self.request['kind'] in ('deposit','address_deposit','withdrawal_fee') else 'escrow'
        if r.contract('relay').address!=self.worker['zk_state'] or r.contract(consumer).address!=self.request['consumer']:
            raise ValueError('CONTOUR_BOOTSTRAP_GRAPH_MISMATCH')
        return dict(state=r.contract('relay').address,adapter=r.contract('spv_guard').address,
            consumer=r.contract(consumer).address,manifest_sha256=digest(r.manifest()))

    def execute(self,action):
        fd=os.open(str(self.runner.journal.path)+'.contour.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_BOOTSTRAP_BUSY') from None
            self.record=self.runner.journal.get(self.key)
            self.actions=self.record['result'].get('actions',{});self.reservations=self.record['result'].get('reservations',{})
            self.used=False;self.runner.contour=self
            return self._execute(action)
        finally:os.close(fd)

    def _execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        self.context['action']=action
        try:
            result=self.runner.apply(self.c['identity_file'],verify=action=='probe',
                before_submit=self.before_submit,defer_zk_freshness=True)
        except Boundary:
            result=dict(status='UNIT_COMPLETED')
        status=result['status']
        facts=self.binding() if status=='GRAPH_CONFIGURED_FRESHNESS_PENDING' else {}
        return dict(status='SATISFIED' if facts else 'RECOVERY_REQUIRED' if status=='NOT_SIGNED' else
            'WAITING' if status.startswith('AWAITING_') else 'ABSENT',observations=dict(reason=status,
                deployment_id=self.runner.prefix,profile_sha256=digest(self.runner.profile),
                max_new_units_per_action=1,max_transactions_per_unit=8,freshness_deferred=True,
                ready=False,production_ready=False,**facts))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('bootstrap-config','bootstrap-config-sha256','profile-sha256','worker-config',
                 'worker-config-sha256','request-file','request-sha256'):p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    from signer_agent.bitcoin_rpc import BitcoinRPC
    _protect_transport_logs()
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);context=json.loads(read_private(path))
    c=load_config(a.bootstrap_config);wc=worker_config(a.worker_config);request=json.loads(read_private(a.request_file))
    prof=profile(c,wc)
    if any(x!=y for x,y in ((digest(c),a.bootstrap_config_sha256),(digest(prof),a.profile_sha256),
                            (digest(wc),a.worker_config_sha256),(digest(request),a.request_sha256))):
        raise ValueError('CONTOUR_BOOTSTRAP_SCOPE')
    if Account.from_key(json.loads(read_private(c['identity_file']))['private_key']).address!=prof['operator']:
        raise ValueError('CONTOUR_BOOTSTRAP_OPERATOR')
    bitcoin=BitcoinRPC(c['bitcoin_rpc'],c['bitcoin_cookie'],wc['bitcoin_policy']['network'],wc['bitcoin_policy'])
    runner=BudgetedBootstrap(prof,c['state_file'],bitcoin=bitcoin)
    result=Pipeline(runner,c,wc,request,context).execute(context['action'])
    artifacts=context['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'bootstrap-evidence.json';private_write(out,result['observations'])
        artifacts={str(out.relative_to(context['directory'])):sha(out)}
    binding={k:context[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(context['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
