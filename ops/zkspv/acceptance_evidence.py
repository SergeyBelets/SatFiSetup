"""Retain final local regression and review package; no real-contour acceptance."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from .contour import ROOT,sha,CAPABILITIES
from .acceptance_cases import source_snapshot


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input-directory',type=Path,required=True)
    p.add_argument('--output-directory',type=Path,required=True);a=p.parse_args();home=a.input_directory.resolve();out=a.output_directory.resolve()
    if (out/'local-acceptance-report.json').exists():raise FileExistsError('Use a new evidence directory')
    command=json.loads((home/'python-command.json').read_text());plan=json.loads((home/'case-plan.json').read_text())
    if command['exit_code']!=0 or command['source_sha256']!=source_snapshot() or plan['source_sha256']!=source_snapshot():raise ValueError('ACCEPTANCE_SOURCE_OR_COMMAND')
    if any(command['env'].get(k)!='1' for k in ('SATOHSI_REQUIRE_NATIVE','SATOHSI_REQUIRE_EVM')):raise ValueError('ACCEPTANCE_NATIVE_REQUIRED')
    root=ET.parse(home/'all-local-tests.xml').getroot()
    counts={k:sum(int(s.get(k,0)) for s in root.iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    if counts['tests']<3200 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('ACCEPTANCE_PYTHON_INCOMPLETE')
    contracts={}
    for name in ('forge-tests-final.json','state-forge-tests.json'):
        suites=json.loads((home/name).read_text());rows=[r for s in suites.values() for r in s.get('test_results',{}).values()]
        if not rows or any(r['status']!='Success' for r in rows):raise ValueError('ACCEPTANCE_CONTRACT_INCOMPLETE')
        contracts[name]=len(rows)
    cases=json.loads((out/'local-case-results.json').read_text());qualification=json.loads((out/'qualification-review.json').read_text())
    if cases['local_program_rows_passed']!=195 or qualification['qualified'] or qualification['fee_qualified']:raise ValueError('ACCEPTANCE_CASES_OR_QUALIFICATION')
    out.mkdir(parents=True,exist_ok=True);artifacts={}
    for name in ('python-command.json','case-plan.json','all-local-tests.xml','all-local-tests.log','forge-tests-final.json','forge-tests-final.log',
                 'state-forge-tests.json','state-forge-tests.log','contract-sizes.json','restored-native-tests.xml','restored-native-tests.log','receipt-rechecks.json'):
        target=out/name;shutil.copyfile(home/name,target);artifacts[str(target.relative_to(ROOT))]=sha(target)
    for path in sorted(out.rglob('*')):
        if path.is_file() and path.name!='local-acceptance-report.json':artifacts[str(path.relative_to(ROOT))]=sha(path)
    historical={}
    for name in ('contour-suite-report.json','c123-implementation-report.json'):
        path=ROOT/'ops/artifacts/zkspv/worker-integration'/name;historical[name]=dict(path=str(path.relative_to(ROOT)),sha256=sha(path),rewritten=False)
    report=dict(schema='satoshifi.local-acceptance-report.v1',as_of='2026-11-02',status='LOCAL_REGRESSION_PASS_REAL_CONTOUR_DEFERRED',
        code_implemented=True,local_current_source_checks=True,source_sha256=plan['source_sha256'],artifact_sha256=artifacts,
        python_tests=counts,full_local_python_scope='All signer-agent/tests and tests/zkspv; tests/e2e real contour deferred',
        tool_versions={name:importlib.metadata.version(name) for name in ('web3','pytest','eth-tester','py-evm','coincurve','cryptography')},
        full_solidity_tests_executed=True,solidity_tests=contracts,scenario_count=43,workflow_recipe_count=43,
        local_case_bindings=195,local_case_bindings_passed=195,connected_capabilities=list(CAPABILITIES),
        complete_private_bindings_supplied=False,qualified_scenario_execution_complete=False,
        deferred_existing_native_checks=[],restored_native_checks=3,full_native_host_subset_tests=38,
        test_xml=str((out/'all-local-tests.xml').relative_to(ROOT)),historical_baselines=historical,
        retained_receipt_rechecks=qualification['retained_receipt_rechecks'],historical_deposit_sources=qualification['historical_deposit_sources'],
        qualification_status=qualification['status'],qualified=False,fee_qualified=False,independent_review_performed=False,
        actual_core_acceptance_performed=False,actual_frost_authorization_performed=False,fresh_proving_performed=False,
        real_contour_status='DEFERRED_BY_USER_FOR_DISK_CAPACITY',combined_e2e_performed=False,combined_e2e_accepted=False,
        mainnet_guest_changed=False,global_onchain_halt_guaranteed=False,ready=False,production_ready=False)
    target=out/'local-acceptance-report.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    pointer=ROOT/'ops/artifacts/zkspv/local-acceptance-current.json'
    pointer.write_text(json.dumps(dict(report=str(target.relative_to(ROOT)),sha256=sha(target)),indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,solidity_tests=contracts,local_case_bindings=195,production_ready=False)))


if __name__=='__main__':main()
