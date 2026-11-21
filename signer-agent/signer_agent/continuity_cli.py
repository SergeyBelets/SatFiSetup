"""Canonical inventory and explicit ORIGINAL_GRAPH_ONLY transition decisions."""
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .epoch_service import ObservationClient
from .address_transport import load_config
from .bitcoin_continuity import Catalog,sample,inventory,PROFILE,digest
from .dkg import atomic_private


def compare(source,candidate):
    b,_,ctx,_,_=source.context();old=sample(source.w3,ctx,b.number)
    b,_,ctx,_,_=candidate.context();new=sample(candidate.w3,ctx,b.number)
    catalog=Catalog(identity_file=source.c['identity_file'])
    original=catalog.observe(source.w3,old);target=catalog.observe(candidate.w3,new)
    same=old['identity']==new['identity']
    reasons=original['reasons']+target['reasons']
    reason=('ORIGINAL_GRAPH_CONTINUATION' if same and not reasons else reasons[0] if same
        else 'CONTINUITY_GRAPH_CHANGE' if old['family']==new['family'] else 'CONTINUITY_CUSTODY_MIGRATION_UNSUPPORTED')
    result=dict(profile=PROFILE,status='SUPPORTED_ORIGINAL_GRAPH' if reason=='ORIGINAL_GRAPH_CONTINUATION' else 'BLOCKED',
        reason=reason,source_family=old['family'],target_family=new['family'],source_fingerprint=digest(old['identity']),
        target_fingerprint=digest(new['identity']),original=original,candidate=target,
        transfers_assets=False,monetary_permission_checked=False,activation_allowed=False,ready=False,production_ready=False)
    catalog.lane.put('continuity-transition:'+digest(result),result)
    return result


def main(argv=None):
    import sys
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0]=='handover':
        from .continuity_handover_cli import main as handover_main
        return handover_main(argv[1:])
    from .cli import _protect_transport_logs
    _protect_transport_logs()
    p=DiagnosticArgumentParser(prog='frost-signer bitcoin continuity',description=__doc__)
    p.add_argument('action',choices=('inspect','check','recover','pin-root','root-plan'))
    p.add_argument('--root-policy')
    p.add_argument('--config');p.add_argument('--candidate-config');p.add_argument('--output')
    p.add_argument('--identity-file');p.add_argument('--evm-rpc');p.add_argument('--evm-confirmations',default='12')
    p.add_argument('--auth-bundle');p.add_argument('--registry-target');p.add_argument('--deadline')
    p.add_argument('--max-entries',type=int,default=4096);p.add_argument('--max-bindings',type=int,default=256)
    p.add_argument('--request-id',action='append',default=[]);p.add_argument('--batch-id',action='append',default=[])
    a=p.parse_args(argv)
    if a.action not in ('pin-root','root-plan') and not a.config:raise ValueError('CONTINUITY_CONFIG_REQUIRED')
    source=ObservationClient(load_config(a.config)) if a.config else None
    if a.action in ('pin-root','root-plan'):
        if not a.root_policy:raise ValueError('FAMILY_ROOT_POLICY')
        from .family_registry import install
        from .onboarding import read_private
        if source is not None:w3=source.w3;identity=source.c['identity_file'];confirmations=source.c['evm_confirmations']
        else:
            from web3 import Web3
            if not a.evm_rpc or a.action=='pin-root' and not a.identity_file:raise ValueError('FAMILY_ROOT_POLICY')
            w3=Web3(Web3.HTTPProvider(a.evm_rpc,request_kwargs={'timeout':30}));identity=a.identity_file;confirmations=a.evm_confirmations
        from .evm_network import read_block
        b=read_block(w3,int(confirmations))
        if a.action=='pin-root':result=install(w3,Catalog(identity_file=identity),json.loads(read_private(a.root_policy)),b.number)
        else:
            if not a.output or not a.auth_bundle or not a.registry_target or not a.deadline:raise ValueError('FAMILY_ROOT_ENROLLMENT')
            from .family_registry import enrollment_plan
            result=enrollment_plan(w3,json.loads(read_private(a.root_policy)),json.loads(read_private(a.auth_bundle)),a.registry_target,a.deadline,a.evm_rpc,confirmations)
    elif a.action=='check':
        if not a.candidate_config:raise ValueError('CONTINUITY_CANDIDATE_REQUIRED')
        result=compare(source,ObservationClient(load_config(a.candidate_config)))
    elif a.action=='recover':
        b,_,ctx,_,_=source.context();s=sample(source.w3,ctx,b.number)
        result=Catalog(identity_file=source.c['identity_file']).observe(source.w3,s,recover=True)
        result['status']='CONTINUITY_OBSERVATION_REANCHORED'
    else:
        if not a.output:raise ValueError('CONTINUITY_OUTPUT_REQUIRED')
        result=inventory(source,max_entries=a.max_entries,max_bindings=a.max_bindings,requests=a.request_id,batches=a.batch_id)
    if a.output:
        if a.action=='root-plan' and Path(a.output).exists():raise ValueError('FAMILY_ROOT_PLAN_EXISTS')
        atomic_private(a.output,(json.dumps(result,sort_keys=True)+'\n').encode())
        print(json.dumps(dict(status=result.get('status','ROOT_ENROLLMENT_PLAN_SAVED' if a.action=='root-plan' else 'CONTINUITY_INVENTORY_SAVED'),profile=PROFILE,ready=False,production_ready=False)))
    else:print(json.dumps(result))
    return 0


if __name__=='__main__':
    try:main()
    except Exception as exc:
        from .participant_errors import participant_exception_code
        print(json.dumps(dict(status='ERROR',code=participant_exception_code(exc))))
        raise SystemExit(1) from None
