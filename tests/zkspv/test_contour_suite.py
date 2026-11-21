"""Real subprocess/SQLite DAG and collection; drivers explicitly have no monetary effects."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from ops.zkspv.contour import digest,sha
from ops.zkspv.contour_suite import Suite,validate
from ops.zkspv.contour_matrix import CASES,assess
from test_contour import DRIVER


@pytest.fixture
def suite(tmp_path):
    root=tmp_path/'repo';root.mkdir();(root/'source.py').write_text('source');(root/'driver.py').write_text(DRIVER)
    directory=tmp_path/'suite'
    def job(key,parents=(),mode='normal'):
        command=[sys.executable,str(root/'driver.py'),mode]
        return dict(id=key,depends_on=list(parents),spec={},profile=dict(role='fresh',require_proof=False),
            plan=dict(version='1',run_id=digest(key),directory=str(directory/'jobs'/key),source_paths=['source.py','driver.py'],
                stages=[dict(id='effect',capability='state_update',timeout_seconds=5,probe=command,run=command,recover=command)]))
    plan=dict(version='1',run_id='aa'*32,directory=str(directory),mode='TEST_ONLY',source_paths=['source.py','driver.py'],
        input_sha256={},jobs=[job('a'),job('b',['a']),job('c')],cases={'initial_fresh':['a']})
    return root,plan,job


def finish(root,plan,limit=12):
    for _ in range(limit):
        result=Suite(plan,root=root).step()
        if result['status']=='EXECUTION_COMPLETE_UNQUALIFIED':return result
    raise AssertionError(result)


def test_dag_restart_pins_parent_commitments_and_never_repeats_actions(suite):
    root,plan,_=suite;result=finish(root,plan)
    assert not result['combined_e2e_accepted'] and not result['production_ready']
    s=Suite(plan,root=root);parent=s.journal.get('job:a')['result']['commitment']
    contexts=list((Path(plan['directory'])/'jobs/b').glob('invoke-*/context.json'))
    assert contexts
    assert all(json.loads(p.read_text())['predecessors']==[dict(stage='job:a',result_sha256=parent)] for p in contexts)
    for key in ('a','b','c'):assert (Path(plan['directory'])/'jobs'/key/'calls').read_text()=='run\n'


def test_waiting_independent_job_cannot_starve_other_consumers(suite):
    root,plan,job=suite;plan['jobs'][0]=job('a',mode='waiting');s=Suite(plan,root=root)
    assert s.step()['job']=='a'
    assert Suite(plan,root=root).step()['job']=='c'
    report=s.collect();assert report['jobs']==dict(a='PENDING',b='DEPENDENCY_BLOCKED',c='VERIFIED')
    assert not (Path(plan['directory'])/'jobs/b/calls').exists()


def test_crash_after_effect_is_adopted_without_second_envelope_and_retained_in_bundle(suite,tmp_path):
    root,plan,_=suite;plan['jobs'][0]['plan']['stages'][0]['run'][-1]='crash'
    with pytest.raises(ValueError,match='DRIVER_FAILED'):Suite(plan,root=root).step()
    result=finish(root,plan);assert result['jobs']['a']=='VERIFIED'
    out=tmp_path/'bundle';report=Suite(plan,root=root).collect(bundle=out)
    assert any(r['interrupted'] and r['binding']['action']=='run' for r in report['invocations'])
    assert all(r['binding']['action']=='probe' for r in report['invocations'] if r['binding']['action']=='probe')
    assert json.loads((out/'manifest.json').read_text())==report
    for name,h in report['artifact_sha256'].items():assert sha(out/name)==h and (out/name).stat().st_mode&0o777==0o600
    assert (Path(plan['directory'])/'jobs/a/calls').read_text()=='run\n'


def test_collection_only_probes_and_commits_original_response_hashes(suite):
    root,plan,_=suite;finish(root,plan);s=Suite(plan,root=root)
    before={p:p.read_bytes() for p in Path(plan['directory']).glob('jobs/*/calls')}
    first=s.collect();second=s.collect()
    assert before=={p:p.read_bytes() for p in before}
    assert first['evidence']['b']['commitment']==second['evidence']['b']['commitment']
    for a in second['evidence'].values():
        assert a['status']=='VERIFIED'
        for stage in a['stages']:assert stage['response_sha256'] and stage['probe_response_sha256']
    assert second['matrix']['cases']['initial_fresh']['status']=='VERIFIED'
    assert not second['matrix']['complete'] and not second['fresh_proving_verified']


@pytest.mark.parametrize('mutation',['effect','response','canonical'])
def test_evidence_loss_blocks_descendants_but_independent_job_progresses(suite,mutation):
    root,plan,_=suite;s=Suite(plan,root=root);s.step();base=Path(plan['directory'])/'jobs/a'
    if mutation=='effect':(base/'effect').write_text('tamper')
    if mutation=='response':
        # Mutate the exact completion response, independent of directory order.
        from ops.zkspv.contour import Contour
        c=Contour(plan['jobs'][0]['plan'],root=root);(base/c.journal.get('effect')['result']['response_file']).write_text('{}')
    if mutation=='canonical':(base/'lost-completion').touch()
    result=s.step();assert result['job']=='c'
    if mutation=='response':
        with pytest.raises(ValueError,match='PROCESS_CHANGED'):s.collect()
        return
    report=s.collect();assert report['jobs']['a']=='RECOVERY_REQUIRED' and report['jobs']['b']=='DEPENDENCY_BLOCKED'
    assert not (Path(plan['directory'])/'jobs/b/calls').exists()


def test_completed_parent_rpc_lag_blocks_dependents_without_recovery_required(suite):
    root,plan,_=suite;s=Suite(plan,root=root);s.step()
    base=Path(plan['directory'])/'jobs/a'
    original=s.journal.get('job:a')
    (base/'lagging-rpc').write_text('EVM_RECEIPT_PENDING')
    assert s.step()['job']=='c'
    assert s.collect()['jobs']==dict(a='WAITING',b='DEPENDENCY_BLOCKED',c='VERIFIED')
    assert s.journal.get('job:a')==original
    assert (base/'calls').read_text()=='run\n' and not (base.parent/'b/calls').exists()
    (base/'lagging-rpc').unlink()
    assert s.step()['job']=='b'
    assert s.journal.get('job:a')==original


@pytest.mark.parametrize('mutation',['source','input','plan'])
def test_immutable_inputs_and_sources_reject_resume(suite,tmp_path,mutation):
    root,plan,_=suite
    path=tmp_path/'input';path.write_text('original');path.chmod(0o600);plan['input_sha256']={str(path):sha(path)}
    s=Suite(plan,root=root)
    if mutation=='source':(root/'source.py').write_text('changed')
    if mutation=='input':path.write_text('changed')
    if mutation=='plan':plan['jobs'][0]['depends_on']=['c']
    with pytest.raises(ValueError):
        if mutation=='plan':Suite(plan,root=root)
        else:s.step()


def test_shared_root_lock_blocks_parallel_controllers(suite):
    root,plan,_=suite;s=Suite(plan,root=root)
    with s.exclusive():
        with pytest.raises(ValueError,match='SUITE_BUSY'):Suite(plan,root=root)


def test_real_child_inherits_parent_lock_even_with_its_own_new_process_group(suite):
    root,plan,_=suite;path=root/'driver.py'
    check="""import fcntl
fd=os.open(d.parent.parent/'suite.lock',os.O_RDWR)
try:
 fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 raise RuntimeError('parent guard was not inherited')
except BlockingIOError:pass
finally:os.close(fd)
"""
    path.write_text(DRIVER.replace("mode=sys.argv[1]",check+"\nmode=sys.argv[1]"))
    assert finish(root,plan)['jobs']['b']=='VERIFIED'


@pytest.mark.parametrize('mutation',['cycle','duplicate','unknown_case','foreign_directory','self_dependency'])
def test_bad_graphs_and_bindings_are_rejected(suite,mutation):
    _,p,_=suite
    if mutation=='cycle':p['jobs'][0]['depends_on']=['b']
    if mutation=='duplicate':p['jobs'][1]['id']='a'
    if mutation=='unknown_case':p['cases']['invented']=[]
    if mutation=='foreign_directory':p['jobs'][0]['plan']['directory']='/tmp/other'
    if mutation=='self_dependency':p['jobs'][0]['depends_on']=['a']
    with pytest.raises(ValueError):validate(p)


def matrix_case(case):
    jobs=[]
    for i,(role,properties) in enumerate(CASES[case]):
        profile=dict(role=role,need={'txid':'aa'*32},scope={'chain_id':'1'},version_hash='v',request_id='r',address_version_hash='a',**properties)
        if '_rbf_' in case:
            profile.update(batch='new' if profile.get('replacement') else 'old',previous='old' if profile.get('replacement') else '00'*32,
                version_hash='new' if profile.get('replacement') else 'old')
        jobs.append(dict(id=str(i),depends_on=[str(i-1)] if i else [],profile=profile,plan={'reviewed':True}))
    return dict(jobs=jobs,cases={case:[j['id'] for j in jobs]})


@pytest.mark.parametrize('case',CASES)
def test_every_catalog_recipe_requires_verified_typed_roles_and_dependencies(case):
    p=matrix_case(case);audits={j['id']:dict(status='VERIFIED') for j in p['jobs']}
    assert assess(p,audits)['cases'][case]['status']==('OBSERVATIONS_VERIFIED' if case.startswith('spec/') else 'VERIFIED')
    audits['0']['status']='RECOVERY_REQUIRED';assert assess(p,audits)['cases'][case]['status']=='PENDING'


@pytest.mark.parametrize('mutation',['need','graph','version','mode','calculation','edge','unbound'])
def test_labels_do_not_cover_mixed_requests_versions_or_missing_edges(mutation):
    case='withdrawal_btc_total';p=matrix_case(case)
    if mutation=='need':p['jobs'][2]['profile']['need']={'txid':'bb'*32}
    if mutation=='graph':p['jobs'][2]['profile']['scope']={'chain_id':'2'}
    if mutation=='version':p['jobs'][2]['profile']['version_hash']='other'
    if mutation=='mode':p['jobs'][2]['profile']['fee_mode']='MP'
    if mutation=='calculation':p['jobs'][2]['profile']['calculation']='payout'
    if mutation=='edge':p['jobs'][2]['depends_on']=[]
    if mutation=='unbound':p['jobs'][2]['plan']=None
    assert assess(p,{j['id']:dict(status='VERIFIED') for j in p['jobs']})['cases'][case]['status']=='UNBOUND'


def test_refund_owner_and_bitcoin_payment_must_name_same_refund():
    case='refund_btc';p=matrix_case(case);p['jobs'][0]['profile']['request_id']='other'
    assert assess(p,{j['id']:dict(status='VERIFIED') for j in p['jobs']})['cases'][case]['status']=='UNBOUND'
