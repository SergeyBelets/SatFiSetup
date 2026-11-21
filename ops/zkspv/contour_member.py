"""Root-bound header materialization, using the admitted archive and worker gas/nonce engine."""
import argparse
import json
import os
from pathlib import Path

from .contour import digest,private_write,sha
from .contour_tx import original,exact
from signer_agent.bootstrap import Bootstrap
from signer_agent.bitcoin_network import header_hash
from signer_agent.onboarding import read_private
from signer_agent.zk_worker import Worker,load_config,state_wire
from signer_agent.zk_spv_cli import materialization_step
from signer_agent.zk_profiles import profile as zk_profile


class Pipeline:
    def __init__(self,worker,request,context):
        if context['capability']!='membership':raise ValueError('CONTOUR_CAPABILITY')
        self.worker,self.request,self.context=worker,request,context
        self.key='contour-member:'+digest([context['run_id'],context['plan_sha256']])
        self.immutable=dict(worker_config_sha256=digest(worker.c),scope=worker.inbox.get('scope'),request=request)

    def save(self):self.worker.inbox.put(self.key,self.record)
    def result(self,status,reason,**facts):
        return dict(status=status,observations=dict(reason=reason,ready=False,production_ready=False,**facts))

    def cache(self,adapter,number,member,root):
        value=adapter.functions.materializedHeaders(header_hash(bytes.fromhex(member['header']))).call(block_identifier=number)
        return list(value)==[bytes.fromhex(root),bytes.fromhex(member['header'])[36:68],int(member['height'])]

    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        w=self.worker
        with w.exclusive():
            self.record=w.inbox.get(self.key)
            if self.record and self.record['immutable']!=self.immutable:raise ValueError('CONTOUR_MEMBER_SCOPE')
            b,state,adapter,records,core=w.context(allow_halt=True)
            if self.record and self.record.get('cached'):
                saved=self.record['cached'];n=int(saved['number'])
                from signer_agent.evm_network import verify_anchor
                from signer_agent.transaction_wait import observation_pending
                try:
                    verify_anchor(w.w3,n,saved['hash'],safe=b)
                    cached=self.cache(adapter,n,self.record['member'],self.record['root'])
                except Exception as error:
                    if observation_pending(error):return self.result('WAITING','EVM_RPC_BEHIND')
                    if not isinstance(error,ValueError) or str(error)!='EVM_REORG_RECOVERY_REQUIRED':raise
                    cached=False
                if not cached:
                    return self.result('RECOVERY_REQUIRED','CACHED_MEMBERSHIP_LOST')
                w.stable(b);return self.result('SATISFIED','CANONICAL_CACHED_MEMBERSHIP',root=self.record['root'])
            if self.record and self.record.get('operation'):
                step=materialization_step(self.record['member']);name=self.record['operation']
                entry=original(w.inbox.path/'transactions.sqlite',name,step,w.c['identity_file'])
                if entry['result'].get('raw_transaction'):
                    signed=exact(entry,step,w.c['chain_id'],adapter.address,zk_profile(w.c['bitcoin_policy'])['adapter'],w.c['identity_file'],w.c)
                    checker=Bootstrap.__new__(Bootstrap);checker.w3=w.w3;checker.profile=w.c
                    receipt=checker.observe_receipt(signed)
                    if receipt is not None and entry['status']=='CONFIRMED':
                        from web3.logs import DISCARD
                        events=adapter.events.HeaderMaterialized().process_receipt(receipt,errors=DISCARD)
                        expected=header_hash(bytes.fromhex(self.record['member']['header']))
                        if not any(e['address']==adapter.address and bytes(e['args']['blockHash'])==expected
                            and int(e['args']['height'])==int(self.record['member']['height'])
                            and bytes(e['args']['canonicalRoot']).hex()==self.record['root'] for e in events):
                            raise ValueError('CONTOUR_MEMBER_RECEIPT')
                        return self.result('SATISFIED','EXACT_MEMBERSHIP_RECEIPT',tx_hash=signed['tx_hash'],root=self.record['root'])
                    if receipt is None and (entry['status']=='CONFIRMED' or signed.get('block_hash')):
                        return self.result('WAITING','EVM_RECEIPT_PENDING')
                    if action=='probe':return self.result('ABSENT','SIGNED_MEMBERSHIP_PENDING')
                    w.publication_needs=[]
                    from signer_agent.transaction_wait import observation_pending
                    try:
                        result=w.publish(name,step,records,recover_only=True)
                    except Exception as error:
                        if not observation_pending(error):raise
                        return self.result('WAITING','EVM_RECEIPT_PENDING')
                    return self.result('RECOVERY_REQUIRED' if result['status']=='NOT_SIGNED' else 'ABSENT',result['status'])
                if self.record.get('started'):return self.result('RECOVERY_REQUIRED','UNSIGNED_MEMBERSHIP_RECOVERY')
            if core is None:return self.result('WAITING','STATE_HALTED')
            observed=w.observe_need(self.request,b,adapter,core)
            if observed['status'] not in ('READY','NEEDS_STATE'):return self.result('WAITING',observed['status'])
            current=state_wire(state.functions.getState().call(block_identifier=b.number))
            if (not w.aligned(state,b,core) or not state.functions.isFresh().call(block_identifier=b.number)
                    or observed['height']+observed['depth']-1>int(current[3])):
                return self.result('WAITING','FRESH_COVERING_STATE_REQUIRED')
            member=dict(height=str(observed['height']),header=observed['header'],siblings=[])
            if self.cache(adapter,b.number,member,current[6]):
                self.record=self.record or dict(immutable=self.immutable)
                self.record.update(member=member,root=current[6],cached=dict(number=str(b.number),hash=b.hash.hex()))
                w.stable(b);self.save();return self.result('SATISFIED','CANONICAL_CACHED_MEMBERSHIP',root=current[6])
            if action=='probe':return self.result('ABSENT','HEADER_MATERIALIZATION_REQUIRED')
            if not self.record:
                if action=='recover':return self.result('RECOVERY_REQUIRED','MEMBERSHIP_ENROLLMENT_REQUIRED')
                self.record=dict(immutable=self.immutable);self.save()
            if self.record.get('root') and self.record['root']!=current[6]:return self.result('RECOVERY_REQUIRED','MEMBERSHIP_ROOT_CHANGED')
            if not self.record.get('operation'):
                active=w.inbox.get('attempt')
                if active:
                    from signer_agent.zk_worker_prover import read
                    native=read(w.inbox.path/'attempts'/active['id']/'native.json')
                    if native['after']==current:w.retain_accepted_archive(active,native,current,b)
                archive=w.archive_ref(current) if w.c.get('archive') else w.inbox.get('archive:'+current[7])
                if archive is None and current[11]!='1':raise ValueError('ZK_ARCHIVE_RECOVERY_REQUIRED')
                out=w.inbox.path/'membership'/current[7]
                native=w.native(archive or [],[],[observed['height']],out)
                if native['before']!=current:raise ValueError('ZK_ARCHIVE_STATE_MISMATCH')
                member=next((m for m in native['memberships'] if int(m['height'])==observed['height'] and m['header']==observed['header']),None)
                if member is None:raise ValueError('ZK_HEADER_MEMBERSHIP_UNAVAILABLE')
                materialization_step(member)
                self.record.update(member=member,root=current[6],operation='zk-member-'+digest([current[6],member]));self.save()
                return self.result('ABSENT','EXACT_MEMBERSHIP_PREPARED')
            from signer_agent.zk_worker_service import preflight,source
            key=digest(['satoshifi.local-spv-need.v1',w.inbox.get('scope'),self.request])
            paid=source(w,[key],allow_unpaid=True)['source']=='PAID_SERVICE'
            w.publication_needs=[key if paid else 'maintenance:'+key]
            preflight(w,w.publication_needs)
            # The original archive and the adapter's current-root verifier both
            # check the proof; materialization never proves or advances state.
            step=materialization_step(self.record['member'])
            from signer_agent.bootstrap import encode_args,artifact
            spec=next(x for x in artifact(zk_profile(w.c['bitcoin_policy'])['adapter'])['abi'] if x.get('name')=='materializeHeader')
            adapter.functions.materializeHeader(*encode_args(spec['inputs'],step['args'])).call(block_identifier=b.number)
            if self.record['member']['header']!=observed['header']:raise ValueError('CONTOUR_MEMBER_HEADER_CHANGED')
            w.stable(b);self.record['started']=True;self.save()
            result=w.publish(self.record['operation'],step,records)
            return self.result('ABSENT',result['status'])


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('worker-config','worker-config-sha256','request-file','request-sha256'):p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);context=json.loads(read_private(path))
    c=load_config(a.worker_config);request=json.loads(read_private(a.request_file))
    if digest(c)!=a.worker_config_sha256 or digest(request)!=a.request_sha256:raise ValueError('CONTOUR_MEMBER_SCOPE')
    w=Worker(c)
    try:
        result=Pipeline(w,request,context).execute(context['action'])
        artifacts=context['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
        if result['status']=='SATISFIED' and not artifacts:
            out=path.parent/'membership-evidence.json';private_write(out,result['observations'])
            artifacts={str(out.relative_to(context['directory'])):sha(out)}
        binding={k:context[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
        private_write(context['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))
    finally:w.close()


if __name__=='__main__':main()
