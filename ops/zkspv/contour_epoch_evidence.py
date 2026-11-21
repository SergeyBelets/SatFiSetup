"""Source-bound old-epoch adapter checks; operational claims do not establish live availability."""
import argparse
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .contour import CAPABILITIES
from .contour_worker_evidence import ROOT,OUT,sha


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'epoch-contour-tests.xml')
    counts={k:sum(int(s.get(k,0)) for s in tree.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in [('test_contour_epoch','signer-agent/tests/test_contour_epoch.py'),
                      ('test_epoch_service','signer-agent/tests/test_epoch_service.py'),
                      ('test_contour_incident','signer-agent/tests/test_contour_incident.py'),
                      ('test_contour_withdrawal','signer-agent/tests/test_contour_withdrawal.py'),
                      ('test_contour_mint','signer-agent/tests/test_contour_mint.py'),
                      ('test_contour_setup','signer-agent/tests/test_contour_setup.py'),
                      ('test_contour_service','signer-agent/tests/test_contour_service.py'),
                      ('test_contour_worker','signer-agent/tests/test_contour_worker.py'),('test_contour','tests/zkspv/test_contour.py')]:
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if {c.get('name','').split('[')[0] for c in cases}!=expected:raise ValueError('CONTOUR_EPOCH_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if focused['test_contour_epoch']<45 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('CONTOUR_EPOCH_CHECKS_INCOMPLETE')
    artifacts=[]
    for name in ('epoch-contour-tests.xml','epoch-contour-tests.log','epoch-contour-command.json'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    previous=json.loads((OUT/'contour-incident-report.json').read_text());paths=set(previous['source_sha256'])
    paths.update(['ops/zkspv/contour_epoch.py','ops/zkspv/contour_epoch_evidence.py',
        'signer-agent/tests/test_contour_epoch.py','signer-agent/tests/test_epoch_service.py',
        'signer-agent/signer_agent/epoch_service.py','signer-agent/signer_agent/epoch_retention.py','signer-agent/signer_agent/epoch_cli.py',
        'signer-agent/signer_agent/address_watch.py','signer-agent/signer_agent/withdrawal_fee_watch.py',
        'signer-agent/signer_agent/withdrawal_fee_funding.py','config/zkspv-contour-epoch.example.json',
        'config/bitcoin-epochs.example.json','docs/BITCOIN_EPOCH_SERVICE.md','ops/zkspv/report.py'])
    report=dict(schema='satoshifi.contour-old-epoch.v1',as_of='2026-11-02',
        status='OLD_EPOCH_ADAPTER_IMPLEMENTED_ALL_ADAPTER_TYPES_AVAILABLE_COMBINED_RUN_PENDING',code_implemented=True,
        connected_capabilities=list(CAPABILITIES),all_capability_adapter_types_implemented=True,
        complete_driver_set_implemented=False,multi_need_orchestrator_implemented=False,
        global_onchain_family_registry_implemented=False,cross_graph_asset_migration_supported=False,
        ready=False,production_ready=False,python_tests=counts,focused_tests=focused,
        max_sweep_epochs_per_action=8,evm_operations_per_action=0,bitcoin_broadcasts_per_action=0,
        implicit_spv_scheduling=False,retention_claims_automatically_created=False,implicit_challenge_or_expiry_renewal=False,
        deferred_existing_native_checks=previous['deferred_existing_native_checks'],
        implemented=['Standalone original Fleet adapter, original identities/journals/round-robin cursor and bounded address/fee/refund observation phases',
            'Private fleet/address/fee/key-file/challenge/attestation pins before RPC; exact challenge must already exist in original identity lane',
            'Distinct input-bound contour enrollment for each explicitly reviewed retention dataset; original journals are never reset',
            'Selected allowlisted worker graph state/adapter origins authenticated without Fresh; old mainnet epochs keep their independent pinned policies',
            'Original complete BTC_SPEND/board/transport pair, generation/terminal/qualification/watch/archive/reserve checks before positive retention',
            'Original distinct retained EOA attestations, canonical anchors, fixed expiry, departing-member exclusion and aggregate shared-payer funding',
            'Unknown signed EVM obligations require exact source receipt; this observer cannot resend or allocate a transaction',
            'Native late-deposit/reorg catchup preserves original observations and orphan history; failed phase cannot grant positive completion',
            'Fresh full-fleet review on every positive probe; frozen historical evidence never grants future service availability or issuance authority',
            'All twenty capability adapter types exist across supported partial configurations; complete multi-need bindings/collector remain pending'],
        boundaries=['Original Fleet/retention policy, real EOA claim signatures, native block/watch/reorg codec and private SQLite journals are exercised',
            'Canonical graph/binding origins, qualification, reserve and Core/EVM providers are explicit fixtures; no actual PoW/node/threshold/monetary operation is established',
            'Actual CLI/controller entrypoints execute in-process with real input/challenge/response/artifact binding',
            'One original bounded Fleet sweep per action, using schedule_spv=False; original supervised Fleet remains responsible for continuous service',
            'A positive pass uses externally supplied original challenge/claims and does not create a BTC nonce, private FROST share or retention claim',
            'Expired/changed inputs require explicit new review/plan; no automatic key rotation, asset migration, pause clearing, funding or service payment',
            'Configured legacy/one-pool/profile constraints and actual QUALIFIED/FEE_QUALIFIED remain unchanged',
            'All adapter types available does not imply one complete driver binding set, multi-need collector or combined E2E acceptance'],
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_frost_authorization_performed=False,
        actual_value_transfer_performed=False,actual_bootstrap_deployment_performed=False,actual_live_retention_performed=False,
        combined_e2e_performed=False,independent_review_performed=False,
        deferred=['Multi-need orchestrator and source-bound acceptance collector/full scenario bindings, including initial heartbeat/setup-to-withdrawal coordination',
            'Actual long proving/monetary/incident/old-epoch operation, availability/latency/TCO and independent qualification',
            'Global on-chain family/status continuity, live inventory/migration qualification and specification 15.11'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-epoch-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
