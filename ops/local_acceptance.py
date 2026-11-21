#!/usr/bin/env python3
"""Reproduce cryptographic/Bitcoin integration tests on isolated loopback nodes.
Includes factory/bootstrap, proof-backed MP/S settlement, fee funding and DAO claim.
Requires forge/anvil and Bitcoin Core 30.3 on PATH and an installed signer-agent[test].
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def run(args,env=None):
    subprocess.run(args,cwd=ROOT,env=env,check=True)

def uses_process_environment(path, seen=None):
    """Include local fixture/script imports when deciding test isolation."""
    path = Path(path).resolve()
    seen = set() if seen is None else seen
    if path in seen:
        return False
    seen.add(path)
    source = path.read_text()
    if re.search(r'\bvm\.(?:setEnv|env\w*)\s*\(', source):
        return True
    imports = re.findall(r'\bimport\s+(?:[^;]*?\bfrom\s+)?[\"\']([^\"\']+)[\"\']\s*;', source)
    return any(uses_process_environment(path.parent / imported, seen)
               for imported in imports if imported.startswith('.'))

def check_solidity_coverage(output, expected_tests, name):
    summary = re.search(
        r'Ran 1 test suite .*?: (\d+) tests? passed, (\d+) failed, (\d+) skipped \((\d+) total tests\)', output)
    if summary is None:
        raise RuntimeError('SOLIDITY_TEST_SUMMARY_MISSING: '+name)
    passed, failed, skipped, total = map(int, summary.groups())
    executed = re.findall(r'^\[PASS\]\s+([^\s(]+)\(', output, flags=re.MULTILINE)
    if (total != len(expected_tests) or passed != total or failed or skipped
            or len(executed) != total or set(executed) != set(expected_tests)):
        raise RuntimeError('SOLIDITY_TEST_COVERAGE_MISMATCH: '+name)
    return passed, failed, skipped

def run_solidity_suites(report_path):
    # Dynamic test linking recompiles production contracts for individual test
    # harnesses. Never mutate the bytecode pinned by simultaneous/following CLI
    # and E2E work, even when the caller has configured a shared FOUNDRY_OUT.
    with tempfile.TemporaryDirectory(prefix='satoshifi-solidity-tests-') as temporary:
        root = Path(temporary)
        environment = dict(os.environ, FOUNDRY_OUT=str(root/'out'),
                           FOUNDRY_CACHE_PATH=str(root/'cache'),
                           FOUNDRY_BUILD_INFO_PATH=str(root/'build-info'))
        return _run_solidity_suites(report_path, environment)

def _run_solidity_suites(report_path, environment):
    private_foundry_read_permission(environment)
    # --list can discover abstract/interface declarations without emitting
    # their artifacts. Populate the private out first instead of depending on
    # a previously built shared output directory.
    print('SOLIDITY_PRIVATE_BUILD', flush=True)
    built = subprocess.run(['forge','build','--root','web3'], cwd=ROOT,
                           text=True, capture_output=True, env=environment)
    print(built.stdout, end='', flush=True)
    print(built.stderr, end='', file=sys.stderr, flush=True)
    built.check_returncode()
    # vm.setEnv belongs to the Forge process, not an EVM snapshot. Artifact
    # lookups can allow tests to overlap even with --threads 1. Isolate each
    # environment-dependent test; ordinary suites run once in their own process.
    listing = json.loads(subprocess.check_output(
        ['forge','test','--root','web3','--list','--json'],cwd=ROOT,text=True,env=environment))
    config = json.loads(subprocess.check_output(
        ['forge','config','--root','web3','--json'],cwd=ROOT,text=True,env=environment))
    out = Path(config['out'])
    if not out.is_absolute():
        out = ROOT/'web3'/out
    suites, nondeployable = [], []
    for path,contracts in listing.items():
        for name,tests in contracts.items():
            if not tests:
                continue
            build = json.loads((out/Path(path).name/(name+'.json')).read_text())
            # --list also includes inherited test declarations on abstract
            # bases and interface methods such as testHelpersEnabled. Forge
            # cannot instantiate artifacts with no creation bytecode.
            if not build['bytecode']['object'].removeprefix('0x'):
                nondeployable.append(dict(path=path,contract=name,tests=tests,
                    reason='abstract/interface artifact has no creation bytecode'))
                continue
            isolated = uses_process_environment(ROOT / 'web3' / path)
            suites.append((path,name,tests,isolated))
    if not suites:
        raise RuntimeError('SOLIDITY_TEST_DISCOVERY_EMPTY')
    expected = sum(len(tests) for _,_,tests,_ in suites)
    report = dict(status='running',isolation='environment_tests_otherwise_suite',
                  artifact_isolation='private_temporary_out_cache_build_info',discovered_tests=expected,
                  discovered_suites=len(suites),nondeployable_declarations=nondeployable,suites=[])
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True,exist_ok=True)
    started = time.monotonic()
    print(f'SOLIDITY_DISCOVERED: {expected} tests in {len(suites)} suites',flush=True)
    try:
        for path,name,tests,isolated in sorted(suites):
            print(f'SOLIDITY_SUITE: {path}:{name} ({len(tests)} tests)',flush=True)
            suite = dict(path=path,contract=name,tests=tests,
                         isolation='process_per_test' if isolated else 'process_per_suite',
                         passed=0,failed=0,skipped=0,cases=[])
            report['suites'].append(suite)
            batches = [[test] for test in sorted(tests)] if isolated else [sorted(tests)]
            for batch in batches:
                label = batch[0] if isolated else 'all'
                print(f'SOLIDITY_TEST: {name}.{label}',flush=True)
                # Forge matches ABI signatures, while --list returns names.
                # The opening parenthesis prevents prefix matches and also
                # selects fuzz tests with arguments. Exactly one must run.
                command = ['forge','test','--root','web3','--threads','1','--color','never',
                           '--match-path',path,'--match-contract','^'+re.escape(name)+'$']
                if isolated:
                    command += ['--match-test','^'+re.escape(batch[0])+r'\(']
                case = dict(name=label,tests=batch,command=command,status='running')
                suite['cases'].append(case)
                case_started = time.monotonic()
                result = subprocess.run(command,cwd=ROOT,text=True,capture_output=True,env=environment)
                case.update(returncode=result.returncode,
                            seconds=round(time.monotonic()-case_started,3))
                print(result.stdout,end='',flush=True)
                print(result.stderr,end='',file=sys.stderr,flush=True)
                result.check_returncode()
                passed,failed,skipped = check_solidity_coverage(result.stdout,batch,name+'.'+label)
                case.update(status='passed',passed=passed,failed=failed,skipped=skipped)
                suite['passed'] += passed
            if suite['passed'] != len(tests):
                raise RuntimeError('SOLIDITY_SUITE_COVERAGE_MISMATCH: '+name)
        report.update(status='passed',passed=sum(item['passed'] for item in report['suites']),
                      failed=0,skipped=0)
    except BaseException as exc:
        report['status'] = 'failed'
        report['error'] = dict(type=type(exc).__name__,message=str(exc),traceback=traceback.format_exc())
        raise
    finally:
        report['seconds'] = round(time.monotonic()-started,3)
        report_path.write_text(json.dumps(report,indent=2)+'\n')
    print(f'SOLIDITY_ACCEPTANCE: {report["passed"]} passed; {len(suites)} suites; environment tests isolated; '
          f'{report["seconds"]} seconds including process startup',flush=True)

def private_foundry_read_permission(environment):
    """Preserve configured grants and add read access only to the private out.

    Foundry intentionally ignores FOUNDRY_FS_PERMISSIONS. Its canonical TOML
    output uses array-of-table grants, so appending one grant avoids rewriting
    the source config or broadening permissions to a whole filesystem subtree.
    The effective configuration stays private, because RPC settings may contain
    credentials; never emit this TOML in diagnostic output.
    """
    configured = subprocess.check_output(['forge','config','--root','web3'],
        cwd=ROOT,text=True,env=environment)
    profiles = tomllib.loads(configured).get('profile', {})
    if len(profiles) != 1:
        raise RuntimeError('SOLIDITY_PRIVATE_PROFILE_AMBIGUOUS')
    profile = next(iter(profiles))
    out = Path(environment['FOUNDRY_OUT']).resolve()
    original = profiles[profile].get('fs_permissions', [])
    grant = dict(access='read', path=str(out))
    addition = ('\n[[profile.'+json.dumps(profile)+'.fs_permissions]]\n'
                'access = "read"\npath = '+json.dumps(str(out))+'\n')
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',prefix='foundry-',suffix='.toml',
                                     dir=out.parent,delete=False) as config:
        config.write(configured+addition)
        environment['FOUNDRY_CONFIG'] = config.name
    effective = json.loads(subprocess.check_output(['forge','config','--root','web3','--json'],
        cwd=ROOT,text=True,env=environment))
    if effective.get('fs_permissions') != [*original,grant]:
        raise RuntimeError('SOLIDITY_PRIVATE_READ_PERMISSION_MISMATCH')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--junitxml',default='ops/artifacts/lifecycle.xml')
    options=parser.parse_args()
    Path(options.junitxml).parent.mkdir(parents=True,exist_ok=True)
    for binary in ('cargo','forge','anvil','bitcoind'):
        if not shutil.which(binary):raise SystemExit('MISSING_TOOL: '+binary)
    if 'v30.3' not in subprocess.check_output(['bitcoind','-version','-nosettings'],text=True):raise SystemExit('BITCOIN_VERSION: require 30.3')
    run(['cargo','build','--locked','--manifest-path','frost-crypto/Cargo.toml'])
    run(['cargo','test','--locked','--manifest-path','frost-crypto/Cargo.toml'])
    run(['forge','build','--root','web3','--sizes'])
    run([sys.executable,'ops/check_contract_sizes.py'])
    run([sys.executable,'ops/generate_abis.py','--check'])
    run_solidity_suites(Path(options.junitxml).with_suffix('.solidity.json'))
    run([sys.executable,'-m','pytest','signer-agent/tests','-q',
         '--junitxml='+str(Path(options.junitxml).with_suffix('.python.xml'))])
    with tempfile.TemporaryDirectory(prefix='satohsi-acceptance-') as temporary:
        root=Path(temporary);evm_port,btc_port=port(),port()
        nodes=[]
        try:
            nodes.append(subprocess.Popen(['anvil','--host','127.0.0.1','--port',str(evm_port),'--silent'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
            nodes.append(subprocess.Popen(['bitcoind','-regtest','-datadir='+str(root),'-server','-txindex=1','-listen=0','-printtoconsole=0','-rpcport='+str(btc_port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
            env=dict(os.environ,SATOHSI_TEST_RPC=f'http://127.0.0.1:{evm_port}',SATOHSI_BTC_RPC=f'http://127.0.0.1:{btc_port}',SATOHSI_BTC_COOKIE=str(root/'regtest/.cookie'),
                     FOUNDRY_OUT=str(root/'out'),FOUNDRY_CACHE_PATH=str(root/'cache'),
                     FOUNDRY_BUILD_INFO_PATH=str(root/'build-info'))
            private_foundry_read_permission(env)
            for _ in range(100):
                if any(p.poll() is not None for p in nodes):raise RuntimeError('NODE_START_FAILED')
                if (root/'regtest/.cookie').exists():break
                time.sleep(.1)
            else:raise RuntimeError('NODE_START_TIMEOUT')
            run([sys.executable,'-m','pytest','tests/e2e','-v','-o','junit_family=legacy','--junitxml='+options.junitxml],env)
        finally:
            for p in nodes:p.terminate()
            for p in nodes:
                try:p.wait(timeout=15)
                except subprocess.TimeoutExpired:p.kill();p.wait()
if __name__=='__main__':main()
