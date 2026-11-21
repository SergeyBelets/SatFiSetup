"""Bounded original-fleet observation and fresh retained-threshold review; no spending or rotation."""
import argparse
import fcntl
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .contour import digest,private_write,sha
from signer_agent.epoch_service import Fleet,load_config as fleet_config
from signer_agent import epoch_retention as retention
from signer_agent.address_transport import load_config as address_config
from signer_agent.btc_fee_reserve import load_config as fee_config
from signer_agent.onboarding import read_private


def load_config(path):
    c=json.loads(read_private(path))
    if (not isinstance(c,dict) or set(c)!={'version','fleet_config','challenge_file','attestation_files'} or c['version']!='1'
            or any(not isinstance(c[k],str) or not Path(c[k]).is_absolute() for k in ('fleet_config','challenge_file'))
            or not isinstance(c['attestation_files'],list) or len(c['attestation_files'])>512
            or any(not isinstance(p,str) or not Path(p).is_absolute() for p in c['attestation_files'])
            or len(set(c['attestation_files']))!=len(c['attestation_files'])):
        raise ValueError('CONTOUR_EPOCH_CONFIG')
    return c


def small_json(path):
    if Path(path).stat().st_size>16384:raise ValueError('CONTOUR_EPOCH_INPUT_LIMIT')
    return json.loads(read_private(path))


def matching(ac,wc):
    return (all(ac[k]==wc[k] for k in ('chain_id','bitcoin_policy','evm_confirmations'))
        and wc['allowed_consumers'].get(ac['pool'])=='MiningPoolV1')


def inputs(c,wc):
    fc=fleet_config(c['fleet_config']);entries=[]
    for e in fc['epochs']:
        ac=address_config(e['address_config'])
        if ac['bitcoin_network']!=wc['bitcoin_policy']['network'] or ac['version']!='2':raise ValueError('CONTOUR_EPOCH_SCOPE')
        fee=fee_config(ac['fee_reserve_config'])
        if any(fee.get(k)!=ac[k] for k in ('chain_id','bitcoin_network','bitcoin_policy','evm_confirmations','identity_file')):
            raise ValueError('CONTOUR_EPOCH_SCOPE')
        # Pin retained transport files without exporting their private material.
        keys=[]
        for p in ac['key_files']:
            read_private(p);keys.append([p,sha(Path(p))])
        entries.append(dict(wallet_context=e['wallet_context'],address=ac,fee=fee,key_sha256=keys))
    if (not any(e['mode']=='SERVICING' for e in fc['epochs'])
            or not any(matching(e['address'],wc) for e in entries)):raise ValueError('CONTOUR_EPOCH_SCOPE')
    request=small_json(c['challenge_file']);retention.validate_challenge(SimpleNamespace(c=fc),request)
    packets=[small_json(p) for p in c['attestation_files']]
    return fc,entries,request,packets


class ScopedFleet(Fleet):
    def snapshot(self,e,client):
        s=super().snapshot(e,client)
        if matching(self.configs[e['wallet_context']],self.worker):
            from signer_agent.zk_spv import inspect_binding
            from signer_agent.address_mint import read
            inspect_binding(client.w3,self.worker['zk_state'],self.worker['bitcoin_policy'],s['block'].number,
                adapter_address=read(s['context']['common'],s['block'].number,'spv'))
        return s


class Runtime:
    def __init__(self,c,wc,data):
        self.c=c;self.input_hash=digest(data);self.fleet=ScopedFleet(data[0],configuration=c['fleet_config']);self.fleet.worker=wc
        self.request,self.packets=data[2:];self.journal=self.fleet.lane
        # An imported challenge file cannot enroll itself as review authority.
        try:saved=self.journal.get('epoch-review:'+self.request['challenge'])['request']
        except KeyError:raise ValueError('EPOCH_RETENTION_REQUEST') from None
        if saved!=self.request:raise ValueError('EPOCH_RETENTION_REQUEST')
    def scan(self):return self.fleet.run_once(schedule_spv=False)
    def review(self):
        with self.fleet.exclusive():return retention.review(self.fleet,self.request,self.packets)


def scan_summary(row):
    result={k:row[k] for k in ('wallet_context','mode','anchor') if k in row}
    result['observation_sha256']=digest(row)
    phases={}
    for name in ('archive','bindings','transport','watch','retained'):
        p=row.get(name,{})
        phases[name]={k:p[k] for k in ('status','reason','missing') if k in p}
    fee=row.get('fee_observations',{});phases['fee_observations']={k:fee[k] for k in ('status','reason') if k in fee}
    phases['fee_archives']=[]
    for r in fee.get('archives',[]):
        o=r.get('observation',{});phases['fee_archives'].append(dict(status=o.get('status'),reason=o.get('reason'),
            watch=o.get('watch',{}).get('status'),archive=o.get('archive',{}).get('status')))
    funding=row.get('funding',{});phases['funding']=dict(status=funding.get('reserve',{}).get('status'),reason=funding.get('reason'))
    phases['funding_watches']=[dict(archive=r.get('archive',{}).get('status'),scan=r.get('scan',{}).get('status')) for r in funding.get('fee_watches',[])]
    result['phases']=phases
    if 'reason' in row:result['reason']=row['reason']
    return result


def complete_scan(s):
    p=s['phases']
    if any(p[k].get('status')!=v for k,v in dict(archive='ARCHIVE_RETAINED',bindings='BINDINGS_OBSERVED',
        transport='RECEIVED',retained='ARCHIVE_RETAINED').items()):return False
    if p['watch'].get('status') not in ('WATCH_COVERED','IDLE'):return False
    if p['fee_observations'].get('status') not in ('FEE_ARCHIVES_OBSERVED','NO_FEE_PURPOSE_ARCHIVE'):return False
    if any(r['status'] is not None or r['watch'] not in ('WATCH_COVERED','IDLE') or r['archive']!='ARCHIVE_RETAINED' for r in p['fee_archives']):return False
    if p['funding']['status']!='SUFFICIENT_QUALIFICATION_REQUIRED':return False
    return all(r['archive']=='ARCHIVE_RETAINED' and r['scan'] in ('WATCH_COVERED','IDLE') for r in p['funding_watches'])


class Pipeline:
    def __init__(self,runtime,c,context):
        if context['capability']!='old_epoch_service':raise ValueError('CONTOUR_CAPABILITY')
        self.r,self.c=runtime,c;self.journal=runtime.journal;self.key='contour-epoch:'+digest([c,runtime.input_hash])
        self.journal.put(self.key,dict(config=c,inputs_sha256=runtime.input_hash))
    def save(self):self.journal.update(self.key,'ENROLLED',self.record)
    def result(self,status,reason,review=None):
        return dict(status=status,observations=dict(reason=reason,fleet_id=self.r.fleet.c['fleet_id'],
            challenge=self.r.request['challenge'],epochs=(review or {}).get('epochs',[]),
            observed_contexts=sorted(self.record.get('observed',{})),
            sweep=[self.record['observed'][k] for k in sorted(self.record.get('observed',{}))],
            evm_operations_performed=0,bitcoin_broadcasts_performed=0,ready=False,production_ready=False))
    def check(self):
        expected={e['wallet_context'] for e in self.r.fleet.c['epochs']};observed=self.record.get('observed',{})
        if set(observed)!=expected:return self.result('ABSENT','BOUNDED_EPOCH_SWEEP_REQUIRED')
        def archive_failure(value):
            if isinstance(value,list):return any(archive_failure(v) for v in value)
            if not isinstance(value,dict):return False
            return (any(value.get(k)=='ARCHIVE_RECOVERY_REQUIRED' for k in ('status','archive'))
                or value.get('reason') in ('EPOCH_ARCHIVE_CONFLICT','EPOCH_ARCHIVE_REQUIRED','ADDRESS_DESCRIPTOR_CONFLICT')
                or any(archive_failure(v) for v in value.values()))
        for s in observed.values():
            if archive_failure(s):
                return self.result('RECOVERY_REQUIRED','ORIGINAL_EPOCH_ARCHIVE_REQUIRED')
        if not all(complete_scan(s) for s in observed.values()):return self.result('ABSENT','EPOCH_OBSERVATION_INCOMPLETE')
        review=self.r.review()
        if review['status']=='RETENTION_CHECKED':return self.result('SATISFIED','FRESH_ORIGINAL_EPOCH_SERVICE_AND_THRESHOLD_CHECKED',review)
        if any(e.get('reason') in ('EPOCH_WATCH_INCOMPLETE','ADDRESS_CORE_CHANGED') for e in review['epochs']):
            return self.result('ABSENT','BOUNDED_EPOCH_CATCHUP_REQUIRED',review)
        return self.result('RECOVERY_REQUIRED','FRESH_EPOCH_RETENTION_REVIEW_REQUIRED',review)
    def execute(self,action):
        if action not in ('probe','run','recover'):raise ValueError('CONTOUR_RESPONSE')
        fd=os.open(str(self.journal.path)+'.contour-epochs.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('CONTOUR_EPOCH_BUSY') from None
            self.record=self.journal.get(self.key)['result']
            if not self.record:
                self.record=dict(observed={})
                if action=='probe':return self.result('ABSENT','ORIGINAL_EPOCH_SERVICE_SWEEP_REQUIRED')
                if action=='recover':return self.result('RECOVERY_REQUIRED','EPOCH_ENROLLMENT_REQUIRED')
                self.save()
            if action=='probe':return self.check()
            result=self.r.scan()
            if result['status']!='EPOCH_SERVICE_OBSERVED':raise ValueError('CONTOUR_EPOCH_OBSERVATION')
            expected={e['wallet_context'] for e in self.r.fleet.c['epochs']}
            for row in result['epochs']:
                if row['wallet_context'] not in expected:raise ValueError('CONTOUR_EPOCH_SCOPE')
                self.record['observed'][row['wallet_context']]=scan_summary(row)
            self.save()
            return self.result('ABSENT','BOUNDED_ORIGINAL_EPOCH_OBSERVATION_RETAINED')
        finally:os.close(fd)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('epoch-config','epoch-config-sha256','inputs-sha256','worker-config','worker-config-sha256'):p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    from signer_agent.zk_worker import load_config as worker_config
    _protect_transport_logs();path=Path(os.environ['SATOHSI_CONTOUR_CONTEXT']);ctx=json.loads(read_private(path))
    c=load_config(a.epoch_config);wc=worker_config(a.worker_config);data=inputs(c,wc)
    if digest(c)!=a.epoch_config_sha256 or digest(wc)!=a.worker_config_sha256 or digest(data)!=a.inputs_sha256:
        raise ValueError('CONTOUR_EPOCH_SCOPE')
    result=Pipeline(Runtime(c,wc,data),c,ctx).execute(ctx['action']);artifacts=ctx['prior'].get('artifacts',{}) if result['status']=='SATISFIED' else {}
    if result['status']=='SATISFIED' and not artifacts:
        out=path.parent/'epoch-evidence.json';private_write(out,result['observations']);artifacts={str(out.relative_to(ctx['directory'])):sha(out)}
    binding={k:ctx[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
    private_write(ctx['response_file'],dict(binding=binding,status=result['status'],artifacts=artifacts,observations=result['observations']))


if __name__=='__main__':main()
