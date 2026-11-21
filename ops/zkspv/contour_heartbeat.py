"""Initial Fresh/own-heartbeat through the original bounded worker/prover/publication engines."""
import argparse
import json
import os
from pathlib import Path
from copy import deepcopy

from .contour import digest,private_write,sha,relative_file
from .contour_worker import Pipeline as WorkerPipeline,FILES,emit
from signer_agent.zk_worker import Worker,load_config,state_wire
from signer_agent.zk_worker_prover import read
from signer_agent.onboarding import read_private


class Pipeline(WorkerPipeline):
    def save(self):
        if not getattr(self,'checking_history',False):super().save()
    def __init__(self,worker,context,invocation,require_proof=False):
        request=dict(kind='heartbeat',state=worker.c['zk_state'])
        super().__init__(worker,request,context,invocation)
        self.require_proof=require_proof
        if context['capability'] not in ('fresh_proof','state_update'):raise ValueError('CONTOUR_CAPABILITY')
        if not worker.c.get('heartbeat',{}).get('enabled'):raise ValueError('CONTOUR_HEARTBEAT_POLICY')
    def prepare_heartbeat(self,action):
        if action=='probe':return self.observation('ABSENT','ORIGINAL_HEARTBEAT_PREPARATION_REQUIRED')
        active=self.worker.inbox.get('attempt')
        if active and any(not n.startswith('heartbeat:') for n in active.get('need_ids',[])):
            return self.observation('WAITING','OTHER_WORKER_ATTEMPT')
        result=self.worker._tick(boundary='prepare');active=self.worker.inbox.get('attempt')
        if result['status']!='PROOF_PREPARED' or not active:return self.observation('WAITING',result['status'])
        if not active['need_ids'] or any(not n.startswith('heartbeat:') for n in active['need_ids']):
            return self.observation('WAITING','OWN_HEARTBEAT_REQUIRED')
        key=active['id'];base=self.worker.inbox.path/'attempts'/key
        self.lease['attempt']=dict(id=key,target_height=active['target_height'],target_hash=active['target_hash'],
            files={name:sha(relative_file(base,name)) for name in FILES})
        self.save();self.base()
        return self.observation('ABSENT','ORIGINAL_HEARTBEAT_PREPARED')
    def check_history(self):
        current=self.lease;self.checking_history=True
        try:
            for past in current.get('history',[]):
                self.lease=past;base=self.base()
                observed=self.publish('probe',base)
                if observed['observations']['reason']=='EVM_RECEIPT_PENDING':return observed
                if observed['status']!='SATISFIED':raise ValueError('CONTOUR_HEARTBEAT_HISTORY_LOST')
        finally:self.lease=current;self.checking_history=False
    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        w=self.worker
        with w.exclusive():
            if self.lease:
                pending=self.check_history()
                if pending:return pending
            b,state,_,_,core=self.current()
            active=w.inbox.get('attempt');current=state_wire(state.functions.getState().call(block_identifier=b.number))
            if active:
                native=read(relative_file(w.inbox.path,'attempts/'+active['id']+'/native.json'))
                if native['after']==current:w.retain_accepted_archive(active,native,current,b)
            fresh=(core is not None and w.aligned(state,b,core) and state.functions.isFresh().call(block_identifier=b.number)
                and not state.functions.halted().call() and state.functions.hasUsefulUpdate().call(block_identifier=b.number))
            if self.lease and self.lease.get('completed'):
                base=self.base();result=self.publish('probe',base)
                if result['status']!='SATISFIED':return result
                return self.observation('SATISFIED','ORIGINAL_FRESH_HEARTBEAT_VERIFIED',current_fresh=bool(fresh),**self.lease['completed'])
            prior=self.context.get('prior',{}).get('observations',{})
            if self.lease is None and not self.require_proof and prior.get('reason')=='CANONICAL_INITIAL_FRESH_STATE':
                from signer_agent.evm_network import verify_anchor
                from signer_agent.transaction_wait import observation_pending
                anchor=prior['anchor']
                try:
                    old=verify_anchor(w.w3,int(anchor['number']),anchor['hash'],safe=b)
                except ValueError as error:
                    if observation_pending(error):return self.observation('WAITING','EVM_RPC_BEHIND')
                    if str(error)!='EVM_REORG_RECOVERY_REQUIRED':raise
                    return self.observation('RECOVERY_REQUIRED','INITIAL_FRESH_ANCHOR_LOST')
                if (core is None
                        or state_wire(state.functions.getState().call(block_identifier=old.number))!=prior['state']
                        or not w.aligned(state,old,core) or not state.functions.isFresh().call(block_identifier=old.number)
                        or not state.functions.hasUsefulUpdate().call(block_identifier=old.number)):
                    return self.observation('RECOVERY_REQUIRED','INITIAL_FRESH_ANCHOR_LOST')
                return self.observation('SATISFIED','CANONICAL_INITIAL_FRESH_STATE',state=prior['state'],anchor=anchor,
                    fresh_proving_performed=False,current_fresh=bool(fresh))
            if self.lease is None and fresh and not self.require_proof:
                return self.observation('SATISFIED','CANONICAL_INITIAL_FRESH_STATE',
                    state=state_wire(state.functions.getState().call(block_identifier=b.number)),fresh_proving_performed=False,
                    anchor=dict(number=str(b.number),hash=b.hash.hex()),current_fresh=True)
            if self.lease is None:
                if action=='probe':return self.observation('ABSENT','INITIAL_HEARTBEAT_REQUIRED')
                if action=='recover':return self.observation('RECOVERY_REQUIRED','HEARTBEAT_ENROLLMENT_REQUIRED')
                self.lease=dict(immutable=self.immutable,admitted=True,require_proof=self.require_proof);self.save()
            if self.lease.get('require_proof')!=self.require_proof:raise ValueError('CONTOUR_HEARTBEAT_SCOPE')
            if not self.lease.get('attempt'):return self.prepare_heartbeat(action)
            base=self.base();seal=self.proof_check(base)
            if seal is None:
                result=self.proof(action,base)
                return self.observation('ABSENT','HEARTBEAT_PROOF_VERIFIED_PUBLICATION_REQUIRED') if result['status']=='SATISFIED' else result
            publish_action=action
            if action!='probe' and not self.lease.get('publish_started'):
                # This is the next reviewed phase, not an unsigned retry.
                # Persist before entering the SDK; an interruption after this
                # point can recover only its exact original signed envelope.
                self.lease['publish_started']=True;self.save();publish_action='run'
            result=self.publish(publish_action,base)
            if result['status']!='SATISFIED':return result
            b,state,_,_,core=self.current()
            if core is None or not w.aligned(state,b,core) or not state.functions.isFresh().call(block_identifier=b.number):
                if action=='probe':return self.observation('ABSENT','HEARTBEAT_CATCHUP_REQUIRED')
                if len(self.lease.get('history',[]))+1>=int(w.c['max_attempts']):
                    return self.observation('RECOVERY_REQUIRED','HEARTBEAT_CATCHUP_ATTEMPT_LIMIT')
                if state_wire(state.functions.getState().call(block_identifier=b.number))!=read(base/'native.json')['after']:
                    return self.observation('RECOVERY_REQUIRED','HEARTBEAT_CATCHUP_STATE_CHANGED')
                # Promote only this already accepted chunk through the original
                # archive path. This boundary cannot publish another transition.
                if w.inbox.get('attempt') is not None:
                    return self.observation('RECOVERY_REQUIRED','HEARTBEAT_CATCHUP_ARCHIVE_REQUIRED')
                past=deepcopy(self.lease);history=past.pop('history',[]);history.append(past)
                self.lease=dict(immutable=self.immutable,admitted=True,require_proof=self.require_proof,history=history);self.save()
                return self.observation('ABSENT','HEARTBEAT_NEXT_CATCHUP_CHUNK_REQUIRED')
            self.lease['completed']=dict(tx_hash=result['observations']['tx_hash'],
                receipt_sha256=self.lease['proof_completed']['receipt_sha256'],fresh_proving_performed=True)
            self.save();return self.observation('SATISFIED','ORIGINAL_FRESH_HEARTBEAT_VERIFIED',**self.lease['completed'])


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--worker-config',required=True);p.add_argument('--worker-config-sha256',required=True)
    p.add_argument('--require-proof',action='store_true');a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path));wc=load_config(a.worker_config)
    if digest(wc)!=a.worker_config_sha256:raise ValueError('CONTOUR_HEARTBEAT_SCOPE')
    w=Worker(wc)
    try:
        pipeline=Pipeline(w,ctx,path.parent,a.require_proof);result=pipeline.execute(ctx['action'])
        if result['status']=='SATISFIED' and not pipeline.lease:
            artifacts=ctx['prior'].get('artifacts',{})
            if not artifacts:
                out=path.parent/'fresh-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
            binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
            private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))
        else:emit(ctx,path.parent,result,pipeline)
    finally:w.close()


if __name__=='__main__':main()
