"""Retain source-bound local service checks; live monetary/proving acceptance is pending."""
import argparse
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .contour_worker_evidence import ROOT, OUT, sha


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'service-contour-tests.xml')
    suites=list(tree.getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in [('test_contour_service','signer-agent/tests/test_contour_service.py'),
                      ('test_contour_worker','signer-agent/tests/test_contour_worker.py'),
                      ('test_contour','tests/zkspv/test_contour.py'),
                      ('test_continuity_handover','signer-agent/tests/test_continuity_handover.py')]:
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body
                  if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if {c.get('name','').split('[')[0] for c in cases}!=expected:
            raise ValueError('CONTOUR_SERVICE_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if counts['tests']==0 or focused['test_contour_service']<41 or any(counts[k] for k in ('failures','errors','skipped')):
        raise ValueError('CONTOUR_SERVICE_CHECKS_INCOMPLETE')
    artifacts=[]
    for name in ('service-contour-tests.xml','service-contour-tests.log'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    previous=json.loads((OUT/'contour-worker-report.json').read_text())
    paths=set(previous['source_sha256'])
    paths.update(['ops/zkspv/contour_service.py','ops/zkspv/contour_service_evidence.py',
        'config/zkspv-contour-service.example.json','signer-agent/tests/test_contour_service.py',
        'signer-agent/tests/test_spv_transport.py','signer-agent/signer_agent/spv_transport.py',
        'signer-agent/signer_agent/spv_automation.py','signer-agent/signer_agent/zk_service_issuer.py',
        'signer-agent/signer_agent/gas_treasury.py','signer-agent/signer_agent/zk_worker_transport.py',
        'signer-agent/signer_agent/withdrawal_fee_spv.py','signer-agent/signer_agent/address_spv.py',
        'ops/zkspv/contour.py','ops/zkspv/contour_worker.py','ops/zkspv/contour_plan.py','ops/zkspv/report.py',
        'config/zkspv-contour.example.json','docs/ZKSPV_CONTOUR.md','docs/API.md','docs/CONFIGURATION.md','docs/README.md'])
    report=dict(schema='satoshifi.contour-service-adapters.v1',as_of='2026-11-02',
        status='SERVICE_WORKER_PIPELINE_IMPLEMENTED_COMBINED_RUN_PENDING',code_implemented=True,
        connected_capabilities=['consumer_request','encrypted_transport','admission_payment','proof_preparation',
            'fresh_proof','state_update','service_status','continuity'],
        complete_driver_set_implemented=False,global_onchain_family_registry_implemented=False,
        cross_graph_asset_migration_supported=False,ready=False,production_ready=False,
        python_tests=counts,focused_tests=focused,max_service_evm_operations_per_action=1,
        funding_modes=['PAID','CONFEDERATION'],
        deferred_existing_native_checks=[
            'test_zk_worker_service.test_unpaid_consumer_cannot_veto_own_heartbeat',
            'test_zk_worker_service.test_existing_fresh_root_does_not_require_second_service_payment',
            'test_spv_transport.test_worker_groups_two_ready_needs_into_one_prepared_proof'],
        implemented=[
            'Original native HPKE REQUEST/ACK/STATUS with stable revision/ciphertext/nonce/deadline and complete canonical packet checks',
            'Selected-route transport and original remote inbox/SLA admission without acting on unrelated queued requests',
            'Canonical issuer admission, encrypted quote and consent-bound payer automation through original gas budgets and nonce journals',
            'Persisted typed substeps before submission; exact signed recovery and unsigned recovery holds without a new nonce/value transfer',
            'Confirmed payer transaction/block hashes revalidated for finality/reorgs before new work; pending recovery precedes covered-state shortcuts',
            'Compatible alias/job reuse with original semantic needs/deadline and canonical Bitcoin target, without duplicate quote/payment',
            'Explicit confederation own funding, also under paid_or_confederation, independent of project treasury balance within original publisher/cost caps',
            'Own cost reservations and original paid history remain distinct; no synthetic paid marker, reimbursement claim or principal spending',
            'Fresh delivery/funding prerequisites for new prepare/prove/unsigned publication; historical proof and signed publication recovery retained',
            'Encrypted post-publication status re-observes original need, including NEEDS_STATE/HALTED without asserting monetary readiness',
            'Offline seven-stage plan builder pins all actor configs; retained-graph review remains a separate eighth capability'],
        boundaries=[
            'Real native HPKE, SQLite, transport/issuer/payer state machines, typed request preparation, budget reservation and worker/controller code are exercised',
            'Canonical board, Core, EVM execution/payment receipts and proving/receipt verification are explicit external-boundary fixtures in service checks',
            'The runner CLI-chain test dispatches actual adapter entry points in-process; it is not a live combined monetary run',
            'Selected local regression and prior signed-envelope/controller checks do not replace deferred native host/guest tests or full E2E',
            'Independent journals and signed-byte recovery do not protect total destruction of all retained state or guarantee operation with all RPC unavailable',
            'No contract, mainnet guest, maturity, owner consent, threshold or principal-accounting policy is relaxed'],
        real_native_hpke_exercised=True,fresh_proving_performed=False,actual_core_acceptance_performed=False,
        actual_value_transfer_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        deferred=['Bootstrap/Bitcoin funding, membership materialization and SMP1/address mint adapters',
            'BTC/MP/S withdrawal, reconciliation, incident/recovery and old-epoch service adapters',
            'Complete source-bound acceptance collector and actual long combined proving/monetary run',
            'Global on-chain family/status continuity, live inventory/independent qualification and specification 15.11'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-service-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
