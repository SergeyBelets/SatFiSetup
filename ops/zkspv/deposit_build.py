"""Offline build of the separate regtest deposit guest and ordinary adapter graph.

Public dependency/toolchain download is a separate preparation step. All external
packages must match the already qualified mainnet native/guest lockfiles.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import time
import tomllib
from .state_trial import ROOT, GUEST, read, write
from .wrap_local import sha

HERE=ROOT/'rust/zkspv/deposit-integration'
BUILD=Path('/private/tmp/satoshifi-deposit-build')
CARGO=Path('/Users/dmitrysubbotin/.rustup/toolchains/stable-aarch64-apple-darwin/bin/cargo')
RUSTC=CARGO.with_name('rustc')
GUEST_RUSTC=Path('/private/tmp/satoshifi-deposit-rust/bin/rustc')
CARGO_CACHE=Path('/private/tmp/satoshifi-deposit-cargo')
NATIVE=Path('/private/tmp/satoshifi-deposit-native-target')
TARGET=Path('/private/tmp/satoshifi-deposit-guest-target')

def build():
    pins=read(GUEST/'toolchain.json')
    assert sha(GUEST_RUSTC)==pins['guest_rustc_sha256']
    assert sha(Path('/private/tmp/satoshifi-risc0-3.0.3/r0vm'))==pins['r0vm_sha256']
    parent=read(GUEST/'report.json')
    for name,digest in parent['source_sha256'].items():assert sha(ROOT/name)==digest,name
    source=Path('/private/tmp/satoshifi-scenario-native')
    prepared=read(source/'prepared-source.json')
    for name,digest in prepared['upstream_files'].items():
        assert sha(BUILD/'vendor-native'/name)==digest,name
    shutil.copytree(HERE/'kernel',BUILD/'deposit-integration/kernel',dirs_exist_ok=True)
    deps={}
    for name in ['Cargo.lock','guest/Cargo.lock']:
        original={(p['name'],p['version'],p.get('source')):p.get('checksum') for p in tomllib.loads((GUEST/name).read_text())['package']}
        external=[p for p in tomllib.loads((HERE/'kernel'/name).read_text())['package'] if 'source' in p]
        for p in external:
            key=(p['name'],p['version'],p['source']);assert key in original and original[key]==p.get('checksum'),key
        deps[name]=len(external)
    evidence=HERE/'evidence';evidence.mkdir(exist_ok=True)
    env={k:v for k,v in os.environ.items() if not k.startswith(('RUST','CARGO_','RISC0_','BONSAI_'))}
    env.update(CARGO_HOME=str(CARGO_CACHE),RUSTC=str(RUSTC),CARGO_BUILD_JOBS='2',CARGO_TARGET_DIR=str(NATIVE))
    results={}
    def run(name,command,environment):
        start=time.monotonic()
        with (evidence/(name+'.log')).open('w') as log:
            p=subprocess.run(list(map(str,command)),env=environment,stdout=log,stderr=subprocess.STDOUT)
        assert p.returncode==0,f'{name}: see build log'
        results[name]=dict(exit_code=0,elapsed_seconds=round(time.monotonic()-start,3))
    manifest=BUILD/'deposit-integration/kernel/Cargo.toml'
    run('native-build',[CARGO,'build','--locked','--offline','--release','--manifest-path',manifest],env)
    flags=['-C','passes=lower-atomic','-C','link-arg=-Ttext=0x00200800','-C','link-arg=--fatal-warnings',
           '-C','panic=abort','--cfg','getrandom_backend="custom"',
           f'--remap-path-prefix={BUILD}=/satoshifi-zkspv',f'--remap-path-prefix={CARGO_CACHE}=/cargo',
           f'--remap-path-prefix={GUEST_RUSTC.parent.parent}=/rust']
    guest_env=dict(env,RUSTC=str(GUEST_RUSTC),CARGO_TARGET_DIR=str(TARGET),RISC0_FEATURE_bigint2='',CARGO_ENCODED_RUSTFLAGS='\x1f'.join(flags))
    run('guest-build',[CARGO,'build','--locked','--offline','--release','--target','riscv32im-risc0-zkvm-elf',
                       '--manifest-path',manifest.parent/'guest/Cargo.toml'],guest_env)
    elf=evidence/'regtest-deposit.elf';shutil.copyfile(TARGET/'riscv32im-risc0-zkvm-elf/release/satoshifi-regtest-deposit-guest',elf)
    program=evidence/'regtest-deposit.r0bf'
    image=subprocess.check_output(['/private/tmp/satoshifi-proof-tools/scenario-image-tool',elf,program],text=True).strip()
    assert image==subprocess.check_output(['/private/tmp/satoshifi-risc0-3.0.3/r0vm','--elf',program,'--id'],text=True).strip()
    artifacts={}
    for name in ['TestRegtestDepositState','ZkSpvAdapter','RiscZeroGroth16Verifier']:
        original=Path('/private/tmp/satoshifi-deposit-evm-artifacts')/(name+'.sol')/(name+'.json')
        path=evidence/'contracts'/(name+'.json');path.parent.mkdir(exist_ok=True);shutil.copyfile(original,path)
        a=read(path);creation=bytes.fromhex(a['bytecode']['object'].removeprefix('0x'));runtime=bytes.fromhex(a['deployedBytecode']['object'].removeprefix('0x'))
        assert len(creation)<=49152 and len(runtime)<=24576
        artifacts[name]=dict(path=str(path.relative_to(ROOT)),sha256=sha(path),creation_bytes=len(creation),runtime_bytes=len(runtime))
    sources=dict(parent['source_sha256'])
    for path in list((HERE/'kernel').rglob('*'))+list((HERE/'contracts').rglob('*'))+[HERE/'foundry.toml',Path(__file__)]:
        if path.is_file():sources[str(path.resolve().relative_to(ROOT))]=sha(path)
    write(HERE/'build-report.json',dict(schema='satoshifi.regtest-deposit-build.v1',status='TEST_ONLY_GUEST_BUILD_PASS',
        production_ready=False,network='regtest',source_sha256=sources,upstream_files=prepared['upstream_files'],
        external_dependencies_match=deps,program=str(program.relative_to(ROOT)),program_sha256=sha(program),
        program_image_id=image,mainnet_image_id=parent['program_image_id'],mainnet_guest_unchanged=True,
        tools=dict(cargo=sha(CARGO),rustc=sha(RUSTC),guest_rustc=sha(GUEST_RUSTC)),build=results,artifacts=artifacts,
        native_binary=str(NATIVE/'release/prepare'),native_binary_sha256=sha(NATIVE/'release/prepare')))
    print(image,flush=True)

if __name__=='__main__':build()
