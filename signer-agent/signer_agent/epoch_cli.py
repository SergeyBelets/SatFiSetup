"""Local epoch service and operational handover checks; no key rotation or spending."""
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .epoch_service import Fleet,load_config
from .onboarding import read_private
from .dkg import atomic_private


def main(argv=None):
    p=DiagnosticArgumentParser(prog='frost-signer bitcoin epochs',description=__doc__)
    p.add_argument('action',choices=('run-once','review-start','attest','review'))
    p.add_argument('--config',required=True)
    p.add_argument('--schedule-spv',action='store_true',help='Explicit opt-in to the existing bounded admission/quote/payment route')
    p.add_argument('--departing',action='append',default=[])
    p.add_argument('--challenge-file');p.add_argument('--attestation-file',action='append',default=[])
    p.add_argument('--output');p.add_argument('--manifest');p.add_argument('--state');p.add_argument('--password-file');p.add_argument('--identity-file')
    a=p.parse_args(argv)
    from .cli import _protect_transport_logs
    _protect_transport_logs()
    fleet=Fleet(load_config(a.config),configuration=a.config)
    if a.schedule_spv and a.action!='run-once':raise ValueError('EPOCH_CONFIG')
    if a.action=='run-once':result=fleet.run_once(schedule_spv=a.schedule_spv)
    elif a.action=='review-start':
        if not a.output:raise ValueError('EPOCH_OUTPUT_REQUIRED')
        from .epoch_retention import challenge
        request=challenge(fleet,a.departing)
        atomic_private(a.output,(json.dumps(request,sort_keys=True)+'\n').encode())
        result=dict(status='RETENTION_REVIEW_CREATED',challenge=request['challenge'],ready=False)
    else:
        if not a.challenge_file or Path(a.challenge_file).stat().st_size>16384:raise ValueError('EPOCH_RETENTION_REQUEST')
        request=json.loads(read_private(a.challenge_file))
        if a.action=='attest':
            if not all((a.output,a.manifest,a.state,a.password_file,a.identity_file)):raise ValueError('EPOCH_SIGNER_UNAVAILABLE')
            from .dkg_protocol import Manifest
            from .dkg import Registry
            from .secure_state import SecureState
            from .onboarding import password_file,validate_bundle
            from .epoch_retention import attest
            bundle=json.loads(read_private(a.manifest));m=Manifest.from_dict(bundle)
            e=fleet.entry(m.context.hex());c=fleet.configs[e['wallet_context']]
            private=json.loads(read_private(a.identity_file))['private_key']
            registry=Registry(c['evm_rpc'],m.registry,private)
            registry.identity_file=a.identity_file
            registry.transaction_journal=str(Path(a.identity_file).resolve())+'.transactions.sqlite'
            registry.fee_reserve_config=c.get('fee_reserve_config')
            registry.bitcoin_graph=dict(version='2',chain_id=c['chain_id'],bitcoin_network=c['bitcoin_network'],evm_confirmations=c['evm_confirmations'],
                **({'bitcoin_policy':c['bitcoin_policy']} if c['bitcoin_policy'] is not None else {}))
            state=SecureState(a.state,password_file(a.password_file));registry.check(m)
            if bundle.get('onboarding'):validate_bundle(bundle,state,registry)
            packet=attest(fleet,request,m,registry,state,private,a.config)
            atomic_private(a.output,(json.dumps(packet,sort_keys=True)+'\n').encode())
            result=dict(status='EPOCH_RETENTION_ATTESTED',wallet_context=m.context.hex(),participant=packet['body']['participant'],
                expires_at=packet['body']['expires_at'],ready=False)
        else:
            from .epoch_retention import review
            if len(a.attestation_file)>512:raise ValueError('EPOCH_ATTESTATION')
            packets=[]
            for path in a.attestation_file:
                if Path(path).stat().st_size>16384:raise ValueError('EPOCH_ATTESTATION')
                packets.append(json.loads(read_private(path)))
            result=review(fleet,request,packets)
    print(json.dumps(result));return 0


if __name__=='__main__':
    try:main()
    except Exception as exc:
        from .participant_errors import participant_exception_code
        print(json.dumps(dict(status='ERROR',error=participant_exception_code(exc))))
        raise SystemExit(1) from None
