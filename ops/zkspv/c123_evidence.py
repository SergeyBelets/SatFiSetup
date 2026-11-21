"""Record current C1/C2/C3 checks; preserve the historical 1776-test evidence."""
import argparse
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from .contour import ROOT,sha,digest
from .contour_plan import source_paths
from .contour_matrix import CASES,FLOW_CASES
from .contour_suite_evidence import FOCUSED

OUT=ROOT/'ops/artifacts/zkspv/worker-integration'
NEW_TESTS={
    'test_key_family_evm':'signer-agent/tests/test_key_family_evm.py',
    'test_family_registry_policy':'signer-agent/tests/test_family_registry_policy.py',
    'test_zk_observations':'signer-agent/tests/test_zk_observations.py',
    'test_contour_lifecycle_extensions':'signer-agent/tests/test_contour_lifecycle_extensions.py'}


def paths():
    result=set(source_paths())|set(FOCUSED.values())|set(NEW_TESTS.values())
    result.update(['ops/compile_continuity.cjs','ops/check_continuity_interfaces.cjs','ops/generate_abis.py',
        'ops/zkspv-required-scenarios.json','ops/zkspv-observation-policy.json','signer-agent/pyproject.toml',
        'web3/foundry.toml','web3/test/fixtures/FamilyRegistryHarness.sol','web3/test/BitcoinAddressRegistry.t.sol',
        'config/zkspv-observed-worker.example.json','config/zkspv-contour-suite.example.json'])
    return sorted(result)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-directory',type=Path,required=True);p.add_argument('--build-report',type=Path,required=True)
    p.add_argument('--interface-report',type=Path,required=True);p.add_argument('--sizes',type=Path,required=True)
    a=p.parse_args();command=json.loads((a.input_directory/'c123-command.json').read_text())
    before=json.loads((a.input_directory/'c123-source-before.json').read_text())
    if before!={name:sha(ROOT/name) for name in paths()}:raise ValueError('C123_EXECUTION_SOURCE_CHANGED')
    xml=ET.parse(a.input_directory/'c123-tests.xml')
    counts={k:sum(int(s.get(k,0)) for s in xml.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    if counts['tests']<2000 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('C123_CHECKS_INCOMPLETE')
    focused={}
    for name,path in {**FOCUSED,**NEW_TESTS}.items():
        expected={n.name for n in ast.parse((ROOT/path).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
        selected=[c for c in xml.getroot().iter('testcase') if c.get('classname','').endswith(name)]
        if {c.get('name','').split('[')[0] for c in selected}!=expected:raise ValueError('C123_FOCUSED_CHECKS_INCOMPLETE:'+name)
        focused[name]=len(selected)
    if command['env'].get('SATOHSI_REQUIRE_EVM')!='1':raise ValueError('C123_EVM_CHECK_REQUIRED')
    build=json.loads(a.build_report.read_text());interfaces=json.loads(a.interface_report.read_text())
    for name,h in build['source_sha256'].items():
        if sha(ROOT/'web3'/name)!=h:raise ValueError('C123_SOLIDITY_SOURCE_CHANGED')
    for name,h in interfaces['source_sha256'].items():
        if sha(ROOT/'web3'/name)!=h:raise ValueError('C123_TEST_INTERFACE_SOURCE_CHANGED')
    if interfaces['status']!='TYPE_AND_ABI_COMPILE_PASS' or interfaces['solidity_tests_executed'] is not False:raise ValueError('C123_INTERFACE_CHECKS')
    bytecodes={}
    for source,contracts in build['contracts'].items():
        if not source.startswith('contracts/src/'):continue
        for name,c in contracts.items():
            bytecodes[name]=dict(source=source,creation_sha256=hashlib.sha256(bytes.fromhex(c['evm']['bytecode']['object'])).hexdigest(),
                runtime_sha256=hashlib.sha256(bytes.fromhex(c['evm']['deployedBytecode']['object'])).hexdigest(),runtime_bytes=len(c['evm']['deployedBytecode']['object'])//2)
    compile_manifest=dict(compiler=build['compiler'],input_sha256=build['input_sha256'],settings=build['settings'],
        source_sha256=build['source_sha256'],contracts=bytecodes,build_result_sha256=sha(a.build_report),incremental=build.get('incremental'),
        project_runtime_and_initcode_limits_checked=True,solidity_tests_executed=False)
    artifacts=[]
    for name in ('c123-command.json','c123-source-before.json','c123-tests.xml','c123-tests.log'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    for name,value in [('c123-solidity-build.json',compile_manifest),('c123-solidity-interfaces.json',interfaces),('c123-contract-sizes.json',json.loads(a.sizes.read_text()))]:
        target=OUT/name;target.write_text(json.dumps(value,indent=2)+'\n');artifacts.append(target)
    old_path=OUT/'contour-suite-report.json';old=json.loads(old_path.read_text())
    report=dict(schema='satoshifi.c123-implementation-checks.v1',as_of='2026-11-02',
        status='CODE_IMPLEMENTED_SELECTED_LOCAL_OBSERVATION_POLICY_QUALIFIED_CONTOUR_PENDING',code_implemented=True,
        canonical_family_registry_implemented=True,global_onchain_family_registry_implemented=True,
        canonical_root_scope='One independently pinned canonical EVM root; all admitted registries/DKG deployments and known child aliases',
        cross_graph_asset_migration_supported=False,canonical_root_independent_qualification=False,
        processed_observation_history_implemented=True,recoverable_need_outbox_implemented=True,
        observation_policy=json.loads((ROOT/'ops/zkspv-observation-policy.json').read_text()),
        mainnet_guest_changed=False,unpublished_history_completeness_proven=False,global_onchain_halt_guaranteed=False,
        scenario_count=len(FLOW_CASES),workflow_recipe_count=len(FLOW_CASES),mandatory_observation_slots=len(CASES)-len(FLOW_CASES),
        matrix_entry_count=len(CASES),max_jobs=512,complete_private_bindings_supplied=False,qualified_scenario_execution_complete=False,
        addressed=['Canonical family/status/alias directory, original FROST enrollment/bind, independent SDK pins and bootstrap/recovery',
            'Durable Core tips, linked resumable branch packets, dual-journal history, sticky 100/101 boundary, fixed-deadline atomic outbox and own-funded maintenance',
            'Address request/ACK/registration/coverage with three recipient profiles; per-owner batch/consent, RBF old/new winners, safe owner exits and refund lifecycle',
            'Pinned 195-row assertion catalog, read-only original SDK/typed EVM probes, separate observational evidence and bounded suite input/lineage'],
        python_tests=counts,focused_tests=focused,actual_local_evm_unit_execution_performed=True,
        evm_boundary='Real compiled bytecode on isolated PyEVM with explicit UNIT DKG/approval fixtures; not real FROST/Core/prover acceptance',
        solidity_test_interfaces_checked=interfaces['test_source_count'],full_solidity_tests_executed=False,
        deferred_existing_native_checks=old['deferred_existing_native_checks'],
        historical_baseline=dict(path=str(old_path.relative_to(ROOT)),sha256=sha(old_path),python_tests=old['python_tests'],rewritten=False),
        test_xml='ops/artifacts/zkspv/worker-integration/c123-tests.xml',
        source_sha256=before,artifact_sha256={str(p.relative_to(ROOT)):sha(p) for p in artifacts},
        tool_versions={n:importlib.metadata.version(n) for n in ('web3','eth-tester','py-evm','trie','hexbytes','rlp','eth-typing','pytest','coincurve')},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'),
        fresh_proving_performed=False,actual_core_acceptance_performed=False,actual_frost_authorization_performed=False,
        actual_value_transfer_performed=False,combined_e2e_performed=False,combined_e2e_accepted=False,
        independent_review_performed=False,ready=False,production_ready=False)
    (OUT/'c123-implementation-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,workflow_recipes=len(FLOW_CASES),mandatory_observation_slots=195,production_ready=False)))


if __name__=='__main__':main()
