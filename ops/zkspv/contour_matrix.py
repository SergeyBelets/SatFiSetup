"""Executable coverage rules for the combined contour; labels alone grant no coverage."""
from .contour import digest


CASES={
    'bootstrap': [('bootstrap',{})],
    'initial_fresh': [('fresh',{'require_proof':False})],
    'fresh_groth16': [('fresh',{'require_proof':True})],
    'service_paid': [('proof',{'funding_mode':'PAID'})],
    'service_confederation': [('proof',{'funding_mode':'CONFEDERATION'})],
    'smp1_v1': [('funding',{}),('proof',{}),('mint',{'mint_profile':'SMP1_V1'})],
    'smp1_v2': [('funding',{}),('proof',{}),('mint',{'mint_profile':'SMP1_V2'})],
    'address_mint': [('funding',{}),('proof',{}),('mint',{'mint_profile':'ADDRESS'})],
    'fee_funding': [('fee_ingress',{}),('proof',{}),('fee_funding',{})],
    'refund_mp': [('fee_owner',{'method':'refundTokens','fee_mode':'MP'})],
    'refund_s': [('fee_owner',{'method':'refundTokens','fee_mode':'S'})],
    'refund_btc': [('fee_owner',{'method':'createRefund','fee_mode':'BTC'}),('refund_prepare',{}),('proof',{}),('refund_settle',{})],
    'incident_suspend': [('incident',{'transition':'RESTRICT'})],
    'incident_resume': [('incident',{'transition':'RESTRICT'}),('incident',{'transition':'RESUME'})],
    'reconciliation': [('reconcile',{})],
    'old_epoch': [('epoch',{})],
    'continuity': [('continuity',{})],
}
for mode in ('BTC','MP','S'):
    for calculation in ('payout','total'):
        CASES['withdrawal_'+mode.lower()+'_'+calculation]=[
            ('withdrawal_prepare',dict(fee_mode=mode,calculation=calculation)),('proof',{}),
            ('withdrawal_settle',dict(fee_mode=mode,calculation=calculation)),('reconcile',{})]
    for winner in ('old','new'):
        CASES['withdrawal_'+mode.lower()+'_rbf_'+winner+'_winner']=[
            ('withdrawal_prepare',dict(fee_mode=mode,replacement=False)),('withdrawal_prepare',dict(fee_mode=mode,replacement=True)),
            ('proof',{}),('withdrawal_settle',dict(fee_mode=mode,replacement=winner=='new')),('reconcile',{})]
for winner in ('old','new'):
    CASES['refund_rbf_'+winner+'_winner']=[('refund_prepare',dict(replacement=False)),('refund_prepare',dict(replacement=True)),
        ('proof',{}),('refund_settle',dict(replacement=winner=='new')),('reconcile',{})]
for mode in ('ordinary','wallet-v1','advanced-contract'):
    CASES['address_'+mode]=[(role,dict(recipient_mode=mode)) for role in ('address_request','address_registration','address_ready')]+[
        ('funding',{}),('proof',{}),('mint',dict(mint_profile='ADDRESS'))]
CASES['refund_btc_cap_raise']=[('fee_owner',dict(method='raiseRefundCap',fee_mode='BTC'))]
CASES['refund_btc_cancel']=[('fee_owner',dict(method='cancelRefund',fee_mode='BTC'))]
for mode in ('BTC','MP','S'):
    CASES['withdrawal_cancel_'+mode.lower()]=[('withdrawal_cancel',dict(fee_mode=mode))]
    CASES['fee_consent_'+mode.lower()]=[('fee_owner',dict(fee_mode=mode,method='consentFee' if mode=='S' else 'consent'))]
CASES['withdrawal_batch_mixed']=[('withdrawal_prepare',dict(multi_owner=True,fee_modes=['BTC','MP','S'])),('proof',{}),
    ('withdrawal_settle',dict(multi_owner=True,fee_modes=['BTC','MP','S'])),('reconcile',{})]

FLOW_CASES=set(CASES)
from .contour_checks import catalog
for identifier,row in catalog().items():
    CASES['spec/'+identifier]=[('scenario_check',dict(scenario_id=identifier,row_sha256=row['row_sha256'],observation_only=True))]


def ancestors(jobs,identifier):
    seen=set();todo=list(jobs[identifier]['depends_on'])
    while todo:
        key=todo.pop()
        if key not in seen:seen.add(key);todo.extend(jobs[key]['depends_on'])
    return seen


def assess(plan,audits):
    jobs={j['id']:j for j in plan['jobs']};results={}
    for case,rules in CASES.items():
        selected=plan['cases'].get(case,[]);missing=[];matched=[]
        for role,properties in rules:
            candidates=[key for key in selected if jobs[key]['profile']['role']==role
                and all(jobs[key]['profile'].get(k)==v for k,v in properties.items())]
            if len(candidates)!=1:missing.append(dict(role=role,properties=properties,reason='EXACT_ROLE_BINDING_REQUIRED'));continue
            matched.append(candidates[0])
        if any(jobs[k]['plan'] is None for k in selected):missing.append(dict(reason='EXPLICIT_JOB_INPUT_BINDING_REQUIRED'))
        if not missing:
            # Ordered monetary roles must be joined by actual dependency edges.
            for before,after in zip(matched,matched[1:]):
                if before not in ancestors(jobs,after):missing.append(dict(role=jobs[after]['profile']['role'],reason='DEPENDENCY_EDGE_REQUIRED'))
            linked=[jobs[k]['profile'] for k in matched if jobs[k]['profile'].get('need') is not None]
            scopes=[jobs[k]['profile'].get('scope') for k in matched]
            if any(s!=scopes[0] for s in scopes):missing.append(dict(reason='EXACT_WORKER_GRAPH_REQUIRED'))
            rbf='_rbf_' in case
            monetary_linked=[p for p in linked if not (rbf and p['role'] in ('withdrawal_prepare','refund_prepare'))]
            if monetary_linked and any(p['need']!=monetary_linked[0]['need'] or p['scope']!=monetary_linked[0]['scope'] for p in monetary_linked):
                missing.append(dict(reason='EXACT_CONSUMER_NEED_AND_GRAPH_REQUIRED'))
            versions=[jobs[k]['profile'] for k in matched if jobs[k]['profile']['role'] in
                ('withdrawal_prepare','withdrawal_settle','refund_prepare','refund_settle')]
            if rbf:
                initial,replacement=[jobs[k]['profile'] for k in matched[:2]];winner=jobs[matched[3]]['profile']
                if (replacement.get('previous')!=initial.get('batch') or any(p.get('request_id')!=initial.get('request_id') for p in (replacement,winner))
                        or winner.get('version_hash')!=(replacement if winner.get('replacement') else initial).get('version_hash')):
                    missing.append(dict(reason='EXACT_REPLACEMENT_FAMILY_AND_WINNER_REQUIRED'))
            elif versions and any(p['version_hash']!=versions[0]['version_hash'] for p in versions):missing.append(dict(reason='EXACT_REVIEWED_VERSION_REQUIRED'))
            address=[jobs[k]['profile'] for k in matched if jobs[k]['profile']['role'].startswith('address_')]
            if address and (not address[0].get('address_version_hash') or any(p.get('address_version_hash')!=address[0]['address_version_hash'] for p in address)):
                missing.append(dict(reason='EXACT_REGISTERED_RECIPIENT_REQUIRED'))
            reserves=[jobs[k]['profile']['reserve'] for k in matched if 'reserve' in jobs[k]['profile']]
            if reserves and any(r!=reserves[0] for r in reserves):missing.append(dict(reason='EXACT_RESERVE_REQUIRED'))
            if case=='refund_btc':
                owner=jobs[matched[0]]['profile'];refund=jobs[matched[1]]['profile']
                if owner.get('request_id')!=refund.get('request_id'):missing.append(dict(reason='EXACT_REFUND_REQUEST_REQUIRED'))
            if case=='incident_resume':
                first,second=[jobs[k]['profile'] for k in matched]
                if (first.get('registry')!=second.get('registry') or first.get('incident_scope')!=second.get('incident_scope')
                        or 'expected_generation' in first and int(second['expected_generation'])!=int(first['expected_generation'])+1):
                    missing.append(dict(reason='EXACT_INCIDENT_SCOPE_AND_GENERATION_REQUIRED'))
        completed=not missing and all(audits.get(k,{}).get('status')=='VERIFIED' for k in matched)
        results[case]=dict(status=('OBSERVATIONS_VERIFIED' if case.startswith('spec/') else 'VERIFIED') if completed else 'UNBOUND' if missing else 'PENDING',jobs=matched,missing=missing,
            observation_only=case.startswith('spec/'))
    return dict(schema='satoshifi.contour-scenario-matrix.v1',catalog_sha256=digest(CASES),
        complete=all(r['status'] in ('VERIFIED','OBSERVATIONS_VERIFIED') for r in results.values()),
        e2e_qualified=False,required_scenario_observations=sum(r['status']=='OBSERVATIONS_VERIFIED' for r in results.values()),cases=results)
