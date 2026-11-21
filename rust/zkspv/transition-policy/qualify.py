#!/usr/bin/env python3
"""Offline complete-journal component evidence; not a real ZK proof qualification."""
import argparse
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
CHECKPOINT=HERE.parent/'checkpoint-policy'
spec=importlib.util.spec_from_file_location('transition_parent_checkpoint',CHECKPOINT/'qualify.py')
parent=importlib.util.module_from_spec(spec); spec.loader.exec_module(parent)
prepare=parent.prepare
sha,rust_pass=parent.sha,parent.rust_pass
common=parent.parent.parent
SOLIDITY=['web3/contracts/src/core/BitcoinTransition.sol','web3/contracts/src/core/BitcoinHeaderTime.sol',
    'web3/test/BitcoinTransition.t.sol','web3/test/fixtures/transition-policy-v1.json','web3/foundry.toml']

def run(command,cwd,env,log):
    started=time.monotonic()
    result=subprocess.run(list(map(str,command)),cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    log.write_text(result.stdout)
    return dict(command=list(map(str,command)),exit_code=result.returncode,elapsed_seconds=round(time.monotonic()-started,3),
        log=str(log.relative_to(HERE)),log_sha256=sha(log)),result.stdout

def dependencies():
    key=lambda p:(p['name'],p['version'],p.get('source'),p.get('checksum'))
    known={key(p) for p in tomllib.loads((prepare.VENDOR/'Cargo.lock').read_text())['package']}
    entries=[p for p in tomllib.loads((HERE/'Cargo.lock').read_text())['package'] if p.get('source')]
    if not entries or any(key(p) not in known for p in entries): raise ValueError('Dependency differs from upstream pin')
    return entries

def snapshot():
    report_path=CHECKPOINT/'report.json'; report=json.loads(report_path.read_text())
    if report['status']!='LOCAL_COMPONENT_PASS': raise ValueError('Missing parent qualification')
    paths={report_path}
    for name,value in report['source_sha256'].items():
        if sha(ROOT/name)!=value: raise ValueError('Parent evidence stale: '+name)
        paths.add(ROOT/name)
    paths|={p for p in HERE.rglob('*') if p.is_file() and p.name!='report.json'
            and not any(part in ('target','evidence','__pycache__') for part in p.relative_to(HERE).parts)}
    paths|={ROOT/name for name in SOLIDITY}
    paths|={ROOT/'tests/zkspv/test_transition_policy.py'}
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)}

def native_source(original,corrected):
    output=parent.native_source(original,corrected)
    output+='\n#[cfg(test)]\n#[allow(dead_code)]\nmod satoshifi_transition_policy {\n'
    for name,module in [('checkpoint','checkpoint'),('fork','fork'),('time','time')]:
        output+=f'use super::satoshifi_{module}_policy as {name};\n'
    output+=f'#[path = r"{HERE}/src/transition.rs"]\nmod transition;\npub use transition::*;\n}}\n'
    output+='#[cfg(test)]\n#[allow(dead_code, unused_imports)]\nmod satoshifi_transition_tests {\n'
    for const,name in [('HEADERS','headers'),('EXPECTED','expected')]:
        path=ROOT/f'web3/test/fixtures/mainnet/{name}-32246-36288.bin'
        output+=f'const {const}: &[u8] = include_bytes!(r"{path}");\n'
    # Reuse the unchanged native adapter and fixture plumbing, no new Bitcoin core.
    helpers=(CHECKPOINT/'native_tests.rs').read_text().split('#[test] fn checkpoint_factory_mainnet')[0]
    return output+helpers+(HERE/'native_tests.rs').read_text()+'\n}\n'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('cargo','rustc','rustdoc','cargo-home','target-dir','forge','source','archive','native-target-dir'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    rustc=subprocess.check_output([str(args.rustc),'-Vv'],text=True)
    cargo=subprocess.check_output([str(args.cargo),'--version'],text=True)
    if not rustc.startswith('rustc 1.88.0 ') or not cargo.startswith('cargo 1.88.0 '): raise ValueError('Pinned toolchain required')
    manifest,original,corrected=prepare.checked_sources(); prepare.checked_core_vectors()
    deps=dependencies(); sources=snapshot(); evidence=HERE/'evidence'; evidence.mkdir(exist_ok=True)
    env=dict(os.environ)
    for key in ('RUSTC_WRAPPER','RUSTC_WORKSPACE_WRAPPER','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS'): env.pop(key,None)
    env.update(CARGO_HOME=str(args.cargo_home.resolve()),CARGO_TARGET_DIR=str(args.target_dir.resolve()),
        RUSTC=str(args.rustc.resolve()),RUSTDOC=str(args.rustdoc.resolve()),CARGO_INCREMENTAL='0')
    runs={}
    for profile in ('debug','release'):
        command=[args.cargo,'test','--locked','--offline','--manifest-path',HERE/'Cargo.toml','--test','policy']
        if profile=='release': command.append('--release')
        record,output=run(command,ROOT,env,evidence/(profile+'.log')); rust_pass(record,output,14)
        runs[profile]=record; print(profile+': 14 passed',flush=True)
    build,output=run([args.cargo,'build','--locked','--offline','--manifest-path',HERE/'Cargo.toml','--bin','transition_vectors'],ROOT,env,evidence/'vector-build.log')
    if build['exit_code']: raise RuntimeError('Vector build failed')
    binary=args.target_dir/'debug/transition_vectors'
    vectors,output=run([sys.executable,HERE/'generate_vectors.py','--binary',binary,'--check'],ROOT,env,evidence/'vectors.log')
    if vectors['exit_code']: raise RuntimeError('Shared fixture mismatch')
    runs['vector_build'],runs['vectors']=build,vectors
    with tempfile.TemporaryDirectory(prefix='satoshifi-transition-forge-') as folder:
        project=Path(folder)
        for name in SOLIDITY:
            destination=project/Path(name).relative_to('web3'); destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/name,destination)
        shutil.copytree(common.FORGE_STD,project/common.FORGE_STD.relative_to(ROOT/'web3'))
        results=project/'results.json'
        command=[args.forge,'test','--root',project,'--offline','--out','out','--cache-path','cache',
            '--match-contract','BitcoinTransitionTest','--gas-limit','16777216','--enable-tx-gas-limit','--isolate',
            '--color','never','-vv','--json-file',results]
        record,output=run(command,project,env,evidence/'solidity.log')
        if not results.is_file(): raise RuntimeError('No Solidity result; see log')
        shutil.copyfile(results,evidence/'solidity-results.json')
        suites=json.loads(results.read_text()); tests=[v for s in suites.values() for v in s.get('test_results',{}).values()]
        record['tests_passed']=sum(t['status']=='Success' for t in tests)
        if record['exit_code'] or len(tests)!=8 or record['tests_passed']!=8: raise RuntimeError('Solidity qualification failed')
        artifact=json.loads((project/'out/BitcoinTransition.t.sol/TransitionHarness.json').read_text())
        bytecode=bytes.fromhex(artifact['deployedBytecode']['object'].removeprefix('0x'))
        record.update(compiler=artifact['metadata']['compiler'],settings=artifact['metadata']['settings'],
            fixture_runtime_bytes=len(bytecode),fixture_runtime_sha256=hashlib.sha256(bytecode).hexdigest(),shared_journal_cases=6,
            authentication='NONE: library tests do not verify a proof',production_artifacts_untouched=True)
        runs['solidity']=record; print('Solidity: 8 passed',flush=True)
    files=common.verify_source(args.archive,args.source)
    source=args.source/'crates/bitcoin-da/src/verifier.rs'
    if source.read_text()!=original: raise ValueError('Native input mismatch')
    native=native_source(original,corrected)
    (evidence/'native-test-adjustment.patch').write_text(''.join(difflib.unified_diff(corrected.splitlines(True),native.splitlines(True),
        fromfile='a/crates/bitcoin-da/src/verifier.rs',tofile='b/crates/bitcoin-da/src/verifier.rs')))
    native_env=dict(env,CARGO_TARGET_DIR=str(args.native_target_dir.resolve()),CARGO_PROFILE_DEV_DEBUG='0',CARGO_PROFILE_TEST_DEBUG='0',CARGO_BUILD_JOBS='2')
    command=[args.cargo,'test','--locked','--offline','-p','bitcoin-da','--features','native','--lib','verifier::','--','--nocapture']
    try:
        source.write_text(native)
        record,output=run(command,args.source,native_env,evidence/'native.log')
        for name,value in files.items():
            path=args.source/name
            if path==source: value=hashlib.sha256(native.encode()).hexdigest()
            if sha(path)!=value: raise ValueError('Native source changed during run')
    finally: source.write_text(original)
    if common.verify_source(args.archive,args.source)!=files: raise ValueError('Native restoration failed')
    rust_pass(record,output,28)
    record.update(original_archive_files=len(files),temporary_checkout_restored=True,new_transition_tests=4,
        previous_verifier_time_fork_checkpoint_tests=24,resume_retarget_boundaries=2,
        compiled_verifier_sha256=hashlib.sha256(native.encode()).hexdigest())
    runs['native']=record; print('Native: 28 passed; 887 files restored',flush=True)
    if any(sha(ROOT/name)!=value for name,value in sources.items()): raise ValueError('Source changed during qualification')
    report=dict(schema='satoshifi.transition-evidence.v1',status='LOCAL_COMPONENT_PASS',production_ready=False,
        compiler=rustc,cargo=cargo,rustc_sha256=sha(args.rustc),cargo_sha256=sha(args.cargo),
        forge=subprocess.check_output([str(args.forge),'--version'],text=True),forge_sha256=sha(args.forge),
        source_sha256=sources,sources_unchanged_during_run=True,upstream_matching_dependencies=deps,
        parent_patched_verifier_sha256=manifest['patched_sha256'],vector_binary_sha256=sha(binary),runs=runs,
        evidence_sha256={str(p.relative_to(HERE)):sha(p) for p in sorted(evidence.iterdir()) if p.is_file()},
        unavailable=dict(real_guest_proof='NOT_IMPLEMENTED',production_state_adapter='NOT_IMPLEMENTED',
            production_checkpoint_selection='NOT_SUPPLIED',bounded_proving_witness='NOT_IMPLEMENTED',independent_review='UNKNOWN'),
        limits=['Journal encoding and EVM checks do not authenticate a proof; exact bytes and fixed image ID require the real verifier',
            'The verifier adapter is trusted static integration code, never untrusted guest input',
            'Complete snapshot replay remains unbounded; one new batch contains 1..64 entries',
            'Synthetic graph fixtures do not qualify PoW branches; native tests use the actual pinned verifier',
            'No production guest, state/adapter, deployment, RPC or public transaction'])
    (HERE/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],rust_tests=28,native_tests=28,solidity_tests=8,production_ready=False)))

if __name__=='__main__': main()
