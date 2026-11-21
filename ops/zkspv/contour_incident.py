"""Standalone, explicitly reviewed incident transition on the original transport/status journals."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .contour import digest,private_write,sha
from .contour_calls import Calls,Required,Hold,Progress,ObservationPending
from signer_agent import address_status as policy
from signer_agent.address_status_client import Driver,request_id
from signer_agent.bootstrap import Bootstrap
from signer_agent.incident_transport import Client,load_config as transport_config,report_id,public_report,validate_report
from signer_agent.onboarding import read_private
from signer_agent.operations import canonical
from signer_agent.spv_transport import hex32,uint,message_key,KINDS


def load_config(path):
    c=json.loads(read_private(path))
    fields={'version','reporter_config','operator_config','auth_bundle_file','report_file','decision_file','accepted_decision_sha256','delivery'}
    if (not isinstance(c,dict) or set(c)!=fields or c['version']!='1'
            or any(not isinstance(c[k],str) or not Path(c[k]).is_absolute() for k in
                ('reporter_config','operator_config','auth_bundle_file','report_file','decision_file'))):raise ValueError('CONTOUR_INCIDENT_CONFIG')
    hex32(c['accepted_decision_sha256']);uint(c['delivery'],0,31);return c


def dependencies(c):
    from signer_agent.address_transport import load_config
    from signer_agent.btc_fee_reserve import load_config as fee_config
    result=[]
    if c.get('version')=='2':result.append([c['fee_reserve_config'],fee_config(c['fee_reserve_config'])])
    if c['recovery_service_config'] is not None:
        path=c['recovery_service_config'];service=load_config(path);result.append([path,service])
        if service['version']=='2':result.append([service['fee_reserve_config'],fee_config(service['fee_reserve_config'])])
    return result


def inputs(c,wc):
    from signer_agent.dkg_protocol import Manifest
    reporter=transport_config(c['reporter_config']);operator=transport_config(c['operator_config'])
    bundle=json.loads(read_private(c['auth_bundle_file']));manifest=Manifest.from_dict(bundle)
    report=json.loads(read_private(c['report_file']));decision=json.loads(read_private(c['decision_file']))
    if (set(decision)!={'version','report_id','operation_id','change'} or decision['version']!='1'
            or digest(decision)!=c['accepted_decision_sha256']):raise ValueError('CONTOUR_INCIDENT_DECISION')
    change=policy.parse_change(decision['change'])
    if (Bootstrap.wire(change)!=decision['change'] or decision['report_id']!=report_id(report)
            or change[0]!=int(report['scope']) or change[7]!=[bytes.fromhex(report[k]) for k in
                ('incident_id','reason','evidence_hash','bitcoin_block','bitcoin_tx','evm_anchor')]
            or decision['operation_id']!=request_id(operator['registry'],decision['report_id'],change[0],change[4],change[5])):
        raise ValueError('CONTOUR_INCIDENT_DECISION')
    for actor in (reporter,operator):
        if (actor['bitcoin_network']!=wc['bitcoin_policy']['network'] or any(actor[k]!=wc[k] for k in ('chain_id','evm_confirmations','bitcoin_policy'))
                or any(actor[k]!=operator[k] for k in ('registry','pool','board'))):raise ValueError('CONTOUR_INCIDENT_SCOPE')
    if (manifest.purpose!='EVM_AUTH' or manifest.chain_id!=int(wc['chain_id']) or bundle.get('role')!='conf_ceremony'
            or wc['allowed_consumers'].get(operator['pool'])!='MiningPoolV1'
            or reporter['identity_file']==operator['identity_file'] or reporter['directory']==operator['directory']):
        raise ValueError('CONTOUR_INCIDENT_SCOPE')
    return reporter,operator,bundle,report,decision,dependencies(reporter),dependencies(operator)


class Runtime:
    def __init__(self,c,wc,data):
        self.c,self.wc=c,wc
        rc,oc,self.bundle,self.report,self.decision,_,_=data
        self.actors=dict(reporter=Client(rc),operator=Client(oc));self.reporter=self.actors['reporter'];self.operator=self.actors['operator']
        self.driver=Driver(self.operator,self.bundle['onchain_board']);self.change=policy.parse_change(self.decision['change'])
        self.journal=self.operator.costs;self.job=digest(c);self.active_message=None
        if self.reporter.account.address!=self.report['reporter'] or self.reporter.account.address==self.operator.account.address:
            raise ValueError('CONTOUR_INCIDENT_REPORTER')
        self.contexts()

    def contexts(self):
        from signer_agent.address_registration import authority
        from signer_agent.zk_spv import inspect_binding
        contexts=[client.context() for client in self.actors.values()]
        if (contexts[0][4]!=contexts[1][4] or any(contexts[0][2][k].address!=contexts[1][2][k].address
                for k in ('registry','pool','common','batch'))):raise ValueError('CONTOUR_INCIDENT_GRAPH')
        b,_,ctx,_,_=contexts[1]
        validate_report(self.report,self.operator.w3,ctx,b.number)
        a=authority(self.operator.w3,ctx,self.bundle['onchain_board'],b.number,self.bundle)
        inspect_binding(self.operator.w3,self.wc['zk_state'],self.wc['bitcoin_policy'],b.number,
            adapter_address=policy.read(ctx['common'],b.number,'spv'))
        for client,context in zip(self.actors.values(),contexts):client.stable(context[0])
        return b,ctx,a

    def operation(self):
        b,ctx,a=self.contexts()
        return policy.operation_hash(self.operator.w3,ctx,a['authorization'],self.change,b.number)

    def record(self):
        r=self.operator.store.get(self.driver._key(self.decision['operation_id']))
        if r is None:return None
        r=self.driver._get(self.decision['operation_id'])
        if (len(r['attempts'])!=1 or r['attempts'][0]['ordinal']!='1' or r['attempts'][0]['change']!=self.decision['change']
                or r['report']!=public_report(self.report)):raise ValueError('CONTOUR_INCIDENT_ATTEMPT')
        return r

    def before_send(self,role,profile):
        if profile['steps'][0]['method'] in ('request','reply'):return
        b,ctx,_=self.contexts()
        self.guard(ctx,b.number)
        self.guard(ctx,'latest')
        self.operator.stable(b)

    def guard(self,ctx,number):
        from signer_agent.incident_observations import observe
        policy.pending(self.operator.w3,ctx,self.change,number)
        restrictive=self.change[4] in ((2,3) if self.change[0]==0 else (1,2))
        observe(self.operator,ctx,self.change[0],number,restrictive=restrictive)
        return policy.resume_service(self.operator,ctx,self.change,number)

    def allowed(self,role,profile):
        b,ctx,a=self.contexts();step=profile['steps'][0];target=profile['contracts'][step['contract']]
        method=step['method'];args=step['args']
        if len(profile['steps'])!=1 or step['kind']!='call' or step.get('value','0')!='0':raise ValueError('CONTOUR_INCIDENT_CALL')
        if method in ('request','reply'):
            if self.active_message is None or self.active_message[0]!=role:raise ValueError('CONTOUR_INCIDENT_ROUTE')
            _,key,p=self.active_message;h=p['header']
            expected=(["0x"+p['request_id'],h['recipient'],h['sender_epoch'],h['recipient_epoch'],h['expires_at'],'0x'+p['ciphertext']]
                if h['kind']=='REQUEST' else ['0x'+h['route'],str(KINDS[h['kind']]),h['sequence'],h['sender_epoch'],h['recipient_epoch'],h['expires_at'],'0x'+p['ciphertext']])
            if (target!={'address':self.operator.c['board'],'artifact':'BitcoinIncidentMessageBoard'} or args!=expected
                    or method!=('request' if h['kind']=='REQUEST' else 'reply')):raise ValueError('CONTOUR_INCIDENT_ROUTE')
            return
        if role!='operator':raise ValueError('CONTOUR_INCIDENT_ACTOR')
        op='0x'+self.operation().hex();r=self.record();attempt=r['attempts'][0] if r else None
        options={'createSessionFromDKG':('FROSTCoordinator',a['coordinator'].address,
                ['0x'+a['dkg_id'].hex(),op,ctx['registry'].address,self.decision['change'][6]]),
            'setStatus':('BitcoinAddressRegistry',ctx['registry'].address,[self.decision['change'],op])}
        if attempt and attempt['session'] is not None:
            options.update(publishIntent=('DKGOnchainBoard',a['board'].address,
                [attempt['session'],'0x'+canonical(attempt['intent']).encode().hex()]),
                authorizeOperation=('DKGAuthorization',a['authorization'].address,[attempt['session'],op,ctx['registry'].address]))
        expected=options.get(method)
        if expected is None or (target['artifact'],target['address'],args)!=expected:raise ValueError('CONTOUR_INCIDENT_CALL')

    def outgoing(self,client,key):
        with client.store.journal.db() as db:row=db.execute("SELECT body FROM address_packets WHERE id=? AND direction='OUT'",(key,)).fetchone()
        if not row:raise ValueError('CONTOUR_INCIDENT_ARCHIVE_REQUIRED')
        return json.loads(row[0])

    def canonical(self,client,key,p):
        b,_,_,board,_=client.context();packet=client.read(board,b,'packet',hex32(key));h=p['header']
        if not bytes(packet[8]):client.stable(b);return False
        expected=[bytes.fromhex(h['route']),KINDS[h['kind']],int(h['sequence']),h['sender'],h['recipient'],
            int(h['sender_epoch']),int(h['recipient_epoch']),int(h['expires_at']),bytes.fromhex(p['ciphertext'])]
        if list(packet)!=expected:raise ValueError('CONTOUR_INCIDENT_PACKET')
        client.stable(b);return True

    def received(self,client,key,p):
        with client.store.journal.db() as db:row=db.execute("SELECT body,state FROM address_packets WHERE id=? AND direction='IN'",(key,)).fetchone()
        if not row or row[1]!='HANDLED':return False
        local=json.loads(row[0])
        if (local['header']!=p['header'] or local['body']!=p['body']
                or local['ciphertext_hash']!=hashlib.sha256(bytes.fromhex(p['ciphertext'])).hexdigest()):raise ValueError('CONTOUR_INCIDENT_DECRYPTED')
        return True

    def prepare(self):
        r=self.record()
        if r is None:
            b,ctx,_=self.contexts();policy.pending(self.operator.w3,ctx,self.change,b.number)
            self.driver.prepare(self.decision['report_id'],policy.STATES[self.change[0]][self.change[4]],self.change[5],str(self.change[6]))
            r=self.record()
        return r

    def advance(self):
        r=self.record()
        if r is None:raise Required()
        result=self.driver.status(self.decision['operation_id'])
        if result['status']=='STATUS_CONFIRMING':return result
        if result['status'].startswith('STATUS_APPLIED'):
            if r['attempts'][0]['intent'] is None:raise Hold()
            policy.review(self.operator.w3,r['attempts'][0]['intent'],self.bundle,'latest',executable=False)
            return result
        b,ctx,_=self.contexts();self.guard(ctx,b.number);self.guard(ctx,'latest')
        result=self.driver.propose(self.decision['operation_id']);r=self.record()
        reviewed=policy.review(self.operator.w3,r['attempts'][0]['intent'],self.bundle,'latest')
        if reviewed['state']!=2:return dict(status='WAITING_EXTERNAL_INCIDENT_REVIEW_AND_FROST')
        return self.driver.execute(self.decision['operation_id'])

    def notice(self,route):
        self.operator.notify(self.decision['report_id'],route=route)
        with self.operator.store.journal.db() as db:
            rows=db.execute("SELECT id,body FROM address_packets WHERE direction='OUT' AND json_extract(body,'$.header.route')=? AND json_extract(body,'$.header.kind')='STATUS'",(route,)).fetchall()
        if not rows:raise Hold()
        key,encoded=max(rows,key=lambda row:int(json.loads(row[1])['header']['sequence']));p=json.loads(encoded)
        b,ctx,_=self.contexts()
        if any(p['body'][k]!=policy.snapshot(ctx,b.number,n) for n,k in enumerate(('custody','derivation'))):raise Hold()
        return key,p,dict(number=str(b.number),hash=b.hash.hex())


class Pipeline:
    def __init__(self,runtime,c,context):
        if context['capability']!='incident_recovery':raise ValueError('CONTOUR_CAPABILITY')
        self.r,self.c=runtime,c;self.journal=runtime.journal;self.key='contour-incident:'+digest(c)
        self.journal.put(self.key,c)
    def save(self):self.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason,**facts):return dict(status=status,observations=dict(reason=reason,
        report_id=self.r.decision['report_id'],operation_id=self.r.decision['operation_id'],
        scope=str(self.r.change[0]),next_state=policy.STATES[self.r.change[0]][self.r.change[4]],
        expected_generation=str(self.r.change[2]),ready=False,production_ready=False,
        transactions=Calls(self.r,self.record,self.save,'probe').evidence() if status=='SATISFIED' else [],**facts))
    def message(self,role,key,p,action):
        client=self.r.actors[role]
        if self.r.canonical(client,key,p):return
        if action=='probe':raise Required()
        self.r.active_message=(role,key,p)
        result=client.transmit(message_id=key)
        if result['status'] in ('ADDRESS_DELIVERY_EXPIRED','IDLE'):raise Hold()
        raise Progress()
    def receive(self,role,key,p,action):
        client=self.r.actors[role]
        if self.r.received(client,key,p):return True
        if action=='probe':raise Required()
        client.receive_once(message_id=key)
        return self.r.received(client,key,p)
    def transport(self,action):
        r=self.r
        if 'delivery' not in self.record:
            if action=='probe':raise Required()
            b,ctx,_=r.contexts();r.reporter._save_report(r.report,ctx,b.number)
            self.record['delivery']=r.reporter.send_report(r.report,r.operator.account.address,self.c['delivery']);self.save()
        delivery=self.record['delivery'];key=delivery['message_id'];packet=r.outgoing(r.reporter,key)
        if packet['body'].get('report')!=r.report or packet['body'].get('type')!='INCIDENT_REPORT':raise ValueError('CONTOUR_INCIDENT_REPORT')
        pinned=self.record.get('request_sha256')
        if pinned is not None and pinned!=digest(packet):raise Hold()
        if pinned is None:
            if action=='probe':raise Required()
            self.record['request_sha256']=digest(packet);self.save()
        self.message('reporter',key,packet,action)
        if not self.receive('operator',key,packet,action):return False
        ack=message_key(delivery['route'],'ACK','0');p=r.outgoing(r.operator,ack)
        self.message('operator',ack,p,action)
        return self.receive('reporter',ack,p,action)
    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        fd=os.open(str(self.journal.path)+'.contour.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_INCIDENT_BUSY') from None
            self.record=self.journal.get(self.key)['result']
            if not self.record:
                if action=='probe':return self.result('ABSENT','REVIEWED_INCIDENT_DELIVERY_REQUIRED')
                if action=='recover':return self.result('RECOVERY_REQUIRED','INCIDENT_ENROLLMENT_REQUIRED')
                self.record=dict(calls={});self.save()
            calls=Calls(self.r,self.record,self.save,action)
            for client in self.r.actors.values():client.transaction_controller=calls
            try:
                calls.pending()
                if not self.transport(action):return self.result('WAITING','ENCRYPTED_INCIDENT_DELIVERY_PENDING')
                if self.r.record() is None:
                    if action=='probe':raise Required()
                    self.r.prepare()
                result=self.r.advance()
                if not result['status'].startswith('STATUS_APPLIED'):return self.result('WAITING',result['status'])
                if not calls.has('setStatus'):raise Hold()
                if 'notice' not in self.record:
                    if action=='probe':raise Required()
                    key,p,anchor=self.r.notice(self.record['delivery']['route'])
                    self.record['notice']=dict(key=key,sha256=digest(p),anchor=anchor);self.save()
                notice=self.record['notice'];p=self.r.outgoing(self.r.operator,notice['key'])
                if digest(p)!=notice['sha256']:raise Hold()
                if self.r.operator.w3.eth.get_block(int(notice['anchor']['number'])).hash.hex()!=notice['anchor']['hash']:raise Hold()
                self.message('operator',notice['key'],p,action)
                if not self.receive('reporter',notice['key'],p,action):return self.result('WAITING','INCIDENT_STATUS_DELIVERY_PENDING')
                return self.result('SATISFIED','EXACT_INCIDENT_TRANSITION_AND_ENCRYPTED_STATUS_RETAINED',
                    generation=str(self.r.change[2]+1),messages=dict(request=self.record['delivery']['message_id'],status=notice['key']))
            except Required:return self.result('ABSENT','ORIGINAL_INCIDENT_OPERATION_REQUIRED')
            except ObservationPending:return self.result('WAITING','EVM_RECEIPT_PENDING')
            except Hold:return self.result('RECOVERY_REQUIRED','ORIGINAL_INCIDENT_RECOVERY_REQUIRED')
            except Progress:return self.result('ABSENT','ORIGINAL_INCIDENT_OPERATION_PROGRESS')
            finally:
                for client in self.r.actors.values():client.transaction_controller=None
                self.r.active_message=None
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('incident-config','incident-config-sha256','inputs-sha256','worker-config','worker-config-sha256'):p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    from signer_agent.zk_worker import load_config as worker_config
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path))
    c=load_config(a.incident_config);wc=worker_config(a.worker_config);data=inputs(c,wc)
    if digest(c)!=a.incident_config_sha256 or digest(wc)!=a.worker_config_sha256 or digest(data)!=a.inputs_sha256:
        raise ValueError('CONTOUR_INCIDENT_SCOPE')
    result=Pipeline(Runtime(c,wc,data),c,ctx).execute(ctx['action']);artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'incident-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
