"""Source-bound reviewed-version withdrawal/reconciliation checks, without live acceptance."""
import argparse
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .contour import CAPABILITIES
from .contour_worker_evidence import ROOT,OUT,sha


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'withdrawal-contour-tests.xml')
    counts={k:sum(int(s.get(k,0)) for s in tree.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in [('test_contour_withdrawal','signer-agent/tests/test_contour_withdrawal.py'),
                      ('test_withdrawal_user','signer-agent/tests/test_withdrawal_user.py'),
                      ('test_contour_mint','signer-agent/tests/test_contour_mint.py'),
                      ('test_contour_setup','signer-agent/tests/test_contour_setup.py'),
                      ('test_contour_service','signer-agent/tests/test_contour_service.py'),
                      ('test_contour_worker','signer-agent/tests/test_contour_worker.py'),('test_contour','tests/zkspv/test_contour.py')]:
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if {c.get('name','').split('[')[0] for c in cases}!=expected:raise ValueError('CONTOUR_WITHDRAWAL_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if focused['test_contour_withdrawal']<79 or any(counts[k] for k in ('failures','errors','skipped')):
        raise ValueError('CONTOUR_WITHDRAWAL_CHECKS_INCOMPLETE')
    artifacts=[]
    for name in ('withdrawal-contour-tests.xml','withdrawal-contour-tests.log','withdrawal-contour-command.json'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    previous=json.loads((OUT/'contour-mint-report.json').read_text());paths=set(previous['source_sha256'])
    paths.update(['ops/zkspv/contour_calls.py','ops/zkspv/contour_withdrawal.py','ops/zkspv/contour_reconcile.py',
        'ops/zkspv/contour_withdrawal_evidence.py','signer-agent/tests/test_contour_withdrawal.py',
        'signer-agent/signer_agent/btc_fee_client.py','signer-agent/signer_agent/btc_fee_policy.py',
        'signer-agent/signer_agent/btc_fee_reserve.py','signer-agent/signer_agent/withdrawal_user.py',
        'signer-agent/signer_agent/withdrawal_fee_client.py','signer-agent/signer_agent/withdrawal_fee_policy.py',
        'signer-agent/signer_agent/withdrawal_fee.py','signer-agent/signer_agent/withdrawal_fee_transport.py',
        'signer-agent/signer_agent/bitcoin_worker.py','signer-agent/tests/test_withdrawal_user.py',
        'config/zkspv-contour-withdrawal.example.json','config/zkspv-contour-reconciliation.example.json',
        'docs/ZKSPV_CONTOUR.md','docs/API.md','docs/CONFIGURATION.md','docs/README.md','docs/WITHDRAWAL_FEE_SUBSIDY.md'])
    report=dict(schema='satoshifi.contour-withdrawal-reconciliation.v1',as_of='2026-11-02',
        status='REVIEWED_WITHDRAWAL_RECONCILIATION_ADAPTERS_IMPLEMENTED_COMBINED_RUN_PENDING',code_implemented=True,
        connected_capabilities=[c for c in CAPABILITIES if c not in ('incident_recovery','old_epoch_service')],
        complete_driver_set_implemented=False,global_onchain_family_registry_implemented=False,cross_graph_asset_migration_supported=False,
        ready=False,production_ready=False,python_tests=counts,focused_tests=focused,withdrawal_fee_modes=['BTC','MP','S'],
        max_evm_operations_per_action=1,max_initial_bitcoin_broadcasts_per_action=1,
        frost_approval_automatically_created=False,implicit_rbf_or_session_retry=False,
        deferred_existing_native_checks=previous['deferred_existing_native_checks'],
        implemented=['One accepted owner quote/request and one explicitly accepted initial V4 terms hash; immutable native withdrawal need',
            'Separate original BTC_SPEND and enrolled EVM_AUTH bundles, DKG/wallet binding and independent owner/operator identities',
            'Original owner/fee SDK preparation, exact version consent, reserve/bind/public plan and external Bitcoin FROST wait',
            'Original EVM nonce lanes, pre-sign typed latches, original fee budgets plus bounded Bitcoin SDK caller budget',
            'Exact original signed EVM recovery/finality checks before new SDK guards; unsigned ambiguity holds without another nonce',
            'Original BitcoinWorker raw-before-RPC storage and exact-byte recovery before new preparation checks; no new signature on lost response',
            'Selected original BTC/MP/S settlement checks exact paid request/txid, closed budget and original receipt',
            'Original full inventory reconciliation, immutable observation/request, external typed FROST authorization and exact publication receipt',
            'Critical scan persists incident pause in original Bitcoin state; negative/unknown reports do not satisfy stage or clear pause',
            'Offline source/config-pinned builder with explicit withdrawal preparation and partial existing-family settlement mode'],
        boundaries=['Original ABI encoding, EOA signatures, global nonce/budget/SQLite recovery and native Bitcoin parsing are exercised',
            'Withdrawal preparation/settlement and graph/Core/FROST/EVM monetary effects are explicit fixtures; actual token/principal/refund execution is not qualified',
            'Reconciliation uses original Inspector/full scan and typed Driver/policies with fixture Core, graph origins, FROST outcomes and EVM effects',
            'Reconciliation CLI/controller chain is dispatched in-process with real response binding, private evidence and original report operation journals',
            'One already prepared initial V4 version/request per pipeline; original request/consent, UTXO selection, initial Fresh/heartbeat and healthy report prerequisites remain',
            'BTC fee-purpose funding/registration and separate BTC refund remain explicit original workflows; no donor input selection or owner consent is inferred',
            'No private FROST nonce/share/finalization, session retry, deadline/report renewal, RBF version or incident-pause clearing is automated',
            'Actual QUALIFIED/FEE_QUALIFIED, maturity, principal and generation guards remain in force; all prior source snapshots remain historical'],
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_frost_authorization_performed=False,
        actual_value_transfer_performed=False,actual_bootstrap_deployment_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        deferred=['Incident/recovery and old-epoch service adapters',
            'Source-bound multi-need acceptance collector, initial heartbeat/setup-to-withdrawal coordination and actual long proving/monetary run',
            'Global on-chain family/status continuity, live inventory/independent qualification and specification 15.11'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-withdrawal-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
