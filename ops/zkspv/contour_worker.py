"""Consumer/preparation/proving/publication adapters using the original worker engines.

The proof stage bypasses receipt-cache reuse. A private, durable enrollment must
precede its first local proving stage. This is local provenance, not a remote
attestation or production qualification. No mint or payment is authorized here.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import zipfile

from .contour import digest, private_write, relative_file, sha
from signer_agent.zk_worker import Worker, load_config, state_wire
from signer_agent.zk_worker_prover import LocalProver, read

PHASES = ('consumer_request', 'proof_preparation', 'fresh_proof', 'state_update')
FILES = ('preparation.json', 'native.json', 'state.input', 'state.journal')


class Pipeline:
    def __init__(self, worker, request, context, invocation):
        self.worker, self.request, self.context, self.invocation = worker, request, context, Path(invocation)
        if context['capability'] not in PHASES: raise ValueError('CONTOUR_CAPABILITY')
        self.phase = context['capability']
        self.key = 'contour-pipeline:' + digest([context['run_id'], context['plan_sha256']])
        self.immutable = dict(worker_config_sha256=digest(worker.c), scope=worker.inbox.get('scope'), request=request)
        self.lease = worker.inbox.get(self.key)
        if self.lease and self.lease['immutable'] != self.immutable: raise ValueError('CONTOUR_WORKER_SCOPE')

    def save(self): self.worker.inbox.put(self.key, self.lease)

    def retained_leases(self):return [*self.lease.get('history',[]),self.lease]

    def need_id(self):
        return digest(['satoshifi.local-spv-need.v1', self.worker.inbox.get('scope'), self.request])

    def base(self):
        attempt = self.lease['attempt']; identifier = attempt['id']
        if len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('CONTOUR_WORKER_ATTEMPT')
        for name, expected in attempt['files'].items():
            path = relative_file(self.worker.inbox.path, 'attempts/' + identifier + '/' + name)
            if sha(path) != expected: raise ValueError('CONTOUR_WORKER_ARTIFACT_CHANGED')
        base = self.worker.inbox.path / 'attempts' / identifier
        prep = read(base / 'preparation.json')
        from signer_agent.zk_profiles import profile as zk_profile
        if (prep['program_image_id'] != zk_profile(self.worker.c['bitcoin_policy'])['image'] or prep['input_sha256'] != sha(base/'state.input')
                or prep['journal_sha256'] != sha(base/'state.journal')
                or prep.get('native_sha256') != sha(base/'native.json')):
            raise ValueError('CONTOUR_WORKER_PREPARATION')
        from signer_agent.zk_worker_prover import ROOT
        for name, expected in prep['source_sha256'].items():
            if sha(relative_file(ROOT, name)) != expected: raise ValueError('CONTOUR_WORKER_SOURCE_CHANGED')
        return base

    def observation(self, status, reason, **facts):
        return dict(status=status, observations=dict(reason=reason, need_id=self.need_id(),
            worker_attempt=(self.lease or {}).get('attempt', {}).get('id'), production_ready=False, **facts))

    def current(self):
        block, state, adapter, records, core = self.worker.context(allow_halt=True)
        self.worker.stable(block)
        return block, state, adapter, records, core

    def service_guard(self):
        self.worker.new_work_guard()
        if self.lease.get('service'):
            from .contour_service import require
            require(self.worker,self.request,self.context,self.invocation,self.lease['service'])

    def execute(self, action):
        if action not in ('probe', 'run', 'recover'): raise ValueError('CONTOUR_RESPONSE')
        with self.worker.exclusive():
            # All phases authenticate current network/provenance/finality, including probes.
            self.current()
            if self.phase == 'consumer_request': return self.consumer(action)
            if not self.lease or not self.lease.get('admitted'):
                return self.observation('WAITING', 'CONSUMER_REQUEST_REQUIRED')
            if self.phase == 'proof_preparation': return self.prepare(action)
            if not self.lease.get('attempt'): return self.observation('WAITING', 'PROOF_PREPARATION_REQUIRED')
            base = self.base()
            return self.proof(action, base) if self.phase == 'fresh_proof' else self.publish(action, base)

    def consumer(self, action):
        if self.lease and self.lease.get('admitted'):
            rows = {k:r for k,r,_ in self.worker.inbox.needs()}
            if rows.get(self.need_id()) != self.request: raise ValueError('CONTOUR_WORKER_INBOX_RECOVERY_REQUIRED')
            return self.observation('SATISFIED', 'CANONICAL_CONSUMER_REQUEST_REGISTERED')
        if action == 'probe': return self.observation('ABSENT', 'CONSUMER_REQUEST_NOT_REGISTERED')
        if self.lease is None:
            self.lease = dict(immutable=self.immutable, admitted=False); self.save()
        result = self.worker.request(self.request)
        if result['local_need_id'] != self.need_id(): raise ValueError('CONTOUR_WORKER_NEED')
        self.lease['admitted'] = True; self.save()
        return self.observation('SATISFIED', 'CANONICAL_CONSUMER_REQUEST_REGISTERED')

    def prepare(self, action):
        if self.lease.get('attempt'):
            self.base()
            return self.observation('SATISFIED', 'EXACT_NATIVE_PREPARATION_RETAINED')
        if action == 'probe': return self.observation('ABSENT', 'NO_PINNED_PREPARATION')
        self.service_guard()
        block, state, adapter, _, core = self.current()
        if core is None: return self.observation('WAITING', 'STATE_HALTED')
        current=state_wire(state.functions.getState().call(block_identifier=block.number))
        active=self.worker.inbox.get('attempt')
        if active:
            native=read(relative_file(self.worker.inbox.path,'attempts/'+active['id']+'/native.json'))
            if native['after']==current:
                self.worker.retain_accepted_archive(active,native,current,block)
                active=None
        observed = self.worker.observe_need(self.request, block, adapter, core)
        if observed['status'] != 'NEEDS_STATE':
            return self.observation('WAITING', 'NO_NEW_STATE_PROOF_REQUIRED', consumer_status=observed['status'])
        current = state_wire(state.functions.getState().call(block_identifier=block.number))
        original = self.lease.get('preparing_state')
        if original is not None and original != current: return self.observation('RECOVERY_REQUIRED', 'PREPARATION_STATE_CHANGED')
        if active and self.need_id() not in active.get('need_ids', []):
            return self.observation('WAITING', 'OTHER_WORKER_ATTEMPT')
        self.lease['preparing_state'] = current; self.save()
        # Same consensus/native/admission code, with a hard boundary before any publication.
        result = self.worker._tick(boundary='prepare')
        active = self.worker.inbox.get('attempt')
        if result['status'] != 'PROOF_PREPARED' or not active or self.need_id() not in active.get('need_ids', []):
            return self.observation('WAITING', result['status'])
        identifier = active['id']
        if len(identifier) != 64 or any(c not in '0123456789abcdef' for c in identifier): raise ValueError('CONTOUR_WORKER_ATTEMPT')
        files = {name:sha(relative_file(self.worker.inbox.path, 'attempts/'+identifier+'/'+name)) for name in FILES}
        self.lease['attempt'] = dict(id=identifier, target_height=active['target_height'], target_hash=active['target_hash'], files=files)
        self.save(); self.base()
        return self.observation('SATISFIED', 'EXACT_NATIVE_PREPARATION_RETAINED')

    def proof_check(self, base):
        enrolled = self.lease.get('proof_started')
        if not enrolled: return None
        prover = LocalProver(self.worker.c, self.worker.lock_fd)
        reports = {}
        for stage in ('composite', 'succinct', 'groth16'):
            folder = base / stage
            if not (folder/'report.json').exists():
                if self.lease.get('proof_completed'):raise ValueError('CONTOUR_WORKER_PROOF_CHANGED')
                return None
            report = read(relative_file(base, stage+'/report.json'))
            for name in report.get('artifacts', {}): relative_file(folder, name)
            if not prover.verified_stage(base, folder):
                if self.lease.get('proof_completed'):raise ValueError('CONTOUR_WORKER_PROOF_CHANGED')
                return None
            reports[stage] = sha(folder/'report.json')
        receipt = relative_file(base, 'groth16/receipt.bin')
        completion = dict(receipt_sha256=sha(receipt), stage_reports=reports)
        if self.lease.get('proof_completed') not in (None, completion): raise ValueError('CONTOUR_WORKER_PROOF_CHANGED')
        seal = prover.verify(base, receipt, self.invocation/'receipt-inspection')
        if len(seal) != 260: raise ValueError('CONTOUR_WORKER_SEAL')
        prover.retain_local(base,receipt)
        self.lease['proof_completed'] = completion; self.save()
        return seal

    def proof(self, action, base):
        owner = self.lease.get('proof_started')
        if owner and owner['owner'] != self.context['attempt_id']: raise ValueError('CONTOUR_WORKER_PROOF_OWNER')
        if self.proof_check(base) is not None:
            return self.observation('SATISFIED', 'FRESH_LOCAL_STAGES_AND_RECEIPT_VERIFIED',
                                    receipt_sha256=self.lease['proof_completed']['receipt_sha256'])
        if not owner and any((base/name).exists() for name in ('composite','succinct','groth16','proof-result.json',
                                      'composite.running.json','succinct.running.json','groth16.running.json')):
            return self.observation('RECOVERY_REQUIRED', 'UNENROLLED_PROOF_ARTIFACTS')
        if action == 'probe': return self.observation('ABSENT', 'LOCAL_PROVING_REQUIRED')
        active = self.worker.inbox.get('attempt'); block, state, _, _, _ = self.current()
        if (not active or active['id'] != self.lease['attempt']['id'] or active['phase'] == 'SENDING'
                or state_wire(state.functions.getState().call(block_identifier=block.number)) != read(base/'native.json')['before']):
            return self.observation('RECOVERY_REQUIRED', 'PROVING_CONTEXT_CHANGED')
        if self.worker.bitcoin.call('getblockhash', active['target_height']) != active['target_hash']:
            return self.observation('RECOVERY_REQUIRED', 'PROVING_BITCOIN_CHANGED')
        from signer_agent.zk_worker_service import preflight
        self.service_guard()
        preflight(self.worker, active.get('funding_need_ids', active.get('need_ids', [])))
        if not owner:
            self.lease['proof_started'] = dict(owner=self.context['attempt_id'],preparation_sha256=sha(base/'preparation.json'))
            self.save()  # Before any local stage. Receipt cache remains intact and is not consulted.
        LocalProver(self.worker.c, self.worker.lock_fd).prove(base, use_cache=False,max_new_stages=1)
        if self.proof_check(base) is None:return self.observation('ABSENT','LOCAL_STAGE_PROGRESS')
        return self.observation('SATISFIED', 'FRESH_LOCAL_STAGES_AND_RECEIPT_VERIFIED',
                                receipt_sha256=self.lease['proof_completed']['receipt_sha256'])

    def transaction(self):
        from signer_agent.operations import Journal
        path = self.worker.inbox.path/'transactions.sqlite'
        if not path.is_file(): return None
        try: return Journal(path).get('zk-worker-'+self.lease['attempt']['id']+':transition')
        except KeyError: return None

    def exact_transaction(self, record, step):
        from eth_account import Account
        from eth_account.typed_transactions import TypedTransaction
        from eth_account._utils.legacy_transactions import Transaction
        from eth_abi import encode
        from hexbytes import HexBytes
        from web3 import Web3
        from signer_agent.onboarding import read_private
        if record['request'] != step: raise ValueError('CONTOUR_WORKER_TRANSACTION')
        result = record['result']; raw = bytes.fromhex(result['raw_transaction'].removeprefix('0x'))
        account = Account.from_key(json.loads(read_private(self.worker.c['identity_file']))['private_key'])
        if Web3.keccak(raw).hex() != result['tx_hash'] or Account.recover_transaction(raw) != account.address:
            raise ValueError('CONTOUR_WORKER_TRANSACTION')
        if raw[0] in (1,2):
            decoded=TypedTransaction.from_bytes(HexBytes(raw)).as_dict();chain=decoded['chainId']
        elif raw[0]>=0xc0:
            decoded=Transaction.from_bytes(raw).as_dict();chain=(decoded['v']-35)//2 if decoded['v']>=35 else None
        else:raise ValueError('CONTOUR_WORKER_TRANSACTION')
        data=Web3.keccak(text='submitTransition(bytes,bytes)')[:4]+encode(['bytes','bytes'],
                [bytes.fromhex(v.removeprefix('0x')) for v in step['args']])
        fee=decoded.get('maxFeePerGas',decoded.get('gasPrice',0))*decoded['gas']+decoded['value']
        if (chain!=int(self.worker.c['chain_id']) or bytes(decoded['to'])!=bytes.fromhex(self.worker.c['zk_state'][2:])
                or bytes(decoded['data'])!=data or decoded['value']!=0 or decoded['nonce']!=int(result['nonce'])
                or decoded['gas']>int(self.worker.c['max_tx_gas']) or fee>int(self.worker.c['max_transaction_cost_wei'])):
            raise ValueError('CONTOUR_WORKER_TRANSACTION')
        return result

    def publish(self, action, base):
        seal = self.proof_check(base)
        if seal is None: return self.observation('WAITING', 'FRESH_PROOF_REQUIRED')
        from signer_agent.zk_spv_cli import transition_step
        from signer_agent.bootstrap import Bootstrap
        from signer_agent.transaction_wait import observation_pending
        step = transition_step(self.worker.c['chain_id'],self.worker.c['zk_state'],self.worker.c['bitcoin_policy'],
                               (base/'state.journal').read_bytes(),seal)
        record = self.transaction(); signed = record and record['result'].get('raw_transaction')
        if signed:
            if record['status'] not in ('SIGNED','BROADCAST','CONFIRMING','CONFIRMED'):
                return self.observation('RECOVERY_REQUIRED','STATE_TRANSACTION_ENGINE_HOLD')
            result = self.exact_transaction(record, step)
            checker = Bootstrap.__new__(Bootstrap); checker.w3 = self.worker.w3; checker.profile = self.worker.c
            receipt = checker.observe_receipt(result)
            if receipt is None and (record['status']=='CONFIRMED' or result.get('block_hash')):
                return self.observation('WAITING','EVM_RECEIPT_PENDING')
            if receipt is not None:
                try:
                    transaction = self.worker.w3.eth.get_transaction(result['tx_hash'])
                except Exception as error:
                    if not observation_pending(error):raise
                    return self.observation('WAITING','EVM_RECEIPT_PENDING')
                _, state, _, _, _ = self.current()
                expected = state.functions.submitTransition((base/'state.journal').read_bytes(),seal)._encode_transaction_data()
                actual = transaction['input']; actual = bytes.fromhex(actual.removeprefix('0x')) if isinstance(actual,str) else bytes(actual)
                if (transaction['to'] != state.address or actual != bytes.fromhex(expected.removeprefix('0x'))
                        or int(transaction['nonce']) != int(result['nonce'])):
                    raise ValueError('CONTOUR_WORKER_TRANSACTION')
                if record['status'] == 'CONFIRMED':
                    return self.observation('SATISFIED','EXACT_STATE_TRANSACTION_CONFIRMED',tx_hash=result['tx_hash'],
                        block_number=str(receipt.blockNumber),block_hash=receipt.blockHash.hex())
            if action == 'probe':
                return self.observation('ABSENT','ORIGINAL_STATE_TRANSACTION_PENDING')
        elif action == 'probe': return self.observation('ABSENT','STATE_TRANSACTION_NOT_SIGNED')
        if action == 'recover' and not signed:
            return self.observation('RECOVERY_REQUIRED','NO_SIGNED_STATE_TRANSACTION_TO_RECOVER')
        if signed:
            # Recover only the checked original signed envelope, including lost local
            # completion writes after an accepted state. No new nonce or proving work.
            _, _, _, records, _ = self.current()
            self.worker.publication_needs = []
            try:
                result = self.worker.publish('zk-worker-'+self.lease['attempt']['id'],step,records)
            except Exception as error:
                if not observation_pending(error):raise
                return self.observation('WAITING','EVM_RECEIPT_PENDING')
        else:
            active = self.worker.inbox.get('attempt')
            if not active or active['id'] != self.lease['attempt']['id']:
                return self.observation('RECOVERY_REQUIRED','PUBLICATION_CONTEXT_CHANGED')
            self.service_guard()
            result = self.worker._tick(boundary='publish')
        return self.observation('WAITING',result['status'])  # Completion requires a separate canonical probe.


def emit(context, invocation, result, pipeline):
    artifacts = {}
    if result['status'] == 'SATISFIED':
        artifacts = context['prior'].get('artifacts', {})
        if not artifacts:
            snapshot = invocation/'worker-evidence.json'; private_write(snapshot,result['observations'])
            artifacts[str(snapshot.relative_to(context['directory']))] = sha(snapshot)
            if pipeline.phase in ('proof_preparation','fresh_proof','state_update'):
                base = pipeline.base()
                names = FILES if pipeline.phase=='proof_preparation' else (*FILES,'groth16/receipt.bin')
                for name in names:
                    destination = invocation/name.replace('/','-')
                    with relative_file(base,name).open('rb') as source, destination.open('xb') as out:
                        os.chmod(destination,0o600); shutil.copyfileobj(source,out);out.flush();os.fsync(out.fileno())
                    artifacts[str(destination.relative_to(context['directory']))] = sha(destination)
                if pipeline.phase!='proof_preparation':
                    # All retained catch-up chunks and their exact original
                    # EVM envelopes travel with the binary proof evidence.
                    archive=invocation/'proof-lineage.zip';current=pipeline.lease
                    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_STORED) as out:
                        os.chmod(archive,0o600)
                        try:
                            for i,lease in enumerate(pipeline.retained_leases()):
                                pipeline.lease=lease;base=pipeline.base();prefix=str(i)+'/'
                                out.writestr(prefix+'contour-enrollment.json',json.dumps(lease,sort_keys=True))
                                for name in FILES:out.write(relative_file(base,name),prefix+name)
                                if (base/'proof-result.json').exists():out.write(relative_file(base,'proof-result.json'),prefix+'proof-result.json')
                                for stage in ('composite','succinct','groth16'):
                                    report=read(relative_file(base,stage+'/report.json'))
                                    for name in ('report.json',*report.get('artifacts',{})):
                                        source=relative_file(base,stage+'/'+name);out.write(source,prefix+stage+'/'+name)
                                entry=pipeline.transaction()
                                if entry:out.writestr(prefix+'original-state-transaction.json',json.dumps(entry,sort_keys=True))
                        finally:pipeline.lease=current
                    artifacts[str(archive.relative_to(context['directory']))]=sha(archive)
    binding = {k:context[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(context['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,
                                              observations=result['observations']))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--phase',required=True,choices=PHASES);p.add_argument('--worker-config',required=True)
    p.add_argument('--request-file',required=True)
    p.add_argument('--worker-config-sha256',required=True);p.add_argument('--request-sha256',required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    from signer_agent.onboarding import read_private
    _protect_transport_logs()
    path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT'])
    if path.stat().st_size>1024*1024 or Path(a.request_file).stat().st_size>65536:raise ValueError('CONTOUR_RESPONSE')
    context=read(path)
    if context['capability']!=a.phase:raise ValueError('CONTOUR_CAPABILITY')
    config=load_config(a.worker_config);request=json.loads(read_private(a.request_file))
    if digest(config)!=a.worker_config_sha256 or digest(request)!=a.request_sha256:raise ValueError('CONTOUR_WORKER_SCOPE')
    worker=Worker(config)
    try:
        pipeline=Pipeline(worker,request,context,path.parent)
        result=pipeline.execute(context['action']);emit(context,path.parent,result,pipeline)
    finally:worker.close()


if __name__=='__main__':main()
