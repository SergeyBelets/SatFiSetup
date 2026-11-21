"""Source-bound standalone incident control checks, without claiming live recovery."""
import argparse
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .contour import CAPABILITIES
from .contour_worker_evidence import ROOT,OUT,sha


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'incident-contour-tests.xml')
    counts={k:sum(int(s.get(k,0)) for s in tree.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in [('test_contour_incident','signer-agent/tests/test_contour_incident.py'),
                      ('test_incident_status','signer-agent/tests/test_incident_status.py'),
                      ('test_contour_withdrawal','signer-agent/tests/test_contour_withdrawal.py'),
                      ('test_contour_mint','signer-agent/tests/test_contour_mint.py'),
                      ('test_contour_setup','signer-agent/tests/test_contour_setup.py'),
                      ('test_contour_service','signer-agent/tests/test_contour_service.py'),
                      ('test_contour_worker','signer-agent/tests/test_contour_worker.py'),('test_contour','tests/zkspv/test_contour.py')]:
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if {c.get('name','').split('[')[0] for c in cases}!=expected:raise ValueError('CONTOUR_INCIDENT_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if focused['test_contour_incident']<79 or any(counts[k] for k in ('failures','errors','skipped')):
        raise ValueError('CONTOUR_INCIDENT_CHECKS_INCOMPLETE')
    artifacts=[]
    for name in ('incident-contour-tests.xml','incident-contour-tests.log','incident-contour-command.json'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    previous=json.loads((OUT/'contour-withdrawal-report.json').read_text());paths=set(previous['source_sha256'])
    paths.update(['ops/zkspv/contour_incident.py','ops/zkspv/contour_incident_evidence.py',
        'signer-agent/tests/test_contour_incident.py','config/zkspv-contour-incident.example.json',
        'config/incident-service.example.json','signer-agent/signer_agent/incident_cli.py',
        'signer-agent/signer_agent/incident_transport.py','signer-agent/signer_agent/incident_observations.py',
        'signer-agent/signer_agent/address_transport.py','signer-agent/signer_agent/address_status.py',
        'signer-agent/signer_agent/address_status_client.py','signer-agent/signer_agent/address_registration.py',
        'signer-agent/signer_agent/address_registration_client.py','signer-agent/tests/test_incident_status.py',
        'signer-agent/contracts/BitcoinIncidentMessageBoard.json','signer-agent/contracts/BitcoinAddressRegistry.json',
        'web3/contracts/src/federation/BitcoinIncidentMessageBoard.sol','web3/contracts/src/federation/BitcoinAddressRegistry.sol',
        'docs/ADDRESS_DEPOSIT_REGISTRY.md'])
    report=dict(schema='satoshifi.contour-incident-recovery.v1',as_of='2026-11-02',
        status='STANDALONE_INCIDENT_RECOVERY_ADAPTER_IMPLEMENTED_COMBINED_RUN_PENDING',code_implemented=True,
        connected_capabilities=[c for c in CAPABILITIES if c!='old_epoch_service'],
        complete_driver_set_implemented=False,global_onchain_family_registry_implemented=False,cross_graph_asset_migration_supported=False,
        ready=False,production_ready=False,python_tests=counts,focused_tests=focused,
        max_evm_operations_per_action=1,implicit_attempt_retry=False,implicit_deadline_extension=False,
        local_signer_approval_automatically_created=False,frost_share_automatically_created=False,
        deferred_existing_native_checks=previous['deferred_existing_native_checks'],
        implemented=['Original read-only status-preview freezes exact state/generation/sequence/target/deadline and evidence commitments before transport',
            'Explicitly accepted decision hash, original reporter/operator client scopes, enrolled EVM_AUTH and transitive recovery service/fee configuration pins',
            'Standalone partial runner plan has no monetary, proof or Fresh prerequisites; authenticates configured state/adapter origins without live freshness',
            'Original native HPKE/private report/ACK/status, selected route only, retained ciphertext and reporter identity; unrelated outboxes remain unpublished',
            'Original status Driver/policy prepares and publishes one exact initial intent, waits for external private participant approvals/FROST, and executes exact status receipt',
            'Original actor gas ledger and nonce journal; typed pre-sign enrollment outside inbox backup, exact signed recovery first, unsigned ambiguity holds',
            'Confirmed/latest generation checks and retained terminal/closed/reorg observations before new authorization',
            'Resume uses original complete watch/Core and applicable reserve service checks; missing qualification/service blocks new work without clearing financial pauses',
            'Canonical exact status receipt plus selected encrypted status delivery; latest-only receipt waits and historical completion remains independently inspectable'],
        boundaries=['Original HPKE, ABI/EOA signatures, address budgets, global nonce/SQLite and retained observation journals are exercised',
            'Original report/status clients, typed EVM_AUTH policies and Core/watch resume-service guard execute against explicit graph/provenance/provider/qualification/FROST/EVM state fixtures',
            'Actual CLI/controller entrypoints are dispatched in-process with real context/challenge binding and private hashed completion evidence',
            'One original reporter, one operator, one direct delivery and one initial status attempt; forwarding/key registration/retries/reanchor remain explicit original workflows',
            'Preview/accepted decision does not approve evidence or grant status authority; external participants retain original private review, approval and FROST nonce/spent workflow',
            'No silent generation rebase, deadline extension, session retry, implicit financial pause clearing or resurrection of terminal/closed issuance',
            'Historical receipt/notification is not current Fresh, perpetual service health or Ready; actual QUALIFIED/FEE_QUALIFIED flags remain false',
            'No actual public incident/status transaction, live threshold authorization, Core/proof/consumer run or production recovery is established'],
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_frost_authorization_performed=False,
        actual_onchain_incident_performed=False,actual_value_transfer_performed=False,actual_bootstrap_deployment_performed=False,
        combined_e2e_performed=False,independent_review_performed=False,
        deferred=['Old-epoch service adapter',
            'Source-bound multi-need acceptance collector, initial heartbeat/setup-to-withdrawal coordination and actual long proving/monetary/recovery run',
            'Global on-chain family/status continuity, live inventory/independent qualification and specification 15.11'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-incident-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
