"""Keep genuine-mainnet and synthetic-policy proof evidence distinguishable."""
import hashlib
import json
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[2]
HERE=ROOT/'rust/zkspv/proof-scenarios'
def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def words(j):return [j[i:i+32] for i in range(0,len(j),32)]

@pytest.mark.parametrize('height',[34272,36288])
def test_actual_retarget_calculation_and_first_epoch_header_are_in_input(height):
    base=HERE/'evidence/native-debug';frame=(base/f'retarget-{height}.input').read_bytes()
    assert int.from_bytes(frame[:4],'little')==len(frame)-4
    assert int.from_bytes(frame[404:406],'big')==2
    at=406;headers=[]
    for _ in range(2):
        n=int.from_bytes(frame[at:at+2],'big');assert n==80
        headers.append(frame[at+2:at+2+n]);at+=n+2
    history=(ROOT/'web3/test/fixtures/mainnet/headers-32246-36288.bin').read_bytes()
    for h,raw in zip([height-1,height],headers):assert raw==history[(h-32246)*80:(h-32245)*80]
    assert headers[0][72:76]!=headers[1][72:76]
    journal=words((base/f'retarget-{height}.journal').read_bytes())
    assert int.from_bytes(journal[12],'big')==height-2
    assert int.from_bytes(journal[16],'big')==height
    assert int.from_bytes(journal[21],'big')==2
    for suffix in ['input','journal']:
        assert (base/f'retarget-{height}.{suffix}').read_bytes()==(HERE/f'evidence/native-release/retarget-{height}.{suffix}').read_bytes()

@pytest.mark.parametrize('name',['fork-work-reorg100','halt101-prefix'])
def test_model_domains_images_and_deployment_binding_cannot_be_confused_with_mainnet(name):
    base=HERE/'evidence'/name;prep=read(base/'preparation.json');frame=(base/'state.input').read_bytes();journal=(base/'state.journal').read_bytes()
    assert prep['test_only_model'] and not prep['production_ready']
    assert prep['program_image_id']!=read(ROOT/'rust/zkspv/guest-integration/report.json')['program_image_id']
    assert journal[:32].rstrip(b'\0')==b'SATOSHIFI_TEST_POLICY_V1'
    assert journal[32:64].rstrip(b'\0')==b'synthetic-no-pow'
    assert frame[68:88]==bytes.fromhex(prep['model_state'][2:])==journal[108:128]
    assert sha(base/'state.input')==prep['input_sha256'] and sha(base/'state.journal')==prep['journal_sha256']
    original=(HERE/'evidence/model-debug'/f'{name}.input').read_bytes()
    assert frame[:68]==original[:68] and frame[88:]==original[88:]
    w=words(journal)
    if name=='halt101-prefix':
        assert [int.from_bytes(w[i],'big') for i in [20,21,26,27,31]]==[3,2,1,2,101]
        assert int.from_bytes(w[16],'big')==32357
    else:
        assert int.from_bytes(w[12],'big')==32356 and int.from_bytes(w[16],'big')==32257
        assert int.from_bytes(w[17],'big')>int.from_bytes(w[13],'big')
        assert int.from_bytes(w[26],'big')==0 and int.from_bytes(w[21],'big')==3

@pytest.mark.parametrize('name',['retarget-34272','fork-work-reorg100','halt101-prefix'])
def test_completed_proof_and_evm_evidence_agree(name):
    base=HERE/'evidence'/name;prep=read(base/'preparation.json');evm=read(base/'evm.json');verified=read(base/'groth16/verified/receipt.json')
    assert evm['status']=='LOCAL_SCENARIO_VERIFIER_PASS' and verified['status']=='VERIFIED'
    assert evm['image_id']==verified['image_id']==prep['program_image_id']
    assert evm['journal_sha256']==verified['journal_sha256']==sha(base/'state.journal')
    assert evm['receipt_sha256']==verified['receipt_sha256']==sha(base/'groth16/receipt.bin')
    assert [evm['checks'][key]['status'] for key in ['accepted','wrong_image','wrong_journal','modified_seal']]==[1,0,0,0]
    if prep['test_only_model']:
        result=read(base/'model-acceptance.json');assert result['status']=='TEST_ONLY_STATE_PROOF_PASS'
        assert result['state']==prep['model_state'] and result['state_version']==1
        assert [result['checks'][key]['status'] for key in ['production_decoder_rejects_model','mainnet_image_rejects_model','accepted','replay_rejected','modified_journal_rejected']]==[0,0,1,0,0]
        assert result['halted']==(name=='halt101-prefix')
