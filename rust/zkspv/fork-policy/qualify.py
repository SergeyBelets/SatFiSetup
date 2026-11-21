#!/usr/bin/env python3
"""Offline fork/history component evidence; no production proof or deployment."""
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
import tomllib

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
TIME=HERE.parent/'time-policy'
spec=importlib.util.spec_from_file_location('time_qualification',TIME/'qualify.py')
parent=importlib.util.module_from_spec(spec); spec.loader.exec_module(parent)
prepare=parent.prepare
sha,run,rust_pass=parent.sha,parent.run,parent.rust_pass
SOLIDITY=['web3/contracts/src/core/BitcoinCanonicalHistory.sol','web3/test/BitcoinCanonicalHistory.t.sol',
    'web3/contracts/src/federation/MainnetHeaderRelay.sol','web3/contracts/src/libs/TargetsHelper.sol',
    'web3/contracts/src/core/BitcoinTxParser.sol','web3/test/MainnetRelayFixture.sol','web3/foundry.toml',
    'web3/test/fixtures/mainnet/headers-32246-36288.bin','web3/test/fixtures/mainnet/expected-32246-36288.bin',
    'web3/test/fixtures/fork-policy-v1.json']

def dependencies():
    key=lambda p:(p['name'],p['version'],p.get('source'),p.get('checksum'))
    upstream=tomllib.loads((prepare.VENDOR/'Cargo.lock').read_text())
    known={key(p) for p in upstream['package']}
    own=tomllib.loads((HERE/'Cargo.lock').read_text())
    entries=[p for p in own['package'] if p.get('source')]
    if not entries or any(key(p) not in known for p in entries): raise ValueError('Dependency differs from upstream pin')
    return entries

def snapshot():
    paths=set()
    for report_path in (TIME/'report.json',HERE/'source-map.json'):
        report=json.loads(report_path.read_text())
        for name,value in report['source_sha256'].items():
            if sha(ROOT/name)!=value: raise ValueError('Prior/reference evidence is stale: '+name)
            paths.add(ROOT/name)
        paths.add(report_path)
    paths|={p for p in HERE.rglob('*') if p.is_file() and p.name!='report.json'
            and not any(part in ('target','evidence','__pycache__') for part in p.relative_to(HERE).parts)}
    paths|={ROOT/name for name in SOLIDITY}
    paths|={ROOT/'tests/zkspv/test_fork_policy.py'}
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)}

def native_source(original,corrected):
    output=parent.native_source(original,corrected)
    output+='\n#[cfg(test)]\n#[allow(dead_code)]\n#[path = r"'+str(HERE/'src/lib.rs')+'"]\nmod satoshifi_fork_policy;\n'
    output+='#[cfg(test)]\nmod satoshifi_fork_policy_tests {\n'
    for short,method,fixture in [('old','test_header_chain_mainnet_old_blocks','headers-40310-42346.txt'),
                               ('new','test_header_chain_mainnet_newer_blocks','headers-872918-874954.txt')]:
        body=prepare.extract.function(original,method)
        preamble=body[body.index('let mut block_hash'):body.index('let file =')]
        output+=f'fn initial_{short}() -> LatestDaState {{\n{preamble}\nda_state\n}}\n'
        path=prepare.VENDOR/'crates/bitcoin-da/test_data/mainnet'/fixture
        output+=f'const {short.upper()}: &str = include_str!(r"{path}");\n'
    return output+(HERE/'native_tests.rs').read_text()+'\n}\n'

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
    # The shared helper computes log paths relative to its module; use this task's directory.
    parent.HERE=HERE
    runs={}
    for profile in ('debug','release'):
        command=[str(args.cargo),'test','--locked','--offline','--manifest-path',str(HERE/'Cargo.toml'),'--test','policy']
        if profile=='release': command.append('--release')
        record,output=run(command,ROOT,env,evidence/(profile+'.log')); rust_pass(record,output,18)
        runs[profile]=record; print(profile+': 18 passed',flush=True)
    build,output=run([str(args.cargo),'build','--locked','--offline','--manifest-path',str(HERE/'Cargo.toml'),
        '--bin','policy_vectors'],ROOT,env,evidence/'vector-build.log')
    if build['exit_code']: raise RuntimeError('Vector build failed')
    binary=args.target_dir/'debug/policy_vectors'
    vectors,output=run([sys.executable,str(HERE/'generate_vectors.py'),'--binary',str(binary),'--check'],ROOT,env,evidence/'vectors.log')
    if vectors['exit_code']: raise RuntimeError('Shared fixture mismatch')
    runs['vector_build'],runs['vectors']=build,vectors
    with tempfile.TemporaryDirectory(prefix='satoshifi-fork-forge-') as folder:
        project=Path(folder)
        for name in SOLIDITY:
            destination=project/Path(name).relative_to('web3'); destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/name,destination)
        shutil.copytree(parent.FORGE_STD,project/parent.FORGE_STD.relative_to(ROOT/'web3'))
        results=project/'results.json'
        command=[str(args.forge),'test','--root',str(project),'--offline','--out','out','--cache-path','cache',
            '--match-contract','BitcoinCanonicalHistoryTest','--gas-limit','16777216','--enable-tx-gas-limit','--isolate',
            '--color','never','-vv','--json-file',str(results)]
        record,output=run(command,project,env,evidence/'solidity.log')
        if not results.is_file(): raise RuntimeError('No Solidity result; see log')
        shutil.copyfile(results,evidence/'solidity-results.json')
        suites=json.loads(results.read_text()); tests=[v for s in suites.values() for v in s.get('test_results',{}).values()]
        record['tests_passed']=sum(t['status']=='Success' for t in tests)
        if record['exit_code'] or len(tests)!=10 or record['tests_passed']!=10: raise RuntimeError('Solidity qualification failed')
        artifact=json.loads((project/'out/BitcoinCanonicalHistory.t.sol/CanonicalHistoryHarness.json').read_text())
        bytecode=bytes.fromhex(artifact['deployedBytecode']['object'].removeprefix('0x'))
        record.update(compiler=artifact['metadata']['compiler'],settings=artifact['metadata']['settings'],
            fixture_runtime_bytes=len(bytecode),fixture_runtime_sha256=hashlib.sha256(bytecode).hexdigest(),
            synthetic_selection_transitions=505,shared_membership_cases=9,actual_public_batch_tests=3,
            gas_scope='Synthetic graph setup resets test gas; no aggregate production gas measurement')
        runs['solidity']=record; print('Solidity: 10 passed',flush=True)
    files=parent.verify_source(args.archive,args.source)
    source=args.source/'crates/bitcoin-da/src/verifier.rs'
    if source.read_text()!=original: raise ValueError('Native input mismatch')
    # Restore the parent module's paths before generating its unchanged time tests.
    parent.HERE=TIME
    native=native_source(original,corrected)
    parent.HERE=HERE
    (evidence/'native-test-adjustment.patch').write_text(''.join(difflib.unified_diff(corrected.splitlines(True),native.splitlines(True),
        fromfile='a/crates/bitcoin-da/src/verifier.rs',tofile='b/crates/bitcoin-da/src/verifier.rs')))
    native_env=dict(env,CARGO_TARGET_DIR=str(args.native_target_dir.resolve()),CARGO_PROFILE_DEV_DEBUG='0',CARGO_PROFILE_TEST_DEBUG='0',CARGO_BUILD_JOBS='2')
    command=[str(args.cargo),'test','--locked','--offline','-p','bitcoin-da','--features','native','--lib','verifier::','--','--nocapture']
    try:
        source.write_text(native)
        record,output=run(command,args.source,native_env,evidence/'native.log')
        for name,value in files.items():
            path=args.source/name
            if path==source: value=hashlib.sha256(native.encode()).hexdigest()
            if sha(path)!=value: raise ValueError('Native source changed during run')
    finally: source.write_text(original)
    if parent.verify_source(args.archive,args.source)!=files: raise ValueError('Native restoration failed')
    rust_pass(record,output,19)
    record.update(original_archive_files=len(files),temporary_checkout_restored=True,new_fork_tests=4,
        previous_verifier_and_time_tests=15,historical_headers=4074,descendants_after_checkpoint_preparation=4052,
        compiled_verifier_sha256=hashlib.sha256(native.encode()).hexdigest())
    runs['native']=record; print('Native: 19 passed; 887 files restored',flush=True)
    if any(sha(ROOT/name)!=value for name,value in sources.items()): raise ValueError('Source changed during qualification')
    report=dict(schema='satoshifi.fork-history-evidence.v1',status='LOCAL_COMPONENT_PASS',production_ready=False,
        compiler=rustc,cargo=cargo,rustc_sha256=sha(args.rustc),cargo_sha256=sha(args.cargo),
        forge=subprocess.check_output([str(args.forge),'--version'],text=True),forge_sha256=sha(args.forge),
        source_sha256=sources,sources_unchanged_during_run=True,upstream_matching_dependencies=deps,
        parent_patched_verifier_sha256=manifest['patched_sha256'],vector_binary_sha256=sha(binary),runs=runs,
        evidence_sha256={str(p.relative_to(HERE)):sha(p) for p in sorted(evidence.iterdir()) if p.is_file()},
        unavailable=dict(real_guest_proof='NOT_IMPLEMENTED',production_state_adapter='NOT_IMPLEMENTED',
            authenticated_checkpoint_factory='NOT_IMPLEMENTED',bounded_proving_witness='NOT_IMPLEMENTED',independent_review='UNKNOWN'),
        limitations=['New critical policy/history code; no inherited audit acceptance',
            'Synthetic differential forks do not establish PoW validity of competing branches',
            'Historical native tests use fixture-relative chainwork and known checkpoint context',
            'Complete history replay recovers state; no bounded succinct resume witness or availability service',
            'Persistent in-memory nodes are retained without pruning; resource/proving costs are not qualified',
            'Membership requires an authenticated accepted View and separate Fresh/transaction inclusion checks',
            'No production guest, state/adapter, deployment, RPC or public transaction'])
    (HERE/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(status=report['status'],rust_tests=36,native_tests=19,solidity_tests=10,production_ready=False)))

if __name__=='__main__': main()
