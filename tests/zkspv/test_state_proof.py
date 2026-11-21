"""Check domain rebinding and independent real-header membership preparation."""
import json
import hashlib
from pathlib import Path
import pytest
from ops.zkspv.state_trial import GUEST, ROOT, rebind, membership, journal_state


def inputs():
    return ((GUEST/'evidence/mainnet-32257.input').read_bytes(),
            (GUEST/'evidence/mainnet-32257.journal').read_bytes())


def test_only_deployment_address_changes():
    frame,journal=inputs();address=bytes.fromhex('5fbdb2315678afecb367f032d93f642f64180aa3')
    actual,expected=rebind(frame,journal,address)
    assert actual[:68]==frame[:68] and actual[68:88]==address and actual[88:]==frame[88:]
    assert expected[:96]==journal[:96] and expected[96:128]==bytes(12)+address and expected[128:]==journal[128:]
    assert journal_state(expected,False)==journal_state(journal,False)
    assert journal_state(expected,True)==journal_state(journal,True)


@pytest.mark.parametrize('fault',['length','chain','old_address','new_address'])
def test_unexpected_rebinding_input_rejected(fault):
    frame,journal=inputs();address=bytes([2])*20
    if fault=='length':frame=frame[:-1]
    elif fault=='chain':frame=frame[:36]+bytes(32)+frame[68:]
    elif fault=='old_address':frame=frame[:68]+bytes(20)+frame[88:]
    else:address=bytes(20)
    with pytest.raises(ValueError):rebind(frame,journal,address)


def test_real_membership_matches_guest_root_and_rejects_changed_root():
    _,journal=inputs();raw,path=membership(journal)
    assert len(raw)==80 and len(path)==64 and journal_state(journal,True)[3]==32257
    altered=bytearray(journal);altered[10*32]^=1
    with pytest.raises(ValueError,match='canonical tree'):membership(bytes(altered))


def test_real_preparation_binds_two_distinct_deployments():
    prep=json.loads((ROOT/'rust/zkspv/state-proof-integration/evidence/local-state/preparation.json').read_text())
    assert prep['status']=='PREPARED' and prep['public_transactions']==0
    assert len({d['address'] for d in prep['deployments']})==2
    assert prep['prior_genuine_wrong_address_rejected']['status']==0
    for d in prep['deployments']:
        assert d['receipt']['status']==1
        assert all(c['ordinary_match'] for c in d['graph'].values())


def test_actual_state_acceptance_is_bound_to_verified_proof():
    base=ROOT/'rust/zkspv/state-proof-integration/evidence/local-state'
    prep=json.loads((base/'preparation.json').read_text())
    accepted=json.loads((base/'acceptance.json').read_text())
    verified=json.loads((base/'groth16/verified/receipt.json').read_text())
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    assert accepted['status']=='LOCAL_STATE_PROOF_PASS' and accepted['public_transactions']==0
    assert accepted['state']==prep['deployments'][0]['address']
    assert accepted['other_state']==prep['deployments'][1]['address']!=accepted['state']
    assert accepted['journal_sha256']==verified['journal_sha256']==digest(base/'state.journal')
    assert accepted['receipt_sha256']==verified['receipt_sha256']==digest(base/'groth16/receipt.bin')
    assert verified['receipt_kind']=='groth16' and verified['dev_mode_disabled']
    assert verified['image_id']==prep['program_image_id']
    assert accepted['accepted_height']==32257 and accepted['state_version']==1
    for case,status in [('wrong_state_rejected',0),('transition_accepted',1),('replay_rejected',0),
                        ('modified_journal_rejected',0),('header_materialized',1)]:
        assert accepted['checks'][case]['status']==status
        assert len(bytes.fromhex(accepted['checks'][case]['transaction_hash'].removeprefix('0x')))==32
        assert accepted['checks'][case]['gas_used']>0
    assert accepted['fresh_after_acceptance'] and accepted['stale_after_state_age']
    assert accepted['real_header_materialized'] and not accepted['production_ready']
