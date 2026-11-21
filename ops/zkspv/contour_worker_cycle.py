"""Bounded consumer catch-up retaining every original proof and publication."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path

from .contour import digest,sha,relative_file
from .contour_worker import Pipeline as Original,FILES,emit
from signer_agent.zk_worker import Worker,load_config,state_wire
from signer_agent.zk_worker_prover import read
from signer_agent.onboarding import read_private


class Pipeline(Original):
    def __init__(self,worker,request,context,invocation):
        super().__init__(worker,request,context,invocation)
        if self.phase not in ('fresh_proof','state_update'):raise ValueError('CONTOUR_CAPABILITY')
        self.outer=self.lease;self.cycle=(self.outer or {}).get('cycle');self.verifying_history=False
        self.lease=(self.cycle or {}).get('current')

    def save(self):
        if self.verifying_history:return
        self.cycle['current']=deepcopy(self.lease);self.outer['cycle']=self.cycle
        self.worker.inbox.put(self.key,self.outer)

    def retained_leases(self):return [*self.cycle['history'],self.lease]

    def verify_history(self):
        active=self.lease;self.verifying_history=True
        try:
            for entry in self.cycle['history']:
                self.lease=entry
                observed=self.publish('probe',self.base())
                if observed['observations']['reason']=='EVM_RECEIPT_PENDING':return observed
                if observed['status']!='SATISFIED':raise ValueError('CONTOUR_CYCLE_HISTORY_LOST')
        finally:self.lease=active;self.verifying_history=False

    def enough(self,b,state,adapter,core):
        if core is None or not self.worker.aligned(state,b,core):return False
        observed=self.worker.observe_need(self.request,b,adapter,core)
        current=state_wire(state.functions.getState().call(block_identifier=b.number))
        covered=(observed['status'] in ('READY','COMPLETED') or observed['status']=='NEEDS_STATE'
            and int(observed['height'])+int(observed['depth'])-1<=int(current[3]))
        return bool(covered and state.functions.isFresh().call(block_identifier=b.number) and not state.functions.halted().call())

    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        w=self.worker
        with w.exclusive():
            b,state,adapter,_,core=self.current()
            if not self.outer or not self.outer.get('admitted') or not self.outer.get('attempt'):
                return self.observation('WAITING','ORIGINAL_CONSUMER_PREPARATION_REQUIRED')
            if self.cycle is None:
                if self.phase=='state_update':return self.observation('WAITING','CONSUMER_PROOF_CYCLE_REQUIRED')
                if action=='probe':return self.observation('ABSENT','CONSUMER_PROOF_CYCLE_REQUIRED')
                self.cycle=dict(history=[])
                self.lease={k:deepcopy(v) for k,v in self.outer.items() if k!='cycle'};self.save()
            pending=self.verify_history()
            if pending:return pending
            if self.cycle.get('completed'):
                result=self.publish('probe',self.base())
                if result['status']!='SATISFIED':return result
                return self.observation('SATISFIED','ORIGINAL_CONSUMER_CATCHUP_VERIFIED',
                    chunks=len(self.cycle['history'])+1,**self.cycle['completed'])
            if self.phase=='state_update':return self.observation('WAITING','CONSUMER_PROOF_CYCLE_REQUIRED')
            active=w.inbox.get('attempt');current=state_wire(state.functions.getState().call(block_identifier=b.number))
            if active:
                native=read(relative_file(w.inbox.path,'attempts/'+active['id']+'/native.json'))
                if native['after']==current:w.retain_accepted_archive(active,native,current,b)
            if not self.lease.get('attempt'):
                if action=='probe':return self.observation('ABSENT','NEXT_CONSUMER_CHUNK_PREPARATION_REQUIRED')
                self.service_guard()
                active=w.inbox.get('attempt')
                if active and self.need_id() not in active.get('need_ids',[]):return self.observation('WAITING','OTHER_WORKER_ATTEMPT')
                result=w._tick(boundary='prepare');active=w.inbox.get('attempt')
                if result['status']!='PROOF_PREPARED' or not active or self.need_id() not in active.get('need_ids',[]):
                    return self.observation('WAITING',result['status'])
                base=w.inbox.path/'attempts'/active['id']
                self.lease['attempt']=dict(id=active['id'],target_height=active['target_height'],target_hash=active['target_hash'],
                    files={n:sha(relative_file(base,n)) for n in FILES});self.save();self.base()
                return self.observation('ABSENT','NEXT_CONSUMER_CHUNK_PREPARED')
            base=self.base()
            if self.proof_check(base) is None:
                result=self.proof(action,base)
                return self.observation('ABSENT','CONSUMER_CHUNK_PROOF_VERIFIED_PUBLICATION_REQUIRED') if result['status']=='SATISFIED' else result
            publish_action=action
            if action!='probe' and not self.lease.get('publish_started'):
                self.lease['publish_started']=True;self.save();publish_action='run'
            result=self.publish(publish_action,base)
            if result['status']!='SATISFIED':return result
            b,state,adapter,_,core=self.current()
            if self.enough(b,state,adapter,core):
                active=w.inbox.get('attempt');current=state_wire(state.functions.getState().call(block_identifier=b.number))
                if active and active['id']==self.lease['attempt']['id'] and current==read(base/'native.json')['after']:
                    w.retain_accepted_archive(active,read(base/'native.json'),current,b)
                self.cycle['completed']=dict(tx_hash=result['observations']['tx_hash'],
                    receipt_sha256=self.lease['proof_completed']['receipt_sha256'],fresh_proving_performed=True)
                self.save()
                return self.observation('SATISFIED','ORIGINAL_CONSUMER_CATCHUP_VERIFIED',chunks=len(self.cycle['history'])+1,**self.cycle['completed'])
            if action=='probe':return self.observation('ABSENT','CONSUMER_CATCHUP_REQUIRED')
            if len(self.cycle['history'])+1>=int(w.c['max_attempts']):return self.observation('RECOVERY_REQUIRED','CONSUMER_CATCHUP_ATTEMPT_LIMIT')
            current=state_wire(state.functions.getState().call(block_identifier=b.number))
            if current!=read(base/'native.json')['after']:return self.observation('RECOVERY_REQUIRED','CONSUMER_CATCHUP_STATE_CHANGED')
            active=w.inbox.get('attempt')
            if active:
                if active['id']!=self.lease['attempt']['id']:return self.observation('WAITING','OTHER_WORKER_ATTEMPT')
                w.retain_accepted_archive(active,read(base/'native.json'),current,b)
            self.cycle['history'].append(deepcopy(self.lease))
            self.lease={k:deepcopy(self.outer[k]) for k in ('immutable','admitted','service') if k in self.outer};self.save()
            return self.observation('ABSENT','NEXT_CONSUMER_CHUNK_REQUIRED')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--phase',required=True,choices=('fresh_proof','state_update'))
    for name in ('worker-config','request-file','worker-config-sha256','request-sha256'):p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path))
    wc=load_config(a.worker_config);request=json.loads(read_private(a.request_file))
    if ctx['capability']!=a.phase or digest(wc)!=a.worker_config_sha256 or digest(request)!=a.request_sha256:raise ValueError('CONTOUR_WORKER_SCOPE')
    worker=Worker(wc)
    try:
        pipeline=Pipeline(worker,request,ctx,path.parent);result=pipeline.execute(ctx['action']);emit(ctx,path.parent,result,pipeline)
    finally:worker.close()


if __name__=='__main__':main()
