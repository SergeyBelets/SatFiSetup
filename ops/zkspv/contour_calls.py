"""Bounded calls from original SDKs, retaining each actor's budget and nonce journal."""
from pathlib import Path

from .contour import digest
from .contour_tx import original,exact
from signer_agent.bootstrap import Bootstrap
from signer_agent.spv_automation import Budget


class Required(Exception):pass
class Hold(Exception):pass
class Progress(Exception):pass


def evidence(profile,entry,receipt):
    if receipt is None or entry['status']!='CONFIRMED':raise Hold()
    return dict(profile=profile,original_transaction=entry,receipt=dict(status=int(receipt['status']),
        block_number=str(receipt['blockNumber']),block_hash=bytes(receipt['blockHash']).hex(),tx_hash=entry['result']['tx_hash']))


class Calls:
    def __init__(self,runtime,record,save,action):
        self.runtime,self.record,self.save,self.action=runtime,record,save,action;self.used=False

    def runner(self,profile,actor,budget=None,budget_key=None):
        class Sender(Bootstrap):
            def build_transaction(self,step,account,nonce):
                tx=super().build_transaction(step,account,nonce)
                if budget is None:raise Hold()
                budget.reserve(budget_key,tx);return tx
        return Sender(profile,str(Path(actor.c['identity_file']).resolve())+'.transactions.sqlite')

    def entry(self,profile,actor):
        return original(str(Path(actor.c['identity_file']).resolve())+'.transactions.sqlite',
                        profile['deployment_id'],profile['steps'][0],actor.c['identity_file'])

    def receipt(self,profile,actor,entry):
        from web3.exceptions import TransactionNotFound
        target=profile['contracts'][profile['steps'][0]['contract']]
        exact(entry,profile['steps'][0],actor.c['chain_id'],target['address'],target['artifact'],actor.c['identity_file'],actor.c)
        checker=self.runner(profile,actor)
        try:return checker.verify_receipt(entry['result'])
        except TransactionNotFound:return None

    def apply(self,profile,actor,budget=None,key=None,*,recover=False):
        runner=self.runner(profile,actor,budget,key)
        result=runner.apply(actor.c['identity_file'],before_submit=(lambda steps:True) if recover else None)
        if result['status']=='NOT_SIGNED':raise Hold()
        if result['status']!='VERIFIED':raise Progress()
        receipt=self.receipt(profile,actor,self.entry(profile,actor))
        if receipt is None:raise Progress()
        return receipt

    def pending(self):
        for operation in self.record.get('calls',{}).values():
            actor=self.runtime.actors[operation['actor']];profile=operation['profile'];entry=self.entry(profile,actor)
            if not entry['result'].get('raw_transaction'):raise Hold()
            receipt=self.receipt(profile,actor,entry)
            if receipt is not None and entry['status']=='CONFIRMED':continue
            if receipt is None and entry['status']=='CONFIRMED':raise Hold()
            if self.action=='probe':raise Required()
            self.used=True;self.apply(profile,actor,recover=True);raise Progress()

    def send(self,actor_name,profile,budget,key):
        if self.used:raise Progress()
        actor=self.runtime.actors[actor_name]
        self.runtime.allowed(actor_name,profile)
        entry=self.entry(profile,actor)
        if entry['result'].get('raw_transaction'):
            receipt=self.receipt(profile,actor,entry)
            if receipt is not None and entry['status']=='CONFIRMED':
                identifier=digest([actor.c['identity_file'],profile['deployment_id']]);saved=dict(actor=actor_name,profile=profile)
                old=self.record.setdefault('calls',{}).get(identifier)
                if old not in (None,saved):raise ValueError('CONTOUR_CALL_SCOPE')
                if old is None:self.record['calls'][identifier]=saved;self.save()
                return receipt
        if self.action=='probe':raise Required()
        identifier=digest([actor.c['identity_file'],profile['deployment_id']]);saved=dict(actor=actor_name,profile=profile)
        previous=self.record.setdefault('calls',{}).get(identifier)
        if previous is None:self.record['calls'][identifier]=saved;self.save();recover=False
        else:
            if previous!=saved:raise ValueError('CONTOUR_CALL_SCOPE')
            if not entry['result'].get('raw_transaction'):raise Hold()
            recover=True
        self.used=True
        return self.apply(profile,actor,budget,key,recover=recover)

    def submit_fee(self,driver,key,profile,budget):
        role=next((name for name,i in self.runtime.actors.items() if i is driver.i),None)
        if role is None:raise ValueError('CONTOUR_CALL_ACTOR')
        runtime=self.runtime
        class OriginalBudget:
            def reserve(self,operation,tx):
                guard=getattr(runtime,'before_send',None)
                if guard is not None:guard(role,profile)
                budget.reserve(operation,tx)
        return self.send(role,profile,OriginalBudget(),key)

    def submit(self,registry,profile):
        actor=self.runtime.actors['operator'];key=profile['deployment_id']
        budget=Budget(actor.c['identity_file'],'contour-btc:'+actor.c['reserve'],actor.c,self.runtime.job)
        return self.send('operator',profile,budget,key)

    def submit_address(self,client,key,profile,*,receipt=False):
        role=next((name for name,actor in self.runtime.actors.items() if actor is client),None)
        if role is None:raise ValueError('CONTOUR_CALL_ACTOR')
        controller=self
        class OriginalBudget:
            def reserve(self,operation,tx):
                guard=getattr(controller.runtime,'before_send',None)
                if guard is not None:guard(role,profile)
                client.reserve_cost(operation,tx)
        result=self.send(role,profile,OriginalBudget(),key)
        return result if receipt else dict(status='VERIFIED',message_id=key,ready=False)

    def has(self,method):return any(r['profile']['steps'][0]['method']==method for r in self.record.get('calls',{}).values())

    def evidence(self):
        rows=[]
        for operation in self.record.get('calls',{}).values():
            actor=self.runtime.actors[operation['actor']];profile=operation['profile'];entry=self.entry(profile,actor)
            rows.append(evidence(profile,entry,self.receipt(profile,actor,entry)))
        return rows
