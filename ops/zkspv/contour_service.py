"""Encrypted on-chain delivery and canonical service funding for a contour.

Transport completion is delivery evidence only. Paid funding is re-read from the
canonical registry; own funding uses the original publisher balance/budget guards.
Each action starts or recovers at most one EVM operation in the selected route.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from .contour import digest, private_write
from signer_agent.onboarding import read_private
from signer_agent.spv_transport import Client,load_config as transport_config,hex32,uint,message_key,KINDS
from signer_agent.zk_worker import Worker,load_config as worker_config
from signer_agent.zk_worker_transport import WorkerTransport
from signer_agent.zk_worker_service import preflight,semantic_key

PHASES=('consumer_request','encrypted_transport','admission_payment','service_status')


def load_config(path):
    c=json.loads(read_private(path))
    fields={'version','requester_config','federation','service_identity','revision','funding_mode'}
    from web3 import Web3
    if (not isinstance(c,dict) or set(c)!=fields or c['version']!='1'
            or c['funding_mode'] not in ('PAID','CONFEDERATION') or not Path(c['requester_config']).is_absolute()
            or not isinstance(c['federation'],str) or not 1<=len(c['federation'].encode())<=128
            or not Web3.is_checksum_address(c['service_identity']) or int(c['service_identity'],16)==0):
        raise ValueError('CONTOUR_SERVICE_CONFIG')
    uint(c['revision']);return c


def validate(worker,c,requester,service,need):
    common=('chain_id','evm_confirmations','bitcoin_policy','zk_state','factory','confederation_id','board')
    if any(requester[k]!=service[k] for k in common):raise ValueError('CONTOUR_SERVICE_SCOPE')
    if any(service[k]!=worker[k] for k in ('chain_id','evm_confirmations','bitcoin_policy','zk_state')):
        raise ValueError('CONTOUR_SERVICE_SCOPE')
    for name in ('address_deposits','withdrawal_fees'):
        if requester.get(name)!=worker.get(name) or service.get(name)!=worker.get(name):raise ValueError('CONTOUR_SERVICE_SCOPE')
    if (not worker.get('transport') or need.get('consumer') not in worker['transport']['federation_consumers'].get(c['federation'],[])
            or len({str(Path(x).resolve()) for x in (worker['directory'],requester['directory'],service['directory'])})!=3):
        raise ValueError('CONTOUR_SERVICE_SCOPE')
    mode=worker.get('service',{}).get('mode');payment=requester.get('payment',{})
    if c['funding_mode']=='PAID':
        if (mode not in ('paid','paid_or_confederation') or payment.get('enabled') is not True
                or payment['binding']!=worker['service']['binding'] or not worker.get('automation',{}).get('enabled')):
            raise ValueError('CONTOUR_SERVICE_FUNDING_POLICY')
    elif mode not in ('confederation','paid_or_confederation') or payment.get('enabled'):
        raise ValueError('CONTOUR_SERVICE_FUNDING_POLICY')


class Pipeline:
    def __init__(self,worker,request,context,invocation,c,requester,transport,*,config_file=None):
        if context['capability'] not in PHASES:raise ValueError('CONTOUR_CAPABILITY')
        self.worker,self.request,self.context,self.invocation=worker,request,context,Path(invocation)
        self.c,self.requester,self.transport=c,requester,transport;self.peer=transport.client
        self.config_file=str(Path(config_file).resolve()) if config_file else None
        self.phase=context['capability']
        validate(worker.c,c,requester.c,self.peer.c,request)
        if self.peer.account.address!=c['service_identity']:raise ValueError('CONTOUR_SERVICE_IDENTITY')
        self.base_key='contour-pipeline:'+digest([context['run_id'],context['plan_sha256']])
        self.key='contour-service:'+digest([context['run_id'],context['plan_sha256']])
        self.base_immutable=dict(worker_config_sha256=digest(worker.c),scope=worker.inbox.get('scope'),request=request)
        self.immutable=dict(worker=self.base_immutable,service_config=c,requester_config_sha256=digest(requester.c),
                            peer_config_sha256=digest(self.peer.c))
        self.record=worker.inbox.get(self.key)
        if self.record and self.record['immutable']!=self.immutable:raise ValueError('CONTOUR_SERVICE_SCOPE')

    def save(self):self.worker.inbox.put(self.key,self.record)
    def need_id(self):return digest(['satoshifi.local-spv-need.v1',self.worker.inbox.get('scope'),self.request])
    def outcome(self,status,reason,**facts):
        return dict(status=status,observations=dict(reason=reason,need_id=self.need_id(),
            route=(self.record or {}).get('route'),funding_mode=self.c['funding_mode'],
            messages={k:self.record[k] for k in ('request_id','ack','initial_status','final_status') if self.record and k in self.record},
            ready=False,production_ready=False,**facts))

    def contexts(self):
        a=self.requester.context();b=self.peer.context()
        if a[2]!=b[2]:raise ValueError('CONTOUR_SERVICE_DOMAIN')
        self.requester.stable(a[0]);self.peer.stable(b[0])
        return a,b

    def outgoing(self,client,key):
        row=client.store.db.execute('SELECT body FROM outgoing WHERE id=?',(key,)).fetchone()
        if not row:raise ValueError('CONTOUR_SERVICE_ARCHIVE_REQUIRED')
        return json.loads(row[0])

    def canonical(self,client,key,packet):
        b,board,_=client.context();p=board.functions.packet(hex32(key)).call(block_identifier=b.number)
        if not bytes(p[8]):client.stable(b);return False
        h=packet['header']
        expected=[bytes.fromhex(h['route']),KINDS[h['kind']],int(h['sequence']),h['sender'],h['recipient'],
                  int(h['sender_epoch']),int(h['recipient_epoch']),int(h['expires_at']),bytes.fromhex(packet['ciphertext'])]
        if list(p)!=expected:raise ValueError('CONTOUR_SERVICE_PACKET_CONFLICT')
        client.stable(b);return True

    def received(self,client,key,packet):
        row=client.store.db.execute('SELECT body,state FROM incoming WHERE id=?',(key,)).fetchone()
        if not row or row[1] in ('QUARANTINED','ORPHANED','REJECTED'):return False
        r=json.loads(row[0]);h=packet['header']
        if (r.get('header')!=h or r.get('body')!=packet['body']
                or r.get('ciphertext_sha256')!=hashlib.sha256(bytes.fromhex(packet['ciphertext'])).hexdigest()):
            raise ValueError('CONTOUR_SERVICE_DECRYPTED_CONFLICT')
        original=self.outgoing(self.requester,self.record['request_id'])
        if r.get('route')!=dict(requester=self.requester.account.address,service=self.peer.account.address,
            federation=self.c['federation'],request_id=original['request_id']):raise ValueError('CONTOUR_SERVICE_ROUTE')
        return True

    def original(self):
        packet=self.outgoing(self.requester,self.record['request_id'])
        if digest(packet)!=self.record['request_sha256']:raise ValueError('CONTOUR_SERVICE_PACKET_CONFLICT')
        return packet

    def initial(self):
        if not self.record or not self.record.get('request_id'):return False
        original=self.original();route=self.record['route']
        if not self.canonical(self.requester,self.record['request_id'],original):return False
        if not self.received(self.peer,self.record['request_id'],original):return False
        link=self.worker.inbox.get('remote:'+route)
        if not link:return False
        if (link['local_need_id']!=self.need_id() or link['operation_id']!=original['body']['operation_id']
                or link['request_id']!=original['request_id'] or link['deadline']!=int(original['body']['service_deadline'])
                or link['federation']!=self.c['federation'] or link.get('payment_request')!=original['body'].get('payment_request')):
            raise ValueError('CONTOUR_SERVICE_ROUTE')
        for slot in ('ack','initial_status'):
            key=self.record.get(slot)
            if not key:return False
            packet=self.outgoing(self.peer,key);body=packet['body']
            if (body.get('request_id')!=original['request_id'] or body.get('operation_id')!=original['body']['operation_id']
                    or body.get('local_need_id')!=self.need_id() or body.get('service_deadline')!=original['body']['service_deadline']
                    or slot=='ack' and body.get('status')!='ACCEPTED'):
                raise ValueError('CONTOUR_SERVICE_REPLY')
            if not self.canonical(self.peer,key,packet) or not self.received(self.requester,key,packet):return False
        return True

    def adopt(self):
        rows={k:r for k,r,_ in self.worker.inbox.needs()}
        if rows.get(self.need_id())!=self.request:raise ValueError('CONTOUR_SERVICE_INBOX_REQUIRED')
        saved=self.worker.inbox.get(self.base_key)
        if saved and saved['immutable']!=self.base_immutable:raise ValueError('CONTOUR_SERVICE_SCOPE')
        saved=saved or dict(immutable=self.base_immutable)
        saved['admitted']=True
        if self.config_file:
            marker=dict(config=self.config_file,sha256=digest(self.c),requester_sha256=digest(self.requester.c),peer_sha256=digest(self.peer.c))
            if saved.get('service') not in (None,marker):raise ValueError('CONTOUR_SERVICE_SCOPE')
            saved['service']=marker
        self.worker.inbox.put(self.base_key,saved)

    def latch(self,kind,request,job):
        name=kind+':'+digest(request);actions=self.record.setdefault('actions',{})
        binding=dict(kind=kind,request_sha256=digest(request),job=job)
        if name in actions:
            if actions[name]!=binding:raise ValueError('CONTOUR_SERVICE_OPERATION')
            return True
        actions[name]=binding;self.save();return False

    def send(self,client,key):
        packet=self.outgoing(client,key)
        if self.canonical(client,key,packet):
            client.transmit(message_id=key,recover_only=True)
            return self.outcome('ABSENT','PACKET_CONFIRMED')
        b,_,_=client.context();retry=client.store.get('retry:'+key) or dict(next_at=0,attempts=0)
        if b.timestamp<retry['next_at']:return self.outcome('WAITING','TRANSPORT_BACKOFF')
        if retry['attempts']>=int(client.c['max_publish_attempts']):return self.outcome('RECOVERY_REQUIRED','TRANSPORT_ATTEMPT_LIMIT')
        recovery=self.latch('message',packet,key)
        result=client.transmit(message_id=key,recover_only=recovery)
        return self.outcome('RECOVERY_REQUIRED' if result['status']=='NOT_SIGNED' else 'ABSENT',
                            'UNSIGNED_MESSAGE_RECOVERY' if result['status']=='NOT_SIGNED' else result['status'])

    def consumer(self,action):
        if self.record and self.record.get('request_id'):
            self.original();return self.outcome('SATISFIED','ENCRYPTED_REQUEST_PERSISTED')
        if action=='probe':return self.outcome('ABSENT','REQUEST_NOT_QUEUED')
        b,state,adapter,_,core=self.worker.context()
        self.worker.observe_need(self.request,b,adapter,core);self.worker.stable(b)
        if self.record is None:self.record=dict(immutable=self.immutable,actions={});self.save()
        key,packet=self.requester.send_request(self.request,self.c['federation'],self.c['service_identity'],self.c['revision'])
        self.record.update(request_id=key,route=packet['header']['route'],request_sha256=digest(packet));self.save()
        return self.outcome('SATISFIED','ENCRYPTED_REQUEST_PERSISTED')

    def status_slot(self,slot):
        if self.record.get(slot):return self.record[slot]
        if slot=='ack':key=message_key(self.record['route'],'ACK','0')
        else:
            reason='CONTOUR_INITIAL_STATUS' if slot=='initial_status' else 'CONTOUR_STATE_STATUS'
            def matching():
                return [k for k,raw in self.peer.store.db.execute("SELECT id,body FROM outgoing WHERE json_extract(body,'$.header.route')=? AND json_extract(body,'$.header.kind')='STATUS' ORDER BY rowid",(self.record['route'],))
                        if json.loads(raw)['body'].get('result')==reason]
            candidates=matching()
            if not candidates or slot!='initial_status':
                self.transport.updates(dict(status=reason),routes=[self.record['route']]);candidates=matching()
            if not candidates:raise ValueError('CONTOUR_SERVICE_STATUS_LIMIT')
            key=candidates[0] if slot=='initial_status' else candidates[-1]
        self.outgoing(self.peer,key);self.record[slot]=key;self.save();return key

    def delivery(self,action):
        self.peer.poll();self.requester.poll();self.requester.process_replies()
        if self.initial():self.adopt();return self.outcome('SATISFIED','REQUEST_ACK_STATUS_DELIVERED')
        if action=='probe':return self.outcome('ABSENT','DELIVERY_PENDING')
        original=self.original()
        if not self.canonical(self.requester,self.record['request_id'],original):return self.send(self.requester,self.record['request_id'])
        self.transport.ingest(routes=[self.record['route']])
        if not self.worker.inbox.get('remote:'+self.record['route']):return self.outcome('WAITING','SERVICE_VALIDATION_PENDING')
        for slot in ('ack','initial_status'):
            key=self.status_slot(slot);packet=self.outgoing(self.peer,key)
            if not self.canonical(self.peer,key,packet):return self.send(self.peer,key)
        self.requester.poll();self.requester.process_replies()
        if self.initial():self.adopt();return self.outcome('SATISFIED','REQUEST_ACK_STATUS_DELIVERED')
        return self.outcome('ABSENT','DECRYPTION_PENDING')

    def pending_payment(self):
        for key,record,_ in self.requester.store.incoming('STATUS'):
            if record.get('header',{}).get('route')!=self.record['route']:continue
            saved=self.requester.store.get('payment:'+key)
            if not saved or not saved.get('request'):continue
            if saved.get('status')=='CONFIRMED':
                receipt=saved.get('receipt',{})
                if not receipt.get('transaction_hash') or not receipt.get('block_hash'):return True
                from signer_agent.bootstrap import Bootstrap
                check=Bootstrap.__new__(Bootstrap);check.w3=self.requester.w3;check.profile=self.requester.c
                observed=dict(tx_hash=receipt['transaction_hash'],block_hash=receipt['block_hash'])
                if 'block_number' in receipt:observed['block_number']=receipt['block_number']
                verified=check.observe_receipt(observed)
                if verified is None:return True
            elif saved.get('status') not in ('COVERED','STALE_QUOTE','REJECTED'):return True
        return False

    def funding(self):
        if (self.c['funding_mode']=='PAID' and self.worker.inbox.get('auto_admission')) or self.pending_payment():return None
        b,state,adapter,_,core=self.worker.context()
        observation=self.worker.observe_need(self.request,b,adapter,core)
        if observation['status']=='READY' and not self.worker.aligned(state,b,core):observation['status']='NEEDS_STATE'
        self.worker.stable(b)
        from signer_agent.zk_worker_service import source
        coverage=self.worker.inbox.get('auto_coverage') or {}
        if (self.c['funding_mode']=='PAID' and self.need_id() in coverage.get('need_ids',[])
                and not self.worker.inbox.get('service:'+self.need_id())):
            from signer_agent.gas_treasury import GasTreasuryClient
            client=GasTreasuryClient(self.worker.w3,self.worker.c['service']['binding'])
            if client.status('0x'+coverage['job'])['paid']:
                from signer_agent.zk_worker_service import attach
                primary=coverage['semantic_need_ids'][0]
                if not self.worker.inbox.get('service:'+primary):
                    attach(self.worker,dict(service_job_id=coverage['job'],need_ids=coverage['semantic_need_ids']))
                original=self.worker.inbox.get('service:'+primary)
                if (original['service_job_id']!=coverage['job'] or original['need_ids']!=coverage['semantic_need_ids']):
                    raise ValueError('CONTOUR_SERVICE_COVERAGE')
                if self.need_id()!=primary:self.worker.inbox.put('service:'+self.need_id(),dict(original,coverage_job=coverage['job']))
        historical=source(self.worker,[self.need_id()],allow_unpaid=True)
        if observation['status'] in ('READY','COMPLETED') and historical['source']!='PAID_SERVICE':
            return dict(source='COVERED_BY_STATE',jobs=[],paid_service=False)
        decision=historical if observation['status'] in ('READY','COMPLETED') else preflight(self.worker,[self.need_id()])
        if self.c['funding_mode']=='PAID' and decision['source']!='PAID_SERVICE':return None
        if decision['source']=='PAID_SERVICE':
            linked=self.worker.inbox.get('service:'+self.need_id());coverage=self.worker.inbox.get('auto_coverage') or {}
            if self.need_id() not in linked['need_ids']:
                if (linked.get('coverage_job')!=linked['service_job_id'] or coverage.get('job')!=linked['service_job_id']
                        or self.need_id() not in coverage.get('need_ids',[]) or linked['need_ids']!=coverage['semantic_need_ids']
                        or observation['status']=='NEEDS_STATE' and observation['height']+observation['depth']-1>coverage['target_height']
                        or self.worker.bitcoin.call('getblockhash',coverage['target_height'])!=coverage['target_hash']):
                    raise ValueError('CONTOUR_SERVICE_COVERAGE')
        return dict(decision,paid_service=decision['source']=='PAID_SERVICE')

    def admission(self,action):
        self.requester.poll();self.requester.process_replies()
        from signer_agent.zk_service_issuer import tick as issue
        from signer_agent.spv_automation import payer_tick
        try:funding=self.funding()
        except ValueError as exc:
            if str(exc)!='ZK_SERVICE_AWAITING_PAYMENT':raise
            funding=None
        if funding:
            self.record['funding_observed']=funding;self.save()
            return self.outcome('SATISFIED','FUNDING_RECHECKED',funding=funding)
        if action=='probe':return self.outcome('ABSENT','FUNDING_PENDING')
        if self.c['funding_mode']=='CONFEDERATION':return self.outcome('WAITING','FUNDING_OBSERVATION_PENDING')
        if self.pending_payment():
            result=payer_tick(self.requester,routes=[self.record['route']],before_submit=self.latch)
            return self.outcome('RECOVERY_REQUIRED' if result['status']=='NOT_SIGNED' else 'ABSENT',result['status'])
        result=issue(self.worker,self.transport,routes=[self.record['route']],before_submit=self.latch)
        if result['status']!='IDLE':
            return self.outcome('RECOVERY_REQUIRED' if result['status']=='NOT_SIGNED' else 'ABSENT',result['status'])
        for key,encoded in self.peer.store.db.execute("SELECT id,body FROM outgoing WHERE state!='CONFIRMED' AND json_extract(body,'$.header.route')=? ORDER BY rowid",(self.record['route'],)):
            packet=json.loads(encoded)
            if packet['body'].get('status')=='QUOTE':return self.send(self.peer,key)
        self.requester.poll();self.requester.process_replies()
        result=payer_tick(self.requester,routes=[self.record['route']],before_submit=self.latch)
        return self.outcome('RECOVERY_REQUIRED' if result['status']=='NOT_SIGNED' else 'ABSENT',result['status'])

    def status(self,action):
        self.requester.poll();self.requester.process_replies()
        key=self.record.get('final_status')
        if key:
            packet=self.outgoing(self.peer,key)
            if self.canonical(self.peer,key,packet) and self.received(self.requester,key,packet):
                return self.outcome('SATISFIED','STATE_STATUS_DELIVERED',reported_status=packet['body']['status'])
        if action=='probe':return self.outcome('ABSENT','STATE_STATUS_PENDING')
        if key is None:
            b,state,adapter,_,core=self.worker.context(allow_halt=True)
            observed=dict(status='HALTED') if core is None else self.worker.observe_need(self.request,b,adapter,core)
            if observed['status']=='READY' and not self.worker.aligned(state,b,core):observed['status']='NEEDS_STATE'
            self.worker.stable(b);self.worker.inbox.status(self.need_id(),observed['status'])
            key=self.status_slot('final_status')
        return self.send(self.peer,key)

    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        with self.worker.exclusive():
            self.contexts()
            if self.phase=='consumer_request':return self.consumer(action)
            if not self.record or not self.record.get('request_id'):return self.outcome('WAITING','ENCRYPTED_REQUEST_REQUIRED')
            if self.phase=='encrypted_transport':return self.delivery(action)
            self.peer.poll();self.requester.poll();self.requester.process_replies()
            if not self.initial():return self.outcome('RECOVERY_REQUIRED','ORIGINAL_DELIVERY_REQUIRED')
            self.adopt()
            return self.admission(action) if self.phase=='admission_payment' else self.status(action)


def open_pipeline(worker,request,context,invocation,filename,expected,requester_expected,peer_expected):
    c=load_config(filename)
    if digest(c)!=expected:raise ValueError('CONTOUR_SERVICE_SCOPE')
    rc=transport_config(c['requester_config'])
    pc=transport_config(worker.c['transport']['config_file'])
    if digest(rc)!=requester_expected or digest(pc)!=peer_expected:raise ValueError('CONTOUR_SERVICE_SCOPE')
    requester=Client(rc);transport=None
    try:
        transport=WorkerTransport(worker)
        if digest(transport.client.c)!=peer_expected:raise ValueError('CONTOUR_SERVICE_SCOPE')
        return Pipeline(worker,request,context,invocation,c,requester,transport,config_file=filename)
    except BaseException:
        requester.close()
        if transport:transport.close()
        raise


def require(worker,request,context,invocation,marker):
    p=open_pipeline(worker,request,dict(context,capability='admission_payment'),invocation,
        marker['config'],marker['sha256'],marker['requester_sha256'],marker['peer_sha256'])
    try:
        p.contexts();p.peer.poll();p.requester.poll();p.requester.process_replies()
        if not p.initial():raise ValueError('CONTOUR_SERVICE_FUNDING_REQUIRED')
        try:funded=p.funding()
        except ValueError as exc:
            if str(exc)=='ZK_SERVICE_AWAITING_PAYMENT':raise ValueError('CONTOUR_SERVICE_FUNDING_REQUIRED') from None
            raise
        if not funded:raise ValueError('CONTOUR_SERVICE_FUNDING_REQUIRED')
    finally:p.requester.close();p.transport.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase',required=True,choices=PHASES)
    for name in ('worker-config','request-file','worker-config-sha256','request-sha256','service-config',
                 'service-config-sha256','requester-config-sha256','peer-config-sha256'):p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs()
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT'])
    if path.stat().st_size>1024*1024 or Path(a.request_file).stat().st_size>65536:raise ValueError('CONTOUR_RESPONSE')
    context=json.loads(path.read_text());request=json.loads(read_private(a.request_file));wc=worker_config(a.worker_config)
    if context['capability']!=a.phase:raise ValueError('CONTOUR_CAPABILITY')
    if digest(request)!=a.request_sha256 or digest(wc)!=a.worker_config_sha256:raise ValueError('CONTOUR_SERVICE_SCOPE')
    worker=Worker(wc);pipeline=None
    try:
        pipeline=open_pipeline(worker,request,context,path.parent,a.service_config,a.service_config_sha256,a.requester_config_sha256,a.peer_config_sha256)
        result=pipeline.execute(context['action']);artifacts=context['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
        if result['status']=='SATISFIED' and not artifacts:
            out=path.parent/'service-evidence.json';private_write(out,result['observations'])
            from .contour import sha
            artifacts={str(out.relative_to(context['directory'])):sha(out)}
        binding={k:context[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
        private_write(context['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))
    finally:
        if pipeline:pipeline.requester.close();pipeline.transport.close()
        worker.close()


if __name__=='__main__':main()
