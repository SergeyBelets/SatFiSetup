"""Read-only canonical handover adapter for the contour driver's evidence contract."""
import argparse
import json
import os
from pathlib import Path

from .contour import private_write, sha


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fleet-config', required=True); p.add_argument('--plan-id', required=True)
    a = p.parse_args(argv)
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs()
    context_file = Path(os.environ['SATOHSI_CONTOUR_CONTEXT'])
    if context_file.stat().st_size > 1024 * 1024: raise ValueError('CONTOUR_RESPONSE')
    context = json.loads(context_file.read_text())
    if context['action'] not in ('probe', 'run', 'recover'): raise ValueError('CONTOUR_RESPONSE')
    if context['capability']!='continuity':raise ValueError('CONTOUR_CAPABILITY')
    from signer_agent.continuity_handover import Handover
    from signer_agent.epoch_service import Fleet, load_config
    result = Handover(Fleet(load_config(a.fleet_config), configuration=a.fleet_config)).review(a.plan_id)
    satisfied = result['status'] == 'RETAINED_SERVICE_CHECKED'
    artifacts = {}
    if satisfied:
        # Fresh review above is mandatory. The first retained report stays immutable
        # as evidence of enrollment, and is not used as a cached permission check.
        artifacts = context['prior'].get('artifacts', {})
        if not artifacts:
            path = context_file.parent / 'handover.json'; private_write(path, result)
            artifacts = {str(path.relative_to(Path(context['directory']))): sha(path)}
    binding = {k: context[k] for k in ('version', 'run_id', 'plan_sha256', 'stage','capability', 'attempt_id',
                                      'challenge', 'action', 'predecessors')}
    private_write(context['response_file'], dict(binding=binding, status='SATISFIED' if satisfied else 'WAITING',
        artifacts=artifacts, observations=dict(plan_id=a.plan_id, status=result['status'], reasons=result['reasons'],
            actual_value_transfer_performed=False, fresh_proving_performed=False, production_ready=False)))


if __name__ == '__main__': main()
