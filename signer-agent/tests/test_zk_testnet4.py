"""Network-bound testnet4 guest, deployment and journal regression tests."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from web3 import Web3

from signer_agent.bitcoin_network import TESTNET4_GENESIS, MAINNET_GENESIS, validate_policy
from signer_agent.zk_profiles import MAINNET_IMAGE, profile as zk_profile
from signer_agent.zk_spv import policy_from_checkpoint, checkpoint_descriptor, constructor_args
from signer_agent.zk_spv_cli import transition_step

ROOT=Path(__file__).resolve().parents[2]


def relay_policy():
    folder=ROOT/'web3/test/fixtures/testnet4'
    headers=(folder/'headers-2006-4050.bin').read_bytes()
    records=(folder/'expected-2006-4050.bin').read_bytes()
    raw=lambda h:headers[(h-2006)*80:(h-2006+1)*80].hex()
    return dict(version='1',network='testnet4',genesis_hash=TESTNET4_GENESIS,
        checkpoint_header=raw(2016),checkpoint_height='2016',
        checkpoint_chainwork=records[10*68+32:10*68+64].hex(),
        checkpoint_predecessors=[raw(h) for h in range(2006,2016)],max_reorg_depth='12',
        max_tip_age_seconds='86400',max_relay_lag_blocks='0',max_headers_per_sync='64')


def zk_policy(identifier='conf-a'):
    return policy_from_checkpoint(relay_policy(),identifier,'3600','7200')


def test_testnet4_uses_distinct_pinned_program_contract_and_checkpoint_domain():
    p=zk_policy();selected=zk_profile(p)
    assert selected['state']=='ZkBitcoinTestnet4StateV2'
    assert selected['view']=='ZkTestnet4SPVServiceStateView'
    assert selected['adapter']=='ZkTestnet4SpvAdapter' and selected['image']!=MAINNET_IMAGE
    assert constructor_args(p)[0][1].hex()==selected['image']
    source=relay_policy();source.update(network='mainnet',genesis_hash=MAINNET_GENESIS)
    mainnet=policy_from_checkpoint(source,'conf-a','3600','7200')
    assert checkpoint_descriptor(p)!=checkpoint_descriptor(mainnet)
    assert relay_policy()['version']=='1'  # Candidate construction never mutates its input.


@pytest.mark.parametrize('attack',['mainnet_image','mainnet_network','wrong_image','regtest'])
def test_cross_network_program_policy_is_rejected(attack):
    p=zk_policy()
    if attack=='mainnet_image':p['zk_spv']['program_image_id']=MAINNET_IMAGE
    elif attack=='mainnet_network':p.update(network='mainnet',genesis_hash=MAINNET_GENESIS)
    elif attack=='wrong_image':p['zk_spv']['program_image_id']='12'*32
    else:p['network']='regtest'
    with pytest.raises(ValueError):validate_policy(p)


def test_native_journal_matches_sdk_and_wrong_network_is_refused_before_submission():
    value=json.loads((ROOT/'rust/zkspv/state-adapter/fixtures/testnet4-state-v2.json').read_text())
    journal=bytes.fromhex(value['journal']);p=zk_policy()
    assert journal[128:160]==checkpoint_descriptor(p)
    # A zero seal here tests only typed submission encoding, never proof acceptance.
    from signer_agent.zk_spv import VERIFIER_SELECTOR
    step=transition_step('31337','0x'+'11'*20,p,journal,VERIFIER_SELECTOR+bytes(256))
    assert step['method']=='submitTransition'
    bad=bytearray(journal);bad[32:64]=b'bitcoin-mainnet'.ljust(32,b'\0')
    with pytest.raises(ValueError,match='ZK_PROOF_DEPLOYMENT_MISMATCH'):
        transition_step('31337','0x'+'11'*20,p,bytes(bad),bytes(260))


def test_policy_cli_is_offline_and_refuses_overwriting(tmp_path,capsys):
    from signer_agent.zk_spv_cli import main
    source=tmp_path/'source.json';source.write_text(json.dumps(relay_policy()))
    target=tmp_path/'policy.json'
    args=['policy','--source-policy',str(source),'--confederation-id','conf-a',
        '--max-state-age-seconds','3600','--max-tip-lag-seconds','7200','--output',str(target)]
    main(args)
    assert json.loads(target.read_text())==zk_policy()
    with pytest.raises(FileExistsError):main(args)


def test_testnet4_worker_template_has_working_v2_schema_after_personal_bindings(tmp_path):
    from signer_agent.zk_worker import load_config
    c=json.loads((ROOT/'config/zkspv-testnet4-worker.example.json').read_text())
    c['evm_rpc']='https://sepolia.example.test/participant-key'
    c['bitcoin_policy']=zk_policy()
    for key in ('directory','identity_file','host_binary','bitcoin_cookie'):
        c[key]=str(tmp_path/key)
    c['host_sha256']='12'*32
    c['zk_state']=Web3.to_checksum_address('0x'+'11'*20)
    c['allowed_consumers']={Web3.to_checksum_address('0x'+'22'*20):'MiningPoolV1'}
    path=tmp_path/'worker.json';path.write_text(json.dumps(c));path.chmod(0o600)
    assert load_config(path)==c
    assert c['heartbeat']['enabled'] and c['heartbeat']['continuous_fresh']


@pytest.mark.parametrize('action',['plan','verify'])
def test_deferred_freshness_cannot_be_used_to_mark_graph_verified(tmp_path,action):
    from signer_agent.bootstrap import main
    path=tmp_path/'profile.json';path.write_text(json.dumps({'steps':[]}))
    with pytest.raises(ValueError,match='BOOTSTRAP_ZK_BINDING_OPTIONS'):
        main([action,'--profile',str(path),'--state',str(tmp_path/'journal.sqlite'),'--defer-zk-freshness'])
