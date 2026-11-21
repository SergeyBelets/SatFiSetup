"""Durable execution of reviewed contour drivers; command success is not acceptance.

Drivers run without a shell and share the original transaction/signing journals.
After an interrupted action only a fresh observation or explicit recovery driver
may advance it. This controller never retries a new monetary action implicitly.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time

from signer_agent.operations import Journal, canonical, public_data

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = (
    'bootstrap', 'bitcoin_funding', 'withdrawal_prepare', 'consumer_request', 'encrypted_transport',
    'admission_payment', 'proof_preparation', 'fresh_proof', 'state_update', 'membership', 'service_status','smp1_mint', 'address_mint',
    'withdrawal_btc', 'withdrawal_mp', 'withdrawal_s', 'reconciliation',
    'incident_recovery', 'old_epoch_service', 'continuity','address_request','address_registration','address_ready','withdrawal_cancel','scenario_check',
)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''): h.update(part)
    return h.hexdigest()


def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()


def private_write(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical(value) + '\n'); stream.flush(); os.fsync(stream.fileno())


def relative_file(base, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError('CONTOUR_PATH')
    raw = base / name
    if '..' in Path(name).parts or any((base / Path(*Path(name).parts[:i])).is_symlink()
                                      for i in range(1, len(Path(name).parts) + 1)):
        raise ValueError('CONTOUR_PATH')
    if not raw.is_file(): raise ValueError('CONTOUR_ARTIFACT_MISSING')
    return raw


def validate(plan):
    public_data(plan)
    if (not isinstance(plan, dict) or set(plan) != {'version', 'run_id', 'directory', 'source_paths', 'stages'}
            or plan['version'] != '1' or not isinstance(plan['run_id'], str)
            or len(plan['run_id']) != 64 or any(c not in '0123456789abcdef' for c in plan['run_id'])
            or not isinstance(plan['directory'], str) or not Path(plan['directory']).is_absolute()):
        raise ValueError('CONTOUR_CONFIG')
    paths = plan['source_paths']
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 8192
            or any(not isinstance(p, str) for p in paths) or len(set(paths)) != len(paths)):
        raise ValueError('CONTOUR_CONFIG')
    stages = plan['stages']
    if not isinstance(stages, list) or not 1 <= len(stages) <= 32: raise ValueError('CONTOUR_CONFIG')
    seen = set()
    for stage in stages:
        if (not isinstance(stage, dict) or set(stage) != {'id', 'capability', 'timeout_seconds', 'probe', 'run', 'recover'}
                or not isinstance(stage['id'], str) or not stage['id'] or len(stage['id']) > 48
                or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in stage['id'])
                or stage['id']=='contour' or stage['id'] in seen or stage['capability'] not in CAPABILITIES
                or type(stage['timeout_seconds']) is not int or not 1 <= stage['timeout_seconds'] <= 14400):
            raise ValueError('CONTOUR_CONFIG')
        seen.add(stage['id'])
        for action in ('probe', 'run', 'recover'):
            command = stage[action]
            if command is None: continue  # Missing integrations remain explicit blockers.
            if (not isinstance(command, list) or not 1 <= len(command) <= 128
                    or any(not isinstance(x, str) or not x or len(x) > 4096 or '\x00' in x for x in command)
                    or not Path(command[0]).is_absolute()
                    or any(x.split('=')[0] in ('--private-key', '--password', '--secret', '--mnemonic') for x in command)):
                raise ValueError('CONTOUR_COMMAND')
    return plan


class Contour:
    def __init__(self, plan, *, root=ROOT, upstream=(), guard_fds=()):
        self.plan = validate(plan); self.root = Path(root).resolve()
        self.directory = Path(plan['directory']); self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink(): raise ValueError('CONTOUR_PATH')
        self.journal = Journal(self.directory / 'contour.sqlite')
        paths = set(plan['source_paths'])
        # The controller itself is always pinned in real repository runs.
        if self.root == ROOT: paths.add('ops/zkspv/contour.py')
        self.sources = {name: sha(relative_file(self.root, name)) for name in sorted(paths)}
        executables = {command[0] for stage in plan['stages']
                       for action in ('probe', 'run', 'recover') if (command := stage[action]) is not None}
        self.tools = {name: sha(name) for name in sorted(executables)}
        self.upstream=list(upstream);self.guard_fds=tuple(guard_fds)
        if any(not isinstance(p,dict) or set(p)!={'stage','result_sha256'} or not isinstance(p['stage'],str)
                or not isinstance(p['result_sha256'],str) or len(p['result_sha256'])!=64
                or any(c not in '0123456789abcdef' for c in p['result_sha256']) for p in self.upstream):raise ValueError('CONTOUR_DEPENDENCY')
        self.binding = dict(plan=plan, source_sha256=self.sources, executable_sha256=self.tools)
        if self.upstream:self.binding['upstream']=self.upstream
        self.plan_hash = digest(self.binding)
        with self.exclusive():
            self.journal.put('contour', self.binding)
            for stage in plan['stages']:
                self.journal.put(stage['id'], dict(stage=stage, plan_sha256=self.plan_hash))

    @contextmanager
    def exclusive(self):
        fd = os.open(self.directory / 'contour.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('CONTOUR_BUSY') from None
            self.lock_fd = fd
            yield
        finally:
            self.lock_fd = None
            os.close(fd)

    def unchanged(self):
        if any(sha(relative_file(self.root, name)) != value for name, value in self.sources.items()):
            raise ValueError('CONTOUR_SOURCE_CHANGED')
        if any(sha(name) != value for name, value in self.tools.items()): raise ValueError('CONTOUR_TOOL_CHANGED')

    def artifacts(self, files):
        if not isinstance(files, dict) or not 1 <= len(files) <= 64: raise ValueError('CONTOUR_ARTIFACTS')
        for name, expected in files.items():
            if (not isinstance(expected, str) or len(expected) != 64
                    or sha(relative_file(self.directory, name)) != expected):
                raise ValueError('CONTOUR_ARTIFACT_CHANGED')

    def invoke(self, stage, action, prior):
        command = stage[action]
        if command is None: raise ValueError('CONTOUR_DRIVER_MISSING')
        invocation = self.directory / ('invoke-' + secrets.token_hex(16)); invocation.mkdir(mode=0o700)
        response = invocation / 'response.json'
        predecessors = list(self.upstream)
        for entry in self.plan['stages']:
            if entry['id'] == stage['id']: break
            record = self.journal.get(entry['id'])
            if record['status'] != 'COMPLETED': raise ValueError('CONTOUR_DEPENDENCY')
            self.artifacts(record['result']['artifacts'])
            predecessors.append(dict(stage=entry['id'], result_sha256=digest(record['result'])))
        binding = dict(version='1', run_id=self.plan['run_id'], plan_sha256=self.plan_hash,
                       stage=stage['id'], capability=stage['capability'],attempt_id=digest([self.plan_hash, stage['id']]),
                       challenge=secrets.token_hex(32), action=action, predecessors=predecessors)
        private_write(invocation / 'context.json', dict(binding, directory=str(self.directory),
                      response_file=str(response), prior=prior, production_ready=False))
        context_hash=sha(invocation/'context.json')
        env = dict(os.environ, SATOHSI_CONTOUR_CONTEXT=str(invocation / 'context.json'),
                   PYTHONDONTWRITEBYTECODE='1', RISC0_DEV_MODE='0')
        process = None; code = None; started = time.monotonic()
        try:
            with (invocation / 'driver.log').open('xb') as log:
                os.chmod(log.name, 0o600)
                process = subprocess.Popen(command, cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(self.lock_fd,*self.guard_fds))
                try: code = process.wait(timeout=stage['timeout_seconds'])
                except subprocess.TimeoutExpired: raise ValueError('CONTOUR_TIMEOUT') from None
                if code != 0: raise ValueError('CONTOUR_DRIVER_FAILED')
        finally:
            if process is not None:
                # Includes descendants left behind by an exiting driver. Drivers may not daemonize.
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                code=process.wait()
            process_record=dict(binding=binding,command_sha256=digest(command),exit_code=code,
                context_sha256=context_hash,
                driver_log_sha256=sha(invocation/'driver.log') if (invocation/'driver.log').exists() else None,
                response_sha256=sha(response) if response.is_file() and not response.is_symlink() else None,
                elapsed_seconds=round(time.monotonic()-started,3))
            private_write(invocation/'process.json',process_record)
            self.journal.put('invocation:'+binding['challenge'],dict(process_file=str((invocation/'process.json').relative_to(self.directory)),
                process_sha256=sha(invocation/'process.json')))
        self.unchanged()
        if response.is_symlink() or not response.is_file() or response.stat().st_size > 1024 * 1024:
            raise ValueError('CONTOUR_RESPONSE')
        result = json.loads(response.read_text())
        if (not isinstance(result, dict) or set(result) != {'binding', 'status', 'artifacts', 'observations'}
                or result['binding'] != binding or result['status'] not in
                ('SATISFIED', 'ABSENT', 'WAITING', 'RECOVERY_REQUIRED') or not isinstance(result['observations'], dict)):
            raise ValueError('CONTOUR_RESPONSE')
        public_data(result)
        if result['status'] == 'SATISFIED': self.artifacts(result['artifacts'])
        elif result['artifacts']: raise ValueError('CONTOUR_RESPONSE')
        return dict(result, elapsed_seconds=round(time.monotonic() - started, 3),
                    response_file=str(response.relative_to(self.directory)), response_sha256=sha(response))

    def step(self):
        """One bounded new action; every predecessor is freshly observed again."""
        with self.exclusive():
            self.unchanged()
            for stage in self.plan['stages']:
                record = self.journal.get(stage['id']); prior = record['result']
                if record['status'] == 'COMPLETED': self.artifacts(prior['artifacts'])
                observed = self.invoke(stage, 'probe', prior)
                if observed['status'] == 'SATISFIED':
                    if record['status'] == 'COMPLETED' and observed['artifacts'] != prior['artifacts']:
                        raise ValueError('CONTOUR_COMPLETION_CHANGED')
                    # Keep the first receipt stable so downstream input commitments survive polling.
                    if record['status'] != 'COMPLETED': self.journal.update(stage['id'], 'COMPLETED', observed)
                    continue
                if record['status'] == 'COMPLETED': raise ValueError('CONTOUR_COMPLETION_LOST')
                if observed['status'] in ('WAITING', 'RECOVERY_REQUIRED'):
                    return dict(status=observed['status'], stage=stage['id'], production_ready=False)
                # ABSENT after an interrupted action does not establish that no spend occurred.
                action = 'run' if record['status'] == 'PREPARED' else 'recover'
                if stage[action] is None:
                    return dict(status='RECOVERY_REQUIRED' if action == 'recover' else 'DRIVER_MISSING',
                                stage=stage['id'], production_ready=False)
                original=prior.get('prior',{}) if record['status']=='STARTED' else prior
                self.journal.update(stage['id'], 'STARTED', dict(prior=original, observation=observed))
                # Even a normal exit must be followed by a separate fresh probe.
                outcome = self.invoke(stage, action, prior)
                verified = self.invoke(stage, 'probe', prior)
                if verified['status'] == 'SATISFIED': self.journal.update(stage['id'], 'COMPLETED', verified)
                status = ('RECOVERY_REQUIRED' if verified['status']=='ABSENT' and outcome['status']=='RECOVERY_REQUIRED'
                          else verified['status'])
                return dict(status='STAGE_COMPLETED' if status == 'SATISFIED' else status,
                            stage=stage['id'], production_ready=False)
            return self.status()

    def status(self):
        rows = [self.journal.get(s['id']) for s in self.plan['stages']]
        completed = {s['request']['stage']['capability'] for s in rows if s['status'] == 'COMPLETED'}
        missing = sorted(set(CAPABILITIES) - completed)
        return dict(schema='satoshifi.contour-controller.v1', run_id=self.plan['run_id'], plan_sha256=self.plan_hash,
            status='DRIVERS_COMPLETED_UNQUALIFIED' if all(r['status'] == 'COMPLETED' for r in rows) else 'PENDING',
            stages=[dict(id=r['id'], status=r['status']) for r in rows], missing_capabilities=missing,
            configured_missing_drivers=[s['id'] for s in self.plan['stages'] if s['probe'] is None or s['run'] is None],
            execution_scope='REVIEWED_DRIVER_CONTRACT_ONLY', source_sha256=self.sources,
            executable_sha256=self.tools,
            combined_e2e_accepted=False, fresh_proving_verified=False, ready=False, production_ready=False)

    def audit(self, *, refresh=True):
        """Read-only public probes and exact retained response/artifact lineage; never run/recover."""
        with self.exclusive():
            self.unchanged();rows=[];expected=list(self.upstream)
            for stage in self.plan['stages']:
                record=self.journal.get(stage['id']);saved=record['result']
                if record['status']!='COMPLETED':
                    return dict(status='PENDING',stages=rows,next_stage=stage['id'])
                response=relative_file(self.directory,saved['response_file'])
                if sha(response)!=saved['response_sha256']:raise ValueError('CONTOUR_RESPONSE_CHANGED')
                original=json.loads(response.read_text())
                if original!={k:saved[k] for k in ('binding','status','artifacts','observations')}:raise ValueError('CONTOUR_RESPONSE_CHANGED')
                b=original['binding']
                if (b['run_id']!=self.plan['run_id'] or b['plan_sha256']!=self.plan_hash or b['stage']!=stage['id']
                        or b['capability']!=stage['capability'] or b['attempt_id']!=digest([self.plan_hash,stage['id']])
                        or b['predecessors']!=expected or original['status']!='SATISFIED'):raise ValueError('CONTOUR_DEPENDENCY')
                self.artifacts(original['artifacts'])
                execution_files={}
                invocation=response.parent
                process_file=str((invocation/'process.json').relative_to(self.directory))
                process=self.journal.get('invocation:'+b['challenge'])['request']
                if process['process_file']!=process_file or sha(relative_file(self.directory,process_file))!=process['process_sha256']:
                    raise ValueError('CONTOUR_INVOCATION_CHANGED')
                receipt=json.loads((invocation/'process.json').read_text())
                if receipt['binding']!=b or receipt['response_sha256']!=saved['response_sha256'] or receipt['exit_code']!=0:
                    raise ValueError('CONTOUR_INVOCATION_CHANGED')
                for name,expected_hash in (('context.json',receipt['context_sha256']),('driver.log',receipt['driver_log_sha256']),('process.json',process['process_sha256'])):
                    file=str((invocation/name).relative_to(self.directory))
                    if sha(relative_file(self.directory,file))!=expected_hash:raise ValueError('CONTOUR_INVOCATION_CHANGED')
                    execution_files[file]=expected_hash
                current=self.invoke(stage,'probe',saved) if refresh else saved
                if current['status']!='SATISFIED' or current['artifacts']!=saved['artifacts']:
                    return dict(status='RECOVERY_REQUIRED',stages=rows,next_stage=stage['id'],reason='COMPLETION_LOST')
                rows.append(dict(stage=stage['id'],capability=stage['capability'],result_sha256=digest(saved),
                    response_file=saved['response_file'],response_sha256=saved['response_sha256'],artifacts=saved['artifacts'],
                    observations=saved['observations'],execution_files=execution_files,
                    probe_response_file=current['response_file'],probe_response_sha256=current['response_sha256']))
                expected.append(dict(stage=stage['id'],result_sha256=digest(saved)))
            return dict(status='VERIFIED',stages=rows,plan_sha256=self.plan_hash,
                commitment=digest(dict(plan_sha256=self.plan_hash,stages=[r['result_sha256'] for r in rows])))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('inspect', 'step')); p.add_argument('--config', type=Path, required=True)
    a = p.parse_args(argv)
    if a.config.stat().st_size > 1024 * 1024: raise ValueError('CONTOUR_CONFIG')
    runner = Contour(json.loads(a.config.read_text()))
    result = runner.status() if a.action == 'inspect' else runner.step()
    # Paths, raw child logs, private observations and command arguments stay in the private journal.
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('CONTOUR_') else 'CONTOUR_ERROR'
        print(json.dumps(dict(status='ERROR', code=code, production_ready=False)))
        raise SystemExit(1) from None
