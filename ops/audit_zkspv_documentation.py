"""Reproduce the documentation audit; does not run or requalify protocol tests."""
import argparse
import ast
import csv
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'ops/artifacts/zkspv'
CLAUSES=('11.9','11.10','11.11','11.12','11.13','11.14','11.15')
DOCS=('docs/ZKSPV_FUNCTIONAL_COVERAGE.md','docs/ZK_SPV_ARCHITECTURE.md','docs/ZKSPV_CONTOUR.md',
    'docs/README.md','docs/ARCHITECTURE.md','docs/ZK_SPV.md','docs/ZK_SPV_MIGRATION.md','docs/ZK_SPV_WORKER.md',
    'docs/BITCOIN_KEY_FAMILY.md','docs/ZKSPV_OBSERVATION_HISTORY.md','docs/ZKSPV_SCENARIO_BINDINGS.md')


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def scenarios(text):
    clause=None;rows=[];counts=Counter()
    for line in text.splitlines():
        if line.startswith('## '):clause=None
        match=re.match(r'(\d+\.\d+)\. ',line)
        if match:clause=match[1]
        if clause not in CLAUSES or not line.startswith('|'):continue
        cells=[c.strip() for c in line.strip('|').split('|')]
        if not cells[0] or cells[0].startswith('-') or cells[0] in ('Сценарий','Код','ID','Проверка','Случай'):continue
        counts[clause]+=1
        named=re.match(r'^((?:ADDR|SUB)-\d+)$',cells[0])
        identifier=named[1] if named else clause+'/'+str(counts[clause]).zfill(2)
        rows.append(dict(id=identifier,clause=clause,title=cells[0] if not named else cells[1],
            requirement_cells=cells,execution_status='NOT_JOINED_TO_UNIFIED_QUALIFIED_EVIDENCE',
            interpretation='Existing component tests may exercise this requirement; no complete per-case qualified execution is inferred.'))
    if len({r['id'] for r in rows})!=len(rows):raise ValueError('DUPLICATE_SCENARIO')
    return rows,dict(counts)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--diagrams',type=Path,required=True)
    parser.add_argument('--baseline',type=Path,default=BASE/'worker-integration/contour-suite-report.json');a=parser.parse_args()
    plan_path=ROOT/'ops/zkspv-functional-coverage.json';plan=json.loads(plan_path.read_text())
    spec=a.spec.read_text();index=json.loads((BASE/'requirements-index.json').read_text())
    if sha(a.spec)!=index['specification_sha256']:raise ValueError('SPECIFICATION_SNAPSHOT_CHANGED')
    requirement_map={clause:[area['id'] for area in plan['areas'] if any(clause==prefix or clause.startswith(prefix+'.')
        for prefix in area['requirements'])] for clause in index['numbered_clauses']}
    if any(not areas for areas in requirement_map.values()):raise ValueError('REQUIREMENT_AREA_MISSING')
    rows,counts=scenarios(spec)
    if counts!={'11.9':29,'11.10':44,'11.11':12,'11.12':28,'11.13':34,'11.14':32,'11.15':16}:raise ValueError('SCENARIO_TABLES_CHANGED')
    baseline_path=a.baseline.resolve();baseline=json.loads(baseline_path.read_text())
    changed=[p for p,h in baseline['source_sha256'].items() if sha(ROOT/p)!=h]
    executable_changed=[p for p in changed if not p.startswith('docs/')]
    if executable_changed:raise ValueError('RECORDED_EXECUTION_SOURCE_CHANGED:'+','.join(executable_changed))
    for path,h in baseline['artifact_sha256'].items():
        if sha(ROOT/path)!=h:raise ValueError('BASELINE_EVIDENCE_CHANGED')
    xml=ET.parse(ROOT/baseline.get('test_xml','ops/artifacts/zkspv/worker-integration/suite-contour-tests.xml'))
    tests={k:sum(int(s.get(k,0)) for s in xml.getroot().iter('testsuite')) for k in ('tests','failures','errors','skipped')}
    if tests!=baseline['python_tests']:raise ValueError('BASELINE_TEST_COUNTS')
    catalog=json.loads((ROOT/'ops/zkspv-required-scenarios.json').read_text())
    catalog_rows=catalog['scenarios']
    if {(r['id'],r['clause'],r['title']) for r in catalog_rows}!={(r['id'],r['clause'],r['title']) for r in rows}:raise ValueError('SCENARIO_CATALOG_CHANGED')
    inputs=set(DOCS)|{'ops/audit_zkspv_documentation.py','ops/zkspv-functional-coverage.json',
        'ops/zkspv-required-scenarios.json','ops/zkspv-observation-policy.json',
        'ops/render_docs_diagrams.cjs','ops/zkspv/contour_matrix.py'}
    for area in plan['areas']:
        for path in area['sources']:
            if not (ROOT/path).is_file():raise ValueError('MISSING_AREA_SOURCE:'+path)
            if path not in ('ops/artifacts/zkspv/decision.json','ops/artifacts/zkspv/regression-report.json'):inputs.add(path)
    links=[]
    for name in DOCS:
        body=(ROOT/name).read_text()
        for link in re.findall(r'\]\(([^)]+)\)',body):
            if '://' in link or link.startswith('#'):continue
            target=link.split('#')[0]
            destination=(ROOT/name).parent.joinpath(target).resolve()
            pending={BASE/'functional-coverage-report.json',BASE/'functional-scenario-inventory.csv'}
            if target and not destination.exists() and destination not in pending:links.append(dict(file=name,target=target))
    if links:raise ValueError('MISSING_DOCUMENT_LINKS:'+json.dumps(links))
    manifest=json.loads(a.diagrams.read_text())
    figures=manifest['results'];expected=[]
    for name in DOCS:
        for n,m in enumerate(re.finditer(r'^```mermaid[^\n]*\n([\s\S]*?)^```\s*$',(ROOT/name).read_text(),re.M),1):
            expected.append((name,n,hashlib.sha256(m[1].encode()).hexdigest()))
    actual=[(f['file'],f['index'],f['source_sha256']) for f in figures]
    if set(actual)!=set(expected):raise ValueError('DIAGRAM_SOURCE_SET')
    artifacts={}
    for figure in figures:
        if figure['out_of_bounds_labels']:raise ValueError('CLIPPED_DIAGRAM_LABEL')
        if sha(ROOT/figure['source'])!=figure['source_sha256'] or sha(ROOT/figure['svg'])!=figure['svg_sha256']:raise ValueError('DIAGRAM_HASH')
        for name in ('source','svg'):artifacts[figure[name]]=sha(ROOT/figure[name])
    artifacts[str(a.diagrams.resolve().relative_to(ROOT))]=sha(a.diagrams)
    render_checks_path=a.diagrams.parent/'render-checks.json';render_checks=json.loads(render_checks_path.read_text())
    if render_checks['renderer_sha256']!=sha(ROOT/'ops/render_docs_diagrams.cjs'):raise ValueError('RENDERER_SOURCE_CHANGED')
    for name,h in render_checks['png_sha256'].items():
        if sha(ROOT/name)!=h:raise ValueError('PNG_HASH')
        artifacts[name]=h
    if len(render_checks['png_sha256'])!=len(figures):raise ValueError('PNG_EXPORT_SET')
    artifacts[str(render_checks_path.resolve().relative_to(ROOT))]=sha(render_checks_path)
    for path in inputs:
        if path.endswith('.py'):ast.parse((ROOT/path).read_text(),filename=path)
    csv_path=BASE/'functional-scenario-inventory.csv'
    with csv_path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=('id','clause','title','execution_status'));writer.writeheader()
        writer.writerows({k:r[k] for k in writer.fieldnames} for r in rows)
    artifacts[str(csv_path.relative_to(ROOT))]=sha(csv_path)
    report=dict(schema='satoshifi.functional-coverage-documentation-audit.v1',as_of=plan['as_of'],
        specification_sha256=sha(a.spec),numbered_clauses=len(index['numbered_clauses']),
        functional_complete=False,production_ready=False,scope='Documentation/code/evidence comparison; no new protocol execution',
        suite_recipe_count=baseline['scenario_count'],mandatory_table_row_count=len(rows),mandatory_table_rows_by_clause=counts,
        named_scenario_counts=dict(ADDR=sum(r['id'].startswith('ADDR-') for r in rows),SUB=sum(r['id'].startswith('SUB-') for r in rows)),
        coverage_percentage=None,per_case_qualified_execution_complete=False,
        baseline=dict(path=str(baseline_path.relative_to(ROOT)),sha256=sha(baseline_path),python_tests=tests,
            deferred_native_checks=baseline['deferred_existing_native_checks'],recorded_execution_source_status='MATCH_RECORDED_PATHS',
            changed_recorded_documentation=changed,new_protocol_tests_run=False),
        areas=plan['areas'],gaps=plan['gaps'],scenario_inventory=rows,
        requirement_area_map=requirement_map,requirement_area_map_is_execution_coverage=False,
        documentation_checks=dict(local_links='PASS',diagram_sources='PASS',diagram_count=len(figures),clipped_labels=0,python_syntax='PASS'),
        source_sha256={p:sha(ROOT/p) for p in sorted(inputs)},artifact_sha256=artifacts)
    (BASE/'functional-coverage-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(scenarios=len(rows),suite_recipes=baseline['scenario_count'],diagrams=len(figures),
        recorded_execution_source='MATCH_RECORDED_PATHS',changed_documentation=len(changed),new_protocol_tests_run=False,production_ready=False)))


if __name__=='__main__':main()
