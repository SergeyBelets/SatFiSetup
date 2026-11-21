"""One reviewed withdrawal/version through original owner, fee and Bitcoin SDKs."""
import argparse
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .contour import digest,private_write,sha
from .contour_calls import Calls,Required,Hold,Progress,ObservationPending
from signer_agent.onboarding import read_private,password_file
from signer_agent.btc_fee_reserve import Inspector,load_config as fee_config,read
from signer_agent.withdrawal_user import Withdrawal
from signer_agent.withdrawal_fee_client import Driver,quote as version_quote
from signer_agent.zk_worker import Worker,load_config as worker_config

FIELDS={'version','mode','owner_config','operator_config','quote_file','accepted_quote_hash','payment_file',
        'accepted_terms_hash','request_id','bundle_file','auth_bundle_file','state_file','password_file'}


def hex32(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def load_config(path):
    c=json.loads(read_private(path))
    if (not isinstance(c,dict) or c.get('version') not in ('1','2','3') or set(c)!=(FIELDS|({'previous_config','accepted_previous_sha256'} if c.get('version') in ('2','3') else set())|({'additional_owners'} if c.get('version')=='3' else set())) or c['mode'] not in ('BTC','MP','S')
            or any(not isinstance(c[k],str) or not Path(c[k]).is_absolute() for k in
                   ('owner_config','operator_config','quote_file','payment_file','bundle_file','auth_bundle_file','state_file','password_file'))
            or any(not hex32(c[k]) for k in ('accepted_quote_hash','accepted_terms_hash','request_id'))):
        raise ValueError('CONTOUR_WITHDRAWAL_CONFIG')
    if c['version'] in ('2','3') and (c['version']=='2' or c['previous_config'] is not None) and (not isinstance(c['previous_config'],str) or not Path(c['previous_config']).is_absolute() or not hex32(c['accepted_previous_sha256'])):
        raise ValueError('CONTOUR_WITHDRAWAL_CONFIG')
    if c['version']=='3':
        if c['previous_config'] is None and c['accepted_previous_sha256'] is not None:raise ValueError('CONTOUR_WITHDRAWAL_CONFIG')
        if not isinstance(c['additional_owners'],list) or not 1<=len(c['additional_owners'])<=7:raise ValueError('CONTOUR_WITHDRAWAL_CONFIG')
        for row in c['additional_owners']:
            if (not isinstance(row,dict) or set(row)!={'owner_config','quote_file','accepted_quote_hash','request_id'}
                    or any(not isinstance(row[k],str) or not Path(row[k]).is_absolute() for k in ('owner_config','quote_file'))
                    or not hex32(row['accepted_quote_hash']) or not hex32(row['request_id'])):raise ValueError('CONTOUR_WITHDRAWAL_CONFIG')
    return c


def inputs(c,wc,need,*,_seen=()):
    from signer_agent.dkg_protocol import Manifest
    from signer_agent.bitcoin_onchain_plan import validate_plan
    from signer_agent.bitcoin_input_keys import build
    owner=fee_config(c['owner_config']);operator=fee_config(c['operator_config'])
    bundle=json.loads(read_private(c['bundle_file']));manifest=Manifest.from_dict(bundle)
    auth_bundle=json.loads(read_private(c['auth_bundle_file']));auth=Manifest.from_dict(auth_bundle)
    qbody=json.loads(read_private(c['quote_file']));q=qbody.get('quote',qbody)
    body=json.loads(read_private(c['payment_file'],max_bytes=512*1024));request=body['request'];plan=body['plan']
    additional=[]
    for row in c.get('additional_owners',[]):
        cfg=fee_config(row['owner_config']);qb=json.loads(read_private(row['quote_file']));quote=qb.get('quote',qb)
        if (digest(quote)!=row['accepted_quote_hash'] or quote['options']['fee_mode'] not in ('BTC','MP','S') or quote['options']['asset'] not in ('MP','S')
                or quote.get('kind')!=q.get('kind') or quote['graph']!=q['graph'] or cfg['reserve']!=owner['reserve']
                or cfg['bitcoin_network']!=wc['bitcoin_policy']['network'] or any(cfg[k]!=wc[k] for k in ('chain_id','evm_confirmations','bitcoin_policy'))
                or any(quote[k]!=cfg[k] for k in ('chain_id','bitcoin_network'))):raise ValueError('CONTOUR_WITHDRAWAL_SCOPE')
        additional.append(dict(config=cfg,quote=quote,accepted_quote_hash=row['accepted_quote_hash'],request_id=row['request_id']))
    refs=sorted([c['request_id'],*[r['request_id'] for r in additional]])
    replacement=c.get('previous_config') is not None
    if (digest(q)!=c['accepted_quote_hash'] or q['options']['fee_mode']!=c['mode']
            or q['options']['asset'] not in ('MP','S') or request['metadata']['method']!='prepare'
            or request['metadata']['bitcoin_plan']!=plan or plan.get('version')!='4'
            or len(set(refs))!=len(refs) or plan['withdrawal_refs']!=refs or replacement!=(plan['withdrawal_fee']['previous']!='00'*32)
            or body.get('terms_hash')!=c['accepted_terms_hash'] or q['graph']['reserve']!=owner['reserve']
            or owner['reserve']!=operator['reserve'] or manifest.purpose!='BTC_SPEND'
            or manifest.chain_id!=int(wc['chain_id']) or auth.chain_id!=manifest.chain_id
            or auth.purpose!='EVM_AUTH' or auth_bundle.get('role')!='conf_ceremony'
            or auth.registry!=manifest.registry):raise ValueError('CONTOUR_WITHDRAWAL_SCOPE')
    for actor in (owner,operator):
        if actor['bitcoin_network']!=wc['bitcoin_policy']['network'] or any(actor[k]!=wc[k] for k in ('chain_id','evm_confirmations','bitcoin_policy')):
            raise ValueError('CONTOUR_WITHDRAWAL_SCOPE')
    if any(q[k]!=owner[k] for k in ('chain_id','bitcoin_network')):raise ValueError('CONTOUR_WITHDRAWAL_SCOPE')
    validate_plan(plan,manifest)
    if replacement:
        path=str(Path(c['previous_config']).resolve())
        if path in _seen or len(_seen)>=15:raise ValueError('CONTOUR_WITHDRAWAL_VERSION_LIMIT')
        previous=load_config(path)
        if digest(previous)!=c['accepted_previous_sha256']:raise ValueError('CONTOUR_WITHDRAWAL_PREVIOUS')
        oldbody=json.loads(read_private(previous['payment_file'],max_bytes=512*1024));oldplan=oldbody['plan']
        oldbuilt=build(oldplan);oldneed=dict(kind='withdrawal',consumer=need['consumer'],batch=oldplan['batch_id'],txid=oldbuilt['txid'])
        inputs(previous,wc,oldneed,_seen=(*_seen,path))
        for key in ('mode','owner_config','operator_config','quote_file','accepted_quote_hash','request_id','bundle_file','auth_bundle_file','state_file','password_file'):
            if c[key]!=previous[key]:raise ValueError('CONTOUR_WITHDRAWAL_FAMILY')
        if c.get('additional_owners')!=previous.get('additional_owners'):raise ValueError('CONTOUR_WITHDRAWAL_FAMILY')
        # The original verifier additionally enforces complete source/return
        # accounting. The runner cannot relabel a changed payout or input family.
        if (plan['withdrawal_fee']['previous']!=oldplan['batch_id'] or plan['batch_id']==oldplan['batch_id']
                or plan['inputs']!=oldplan['inputs'] or plan['input_keys']!=oldplan['input_keys']
                or plan['withdrawal_refs']!=oldplan['withdrawal_refs'] or int(plan['fee'])<=int(oldplan['fee'])):
            raise ValueError('CONTOUR_WITHDRAWAL_FAMILY')
    built=build(plan)
    expected=dict(kind='withdrawal',consumer=q['graph']['escrow'],batch=plan['batch_id'],txid=built['txid'])
    if need!=expected or any(int(plan['deadline'])>int(quote['options']['deadline']) for quote in [q,*[r['quote'] for r in additional]]):raise ValueError('CONTOUR_WITHDRAWAL_NEED')
    data=(owner,operator,bundle,manifest,q,body,auth_bundle)
    return (*data,additional) if additional else data


def wire_inputs(data):return [data[0],data[1],data[2],*data[4:]]


class Runtime:
    def __init__(self,c,wc,need,data,*,owner_factory=Withdrawal):
        from signer_agent.dkg import Registry
        from signer_agent.secure_state import SecureState
        self.c,self.wc,self.need=c,wc,need
        oc,pc,self.bundle,self.manifest,self.quote,self.body,auth_bundle=data[:7];self.plan=self.body['plan'];self.job=digest([need,c])
        self.actors=dict(owner=Inspector(oc),operator=Inspector(pc));self.owner=owner_factory(self.actors['owner'])
        self.owner_records=[dict(actor='owner',owner=self.owner,quote=self.quote,accepted_quote_hash=c['accepted_quote_hash'],request_id=c['request_id'])]
        for index,row in enumerate(data[7] if len(data)>7 else [],1):
            name='owner'+str(index);self.actors[name]=Inspector(row['config'])
            self.owner_records.append(dict(row,actor=name,owner=owner_factory(self.actors[name])))
        self.driver=Driver(self.actors['operator'],auth_bundle)
        self.state=SecureState(c['state_file'],password_file(c['password_file']))
        key=json.loads(read_private(pc['identity_file']))['private_key']
        self.registry=Registry(pc['evm_rpc'],self.manifest.registry,key)
        self.registry.identity_file=pc['identity_file'];self.registry.transaction_journal=str(Path(pc['identity_file']).resolve())+'.transactions.sqlite'
        self.registry.fee_reserve_config=c['operator_config'];self.registry.bitcoin_graph=dict(version='2',chain_id=wc['chain_id'],
            bitcoin_network=wc['bitcoin_policy']['network'],bitcoin_policy=wc['bitcoin_policy'],evm_confirmations=wc['evm_confirmations'])
        if self.bundle.get('onboarding'):
            from signer_agent.onboarding import validate_bundle
            validate_bundle(self.bundle,self.state,self.registry)
        self.registry.check(self.manifest);self.key=self.state.get(self.manifest.context.hex())
        if not self.key or self.key.get('stage')!='active':raise ValueError('KEY_NOT_ACTIVE')
        if bytes(self.registry.read('getCompletedResult',self.manifest.session_id)[1]).hex()!=self.key['result_hash']:
            raise ValueError('TRANSCRIPT_MISMATCH')
        for row in self.owner_records:row['owner'].validate_quote(row['quote'],row['accepted_quote_hash'])
        ctx,_=self.graph_check()
        from signer_agent.withdrawal_fee_client import candidate_terms
        from signer_agent.bootstrap import artifact,encode_args
        spec=next(x for x in artifact('WithdrawalFeePayments')['abi'] if x.get('name')=='getPrepareOperationHash')
        candidate=encode_args(spec['inputs'],self.body['request']['metadata']['args'])[0]
        if candidate_terms(bytes(read(ctx['withdrawal_fees']['payments'],ctx['block'].number,'scope')),candidate).hex()!=c['accepted_terms_hash']:
            raise ValueError('CONTOUR_WITHDRAWAL_TERMS')

    def graph_check(self):
        a=self.actors['owner'].context();b=self.actors['operator'].context()
        def fields(ctx):
            return [ctx[k].address for k in ('registry','fee','pool','common','batch','escrow','board')]+[
                ctx['withdrawal_fees'][k].address for k in ('payments','funding','credits')]
        if (fields(a)!=fields(b) or a['escrow'].address!=self.need['consumer'] or a['pool'].address!=self.quote['graph']['mining']
                or read(b['registry'],b['block'].number,'dkg')!=self.manifest.registry
                or bytes(read(b['registry'],b['block'].number,'walletId'))!=self.manifest.session_id):
            raise ValueError('CONTOUR_WITHDRAWAL_GRAPH')
        authority=self.driver.authority(b)
        for row in self.owner_records[1:]:
            if fields(self.actors[row['actor']].context())!=fields(b):raise ValueError('CONTOUR_WITHDRAWAL_GRAPH')
        return b,authority

    def allowed(self,role,profile):
        ctx,a=self.graph_check();step=profile['steps'][0];target=profile['contracts']['target']
        selected=next((row['quote'] for row in self.owner_records if row['actor']==role),self.quote)
        addresses={'PoolMpToken':selected['graph']['token'],'SimpleMultiPool':selected['options']['pool'],
            'BitcoinWithdrawalEscrow':ctx['escrow'].address,'WithdrawalFeeCredits':ctx['withdrawal_fees']['credits'].address,
            'WithdrawalFeePayments':ctx['withdrawal_fees']['payments'].address,'FROSTCoordinator':a['coordinator'].address,
            'DKGAuthorization':a['authorization'].address,'DKGOnchainBoard':a['board'].address,
            'BitcoinBatchRegistry':ctx['batch'].address,'BitcoinSigningBoard':ctx['board'].address}
        methods=({'PoolMpToken':{'approve'},'SimpleMultiPool':{'requestWithdrawalQuoted','consentFee'},
            'BitcoinWithdrawalEscrow':{'requestDirect'},'WithdrawalFeeCredits':{'consent','approveVersion'}} if role.startswith('owner') else
            {'WithdrawalFeePayments':{'prepare'},'FROSTCoordinator':{'createSessionFromDKG'},'DKGAuthorization':{'authorizeOperation'},
             'DKGOnchainBoard':{'publishIntent','appendFeeIntent','commitFeeIntent'},
             'BitcoinBatchRegistry':{'reserve','reserveReplacement'},'BitcoinWithdrawalEscrow':{'bind','bindReplacement','settle'},
             'BitcoinSigningBoard':{'publishPlan','appendFeePlan','commitFeePlan'}})
        if (len(profile['steps'])!=1 or step.get('kind')!='call' or step.get('value','0')!='0'
                or target['address']!=addresses.get(target['artifact']) or step['method'] not in methods.get(target['artifact'],set())):
            raise ValueError('CONTOUR_WITHDRAWAL_CALL_SCOPE')

    def btc(self,action,operation=None):
        from signer_agent.bitcoin_onchain import dispatch
        # Import only the plan, not the build-payment envelope containing a
        # duplicate plan and public authorization. Keep the SDK's size bound.
        plan_file=Path(self.c['state_file']).parent/('.contour-withdrawal-plan-'+digest(self.plan)+'.json')
        if action=='btc-import-plan':
            if plan_file.exists():
                if json.loads(read_private(plan_file))!=self.plan:raise ValueError('CONTOUR_WITHDRAWAL_PLAN')
            else:private_write(plan_file,self.plan)
        a=SimpleNamespace(action=action,operation=operation,board=self.bundle['onchain_board'],signing_board=self.plan['signing_board'],
            batch=self.plan['batch_id'],batch_registry=self.plan['batch_registry'],mint_only=False,bitcoin_rpc=self.wc['bitcoin_rpc'],
            bitcoin_cookie=self.wc['bitcoin_cookie'],plan_file=str(plan_file),state=self.c['state_file'],
            zk_worker_config=None,zk_transport_config=None)
        return dispatch(a,self.manifest,self.key,self.registry,self.state)

    def payment(self):
        result=self.driver.propose(self.body['request'])
        if result['status']=='WITHDRAWAL_FEE_EXECUTED':return True
        intent=result['intent'];ctx,a=self.graph_check()
        info=read(a['coordinator'],ctx['block'].number,'getSession',int(intent['session']))
        if int(info[10])!=2:return False
        return self.driver.execute(intent)['status']=='WITHDRAWAL_FEE_EXECUTED'

    def primary(self):
        outcomes=[]
        for row in self.owner_records:
            owner,q=row['owner'],row['quote']
            result=(owner.status(q['operation_id']) if self.c.get('previous_config') is not None else owner.submit(q,row['accepted_quote_hash']))
            if result.get('request')!=row['request_id']:raise ValueError('CONTOUR_WITHDRAWAL_REQUEST')
            exact=owner.status(q['operation_id'])
            if exact['cost']['principal_sats']!=q['cost']['principal_sats']:raise ValueError('CONTOUR_WITHDRAWAL_PRINCIPAL')
            outcomes.append(result)
        return outcomes[0]

    def version(self):
        from eth_abi import encode
        from eth_utils import keccak
        ctx,_=self.graph_check();batch=bytes.fromhex(self.plan['batch_id'])
        for row in self.owner_records:
            request=bytes.fromhex(row['request_id']);terms=version_quote(self.actors[row['actor']],batch,request)
            if terms['terms_hash']!=self.c['accepted_terms_hash'] or terms['principal_sats']!=row['quote']['cost']['principal_sats']:
                raise ValueError('CONTOUR_WITHDRAWAL_TERMS')
            expected=keccak(encode(['bytes32','uint256'],[bytes.fromhex(terms['terms_hash']),int(terms['revision'])]))
            if bytes(read(ctx['withdrawal_fees']['credits'],ctx['block'].number,'admitted',batch,request))!=expected:
                row['owner'].approve_version(row['quote']['operation_id'],batch,self.c['accepted_terms_hash'])

    def session_ready(self,operation):
        from signer_agent.bitcoin_onchain_payment import _cache_key
        from signer_agent.bitcoin_trust import require_registry
        graph=require_registry(self.manifest,self.key,self.registry,self.state,self.plan['batch_registry'])['graph']
        saved=self.state.get(_cache_key(self.manifest,self.plan,operation))
        if not saved:return None
        ctx,a=self.graph_check();sid=int(saved['session'])
        public=bytes(read(a['board'],ctx['block'].number,'intents',sid))
        if not public:return None
        info=read(a['coordinator'],ctx['block'].number,'getSession',sid)
        return int(info[10])==2

    def prepare(self):
        self.primary()
        if not self.payment():return dict(status='WAITING_FEE_AUTHORIZATION')
        self.version()
        self.btc('btc-import-plan')
        ctx,_=self.graph_check();state=int(read(ctx['batch'],ctx['block'].number,'batchState',bytes.fromhex(self.plan['batch_id'])))
        if state==3:return dict(status='CANCELLED_FAMILY_REQUIRES_EXPLICIT_RECOVERY')
        for operation,required in (('reserve',1),('bind',2)):
            if state>=required:continue
            ready=self.session_ready(operation)
            if ready is None:self.btc('btc-propose',operation);return dict(status='WAITING_'+operation.upper()+'_AUTHORIZATION')
            if not ready:return dict(status='WAITING_'+operation.upper()+'_AUTHORIZATION')
            self.btc('btc-execute',operation)
            return dict(status='WITHDRAWAL_PREPARATION_PROGRESS')
        self.btc('btc-publish-plan')
        status=self.btc('btc-status')
        if status['status']=='READY_TO_BROADCAST':
            self.before_broadcast()
            self.btc('btc-broadcast');return dict(status='BTC_BROADCAST')
        return status

    def broadcast_record(self):
        from signer_agent.bitcoin_worker import BitcoinWorker
        worker=BitcoinWorker(self.c['state_file']+'.bitcoin.sqlite',self.actors['operator'].bitcoin)
        with worker.db() as db:row=db.execute('SELECT signed,txid,status FROM payments WHERE id=?',(self.plan['batch_id'],)).fetchone()
        if not row:return None
        from signer_agent import crypto
        parsed=crypto.call('bitcoin_funding',raw=row[0],txid=self.need['txid'])
        from signer_agent.bitcoin_input_keys import build
        if parsed['stripped']!=build(self.plan)['unsigned'] or row[1]!=self.need['txid']:raise ValueError('CONTOUR_WITHDRAWAL_BROADCAST')
        return dict(raw_sha256=digest(row[0]),txid=row[1],status=row[2])

    def recover_broadcast(self):
        from signer_agent.bitcoin_worker import BitcoinWorker
        from signer_agent import crypto
        worker=BitcoinWorker(self.c['state_file']+'.bitcoin.sqlite',self.actors['operator'].bitcoin)
        with worker.db() as db:row=db.execute('SELECT signed FROM payments WHERE id=?',(self.plan['batch_id'],)).fetchone()
        if not row:raise Hold()
        parsed=crypto.call('bitcoin_funding',raw=row[0],txid=self.need['txid'])
        previous=self.plan.get('withdrawal_fee',{}).get('previous','00'*32)
        worker.broadcast(self.plan['batch_id'],dict(signed=row[0],txid=self.need['txid'],wtxid=parsed['wtxid']),
            replaces=previous if previous!='00'*32 else None)

    def settled(self):
        ctx,_=self.graph_check();batch=bytes.fromhex(self.plan['batch_id']);payment=read(ctx['escrow'],ctx['block'].number,'payment',batch)
        if not payment[2]:return None
        if bytes(payment[0])[::-1].hex()!=self.need['txid']:raise ValueError('CONTOUR_WITHDRAWAL_WINNER')
        statuses=[];fee=ctx['withdrawal_fees']
        for row in self.owner_records:
            status=row['owner'].status(row['quote']['operation_id'])
            if status['request']!=row['request_id'] or status['primary_state']!='PAID' or status['cost']['principal_sats']!=row['quote']['cost']['principal_sats']:
                raise ValueError('CONTOUR_WITHDRAWAL_PRINCIPAL')
            budget=read(fee['credits'],ctx['block'].number,'getBudget',bytes.fromhex(row['request_id']))
            if not budget[13] or not read(fee['payments'],ctx['block'].number,'settled',batch):raise ValueError('CONTOUR_WITHDRAWAL_FEE_SETTLEMENT')
            statuses.append(status)
        return statuses[0] if len(statuses)==1 else dict(statuses[0],batch_owners=statuses)


class Pipeline:
    def __init__(self,runtime,c,need,context):
        phase=context['capability'];expected='withdrawal_'+c['mode'].lower()
        if phase not in ('withdrawal_prepare',expected):raise ValueError('CONTOUR_CAPABILITY')
        self.r,self.c,self.need,self.context,self.phase=runtime,c,need,context,phase
        self.journal=runtime.actors['operator'].journal;self.key='contour-withdrawal:'+digest([c,need])
        self.journal.put(self.key,dict(config=c,need=need))
    def save(self):self.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason,**facts):return dict(status=status,observations=dict(reason=reason,mode=self.c['mode'],
        principal_sats=self.r.quote['cost']['principal_sats'],txid=self.need['txid'],ready=False,production_ready=False,
        batch_owners=[dict(request_id=row['request_id'],principal_sats=row['quote']['cost']['principal_sats'],fee_mode=row['quote']['options']['fee_mode'])
            for row in getattr(self.r,'owner_records',[])],
        transactions=Calls(self.r,self.record,self.save,'probe').evidence() if status=='SATISFIED' else [],**facts))
    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        fd=os.open(str(self.journal.path)+'.contour.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_WITHDRAWAL_BUSY') from None
            self.record=self.journal.get(self.key)['result']
            if not self.record:
                if action=='recover':return self.result('RECOVERY_REQUIRED','WITHDRAWAL_ENROLLMENT_REQUIRED')
                self.record=dict(calls={})
                if action!='probe':self.save()
            calls=Calls(self.r,self.record,self.save,action)
            for actor in self.r.actors.values():actor.transaction_controller=calls
            self.r.registry.transaction_controller=calls
            def before_broadcast():
                if calls.used:raise Progress()
                if action=='probe':raise Required()
                if self.record.get('broadcast_started'):raise Hold()
                self.record['broadcast_started']=True;self.save();calls.used=True
            self.r.before_broadcast=before_broadcast
            try:
                calls.pending()
                if self.phase=='withdrawal_prepare':
                    broadcast=self.r.broadcast_record()
                    if broadcast and broadcast['status'] in ('BROADCAST','CONFIRMED','SETTLED'):
                        if self.record.get('broadcast') not in (None,broadcast['raw_sha256']):raise ValueError('CONTOUR_WITHDRAWAL_BROADCAST')
                        self.record['broadcast']=broadcast['raw_sha256'];self.save()
                        return self.result('SATISFIED','ORIGINAL_BTC_BROADCAST_RETAINED',broadcast=broadcast)
                    if action=='probe' and broadcast is None and self.record.get('broadcast_started'):
                        return self.result('RECOVERY_REQUIRED','ORIGINAL_BTC_BROADCAST_RECOVERY_REQUIRED')
                    if broadcast is not None:
                        if action=='probe':return self.result('ABSENT','SIGNED_BTC_BROADCAST_PENDING')
                        self.r.recover_broadcast();return self.result('ABSENT','ORIGINAL_BTC_BROADCAST_PROGRESS')
                    # SDK sends are intercepted by Calls; a read-only probe
                    # cannot sign or broadcast a Bitcoin transaction.
                    self.r.broadcast_action=action;result=self.r.prepare()
                    return self.result('RECOVERY_REQUIRED' if result['status']=='CANCELLED_FAMILY_REQUIRES_EXPLICIT_RECOVERY' else 'WAITING',result['status'])
                settled=self.r.settled()
                if settled:
                    if not calls.has('settle'):return self.result('RECOVERY_REQUIRED','EXACT_WITHDRAWAL_RECEIPT_REQUIRED')
                    return self.result('SATISFIED','EXACT_WITHDRAWAL_SETTLED',withdrawal=settled)
                if action=='probe':return self.result('ABSENT','WITHDRAWAL_SETTLEMENT_REQUIRED')
                result=self.r.btc('btc-settle')
                return self.result('ABSENT',result['status'])
            except Required:return self.result('ABSENT','ORIGINAL_WITHDRAWAL_OPERATION_REQUIRED')
            except ObservationPending:return self.result('WAITING','EVM_RECEIPT_PENDING')
            except Hold:return self.result('RECOVERY_REQUIRED','ORIGINAL_WITHDRAWAL_RECOVERY_REQUIRED')
            except Progress:return self.result('ABSENT','ORIGINAL_WITHDRAWAL_OPERATION_PROGRESS')
            finally:
                for actor in self.r.actors.values():actor.transaction_controller=None
                self.r.registry.transaction_controller=None
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('withdrawal-config','withdrawal-config-sha256','inputs-sha256','worker-config','worker-config-sha256','request-file','request-sha256'):
        p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path))
    c=load_config(a.withdrawal_config);wc=worker_config(a.worker_config);need=json.loads(read_private(a.request_file));data=inputs(c,wc,need)
    if any(x!=y for x,y in ((digest(c),a.withdrawal_config_sha256),(digest(wire_inputs(data)),a.inputs_sha256),
        (digest(wc),a.worker_config_sha256),(digest(need),a.request_sha256))):raise ValueError('CONTOUR_WITHDRAWAL_SCOPE')
    result=Pipeline(Runtime(c,wc,need,data),c,need,ctx).execute(ctx['action']);artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'withdrawal-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
