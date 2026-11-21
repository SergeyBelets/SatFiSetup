"""Enroll and review separate original graphs; no asset transfer or authority change."""
import json

from .safe_errors import DiagnosticArgumentParser
from .continuity_handover import Handover
from .epoch_service import Fleet, load_config


def main(argv=None):
    from .cli import _protect_transport_logs
    _protect_transport_logs()
    p = DiagnosticArgumentParser(prog='frost-signer bitcoin continuity handover', description=__doc__)
    p.add_argument('action', choices=('prepare', 'review', 'enroll-signer'))
    p.add_argument('--config', required=True, help='Private epoch fleet configuration retaining both graphs')
    p.add_argument('--source-context'); p.add_argument('--target-context'); p.add_argument('--plan-id')
    p.add_argument('--request-id', action='append', default=[]); p.add_argument('--batch-id', action='append', default=[])
    p.add_argument('--state'); p.add_argument('--password-file'); p.add_argument('--output')
    a = p.parse_args(argv); h = Handover(Fleet(load_config(a.config), configuration=a.config))
    if a.action == 'prepare':
        if not a.source_context or not a.target_context: raise ValueError('CONTINUITY_HANDOVER_CONFIG')
        result = h.prepare(a.source_context, a.target_context, requests=a.request_id, batches=a.batch_id)
    else:
        from .spv_transport import hex32
        if not a.plan_id: raise ValueError('CONTINUITY_HANDOVER_NOT_FOUND')
        hex32(a.plan_id)
        if a.action == 'review': result = h.review(a.plan_id)
        else:
            if not a.state or not a.password_file: raise ValueError('CONTINUITY_HANDOVER_CONFIG')
            from .secure_state import SecureState
            from .onboarding import password_file
            result = h.enroll_signer(a.plan_id, SecureState(a.state, password_file(a.password_file)))
    if a.output:
        from .dkg import atomic_private
        atomic_private(a.output, (json.dumps(result, sort_keys=True) + '\n').encode())
    print(json.dumps(result, sort_keys=True)); return 0
