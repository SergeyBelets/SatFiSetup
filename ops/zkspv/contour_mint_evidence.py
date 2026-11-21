"""Retain source-bound membership/mint checks without claiming live monetary acceptance."""
import argparse
import ast
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .contour_worker_evidence import ROOT,OUT,sha


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    tree=ET.parse(a.input_directory/'mint-contour-tests.xml')
    counts={k:sum(int(s.get(k,0)) for s in tree.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    focused={}
    for name,path in [('test_contour_mint','signer-agent/tests/test_contour_mint.py'),
                      ('test_contour_setup','signer-agent/tests/test_contour_setup.py'),
                      ('test_contour_service','signer-agent/tests/test_contour_service.py'),
                      ('test_contour_worker','signer-agent/tests/test_contour_worker.py'),('test_contour','tests/zkspv/test_contour.py')]:
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        cases=[c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if {c.get('name','').split('[')[0] for c in cases}!=expected:raise ValueError('CONTOUR_MINT_CHECKS_INCOMPLETE')
        focused[name]=len(cases)
    if focused['test_contour_mint']<41 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('CONTOUR_MINT_CHECKS_INCOMPLETE')
    artifacts=[]
    for name in ('mint-contour-tests.xml','mint-contour-tests.log','mint-contour-command.json'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    previous=json.loads((OUT/'contour-setup-report.json').read_text());paths=set(previous['source_sha256'])
    paths.update(['ops/zkspv/contour_member.py','ops/zkspv/contour_mint.py','ops/zkspv/contour_tx.py','ops/zkspv/contour_mint_evidence.py',
        'signer-agent/signer_agent/custody_mint.py','signer-agent/signer_agent/bitcoin_onchain.py','signer-agent/signer_agent/bitcoin_funding.py',
        'signer-agent/signer_agent/bitcoin_onchain_payment.py','signer-agent/signer_agent/mint_admission.py',
        'signer-agent/signer_agent/mint_admission_client.py','signer-agent/signer_agent/address_mint.py',
        'signer-agent/tests/test_contour_mint.py','signer-agent/tests/test_bitcoin_funding.py',
        'config/zkspv-contour-mint.example.json','config/zkspv-contour-address-mint.example.json'])
    report=dict(schema='satoshifi.contour-membership-mint.v1',as_of='2026-11-02',
        status='MEMBERSHIP_MINT_ADAPTERS_IMPLEMENTED_COMBINED_RUN_PENDING',code_implemented=True,
        connected_capabilities=[*previous['connected_capabilities'][:8],'membership','service_status','smp1_mint','address_mint','continuity'],
        complete_driver_set_implemented=False,global_onchain_family_registry_implemented=False,cross_graph_asset_migration_supported=False,
        ready=False,production_ready=False,python_tests=counts,focused_tests=focused,mint_profiles=['SMP1_V1','SMP1_CUSTODY_V2','ADDRESS_V2'],
        max_mint_evm_operations_per_action=1,frost_approval_automatically_created=False,
        deferred_existing_native_checks=previous['deferred_existing_native_checks'],
        implemented=['Current-root native membership from exact accepted archive, independent adapter simulation and original worker publication budgets/nonces',
            'Accepted native archive retention without another proving/publication action; canonical cache adoption and exact HeaderMaterialized receipt/event',
            'Unsigned/root-change holds, exact signed envelope recovery and canonical finality/reorg checks; no second service payment for materialization',
            'SMP1 original accepted quote and recipient review; V1 and V2 custody-generation admission/execution with fixed economics/deadline',
            'Original registered-address intent/recipient/generation/qualification and admission/FROST trust/funding clients retained',
            'One original EVM operation per action, with pre-sign enrollment in independent caller budget journal and exact original transaction/identity nonce recovery',
            'Signed recovery precedes new Core/generation/funding prerequisites; lost receipt cannot be replaced by completion marker and another valid signed call is rejected',
            'Original participant authorization is awaited; no automatic FROST nonce/share/finalization, session retry or deadline extension',
            'Source-pinned offline builder, explicit membership capability and selected mint, with partial consumer-only existing-state mode'],
        boundaries=['Real original SDK/admission clients, ABI encoding, EOA envelopes, private SQLite/nonce journals and existing native codecs are exercised',
            'Graph origins, funding/header proofs, current recipient policy, Core, FROST outcomes and EVM effects are explicit fixtures in new mint/member tests',
            'The actual membership/mint CLI chain is dispatched in-process; synthetic membership verification is not an actual new mainnet proof or EVM transfer',
            'Builder handles one need and one selected mint per pipeline; complete multi-need/monetary acceptance collector remains pending',
            'Existing derivation QUALIFIED/FEE_QUALIFIED flags, principal/maturity/owner consent and threshold are not relaxed',
            'Historical reports retain their original source snapshots; selected Python regression does not replace native host/guest or all E2E'],
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_frost_authorization_performed=False,
        actual_value_transfer_performed=False,actual_bootstrap_deployment_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        deferred=['BTC/MP/S withdrawal, reconciliation, incident/recovery and old-epoch service adapters',
            'Complete source-bound multi-need acceptance collector and actual long proving/monetary run',
            'Global on-chain family/status continuity, live inventory/independent qualification and specification 15.11'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'contour-mint-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,focused_tests=focused,ready=False)))


if __name__=='__main__':main()
