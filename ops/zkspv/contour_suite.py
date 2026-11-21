"""Dependency-aware multi-request contour with one delegated action and immutable evidence lineage."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil

from .contour import ROOT,Contour,digest,sha,relative_file,private_write,validate as validate_child
from .contour_matrix import CASES,assess
from signer_agent.operations import Journal,public_data
from signer_agent.onboarding import read_private


def order(jobs):
    by={j['id']:j for j in jobs};done=[]
    while len(done)<len(jobs):
        ready=[j['id'] for j in jobs if j['id'] not in done and set(j['depends_on'])<=set(done)]
        if not ready:raise ValueError('SUITE_DEPENDENCY_CYCLE')
        done.extend(ready)
    return done


def validate(p):
    public_data(p)
    if (not isinstance(p,dict) or set(p)!={'version','run_id','directory','mode','source_paths','input_sha256','jobs','cases'}
            or p['version']!='1' or p['mode'] not in ('MAINNET_CANDIDATE','TEST_ONLY')
            or not isinstance(p['run_id'],str) or len(p['run_id'])!=64 or any(c not in '0123456789abcdef' for c in p['run_id'])
            or not Path(p['directory']).is_absolute() or not isinstance(p['jobs'],list) or not 1<=len(p['jobs'])<=512):
        raise ValueError('SUITE_CONFIG')
    ids=set();directories=set();runs=set()
    for job in p['jobs']:
        if (set(job)!={'id','depends_on','spec','profile','plan'} or not isinstance(job['id'],str)
                or not 1<=len(job['id'])<=48 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in job['id'])
                or job['id'] in ids or not isinstance(job['depends_on'],list) or len(set(job['depends_on']))!=len(job['depends_on'])
                or not isinstance(job['profile'],dict) or not isinstance(job['spec'],dict)):raise ValueError('SUITE_JOB')
        ids.add(job['id'])
        if job['plan'] is None:
            if p['mode']!='MAINNET_CANDIDATE' or job['profile']!={'role':job['spec'].get('role'),'deferred':True}:raise ValueError('SUITE_UNBOUND_JOB')
            continue
        child=validate_child(job['plan']);directory=Path(p['directory'])/'jobs'/job['id']
        if (child['directory']!=str(directory) or child['directory'] in directories or child['run_id'] in runs):raise ValueError('SUITE_JOB_DIRECTORY')
        directories.add(child['directory']);runs.add(child['run_id'])
    if any(not set(j['depends_on'])<=ids or j['id'] in j['depends_on'] for j in p['jobs']):raise ValueError('SUITE_DEPENDENCY')
    order(p['jobs'])
    if (not isinstance(p['cases'],dict) or set(p['cases'])-CASES.keys()
            or any(not isinstance(v,list) or len(v)!=len(set(v)) or not set(v)<=ids for v in p['cases'].values())):raise ValueError('SUITE_MATRIX')
    if (not isinstance(p['source_paths'],list) or not p['source_paths'] or len(p['source_paths'])>8192
            or len(set(p['source_paths']))!=len(p['source_paths']) or not isinstance(p['input_sha256'],dict)
            or len(p['input_sha256'])>8192):raise ValueError('SUITE_INPUTS')
    for path,h in p['input_sha256'].items():
        if not Path(path).is_absolute() or not isinstance(h,str) or len(h)!=64 or any(c not in '0123456789abcdef' for c in h):raise ValueError('SUITE_INPUTS')
    return p


class Suite:
    def __init__(self,plan,*,root=ROOT):
        self.p=validate(plan);self.root=Path(root).resolve();self.directory=Path(plan['directory'])
        if plan['mode']=='MAINNET_CANDIDATE':
            from .contour_suite_plan import input_files,validate_spec,verify_job
            from .contour_plan import source_paths
            if not set(source_paths())<=set(plan['source_paths']):raise ValueError('SUITE_SOURCE_SCOPE')
            for job in plan['jobs']:
                role=validate_spec(job['spec'])
                selected=(dict(worker_config=job['spec']['worker_config']) if job['plan'] is None else
                    {k:v for k,v in job['spec'].items() if k!='request_file' or role not in ('fresh','incident','epoch','continuity','reconcile','fee_owner','withdrawal_cancel','scenario_check')})
                if any(plan['input_sha256'].get(p)!=h for p,h in input_files(selected).items()):raise ValueError('SUITE_INPUT_CHANGED')
                if job['plan'] is not None:verify_job(job,self.directory)
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.directory.is_symlink():raise ValueError('SUITE_PATH')
        self.journal=Journal(self.directory/'suite.sqlite');self.jobs={j['id']:j for j in plan['jobs']};self.ordered=order(plan['jobs'])
        paths=set(plan['source_paths'])|{p for j in plan['jobs'] if j['plan'] for p in j['plan']['source_paths']}
        if self.root==ROOT:paths.update(['ops/zkspv/contour.py','ops/zkspv/contour_suite.py','ops/zkspv/contour_matrix.py','ops/zkspv/contour_suite_plan.py'])
        self.sources={p:sha(relative_file(self.root,p)) for p in sorted(paths)}
        executables={c[0] for j in plan['jobs'] if j['plan'] for s in j['plan']['stages']
            for a in ('probe','run','recover') if (c:=s[a]) is not None}
        self.tools={p:sha(p) for p in sorted(executables)}
        if plan['mode']=='MAINNET_CANDIDATE':
            import sys
            self.tools[sys.executable]=sha(Path(sys.executable))
        self.binding=dict(plan=plan,source_sha256=self.sources,executable_sha256=self.tools);self.plan_hash=digest(self.binding)
        with self.exclusive():
            self.unchanged();self.journal.put('suite',self.binding)
            for job in plan['jobs']:self.journal.put('job:'+job['id'],dict(job=job,suite_sha256=self.plan_hash))
            self.load_bindings();self.unchanged()

    def load_bindings(self):
        for template in self.p['jobs']:
            if template['plan'] is not None:continue
            try:saved=self.journal.get('binding:'+template['id'])['request']
            except KeyError:continue
            job=saved['job']
            if (saved['suite_sha256']!=self.plan_hash or job['spec']!=template['spec']
                    or job['depends_on']!=template['depends_on']):raise ValueError('SUITE_BINDING_CHANGED')
            for name,h in saved['input_sha256'].items():
                read_private(name)
                if sha(Path(name))!=h:raise ValueError('SUITE_INPUT_CHANGED')
            from .contour_suite_plan import verify_job
            verify_job(job,self.directory)
            self.jobs[template['id']]=job

    def bind(self,key):
        with self.exclusive():
            self.load_bindings();self.unchanged()
            if key not in self.jobs:raise ValueError('SUITE_JOB')
            if self.jobs[key]['plan'] is not None:return dict(status='JOB_ALREADY_BOUND',job=key,production_ready=False)
            audits=self.audits()
            if any(audits[p]['status']!='VERIFIED' or self.journal.get('job:'+p)['status']!='COMPLETED' for p in self.jobs[key]['depends_on']):
                raise ValueError('SUITE_BIND_DEPENDENCY')
            from .contour_suite_plan import compile_job,input_files
            template=self.jobs[key];spec=template['spec'];profile,plan=compile_job(spec,key,self.directory/'jobs'/key)
            if not set(plan['source_paths'])<=self.sources.keys():raise ValueError('SUITE_SOURCE_SCOPE')
            if any(s[a][0] not in self.tools for s in plan['stages'] for a in ('probe','run','recover') if s[a] is not None):
                raise ValueError('SUITE_TOOL_CHANGED')
            ignored=('fresh','incident','epoch','continuity','reconcile','fee_owner','withdrawal_cancel','scenario_check')
            inputs=input_files({k:v for k,v in spec.items() if k!='request_file' or spec['role'] not in ignored})
            job=dict(template,profile=profile,plan=plan)
            self.journal.put('binding:'+key,dict(suite_sha256=self.plan_hash,job=job,input_sha256=inputs))
            self.jobs[key]=job
            return dict(status='JOB_BOUND_REVIEWABLE',job=key,profile=profile,plan_sha256=digest(plan),production_ready=False)

    @contextmanager
    def exclusive(self):
        fd=os.open(self.directory/'suite.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            os.fchmod(fd,0o600)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('SUITE_BUSY') from None
            self.lock_fd=fd;yield
        finally:self.lock_fd=None;os.close(fd)

    def unchanged(self):
        for name,h in self.sources.items():
            if sha(relative_file(self.root,name))!=h:raise ValueError('SUITE_SOURCE_CHANGED')
        for name,h in self.tools.items():
            if sha(name)!=h:raise ValueError('SUITE_TOOL_CHANGED')
        for name,h in self.p['input_sha256'].items():
            read_private(name)
            if sha(Path(name))!=h:raise ValueError('SUITE_INPUT_CHANGED')
        for key,job in self.jobs.items():
            if job['plan'] is None:continue
            try:inputs=self.journal.get('binding:'+key)['request']['input_sha256']
            except KeyError:continue
            for name,h in inputs.items():
                read_private(name)
                if sha(Path(name))!=h:raise ValueError('SUITE_INPUT_CHANGED')

    def child(self,key):
        job=self.jobs[key];upstream=[]
        for parent in job['depends_on']:
            entry=self.journal.get('job:'+parent)
            if entry['status']!='COMPLETED':raise ValueError('SUITE_DEPENDENCY')
            upstream.append(dict(stage='job:'+parent,result_sha256=entry['result']['commitment']))
        if self.p['mode']!='TEST_ONLY':
            from .contour_suite_plan import verify_job
            verify_job(job,self.directory)
        return Contour(job['plan'],root=self.root,upstream=upstream,guard_fds=(self.lock_fd,))

    def audits(self,*,refresh=True):
        result={}
        for key in self.ordered:
            job=self.jobs[key]
            if job['plan'] is None:
                result[key]=dict(status='INPUT_REQUIRED');continue
            if any(result[p]['status']!='VERIFIED' for p in job['depends_on']):
                result[key]=dict(status='DEPENDENCY_BLOCKED');continue
            if any(self.journal.get('job:'+p)['status']!='COMPLETED' for p in job['depends_on']):
                result[key]=dict(status='DEPENDENCY_PENDING');continue
            try:child=self.child(key);audit=child.audit(refresh=refresh)
            except Exception:
                result[key]=dict(status='RECOVERY_REQUIRED',reason='JOB_EVIDENCE_CHECK_FAILED');continue
            result[key]=audit
            saved=self.journal.get('job:'+key)
            if (saved['status']=='COMPLETED' and audit['status']!='WAITING'
                    and (audit['status']!='VERIFIED' or audit.get('commitment')!=saved['result']['commitment'])):
                result[key]=dict(status='RECOVERY_REQUIRED',reason='COMPLETED_JOB_CHANGED')
        return result

    def step(self):
        with self.exclusive():
            self.load_bindings();self.unchanged();audits=self.audits()
            # A completed dependency losing evidence blocks its descendants.
            # Unrelated incident/service jobs can still make progress.
            for key,a in audits.items():
                if a['status']=='VERIFIED' and self.journal.get('job:'+key)['status']!='COMPLETED':
                    self.journal.update('job:'+key,'COMPLETED',dict(commitment=a['commitment'],plan_sha256=a['plan_sha256']))
            cursor=self.journal.get('suite')['result'].get('cursor',0)
            candidates=self.ordered[cursor:]+self.ordered[:cursor]
            for key in candidates:
                if audits[key]['status'] in ('VERIFIED','INPUT_REQUIRED','DEPENDENCY_BLOCKED',
                                              'DEPENDENCY_PENDING','WAITING','RECOVERY_REQUIRED'):continue
                child=self.child(key);self.journal.update('suite','RUNNING',dict(cursor=(self.ordered.index(key)+1)%len(self.ordered)))
                # Child STARTED is committed before spawning; its own original
                # run/recover engine is the sole authority for a new envelope.
                result=child.step();audit=child.audit()
                if audit['status']=='VERIFIED':self.journal.update('job:'+key,'COMPLETED',dict(commitment=audit['commitment'],plan_sha256=audit['plan_sha256']))
                return dict(status=result['status'],job=key,delegated_jobs=1,combined_e2e_accepted=False,production_ready=False)
            return self.summary(audits)

    def summary(self,audits):
        matrix=assess(dict(self.p,jobs=list(self.jobs.values())),audits)
        return dict(status='EXECUTION_COMPLETE_UNQUALIFIED' if all(a['status']=='VERIFIED' for a in audits.values()) else 'PENDING',
            suite_sha256=self.plan_hash,jobs={k:a['status'] for k,a in audits.items()},matrix=matrix,
            combined_e2e_accepted=False,fresh_proving_verified=False,ready=False,production_ready=False)

    def collect(self,*,bundle=None):
        with self.exclusive():
            self.load_bindings();self.unchanged();audits=self.audits();report=self.summary(audits)
            report.update(schema='satoshifi.combined-contour-evidence.v1',mode=self.p['mode'],run_id=self.p['run_id'],
                suite_binding=self.binding,
                source_sha256=self.sources,executable_sha256=self.tools,input_sha256=self.p['input_sha256'],
                job_plans={k:j for k,j in self.jobs.items()},evidence=audits,
                typed_scenario_bindings=self.p['mode']=='MAINNET_CANDIDATE',
                independent_qualification=False,global_onchain_continuity_qualified=False)
            files={}
            for key,a in audits.items():
                child=self.jobs[key]['plan']
                if child is None:continue
                base=Path(child['directory'])
                for stage in a.get('stages',[]):
                    names={**stage['artifacts'],stage['response_file']:stage['response_sha256'],
                        stage['probe_response_file']:stage['probe_response_sha256'],**stage['execution_files']}
                    for name,h in names.items():
                        source=relative_file(base,name)
                        if sha(source)!=h:raise ValueError('SUITE_ARTIFACT_CHANGED')
                        files[str(source.relative_to(self.directory))]=h
                # Include unsuccessful invocations as well as the accepted
                # responses. Contexts bind each process to the same attempt;
                # absence of an exit record explicitly records interruption.
                for invocation in sorted(base.glob('invoke-*')):
                    if invocation.is_symlink() or not invocation.is_dir():raise ValueError('SUITE_ARTIFACT_CHANGED')
                    for name in ('context.json','process.json','driver.log','response.json'):
                        if (invocation/name).exists():
                            source=relative_file(base,str((invocation/name).relative_to(base)))
                            files[str(source.relative_to(self.directory))]=sha(source)
            report['artifact_sha256']=files
            report['invocations']=self.invocations(files)
            report['bound_input_sha256']={}
            for key in self.jobs:
                try:report['bound_input_sha256'][key]=self.journal.get('binding:'+key)['request']['input_sha256']
                except KeyError:pass
            # Qualifying actual deployment/proof/monetary acceptance is never
            # inferred from labels, completion markers or process exit alone.
            if bundle is not None:
                out=Path(bundle)
                if out.exists() or not out.is_absolute():raise ValueError('SUITE_BUNDLE_EXISTS')
                out.mkdir(mode=0o700,parents=True)
                for name,h in files.items():
                    source=relative_file(self.directory,name);target=out/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    with source.open('rb') as src,target.open('xb') as dst:
                        os.chmod(target,0o600);shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
                    if sha(target)!=h:raise ValueError('SUITE_ARTIFACT_CHANGED')
                private_write(out/'manifest.json',report)
            private_write(self.directory/('collection-'+secrets.token_hex(16)+'.json'),report)
            return report

    def invocations(self,files):
        rows=[]
        for name,h in files.items():
            if not name.endswith('/context.json'):continue
            path=relative_file(self.directory,name);context=json.loads(path.read_text());process=path.parent/'process.json'
            exit_record=json.loads(process.read_text()) if process.exists() else None
            binding={k:context[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
            if exit_record is not None and exit_record['binding']!=binding:raise ValueError('SUITE_PROCESS_BINDING')
            if exit_record is not None:
                journal=Journal(path.parent.parent/'contour.sqlite')
                anchored=journal.get('invocation:'+binding['challenge'])['request']
                if anchored['process_sha256']!=sha(process):raise ValueError('SUITE_PROCESS_CHANGED')
                for file,expected in (('context.json',exit_record['context_sha256']),('driver.log',exit_record['driver_log_sha256']),('response.json',exit_record['response_sha256'])):
                    if expected is not None and sha(path.parent/file)!=expected:raise ValueError('SUITE_PROCESS_CHANGED')
            rows.append(dict(context_file=name,context_sha256=h,binding=binding,
                process_file=str(process.relative_to(self.directory)) if process.exists() else None,
                process_sha256=sha(process) if process.exists() else None,
                exit_code=exit_record['exit_code'] if exit_record else None,
                interrupted=exit_record is None or exit_record['exit_code']!=0))
        return rows


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('step','inspect','collect','bind'))
    p.add_argument('--config',required=True);p.add_argument('--bundle-directory');p.add_argument('--job');a=p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs()
    if Path(a.config).stat().st_size>8*1024*1024:raise ValueError('SUITE_CONFIG')
    suite=Suite(json.loads(read_private(a.config,max_bytes=32*1024**2)))
    if a.action=='bind':result=suite.bind(a.job)
    elif a.action=='step':result=suite.step()
    else:
        result=suite.collect(bundle=a.bundle_directory) if a.action=='collect' else None
        if result is None:
            with suite.exclusive():suite.unchanged();result=suite.summary(suite.audits())
        result={k:result[k] for k in ('status','suite_sha256','matrix','combined_e2e_accepted','production_ready')}
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':main()
