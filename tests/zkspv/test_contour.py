"""Real subprocess/SQLite/controller recovery, with explicit non-monetary drivers."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

from ops.zkspv.contour import Contour, CAPABILITIES, digest, validate


DRIVER = '''import hashlib,json,os,pathlib,sys,time
c=json.loads(pathlib.Path(os.environ['SATOHSI_CONTOUR_CONTEXT']).read_text())
d=pathlib.Path(c['directory']); effect=d/(sys.argv[2] if len(sys.argv)>2 else 'effect'); count=d/'calls'
mode=sys.argv[1]
if mode=='timeout':time.sleep(30)
if c['action'] in ('run','recover'):
 count.write_text(count.read_text()+c['action']+'\\n' if count.exists() else c['action']+'\\n')
 if mode!='incomplete':effect.write_text('durable fixture effect')
 if mode=='crash':raise SystemExit(1)
b={k:c[k] for k in ('version','run_id','plan_sha256','stage','capability','attempt_id','challenge','action','predecessors')}
files={'effect':hashlib.sha256(effect.read_bytes()).hexdigest()} if effect.exists() else {}
status='SATISFIED' if files else 'ABSENT'
if mode=='waiting':status='WAITING';files={}
if mode=='lost' or (d/'lost-completion').exists():status='ABSENT';files={}
if mode=='stale':b['challenge']='0'*64
if mode=='empty':files={};status='SATISFIED'
if mode=='unsigned_hold':
 files={};status='ABSENT' if c['action']=='probe' else 'RECOVERY_REQUIRED'
observation={'scope':'EXPLICIT_NON_MONETARY_DRIVER'}
if (d/'lagging-rpc').is_file():
 status='WAITING';files={};observation['reason']=(d/'lagging-rpc').read_text()
r=dict(binding=b,status=status,artifacts=files,observations=observation)
pathlib.Path(c['response_file']).write_text(json.dumps(r))
'''


@pytest.fixture
def contour(tmp_path):
    root = tmp_path / 'repo'; root.mkdir(); (root / 'source.py').write_text('original')
    driver = root / 'driver.py'; driver.write_text(DRIVER)
    command = [sys.executable, str(driver), 'normal']
    plan = dict(version='1', run_id='ab'*32, directory=str(tmp_path/'run'), source_paths=['source.py','driver.py'],
        stages=[dict(id='fund', capability='bitcoin_funding', timeout_seconds=5,
                     probe=command, run=command, recover=None)])
    return root, plan


def test_real_driver_action_is_observed_and_never_repeated(contour):
    root, plan = contour; runner = Contour(plan, root=root)
    assert runner.status()['missing_capabilities'] == sorted(CAPABILITIES)
    assert runner.step()['status'] == 'STAGE_COMPLETED'
    final = Contour(plan, root=root).step()
    assert final['status'] == 'DRIVERS_COMPLETED_UNQUALIFIED'
    assert 'bitcoin_funding' not in final['missing_capabilities']
    assert not final['combined_e2e_accepted'] and not final['fresh_proving_verified']
    assert (Path(plan['directory'])/'calls').read_text() == 'run\n'
    assert (Path(plan['directory'])/'contour.sqlite').stat().st_mode & 0o777 == 0o600


def test_crash_after_effect_adopts_fresh_exact_observation_without_retry(contour):
    root, plan = contour; plan['stages'][0]['run'] = plan['stages'][0]['run'][:-1] + ['crash']
    runner = Contour(plan, root=root)
    with pytest.raises(ValueError, match='CONTOUR_DRIVER_FAILED'): runner.step()
    assert runner.journal.get('fund')['status'] == 'STARTED'
    assert Contour(plan, root=root).step()['status'] == 'DRIVERS_COMPLETED_UNQUALIFIED'
    assert (Path(plan['directory'])/'calls').read_text() == 'run\n'


def test_absent_after_interruption_cannot_start_a_second_action(contour):
    root, plan = contour; runner = Contour(plan, root=root)
    runner.journal.update('fund', 'STARTED', {})
    assert runner.step()['status'] == 'RECOVERY_REQUIRED'
    assert not (Path(plan['directory'])/'calls').exists()


def test_explicit_recovery_uses_same_attempt_namespace(contour):
    root, plan = contour; plan['stages'][0]['recover'] = plan['stages'][0]['run']
    runner = Contour(plan, root=root); runner.journal.update('fund', 'STARTED', {})
    assert runner.step()['status'] == 'STAGE_COMPLETED'
    assert (Path(plan['directory'])/'calls').read_text() == 'recover\n'
    contexts = [json.loads(p.read_text()) for p in Path(plan['directory']).glob('invoke-*/context.json')]
    assert {c['attempt_id'] for c in contexts} == {digest([runner.plan_hash, 'fund'])}
    assert len({c['challenge'] for c in contexts}) == len(contexts)


@pytest.mark.parametrize('mode,code',[('stale','CONTOUR_RESPONSE'),('empty','CONTOUR_ARTIFACTS')])
def test_fresh_binding_and_nonempty_evidence_are_required(contour, mode, code):
    root, plan = contour; plan['stages'][0]['probe'] = plan['stages'][0]['probe'][:-1] + [mode]
    with pytest.raises(ValueError, match=code): Contour(plan, root=root).step()
    assert not (Path(plan['directory'])/'calls').exists()


def test_waiting_does_not_start_action(contour):
    root, plan = contour; plan['stages'][0]['probe'] = plan['stages'][0]['probe'][:-1] + ['waiting']
    assert Contour(plan, root=root).step()['status'] == 'WAITING'
    assert not (Path(plan['directory'])/'calls').exists()


def test_unsigned_action_hold_is_not_hidden_by_absent_probe(contour):
    root,plan=contour
    for action in ('probe','run','recover'):plan['stages'][0][action]=plan['stages'][0]['probe'][:-1]+['unsigned_hold']
    runner=Contour(plan,root=root)
    assert runner.step()['status']=='RECOVERY_REQUIRED'
    assert runner.journal.get('fund')['status']=='STARTED'
    assert runner.step()['status']=='RECOVERY_REQUIRED'
    assert (Path(plan['directory'])/'calls').read_text()=='run\nrecover\n'


def test_source_and_completed_artifact_changes_block_resume(contour):
    root, plan = contour; runner = Contour(plan, root=root); runner.step()
    (root/'source.py').write_text('changed')
    with pytest.raises(ValueError, match='CONTOUR_SOURCE_CHANGED'): runner.step()
    (root/'source.py').write_text('original')
    (Path(plan['directory'])/'effect').write_text('different')
    with pytest.raises(ValueError, match='CONTOUR_ARTIFACT_CHANGED'): runner.step()


def test_changed_plan_cannot_reuse_original_run_journal(contour):
    root, plan = contour; Contour(plan, root=root)
    changed = deepcopy(plan); changed['stages'][0]['timeout_seconds'] = 6
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'): Contour(changed, root=root)


def test_timeout_remains_started_and_requires_recovery(contour):
    root, plan = contour; plan['stages'][0]['timeout_seconds'] = 1
    plan['stages'][0]['run'] = plan['stages'][0]['run'][:-1] + ['timeout']
    runner = Contour(plan, root=root)
    with pytest.raises(ValueError, match='CONTOUR_TIMEOUT'): runner.step()
    assert runner.journal.get('fund')['status'] == 'STARTED'
    assert runner.step()['status'] == 'RECOVERY_REQUIRED'


def test_lock_blocks_second_controller_during_driver_or_other_process(contour):
    root, plan = contour; runner = Contour(plan, root=root)
    with runner.exclusive():
        with pytest.raises(ValueError, match='CONTOUR_BUSY'): Contour(plan, root=root)


def test_missing_driver_stays_visible_and_path_escape_is_rejected(contour):
    root, plan = contour; plan['stages'][0]['probe'] = None
    runner = Contour(plan, root=root)
    assert runner.status()['configured_missing_drivers'] == ['fund']
    with pytest.raises(ValueError, match='CONTOUR_DRIVER_MISSING'): runner.step()
    plan['source_paths'] = ['../outside.py']
    with pytest.raises(ValueError, match='CONTOUR_PATH'): Contour(plan, root=root)


def test_inline_secrets_and_unknown_capabilities_are_rejected(contour):
    _, plan = contour; plan['stages'][0]['run'] += ['--private-key=not-a-real-secret']
    with pytest.raises(ValueError, match='CONTOUR_COMMAND'): validate(plan)
    plan['stages'][0]['run'] = None; plan['stages'][0]['capability'] = 'claimed-global-migration'
    with pytest.raises(ValueError, match='CONTOUR_CONFIG'): validate(plan)


def test_ordered_stage_commitments_survive_revalidation_and_restart(contour):
    root, plan = contour
    command = [sys.executable, str(root/'driver.py'), 'normal', 'second-effect']
    plan['stages'].append(dict(id='second', capability='state_update', timeout_seconds=5,
                               probe=command, run=command, recover=None))
    # Fixture's artifact name must refer to its selected file, too.
    path=root/'driver.py';path.write_text(path.read_text().replace("files={'effect':", "files={effect.name:"))
    runner=Contour(plan,root=root);assert runner.step()['stage']=='fund'
    original=runner.journal.get('fund')['result'];assert runner.step()['stage']=='second'
    assert runner.journal.get('fund')['result']==original
    contexts=[json.loads(p.read_text()) for p in Path(plan['directory']).glob('invoke-*/context.json')]
    for context in contexts:
        if context['stage']=='second':assert context['predecessors']==[dict(stage='fund',result_sha256=digest(original))]
    assert Contour(plan,root=root).step()['status']=='DRIVERS_COMPLETED_UNQUALIFIED'
    assert (Path(plan['directory'])/'calls').read_text()=='run\nrun\n'


def test_lost_canonical_completion_blocks_later_work(contour):
    root,plan=contour;runner=Contour(plan,root=root);runner.step()
    (Path(plan['directory'])/'lost-completion').touch()
    with pytest.raises(ValueError,match='CONTOUR_COMPLETION_LOST'):runner.step()
    assert (Path(plan['directory'])/'calls').read_text()=='run\n'


@pytest.mark.parametrize('reason',['EVM_RECEIPT_PENDING','EVM_RPC_BEHIND'])
def test_completed_stage_waits_for_rpc_without_changing_receipt_or_advancing(contour,reason):
    root,plan=contour;runner=Contour(plan,root=root);runner.step()
    directory=Path(plan['directory'])
    original=runner.journal.get('fund')
    (directory/'lagging-rpc').write_text(reason)
    assert runner.step()==dict(status='WAITING',stage='fund',reason=reason,production_ready=False)
    assert runner.audit()==dict(status='WAITING',stages=[],next_stage='fund',reason=reason)
    assert runner.journal.get('fund')==original
    (directory/'lagging-rpc').unlink()
    assert runner.audit()['status']=='VERIFIED'
    assert runner.journal.get('fund')==original
    assert (directory/'calls').read_text()=='run\n'


def test_executable_symlink_retargeting_is_detected_before_action(contour):
    root,plan=contour
    first=root/'first';first.write_text('first executable fixture');first.chmod(0o700)
    second=root/'second';second.write_text('second executable fixture');second.chmod(0o700)
    command=root/'command';command.symlink_to(first)
    plan['stages'][0]['probe']=[str(command)]
    runner=Contour(plan,root=root);command.unlink();command.symlink_to(second)
    with pytest.raises(ValueError,match='CONTOUR_TOOL_CHANGED'):runner.step()


def test_repeated_pending_recovery_does_not_nest_the_entire_journal_history(contour):
    root,plan=contour;command=plan['stages'][0]['run'][:-1]+['incomplete']
    plan['stages'][0].update(probe=command,run=command,recover=command)
    runner=Contour(plan,root=root)
    for _ in range(8):assert runner.step()['status']=='ABSENT'
    record=runner.journal.get('fund')
    assert record['status']=='STARTED' and record['result']['prior']=={}
    assert len(json.dumps(record))<10000
