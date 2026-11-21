"""Source-bound worker adapter checks; live proving/monetary acceptance stays pending."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'ops/artifacts/zkspv/worker-integration'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'worker-contour-tests.xml')
    suites=list(tree.getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in [('test_contour_worker','signer-agent/tests/test_contour_worker.py'),
                      ('test_contour','tests/zkspv/test_contour.py'),
                      ('test_continuity_handover','signer-agent/tests/test_continuity_handover.py')]:
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)
               or not c.get('classname') and c.get('name','').split('[')[0] in expected]
        if {c.get('name','').split('[')[0] for c in cases}!=expected:raise ValueError('CONTOUR_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if counts['tests']==0 or focused['test_contour_worker']<27 or any(counts[k] for k in ('failures','errors','skipped')):
        raise ValueError('CONTOUR_CHECKS_INCOMPLETE')
    artifacts=[]
    for name in ('worker-contour-tests.xml','worker-contour-tests.log'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    previous=json.loads((OUT/'contour-handover-report.json').read_text())
    paths=set(previous['source_sha256'])
    paths.update(['ops/zkspv/contour_worker.py','ops/zkspv/contour_plan.py','ops/zkspv/contour_worker_evidence.py',
        'ops/zkspv/state_trial_prove.py','signer-agent/signer_agent/zk_worker.py','signer-agent/signer_agent/zk_worker_prover.py',
        'signer-agent/signer_agent/zk_worker_service.py','signer-agent/signer_agent/zk_worker_transport.py',
        'signer-agent/signer_agent/zk_worker_heartbeat.py','signer-agent/signer_agent/zk_worker_batching.py',
        'signer-agent/signer_agent/zk_spv.py','signer-agent/signer_agent/zk_spv_cli.py','signer-agent/signer_agent/bootstrap.py',
        'signer-agent/signer_agent/spv_automation.py','signer-agent/tests/test_contour_worker.py',
        'signer-agent/tests/test_zk_worker.py','signer-agent/tests/test_zk_worker_service.py',
        'signer-agent/tests/test_spv_automation.py','signer-agent/tests/test_zk_archive.py'])
    report=dict(schema='satoshifi.contour-worker-adapters.v1',as_of='2026-11-02',
        status='WORKER_PIPELINE_ADAPTERS_IMPLEMENTED_COMBINED_RUN_PENDING',code_implemented=True,
        connected_capabilities=['consumer_request','proof_preparation','fresh_proof','state_update','continuity'],
        complete_driver_set_implemented=False,global_onchain_family_registry_implemented=False,
        cross_graph_asset_migration_supported=False,ready=False,production_ready=False,python_tests=counts,focused_tests=focused,
        max_new_prover_stages_per_action=1,
        deferred_existing_native_checks=[
            'test_zk_worker_service.test_unpaid_consumer_cannot_veto_own_heartbeat',
            'test_zk_worker_service.test_existing_fresh_root_does_not_require_second_service_payment'],
        implemented=['Actual Worker.request and deterministic original inbox/attempt enrollment',
            'Original preparation state machine with no proving or publication; original default tick preserved',
            'Fresh local proving enrollment before any stage, bypassing receipt-cache reuse without deleting cache',
            'At most one new composite/succinct/Groth16 stage per action; existing retries/budgets and independent final receipt verification',
            'Worker lock retained into new-session prover; interrupted/pending completion remains a recovery hold',
            'Publication verifies an existing proof; exact signed chain/target/input/value/nonce/caps and canonical receipt/finality checks',
            'Recovery through original engine/bytes, including lost local completion; unsigned recovery cannot allocate a nonce',
            'Private offline plan builder pins config/request before first use and explicitly retains missing capabilities',
            'Controller capability binding, frozen evidence and bounded repeated recovery context'],
        boundaries=['Real worker/controller/prover-stage control, SQLite, source/artifact hashes, ABI and EVM signatures are exercised',
            'Core/EVM/native preparation and cryptographic proving/receipt verification are explicit fixtures in worker adapter checks',
            'Real child descriptor inheritance is tested without claiming sandbox/network qualification',
            'Selected local regression does not replace native host/guest builds or all existing E2E',
            'Freshness is local stage provenance, not hardware/remote attestation or independent qualification',
            'No changes to monetary contracts, mainnet guest, custody threshold, owner consent or fee/principal accounting'],
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_value_transfer_performed=False,
        combined_e2e_performed=False,independent_review_performed=False,
        deferred=['Encrypted request/ACK/status and canonical admission/quote/payment adapters',
            'Bootstrap/funding, membership, SMP1/address mint, BTC/MP/S withdrawal, reconciliation/incident/old-epoch adapters',
            'One complete source-bound combined acceptance collector and actual long run',
            'Global on-chain family/status continuity, actual qualification/live inventory and specification 15.11'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-worker-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
