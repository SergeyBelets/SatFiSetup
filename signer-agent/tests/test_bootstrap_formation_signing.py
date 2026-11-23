"""Local fixtures for formation, factory and share-token review before FROST."""

from copy import deepcopy
from types import SimpleNamespace

from eth_abi import encode
import pytest
from web3 import Web3

from signer_agent import bootstrap_formation_signing as review, bootstrap_tokens, evm_network
from signer_agent.bootstrap_formation import expected_formation


def address(value):
    return Web3.to_checksum_address(f"0x{value:040x}")


@pytest.fixture(params=[False, True], ids=["mixed", "custodians-only"])
def enrolled_formation(monkeypatch, request):
    names = ("registry", "coordinator", "fed_factory", "conf_factory", "fed_auth", "conf_auth",
             "fed_pool", "conf_pool", "validator", "fed_share", "conf_share", "governance", "router")
    a = SimpleNamespace(**{name: address(i + 10) for i, name in enumerate(names)})
    roster = [address(i) for i in range(1, 4)]
    invite = dict(version="6", operator=roster[0], participants=roster, threshold="3",
                  federation_id="fixture-fed", confederation_id="fixture-conf", min_custodians="2",
                  sessions=dict(evm_ceremony="e1" * 32, conf_ceremony="c1" * 32),
                  evm_confirmations="12")
    if request.param:
        invite.update(bitcoin_custody=dict(threshold="2"), ulr=dict(version="3"))
    expected = expected_formation(invite)
    fed, conf = expected["federation"], expected["confederation"]
    bundle = dict(registry=a.registry, onboarding=dict(invitation=invite))
    contracts = {
        a.fed_factory: ("FederationFactory", dict(
            dkgCoordinator=a.registry, frostCoordinator=a.coordinator, feeSetupAuthority=roster[0],
            reservedFederationSession=fed["session_id"], reservedFederationTerms=[fed["formation_hash"], fed["token_hash"]],
            getDKGSession=fed["session_id"],
            getFederation=[fed["id"], fed["members"], fed["threshold"], b"", a.fed_auth, a.fed_pool, a.fed_share],
        )),
        a.conf_factory: ("ConfederationFactory", dict(
            dkgCoordinator=a.registry, frostCoordinator=a.coordinator, feeSetupAuthority=roster[0],
            reservedConfederationSession=conf["session_id"],
            reservedConfederationTerms=[conf["formation_hash"], conf["token_hash"]],
            getDKGSession=conf["session_id"], federationFactory=a.fed_factory,
            getConfederation=[conf["id"], conf["federation_ids"], 1, len(conf["custodians"]), conf["threshold"],
                              conf["min_custodians"], b"", a.conf_auth, a.validator, a.conf_pool, a.conf_share],
            getCustodianMembers=deepcopy(conf["custodians"]), getFounders=[roster[0]],
            getFederationMembers=[(fed["id"], roster[0], a.fed_share)],
        )),
        a.fed_auth: ("DKGAuthorization", dict(registry=a.registry, coordinator=a.coordinator, dkgId=fed["session_id"])),
        a.conf_auth: ("DKGAuthorization", dict(registry=a.registry, coordinator=a.coordinator, dkgId=conf["session_id"])),
        a.fed_pool: ("SimpleMultiPool", dict(shareToken=a.fed_share, getFrostMultisig=a.fed_auth)),
        a.conf_pool: ("ConfederationPoolV2" if request.param else "ConfederationMultiPool",
                      dict(shareToken=a.conf_share, getFrostMultisig=a.conf_auth, custodianValidator=a.validator)),
        a.fed_share: ("MultiPoolShareToken", {**fed["token_metadata"], "multiPool": a.fed_pool}),
        a.conf_share: ("ConfederationShareV2" if request.param else "ConfederationToken",
                       {**conf["token_metadata"], "confederationPool": a.conf_pool}),
        a.governance: ("GovernanceRegistry", dict(factory=a.fed_factory)),
    }
    reads, logs = [], []
    at = 170
    block = SimpleNamespace(number=at, hash=b"Z" * 32)
    birth = SimpleNamespace(number=145, hash=b"K" * 32)
    filters = []

    def w3_contract(*, address, abi):
        class Functions:
            def __getattr__(self, name):
                def call(*, block_identifier):
                    assert block_identifier == at
                    reads.append((address, name))
                    return contracts[address][1][name]
                return lambda *_args: SimpleNamespace(call=call)
        return SimpleNamespace(address=address, functions=Functions(), abi=abi)

    def checked_runtime(_w, candidate, kind, number):
        assert number == at
        if contracts.get(candidate, (None,))[0] != kind:
            raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")

    def artifact(kind):
        return dict(abi=[{"type": "function", "name": key} for runtime, values in contracts.values()
                         if runtime == kind for key in values])

    def read_logs(query):
        filters.append(query)
        return logs

    from signer_agent import bootstrap
    monkeypatch.setattr(bootstrap, "artifact", artifact)
    monkeypatch.setattr(review, "artifact", artifact)
    monkeypatch.setattr(review, "checked_runtime", checked_runtime)
    monkeypatch.setattr(review, "birth_block", lambda _w, target, number: birth.number)
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, contract=w3_contract, get_logs=read_logs,
        get_block=lambda number: birth if number == birth.number else block))
    conf_intent = dict(artifact="ConfederationFactory", target=a.conf_factory, role="conf_ceremony",
                       hash_method="getRegisterCustodianDAOOperationHash", hash_args=[conf["id"], []],
                       operation_hash="0x" + "11" * 32, coordinator=a.coordinator)
    return SimpleNamespace(a=a, at=at, birth=birth, block=block, w3=w3, intent=conf_intent, contracts=contracts,
                           bundle=bundle, expected=expected, reads=reads, logs=logs, filters=filters)


def test_formation_checked_even_when_enrollment_uses_default_names(enrolled_formation):
    s = enrolled_formation
    assert bootstrap_tokens.review_intent(s.w3, s.intent, s.bundle, s.at)
    for share in (s.a.fed_share, s.a.conf_share):
        assert s.reads.count((share, "name")) == s.reads.count((share, "symbol")) == 1


@pytest.mark.parametrize("change,error", [
    ("session", "BOOTSTRAP_FORMATION_RESERVATION"), ("commitment", "BOOTSTRAP_FORMATION_RESERVATION"),
    ("missing_abi", "BOOTSTRAP_FORMATION_RESERVATION"), ("adopted", "BOOTSTRAP_FORMATION_MISMATCH"),
    ("threshold", "BOOTSTRAP_FORMATION_MISMATCH"), ("minimum", "BOOTSTRAP_FORMATION_MISMATCH"),
    ("member", "BOOTSTRAP_FORMATION_MISMATCH"), ("federations", "BOOTSTRAP_FORMATION_MISMATCH"),
    ("founder", "BOOTSTRAP_FORMATION_MISMATCH"), ("linked_federation", "BOOTSTRAP_FORMATION_MISMATCH"),
    ("name", "TOKEN_METADATA_MISMATCH"), ("symbol", "TOKEN_METADATA_MISMATCH"),
    ("setup_authority", "BOOTSTRAP_FORMATION_TARGET"), ("pool", "BOOTSTRAP_FORMATION_TARGET"),
    ("operation_id", "BOOTSTRAP_FORMATION_TARGET"),
])
def test_formation_mismatch_rejected_before_signing(enrolled_formation, change, error):
    s = enrolled_formation
    conf = s.contracts[s.a.conf_factory][1]
    if change == "session":
        conf["reservedConfederationSession"] = b"F" * 32
    elif change == "commitment":
        conf["reservedConfederationTerms"][1] = b"X" * 32
    elif change == "missing_abi":
        del conf["reservedConfederationTerms"]
    elif change == "adopted":
        conf["getDKGSession"] = bytes(32)
    elif change in ("minimum", "threshold"):
        conf["getConfederation"][5 if change == "minimum" else 4] += 1
    elif change == "member":
        member = conf["getCustodianMembers"][0]
        conf["getCustodianMembers"][0] = (*member[:2], b"\x03", member[3])
    elif change == "federations":
        conf["getConfederation"][1] = ["another-federation"]
    elif change == "founder":
        conf["getFounders"] = [address(100)]
    elif change == "linked_federation":
        conf["getFederationMembers"][0] = (s.expected["federation"]["id"], address(100), s.a.fed_share)
    elif change in ("name", "symbol"):
        s.contracts[s.a.conf_share][1][change] = "Other"
    elif change == "setup_authority":
        conf["feeSetupAuthority"] = address(100)
    elif change == "pool":
        s.contracts[s.a.conf_share][1]["confederationPool"] = s.a.fed_pool
    else:
        s.intent["hash_args"][0] = "different-confederation"
    with pytest.raises(ValueError, match=error):
        bootstrap_tokens.review_intent(s.w3, s.intent, s.bundle, s.at)


def test_mainnet_project_payer_checks_reservation_before_adoption(enrolled_formation):
    s = enrolled_formation
    conf = s.contracts[s.a.conf_factory][1]
    fed = s.contracts[s.a.fed_factory][1]
    conf["getDKGSession"] = bytes(32)
    fed["getDKGSession"] = bytes(32)
    del conf["getConfederation"]
    del fed["getFederation"]
    intent = dict(s.intent, artifact="CustodianDAOFeeRouter", role="evm_ceremony", target=s.a.router,
                  hash_method="getSetPayerOperationHash", hash_args=[s.a.conf_factory, True])
    assert review.review_intent(s.w3, intent, s.bundle, s.at)
    assert (s.a.conf_share, "name") not in s.reads
    assert (s.a.fed_factory, "reservedFederationTerms") in s.reads
    assert (s.a.fed_share, "name") not in s.reads
    fed["reservedFederationSession"] = bytes(32)
    with pytest.raises(ValueError, match="BOOTSTRAP_FORMATION_RESERVATION"):
        review.review_intent(s.w3, intent, s.bundle, s.at)
    fed["reservedFederationSession"] = s.expected["federation"]["session_id"]
    conf["reservedConfederationTerms"][0] = b"X" * 32
    with pytest.raises(ValueError, match="BOOTSTRAP_FORMATION_RESERVATION"):
        review.review_intent(s.w3, intent, s.bundle, s.at)


def test_independent_payer_operations_still_use_their_original_review(enrolled_formation):
    s = enrolled_formation
    intent = dict(s.intent, artifact="CustodianDAOFeeRouter", role="evm_ceremony", target=s.a.router,
                  hash_method="getSetPayerOperationHash")
    for args in ([s.a.conf_factory, False], [address(123), True]):
        assert not review.review_intent(s.w3, dict(intent, hash_args=args), s.bundle, s.at)
    assert s.reads == []


def test_legacy_ulr_treasury_keeps_its_exact_archived_verifier(enrolled_formation, monkeypatch):
    s = enrolled_formation
    address_ = address(120)
    s.contracts[address_] = ("ULRConfederationTreasury", {
        "confederationId": s.expected["confederation"]["id"], "factory": s.a.conf_factory,
    })
    def archived_runtime(w3, factory, kind, block, *, legacy_ulr_factory):
        assert w3 is s.w3 and factory == s.a.conf_factory and kind == "ConfederationFactory"
        assert block == s.at and legacy_ulr_factory
        return "hash", {"legacy_reference": "ConfederationFactoryULRLegacy2630366"}
    monkeypatch.setattr(review, "runtime_artifact", archived_runtime)
    intent = dict(s.intent, artifact="ULRConfederationTreasury", target=address_, hash_args=[])
    assert not review.review_intent(s.w3, intent, s.bundle, s.at)
    assert (s.a.conf_factory, "reservedConfederationTerms") not in s.reads


def test_pool_lookup_uses_only_canonical_birth_event_and_reviewed_factory(enrolled_formation):
    s = enrolled_formation
    topics = [
        Web3.keccak(text="ConfederationDeployed(string,address,address,address,address)"),
        Web3.keccak(text=s.expected["confederation"]["id"]),
    ]
    data = encode(["address"] * 4, [s.a.conf_auth, s.a.validator, s.a.conf_pool, s.a.conf_share])
    s.logs.extend([
        dict(address=address(100), topics=topics, data=data, blockNumber=s.birth.number, blockHash=s.birth.hash),
        dict(address=s.a.conf_factory, topics=topics, data=data, blockNumber=s.birth.number, blockHash=s.birth.hash),
    ])
    intent = dict(s.intent, artifact=s.contracts[s.a.conf_pool][0], target=s.a.conf_pool, hash_args=[])
    assert review.review_intent(s.w3, intent, s.bundle, s.at)
    assert s.filters == [{"fromBlock": s.birth.number, "toBlock": s.birth.number,
                          "topics": [Web3.to_hex(topic) for topic in topics]}]
    s.logs.pop()
    with pytest.raises(ValueError, match="BOOTSTRAP_FORMATION_ORIGIN"):
        review.review_intent(s.w3, intent, s.bundle, s.at)


def test_repeated_signing_boundary_uses_stronger_local_depth_and_rechecks_formation(enrolled_formation, monkeypatch):
    s = enrolled_formation
    session = SimpleNamespace(message_hash=bytes.fromhex(s.intent["operation_hash"][2:]), origin_contract=s.intent["target"])
    client = SimpleNamespace(w3=s.w3, config=SimpleNamespace(confirmations=15), token_metadata_context=(s.intent, s.bundle))
    depths = []
    def read_block(_w, depth):
        depths.append(depth)
        return s.block
    monkeypatch.setattr(evm_network, "read_block", read_block)
    bootstrap_tokens.guard_signing_session(client, session)
    s.contracts[s.a.conf_factory][1]["reservedConfederationTerms"][0] = b"X" * 32
    with pytest.raises(ValueError, match="BOOTSTRAP_FORMATION_RESERVATION"):
        bootstrap_tokens.guard_signing_session(client, session)
    assert depths == [15, 15]
