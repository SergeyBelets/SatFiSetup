#!/usr/bin/env python3
"""Local immutable state/adapter evidence. Positive proof acceptance remains UNKNOWN."""
import argparse,hashlib,importlib.util,json,os,re,shutil,subprocess,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
PARENT=HERE.parent/'resume-transition'
spec=importlib.util.spec_from_file_location('state_adapter_parent',PARENT/'qualify.py')
parent=importlib.util.module_from_spec(spec);spec.loader.exec_module(parent)
sha=parent.sha
CORE=['BitcoinCheckpoint','BitcoinTransitionV2','BitcoinCanonicalHistory','BitcoinHeaderTime','BitcoinTxParser']

def snapshot():
    report_path=PARENT/'report.json';r=json.loads(report_path.read_text())
    if r['status']!='LOCAL_COMPONENT_PASS':raise ValueError('Parent evidence missing')
    paths={report_path}
    for name,digest in r['source_sha256'].items():
        if sha(ROOT/name)!=digest:raise ValueError('Parent source changed: '+name)
        paths.add(ROOT/name)
    paths|={p for p in HERE.rglob('*') if p.is_file() and p.name!='report.json'
        and not any(v in ('target','evidence','__pycache__','out','cache') for v in p.relative_to(HERE).parts)}
    paths|={ROOT/f'web3/contracts/src/core/{name}.sol' for name in CORE}
    paths|={ROOT/'web3/contracts/src/libs/TargetsHelper.sol',ROOT/'web3/contracts/src/federation/BitcoinWithdrawalEscrow.sol',
        ROOT/'web3/contracts/src/federation/MainnetHeaderRelay.sol',ROOT/'web3/contracts/src/federation/MainnetSPVGuard.sol',
        ROOT/'web3/lib/openzeppelin-contracts/contracts/utils/math/SafeCast.sol',ROOT/'tests/zkspv/test_state_adapter.py'}
    paths|=set((HERE.parent/'vendor/risc0-ethereum-365e7b2/contracts').rglob('*.sol'))
    return {str(p.relative_to(ROOT)):sha(p) for p in sorted(paths)}

def run(command,env,log):
    start=time.monotonic();r=subprocess.run(list(map(str,command)),cwd=HERE,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    log.write_text(r.stdout)
    record=dict(command=list(map(str,command)),exit_code=r.returncode,elapsed_seconds=round(time.monotonic()-start,3),log=str(log.relative_to(HERE)),log_sha256=sha(log))
    if r.returncode:raise RuntimeError('Qualification failed: '+str(log))
    return record

def check_abi(abi):
    text=(ROOT/'web3/contracts/src/federation/BitcoinWithdrawalEscrow.sol').read_text().split('interface IPaymentSPV {',1)[1].split('}',1)[0]
    signatures={}
    for name,args,returns in re.findall(r'function\s+(\w+)\((.*?)\).*?returns\s*\((.*?)\)',text,re.S):
        types=lambda fields:[field.strip().split()[0] for field in fields.split(',') if field.strip()]
        signatures[name]=(types(args),types(returns))
    if len(signatures)!=3:raise ValueError('Actual IPaymentSPV changed')
    for name,(inputs,outputs) in signatures.items():
        function=next(f for f in abi if f.get('type')=='function' and f.get('name')==name)
        if [p['type'] for p in function['inputs']]!=inputs or [p['type'] for p in function['outputs']]!=outputs:raise ValueError('Payment ABI mismatch')
        if function['stateMutability'] not in ('view','pure'):raise ValueError('Payment method not read-only')
    return signatures

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['cargo','rustc','rustdoc','cargo-home','target-dir','forge','local-receipts']:p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();a.target_dir=a.target_dir.resolve();evidence=HERE/'evidence';evidence.mkdir(exist_ok=True)
    rustc=subprocess.check_output([str(a.rustc),'-Vv'],text=True)
    if not rustc.startswith('rustc 1.88.0 '):raise ValueError('Exact Rust pin required')
    # This crate adds no external dependency; compare its whole lock with the pin.
    import tomllib
    key=lambda x:(x['name'],x['version'],x.get('source'),x.get('checksum'))
    known={key(x) for x in tomllib.loads((parent.prepare.VENDOR/'Cargo.lock').read_text())['package']}
    deps=[x for x in tomllib.loads((HERE/'Cargo.lock').read_text())['package'] if x.get('source')]
    if len(deps)!=16 or any(key(x) not in known for x in deps):raise ValueError('Dependency pin mismatch')
    subprocess.run([sys.executable,ROOT/'rust/zkspv/tools/verify_upstream.py'],check=True)
    sources=snapshot();env=dict(os.environ)
    for k in ['RUSTC_WRAPPER','RUSTC_WORKSPACE_WRAPPER','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS']:env.pop(k,None)
    env.update(CARGO_HOME=str(a.cargo_home.resolve()),CARGO_TARGET_DIR=str(a.target_dir),RUSTC=str(a.rustc.resolve()),RUSTDOC=str(a.rustdoc.resolve()),CARGO_INCREMENTAL='0')
    runs={}
    for profile in ['debug','release']:
        command=[a.cargo,'build','--locked','--offline','--manifest-path',HERE/'Cargo.toml','--bin','state_vectors']
        if profile=='release':command.append('--release')
        runs[profile+'_build']=run(command,env,evidence/(profile+'-build.log'))
        binary=a.target_dir/profile/'state_vectors'
        runs[profile+'_vectors']=run([sys.executable,HERE/'generate_vectors.py','--binary',binary,'--check'],env,evidence/(profile+'-vectors.log'))
    results=a.target_dir/'solidity-results.json'
    runs['solidity']=run([a.forge,'test','--root',HERE,'--offline','--out',a.target_dir/'forge-tests','--cache-path',a.target_dir/'forge-cache',
        '--gas-limit','16777216','--enable-tx-gas-limit','--isolate','--color','never','-vv','--json-file',results],env,evidence/'solidity.log')
    shutil.copyfile(results,evidence/'solidity-results.json')
    tests=[v for s in json.loads(results.read_text()).values() for v in s['test_results'].values()]
    if len(tests)!=25 or any(t['status']!='Success' for t in tests):raise ValueError('Incomplete Solidity suite')
    runs['solidity'].update(tests_passed=len(tests),positive_authentication='Foundry exact-call mock at real verifier boundary ONLY',
        real_verifier_negative_tests=True,production_verifier_injection=False)
    ordinary=a.target_dir/'ordinary'
    runs['ordinary_build']=run([a.forge,'build','--root',HERE,'--offline','--skip','test','--out',ordinary,'--cache-path',a.target_dir/'ordinary-cache','--color','never'],env,evidence/'ordinary-build.log')
    receipts=json.loads(a.local_receipts.read_text())
    if receipts['status']!='PASS_LOCAL_EVM' or receipts['production_ready'] or receipts['script_sha256']!=sha(HERE/'local_receipts.py') or receipts['fixture_sha256']!=sha(HERE/'fixtures/state-v2.json'):
        raise ValueError('Stale local receipt evidence')
    artifacts={}
    for name in ['ZkBitcoinStateV2','ZkSpvAdapter','RiscZeroGroth16Verifier']:
        art=json.loads(next(ordinary.glob('*/'+name+'.json')).read_text())
        init=bytes.fromhex(art['bytecode']['object'][2:]);runtime=bytes.fromhex(art['deployedBytecode']['object'][2:])
        if len(init)>49152 or len(runtime)>24576:raise ValueError('Code size limit')
        for d in receipts['deployments']:
            expected=d['contracts'][name]
            if expected['creation_bytecode_sha256']!=hashlib.sha256(init).hexdigest() or expected['runtime_template_sha256']!=hashlib.sha256(runtime).hexdigest():raise ValueError('Receipt artifact mismatch')
            if d['creation_receipt']['status']!=1 or d['creation_receipt']['gas_used']>16777216 or d['fake_seal_receipt']['status']!=0:raise ValueError('Receipt status/limit mismatch')
        artifacts[name]=dict(creation_bytes=len(init),runtime_bytes=len(runtime),creation_sha256=hashlib.sha256(init).hexdigest(),runtime_template_sha256=hashlib.sha256(runtime).hexdigest(),
            compiler=art['metadata']['compiler'],settings=art['metadata']['settings'])
        (evidence/(name+'-abi.json')).write_text(json.dumps(art['abi'],indent=2)+'\n')
        if name=='ZkSpvAdapter':payment_abi=check_abi(art['abi'])
    if len(receipts['deployments'])!=3:raise ValueError('Missing checkpoint receipts')
    shutil.copyfile(a.local_receipts,evidence/'local-receipts.json')
    if snapshot()!=sources:raise ValueError('Source changed during qualification')
    report=dict(schema='satoshifi.state-adapter-evidence.v2',status='LOCAL_COMPONENT_PASS',production_ready=False,source_sha256=sources,
        sources_unchanged_during_run=True,compiler=rustc,forge=subprocess.check_output([str(a.forge),'--version'],text=True),
        tools_sha256={'cargo':sha(a.cargo),'rustc':sha(a.rustc),'forge':sha(a.forge)},upstream_matching_dependencies=deps,runs=runs,
        artifacts=artifacts,payment_abi=payment_abi,local_deployment_receipts=receipts,
        evidence_sha256={str(p.relative_to(HERE)):sha(p) for p in sorted(evidence.iterdir()) if p.is_file()},
        unavailable=dict(real_guest_proof='NOT_PERFORMED',production_activation='NOT_PERFORMED',production_policy='NOT_SELECTED',
            monetary_consumer_graph='NOT_SWITCHED',frost_relay_profile='NOT_IMPLEMENTED',independent_review='UNKNOWN'),
        limits=['Only explicit PROOF_ONLY_V1 is implemented; this does not select the production relay profile',
            'Fixture image ID and Fresh limits are test values, not production values',
            'Cache binds the current canonical root; re-materialization is required after every root change including append',
            'Positive state transitions use exact-call test mocks; only deployment and real-verifier rejection have local transaction receipts',
            'Production state/verifier/adapter graph is built separately from unchanged legacy contracts; no public deployment or consumer migration'])
    (HERE/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'solidity_tests':25,'real_local_deployments':3,'production_ready':False}))
if __name__=='__main__':main()
