"""Record source-bound suite implementation checks without granting live/production acceptance."""
import argparse
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .contour import CAPABILITIES,sha,digest
from .contour_plan import source_paths
from .contour_matrix import CASES
from .contour_worker_evidence import ROOT,OUT


FOCUSED={
    'test_contour_suite':'tests/zkspv/test_contour_suite.py',
    'test_contour_suite_plan':'signer-agent/tests/test_contour_suite_plan.py',
    'test_contour_heartbeat':'signer-agent/tests/test_contour_heartbeat.py',
    'test_contour_worker_cycle':'signer-agent/tests/test_contour_worker_cycle.py',
    'test_contour_fee':'signer-agent/tests/test_contour_fee.py',
    'test_contour_refund':'signer-agent/tests/test_contour_refund.py',
    'test_contour':'tests/zkspv/test_contour.py',
    'test_contour_worker':'signer-agent/tests/test_contour_worker.py',
    'test_contour_mint':'signer-agent/tests/test_contour_mint.py',
    'test_contour_withdrawal':'signer-agent/tests/test_contour_withdrawal.py',
    'test_contour_incident':'signer-agent/tests/test_contour_incident.py',
    'test_contour_epoch':'signer-agent/tests/test_contour_epoch.py',
}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'suite-contour-tests.xml')
    counts={k:sum(int(s.get(k,0)) for s in tree.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in FOCUSED.items():
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if not expected or {c.get('name','').split('[')[0] for c in cases}!=expected:raise ValueError('SUITE_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if counts['tests']<1700 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('SUITE_CHECKS_INCOMPLETE')
    previous=json.loads((OUT/'contour-epoch-report.json').read_text())
    paths=set(previous['source_sha256'])|set(source_paths())|set(FOCUSED.values())
    paths.update(['config/zkspv-contour-suite.example.json','config/zkspv-contour-fee.example.json',
        'docs/ZKSPV_CONTOUR.md','docs/CONFIGURATION.md','docs/README.md'])
    artifacts=[]
    for name in ('suite-contour-tests.xml','suite-contour-tests.log','suite-contour-command.json'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    report=dict(schema='satoshifi.contour-suite-implementation.v1',as_of='2026-11-02',
        status='MULTI_REQUEST_SUITE_MATRIX_COLLECTOR_IMPLEMENTED_ACTUAL_COMBINED_RUN_PENDING',code_implemented=True,
        multi_need_orchestrator_implemented=True,complete_driver_set_implemented=True,
        typed_scenario_matrix_implemented=True,combined_evidence_collector_implemented=True,
        complete_private_bindings_supplied=False,connected_capabilities=list(CAPABILITIES),
        scenario_catalog=CASES,scenario_catalog_sha256=digest(CASES),scenario_count=len(CASES),
        max_jobs=128,max_delegated_jobs_per_step=1,python_tests=counts,focused_tests=focused,
        implemented=[
            'Durable dependency DAG, fair restart cursor, parent commitments, inherited parent/child locks and original recovery engines',
            'Explicit once-only job input binding after verified dependencies; immutable worker/identity/source/input pins, no quote/consent/claim renewal',
            'Original initial Fresh/own heartbeat with staged no-cache proving, pre-sign publication latch, retained catch-up chunks and archive promotion',
            'Bounded consumer proof cycle until required depth/Fresh, retaining the initial preparation predecessor, every chunk, original envelopes and budgets',
            'Typed concrete original-SDK recipes for all 23 catalog cases, actual request/graph/version/reserve/generation joins and mintProofVersion guard',
            'Original external fee credit classification/admission, MP/S owner token returns and exact pre-increment nonce guarded BTC refund creation',
            'Named BTC refund quote/adoption, unchanged original principal, original owner per-version consent and V4 signing/broadcast/settlement',
            'Read-only evidence collection: full suite/job lineage, binary receipt/input/stage archives, exact original EVM envelopes/anchors and anchored process failures',
            'Private evidence bundles with stream hashes; TEST_ONLY commands and local evidence never grant production acceptance'],
        boundaries=[
            'Real subprocess/SQLite/locks/hash recovery and original SDK/ABI/EOA/budget/nonce/native Bitcoin codecs are exercised',
            'Core/EVM/provenance/threshold/prover verification effects in these tests are explicit fixtures; no actual combined monetary or proof run',
            'Concrete private deployment inputs, reviewed quotes/terms, external FROST authorizations and retention claims remain operator supplied',
            'Collector probes never run/recover, rebroadcast, pay, sign, infer owner approval or renew expiry; local journal evidence is not remote attestation',
            'Original generation, terminal, qualification, reserve, nonce, spending and owner-principal constraints remain active',
            'Original-graph continuity is a retained service review, not global on-chain key-family/status continuity or live asset migration'],
        deferred_existing_native_checks=previous['deferred_existing_native_checks'],
        deferred=['Actual long complete suite with real Core/FROST/Groth16/EVM, recovery and monetary receipts',
            'Production parameters, mainnet fork/halt coverage, availability/latency/TCO and independent qualification',
            'Global on-chain family/status continuity, live inventory/transition and specification 15.11'],
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_frost_authorization_performed=False,
        actual_value_transfer_performed=False,actual_bootstrap_deployment_performed=False,actual_live_retention_performed=False,
        combined_e2e_performed=False,combined_e2e_accepted=False,fresh_proving_verified=False,
        global_onchain_family_registry_implemented=False,cross_graph_asset_migration_supported=False,
        independent_review_performed=False,ready=False,production_ready=False,
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-suite-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
