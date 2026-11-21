"""Build the separate 512-byte TEST_ONLY receipt bridge with pinned SDK packages."""
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from .state_trial import ROOT, GROTH, read, write
from .wrap_local import sha
from .deposit_build import HERE, CARGO, RUSTC, CARGO_CACHE

def build():
    source=HERE/'bridge';target=Path('/private/tmp/satoshifi-deposit-bridge-target')
    original={(p['name'],p['version'],p.get('source')):p.get('checksum') for p in tomllib.loads((GROTH/'Cargo.lock').read_text())['package']}
    external=[p for p in tomllib.loads((source/'Cargo.lock').read_text())['package'] if 'source' in p]
    for p in external:
        key=(p['name'],p['version'],p['source']);assert key in original and original[key]==p.get('checksum'),key
    env=dict(os.environ,CARGO_HOME=str(CARGO_CACHE),CARGO_TARGET_DIR=str(target),CARGO_BUILD_JOBS='1',RUSTC=str(RUSTC))
    with (HERE/'evidence/bridge-build.log').open('w') as log:
        r=subprocess.run([str(CARGO),'build','--locked','--offline','--manifest-path',str(source/'Cargo.toml')],env=env,stdout=log,stderr=subprocess.STDOUT)
    assert r.returncode==0
    bridge=Path('/private/tmp/satoshifi-proof-tools/regtest-receipt-bridge')
    wrapper=Path('/private/tmp/satoshifi-proof-tools/regtest-wrap-groth16')
    shutil.copy2(target/'debug/satoshifi-regtest-receipt-bridge',bridge);shutil.copy2(target/'debug/wrap_groth16',wrapper)
    tools=HERE/'evidence/bridge-tools';tools.mkdir(exist_ok=True)
    shutil.copyfile(GROTH/'container-pin.json',tools/'container-pin.json')
    pins=dict(bridge_sha256=sha(bridge),wrapper_sha256=sha(wrapper))
    write(tools/'wrapper-build-report.json',pins)
    sources={str(p.relative_to(ROOT)):sha(p) for p in [source/'Cargo.toml',source/'Cargo.lock',source/'src/main.rs',
        source/'src/bin/wrap_groth16.rs',Path(__file__).resolve(),tools/'container-pin.json',tools/'wrapper-build-report.json']}
    write(HERE/'bridge-build-report.json',dict(schema='satoshifi.regtest-receipt-bridge.v1',status='BUILD_PASS',production_ready=False,
        source_sha256=sources,external_dependencies_match=len(external),sdk='3.0.3',journal_bytes=512,
        journal_domain='SATOSHIFI_REGTEST_DEPOSIT_V1',disable_dev_mode=True,bridge_binary=str(bridge),wrapper_binary=str(wrapper),
        **pins,cargo_sha256=sha(CARGO),rustc_sha256=sha(RUSTC),rust=subprocess.check_output([RUSTC,'--version'],text=True).strip(),
        build_log_sha256=sha(HERE/'evidence/bridge-build.log'),
        change_scope='Separate exact journal length/domain only; same pinned RISC Zero receipt verification and compression APIs'))
    print('Separate regtest bridge BUILD_PASS; same pinned SDK dependencies')

if __name__=='__main__':build()
