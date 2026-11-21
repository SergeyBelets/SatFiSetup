"""Concrete local test programs for every mandatory row; real contour is separate.

Each binding names existing executable fixture setup/actions/assertions, not a
JSON-success probe. A module can exercise several fragments of a requirement;
passing all its tests never certifies the whole specification row or production.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from .contour import ROOT, sha, digest
from .contour_checks import catalog, SPEC
from .contour_plan import source_paths

BINDINGS = ROOT / 'ops/zkspv-acceptance-bindings.json'
COUNTS = {'11.9':29, '11.10':44, '11.11':12, 'ADDR':62, 'SUB':48}
BOUNDARIES = {'MODEL','MODEL_AND_REAL_SQLITE','NATIVE_CRYPTO','NATIVE_CRYPTO_AND_JOURNAL',
    'NATIVE_WITH_RPC_FIXTURES','NATIVE_CRYPTO_WITH_CHAIN_FIXTURES','SDK_WITH_CHAIN_FIXTURES',
    'NATIVE_HPKE_WITH_CHAIN_FIXTURES','NATIVE_CODEC_WITH_CHAIN_FIXTURES','RPC_PROVENANCE_FIXTURES',
    'REAL_LOCAL_EVM_WITH_DKG_APPROVAL_FIXTURES','NATIVE_HPKE_CODEC_WITH_CHAIN_FIXTURES',
    'CONTRACT_WITH_EXPLICIT_FIXTURES','CONTRACT_WITH_EXPLICIT_QUALIFICATION_DKG_SPV_FIXTURES',
    'REAL_LOCAL_EVM_TEST_ASSETS_WITH_EXPLICIT_AUTH_SPV_FIXTURES','CONTRACT_WITH_EXPLICIT_STATE_AUTH_FIXTURES',
    'CONTRACT_WITH_EXPLICIT_SPV_AUTH_FIXTURES','CONTRACT_CODEC'}


def solidity_tests(text):
    # Preserve character offsets while excluding braces/keywords in comments or
    # literals. Interface getters such as testHelpersEnabled are not test cases.
    clean=re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/',
        lambda m:''.join('\n' if c=='\n' else ' ' for c in m[0]),text)
    for contract in re.finditer(r'\bcontract\s+(\w+)\s*([^{};]*)\{',clean):
        if not contract[1].endswith('Test') and not re.search(r'\bis\b.*\bTest\b',contract[2]):continue
        start=contract.end();end=start;depth=1
        while depth and end<len(clean):
            depth+=(clean[end]=='{')-(clean[end]=='}');end+=1
        if depth:raise ValueError('ACCEPTANCE_SOLIDITY_PARSE')
        for match in re.finditer(r'\bfunction\s+(test\w*)\s*\(',clean[start:end-1]):
            yield match[1],text[:start+match.start()].count('\n')+1


def modules_and_cases(value=None):
    c = json.loads(BINDINGS.read_text()) if value is None else value
    if set(c) != {'version','specification_sha256','scope','modules','groups'} or c['version'] != '1' or c['specification_sha256'] != SPEC:
        raise ValueError('ACCEPTANCE_BINDINGS_SCHEMA')
    if set(c['groups']) != set(COUNTS):raise ValueError('ACCEPTANCE_CASE_SET')
    modules={}
    for key,row in c['modules'].items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',key) or not isinstance(row,list) or len(row)!=3:raise ValueError('ACCEPTANCE_MODULE')
        runner,boundary,name=row
        if runner not in ('PYTEST','FORGE') or boundary not in BOUNDARIES:raise ValueError('ACCEPTANCE_MODULE')
        path=ROOT/name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.is_file() or not path.resolve().is_relative_to(ROOT):raise ValueError('ACCEPTANCE_MODULE_PATH')
        tests=[]
        if runner=='PYTEST':
            if not name.startswith(('signer-agent/tests/','tests/zkspv/')) or not name.endswith('.py'):raise ValueError('ACCEPTANCE_MODULE_PATH')
            for node in ast.parse(path.read_text()).body:
                if isinstance(node,ast.FunctionDef) and node.name.startswith('test_'):
                    tests.append(dict(selector=name+'::'+node.name,function=node.name,line=node.lineno,
                        fixture_arguments=[a.arg for a in node.args.args],
                        assertions=sum(isinstance(n,ast.Assert) for n in ast.walk(node))))
        else:
            if not name.startswith(('web3/test/','rust/zkspv/state-adapter/test/')) or not name.endswith('.sol'):raise ValueError('ACCEPTANCE_MODULE_PATH')
            for function,line in solidity_tests(path.read_text()):
                tests.append(dict(selector=name+'::'+function,function=function,line=line,
                    fixture_arguments=['Original Solidity setUp and inherited fixture graph'],assertions=None))
        if not tests:raise ValueError('ACCEPTANCE_MODULE_EMPTY')
        modules[key]=dict(runner=runner,boundary=boundary,path=name,source_sha256=sha(path),tests=tests)
    rows=catalog();cases={}
    for prefix,count in COUNTS.items():
        group=c['groups'][prefix]
        if not isinstance(group,list) or len(group)!=count:raise ValueError('ACCEPTANCE_CASE_SET')
        for index,entry in enumerate(group,1):
            identifier=prefix+('-' if prefix in ('ADDR','SUB') else '/')+str(index).zfill(2)
            keys=entry.split(',') if isinstance(entry,str) else []
            if not keys or len(keys)!=len(set(keys)) or any(k not in modules for k in keys):raise ValueError('ACCEPTANCE_CASE_BINDING')
            cases[identifier]=dict(**rows[identifier],modules=keys,
                local_program='Run every original setup/action/assertion in these explicitly selected test modules',
                real_contour_status='DEFERRED_BY_USER_FOR_DISK_CAPACITY',
                specification_row_fully_verified=False,production_ready=False)
    if set(cases)!=set(rows) or set(modules)!={k for row in cases.values() for k in row['modules']}:raise ValueError('ACCEPTANCE_CASE_SET')
    return modules,cases


def source_snapshot():
    names=set(source_paths())|{'ops/zkspv-acceptance-bindings.json','.gitignore','web3/foundry.toml',
        'ops/compile_continuity.cjs','ops/check_continuity_interfaces.cjs','ops/generate_abis.py',
        'ops/generate_fee_runtime_hashes.py','ops/check_contract_sizes.py','signer-agent/pyproject.toml'}
    for folder in ('signer-agent/tests','tests/zkspv','web3/test','rust/zkspv/state-adapter/test'):
        for p in (ROOT/folder).rglob('*'):
            if p.is_file() and p.suffix in ('.py','.sol','.json','.bin'):names.add(str(p.relative_to(ROOT)))
    names.update(str(p.relative_to(ROOT)) for p in (ROOT/'ops/artifacts/zkspv/historical-sources').iterdir() if p.is_file())
    return {p:sha(ROOT/p) for p in sorted(names)}


def prepare(output):
    modules,cases=modules_and_cases()
    plan=dict(schema='satoshifi.local-mandatory-case-programs.v1',specification_sha256=SPEC,
        bindings_sha256=sha(BINDINGS),modules=modules,cases=cases,source_sha256=source_snapshot(),
        real_contour_deferred=True,coverage_percentage=None,production_ready=False)
    output=Path(output)
    with output.open('x') as f:json.dump(plan,f,ensure_ascii=False,indent=2);f.write('\n')
    return plan


def python_outcomes(path):
    root=ET.parse(path).getroot();out={}
    for c in root.iter('testcase'):
        module=c.get('classname','').split('.')[-1];function=c.get('name','').split('[')[0]
        status='FAIL' if any(c.find(k) is not None for k in ('failure','error')) else 'SKIP' if c.find('skipped') is not None else 'PASS'
        out.setdefault((module,function),[]).append(status)
    if not out:raise ValueError('ACCEPTANCE_EMPTY_PYTHON_RESULTS')
    return out


def contract_outcomes(paths):
    out={}
    for path in paths:
        report=json.loads(Path(path).read_text())
        for name,suite in report.items():
            if not isinstance(suite,dict) or not isinstance(suite.get('test_results'),dict):continue
            filename=Path(name.split(':',1)[0]).name
            for method,result in suite['test_results'].items():
                function=method.split('(')[0]
                status='PASS' if result.get('status')=='Success' else 'SKIP' if result.get('status')=='Skipped' else 'FAIL'
                out.setdefault((filename,function),[]).append(status)
    if not out:raise ValueError('ACCEPTANCE_EMPTY_CONTRACT_RESULTS')
    return out


def collect(plan_path,xml_path,forge_paths,output):
    plan=json.loads(Path(plan_path).read_text());modules,cases=modules_and_cases()
    if (plan['modules']!=modules or plan['cases']!=cases or plan['bindings_sha256']!=sha(BINDINGS)
            or plan['source_sha256']!=source_snapshot()):raise ValueError('ACCEPTANCE_EXECUTION_SOURCE_CHANGED')
    py=python_outcomes(xml_path);evm=contract_outcomes(forge_paths);results={}
    for key,module in modules.items():
        outcomes=py if module['runner']=='PYTEST' else evm
        filename=Path(module['path']).stem if module['runner']=='PYTEST' else Path(module['path']).name
        tests={}
        for test in module['tests']:
            values=outcomes.get((filename,test['function']),[])
            tests[test['function']]=dict(status='PASS' if values and all(x=='PASS' for x in values) else 'FAIL' if 'FAIL' in values else 'SKIP' if 'SKIP' in values else 'MISSING',executions=len(values))
        results[key]=dict(path=module['path'],boundary=module['boundary'],tests=tests,
            status='PASS' if all(x['status']=='PASS' for x in tests.values()) else 'INCOMPLETE')
    joined={key:dict(row,status='LOCAL_CHECKS_PASS_REAL_CONTOUR_DEFERRED' if all(results[k]['status']=='PASS' for k in row['modules']) else 'LOCAL_CHECKS_INCOMPLETE') for key,row in cases.items()}
    passed=sum(c['status']=='LOCAL_CHECKS_PASS_REAL_CONTOUR_DEFERRED' for c in joined.values())
    report=dict(schema='satoshifi.local-mandatory-case-results.v1',specification_sha256=SPEC,
        plan_sha256=sha(Path(plan_path)),source_sha256=plan['source_sha256'],
        input_sha256={str(Path(p)):sha(Path(p)) for p in [xml_path,*forge_paths]},
        local_program_rows=len(cases),local_program_rows_passed=passed,local_modules=results,cases=joined,
        actual_real_contour_performed=False,qualified_scenario_execution_complete=False,
        complete_private_contour_bindings_supplied=False,independent_review_performed=False,
        specification_row_coverage_percentage=None,ready=False,production_ready=False)
    output=Path(output)
    with output.open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('prepare','collect'))
    p.add_argument('--output',type=Path,required=True);p.add_argument('--plan',type=Path)
    p.add_argument('--python-xml',type=Path);p.add_argument('--forge-json',type=Path,action='append',default=[])
    a=p.parse_args()
    if a.action=='prepare':v=prepare(a.output);result=dict(rows=len(v['cases']),modules=len(v['modules']))
    else:
        if not a.plan or not a.python_xml or not a.forge_json:p.error('collect requires plan, python-xml and forge-json')
        v=collect(a.plan,a.python_xml,a.forge_json,a.output);result=dict(rows=v['local_program_rows'],local_checks_passed=v['local_program_rows_passed'])
    print(json.dumps(dict(**result,real_contour_deferred=True,production_ready=False)))


if __name__=='__main__':main()
