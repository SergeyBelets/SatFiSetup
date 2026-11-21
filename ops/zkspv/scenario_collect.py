"""Collect completed proof scenarios without promoting test models to mainnet."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from .scenario_prepare import ROOT,HERE,read,write
from .wrap_local import sha

def collect():
    sources={};scenarios={}
    for name in ['retarget-34272','fork-work-reorg100','halt101-prefix']:
        base=HERE/'evidence'/name;prep=read(base/'preparation.json');evm=read(base/'evm.json')
        assert sha(base/'state.input')==prep['input_sha256'] and sha(base/'state.journal')==prep['journal_sha256']
        for path,digest in prep['source_sha256'].items():
            assert sha(ROOT/path)==digest,path
            assert path not in sources or sources[path]==digest,path
            sources[path]=digest
        phases={};previous=None
        for stage in ['composite','succinct','groth16']:
            folder=base/stage;report=read(folder/'report.json');receipt=read(folder/'verified/receipt.json')
            assert report['status']=='VERIFIED' and report['source_unchanged']
            assert report['preparation_sha256']==sha(base/'preparation.json') and report['prior_report_sha256']==previous
            assert receipt['status']=='VERIFIED' and receipt['receipt_kind']==stage and receipt['dev_mode_disabled']
            assert receipt['image_id']==prep['program_image_id'] and receipt['journal_sha256']==prep['journal_sha256']
            assert receipt['receipt_sha256']==sha(folder/'receipt.bin')
            assert receipt['wrong_image_rejected'] and receipt['modified_journal_rejected']
            assert all(report['checks'][key]['exit_code']==0 for key in ['network','prove','verify'])
            for path,item in report['artifacts'].items():assert sha(folder/path)==item['sha256'],path
            if stage=='groth16':assert report['container']['network_mode']=='none'
            phases[stage]=dict(proving_seconds=report['checks']['prove']['elapsed_seconds'],verification_seconds=report['checks']['verify']['elapsed_seconds'],receipt_sha256=receipt['receipt_sha256'],receipt_bytes=receipt['receipt_bytes'])
            previous=sha(folder/'report.json')
        assert evm['status']=='LOCAL_SCENARIO_VERIFIER_PASS' and evm['receipt_sha256']==receipt['receipt_sha256']
        assert evm['image_id']==prep['program_image_id'] and evm['journal_sha256']==prep['journal_sha256']
        assert evm['preparation_sha256']==sha(base/'preparation.json') and evm['groth16_report_sha256']==previous
        assert [evm['checks'][key]['status'] for key in ['accepted','wrong_image','wrong_journal','modified_seal']]==[1,0,0,0]
        row=dict(status='PASS_TEST_ONLY' if prep['test_only_model'] else 'PASS_MAINNET_CONDITIONAL_TRANSITION',
            image_id=prep['program_image_id'],journal_sha256=prep['journal_sha256'],phases=phases,
            evm_verify_gas=evm['checks']['accepted']['gas_used'],starting_state_scope=prep['starting_state_scope'])
        if prep['test_only_model']:
            accepted=read(base/'model-acceptance.json')
            assert accepted['status']=='TEST_ONLY_STATE_PROOF_PASS' and accepted['receipt_sha256']==receipt['receipt_sha256']
            assert accepted['state']==prep['model_state'] and accepted['journal_sha256']==prep['journal_sha256']
            assert accepted['image_id']==prep['program_image_id']
            assert accepted['state_version']==1 and accepted['halted']==(name=='halt101-prefix')
            assert [accepted['checks'][key]['status'] for key in ['production_decoder_rejects_model','mainnet_image_rejects_model','accepted','replay_rejected','modified_journal_rejected']]==[0,0,1,0,0]
            row['test_receiver_gas']=accepted['checks']['accepted']['gas_used']
        scenarios[name]=row
    for path in [Path(__file__).resolve(),ROOT/'tests/zkspv/test_proof_scenarios.py',HERE/'README.md']:
        sources[str(path.relative_to(ROOT))]=sha(path)
    pause=read(HERE/'evidence/docker-pause.json');assert pause['restored']
    assert {x['name'] for x in pause['running_before']}<=set(pause['running_after'])
    cleanup=read(HERE/'evidence/final-process-check.json');assert cleanup['model_anvil_stopped'] and cleanup['no_task_prover_container']
    suites=ET.parse(ROOT/'ops/artifacts/zkspv/proof-scenario-regression.xml').getroot().findall('.//testsuite')
    assert sum(int(s.get('tests',0)) for s in suites)>=25
    assert all(int(s.get(key,0))==0 for s in suites for key in ['failures','errors','skipped'])
    artifacts={str(p.relative_to(ROOT)):sha(p) for p in (HERE/'evidence').rglob('*') if p.is_file()}
    for path in [HERE/'model-build-report.json',ROOT/'ops/artifacts/zkspv/proof-scenario-regression.xml']:
        artifacts[str(path.relative_to(ROOT))]=sha(path)
    report=dict(schema='satoshifi.proof-scenario-suite.v1',status='LOCAL_SCENARIO_PROOFS_PASS',production_ready=False,
        scenarios=scenarios,source_sha256=sources,artifact_sha256=artifacts,public_transactions=0,
        mainnet_fork_halt_qualified=False,retarget_36288='NATIVE_DEBUG_RELEASE_ONLY',
        limitations=['Fork/halt use synthetic work under a distinct TEST_ONLY image and domain, never mainnet consensus',
        'Retarget proves a conditional two-header transition from a natively replayed state, not its recursive history or production state acceptance',
        'No production profile/consumer activation or independent review'])
    write(HERE/'report.json',report);print(json.dumps(dict(status=report['status'],sources=len(sources),artifacts=len(artifacts))))

if __name__=='__main__':collect()
