"""Verify separate regtest E2E evidence; never promote it to mainnet acceptance."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET
from .state_trial import ROOT, read, write
from .wrap_local import sha
from .deposit_build import HERE, BUILD, CARGO, RUSTC, CARGO_CACHE, NATIVE

def native(base):
    prep=read(base/'preparation.json')
    assert sha(base/'state.input')==prep['input_sha256']
    env=dict(os.environ,CARGO_HOME=str(CARGO_CACHE),CARGO_TARGET_DIR=str(NATIVE),RUSTC=str(RUSTC),
        CARGO_BUILD_JOBS='2',SATOSHIFI_REGTEST_INPUT=str(base/'state.input'))
    results={}
    manifest=BUILD/'deposit-integration/kernel/Cargo.toml'
    for profile in ['debug','release']:
        folder=base/('native-'+profile);folder.mkdir(exist_ok=False)
        start=time.monotonic();flags=['--release'] if profile=='release' else []
        with (folder/'tests.log').open('w') as log:
            run=subprocess.run([str(CARGO),'test','--locked','--offline','--lib','--manifest-path',str(manifest),*flags],env=env,stdout=log,stderr=subprocess.STDOUT)
        assert run.returncode==0 and '3 passed; 0 failed;' in (folder/'tests.log').read_text()
        with (folder/'vectors.log').open('w') as log:
            run=subprocess.run([str(CARGO),'run','--locked','--offline','--manifest-path',str(manifest),*flags,
                '--bin','prepare','--',str(base/'state.input'),str(folder)],env=env,stdout=log,stderr=subprocess.STDOUT)
        assert run.returncode==0
        for path in [base/'state.journal',*base.glob('history-*.bin')]:assert (folder/path.name).read_bytes()==path.read_bytes(),path.name
        results[profile]=dict(status='PASS',tests=3,elapsed_seconds=round(time.monotonic()-start,3),vectors_equal=True)
    write(base/'native-checks.json',dict(status='PASS',profiles=results,input_sha256=sha(base/'state.input'),journal_sha256=sha(base/'state.journal')))
    print(json.dumps(results))

def collect(base):
    prep=read(base/'preparation.json');accepted=read(base/'acceptance.json');build=read(HERE/'build-report.json')
    assert accepted['status']=='LOCAL_REGTEST_ZK_DEPOSIT_PASS' and not accepted['production_ready'] and not accepted['mainnet_qualified']
    assert accepted['state']==prep['addresses']['TestRegtestDepositState'] and accepted['consumer']==prep['addresses']['MiningPoolV1']
    assert sha(base/'state.input')==prep['input_sha256'] and sha(base/'state.journal')==prep['journal_sha256']==accepted['journal_sha256']
    assert sha(ROOT/prep['program'])==prep['program_sha256']==build['program_sha256']
    sources=dict(prep['source_sha256']);phases={};previous=None
    for stage in ['composite','succinct','groth16']:
        folder=base/stage;report=read(folder/'report.json');receipt=read(folder/'verified/receipt.json')
        assert report['status']=='VERIFIED' and report['source_unchanged'] and report['source_sha256']==prep['source_sha256']
        supplemental=report.get('supplemental_source_sha256',{})
        assert supplemental
        for path,digest in supplemental.items():
            assert sha(ROOT/path)==digest,path
            assert path not in sources or sources[path]==digest,path
            sources[path]=digest
        assert report['preparation_sha256']==sha(base/'preparation.json') and report['prior_report_sha256']==previous
        assert receipt['status']=='VERIFIED' and receipt['receipt_kind']==stage and receipt['dev_mode_disabled']
        assert receipt['image_id']==prep['program_image_id'] and receipt['journal_sha256']==prep['journal_sha256']
        assert receipt['receipt_sha256']==sha(folder/'receipt.bin') and receipt['wrong_image_rejected'] and receipt['modified_journal_rejected']
        assert all(report['checks'][key]['exit_code']==0 for key in ['network','prove','verify'])
        for path,item in report['artifacts'].items():assert sha(folder/path)==item['sha256'],path
        if stage=='groth16':assert report['container']['network_mode']=='none'
        phases[stage]=dict(proving_seconds=report['checks']['prove']['elapsed_seconds'],verification_seconds=report['checks']['verify']['elapsed_seconds'],
            receipt_sha256=receipt['receipt_sha256'],receipt_bytes=receipt['receipt_bytes'])
        previous=sha(folder/'report.json')
        if stage=='composite' and 'recovery' in report:
            recovery=report['recovery'];original=ROOT/recovery['original_report']
            assert sha(original)==recovery['original_report_sha256'] and not recovery['new_stark_computed']
            assert sha(original.parent/'receipt.bin')==recovery['original_receipt_sha256']==sha(folder/'receipt.bin')
            assert read(original)['status']=='INCOMPLETE' and read(original)['checks']['prove']['exit_code']==0
    assert accepted['receipt_sha256']==receipt['receipt_sha256']
    positive={'state_accepted','header_materialized','mint_accepted'}
    required=positive|{'wrong_state_rejected','modified_journal_rejected','proof_replay_rejected','mint_without_header_rejected',
        'invalid_history_rejected','wrong_merkle_rejected','wrong_index_rejected','wrong_recipient_rejected',
        'mint_replay_rejected','reauthorized_outpoint_rejected','stale_mint_rejected'}
    assert required<=accepted['checks'].keys()
    for key in required:assert accepted['checks'][key]['status']==int(key in positive)
    assert accepted['cleanup']==dict(anvil='stopped',bitcoin='stopped')
    cleanup=read(base/'final-process-check.json')
    assert all(cleanup['own_nodes_stopped'].values()) and cleanup['no_task_prover_container']
    assert cleanup['docker_not_stopped'] and cleanup['previous_nine_services_running']
    core=read(base/'core-inclusion.json')
    assert core['status']=='PASS' and core['chain']=='regtest' and core['proof_sha256']==sha(base/'core-merkleblock.bin')
    assert set(core['verified_txids'])=={r['txid'] for r in read(base/'funding.json')}
    for name,digest in core['consumer_artifact_sha256'].items():
        assert sha(base/'consumer-artifacts'/(name+'.json'))==digest
    native_check=read(base/'native-checks.json');assert native_check['status']=='PASS' and native_check['journal_sha256']==prep['journal_sha256']
    assert {k:v['tests'] for k,v in native_check['profiles'].items()}==dict(debug=3,release=3)
    regression=ROOT/'ops/artifacts/zkspv/deposit-integration-regression.xml'
    suites=ET.parse(regression).getroot().findall('.//testsuite')
    assert sum(int(s.get('tests',0)) for s in suites)>=30
    assert all(int(s.get(key,0))==0 for s in suites for key in ['failures','errors','skipped'])
    for path in [Path(__file__).resolve(),ROOT/'ops/zkspv/deposit_verify_core.py',ROOT/'tests/zkspv/test_deposit_integration.py',HERE/'README.md']:
        sources[str(path.relative_to(ROOT))]=sha(path)
    for name,digest in sources.items():assert sha(ROOT/name)==digest,name
    artifacts={str(p.relative_to(ROOT)):sha(p) for p in base.rglob('*') if p.is_file()}
    for path in [HERE/'build-report.json',HERE/'bridge-build-report.json',HERE/'evidence/bridge-build.log',HERE/'evidence/current.json',regression,*list((HERE/'evidence/contracts').glob('*.json')),
                 HERE/'evidence/regtest-deposit.r0bf',HERE/'evidence/regtest-deposit.elf']:
        artifacts[str(path.relative_to(ROOT))]=sha(path)
    result=dict(schema='satoshifi.regtest-deposit-integration.v1',status='LOCAL_REGTEST_ZK_DEPOSIT_PASS',production_ready=False,
        public_transactions=0,network='regtest',source_sha256=sources,artifact_sha256=artifacts,phases=phases,
        evidence_directory=str(base.relative_to(ROOT)),program_image_id=prep['program_image_id'],
        mainnet_image_id=build['mainnet_image_id'],mainnet_guest_unchanged=True,mainnet_consumer_qualified=False,
        consumer='MiningPoolV1',adapter='ZkSpvAdapter',state='TestRegtestDepositState',confirmations=6,
        satoshis=accepted['after']['backing'],minted_mp_base_units=accepted['after']['balance'],
        transaction_id=accepted['bitcoin_txid'],state_acceptance_gas=accepted['checks']['state_accepted']['gas_used'],
        header_materialization_gas=accepted['checks']['header_materialized']['gas_used'],mint_gas=accepted['checks']['mint_accepted']['gas_used'],
        checks=accepted['checks'],native_tests=native_check['profiles'],regression_tests=sum(int(s.get('tests',0)) for s in suites),
        limitations=['TEST_ONLY fixed regtest linear guest/state, not the mainnet V2 state or its production bootstrap',
            'Checkpoint and prior work/context are explicit local deployment inputs; earlier history is not recursively proven',
            'Shared canonical-history hash encoding is reused unchanged; network isolation is the distinct guest/image/journal/state',
            'Controlled local EVM clock for Fresh expiry; no production SLA, subsidy/address-mode qualification or independent audit'])
    write(HERE/'report.json',result);print(json.dumps(dict(status=result['status'],sources=len(sources),artifacts=len(artifacts))))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['native','collect']);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();(native if a.stage=='native' else collect)(a.output.resolve())
