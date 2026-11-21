"""Single canonical funding coverage for compatible work; no implicit payer debit."""
import hashlib
from eth_abi import encode
from web3 import Web3
from .gas_treasury import GasTreasuryClient
from .bootstrap import Bootstrap,artifact
from .spv_automation import Budget,identity,integer,validate_budget,funding_scope,quote,observation
from .zk_worker_service import semantic_key,attach
from .operations import canonical


def validate(c):
    p=c['automation']
    if p==dict(enabled=False):return
    fields={'enabled','issuer_identity_file','permission_seconds','max_tx_gas','max_transaction_cost_wei','max_total_cost_wei','max_job_cost_wei','max_attempts_per_job'}
    if set(p)!=fields or p['enabled'] is not True or c['service']['mode']=='confederation':raise ValueError('ZK_AUTO_ISSUER_POLICY')
    from pathlib import Path
    if not Path(p['issuer_identity_file']).is_absolute():raise ValueError('ZK_AUTO_ISSUER_POLICY')
    integer(p['permission_seconds'],60,86400);validate_budget(p)


def tick(worker,transport,*,routes=None,recover_only=False,before_submit=None):
    selected_routes=routes
    p=worker.c.get('automation',{})
    if not p.get('enabled'):return dict(status='DISABLED')
    w=worker;c=transport.client;client=GasTreasuryClient(w.w3,w.c['service']['binding'])
    block=client._binding();client.service_profile(block)
    cfg=client._call(client.registry,'configuration',block)
    if cfg[1]!=identity(p['issuer_identity_file']):raise ValueError('ZK_AUTO_ISSUER_IDENTITY')
    # Recover this exact signed admission before rebasing or issuing a new permission.
    pending=w.inbox.get('auto_admission')
    if pending:
        if routes is not None:
            coverage=w.inbox.get('auto_coverage') or {}
            selected=[w.inbox.get('remote:'+route) for route in routes]
            if (pending['job']!=coverage.get('job') or not any(
                    r and r['local_need_id'] in coverage.get('need_ids',[]) for r in selected)):
                return dict(status='OTHER_ADMISSION_PENDING')
        budget=Budget(p['issuer_identity_file'],funding_scope(client)+':issuer',p,pending['job'])
        result=client.submit(pending['request'],p['issuer_identity_file'],budget=budget,recover_only=True)
        if result['status'] not in ('NOT_SIGNED','CONFIRMED'):return result
        if (recover_only or before_submit is not None) and result['status']=='NOT_SIGNED':return result
        w.inbox.put('auto_admission',None)
        if recover_only or before_submit is not None:return result
    routes=[]
    for key,body in w.inbox.db.execute("SELECT key,body FROM meta WHERE key LIKE 'remote:%'"):
        import json
        link=json.loads(body)
        if link.get('payment_request') and (selected_routes is None or key[7:] in selected_routes):routes.append((key[7:],link))
    statuses={k:s for k,_,s in w.inbox.needs()}
    need_map={k:r for k,r,_ in w.inbox.needs()}
    coverage=w.inbox.get('auto_coverage')
    if coverage and all(statuses.get(k) in ('READY','COMPLETED') for k in coverage['need_ids']):
        coverage['state']='COVERED';w.inbox.put('auto_coverage',coverage)
    for route,link in routes:
        local=link['local_need_id'];need=need_map[local]
        if statuses.get(local) in ('READY','COMPLETED'):continue
        observation_result,core=observation(w.w3,w.c,w.c['zk_state'],need,w.bitcoin,w.c['allowed_consumers'])
        if observation_result['status']!='NEEDS_STATE':continue
        linked=w.inbox.get('service:'+local)
        if linked and (not coverage or coverage['job']!=linked['service_job_id'] or coverage['state']!='ACTIVE'):
            # A completed primary mint must not cause its already covered alias
            # to acquire a second semantic job after restart/state catch-up.
            job=linked['service_job_id'];known=client._call(client.registry,'jobs',block,bytes.fromhex(job))
            if (bytes(known[0]).hex()!=job or bytes(known[1])!=semantic_key(w,linked['need_ids'])
                    or str(known[5])!=linked['deadline']):raise ValueError('ZK_AUTO_JOB_TERMS_CONFLICT')
            names=('jobId','semanticKey','federationIdHash','payer','budgetScope','serviceDeadline')
            coverage=dict(job=job,terms=dict(zip(names,Bootstrap.wire(known))),semantic_need_ids=linked['need_ids'],
                need_ids=[local],payer_route=route,target_height=core['blocks'],target_hash=core['bestblockhash'],state='ACTIVE')
            w.inbox.put('auto_coverage',coverage)
        if coverage and coverage['state']=='ACTIVE':
            if observation_result['height']+observation_result['depth']-1>coverage['target_height']:continue
            if w.bitcoin.call('getblockhash',coverage['target_height'])!=coverage['target_hash']:
                # Preserve paid job/terms; a new proving attempt can rebase it.
                if not client.status('0x'+coverage['job'])['paid']:continue
                coverage['target_height']=core['blocks'];coverage['target_hash']=core['bestblockhash']
            if local not in coverage['need_ids']:coverage['need_ids'].append(local);w.inbox.put('auto_coverage',coverage)
        else:
            offered=link['payment_request']
            treasury=client._treasury(cfg[0],block)
            if (offered['treasury']!=cfg[0] or offered['tariff_version']!=str(cfg[2]) or offered['tariff_hash']!=bytes(cfg[3]).hex()
                    or int(offered['max_service_fee_wei'])<cfg[4] or offered['treasury_epoch']!=str(client._call(treasury,'epoch',block))
                    or offered['roster_hash']!=bytes(client._call(treasury,'rosterHash',block)).hex()):continue
            semantic=semantic_key(w,[local]);existing=client._call(client.registry,'semanticJob',block,semantic)
            job=bytes(existing).hex() if bytes(existing)!=bytes(32) else Web3.keccak(encode(['bytes32','bytes32'],[bytes.fromhex(client.manifest['domain'].removeprefix('0x')),semantic])).hex()
            terms=dict(jobId='0x'+job,semanticKey='0x'+semantic.hex(),federationIdHash=Web3.to_hex(Web3.keccak(text=link['federation'])),
                payer=offered['payer'],budgetScope='0x'+offered['budget_scope'],serviceDeadline=str(link['deadline']))
            known=client._call(client.registry,'jobs',block,bytes.fromhex(job))
            if bytes(known[0])!=bytes(32):
                actual=dict(zip(terms,Bootstrap.wire(known)))
                if any(actual[k]!=terms[k] for k in terms if k!='serviceDeadline') or int(actual['serviceDeadline'])>int(terms['serviceDeadline']):raise ValueError('ZK_AUTO_JOB_TERMS_CONFLICT')
                terms=actual
            coverage=dict(job=job,terms=terms,semantic_need_ids=[local],need_ids=[local],payer_route=route,
                target_height=core['blocks'],target_hash=core['bestblockhash'],state='ACTIVE')
            w.inbox.put('auto_coverage',coverage)
        job=coverage['job']
        paid=client._call(client.registry,'paid',block,bytes.fromhex(job))
        permission=client._call(client.registry,'permissions',block,bytes.fromhex(job))
        if not paid:
            terms=coverage['terms'];deadline=int(terms['serviceDeadline'])
            if block.timestamp>=deadline:return dict(status='SERVICE_DEADLINE_MISSED',job=job)
            view=client.w3.eth.contract(address=client.manifest['state_view'],abi=artifact('ISPVServiceStateView')['abi'])
            snap=client._call(view,'snapshot',block)
            generation=client._call(client.registry,'generation',block)
            usable=permission[5] and permission[1]==generation and bytes(permission[2])==bytes(snap[0]) and permission[3]==snap[1] and permission[4]>block.timestamp+30
            if not usable:
                if recover_only:return dict(status='NOT_SIGNED')
                accept_by=str(min(deadline,block.timestamp+int(p['permission_seconds'])))
                request=client.prepare_admit(terms,accept_by)
                w.inbox.put('auto_admission',dict(job=job,request=request))
                from .address_spv import guard
                from .withdrawal_fee_spv import guard as fee_guard
                budget=Budget(p['issuer_identity_file'],funding_scope(client)+':issuer',p,job,
                    guard=guard(w.w3,w.c,w.c['zk_state'],need,w.bitcoin,w.c['allowed_consumers'])
                        or fee_guard(w.w3,w.c,w.c['zk_state'],need,w.bitcoin,w.c['allowed_consumers']))
                if before_submit is None:result=client.submit(request,p['issuer_identity_file'],budget=budget)
                else:result=client.submit(request,p['issuer_identity_file'],budget=budget,
                    recover_only=bool(before_submit('admit',request,job)))
                if result['status']=='CONFIRMED':w.inbox.put('auto_admission',None)
                return result
            if route==coverage['payer_route']:
                q=quote(client,job,dict(target_height=str(coverage['target_height']),target_hash=coverage['target_hash']))
                sequence=(c.store.get('status:'+route) or {}).get('sequence',0)+1
                marker=hashlib.sha256(canonical(q).encode()).hexdigest()
                if c.store.get('quote:'+route)!=marker and sequence<=64:
                    body=dict(version='1',request_id=link['request_id'],operation_id=link['operation_id'],status='QUOTE',
                        service_deadline=str(link['deadline']),quote=q)
                    c.queue_reply(route,'STATUS',sequence,body)
                    c.store.put('status:'+route,dict(sequence=sequence,digest=None,next_at=block.timestamp))
                    c.store.put('quote:'+route,marker)
        else:
            # Aliases copy the original semantic set. They never alter immutable
            # registry terms or ask another payer for a second full contribution.
            primary=coverage['semantic_need_ids'][0]
            if not w.inbox.get('service:'+primary):attach(w,dict(service_job_id=job,need_ids=coverage['semantic_need_ids']))
            original=w.inbox.get('service:'+primary)
            for need_id in coverage['need_ids']:
                old=w.inbox.get('service:'+need_id)
                if old is None:w.inbox.put('service:'+need_id,dict(original,coverage_job=job))
        client._anchor(block)
    return dict(status='IDLE')
