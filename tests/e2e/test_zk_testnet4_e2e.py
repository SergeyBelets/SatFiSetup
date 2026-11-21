"""Real testnet4 native preparation on a local EVM with Sepolia chain ID.

Historical public Bitcoin headers/Core-shaped observations, simulated inbox need.
No ready receipt, fake proof acceptance, fresh Groth16 or public deployment claim.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import shutil
import socket
import subprocess
import sys
import time

import pytest
from web3 import Web3
from signer_agent.bootstrap import Bootstrap
from signer_agent.operations import canonical
from signer_agent.zk_peer_bundle import _manifest, _pack, _scope, export_attempt, verify_bundle
from signer_agent.zk_spv import VERIFIER_SELECTOR
from signer_agent.zk_spv import constructor_args, inspect_binding
from signer_agent.zk_profiles import profile
from signer_agent.zk_worker import Worker, main as zk_worker_main
from signer_agent.zk_worker_prover import LocalProver, sha, ROOT
from signer_agent.zk_spv_cli import transition_step
from test_network_matrix import HistoricalPublicCore
sys.path.insert(0, str(ROOT/'signer-agent/tests'))
from test_zk_worker import config
from test_zk_testnet4 import zk_policy


def test_testnet4_sepolia_bootstrap_reuse_native_worker_and_fail_closed_proof(tmp_path, monkeypatch, capsys):
    c=config(tmp_path);c.update(chain_id='11155111',bitcoin_policy=zk_policy('explicit-policy-conf'))
    c['bitcoin_policy']['max_headers_per_sync']='1'
    binary=Path(os.environ.get('SATOHSI_ZK_TESTNET4_WORKER_HOST',str(ROOT/'ops/.local-tools/zk-worker/satoshifi-testnet4-zk-worker-host')))
    assert binary.is_file(), 'Build the testnet4 worker host first'
    c.update(host_binary=str(binary),host_sha256=sha(binary))
    core=HistoricalPublicCore('testnet4');core.height=2116
    core.by_hash={core.record(h)['hash']:h for h in range(core.first,core.height+1)}
    core.policy=c['bitcoin_policy']
    timestamp=core.record(core.height)['time']
    monkeypatch.setattr('signer_agent.bitcoin_network.time.time',lambda:timestamp)
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    c['evm_rpc']=f'http://127.0.0.1:{port}'
    anvil=os.environ.get('SATOHSI_ANVIL') or shutil.which('anvil')
    assert anvil, 'Anvil required'
    log=(tmp_path/'anvil.log').open('w')
    node=subprocess.Popen([anvil,'--host','127.0.0.1','--port',str(port),'--chain-id','11155111',
        '--timestamp',str(timestamp),'--hardfork','osaka','--silent'],stdout=log,stderr=log)
    worker=None
    try:
        w3=Web3(Web3.HTTPProvider(c['evm_rpc']))
        for _ in range(100):
            if w3.is_connected():break
            time.sleep(.05)
        assert w3.is_connected()
        identity=Path(c['identity_file']);identity.write_text(json.dumps(dict(private_key='ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80')));identity.chmod(0o600)
        cookie=Path(c['bitcoin_cookie']);cookie.write_text('historical:unused');cookie.chmod(0o600)
        selected=profile(c['bitcoin_policy'])
        deployment=dict(mode='backed',bitcoin_network='testnet4',bitcoin_policy=c['bitcoin_policy'],
            chain_id='11155111',evm_rpc=c['evm_rpc'],evm_confirmations='1',deployment_id='testnet4-state',
            steps=[dict(id='relay',artifact=selected['state'],args=constructor_args(c['bitcoin_policy']),
                        children=[dict(id='spv_guard',artifact=selected['adapter'],nonce='2')])])
        # Use the production planner's child format, constructor and output binding.
        from test_zk_bootstrap import profile as bootstrap_profile, zk_invite
        from signer_agent.bootstrap_plan import build
        invite=zk_invite();invite.update(chain_id='11155111',bitcoin_network='testnet4',bitcoin_policy=c['bitcoin_policy'])
        plan=build(bootstrap_profile(invite));deployment['steps']=[next(s for s in plan['steps'] if s['id']=='relay')]
        runner=Bootstrap(deployment,tmp_path/'bootstrap.sqlite')
        assert runner.apply(identity)['status']=='VERIFIED'
        state=runner.contract('relay');c['zk_state']=state.address
        _,_,records,_=inspect_binding(w3,state.address,c['bitcoin_policy'],w3.eth.block_number)
        nonce=w3.eth.get_transaction_count(w3.eth.accounts[0])
        assert runner.apply(identity)['status']=='VERIFIED'
        assert w3.eth.get_transaction_count(w3.eth.accounts[0])==nonce
        # Complete actual DKG and ordinary consumer deployment, then pin the
        # testnet4 graph with the same participant verifier used before signing.
        from signer_agent.bootstrap import artifact
        from signer_agent.bitcoin_trust import inspect_registry
        from signer_agent.zk_spv import review_consumer
        from ops.zkspv.deposit_trial import helpers
        dkg,authorize,_,_=helpers(c['evm_rpc'])
        def deploy(name,*args):
            a=artifact(name)
            receipt=w3.eth.wait_for_transaction_receipt(w3.eth.contract(abi=a['abi'],bytecode=a['bytecode']['object']).constructor(*args).transact({'from':w3.eth.accounts[0]}))
            assert receipt.status==1
            return w3.eth.contract(address=receipt.contractAddress,abi=a['abi'])
        def tx(call):
            assert w3.eth.wait_for_transaction_receipt(call.transact({'from':w3.eth.accounts[0]})).status==1
        from signer_agent.zk_service_binding import inspect_service_view
        view=deploy(selected['view'],state.address)
        inspect_service_view(w3,view.address,c['bitcoin_policy'],w3.eth.block_number,state_address=state.address)
        assert view.functions.snapshot().call()[2] and not state.functions.isFresh().call()
        registry=deploy('DKGCoordinator');frost=deploy('FROSTVerifier')
        coordinator=deploy('FROSTCoordinator',frost.address);tx(coordinator.functions.bindDKGRegistry(registry.address))
        evm_dir=tmp_path/'evm';evm_dir.mkdir();btc_dir=tmp_path/'btc';btc_dir.mkdir()
        evm,participant,evm_public=dkg.ceremony(evm_dir,w3,registry,4,3,'EVM_AUTH')
        btc,_,btc_public=dkg.ceremony(btc_dir,w3,registry,4,3,'BTC_SPEND')
        auth=deploy('DKGAuthorization',coordinator.address,evm.session_id,bytes.fromhex(evm_public['group'])[1:],list(evm.participants),3)
        bindings=dict(records,conf_auth=dict(address=auth.address,artifact='DKGAuthorization'),dkg=dict(address=registry.address,artifact='DKGCoordinator'))
        cp=bootstrap_profile(invite);cp['btc_ceremony']['id']='0x'+btc.session_id.hex()
        consumer_plan={s['id']:s for s in build(cp)['steps']}
        consumer=Bootstrap(dict(deployment,deployment_id='testnet4-consumers',contracts=bindings,
            steps=[consumer_plan[n] for n in ('batches','escrow','deposits','mining')]),tmp_path/'consumers.sqlite')
        assert consumer.apply(identity)['status']=='VERIFIED'
        batches=consumer.contract('batches');escrow=consumer.contract('escrow');deposits=consumer.contract('deposits');mining=consumer.contract('mining')
        op=authorize(w3,coordinator,auth,participant,tmp_path,batches.address,'BitcoinBatchRegistry','getSetWithdrawalEscrowOperationHash',[escrow.address])
        tx(batches.functions.setWithdrawalEscrow(escrow.address,op))
        op=authorize(w3,coordinator,auth,participant,tmp_path,escrow.address,'BitcoinWithdrawalEscrow','getSetDepositRegistryOperationHash',[deposits.address])
        tx(escrow.functions.setDepositRegistry(deposits.address,op))
        completed=registry.functions.getCompletedResult(btc.session_id).call()
        inspected=inspect_registry(btc,dict(result_hash=bytes(completed[1]).hex(),result=btc_public),SimpleNamespace(w3=w3),batches.address,
            bitcoin_network='testnet4',bitcoin_policy=c['bitcoin_policy'],evm_confirmations='1')
        assert inspected['graph']['contracts']['spv_guard']['artifact']==selected['adapter']
        review_consumer(w3,invite,mining,'MiningPoolV1',{},w3.eth.block_number)
        c['allowed_consumers']={mining.address:'MiningPoolV1',escrow.address:'BitcoinWithdrawalEscrow'}
        worker=Worker(c);worker.bitcoin=core
        request=dict(kind='deposit',consumer=next(iter(c['allowed_consumers'])),txid='11'*32,vout='0')
        worker.inbox.enqueue(request)
        worker.observe_need=lambda *a:dict(status='NEEDS_STATE',height=2017,depth=6,header=core.raw(2017).hex())
        before_prepare = w3.eth.get_transaction_count(w3.eth.accounts[0])
        assert worker.prepare_service()['status']=='PROOF_PREPARED'
        attempt=worker.inbox.get('attempt');base=worker.inbox.path/'attempts'/attempt['id']
        journal=(base/'state.journal').read_bytes()
        assert journal[32:64]==b'bitcoin-testnet4'.ljust(32,b'\0')
        prep=json.loads((base/'preparation.json').read_text())
        assert prep['program_image_id']==selected['image']
        public_bundle = tmp_path/'peer-preparation.zip'
        exported = export_attempt(worker, public_bundle, include_proof=False)
        assert exported['status']=='ZK_BUNDLE_EXPORTED'
        reviewer = SimpleNamespace(c={key: c[key] for key in ('chain_id', 'bitcoin_policy', 'zk_state',
                                                                'evm_confirmations')}, w3=w3)
        reviewed = verify_bundle(reviewer, public_bundle)
        assert reviewed['status']=='ZK_BUNDLE_PREPARATION_REVIEWED'
        assert reviewed['publication_authorized'] is False
        config_file = tmp_path/'review.json'
        config_file.write_text(json.dumps(dict(reviewer.c, version='1', evm_rpc=c['evm_rpc'])))
        config_file.chmod(0o600)
        capsys.readouterr()
        assert zk_worker_main(['verify-bundle', '--config', str(config_file), '--bundle-file', str(public_bundle)]) == 0
        assert json.loads(capsys.readouterr().out)['bundle_sha256'] == reviewed['bundle_sha256']
        with pytest.raises(ValueError, match='ZK_PEER_BUNDLE_PROOF_REQUIRED'):
            verify_bundle(reviewer, public_bundle, require_proof=True)
        # A valid container and pin match cannot make an invalid seal acceptable.
        files = {'state.journal': journal, 'groth16.seal': VERIFIER_SELECTOR + bytes(256)}
        files['manifest.json'] = canonical(_manifest(_scope(reviewer.c), attempt['id'], files)).encode()
        rejected = tmp_path/'peer-invalid-proof.zip'
        rejected.write_bytes(_pack(files))
        with pytest.raises(ValueError, match='ZK_PEER_BUNDLE_PROOF_INVALID'):
            verify_bundle(reviewer, rejected, require_proof=True)
        assert w3.eth.get_transaction_count(w3.eth.accounts[0])==before_prepare
        with pytest.raises(ValueError,match='ZK_LOCAL_PROOF_ORIGIN_REQUIRED'):
            LocalProver(c).verify_own(base,base/'must-not-adopt')
        before=state.functions.getState().call()
        # Execute eth_call against the real pinned verifier. This is a rejection test.
        with pytest.raises(Exception):state.functions.submitTransition(journal,bytes.fromhex('50bd1769')+bytes(256)).call({'from':w3.eth.accounts[0]})
        assert state.functions.getState().call()==before and state.functions.stateVersion().call()==0
        wrong=bytearray(journal);wrong[32:64]=b'bitcoin-mainnet'.ljust(32,b'\0')
        with pytest.raises(ValueError,match='ZK_PROOF_DEPLOYMENT_MISMATCH'):
            transition_step('11155111',state.address,c['bitcoin_policy'],bytes(wrong),bytes(260))
    finally:
        if worker:worker.inbox.close()
        node.terminate()
        try:node.wait(timeout=10)
        except subprocess.TimeoutExpired:node.kill();node.wait()
        log.close()
