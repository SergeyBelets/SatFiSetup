"""Mint trust is separate from Bitcoin signing; provenance boundaries are fixtures."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from signer_agent import address_mint, bitcoin_trust, contract_provenance
from signer_agent.bootstrap import artifact
from signer_agent.secure_state import SecureState
from test_security_crypto_review import graph, address


@pytest.fixture
def mint_graph(graph, monkeypatch):
    g=graph; where=address(200)
    g.names["address_registry"]=where
    g.codes[where]=bytes.fromhex(artifact("BitcoinAddressRegistry")["deployedBytecode"]["object"].removeprefix("0x"))
    g.methods["deposits"]["addressRegistry"]=where
    g.methods["address_registry"]={}
    # This fixture isolates graph routing, not verifier qualification/provenance.
    identity=dict(graph_id="11"*32,custody_scope="22"*32,derivation_scope="33"*32)
    monkeypatch.setattr(address_mint,"registry_identity",lambda *args:(
        SimpleNamespace(address=where),deepcopy(identity),[],{}))
    monkeypatch.setattr(contract_provenance,"check_provenance_anchors",lambda *_:None)
    # Continuity's full canonical fields are covered separately; this fixture
    # deliberately replaces the entire address-registry identity boundary.
    monkeypatch.setattr('signer_agent.bitcoin_continuity.trust_guard',lambda *a,**kw:None)
    return g


def test_new_graph_requires_explicit_mint_profile_and_cannot_enroll_legacy_signer(mint_graph):
    g=mint_graph
    with pytest.raises(ValueError,match="BTC_ADDRESS_SIGNING_PROFILE_REQUIRED"):
        bitcoin_trust.inspect_registry(g.manifest,g.key,g.registry,g.names["batches"])
    public=bitcoin_trust.inspect_registry(g.manifest,g.key,g.registry,g.names["batches"],mint_only=True)
    assert public["graph"]["usage"]=="MINT_ONLY_V1"
    assert public["graph"]["contracts"]["address_registry"]["address"]==g.names["address_registry"]


def test_mint_pin_does_not_grant_bitcoin_signing_trust(mint_graph,tmp_path):
    g=mint_graph;state=SecureState(str(tmp_path/"state"),"11"*32)
    public=bitcoin_trust.inspect_registry(g.manifest,g.key,g.registry,g.names["batches"],mint_only=True)
    bitcoin_trust.pin_registry(g.manifest,g.key,g.registry,state,g.names["batches"],public["fingerprint"],mint_only=True)
    assert state.get("btc-registry:"+g.manifest.context.hex()) is None
    assert bitcoin_trust.require_registry(g.manifest,g.key,g.registry,state,g.names["batches"],mint_only=True)==public
    with pytest.raises(ValueError,match="BTC_REGISTRY_NOT_PINNED"):
        bitcoin_trust.require_registry(g.manifest,g.key,g.registry,state,g.names["batches"])
    # Copying public trust data into the signer namespace still cannot bypass
    # the fresh default-profile inspection.
    state.put("btc-registry:"+g.manifest.context.hex(),public)
    with pytest.raises(ValueError,match="BTC_ADDRESS_SIGNING_PROFILE_REQUIRED"):
        bitcoin_trust.require_registry(g.manifest,g.key,g.registry,state,g.names["batches"])


def test_mint_profile_cannot_be_applied_to_a_legacy_graph(graph):
    with pytest.raises(ValueError,match="BTC_ADDRESS_REGISTRY_REQUIRED"):
        bitcoin_trust.inspect_registry(graph.manifest,graph.key,graph.registry,graph.names["batches"],mint_only=True)


def test_address_signing_requires_explicit_primary_pin_and_keeps_mint_trust_separate(mint_graph,tmp_path):
    g=mint_graph;state=SecureState(str(tmp_path/"state"),"11"*32)
    public=bitcoin_trust.inspect_registry(g.manifest,g.key,g.registry,g.names["batches"],address_signing=True)
    assert public["graph"]["usage"]=="BTC_INPUT_KEYS_V1"
    bitcoin_trust.pin_registry(g.manifest,g.key,g.registry,state,g.names["batches"],public["fingerprint"],address_signing=True)
    assert bitcoin_trust.require_registry(g.manifest,g.key,g.registry,state,g.names["batches"])==public
    assert not state.get("btc-mint-registry:"+g.manifest.context.hex())
    with pytest.raises(ValueError,match="BTC_REGISTRY_PROFILE_CONFLICT"):
        bitcoin_trust.inspect_registry(g.manifest,g.key,g.registry,g.names["batches"],mint_only=True,address_signing=True)


def test_evm_address_signing_requires_enrolled_invitation_before_public_dkg_lookup():
    with pytest.raises(ValueError,match="BTC_ADDRESS_ENROLLMENT_REQUIRED"):
        address_mint.signing_graph({},None)


@pytest.mark.parametrize('attack',[None,'representative','threshold'])
def test_address_mint_public_graph_uses_enrolled_bitcoin_custodians(monkeypatch,attack):
    from test_address_mint import Functions
    representative=address(90);custodians=[address(91),address(92)]
    identifier=b'b'*32;epoch=b'e'*32
    result=[b'c'*32,b'r'*32,bytes(32),bytes(32),b'g'*32,
        [representative,*custodians] if attack=='representative' else custodians,1 if attack=='threshold' else 2]
    dkg=SimpleNamespace(functions=Functions(dict(getCompletedResult=lambda _:result)))
    w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337,contract=lambda **kw:dkg))
    pool=SimpleNamespace(w3=w3)
    registry=SimpleNamespace(functions=Functions(dict(dkg=address(5),walletId=identifier)))
    ctx=dict(registry=registry,batch=SimpleNamespace(address=address(6)))
    monkeypatch.setattr(address_mint,'configuration',lambda *args:ctx)
    calls=[]
    def inspect(manifest,key,*args,**kwargs):
        calls.append(manifest)
        assert list(manifest.participants)==custodians and manifest.threshold==2
        return dict(graph=dict(reviewed=True))
    monkeypatch.setattr(bitcoin_trust,'inspect_registry',inspect)
    invite=dict(version='6',bitcoin_network='regtest',evm_confirmations='1',
        participants=[representative,*custodians],threshold='3',
        sessions=dict(btc_ceremony=identifier.hex(),conf_ceremony=epoch.hex()),
        bitcoin_custody=dict(version='1',mode='custodians',threshold='2',sources=[]))
    bundle=dict(onboarding=dict(invitation=invite))
    context=dict(pool=pool,anchor=SimpleNamespace(number=7),dkg_id=epoch)
    if attack:
        with pytest.raises(ValueError,match='BTC_MINT_ADMISSION_GRAPH_MISMATCH'):
            address_mint.signing_graph(bundle,context)
        assert not calls
    else:
        assert address_mint.signing_graph(bundle,context)==dict(reviewed=True)


@pytest.mark.parametrize("override",["--recipient","--amount","--mint-only"])
def test_address_cli_has_no_recipient_amount_or_signer_trust_override(override):
    from signer_agent.participant_cli import main
    with pytest.raises(SystemExit) as caught:
        main(["btc-address-mint-propose","--intent-id","11"*32,"--txid","22"*32,
              "--vout","0","--deadline","100",override,"1"])
    assert caught.value.code==2
