"""Source-bound setup adapter regression; real long monetary/proving acceptance stays pending."""
import argparse
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .contour_worker_evidence import ROOT,OUT,sha


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'setup-contour-tests.xml')
    counts={k:sum(int(s.get(k,0)) for s in tree.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in [('test_contour_setup','signer-agent/tests/test_contour_setup.py'),
                      ('test_contour_service','signer-agent/tests/test_contour_service.py'),
                      ('test_contour_worker','signer-agent/tests/test_contour_worker.py'),
                      ('test_contour','tests/zkspv/test_contour.py'),
                      ('test_continuity_handover','signer-agent/tests/test_continuity_handover.py')]:
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body
                  if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if {c.get('name','').split('[')[0] for c in cases}!=expected:raise ValueError('CONTOUR_SETUP_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if focused['test_contour_setup']<30 or any(counts[k] for k in ('failures','errors','skipped')):
        raise ValueError('CONTOUR_SETUP_CHECKS_INCOMPLETE')
    artifacts=[]
    for name in ('setup-contour-tests.xml','setup-contour-tests.log','setup-contour-command.json'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    previous=json.loads((OUT/'contour-service-report.json').read_text())
    paths=set(previous['source_sha256'])
    paths.update(['ops/zkspv/contour_bootstrap.py','ops/zkspv/contour_funding.py','ops/zkspv/contour_setup_evidence.py',
        'signer-agent/tests/test_contour_setup.py','config/zkspv-contour-bootstrap.example.json',
        'config/zkspv-contour-funding.example.json','config/zkspv-contour-smp1-funding.example.json',
        'signer-agent/signer_agent/bootstrap.py','signer-agent/signer_agent/bootstrap_parallel.py',
        'signer-agent/signer_agent/bootstrap_create.py','signer-agent/signer_agent/bootstrap_plan.py',
        'signer-agent/signer_agent/smp1_deposit.py','signer-agent/signer_agent/bitcoin_proofs.py',
        'signer-agent/signer_agent/bitcoin_rpc.py','signer-agent/signer_agent/bitcoin_network.py',
        'signer-agent/tests/test_onboarding.py','signer-agent/tests/test_onboarding_mainnet.py',
        'signer-agent/tests/test_onboarding_policy_v3.py','signer-agent/tests/test_transaction_wait.py','docs/BOOTSTRAP.md'])
    report=dict(schema='satoshifi.contour-setup-adapters.v1',as_of='2026-11-02',
        status='SETUP_SERVICE_WORKER_PIPELINE_IMPLEMENTED_COMBINED_RUN_PENDING',code_implemented=True,
        connected_capabilities=['bootstrap','bitcoin_funding','consumer_request','encrypted_transport','admission_payment',
            'proof_preparation','fresh_proof','state_update','service_status','continuity'],
        complete_driver_set_implemented=False,global_onchain_family_registry_implemented=False,
        cross_graph_asset_migration_supported=False,ready=False,production_ready=False,
        python_tests=counts,focused_tests=focused,max_new_bootstrap_units_per_action=1,max_bootstrap_transactions_per_unit=8,
        funding_modes=['OBSERVE','SMP1_QUOTE'],bootstrap_readiness='GRAPH_CONFIGURED_FRESHNESS_PENDING',
        deferred_existing_native_checks=previous['deferred_existing_native_checks'],
        implemented=[
            'Optional offline plan builder setup commands pin generated mainnet ZK profile, worker/request and funding/deposit configs before first RPC/action',
            'Original bootstrap sequential/parallel/atomic CREATE units, receipt/read finality barriers, DKG/approval waits and identity nonce journal retained',
            'One new original unit per action with immutable pre-sign enrollment; strict unsigned/partial parallel recovery holds and atomic signed-group restoration',
            'Private original-deployment gas/per-transaction/total cost reservations persist across controller run changes and stale readers reload under setup lock',
            'Expected state/consumer CREATE addresses checked before their signatures and matched against completed original manifest',
            'Explicit mainnet ZK structural binding/Core checks with deferred freshness markers; normal bootstrap live freshness behavior preserved',
            'Exact mature Bitcoin funding observed through original generation/intent/consumer guards, native parser and independently derived Merkle path',
            'Actual-height maturity: ordinary at least six, coinbase mint 100, fee-input coinbase at least 101; no reliance on RPC confirmation claims',
            'Original header/raw/output funding history rechecked after mint/spend; lost/orphaned/re-mined completion remains a contour hold',
            'Original SMP1 accepted quote, wallet fee/non-custody inputs/locks and native PSBT signer/broadcast recovery; no new quote or ambiguous re-sign',
            'Controller preserves an explicit action recovery hold when its fresh read-only probe can only observe ABSENT',
            'Registered address/fee-purpose external funding uses read-only observation; original qualification/generation policies remain unchanged'],
        boundaries=[
            'Real EOA envelopes, shared identity and private SQLite journals, original bootstrap engines, native PSBT/funding codecs and Merkle inclusion code are exercised',
            'Graph origins/constructor execution, synthetic Bitcoin headers, wallet signatures and EVM/Core execution are explicit fixtures in setup checks',
            'The actual setup CLI entry points are dispatched in-process by a two-stage controller test; it is not a live combined monetary run',
            'New SMP1 signing/sending retains existing fresh-state guards and can require initial independent worker/heartbeat progress',
            'Expected deployment addresses must already be reviewed/pinned; no automatic rewrite of worker/transport configs, participant DKG or transport enrollment is claimed',
            'Historical service/worker reports retain their original source hashes; this selected regression is not the full native/guest/E2E suite',
            'No new proof, node, deployment, real value transfer, threshold change, principal-accounting relaxation or production qualification'],
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_value_transfer_performed=False,
        actual_bootstrap_deployment_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        deferred=['Membership materialization and SMP1/address mint adapters',
            'BTC/MP/S withdrawal, reconciliation, incident/recovery and old-epoch service adapters',
            'Complete source-bound acceptance collector and actual long combined proving/monetary run',
            'Global on-chain family/status continuity, live inventory/independent qualification and specification 15.11'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-setup-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
