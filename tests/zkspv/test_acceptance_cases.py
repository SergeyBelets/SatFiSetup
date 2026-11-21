"""Acceptance evidence must retain fixture boundaries and incomplete requirements."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from ops.zkspv import acceptance_cases as a


def test_every_spec_row_has_executable_source_actions_and_declared_boundaries():
    modules,cases=a.modules_and_cases()
    assert set(cases)==set(a.catalog()) and len(cases)==195
    for row in cases.values():
        assert row['modules'] and not row['specification_row_fully_verified']
        for key in row['modules']:
            assert modules[key]['tests'] and modules[key]['boundary'] in a.BOUNDARIES
            assert all(t['line']>0 for t in modules[key]['tests'])
    assert any(m['boundary']=='NATIVE_CRYPTO' for m in modules.values())
    assert any(m['runner']=='FORGE' for m in modules.values())
    # A production interface getter in this file is not a Forge test case.
    assert 'testHelpersEnabled' not in {t['function'] for t in modules['state_evm']['tests']}


def test_missing_duplicate_or_unknown_case_binding_never_silently_fills_a_row():
    value=json.loads(a.BINDINGS.read_text())
    for bad in ('', 'schedule,schedule', 'invented_probe'):
        changed=deepcopy(value);changed['groups']['11.9'][0]=bad
        with pytest.raises(ValueError,match='ACCEPTANCE_CASE_BINDING'):a.modules_and_cases(changed)
    changed=deepcopy(value);changed['groups']['SUB'].pop()
    with pytest.raises(ValueError,match='ACCEPTANCE_CASE_SET'):a.modules_and_cases(changed)


def test_local_case_cannot_execute_arbitrary_external_file():
    changed=json.loads(a.BINDINGS.read_text());changed['modules']['schedule'][2]='/etc/passwd'
    with pytest.raises(ValueError,match='ACCEPTANCE_MODULE_PATH'):a.modules_and_cases(changed)


def test_one_failed_or_skipped_parameter_cannot_be_hidden_by_successful_retry(tmp_path):
    p=tmp_path/'tests.xml';p.write_text('<testsuites><testsuite><testcase classname="tests.test_example" name="test_case[a]"/><testcase classname="tests.test_example" name="test_case[b]"><failure/></testcase><testcase classname="tests.test_example" name="test_case[c]"><skipped/></testcase></testsuite></testsuites>')
    assert a.python_outcomes(p)=={('test_example','test_case'):['PASS','FAIL','SKIP']}


def test_contract_failure_and_ignored_results_do_not_become_pass(tmp_path):
    p=tmp_path/'forge.json';p.write_text(json.dumps({'test/X.t.sol:XTest':{'test_results':{
        'testFirst()':{'status':'Success'},'testSecond()':{'status':'Failure'},'testThird()':{'status':'Skipped'}}}}))
    assert a.contract_outcomes([p])=={('X.t.sol','testFirst'):['PASS'],('X.t.sol','testSecond'):['FAIL'],('X.t.sol','testThird'):['SKIP']}
    p.write_text(json.dumps({'status':'PASS'}))
    with pytest.raises(ValueError,match='EMPTY_CONTRACT'):a.contract_outcomes([p])


def test_evidence_cannot_relabel_changed_source_or_reuse_previous_output(tmp_path,monkeypatch):
    source={'file':'before'}
    monkeypatch.setattr(a,'source_snapshot',lambda:dict(source))
    p=tmp_path/'plan.json';plan=a.prepare(p)
    assert plan['real_contour_deferred'] and not plan['production_ready']
    with pytest.raises(FileExistsError):a.prepare(p)
    source['file']='after'
    with pytest.raises(ValueError,match='EXECUTION_SOURCE_CHANGED'):a.collect(p,tmp_path/'absent.xml',[],tmp_path/'result.json')
