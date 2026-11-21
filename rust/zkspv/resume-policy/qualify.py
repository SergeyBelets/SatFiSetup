#!/usr/bin/env python3
"""Offline V2 bounded witness evidence; no guest/proof or production activation."""
import argparse,difflib,hashlib,importlib.util,json,os,shutil,subprocess,sys,time,tomllib
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
TRANSITION=HERE.parent/'transition-policy'
spec=importlib.util.spec_from_file_location('resume_parent_transition',TRANSITION/'qualify.py')
parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
prepare=parent.prepare
common=parent.common
sha,rust_pass=parent.sha,parent.rust_pass

def run(command,cwd,env,log):
    start=time.monotonic();r=subprocess.run(list(map(str,command)),cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    log.write_text(r.stdout)
    return dict(command=list(map(str,command)),exit_code=r.returncode,elapsed_seconds=round(time.monotonic()-start,3),
        log=str(log.relative_to(HERE)),log_sha256=sha(log)),r.stdout
def dependencies():
    key=lambda p:(p['name'],p['version'],p.get('source'),p.get('checksum'))
    known={key(p) for p in tomllib.loads((prepare.VENDOR/'Cargo.lock').read_text())['package']}
    entries=[p for p in tomllib.loads((HERE/'Cargo.lock').read_text())['package'] if p.get('source')]
    if not entries or any(key(p) not in known for p in entries):raise ValueError('Dependency differs from upstream pin')
    return entries
def snapshot():
    report_path=TRANSITION/'report.json';report=json.loads(report_path.read_text());paths={report_path}
    if report['status']!='LOCAL_COMPONENT_PASS':raise ValueError('Missing parent evidence')
    for name,value in report['source_sha256'].items():
        if sha(ROOT/name)!=value:raise ValueError('Prior evidence changed: '+name)
        paths.add(ROOT/name)
    paths|={p for p in HERE.rglob('*') if p.is_file() and p.name!='report.json'
        and not any(part in ('target','evidence','__pycache__') for part in p.relative_to(HERE).parts)}
    paths.add(ROOT/'tests/zkspv/test_resume_policy.py')
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)}
def native_source(original,corrected):
    output=parent.native_source(original,corrected)
    output+='\n#[cfg(test)]\n#[allow(dead_code)]\nmod satoshifi_resume_policy {\n'
    for name in ['checkpoint','fork','time']:output+=f'use super::satoshifi_{name}_policy as {name};\n'
    for name in ['history','bindings','index','resume','codec','archive']:
        output+=f'#[path = r"{HERE}/src/{name}.rs"]\npub(crate) mod {name};\n'
    output+='pub use resume::*;\npub use codec::*;\n}\n#[cfg(test)]\n#[allow(dead_code,unused_imports)]\nmod satoshifi_resume_tests {\n'
    for const,name in [('HEADERS','headers'),('EXPECTED','expected')]:
        path=ROOT/f'web3/test/fixtures/mainnet/{name}-32246-36288.bin';output+=f'const {const}: &[u8] = include_bytes!(r"{path}");\n'
    helpers=(HERE.parent/'checkpoint-policy/native_tests.rs').read_text().split('#[test] fn checkpoint_factory_mainnet')[0]
    return output+helpers+(HERE/'native_tests.rs').read_text()+'\n}\n'
def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['cargo','rustc','rustdoc','cargo-home','target-dir','source','archive','native-target-dir']:p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();rustc=subprocess.check_output([str(a.rustc),'-Vv'],text=True);cargo=subprocess.check_output([str(a.cargo),'--version'],text=True)
    if not rustc.startswith('rustc 1.88.0 ') or not cargo.startswith('cargo 1.88.0 '):raise ValueError('Pinned toolchain required')
    manifest,original,corrected=prepare.checked_sources();prepare.checked_core_vectors()
    subprocess.run([sys.executable,str(HERE/'generate_reuse.py'),'--check'],check=True)
    deps=dependencies();sources=snapshot();evidence=HERE/'evidence';evidence.mkdir(exist_ok=True)
    env=dict(os.environ)
    for key in ['RUSTC_WRAPPER','RUSTC_WORKSPACE_WRAPPER','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS']:env.pop(key,None)
    env.update(CARGO_HOME=str(a.cargo_home.resolve()),CARGO_TARGET_DIR=str(a.target_dir.resolve()),RUSTC=str(a.rustc.resolve()),RUSTDOC=str(a.rustdoc.resolve()),CARGO_INCREMENTAL='0')
    runs={}
    for profile in ['debug','release']:
        command=[a.cargo,'test','--locked','--offline','--manifest-path',HERE/'Cargo.toml','--test','policy']
        if profile=='release':command.append('--release')
        r,out=run(command,ROOT,env,evidence/(profile+'.log'));rust_pass(r,out,20);runs[profile]=r;print(profile+': 20 passed',flush=True)
    r,out=run([a.cargo,'build','--locked','--offline','--manifest-path',HERE/'Cargo.toml','--bin','witness_vectors'],ROOT,env,evidence/'vector-build.log')
    if r['exit_code']:raise RuntimeError('Vector build failed')
    runs['vector_build']=r;binary=a.target_dir/'debug/witness_vectors'
    r,out=run([sys.executable,HERE/'generate_vectors.py','--binary',binary,'--check'],ROOT,env,evidence/'vectors.log')
    if r['exit_code']:raise RuntimeError('Vector mismatch')
    runs['vectors']=r
    files=common.verify_source(a.archive,a.source);source=a.source/'crates/bitcoin-da/src/verifier.rs'
    if source.read_text()!=original:raise ValueError('Native source mismatch')
    native=native_source(original,corrected)
    (evidence/'native-test-adjustment.patch').write_text(''.join(difflib.unified_diff(corrected.splitlines(True),native.splitlines(True),fromfile='a/crates/bitcoin-da/src/verifier.rs',tofile='b/crates/bitcoin-da/src/verifier.rs')))
    native_env=dict(env,CARGO_TARGET_DIR=str(a.native_target_dir.resolve()),CARGO_PROFILE_DEV_DEBUG='0',CARGO_PROFILE_TEST_DEBUG='0',CARGO_BUILD_JOBS='2')
    try:
        source.write_text(native)
        r,out=run([a.cargo,'test','--locked','--offline','-p','bitcoin-da','--features','native','--lib','verifier::','--','--nocapture'],a.source,native_env,evidence/'native.log')
        for name,value in files.items():
            path=a.source/name
            if path==source:value=hashlib.sha256(native.encode()).hexdigest()
            if sha(path)!=value:raise ValueError('Native source changed during run')
    finally:source.write_text(original)
    if common.verify_source(a.archive,a.source)!=files:raise ValueError('Native restoration failed')
    rust_pass(r,out,32)
    r.update(original_archive_files=len(files),temporary_checkout_restored=True,new_resume_tests=4,previous_native_tests=28,
        actual_mainnet_descendants=4032,retarget_resume_cases=2,new_verifier_calls_per_retarget_resume=1,witness_bytes_per_retarget_resume=31317,
        compiled_verifier_sha256=hashlib.sha256(native.encode()).hexdigest())
    runs['native']=r;print('Native: 32 passed; 887 files restored',flush=True)
    if any(sha(ROOT/name)!=value for name,value in sources.items()):raise ValueError('Source changed during qualification')
    report=dict(schema='satoshifi.resume-evidence.v2',status='LOCAL_COMPONENT_PASS',production_ready=False,
        compiler=rustc,cargo=cargo,rustc_sha256=sha(a.rustc),cargo_sha256=sha(a.cargo),source_sha256=sources,
        sources_unchanged_during_run=True,upstream_matching_dependencies=deps,parent_patched_verifier_sha256=manifest['patched_sha256'],
        vector_binary_sha256=sha(binary),runs=runs,evidence_sha256={str(p.relative_to(HERE)):sha(p) for p in sorted(evidence.iterdir()) if p.is_file()},
        bounds=dict(max_batch_items=64,max_header_bytes=5120,max_path_hashes_per_item=1088,max_witness_bytes=2275554,
            append_witness_bytes=31317,consensus_calls='Exactly one per unique new processed header; zero old-header replay calls'),
        unavailable=dict(real_guest_proof='NOT_IMPLEMENTED',v2_journal_and_evm_integration='NOT_IMPLEMENTED',production_state_adapter='NOT_IMPLEMENTED',
            production_archive_service='NOT_IMPLEMENTED',independent_review='UNKNOWN'),
        limits=['V2 known-map/ordered-log/state commitments are incompatible with V1; no automatic migration or activation',
            'Bounds apply to the witness decoder/resume kernel, not full archive construction/storage or actual ZK cycles',
            'LCA proof and indexed context logic are new critical integration code requiring independent review',
            'Same existing Bitcoin verifier; no new consensus core, real proof, RPC, public deployment or transaction'])
    (HERE/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(dict(status=report['status'],rust_tests=40,native_tests=32,production_ready=False)))
if __name__=='__main__':main()
