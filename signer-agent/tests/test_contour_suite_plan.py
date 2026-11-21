"""Typed offline recipes and explicit just-in-time binding; no network or signatures."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from ops.zkspv import contour_suite_plan as factory
from ops.zkspv.contour_suite import Suite
from ops.zkspv.contour_matrix import CASES
from ops.zkspv.contour import digest,sha
from signer_agent.zk_worker_prover import write
from test_zk_worker_service import worker
from test_contour_withdrawal import selected,personal,signing,native_keys


@pytest.fixture
def recipe(tmp_path):
    w=worker(tmp_path);c=w.c;w.inbox.close();write(c['identity_file'],dict(private_key='01'*32))
    wc=tmp_path/'worker.json';write(wc,c)
    spec=dict(role='fresh',worker_config=str(wc),request_file=str(tmp_path/'future-need.json'),require_proof=False)
    return dict(version='1',run_id='aa'*32,directory=str(tmp_path/'suite'),
        jobs=[dict(id='fresh',depends_on=[],spec=spec,defer=True)],cases={'initial_fresh':['fresh']})


def test_deferred_build_does_not_require_future_quotes_or_retention_claims(recipe):
    p=factory.build(recipe);assert p['jobs'][0]['plan'] is None
    assert recipe['jobs'][0]['spec']['request_file'] not in p['input_sha256']
    s=Suite(p);report=s.collect();assert report['jobs']['fresh']=='INPUT_REQUIRED'
    assert report['matrix']['cases']['initial_fresh']['status']=='UNBOUND'
    assert not report['combined_e2e_accepted'] and not report['artifact_sha256']


def test_explicit_binding_is_durable_typed_and_once_only(recipe):
    p=factory.build(recipe);s=Suite(p);bound=s.bind('fresh')
    assert bound['status']=='JOB_BOUND_REVIEWABLE'
    assert bound['profile']['require_proof'] is False
    assert Suite(p).bind('fresh')['status']=='JOB_ALREADY_BOUND'
    s=Suite(p);factory.verify_job(s.jobs['fresh'],Path(p['directory']))
    assert s.jobs['fresh']['plan']['stages'][0]['probe'][2]=='ops.zkspv.contour_heartbeat'


@pytest.mark.parametrize('mutation',['sources','inputs'])
def test_mainnet_mode_cannot_omit_sdk_or_private_input_pins(recipe,mutation):
    p=factory.build(recipe)
    if mutation=='sources':p['source_paths']=['ops/zkspv/contour.py']
    else:p['input_sha256']={}
    with pytest.raises(ValueError):Suite(p)


def test_dependency_must_be_verified_before_quotes_can_be_bound(recipe):
    second=deepcopy(recipe['jobs'][0]);second.update(id='later',depends_on=['fresh']);recipe['jobs'].append(second)
    s=Suite(factory.build(recipe))
    with pytest.raises(ValueError,match='SUITE_BIND_DEPENDENCY'):s.bind('later')


def test_changed_immutable_inputs_cannot_be_rebound(recipe):
    p=factory.build(recipe);s=Suite(p);s.bind('fresh')
    path=Path(recipe['jobs'][0]['spec']['worker_config']);c=json.loads(path.read_text());c['max_total_cost_wei']='1';write(path,c)
    with pytest.raises(ValueError,match='SUITE_INPUT_CHANGED'):s.bind('fresh')


@pytest.mark.parametrize('mutation',['profile','command','capability'])
def test_typed_factory_rejects_spoofed_driver_metadata(recipe,mutation):
    s=Suite(factory.build(recipe));s.bind('fresh');job=deepcopy(s.jobs['fresh'])
    if mutation=='profile':job['profile']['require_proof']=True
    if mutation=='command':job['plan']['stages'][0]['run'].append('--unreviewed')
    if mutation=='capability':job['plan']['stages'][0]['capability']='bootstrap'
    with pytest.raises(ValueError,match='SUITE_TYPED_JOB_CHANGED'):factory.verify_job(job,s.directory)


@pytest.mark.parametrize('mutation',['unknown_role','relative','missing_field','heartbeat_disabled'])
def test_bad_recipes_fail_before_any_action(recipe,mutation):
    spec=recipe['jobs'][0]['spec']
    if mutation=='unknown_role':spec['role']='invented'
    if mutation=='relative':spec['worker_config']='relative.json'
    if mutation=='missing_field':del spec['require_proof']
    if mutation=='heartbeat_disabled':
        recipe['jobs'][0]['defer']=False;path=spec['worker_config'];c=json.loads(Path(path).read_text());c['heartbeat']['enabled']=False;write(path,c)
    with pytest.raises(ValueError):factory.build(recipe)


def test_recursive_pins_capture_identity_and_reviewed_inputs_but_ignore_mutable_state(tmp_path):
    identity=tmp_path/'id.json';write(identity,dict(private_key='02'*32))
    state=tmp_path/'state';state.write_text('mutable');state.chmod(0o600)
    c=tmp_path/'config.json';write(c,dict(identity_file=str(identity),state_file=str(state),bitcoin_cookie='/missing/cookie'))
    result=factory.input_files(dict(worker_config=str(c)))
    assert result=={str(c):sha(c),str(identity):sha(identity)}


def test_complete_template_covers_all_cases_and_exact_shared_need_files():
    p=factory.template();assert set(p['cases'])==set(CASES)
    jobs={j['id']:j for j in p['jobs']};assert len(jobs)<512 and all(j['defer'] for j in jobs.values())
    for case,rules in CASES.items():
        roles=[jobs[k]['spec']['role'] for k in p['cases'][case]]
        assert roles==[r for r,_ in rules]
        needs={jobs[k]['spec']['request_file'] for k in p['cases'][case] if jobs[k]['spec']['role'] in
            ('funding','proof','mint','withdrawal_prepare','withdrawal_settle','refund_prepare','refund_settle','fee_ingress','fee_funding')}
        assert len(needs)<=(3 if '_rbf_' in case else 1),case
    assert jobs['incident_resume']['depends_on']==['incident_suspend']


def test_fresh_consumer_recipe_uses_bounded_multi_chunk_cycle_and_existing_uses_only_membership(recipe):
    spec=recipe['jobs'][0]['spec'];c=json.loads(Path(spec['worker_config']).read_text())
    write(spec['request_file'],dict(kind='deposit',consumer=next(iter(c['allowed_consumers'])),txid='11'*32,vout='0'))
    spec={k:v for k,v in spec.items() if k!='require_proof'};spec.update(role='proof',service_config=None,proof_policy='FRESH')
    profile,plan=factory.compile_job(spec,'proof',Path(recipe['directory'])/'jobs'/'proof')
    for stage in plan['stages']:
        if stage['capability'] in ('fresh_proof','state_update'):assert stage['run'][2]=='ops.zkspv.contour_worker_cycle'
    assert profile['proof_policy']=='FRESH'
    spec['proof_policy']='EXISTING';_,plan=factory.compile_job(spec,'membership',Path(recipe['directory'])/'jobs'/'membership')
    assert [s['capability'] for s in plan['stages']]==['membership']


def test_automatic_fresh_gate_precedes_monetary_preparation_without_loading_future_consent(recipe):
    spec=recipe['jobs'][0]['spec'];spec.pop('require_proof');spec.update(role='funding',funding_config='/future/funding.json')
    p=factory.build(recipe);jobs={j['id']:j for j in p['jobs']}
    assert jobs['fresh']['depends_on']==['fresh_fresh']
    assert jobs['fresh_fresh']['profile']==dict(role='fresh',deferred=True)
    assert all(j['plan'] is None for j in p['jobs'])


def test_six_withdrawal_recipes_use_original_reviewed_inputs_and_mode_specific_driver(selected):
    e=selected
    for mode in ('BTC','MP','S'):
        for calculation in ('payout','total'):
            q=deepcopy(e.quote);q['options'].update(fee_mode=mode,asset='S' if mode=='S' else 'MP',calculation=calculation)
            c=dict(e.c,mode=mode,accepted_quote_hash=digest(q));write(e.c['quote_file'],q);write(e.tmp_path/'withdrawal.json',c)
            profiles=[]
            for role in ('withdrawal_prepare','withdrawal_settle'):
                spec=dict(role=role,worker_config=str(e.tmp_path/'worker.json'),request_file=str(e.tmp_path/'need.json'),withdrawal_config=str(e.tmp_path/'withdrawal.json'))
                profile,plan=factory.compile_job(spec,role,e.tmp_path/role);profiles.append(profile)
                caps=[s['capability'] for s in plan['stages']]
                assert caps==(['withdrawal_prepare'] if role.endswith('prepare') else ['membership','withdrawal_'+mode.lower()])
                assert profile['calculation']==calculation and profile['fee_mode']==mode and profile['need']==e.need
            assert profiles[0]['version_hash']==profiles[1]['version_hash']


def test_named_refund_recipe_selects_separate_guest_independent_refund_adapter(selected):
    e=selected;q=deepcopy(e.quote);q.update(kind='BTC_CREDIT_REFUND');q['options'].update(fee_mode='BTC',calculation='refund')
    write(e.c['quote_file'],q);write(e.tmp_path/'withdrawal.json',dict(e.c,mode='BTC',accepted_quote_hash=digest(q)))
    for role in ('refund_prepare','refund_settle'):
        spec=dict(role=role,worker_config=str(e.tmp_path/'worker.json'),request_file=str(e.tmp_path/'need.json'),withdrawal_config=str(e.tmp_path/'withdrawal.json'))
        profile,plan=factory.compile_job(spec,role,e.tmp_path/role)
        for stage in plan['stages']:
            if stage['capability']!='membership':assert stage['run'][2:4]==['ops.zkspv.contour_refund','run']
        assert profile['fee_mode']=='BTC' and profile['calculation']=='refund'
