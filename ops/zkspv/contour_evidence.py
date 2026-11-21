"""Controller/local handover evidence, separate from combined monetary acceptance."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'ops/artifacts/zkspv/worker-integration'


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(); p.add_argument('--input-directory', type=Path, required=True); a = p.parse_args()
    tree = ET.parse(a.input_directory / 'contour-tests.xml')
    suites = list(tree.getroot().iter('testsuite'))
    counts = {k: sum(int(s.get(k, 0)) for s in suites) for k in ('tests', 'failures', 'errors', 'skipped')}
    focused = {}
    for name, path in [('test_contour','tests/zkspv/test_contour.py'),
                       ('test_continuity_handover','signer-agent/tests/test_continuity_handover.py')]:
        expected = {node.name for node in ast.parse((ROOT/path).read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name.startswith('test_')}
        # pytest with signer-agent as root emits empty classnames for outside files.
        # Attribute those cases only through the exact source-bound test identities.
        cases = [c for c in tree.getroot().iter('testcase') if c.get('classname','').endswith(name)
                 or not c.get('classname') and c.get('name','').split('[')[0] in expected]
        if {c.get('name','').split('[')[0] for c in cases} != expected: raise ValueError('CONTOUR_CHECKS_INCOMPLETE')
        focused[name] = len(cases)
    if (counts['tests'] == 0 or focused['test_contour'] < 15 or focused['test_continuity_handover'] < 10
            or any(counts[k] for k in ('failures', 'errors', 'skipped'))):
        raise ValueError('CONTOUR_CHECKS_INCOMPLETE')
    artifacts = []
    for name in ('contour-tests.xml', 'contour-tests.log'):
        target = OUT / name; target.write_bytes((a.input_directory / name).read_bytes()); artifacts.append(target)
    # Earlier component snapshots remain historical; this new run pins its actual inputs.
    prior = json.loads((OUT / 'continuity-report.json').read_text())
    paths = set(prior['source_sha256'])
    paths.update(['ops/zkspv/contour.py', 'ops/zkspv/contour_driver.py', 'ops/zkspv/contour_evidence.py',
        'signer-agent/signer_agent/continuity_handover.py', 'signer-agent/signer_agent/continuity_handover_cli.py',
        'signer-agent/tests/test_continuity_handover.py', 'tests/zkspv/test_contour.py',
        'config/zkspv-contour.example.json', 'docs/ZKSPV_CONTOUR.md'])
    report = dict(schema='satoshifi.contour-local-handover.v1', as_of='2026-11-02',
        status='CONTROLLER_AND_LOCAL_HANDOVER_IMPLEMENTED_DRIVERS_PENDING', code_implemented=True,
        controller_code_implemented=True, local_retained_graph_handover_implemented=True,
        complete_driver_set_implemented=False, global_onchain_family_registry_implemented=False,
        cross_graph_asset_migration_supported=False, ready=False, production_ready=False,
        python_tests=counts, focused_tests=focused,
        implemented=['Private FULL/WAL stage journal, stable attempts and unique fresh probe challenges',
            'Source/executable/dependency/artifact fingerprints; held lock and bounded subprocess cleanup',
            'Interrupted monetary action cannot be implicitly retried; exact observation or explicit recovery driver required',
            'Explicit missing capability/driver inventory; no command success or stale evidence yields acceptance',
            'Canonical retained-source/separate-target plan and shared known-key/child catalog checks',
            'Both operator journals and optional independent signer journal retain issuance holds',
            'Fresh original-graph inventory/status and existing threshold/service/archive/funding review before new registration',
            'Read-only handover adapter connected to controller evidence contract'],
        boundaries=['Subprocess effects are explicit non-monetary fixtures; no real payment/proof is claimed',
            'Chain, completed DKG, provenance, inventory and retention qualification in handover tests are explicit fixtures',
            'Real native curve identities, SQLite/spent storage, locks and subprocess lifecycle are exercised',
            'Old assets/claims/keys/accounting stay in original graphs; no copied root or new mint authority',
            'Known-key catalog is local and cannot identify undisclosed tweaks or globally unknown deployments',
            'The controller trusts reviewed drivers; it is not a network sandbox or independent verifier'],
        fresh_proving_performed=False, actual_core_acceptance_performed=False,
        actual_value_transfer_performed=False, live_dkg_or_evm_quorum_performed=False,
        combined_e2e_performed=False, independent_review_performed=False,
        deferred=['Connect remaining actual Core/transport/payment/prover/state/consumer/withdrawal/recovery drivers',
            'Execute source-bound complete combined contour with real proof and signatures',
            'Global canonical family/status continuity and any separately qualified live transition protocol',
            'Verifier/purpose/profile qualification, complete live inventory and specification 15.11'],
        source_sha256={name: sha(ROOT / name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)): sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT / 'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT / 'contour-handover-report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(dict(status=report['status'], python_tests=counts, focused_tests=focused, ready=False)))


if __name__ == '__main__': main()
