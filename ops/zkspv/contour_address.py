"""Original encrypted address request→registration→watch/status workflow.

External EVM_AUTH participants remain external. Each run delegates at most one
original EOA transaction; probes do not enroll requests, scan, sign or send.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
from .contour import digest,private_write,sha
from .contour_calls import Calls,Required,Hold,Progress
from signer_agent.onboarding import read_private
from signer_agent.address_transport import Client,load_config as address_config
from signer_agent.address_registration_client import Driver
from signer_agent.deposit_intent import DepositIntentV1
from signer_agent.bitcoin_rpc import BitcoinRPC
from signer_agent.operations import Journal
from web3 import Web3


FIELDS={'version','requester_config','service_config','operator_config','registration_board','intent_file',
        'accepted_intent_sha256','deadline','max_scan_blocks','recipient_mode','expected_status'}
MODES=('ordinary','wallet-v1','advanced-contract')


def load_config(path):
    c=json.loads(read_private(path))
    if not isinstance(c,dict) or set(c)!=FIELDS or c.get('version')!='1' or c['recipient_mode'] not in MODES:
        raise ValueError('CONTOUR_ADDRESS_CONFIG')
    from signer_agent.spv_transport import hex32,uint
    from web3 import Web3
    hex32(c['accepted_intent_sha256']);uint(c['deadline'],1,2**64-1);uint(c['max_scan_blocks'],1,256)
    if not Web3.is_checksum_address(c['registration_board']) or c['expected_status'] not in ('READY','FEE_SERVICE_PROFILE_REQUIRED','BTC_FEE_DERIVATION_REQUIRED','DERIVATION_QUALIFICATION_REQUIRED','RECOVERY_REQUIRED','RESERVE_LOW','FEE_CAP_PROFILE_REQUIRED','SUFFICIENT_QUALIFICATION_REQUIRED','OBSERVATION_UNAVAILABLE','RECONCILIATION_REQUIRED'):
        raise ValueError('CONTOUR_ADDRESS_CONFIG')
    if any(not isinstance(c[k],str) or not Path(c[k]).is_absolute() for k in ('requester_config','service_config','operator_config','intent_file')):
        raise ValueError('CONTOUR_ADDRESS_CONFIG')
    return c


def inputs(c,wc,need):
    configs=[address_config(c[k]) for k in ('requester_config','service_config','operator_config')]
    body=json.loads(read_private(c['intent_file']))
    if not isinstance(body,dict) or set(body)!={'intent','recipient_selection','accepted_recipient_sha256'} or digest(body)!=c['accepted_intent_sha256']:
        raise ValueError('CONTOUR_ADDRESS_INTENT')
    intent=DepositIntentV1.decode(bytes.fromhex(body['intent']))
    selection=body['recipient_selection']
    if (not isinstance(selection,dict) or digest(selection)!=body['accepted_recipient_sha256']
            or selection.get('mode')!=c['recipient_mode'] or selection.get('recipient')!=Web3.to_checksum_address(intent.recipient)):
        raise ValueError('CONTOUR_ADDRESS_RECIPIENT')
    if need.get('kind')!='address_deposit' or need.get('intent_id')!=intent.intent_id.hex():raise ValueError('CONTOUR_ADDRESS_SCOPE')
    for cfg in configs:
        if cfg['bitcoin_network']!=wc['bitcoin_policy']['network'] or any(cfg[k]!=wc[k] for k in ('chain_id','bitcoin_policy','evm_confirmations')):
            raise ValueError('CONTOUR_ADDRESS_SCOPE')
        if any(cfg[k]!=configs[0][k] for k in ('board','registry','pool')) or cfg['pool']!=need['consumer']:raise ValueError('CONTOUR_ADDRESS_SCOPE')
    return configs,body


class Runtime:
    def __init__(self,c,wc,need,data):
        self.c,self.wc,self.need=c,wc,need;configs,self.body=data
        self.actors={name:Client(cfg) for name,cfg in zip(('requester','service','operator'),configs)}
        self.journal=Journal(Path(configs[2]['directory'])/'contour-address.sqlite');self.job=digest([c,need])
        self.intent=DepositIntentV1.decode(bytes.fromhex(self.body['intent']))
        self.bitcoin=BitcoinRPC(wc['bitcoin_rpc'],wc['bitcoin_cookie'],wc['bitcoin_policy']['network'],wc['bitcoin_policy'])
        self.driver=Driver(self.actors['operator'],c['registration_board']);self.graph()
    def close(self):
        pass  # Original Store/Journal use bounded per-operation connections.
    def graph(self):
        from signer_agent.zk_spv import inspect_binding
        from signer_agent.address_mint import read
        views=[a.context() for a in self.actors.values()]
        b,_,ctx,board,_=views[0]
        for other in views[1:]:
            if (other[2]['registry'].address!=ctx['registry'].address or other[3].address!=board.address):raise ValueError('CONTOUR_ADDRESS_SCOPE')
        inspect_binding(self.actors['operator'].w3,self.wc['zk_state'],self.wc['bitcoin_policy'],b.number,adapter_address=read(ctx['common'],b.number,'spv'))
        return b,ctx,board
    def allowed(self,role,profile):
        from signer_agent.address_registration import authority
        b,ctx,board=self.graph();a=authority(self.actors['operator'].w3,ctx,self.c['registration_board'],b.number)
        step=profile['steps'][0];target=profile['contracts'][step['contract']]
        targets={'BitcoinAddressMessageBoard':(board.address,{'request','reply'}),
            'FROSTCoordinator':(a['coordinator'].address,{'createSessionFromDKG'}),
            'DKGAuthorization':(a['authorization'].address,{'authorizeOperation'}),
            'DKGOnchainBoard':(a['board'].address,{'publishIntent'}),
            'BitcoinAddressRegistry':(ctx['registry'].address,{'registerBatch'}),
            'RecipientPolicy':(ctx['registry'].functions.recipientPolicy().call(block_identifier=b.number),{'selectFor'})}
        # Exact original SDK methods/arguments are also bound by Calls' original
        # envelope. The requester/service never receive the operator's nonce lane.
        pair=targets.get(target['artifact'])
        if (len(profile['steps'])!=1 or step['kind']!='call' or step.get('value','0')!='0' or pair is None
                or target['address']!=pair[0] or step['method'] not in pair[1]
                or role!='operator' and target['artifact']!='BitcoinAddressMessageBoard'):
            raise ValueError('CONTOUR_ADDRESS_CALL')
    def request(self):
        a=self.actors['requester'];s=self.actors['service']
        selection=self.body['recipient_selection'];accepted=self.body['accepted_recipient_sha256']
        with a.store.journal.db() as db:
            packets=[(key,json.loads(raw)) for key,raw in db.execute("SELECT id,body FROM address_packets WHERE direction='OUT'")]
        old=[(key,p) for key,p in packets if p['header']['kind']=='REQUEST' and p['body'].get('intent')==self.body['intent'] and p['header']['recipient']==s.account.address]
        if len(old)>1:raise ValueError('CONTOUR_ADDRESS_INTENT')
        if old:
            key,packet=old[0]
            if packet['body'].get('recipient_choice')!=selection:raise ValueError('CONTOUR_ADDRESS_RECIPIENT')
            result=dict(message_id=key,route=packet['header']['route'])
        else:
            generated=a.make_intent(None,selection=selection,accepted=accepted)
            raw=generated['intent'] if isinstance(generated,dict) else generated.encode().hex()
            if raw!=self.body['intent']:raise ValueError('CONTOUR_ADDRESS_INTENT')
            result=a.request(self.intent.encode(),s.account.address)
        a.transmit(message_id=result['message_id'])
        s.service_once();s.transmit();a.poll()
        replies=a.replies()['routes'].get(result['route'],{})
        return dict(message_id=result['message_id'],route=result['route'],intent_id=self.intent.intent_id.hex(),
            status='ADDRESS_ACK_CONFIRMED' if replies.get('acknowledged') else 'AWAITING_ACK',ready=False)
    def registration(self):
        request=self.request()
        if request['status']!='ADDRESS_ACK_CONFIRMED':return request
        service=Driver(self.actors['service'],self.c['registration_board'])
        batch=service.prepare([self.intent.intent_id.hex()],self.c['deadline'])
        out=self.driver.prepare(None,self.c['deadline'],exported=service.export(batch['batch_id']))
        if out['status']=='REGISTERED':return out
        out=self.driver.propose(batch['batch_id'])
        if out['status']=='REGISTERED':return out
        b,ctx,a=self.driver.context()
        session=a['coordinator'].functions.getSession(int(out['session'])).call(block_identifier=b.number)
        return self.driver.execute(batch['batch_id']) if int(session[10])==2 else out
    def status(self):return self.actors['service'].status(self.intent.intent_id.hex(),self.bitcoin)
    def ready(self):
        out=self.registration()
        if out['status']!='REGISTERED':return out
        self.actors['service'].store.scan(self.bitcoin,max_blocks=int(self.c['max_scan_blocks']))
        self.actors['service'].publish_status(self.bitcoin);self.actors['service'].transmit();self.actors['requester'].poll()
        current=self.status();coverage=self.actors['service'].store.get('coverage:'+self.intent.intent_id.hex())
        b,_,_=self.graph()
        return dict(current,coverage_anchor=coverage,evm_anchor=dict(number=str(b.number),hash=b.hash.hex()))


class Pipeline:
    def __init__(self,r,c,context):
        self.r,self.c,self.phase=r,c,context['capability']
        if self.phase not in ('address_request','address_registration','address_ready'):raise ValueError('CONTOUR_CAPABILITY')
        self.key='contour-address:'+r.job;self.journal=r.journal;self.journal.put(self.key,dict(config=c,need=r.need))
    def save(self):self.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason,**facts):return dict(status=status,observations=dict(reason=reason,intent_id=self.r.intent.intent_id.hex(),
        recipient_mode=self.c['recipient_mode'],ready=False,production_ready=False,**facts))
    def execute(self,action):
        fd=os.open(str(self.journal.path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_ADDRESS_BUSY') from None
            self.record=self.journal.get(self.key)['result']
            if not self.record:
                if action=='probe':return self.result('ABSENT','ADDRESS_ENROLLMENT_REQUIRED')
                if action=='recover':return self.result('RECOVERY_REQUIRED','ADDRESS_ENROLLMENT_REQUIRED')
                self.record=dict(calls={});self.save()
            calls=Calls(self.r,self.record,self.save,action)
            for actor in self.r.actors.values():actor.transaction_controller=calls
            try:
                calls.pending()
                if action=='probe':
                    retained=self.record.get(self.phase)
                    if not retained:return self.result('ABSENT','ORIGINAL_ADDRESS_OPERATION_REQUIRED')
                    self.r.graph()
                    if self.phase=='address_request' and not self.r.actors['requester'].replies()['routes'].get(retained['route'],{}).get('acknowledged'):
                        return self.result('RECOVERY_REQUIRED','ORIGINAL_ADDRESS_ACK_UNAVAILABLE')
                    if self.phase=='address_registration' and self.r.driver.status(retained['batch_id'])['status']!='REGISTERED':
                        return self.result('RECOVERY_REQUIRED','ORIGINAL_REGISTRATION_UNAVAILABLE')
                    if self.phase=='address_ready':
                        # Historical coverage survives later append/ageing. It
                        # never replaces current mint/signing/Ready checks.
                        a=retained['evm_anchor'];coverage=retained['coverage_anchor']
                        if (self.r.actors['service'].w3.eth.get_block(int(a['number'])).hash.hex()!=a['hash']
                                or self.r.bitcoin.call('getblockhash',int(coverage['tip']))!=coverage['bestblock']):
                            return self.result('RECOVERY_REQUIRED','ORIGINAL_ADDRESS_COVERAGE_ORPHANED')
                    return self.result('SATISFIED','ORIGINAL_ADDRESS_PHASE_CONFIRMED',transactions=calls.evidence())
                method={'address_request':self.r.request,'address_registration':self.r.registration,'address_ready':self.r.ready}[self.phase]
                out=method()
                done=(self.phase=='address_request' and out['status']=='ADDRESS_ACK_CONFIRMED' or self.phase=='address_registration' and out['status']=='REGISTERED'
                    or self.phase=='address_ready' and out.get('registration')=='REGISTERED' and out.get('watch')=='WATCH_COVERED' and out['status']==self.c['expected_status'])
                if done:self.record[self.phase]=out;self.save()
                return self.result('WAITING','ADDRESS_PHASE_AWAITING_CANONICAL_PROBE',current=out)
            except Required:return self.result('ABSENT','ORIGINAL_ADDRESS_OPERATION_REQUIRED')
            except Progress:return self.result('ABSENT','ORIGINAL_ADDRESS_OPERATION_PROGRESS')
            except Hold:return self.result('RECOVERY_REQUIRED','ORIGINAL_ADDRESS_RECOVERY_REQUIRED')
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('address-operation-config','address-operation-config-sha256','inputs-sha256','worker-config','worker-config-sha256','request-file','request-sha256'):p.add_argument('--'+k,required=True)
    a=p.parse_args(argv)
    from signer_agent.zk_worker import load_config as wc_load
    wc=wc_load(a.worker_config);need=json.loads(read_private(a.request_file));c=load_config(a.address_operation_config);data=inputs(c,wc,need)
    if (digest(wc)!=a.worker_config_sha256 or digest(need)!=a.request_sha256 or digest(c)!=a.address_operation_config_sha256 or digest(data)!=a.inputs_sha256):raise ValueError('CONTOUR_ADDRESS_SCOPE')
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path));r=Runtime(c,wc,need,data)
    try:result=Pipeline(r,c,ctx).execute(ctx['action'])
    finally:r.close()
    artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'address-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
