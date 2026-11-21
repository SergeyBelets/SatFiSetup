"""Freeze scenario input, qualified runtime identity and explicit starting-state scope."""
import argparse
import json
import shutil
from pathlib import Path
from .state_trial import ROOT,GUEST,GROTH,read,write
from .wrap_local import sha

HERE=ROOT/'rust/zkspv/proof-scenarios'

def prepare(name):
    model=name in ('fork-work-reorg100','halt101-prefix')
    debug=HERE/'evidence'/('model-debug' if model else 'native-debug')
    release=HERE/'evidence'/('model-release' if model else 'native-release')
    for suffix in ['input','journal']:
        assert (debug/f'{name}.{suffix}').read_bytes()==(release/f'{name}.{suffix}').read_bytes()
    hashes=dict(read(GROTH/'report.json')['source_sha256'])
    hashes[str((GROTH/'report.json').relative_to(ROOT))]=sha(GROTH/'report.json')
    paths=[ROOT/'ops/zkspv'/f for f in ['state_trial.py','state_trial_prove.py','scenario_prepare.py','scenario_prove.py','scenario_evm.py']]
    if model:
        build=read(HERE/'model-build-report.json');assert build['status']=='TEST_ONLY_GUEST_BUILD_PASS'
        hashes.update(build['source_sha256']);paths.append(HERE/'model-build-report.json')
        program=HERE/'evidence/test-policy.r0bf';image=build['program_image_id']
        assert image!=read(GUEST/'report.json')['program_image_id']
    else:
        paths.append(HERE/'retarget_vectors.rs');build=read(GUEST/'report.json')
        program=GUEST/'evidence/mainnet-transition.r0bf';image=build['program_image_id']
    hashes.update({str(p.relative_to(ROOT)):sha(p) for p in paths})
    for p,h in hashes.items():assert sha(ROOT/p)==h,p
    out=HERE/'evidence'/name;out.mkdir(exist_ok=False)
    shutil.copyfile(debug/f'{name}.input',out/'state.input');shutil.copyfile(debug/f'{name}.journal',out/'state.journal')
    journal=(out/'state.journal').read_bytes();assert len(journal)==1216
    assert journal[:32].rstrip(b'\0')==(b'SATOSHIFI_TEST_POLICY_V1' if model else b'SATOSHIFI_TRANSITION_V2')
    scope=('Synthetic work/headers and native-constructed starting state; genuine proof of unchanged bounded policy under a TEST_ONLY image, not Bitcoin PoW'
           if model else 'Native replay of real mainnet history from checkpoint 32256; conditional transition proof, not a recursive proof of that preceding history or an EVM state update')
    report=dict(schema='satoshifi.proof-scenario-preparation.v1',status='PREPARED',scenario=name,test_only_model=model,
        source_sha256=hashes,program=str(program.relative_to(ROOT)),program_sha256=sha(program),program_image_id=image,
        input_sha256=sha(out/'state.input'),journal_sha256=sha(out/'state.journal'),starting_state_scope=scope,
        native_vectors={str(p.relative_to(ROOT)):sha(p) for directory in [debug,release] for p in directory.glob(name+'.*')},
        production_ready=False,public_transactions=0)
    write(out/'preparation.json',report);print(json.dumps(dict(scenario=name,status='PREPARED',model=model,input_bytes=(out/'state.input').stat().st_size)))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('scenario',choices=['retarget-34272','retarget-36288','fork-work-reorg100','halt101-prefix'])
    prepare(p.parse_args().scenario)
