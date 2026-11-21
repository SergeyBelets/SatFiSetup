"""Bounded SMP1/address mint through original public admission and participant journals."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .contour import digest,private_write,sha
from .contour_tx import original,exact
from signer_agent.bootstrap import Bootstrap,artifact
from signer_agent.onboarding import read_private,password_file
from signer_agent.spv_automation import Budget,validate_budget
from signer_agent.zk_worker import Worker,load_config as worker_config

BUDGET=('max_tx_gas','max_transaction_cost_wei','max_total_cost_wei','max_job_cost_wei','max_attempts_per_job')


class Required(Exception):pass
class Hold(Exception):pass
class Progress(Exception):pass


def load_config(path):
    c=json.loads(read_private(path))
    fields={'version','mode','bundle_file','identity_file','state_file','password_file','board','signing_board',
            'mint_admission','deadline','budget'}
    from web3 import Web3
    if (not isinstance(c,dict) or c.get('version')!='1' or c.get('mode') not in ('SMP1','ADDRESS')
            or set(c)!=fields|({'funding_config'} if c.get('mode')=='SMP1' else set())
            or any(not isinstance(c[k],str) or not Path(c[k]).is_absolute()
                   for k in ('bundle_file','identity_file','state_file','password_file'))
            or any(not Web3.is_checksum_address(c[k]) or int(c[k],16)==0 for k in ('board','mint_admission'))
            or not isinstance(c['budget'],dict) or set(c['budget'])!=set(BUDGET)):
        raise ValueError('CONTOUR_MINT_CONFIG')
    from signer_agent.spv_automation import integer
    integer(c['deadline'],1,2**64-1);validate_budget(c['budget'])
    if c['mode']=='SMP1':
        if (not isinstance(c['funding_config'],str) or not Path(c['funding_config']).is_absolute()
                or not Web3.is_checksum_address(c['signing_board']) or int(c['signing_board'],16)==0):
            raise ValueError('CONTOUR_MINT_CONFIG')
    elif c['signing_board'] is not None:raise ValueError('CONTOUR_MINT_CONFIG')
    return c


def inputs(c,wc,request):
    from signer_agent.dkg_protocol import Manifest
    bundle=json.loads(read_private(c['bundle_file']));manifest=Manifest.from_dict(bundle)
    if (manifest.purpose!='BTC_SPEND' or manifest.chain_id!=int(wc['chain_id'])
            or request.get('kind')!=('deposit' if c['mode']=='SMP1' else 'address_deposit')):
        raise ValueError('CONTOUR_MINT_SCOPE')
    fc,dc=None,None
    if c['mode']=='SMP1':
        from .contour_funding import load_config,deposit_config
        fc=load_config(c['funding_config'])
        if fc['mode']!='SMP1_QUOTE':raise ValueError('CONTOUR_MINT_ACCEPTED_QUOTE_REQUIRED')
        dc=deposit_config(fc,wc,request)
    return bundle,manifest,fc,dc


class Runtime:
    def __init__(self,c,wc,request,data):
        from signer_agent.dkg import Registry
        from signer_agent.secure_state import SecureState
        self.c,self.wc,self.request=c,wc,request
        self.bundle,self.manifest,fc,dc=data
        self.state=SecureState(c['state_file'],password_file(c['password_file']))
        key=json.loads(read_private(c['identity_file']))['private_key']
        self.registry=Registry(wc['evm_rpc'],self.manifest.registry,key)
        self.registry.identity_file=c['identity_file'];self.registry.transaction_journal=str(Path(c['identity_file']).resolve())+'.transactions.sqlite'
        self.registry.bitcoin_graph=dict(version='2',chain_id=wc['chain_id'],bitcoin_network=wc['bitcoin_policy']['network'],
            bitcoin_policy=wc['bitcoin_policy'],evm_confirmations=wc['evm_confirmations'])
        if self.bundle.get('onboarding'):
            from signer_agent.onboarding import validate_bundle
            validate_bundle(self.bundle,self.state,self.registry)
        self.registry.check(self.manifest)
        self.key=self.state.get(self.manifest.context.hex())
        if not self.key or self.key.get('stage')!='active':raise ValueError('KEY_NOT_ACTIVE')
        completed=self.registry.read('getCompletedResult',self.manifest.session_id)
        if bytes(completed[1]).hex()!=self.key['result_hash']:raise ValueError('TRANSCRIPT_MISMATCH')
        if self.key['participant']!=self.manifest.participants[self.key['index']-1]:raise ValueError('PARTICIPANT_MISMATCH')
        self.args=SimpleNamespace(action=None,board=c['board'],signing_board=c['signing_board'],mint_admission=c['mint_admission'],
            mint_only=False,batch=None,deadline=c['deadline'],txid=request['txid'],vout=int(request['vout']),
            intent_id=request.get('intent_id'),bitcoin_rpc=wc['bitcoin_rpc'],bitcoin_cookie=wc['bitcoin_cookie'],
            zk_worker_config=None,zk_transport_config=None)
        if dc:
            from signer_agent.smp1_deposit import Deposit
            self.deposit=Deposit(dc)
            _,quote,_=self.deposit.entry(fc['operation_id'],fc['accepted_quote_hash']);self.quote=quote
            if (quote['txid']!=request['txid'] or quote['vout']!=request['vout'] or quote['graph']['pool']!=request['consumer']):
                raise ValueError('CONTOUR_MINT_QUOTE')
            self.args.recipients=[quote['recipient']];self.args.amounts=[quote['mp_units']]

    def dispatch(self,action):
        from signer_agent.bitcoin_onchain import dispatch
        self.args.action=('btc-mint-' if self.c['mode']=='SMP1' else 'btc-address-mint-')+action
        return dispatch(self.args,self.manifest,self.key,self.registry,self.state)

    def targets(self):
        from signer_agent.mint_admission import configuration
        from signer_agent.evm_network import read_block
        block=read_block(self.registry.w3,int(self.wc['evm_confirmations']))
        ctx=configuration(self.registry.w3,self.c['mint_admission'],block.number,expected_pool=self.request['consumer'])
        return {'BitcoinMintAdmission':ctx['board'].address,'MiningPoolV1':ctx['pool'].address,
                'DKGAuthorization':ctx['authorization'].address}

    def review_recipient(self):
        if self.c['mode']!='SMP1':return
        d,q=self.deposit,self.quote;ctx=d.context()
        if ctx['identity']!=q['graph']:raise ValueError('SMP1_GRAPH')
        choice=q.get('recipient_choice');n=ctx['block'].number
        if choice is None:
            if any(d.w3.eth.get_code(q['recipient'],block_identifier=at) for at in (n,'latest')):
                raise ValueError('SMP1_RECIPIENT_PROFILE')
        else:
            from signer_agent.recipient_profiles import review_quote
            if (choice['policy']!=d.recipient_policy(ctx) or choice['namespace']!=q['graph']['pool']
                    or choice['pool']!=q['graph']['pool'] or choice['token']!=q['graph']['token']
                    or choice['recipient']!=q['recipient'] or int(choice['requester'],16)!=0):
                raise ValueError('RECIPIENT_SELECTION_REQUIRED')
            for at in (n,d.w3.eth.get_block('latest').number):
                review_quote(d.w3,choice,digest(choice),at,sinks=d.recipient_sinks(ctx))
        d.stable(ctx['block'])


class Pipeline:
    def __init__(self,worker,request,c,context,runtime):
        capability='smp1_mint' if c['mode']=='SMP1' else 'address_mint'
        if context['capability']!=capability:raise ValueError('CONTOUR_CAPABILITY')
        self.worker,self.request,self.c,self.context,self.runtime=worker,request,c,context,runtime
        self.job=digest([worker.inbox.get('scope'),request,c])
        scope='SATOSHIFI_CONTOUR_MINT_V1:'+digest([worker.c['chain_id'],request['consumer'],c['mint_admission']])
        self.budget=Budget(c['identity_file'],scope,c['budget'],self.job,guard=self.ready)
        self.key='contour-mint:'+self.job
        self.budget.journal.put(self.key,dict(config=c,scope=worker.inbox.get('scope'),request=request))

    def save(self):self.budget.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason,**facts):
        transactions=[]
        if status=='SATISFIED':
            expected=self.expected_mint_method()
            if expected and not any(p['steps'][0]['method']==expected for p in self.record.get('operations',{}).values()):
                raise ValueError('CONTOUR_MINT_PROOF_PROFILE')
            from .contour_calls import evidence
            for profile in self.record.get('operations',{}).values():
                entry=original(self.runtime.registry.transaction_journal,profile['deployment_id'],profile['steps'][0],self.c['identity_file'])
                transactions.append(evidence(profile,entry,self.receipt(profile,entry)))
        return dict(status=status,observations=dict(reason=reason,mode=self.c['mode'],txid=self.request['txid'],
            vout=self.request['vout'],ready=False,production_ready=False,transactions=transactions,**facts))

    def expected_mint_method(self):
        expected=getattr(self.runtime,'expected_proof_version',None)
        if expected is None:return None
        if self.c['mode']=='ADDRESS' and expected!='2':raise ValueError('CONTOUR_MINT_PROOF_PROFILE')
        return 'mintFromAddressProof' if self.c['mode']=='ADDRESS' else 'mintFromProof' if expected=='1' else 'mintFromProofV2'

    def check_proof_version(self):
        expected=getattr(self.runtime,'expected_proof_version',None)
        if expected is None:return
        from signer_agent.evm_network import read_block
        rpc=self.runtime.registry.w3;b=read_block(rpc,int(self.worker.c['evm_confirmations']))
        pool=rpc.eth.contract(address=self.request['consumer'],abi=artifact('MiningPoolV1')['abi'])
        if int(pool.functions.mintProofVersion().call(block_identifier=b.number))!=int(expected):
            raise ValueError('CONTOUR_MINT_PROOF_PROFILE')

    def historical_v1(self,profile):
        from signer_agent.bitcoin_funding import _mint_complete
        from signer_agent.evm_network import read_block
        rpc=self.runtime.registry.w3;b=read_block(rpc,int(self.worker.c['evm_confirmations']))
        pool=rpc.eth.contract(address=self.request['consumer'],abi=artifact('MiningPoolV1')['abi'])
        args=profile['steps'][0]['args']
        original=[bytes.fromhex(self.request['txid'])[::-1],int(self.request['vout']),args[5],[int(x) for x in args[6]]]
        if not _mint_complete(pool,original,b):raise Hold()
        if rpc.eth.get_block(b.number).hash!=b.hash:raise ValueError('BTC_REGISTRY_READ_REORG')

    def ready(self):
        w=self.worker;b,state,adapter,_,core=w.context()
        from signer_agent.zk_spv import inspect_live
        inspect_live(w.bitcoin,state,w.c['bitcoin_policy'],block_identifier=b.number)
        observed=w.observe_need(self.request,b,adapter,core)
        if observed['status']!='READY' or not w.aligned(state,b,core):raise ValueError('CONTOUR_MINT_CURRENT_MEMBERSHIP_REQUIRED')
        self.runtime.review_recipient()
        self.check_proof_version()
        w.stable(b)

    def runner(self,profile):
        owner=self
        class Bounded(Bootstrap):
            def build_transaction(self,step,account,nonce):
                tx=super().build_transaction(step,account,nonce);owner.budget.reserve(profile['deployment_id'],tx);return tx
        return Bounded(profile,self.runtime.registry.transaction_journal)

    def receipt(self,profile,entry):
        runner=self.runner(profile);step=profile['steps'][0];target=profile['contracts']['target']
        exact(entry,step,self.worker.c['chain_id'],target['address'],target['artifact'],self.c['identity_file'],self.c['budget'])
        from web3.exceptions import TransactionNotFound
        try:return runner.verify_receipt(entry['result'])
        except TransactionNotFound:return None

    def apply(self,profile,recover):
        runner=self.runner(profile)
        result=runner.apply(self.c['identity_file'],before_submit=(lambda steps:True) if recover else None)
        if result['status']!='VERIFIED':raise Progress()
        entry=original(runner.journal.path,profile['deployment_id'],profile['steps'][0],self.c['identity_file'])
        return self.receipt(profile,entry)

    def submit(self,registry,profile):
        if self.used:raise Progress()
        step=profile['steps'][0]
        allowed={'BitcoinMintAdmission':{'openMint','openCustodyMint','openAddressMint'},
            'DKGAuthorization':{'authorizeOperation'},'MiningPoolV1':{'mintFromProof','mintFromProofV2','mintFromAddressProof'}}
        target=profile['contracts']['target']
        expected=self.expected_mint_method()
        if expected and target['artifact']=='MiningPoolV1' and step['method']!=expected:raise ValueError('CONTOUR_MINT_PROOF_PROFILE')
        if (len(profile['steps'])!=1 or step.get('kind')!='call' or step.get('value','0')!='0'
                or step['method'] not in allowed.get(target['artifact'],set())
                or target['artifact']=='BitcoinMintAdmission' and target['address']!=self.c['mint_admission']
                or target['artifact']=='MiningPoolV1' and target['address']!=self.request['consumer']):
            raise ValueError('CONTOUR_MINT_CALL_SCOPE')
        if target['address']!=self.runtime.targets()[target['artifact']]:raise ValueError('CONTOUR_MINT_CALL_SCOPE')
        operation=profile['deployment_id'];entry=original(registry.transaction_journal,operation,step,self.c['identity_file'])
        if entry['result'].get('raw_transaction'):
            receipt=self.receipt(profile,entry)
            if receipt is not None and entry['status']=='CONFIRMED':return receipt
        if self.action=='probe':raise Required()
        if operation not in self.record['operations']:
            self.ready();self.record['operations'][operation]=profile;self.save()
            recover=False
        else:
            if self.record['operations'][operation]!=profile:raise ValueError('CONTOUR_MINT_OPERATION')
            if not entry['result'].get('raw_transaction'):raise Hold()
            recover=True
        self.used=True;return self.apply(profile,recover)

    def pending(self):
        for operation,profile in self.record['operations'].items():
            entry=original(self.runtime.registry.transaction_journal,operation,profile['steps'][0],self.c['identity_file'])
            if not entry['result'].get('raw_transaction'):raise Hold()
            receipt=self.receipt(profile,entry)
            if receipt is not None and entry['status']=='CONFIRMED':continue
            if receipt is None and entry['status']=='CONFIRMED':raise Hold()
            if self.action=='probe':raise Required()
            self.used=True;self.apply(profile,True);raise Progress()

    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        self.action=action;self.used=False
        path=str(self.budget.journal.path)+'.mint.lock'
        import fcntl
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_MINT_BUSY') from None
            self.record=self.budget.journal.get(self.key)['result']
            if not self.record:
                if action=='recover':return self.result('RECOVERY_REQUIRED','MINT_ENROLLMENT_REQUIRED')
                self.record=dict(operations={})
                if action!='probe':self.save()
            self.runtime.registry.transaction_controller=self
            try:
                self.pending()
                # Each retained operation above has its original signed bytes
                # and canonical successful receipt checked again. An accepted
                # mint stays historical completion across a later pool version
                # change; reinterpreting its old claim as a new profile would
                # neither improve evidence nor authorize another mint.
                minted=[p for p in self.record['operations'].values() if p['steps'][0]['method'] in
                    ('mintFromProof','mintFromProofV2','mintFromAddressProof')]
                if minted and all(p['steps'][0]['method']=='mintFromProof' for p in minted):
                    for profile in minted:self.historical_v1(profile)
                    return self.result('SATISFIED','EXACT_MINT_CANONICAL_COMPLETION',
                        result=dict(status='BTC_FUNDING_EXECUTED',historical_receipt=True))
                # Execute first adopts an existing claim/completion. Missing
                # admission alone permits proposing the original frozen intent.
                try:result=self.runtime.dispatch('execute')
                except ValueError as exc:
                    if str(exc)!='BTC_MINT_ADMISSION_CLAIM_REQUIRED':raise
                    result=self.runtime.dispatch('propose')
                if result['status']=='BTC_FUNDING_EXECUTED':
                    if not any(p['steps'][0]['method'] in ('mintFromProof','mintFromProofV2','mintFromAddressProof')
                               for p in self.record['operations'].values()):
                        return self.result('RECOVERY_REQUIRED','EXACT_MINT_RECEIPT_REQUIRED')
                    return self.result('SATISFIED','EXACT_MINT_CANONICAL_COMPLETION',result=result)
                return self.result('WAITING',result['status'])
            except Required:return self.result('ABSENT','ORIGINAL_MINT_OPERATION_REQUIRED')
            except Hold:return self.result('RECOVERY_REQUIRED','ORIGINAL_MINT_RECOVERY_REQUIRED')
            except Progress:return self.result('ABSENT','ORIGINAL_MINT_OPERATION_PROGRESS')
            except ValueError as exc:
                if str(exc) in ('BTC_MINT_AUTHORIZATION_NOT_FINALIZED',):return self.result('WAITING',str(exc))
                raise
            finally:self.runtime.registry.transaction_controller=None
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('mint-config','mint-config-sha256','inputs-sha256','worker-config','worker-config-sha256','request-file','request-sha256'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--expected-proof-version',choices=('1','2'))
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);context=json.loads(read_private(path))
    c=load_config(a.mint_config);wc=worker_config(a.worker_config);request=json.loads(read_private(a.request_file));data=inputs(c,wc,request)
    public_inputs=[data[0],data[2],data[3]]
    if any(x!=y for x,y in ((digest(c),a.mint_config_sha256),(digest(public_inputs),a.inputs_sha256),
                            (digest(wc),a.worker_config_sha256),(digest(request),a.request_sha256))):raise ValueError('CONTOUR_MINT_SCOPE')
    w=Worker(wc)
    try:
        runtime=Runtime(c,wc,request,data)
        runtime.expected_proof_version=a.expected_proof_version
        result=Pipeline(w,request,c,context,runtime).execute(context['action'])
        artifacts=context['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
        if result['status']=='SATISFIED' and not artifacts:
            out=path.parent/'mint-evidence.json';private_write(out,result['observations'])
            artifacts={str(out.relative_to(context['directory'])):sha(out)}
        binding={k:context[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
        private_write(context['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))
    finally:w.close()


if __name__=='__main__':main()
