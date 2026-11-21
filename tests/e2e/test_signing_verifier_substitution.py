"""Actual local EVM constructor/runtime substitution, no mock provenance checks."""
import os
from pathlib import Path

from eth_account import Account
import pytest
from web3 import Web3

from signer_agent.bootstrap import artifact
from signer_agent.config import Config
from signer_agent.coordinator import CoordinatorClient
from signer_agent.service import SigningService
from signer_agent import service as signing
from signer_agent.frost_math import scalar_base_mult
from test_dkg_processes import RPC, deploy, tx

ZERO='0x'+'00'*20


@pytest.mark.parametrize('attack',[None,'always_true','session_override','same_code_clone'])
def test_real_evm_verifier_substitution_blocks_private_signing(tmp_path,monkeypatch,attack):
    w3=Web3(Web3.HTTPProvider(RPC));assert w3.eth.chain_id==31337
    operator=w3.eth.accounts[0]
    def false_verifier():
        # Local fixture: 10-byte runtime returns ABI true for any call.
        creation='0x600a600c600039600a6000f3600160005260206000f3'
        receipt=w3.eth.wait_for_transaction_receipt(w3.eth.send_transaction({'from':operator,'data':creation}))
        assert receipt.status==1
        return receipt.contractAddress
    verifier=deploy(w3,'FROSTVerifier')
    selected=false_verifier() if attack=='always_true' else verifier.address
    coordinator=deploy(w3,'FROSTCoordinator',selected)
    tx(w3,coordinator.functions.grantRole(coordinator.functions.SESSION_CREATOR_ROLE().call(),operator))
    sender=Account.from_key((1).to_bytes(32,'big')).address
    point=scalar_base_mult(23)
    override=false_verifier() if attack=='session_override' else ZERO
    tx(w3,coordinator.functions.createSession(point.x.to_bytes(32,'big'),[sender],1,
        w3.eth.get_block('latest').timestamp+3600,True,override,1,(coordinator.address,0,0,bytes(32))))
    sid=coordinator.functions.nextSessionId().call()
    tx(w3,coordinator.functions.bindMessageHash(sid,b'm'*32))
    tx(w3,coordinator.functions.registerParticipantPublicKeyShare(sid,sender,point.x,point.y))
    if attack=='same_code_clone':
        clone=deploy(w3,'FROSTVerifier')
        old=bytes(w3.eth.get_code(coordinator.address))
        new=old.replace(bytes.fromhex(verifier.address[2:]),bytes.fromhex(clone.address[2:]))
        assert new!=old
        # Only this test-owned coordinator is changed. Runtime's immutable mask
        # still matches; original creation/constructor binding must detect it.
        response=w3.provider.make_request('anvil_setCode',[coordinator.address,Web3.to_hex(new)])
        assert 'error' not in response
        assert coordinator.functions.defaultVerifier().call()==clone.address
    state=tmp_path/'state';state.mkdir(mode=0o700)
    cfg=Config(rpc_url=RPC,sender=sender,private_key=f'{1:064x}',coordinator_address=coordinator.address,
        coordinator_abi_path=str(Path(__file__).resolve().parents[2]/'signer-agent/contracts/FROSTCoordinator.json'),
        signing_share_hex=f'{23:064x}',state_key=os.urandom(32).hex(),state_path=str(state/'signer.enc'),
        deployment_block=0,scan_chunk_size=100,confirmations=1,poll_interval=1,receipt_timeout=30,
        gas_limit=0,max_fee_gwei=None,priority_fee_gwei=1,auto_sign=False,auto_finalize=False)
    client=CoordinatorClient(cfg);session=client.session(sid)
    def forbidden(*a,**kw):pytest.fail('substitution reached nonce generation or private scalar arithmetic')
    monkeypatch.setattr(signing,'random_nonce_pair',forbidden);monkeypatch.setattr(signing.crypto,'call',forbidden)
    nonce=w3.eth.get_transaction_count(sender)
    if attack is None:client.guard_verifier(session)
    else:
        error={'always_true':'BTC_CONTRACT_BUILD_MISMATCH','session_override':'FROST_CANONICAL_VERIFIER_REQUIRED',
               'same_code_clone':'BTC_CREATION_RUNTIME_CHANGED|BTC_CONSTRUCTOR_INPUT_MISMATCH|BTC_CANONICAL_CREATION_NOT_FOUND'}[attack]
        with pytest.raises(ValueError,match=error):SigningService(cfg,client).approve(session)
    assert coordinator.functions.getSession(sid).call()[11]==0
    assert w3.eth.get_transaction_count(sender)==nonce
    assert not (state/'signer.enc').exists()
