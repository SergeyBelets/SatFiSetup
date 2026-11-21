"""Offline typed job compiler, complete scenario recipes and immutable private input pins."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import secrets
import sys

from .contour import ROOT,CAPABILITIES,digest,sha,private_write
from .contour_plan import build as child_plan,source_paths
from .contour_matrix import CASES
from signer_agent.onboarding import read_private
from signer_agent.zk_worker import load_config as worker_config

EXTRA={'bootstrap':{'bootstrap_config'},'fresh':{'require_proof'},'funding':{'funding_config'},
    'scenario_check':{'scenario_config'},
    'withdrawal_cancel':{'owner_operation_config'},
    'address_request':{'address_operation_config'},'address_registration':{'address_operation_config'},'address_ready':{'address_operation_config'},
    'proof':{'service_config','proof_policy'},'mint':{'mint_config','mint_profile'},
    'withdrawal_prepare':{'withdrawal_config'},'withdrawal_settle':{'withdrawal_config'},
    'refund_prepare':{'withdrawal_config'},'refund_settle':{'withdrawal_config'},
    'reconcile':{'reconciliation_config'},'incident':{'incident_config'},'epoch':{'epoch_config'},
    'continuity':{'continuity_config'},'fee_ingress':{'fee_operation_config'},'fee_funding':{'fee_operation_config'},'fee_owner':{'fee_operation_config'}}
FILE_KEYS={'worker_config','request_file','bootstrap_config','funding_config','mint_config','withdrawal_config',
    'reconciliation_config','incident_config','epoch_config','continuity_config','fee_operation_config','service_config',
    'fleet_config','challenge_file','quote_file','payment_file','report_file','decision_file','bundle_file','auth_bundle_file',
    'owner_config','operator_config','fee_config','deposit_config','address_config','fee_reserve_config','requester_config',
    'config_file','identity_file','issuer_identity_file','reporter_config','recovery_service_config','key_files','attestation_files','previous_config','address_operation_config','intent_file','owner_operation_config','scenario_config'}


def validate_spec(spec):
    role=spec.get('role');base={'role','worker_config','request_file'}
    if role not in EXTRA or set(spec)!=base|EXTRA[role]:raise ValueError('SUITE_JOB_SPEC')
    for key,value in spec.items():
        if key in FILE_KEYS and value is not None and (not isinstance(value,str) or not Path(value).is_absolute()):raise ValueError('SUITE_JOB_SPEC')
    if spec['worker_config'] is None or spec['request_file'] is None:raise ValueError('SUITE_JOB_SPEC')
    if role=='fresh' and type(spec['require_proof']) is not bool:raise ValueError('SUITE_JOB_SPEC')
    if role=='proof' and spec['proof_policy'] not in ('FRESH','EXISTING'):raise ValueError('SUITE_JOB_SPEC')
    if role=='mint' and spec['mint_profile'] not in ('SMP1_V1','SMP1_V2','ADDRESS'):raise ValueError('SUITE_JOB_SPEC')
    if any(spec[k] is None for k in EXTRA[role]&FILE_KEYS-{'service_config'}):raise ValueError('SUITE_JOB_SPEC')
    return role


def input_files(spec):
    result={};visited=set()
    def walk(value,key='',depth=0):
        if depth>24:raise ValueError('SUITE_INPUT_DEPTH')
        if isinstance(value,dict):
            for k,v in value.items():walk(v,k,depth+1)
        elif isinstance(value,list):
            for v in value:walk(v,key,depth+1)
        elif key in FILE_KEYS and isinstance(value,str) and Path(value).is_absolute():
            if value in visited:return
            visited.add(value)
            if len(visited)>8192:raise ValueError('SUITE_INPUT_LIMIT')
            if Path(value).stat().st_size>16*1024*1024:raise ValueError('SUITE_INPUT_LIMIT')
            raw=read_private(value,max_bytes=16*1024**2);result[value]=sha(Path(value))
            try:body=json.loads(raw)
            except (ValueError,UnicodeError):return
            walk(body,'',depth+1)
    walk(spec);return result


def compile_job(spec,identifier,directory,*,executable=sys.executable):
    role=validate_spec(spec)
    wc=worker_config(spec['worker_config'])
    need=json.loads(read_private(spec['request_file'])) if role not in ('fresh','incident','epoch','continuity','reconcile','fee_owner','withdrawal_cancel','scenario_check') else None
    if need is not None and need.get('consumer') not in wc['allowed_consumers']:raise ValueError('SUITE_CONSUMER_SCOPE')
    scope={k:wc[k] for k in ('chain_id','zk_state','evm_confirmations')};scope['bitcoin_policy_sha256']=digest(wc['bitcoin_policy'])
    profile=dict(role=role,scope=scope,need=None)
    args=dict(worker_only=True,executable=executable);caps=[];command=None
    common=['--worker-config',spec['worker_config'],'--worker-config-sha256',digest(wc)]
    if role=='fresh':
        if type(spec['require_proof']) is not bool or not wc.get('heartbeat',{}).get('enabled'):raise ValueError('SUITE_HEARTBEAT_POLICY')
        cap='fresh_proof' if spec['require_proof'] else 'state_update';caps=[cap]
        command=[executable,'-m','ops.zkspv.contour_heartbeat',*common,*(['--require-proof'] if spec['require_proof'] else [])]
        profile.update(require_proof=spec['require_proof'])
    elif role=='bootstrap':args['bootstrap_config']=spec['bootstrap_config'];caps=['bootstrap']
    elif role=='scenario_check':
        from .contour_checks import load_config,inputs
        c=load_config(spec['scenario_config']);data=inputs(c,wc);caps=[role]
        command=[executable,'-m','ops.zkspv.contour_checks',*common,'--scenario-config',spec['scenario_config'],
            '--scenario-config-sha256',digest(c),'--inputs-sha256',digest(data)]
        profile.update(scenario_id=c['scenario_id'],row_sha256=c['row_sha256'],observation_only=True)
    elif role=='withdrawal_cancel':
        from .contour_owner import load_config,inputs
        c=load_config(spec['owner_operation_config']);data=inputs(c,wc);caps=[role]
        command=[executable,'-m','ops.zkspv.contour_owner',*common,'--owner-operation-config',spec['owner_operation_config'],
            '--owner-operation-config-sha256',digest(c),'--inputs-sha256',digest(data)]
        profile.update(request_id=c['request_id'],reserve=data[0]['reserve'],fee_mode=data[1]['options']['fee_mode'])
    elif role in ('address_request','address_registration','address_ready'):
        from .contour_address import load_config,inputs
        c=load_config(spec['address_operation_config']);data=inputs(c,wc,need);caps=[role]
        command=[executable,'-m','ops.zkspv.contour_address',*common,'--address-operation-config',spec['address_operation_config'],
            '--address-operation-config-sha256',digest(c),'--inputs-sha256',digest(data)]
        profile.update(need=need,intent_id=need['intent_id'],recipient_mode=c['recipient_mode'],address_version_hash=digest([c,data]),
            expected_status=c['expected_status'],registry=data[0][0]['registry'])
    elif role=='funding':args['funding_config']=spec['funding_config'];caps=['bitcoin_funding'];profile['need']=need
    elif role=='proof':
        if spec['proof_policy'] not in ('FRESH','EXISTING'):raise ValueError('SUITE_PROOF_POLICY')
        if spec['proof_policy']=='EXISTING':
            if spec['service_config'] is not None:raise ValueError('SUITE_EXISTING_PROOF_SERVICE')
            args['materialize']=True;caps=['membership'];profile['funding_mode']='NONE'
        else:
            args['materialize']=True
            args['service_config']=spec['service_config'];caps=['consumer_request','encrypted_transport','admission_payment','proof_preparation','fresh_proof','state_update','service_status']
            caps.append('membership')
            from .contour_service import load_config
            profile['funding_mode']=load_config(spec['service_config'])['funding_mode'] if spec['service_config'] else 'NONE'
        profile.update(need=need,proof_policy=spec['proof_policy'])
    elif role=='mint':
        from .contour_mint import load_config
        mc=load_config(spec['mint_config']);wanted=spec['mint_profile']
        if wanted not in ('SMP1_V1','SMP1_V2','ADDRESS') or (wanted=='ADDRESS')!=(mc['mode']=='ADDRESS'):raise ValueError('SUITE_MINT_PROFILE')
        args.update(mint_config=spec['mint_config'],consumer_only=True);caps=['membership','address_mint' if wanted=='ADDRESS' else 'smp1_mint']
        profile.update(need=need,mint_profile=wanted)
    elif role in ('withdrawal_prepare','withdrawal_settle','refund_prepare','refund_settle'):
        from .contour_withdrawal import load_config,inputs
        c=load_config(spec['withdrawal_config']);data=inputs(c,wc,need);quote=data[4]
        if role.startswith('refund')!=(quote.get('kind')=='BTC_CREDIT_REFUND'):raise ValueError('SUITE_WITHDRAWAL_PROFILE')
        if role.startswith('refund') and c['mode']!='BTC':raise ValueError('SUITE_WITHDRAWAL_PROFILE')
        if not role.startswith('refund') and quote['options']['calculation'] not in ('payout','total'):raise ValueError('SUITE_WITHDRAWAL_PROFILE')
        args['withdrawal_config']=spec['withdrawal_config'];caps=['withdrawal_prepare'] if role.endswith('prepare') else ['membership','withdrawal_'+c['mode'].lower()]
        profile.update(need=need,fee_mode=c['mode'],calculation=quote['options']['calculation'],version_hash=digest([c,data[5]]),request_id=c['request_id'],reserve=data[0]['reserve'],
            batch=data[5]['plan']['batch_id'],previous=data[5]['plan']['withdrawal_fee']['previous'],replacement=c.get('previous_config') is not None,
            multi_owner=c['version']=='3',request_ids=data[5]['plan']['withdrawal_refs'],fee_modes=sorted({quote['options']['fee_mode'],*[r['quote']['options']['fee_mode'] for r in data[7]]}) if len(data)>7 else [c['mode']])
    elif role=='reconcile':
        from .contour_reconcile import load_config,inputs
        c=load_config(spec['reconciliation_config']);data=inputs(c,wc);caps=['reconciliation'];profile['reserve']=data[0]['reserve']
        command=[executable,'-m','ops.zkspv.contour_reconcile',*common,'--reconciliation-config',spec['reconciliation_config'],
            '--reconciliation-config-sha256',digest(c),'--inputs-sha256',digest(data)]
    elif role=='incident':
        from .contour_incident import load_config,inputs
        c=load_config(spec['incident_config']);data=inputs(c,wc);change=data[4]['change'];scope_id=int(change[0]);target=int(change[4])
        profile['transition']='RESTRICT' if target in ((2,3) if scope_id==0 else (1,2)) else 'RESUME'
        profile.update(registry=data[1]['registry'],incident_scope=scope_id,expected_generation=change[2])
        caps=['incident_recovery'];command=[executable,'-m','ops.zkspv.contour_incident',*common,'--incident-config',spec['incident_config'],
            '--incident-config-sha256',digest(c),'--inputs-sha256',digest(data)]
    elif role=='epoch':
        from .contour_epoch import load_config,inputs
        c=load_config(spec['epoch_config']);data=inputs(c,wc);caps=['old_epoch_service']
        command=[executable,'-m','ops.zkspv.contour_epoch',*common,'--epoch-config',spec['epoch_config'],
            '--epoch-config-sha256',digest(c),'--inputs-sha256',digest(data)]
    elif role=='continuity':
        c=json.loads(read_private(spec['continuity_config']))
        if set(c)!={'version','fleet_config','plan_id'} or c['version']!='1':raise ValueError('SUITE_CONTINUITY_CONFIG')
        from signer_agent.epoch_service import load_config
        from signer_agent.spv_transport import hex32
        load_config(c['fleet_config']);hex32(c['plan_id']);caps=['continuity']
        command=[executable,'-m','ops.zkspv.contour_driver','--fleet-config',c['fleet_config'],'--plan-id',c['plan_id']]
    else:
        from .contour_fee import load_config,inputs,fee_mode
        c=load_config(spec['fee_operation_config']);data=inputs(c,wc)
        if role in ('fee_ingress','fee_funding') and (c['mode']!='FUNDING' or data[1]!=need):raise ValueError('SUITE_FEE_PROFILE')
        if role=='fee_owner' and c['mode']=='FUNDING':raise ValueError('SUITE_FEE_PROFILE')
        cap='bitcoin_funding' if c['mode']=='FUNDING' else 'withdrawal_'+fee_mode(c['mode']).lower()
        caps=[cap];command=[executable,'-m','ops.zkspv.contour_fee',*common,'--fee-operation-config',spec['fee_operation_config'],
            '--fee-operation-config-sha256',digest(c),'--inputs-sha256',digest(data),*(['--prepare-only'] if role=='fee_ingress' else [])]
        profile.update(need=need if c['mode']=='FUNDING' else None,method=data[1].get('method'),
            fee_mode=fee_mode(c['mode']),request_id=data[1].get('request_id'))
    plan=(dict(version='1',run_id='00'*32,directory=str(directory),source_paths=source_paths(),stages=[]) if command else
        child_plan(spec['worker_config'],spec['request_file'],directory,**args))
    if command:
        timeout=int(wc['stage_timeout_seconds'])+600 if role=='fresh' else 600
        plan['stages']=[dict(id=c,capability=c,timeout_seconds=timeout,probe=command,run=command,recover=command) for c in caps]
    else:plan['stages']=[s for s in plan['stages'] if s['capability'] in caps]
    if role=='mint':
        for stage in plan['stages']:
            if stage['capability'] in ('smp1_mint','address_mint'):
                for a in ('probe','run','recover'):stage[a]=[*stage[a],'--expected-proof-version','1' if spec['mint_profile']=='SMP1_V1' else '2']
    if role=='proof' and spec['proof_policy']=='FRESH':
        for stage in plan['stages']:
            if stage['capability'] in ('fresh_proof','state_update'):
                for a in ('probe','run','recover'):
                    command=stage[a];stage[a]=[command[0],'-m','ops.zkspv.contour_worker_cycle',*command[3:]]
    if role.startswith('refund_'):
        for stage in plan['stages']:
            if stage['capability']!='membership':
                for a in ('probe','run','recover'):
                    cmd=stage[a];stage[a]=[cmd[0],'-m','ops.zkspv.contour_refund','run',*cmd[3:]]
    plan['run_id']=digest(['satoshifi.suite-child.v1',str(directory),identifier,spec])
    return profile,plan


def verify_job(job,directory):
    profile,plan=compile_job(job['spec'],job['id'],Path(directory)/'jobs'/job['id'],executable=job['plan']['stages'][0]['probe'][0])
    if job['profile']!=profile or job['plan']!=plan:raise ValueError('SUITE_TYPED_JOB_CHANGED')


def build(c,*,executable=sys.executable):
    if (set(c)!={'version','run_id','directory','jobs','cases'} or c['version']!='1'
            or not Path(c['directory']).is_absolute() or not isinstance(c['jobs'],list)):raise ValueError('SUITE_BUILD_CONFIG')
    jobs=deepcopy(c['jobs']);inputs={};sources=set()
    # Insert an explicit reusable Fresh gate before operations that need an
    # already authenticated usable graph. Existing user consent stays frozen.
    original={j['id']:j for j in jobs}
    for job in list(jobs):
        if set(job) not in ({'id','depends_on','spec'},{'id','depends_on','spec','defer'}):raise ValueError('SUITE_JOB_SPEC')
        if 'defer' in job and type(job['defer']) is not bool:raise ValueError('SUITE_JOB_SPEC')
        if job['spec'].get('role') not in ('funding','mint','withdrawal_prepare','refund_prepare'):continue
        fresh_id='fresh_'+job['id']
        if fresh_id in original:raise ValueError('SUITE_AUTO_FRESH_ID')
        fresh_spec={k:job['spec'][k] for k in ('worker_config','request_file')};fresh_spec.update(role='fresh',require_proof=False)
        jobs.append(dict(id=fresh_id,depends_on=list(job['depends_on']),spec=fresh_spec,defer=True));job['depends_on']=[fresh_id]
    compiled=[]
    for job in jobs:
        spec=job['spec'];role=validate_spec(spec)
        if job.get('defer',False):
            wc=worker_config(spec['worker_config']);profile=dict(role=role,deferred=True);plan=None
            if role=='fresh' and not wc.get('heartbeat',{}).get('enabled'):raise ValueError('SUITE_HEARTBEAT_POLICY')
            inputs.update(input_files(dict(worker_config=spec['worker_config'])));sources.update(source_paths())
        else:
            profile,plan=compile_job(spec,job['id'],Path(c['directory'])/'jobs'/job['id'],executable=executable)
            selected={k:v for k,v in spec.items() if k!='request_file' or role not in ('fresh','incident','epoch','continuity','reconcile','fee_owner','withdrawal_cancel','scenario_check')}
            inputs.update(input_files(selected));sources.update(plan['source_paths'])
        compiled.append(dict(id=job['id'],depends_on=job['depends_on'],spec=spec,profile=profile,plan=plan))
    from .contour_suite import validate
    return validate(dict(version='1',run_id=c['run_id'],directory=c['directory'],mode='MAINNET_CANDIDATE',
        source_paths=sorted(sources),input_sha256=inputs,jobs=compiled,cases=c['cases']))


def template(directory='/srv/satoshifi/private/combined-suite'):
    jobs=[];bindings={};base=Path('/srv/satoshifi/private/scenarios')
    def add(key,role,depends=(),**extra):
        candidates=[case for case in CASES if key==case or key.startswith(case+'_')]
        home=base/('fee' if key.startswith('fee_') else max(candidates,key=len) if candidates else key)
        spec=dict(role=role,worker_config=str(home/'worker.json'),request_file=str(home/'need.json'),**extra)
        jobs.append(dict(id=key,depends_on=list(depends),spec=spec,defer=True));return key
    boot=add('bootstrap','bootstrap',bootstrap_config=str(base/'bootstrap'/'bootstrap.json'));bindings['bootstrap']=[boot]
    fresh=add('initial_fresh','fresh',[boot],require_proof=False);bindings['initial_fresh']=[fresh]
    proof=add('fresh_groth16','fresh',[fresh],require_proof=True);bindings['fresh_groth16']=[proof]
    for funding in ('paid','confederation'):
        key='service_'+funding;bindings[key]=[add(key,'proof',[proof],service_config=str(base/key/'service.json'),proof_policy='FRESH')]
    for case,profile in [('smp1_v1','SMP1_V1'),('smp1_v2','SMP1_V2'),('address_mint','ADDRESS')]:
        f=add(case+'_fund','funding',[fresh],funding_config=str(base/case/'funding.json'))
        p=add(case+'_proof','proof',[f],service_config=str(base/case/'service.json'),proof_policy='FRESH')
        m=add(case+'_mint','mint',[p],mint_config=str(base/case/'mint.json'),mint_profile=profile);bindings[case]=[f,p,m]
    for mode in ('btc','mp','s'):
        for calc in ('payout','total'):
            case='withdrawal_'+mode+'_'+calc;cfg=str(base/case/'withdrawal.json')
            prerequisites=['address_mint_mint'] if case=='withdrawal_btc_payout' else [fresh]
            a=add(case+'_prepare','withdrawal_prepare',prerequisites,withdrawal_config=cfg)
            b=add(case+'_proof','proof',[a],service_config=str(base/case/'service.json'),proof_policy='FRESH')
            s=add(case+'_settle','withdrawal_settle',[b],withdrawal_config=cfg)
            r=add(case+'_reconcile','reconcile',[s],reconciliation_config=str(base/case/'reconciliation.json'));bindings[case]=[a,b,s,r]
        for winner in ('old','new'):
            case='withdrawal_'+mode+'_rbf_'+winner+'_winner';home=base/case
            a=add(case+'_initial','withdrawal_prepare',[fresh],withdrawal_config=str(home/'initial.json'))
            b=add(case+'_replacement','withdrawal_prepare',[a],withdrawal_config=str(home/'replacement.json'))
            # Preparation observes two different txids. Proof and settlement
            # bind ONLY the selected actual winner; the losing version remains.
            for job,label in zip(jobs[-2:],('old','new')):job['spec']['request_file']=str(home/(label+'-need.json'))
            p=add(case+'_proof','proof',[b],service_config=str(home/'service.json'),proof_policy='FRESH')
            s=add(case+'_settle','withdrawal_settle',[p],withdrawal_config=str(home/('replacement.json' if winner=='new' else 'initial.json')))
            r=add(case+'_reconcile','reconcile',[s],reconciliation_config=str(home/'reconciliation.json'))
            for job in jobs:
                if job['id'] in (p,s):job['spec']['request_file']=str(home/(winner+'-need.json'))
            bindings[case]=[a,b,p,s,r]
        case='withdrawal_cancel_'+mode;bindings[case]=[add(case,'withdrawal_cancel',[fresh],owner_operation_config=str(base/case/'owner.json'))]
        case='fee_consent_'+mode;bindings[case]=[add(case,'fee_owner',[fresh],fee_operation_config=str(base/case/'owner.json'))]
    for mode in ('ordinary','wallet-v1','advanced-contract'):
        case='address_'+mode;home=base/case;selected=[];parent=fresh
        for role in ('address_request','address_registration','address_ready'):
            parent=add(case.replace('-','_')+'_'+role,role,[parent],address_operation_config=str(home/'address.json'));selected.append(parent)
            jobs[-1]['spec']['request_file']=str(home/'need.json')
            jobs[-1]['spec']['worker_config']=str(home/'worker.json')
        f=add(case.replace('-','_')+'_fund','funding',[parent],funding_config=str(home/'funding.json'))
        p=add(case.replace('-','_')+'_proof','proof',[f],service_config=str(home/'service.json'),proof_policy='FRESH')
        m=add(case.replace('-','_')+'_mint','mint',[p],mint_config=str(home/'mint.json'),mint_profile='ADDRESS')
        for job in jobs:
            if job['id'] in (f,p,m):job['spec'].update(request_file=str(home/'need.json'),worker_config=str(home/'worker.json'))
        bindings[case]=[*selected,f,p,m]
    case='withdrawal_batch_mixed';home=base/case
    a=add(case+'_prepare','withdrawal_prepare',[fresh],withdrawal_config=str(home/'withdrawal.json'))
    p=add(case+'_proof','proof',[a],service_config=str(home/'service.json'),proof_policy='FRESH')
    s=add(case+'_settle','withdrawal_settle',[p],withdrawal_config=str(home/'withdrawal.json'))
    r=add(case+'_reconcile','reconcile',[s],reconciliation_config=str(home/'reconciliation.json'));bindings[case]=[a,p,s,r]
    ingress=add('fee_ingress','fee_ingress',[fresh],fee_operation_config=str(base/'fee'/'funding.json'))
    p=add('fee_proof','proof',[ingress],service_config=str(base/'fee'/'service.json'),proof_policy='FRESH')
    f=add('fee_funding','fee_funding',[p],fee_operation_config=str(base/'fee'/'funding.json'));bindings['fee_funding']=[ingress,p,f]
    for mode in ('mp','s'):
        key='refund_'+mode;bindings[key]=[add(key,'fee_owner',[fresh],fee_operation_config=str(base/key/'owner.json'))]
    owner=add('refund_btc_owner','fee_owner',[f],fee_operation_config=str(base/'refund_btc'/'owner.json'))
    a=add('refund_btc_prepare','refund_prepare',[owner],withdrawal_config=str(base/'refund_btc'/'withdrawal.json'))
    p=add('refund_btc_proof','proof',[a],service_config=None,proof_policy='FRESH')
    s=add('refund_btc_settle','refund_settle',[p],withdrawal_config=str(base/'refund_btc'/'withdrawal.json'));bindings['refund_btc']=[owner,a,p,s]
    for method in ('cap_raise','cancel'):
        case='refund_btc_'+method;bindings[case]=[add(case,'fee_owner',[fresh],fee_operation_config=str(base/case/'owner.json'))]
    for winner in ('old','new'):
        case='refund_rbf_'+winner+'_winner';home=base/case
        a=add(case+'_initial','refund_prepare',[owner],withdrawal_config=str(home/'initial.json'))
        b=add(case+'_replacement','refund_prepare',[a],withdrawal_config=str(home/'replacement.json'))
        p=add(case+'_proof','proof',[b],service_config=str(home/'service.json'),proof_policy='FRESH')
        s=add(case+'_settle','refund_settle',[p],withdrawal_config=str(home/('replacement.json' if winner=='new' else 'initial.json')))
        r=add(case+'_reconcile','reconcile',[s],reconciliation_config=str(home/'reconciliation.json'))
        for job in jobs:
            if job['id'] in (a,b,p,s):job['spec']['request_file']=str(home/(('old' if job['id']==a else 'new' if job['id']==b else winner)+'-need.json'))
        bindings[case]=[a,b,p,s,r]
    suspend=add('incident_suspend','incident',incident_config=str(base/'incident_suspend'/'incident.json'));bindings['incident_suspend']=[suspend]
    resume=add('incident_resume','incident',[suspend],incident_config=str(base/'incident_resume'/'incident.json'));bindings['incident_resume']=[suspend,resume]
    bindings['reconciliation']=[add('reconciliation','reconcile',reconciliation_config=str(base/'reconciliation'/'config.json'))]
    bindings['old_epoch']=[add('old_epoch','epoch',epoch_config=str(base/'old_epoch'/'epoch.json'))]
    bindings['continuity']=[add('continuity','continuity',continuity_config=str(base/'continuity'/'handover.json'))]
    from .contour_checks import catalog
    for identifier in catalog():
        key='spec_'+identifier.lower().replace('.','_').replace('/','_').replace('-','_')
        bindings['spec/'+identifier]=[add(key,'scenario_check',[fresh],scenario_config=str(base/key/'checks.json'))]
    return dict(version='1',run_id=secrets.token_hex(32),directory=directory,jobs=jobs,cases=bindings)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('template','build'));p.add_argument('--config');p.add_argument('--output',required=True)
    a=p.parse_args(argv)
    value=template() if a.action=='template' else build(json.loads(read_private(a.config,max_bytes=4*1024**2)))
    private_write(Path(a.output),value);print(json.dumps(dict(status='SUITE_TEMPLATE_CREATED' if a.action=='template' else 'SUITE_PLAN_COMPILED',production_ready=False)))


if __name__=='__main__':main()
