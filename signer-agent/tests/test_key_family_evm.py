"""Real local EVM/root bytecode; quorum and DKG completion are UNIT fixtures."""
import json
import os
from pathlib import Path
import pytest
from web3 import Web3,EthereumTesterProvider
try:
    from eth_tester.exceptions import TransactionFailed
except ImportError:
    if os.environ.get('SATOHSI_REQUIRE_EVM')=='1':raise
    pytest.skip('Install signer-agent[evm-test] for real local EVM tests',allow_module_level=True)


@pytest.fixture
def evm_family():
    selected=os.environ.get('SATOHSI_CONTINUITY_BUILD')
    r=json.loads(Path(selected).read_text()) if selected else None
    w=Web3(EthereumTesterProvider());sender=w.eth.accounts[0];w.eth.default_account=sender
    # eth-tester's in-process full block API uses data/to="" and snake_case;
    # normalize to ordinary JSON-RPC fields without altering executed inputs.
    from web3.datastructures import AttributeDict
    get_block=w.eth.get_block
    def canonical_block(*args,**kw):
        b=get_block(*args,**kw)
        if kw.get('full_transactions'):
            b=AttributeDict(dict(b,transactions=[AttributeDict(dict(t,to=t.get('to') or None,input=t['data'])) for t in b.transactions]))
        return b
    w.eth.get_block=canonical_block
    def deploy(name,*args):
        source='test/fixtures/FamilyRegistryHarness.sol' if name.endswith('Fixture') else 'contracts/src/federation/'+name+'.sol'
        if r is not None:c=r['contracts'][source][name];bytecode=c['evm']['bytecode']['object']
        else:
            from signer_agent.bootstrap import artifact
            c=artifact(name);bytecode=c['bytecode']['object']
        tx=w.eth.contract(abi=c['abi'],bytecode=bytecode).constructor(*args).transact()
        receipt=w.eth.wait_for_transaction_receipt(tx);assert receipt.status==1
        return w.eth.contract(address=receipt.contractAddress,abi=c['abi'])
    dkg=deploy('FamilyDkgFixture');coordinator=deploy('FamilyDkgFixture')
    auth=deploy('FamilyAuthFixture',dkg.address,coordinator.address)
    root=deploy('BitcoinKeyFamilyRegistry',auth.address)
    def graph(key=b'\x11'*32,genesis=b'\x22'*32,label=b'\x33'*32):
        sid=Web3.keccak(key+label);dkg.functions.setKey(sid,key).transact()
        return deploy('FamilyGraphFixture',dkg.address,sid,genesis,label)
    def enroll(g):
        op=root.functions.getEnrollOperationHash(g.address).call();auth.functions.approve(op,root.address).transact()
        root.functions.enroll(g.address,op).transact();return op
    return w,deploy,dkg,auth,root,graph,enroll


def test_root_requires_original_governance_and_exact_completed_key(evm_family):
    w,deploy,dkg,auth,r,graph,enroll=evm_family;g=graph();op=r.functions.getEnrollOperationHash(g.address).call()
    with pytest.raises(TransactionFailed,match='fixture approval'):r.functions.enroll(g.address,op).transact()
    enroll(g);assert r.functions.requireGraph(g.address).call()==r.functions.familyId(b'\x22'*32,b'\x11'*32).call()
    assert w.eth.get_code(r.address) and len(w.eth.get_code(r.address))<=24576


@pytest.mark.parametrize('state,derivation,closed',[(1,1,True),(3,1,True),(0,2,False)])
def test_fresh_client_same_key_resharing_and_new_graph_cannot_reset(evm_family,state,derivation,closed):
    _,_,_,_,r,graph,enroll=evm_family;g=graph();enroll(g)
    g.functions.change(r.address,state,derivation,closed,2,2).transact()
    # New registry and new DKG ceremony labels, no retained client SQLite.
    other=graph(label=b'\x44'*32)
    with pytest.raises(TransactionFailed,match='family already bound'):enroll(other)
    cross_network=graph(genesis=b'\x55'*32,label=b'\x66'*32)
    with pytest.raises(TransactionFailed,match='family already bound'):enroll(cross_network)
    with pytest.raises(TransactionFailed):r.functions.fence(g.address,True,True).call()


def test_root_shares_uniqueness_across_independent_dkg_deployments(evm_family):
    _,deploy,_,_,r,graph,enroll=evm_family;g=graph();enroll(g)
    other=deploy('FamilyDkgFixture');sid=b'\x99'*32;other.functions.setKey(sid,b'\x11'*32).transact()
    duplicate=deploy('FamilyGraphFixture',other.address,sid,b'\x22'*32,b'\x88'*32)
    with pytest.raises(TransactionFailed,match='family already bound'):enroll(duplicate)
    other.functions.setKey(sid,b'\xaa'*32).transact()
    independent=deploy('FamilyGraphFixture',other.address,sid,b'\x22'*32,b'\x88'*32)
    enroll(independent);r.functions.fence(independent.address,False,True).call()


def test_registered_child_cannot_become_a_fresh_parent_or_alias_other_family(evm_family):
    _,_,_,_,r,graph,enroll=evm_family;g=graph();enroll(g)
    g.functions.change(r.address,0,0,False,1,2).transact()
    child=b'\x77'*32;binding=b'\x88'*32
    g.functions.aliasKey(r.address,child,binding).transact()
    g.functions.aliasKey(r.address,child,binding).transact()
    with pytest.raises(TransactionFailed,match='family alias conflict'):g.functions.aliasKey(r.address,child,b'\x99'*32).transact()
    with pytest.raises(TransactionFailed,match='family already bound'):enroll(graph(key=child,label=b'\xaa'*32))
    with pytest.raises(TransactionFailed,match='family alias registrar'):r.functions.bindDerived(g.address,b'\xaa'*32,binding).transact()


def test_terminal_states_and_generation_rollback_are_atomic(evm_family):
    _,_,_,_,r,graph,enroll=evm_family;g=graph();enroll(g)
    g.functions.change(r.address,3,2,True,2,2).transact()
    for args in [(0,2,True,3,2),(3,0,True,2,3),(3,2,False,3,2),(3,2,True,1,2)]:
        with pytest.raises(TransactionFailed):g.functions.change(r.address,*args).transact()
    assert g.functions.custodyGeneration().call()==2 and g.functions.custodyState().call()==3


def test_exact_receipts_survive_nonce_advance_and_governance_rotation(evm_family):
    _,deploy,dkg,auth,r,graph,enroll=evm_family;g=graph();op=enroll(g)
    next_auth=deploy('FamilyAuthFixture',dkg.address,auth.functions.coordinator().call())
    rotate=r.functions.getRotateOperationHash(next_auth.address).call();auth.functions.approve(rotate,r.address).transact()
    r.functions.rotateGovernance(next_auth.address,rotate).transact()
    r.functions.enroll(g.address,op).transact();r.functions.rotateGovernance(next_auth.address,rotate).transact()
    with pytest.raises(TransactionFailed,match='family receipt'):r.functions.enroll(graph(label=b'\xcc'*32).address,op).transact()
    assert r.functions.governanceNonce().call()==2


def test_independent_sdk_pin_is_required_and_retained_across_fresh_clients(evm_family,tmp_path):
    from signer_agent import family_registry as m
    from signer_agent.bitcoin_continuity import Catalog
    from signer_agent.zk_worker_prover import write
    w,_,dkg,auth,r,graph,enroll=evm_family;g=graph();enroll(g)
    p=dict(version='1',chain_id=str(w.eth.chain_id),address=r.address,
        initial_authority=auth.address,dkg=dkg.address,coordinator=auth.functions.coordinator().call())
    # HexBytes.hex differs between supported versions; the policy is canonical.
    p['runtime_hash']='0x'+Web3.keccak(w.eth.get_code(r.address)).hex().removeprefix('0x')
    identity=tmp_path/'identity.json';write(identity,dict(private_key='01'*32));catalog=Catalog(identity_file=identity)
    b=w.eth.get_block('latest');s=dict(identity=dict(chain_id=p['chain_id'],family_root=r.address,registry=g.address),family=r.functions.requireGraph(g.address).call().hex(),
        anchor=dict(number=str(b.number),hash=b.hash.hex()),latest_anchor=dict(number=str(b.number),hash=b.hash.hex()))
    with pytest.raises(ValueError,match='PIN_REQUIRED'):m.guard(catalog,w,s,'BASE_SIGN')
    assert m.install(w,catalog,p,b.number)['status']=='FAMILY_ROOT_PINNED'
    m.guard(catalog,w,s,'BASE_SIGN');m.pin_path(identity).unlink()
    # Restored config/cleared policy file cannot erase the original EOA pin.
    m.guard(Catalog(identity_file=identity),w,s,'BASE_SIGN')
    other=tmp_path/'fresh.json';write(other,dict(private_key='02'*32))
    with pytest.raises(ValueError,match='PIN_REQUIRED'):m.guard(Catalog(identity_file=other),w,s,'BASE_SIGN')
    changed=dict(p,address=w.eth.accounts[8])
    write(m.pin_path(identity),changed)
    with pytest.raises(ValueError,match='PIN_CHANGED'):m.guard(catalog,w,s,'BASE_SIGN')
