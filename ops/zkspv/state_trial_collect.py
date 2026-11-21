"""Bind completed real state-acceptance evidence without promoting production."""
import argparse
import json
from pathlib import Path
from .state_trial import ROOT, HERE, read, write
from .wrap_local import sha


def collect(base):
    prep=read(base/'preparation.json');accepted=read(base/'acceptance.json')
    assert accepted['status']=='LOCAL_STATE_PROOF_PASS' and not accepted['production_ready']
    assert accepted['state']==prep['deployments'][0]['address']
    assert accepted['other_state']==prep['deployments'][1]['address'] and accepted['other_state']!=accepted['state']
    assert sha(base/'state.input')==prep['input_sha256']
    assert sha(base/'state.journal')==prep['journal_sha256']==accepted['journal_sha256']
    sources=dict(prep['source_sha256']);phases={};previous=None
    for stage in ['composite','succinct','groth16']:
        directory=base/stage;report=read(directory/'report.json');verified=read(directory/'verified/receipt.json')
        assert report['status']=='VERIFIED' and report['source_unchanged']
        assert report['preparation_sha256']==sha(base/'preparation.json')
        assert report['prior_report_sha256']==previous
        assert verified['receipt_kind']==stage and verified['status']=='VERIFIED'
        assert verified['image_id']==prep['program_image_id'] and verified['journal_sha256']==prep['journal_sha256']
        assert verified['receipt_sha256']==sha(directory/'receipt.bin')
        assert verified['wrong_image_rejected'] and verified['modified_journal_rejected'] and verified['dev_mode_disabled']
        for p,item in report['artifacts'].items():assert sha(directory/p)==item['sha256'],p
        for p,digest in report['source_sha256'].items():assert sha(ROOT/p)==digest,p
        assert report['checks']['network']['exit_code']==report['checks']['prove']['exit_code']==report['checks']['verify']['exit_code']==0
        phases[stage]=dict(report=str((directory/'report.json').relative_to(ROOT)),report_sha256=sha(directory/'report.json'),
            proving_seconds=report['checks']['prove']['elapsed_seconds'],verification_seconds=report['checks']['verify']['elapsed_seconds'],
            receipt_sha256=verified['receipt_sha256'],receipt_bytes=verified['receipt_bytes'])
        previous=sha(directory/'report.json')
    assert phases['groth16']['receipt_sha256']==accepted['receipt_sha256']
    assert read(base/'groth16/report.json')['container']['network_mode']=='none'
    for case,status in [('wrong_state_rejected',0),('transition_accepted',1),('replay_rejected',0),('modified_journal_rejected',0),('header_materialized',1)]:
        assert accepted['checks'][case]['status']==status
    assert accepted['state_version']==1 and accepted['accepted_height']==32257
    assert accepted['real_header_materialized'] and accepted['fresh_after_acceptance'] and accepted['stale_after_state_age']
    for p in [Path(__file__).resolve(),ROOT/'tests/zkspv/test_state_proof.py',HERE/'README.md']:
        sources[str(p.relative_to(ROOT))]=sha(p)
    for p,digest in sources.items():assert sha(ROOT/p)==digest,p
    pause=read(HERE/'evidence/docker-pause.json')
    assert pause['restored'] and {x['name'] for x in pause['running_before']} <= set(pause['running_after'])
    artifacts={str(p.relative_to(ROOT)):sha(p) for p in (HERE/'evidence').rglob('*') if p.is_file()}
    test=ROOT/'ops/artifacts/zkspv/state-proof-regression.xml';artifacts[str(test.relative_to(ROOT))]=sha(test)
    result=dict(schema='satoshifi.state-proof-integration.v1',status='LOCAL_STATE_PROOF_PASS',production_ready=False,
        source_sha256=sources,artifact_sha256=artifacts,phases=phases,state=accepted['state'],verifier=accepted['verifier'],adapter=accepted['adapter'],
        program_image_id=prep['program_image_id'],journal_sha256=prep['journal_sha256'],receipt_sha256=accepted['receipt_sha256'],
        accepted_height=32257,state_version=1,transition_gas=accepted['checks']['transition_accepted']['gas_used'],
        materialization_gas=accepted['checks']['header_materialized']['gas_used'],public_transactions=0,
        docker_services_restored=True,actual_state_transition='PASS',fresh_and_expiry='PASS_HISTORICAL_TEST_CLOCK',
        real_header_materialization='PASS',limitations=['One mainnet append, not complete fork/retarget/halt proof scenarios',
        'Historical disposable Anvil clock; no live-network freshness or SLA claim',
        'No production profile/checkpoint selection, FROST relay, monetary consumer integration or independent review'])
    write(HERE/'report.json',result)
    print(json.dumps(dict(status=result['status'],sources=len(sources),artifacts=len(artifacts),transition_gas=result['transition_gas'])))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    collect(p.parse_args().output.resolve())
