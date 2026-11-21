"""Custodian-only monetary epochs are committed before any independent DKG."""
from copy import deepcopy
from types import SimpleNamespace as NS
import pytest
from web3 import Web3
from signer_agent.bootstrap_custody import validate, role_inputs, sources_args, guard_signing_session
from signer_agent.bootstrap_plan import build
from signer_agent.onboarding import assemble, validate_invite, verify_profile
from test_bootstrap_system import full_invite, NETWORKS
from test_onboarding_mainnet import signed_enrollments


def policy():
    return dict(version="1", mode="custodians", threshold="2", sources=[])


@pytest.mark.parametrize('actor',['operator','delegate','outsider','none','rpc_failure'])
def test_public_intent_permission_uses_same_snapshot_and_fails_closed(actor):
    from signer_agent.onchain_transport import can_publish_intent
    reads=[]
    def reply(kind,value):
        def read(**kwargs):
            reads.append((kind,kwargs))
            if actor=='rpc_failure' and kind=='delegate':raise RuntimeError('UNAVAILABLE_PERMISSION_EVIDENCE')
            return value
        return NS(call=read)
    board=NS(functions=NS(operator=lambda:reply('operator','original'),
        canPublishIntent=lambda who:reply('delegate',actor=='delegate' and who=='custodian')))
    who=None if actor=='none' else 'original' if actor=='operator' else 'custodian'
    if actor=='rpc_failure':
        with pytest.raises(RuntimeError,match='UNAVAILABLE_PERMISSION_EVIDENCE'):can_publish_intent(board,who,123)
    else:
        assert can_publish_intent(board,who,123)==(actor in ('operator','delegate'))
    assert all(options==dict(block_identifier=123) for _,options in reads)
    if actor in ('none','operator'):assert len(reads)==(0 if actor=='none' else 1)


def custody_invite(*args):
    invite = full_invite(*args)
    count = len(invite["participants"]) - 1
    threshold = str(max(2, (7 * count + 9) // 10))
    invite["bitcoin_custody"] = dict(policy(), threshold=threshold)
    invite["min_custodians"] = threshold
    return invite


@pytest.mark.parametrize("chain,network,backend", NETWORKS)
@pytest.mark.parametrize("mode", ["create", "reuse"])
def test_custodians_alone_own_bitcoin_and_evm_authorization(chain, network, backend, mode):
    invite = custody_invite(chain, network, backend, mode)
    validate_invite(invite)
    profile = assemble(invite, signed_enrollments(invite))
    verify_profile(profile)
    profile["onchain_board"] = Web3.to_checksum_address("0x" + "ab" * 20)
    steps = {s["id"]: s for s in build(profile)["steps"]}
    assert profile["evm_ceremony"]["participants"] == invite["participants"]
    for role in ("conf_ceremony", "btc_ceremony"):
        assert profile[role]["participants"] == invite["participants"][1:]
        assert invite["operator"] not in profile[role]["participants"]
        assert profile[role]["threshold"] == invite["bitcoin_custody"]["threshold"]
        assert len(profile[role]["transport_keys"]) == len(invite["participants"]) - 1
        assert steps[role + "_create"]["args"][1] == invite["participants"][1:]
    assert steps["adopt_confederation"]["method"] == "adoptCustodianConfederationDKG"
    assert [m[1] for m in steps["adopt_confederation"]["args"][2]] == invite["participants"][1:]
    assert steps["batches"]["args"][0] == "$conf_auth"
    assert steps["mint_admission"]["args"] == ["$mining", invite["participants"][1]]
    assert steps["enable_custodian_publishers"]["args"] == [profile["conf_ceremony"]["id"]]
    assert steps["bind_intent_coordinator"]["args"] == ["$coordinator"]
    assert steps["require_local_custody"]["args"][0] == "$conf_pool"
    assert steps["custody_transfer"]["args"] == ["$mining", "$conf_pool", []]
    order = list(steps)
    # No signing session or publisher delegation can precede its immutable
    # coordinator pin, regardless of backend, network or create/reuse policy.
    binding = order.index("bind_intent_coordinator")
    assert order.index("bind_onchain_transport") < binding
    for index, step in enumerate(steps.values()):
        if step.get("method") in ("createCeremony", "createSessionFromDKG", "publishIntent", "enableCustodianPublishers"):
            assert binding < index, step["id"]
    assert order.index("require_local_custody") < order.index("add_mining")
    assert order.index("bind_custody_transfer") < order.index("add_mining")


def test_signed_custody_policy_cannot_be_changed_injected_or_removed():
    invite = custody_invite()
    entries = signed_enrollments(invite)
    profile = assemble(invite, entries)
    for action in ("remove", "change", "roster", "threshold"):
        changed = deepcopy(profile)
        if action == "remove": del changed["bitcoin_custody"]
        elif action == "change": changed["bitcoin_custody"] = dict(changed["bitcoin_custody"], mode="federation")
        elif action == "roster": changed["btc_ceremony"]["participants"] = invite["participants"]
        else: changed["btc_ceremony"]["threshold"] = "1"
        with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"): verify_profile(changed)
    old = full_invite(); profile = assemble(old, signed_enrollments(old))
    profile["bitcoin_custody"] = policy()
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"): verify_profile(profile)


@pytest.mark.parametrize("field,value", [("threshold", "02"), ("threshold", "1"), ("threshold", "4"),
    ("threshold", 2), ("mode", "shared"), ("version", "2"), ("sources", None)])
def test_invalid_custody_policy_fails_before_enrollment(field, value):
    invite = custody_invite(); invite["bitcoin_custody"][field] = value
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_"): validate_invite(invite)


def test_minimum_two_custodians_and_sufficient_separate_custodian_approvals():
    invite = custody_invite()
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_THRESHOLD"):
        validate(invite["bitcoin_custody"], invite["participants"][:2], "2")
    invite["min_custodians"] = "1"
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_THRESHOLD"): validate_invite(invite)


def test_direct_profile_cannot_put_representative_back_in_custody_key():
    invite = custody_invite()
    profile = assemble(invite, signed_enrollments(invite)); del profile["onboarding"]
    profile["conf_ceremony"]["participants"] = profile["evm_ceremony"]["participants"]
    profile["conf_ceremony"]["transport_keys"] = profile["evm_ceremony"]["transport_keys"]
    with pytest.raises(ValueError, match="BOOTSTRAP_CUSTODIAN_ROSTER"): build(profile)


def test_pinned_source_schema_and_order():
    source = dict(escrow=Web3.to_checksum_address("0x" + "ab" * 20),
                  token=Web3.to_checksum_address("0x" + "bc" * 20),
                  **{field: "0x" + "12" * 32 for field in
                     ("escrow_hash", "token_hash", "mining_hash", "deposit_hash", "batch_hash")})
    value = policy(); value["sources"] = [source]
    validate(value)
    assert sources_args(value)[0][:2] == [source["escrow"], source["token"]]
    for attack in ("zero_hash", "missing", "extra", "duplicate", "zero_address"):
        changed = deepcopy(value)
        if attack == "zero_hash": changed["sources"][0]["batch_hash"] = "0x" + "00" * 32
        elif attack == "zero_address": changed["sources"][0]["token"] = "0x" + "00" * 20
        elif attack == "missing": del changed["sources"][0]["mining_hash"]
        elif attack == "extra": changed["sources"][0]["verifier"] = source["escrow"]
        else: changed["sources"].append(source)
        with pytest.raises(ValueError, match="BITCOIN_CUSTODY_"): validate(changed)


def test_no_policy_preserves_historical_roster():
    invite = full_invite()
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        assert role_inputs(invite, role) == (invite["participants"], invite["threshold"])


@pytest.mark.parametrize("attack", [None, "escrow_runtime", "different_batch", "different_verifier"])
def test_in_kind_first_escrow_binding_reviews_enrolled_graph(monkeypatch, attack):
    from signer_agent import bootstrap_custody, bootstrap, bitcoin_trust, contract_provenance, dkg_protocol, zk_spv

    invite = custody_invite()
    invite["ulr"] = {"version": "3", "default_miner_fee_sats": "100"}
    manifest = NS(session_id=bytes.fromhex(invite["sessions"]["conf_ceremony"]),
                  participants=invite["participants"][1:], threshold=int(invite["bitcoin_custody"]["threshold"]),
                  registry=Web3.to_checksum_address(f"0x{1:040x}"))
    coordinator, authorization, batch, escrow, relay = [
        Web3.to_checksum_address(f"0x{index:040x}") for index in range(2, 7)]
    values = {
        batch: dict(authorization=authorization),
        escrow: dict(batches=relay if attack == "different_batch" else batch, spv=relay,
                     depth=int(invite["bitcoin_confirmations"])),
        authorization: dict(registry=manifest.registry, coordinator=coordinator,
                            getParticipants=manifest.participants, threshold=manifest.threshold,
                            dkgId=manifest.session_id),
        manifest.registry: dict(getCompletedResult=[None, None, None, None, b"group"]),
    }

    def contract(address, **_):
        def getter(name):
            return lambda *args: NS(call=lambda *, block_identifier: values[address][name])
        return NS(address=address, functions=type("Getters", (), {"__getattr__": lambda self, name: getter(name)})())

    def checked(w3, address, kind, block):
        assert block == 7
        if kind == "BitcoinWithdrawalEscrowV2" and attack == "escrow_runtime":
            raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")

    def prove(w3, address, kind, args, block):
        if kind == "BitcoinWithdrawalEscrowV2":
            assert args == [batch, relay, int(invite["bitcoin_confirmations"]),
                            int(invite["min_p2wpkh_sats"]), int(invite["min_p2tr_sats"]), 100]
        return dict(method="direct", sender=invite["operator"])

    checked_verifiers = []

    def verify_spv(w3, invite, consumer, kind, intent, block):
        assert consumer.address == escrow and kind == "BitcoinWithdrawalEscrowV2"
        assert block == 7
        checked_verifiers.append(consumer.address)
        if attack == "different_verifier":
            raise ValueError("ZK_CONSUMER_BINDING")

    monkeypatch.setattr(bootstrap_custody, "review_publishers", lambda *args: None)
    monkeypatch.setattr(dkg_protocol, "Manifest", NS(from_dict=lambda _: manifest))
    monkeypatch.setattr(bootstrap, "artifact", lambda _: dict(abi=[]))
    monkeypatch.setattr(bitcoin_trust, "checked_runtime", checked)
    monkeypatch.setattr(contract_provenance, "prove_creation", prove)
    monkeypatch.setattr(zk_spv, "review_consumer", verify_spv)
    w3 = NS(eth=NS(contract=contract))
    intent = dict(artifact="BitcoinBatchRegistry", hash_method="getSetWithdrawalEscrowOperationHash",
                  target=batch, coordinator=coordinator, hash_args=[escrow])
    bundle = dict(role="conf_ceremony", onboarding=dict(invitation=invite))
    if attack is None:
        bootstrap_custody.review_intent(w3, intent, bundle, 7)
        assert checked_verifiers == [escrow]
    else:
        with pytest.raises(ValueError, match="BTC_CONTRACT_BUILD|BITCOIN_CUSTODY_GATEWAY|ZK_CONSUMER"):
            bootstrap_custody.review_intent(w3, intent, bundle, 7)


@pytest.mark.parametrize("attack", ["message", "target", "reorg", "source"])
def test_rechecks_operation_sources_and_canonical_snapshot_at_private_boundaries(monkeypatch, attack):
    from signer_agent import bootstrap_custody, evm_network
    target = Web3.to_checksum_address("0x" + "ab" * 20)
    intent = dict(operation_hash="0x" + "12" * 32, target=target)
    block = NS(number=7, hash=b"a" * 32)
    client = NS(custody_context=(intent, {}, "2"), w3=NS(eth=NS(get_block=lambda _: {"hash": b"b" * 32 if attack == "reorg" else block.hash})))
    session = NS(message_hash=b"\x12" * 32, origin_contract=target)
    if attack == "message": session.message_hash = b"x" * 32
    if attack == "target": session.origin_contract = "0x" + "cd" * 20
    monkeypatch.setattr(evm_network, "read_block", lambda *a: block)
    def review(*args):
        if attack == "source": raise ValueError("BITCOIN_CUSTODY_SOURCE_CODE")
    monkeypatch.setattr(bootstrap_custody, "review_intent", review)
    with pytest.raises(ValueError, match="BITCOIN_CUSTODY_"): guard_signing_session(client, session)


def test_representative_flow_never_loads_or_creates_a_custodian_private_state(tmp_path, monkeypatch):
    from signer_agent import flow_cli, spv_automation
    representative, custodian = "representative", "custodian"
    (tmp_path / "evm_ceremony.enc").write_bytes(b"unit-private-boundary")
    args = NS(directory=str(tmp_path), identity_file="identity", board="board", rpc="rpc",
              bitcoin_rpc=None, bitcoin_cookie=None)
    calls = []
    def invoke(group, values):
        if group == "setup":
            return dict(ceremonies={r: dict(status="IN_PROGRESS") for r in flow_cli.ROLES},
                role_participants={"evm_ceremony": [representative, custodian],
                                   "conf_ceremony": [custodian], "btc_ceremony": [custodian]})
        calls.append(values[values.index("--role") + 1])
        return dict(status="ACTIVE")
    monkeypatch.setattr(flow_cli, "invoke", invoke)
    monkeypatch.setattr(spv_automation, "identity", lambda _: representative)
    result, complete = flow_cli.dkg_tick(args)
    assert complete and calls == ["evm_ceremony"]
    assert result["ceremonies"]["btc_ceremony"]["status"] == "NOT_A_PARTICIPANT"
    assert not (tmp_path / "btc_ceremony.enc").exists()


@pytest.mark.parametrize("source_funding", [False, True])
def test_unaccepted_handover_output_never_becomes_treasury_surplus(monkeypatch, source_funding):
    from signer_agent import bitcoin_trust
    from signer_agent.bitcoin_onchain_payment import protect_unminted_deposit
    origin = Web3.to_checksum_address("0x" + "ab" * 20)
    gateway_address = Web3.to_checksum_address("0x" + "bc" * 20)
    reads = []
    def call(value):
        def read(**kwargs):
            assert kwargs == dict(block_identifier=123)
            return value
        return NS(call=read)
    gateway = NS(functions=NS(isSourceFunding=lambda raw: call(source_funding)))
    mining = NS(functions=NS(custodyTransfer=lambda: call(gateway_address)))
    w3 = NS(eth=NS(contract=lambda **kw: mining if kw["address"] == origin else gateway))
    deposits = NS(w3=w3, functions=NS(backingSats=lambda _: call(0), walletOutput=lambda _: call(False),
        scriptPools=lambda _: call(origin)))
    txid = "aa" * 32
    bitcoin = NS(call=lambda *args: dict(txid=txid, hex="00", vout=[]))
    monkeypatch.setattr(bitcoin_trust, "checked_runtime", lambda w, address, kind, block: reads.append((kind, block)))
    item = dict(outpoint=txid + ":0", script="5120" + "11" * 32)
    if source_funding:
        with pytest.raises(ValueError, match="BTC_UNACCEPTED_CUSTODY_TRANSFER"):
            protect_unminted_deposit(deposits, item, bitcoin, 123)
    else:
        protect_unminted_deposit(deposits, item, bitcoin, 123)
    assert reads == [("MiningPoolV1", 123), ("BitcoinCustodyTransfer", 123)]


@pytest.mark.parametrize("attack", [None, "missing_board", "wrong_epoch", "wrong_registry", "wrong_coordinator"])
def test_public_intents_cannot_remain_under_representative_veto(monkeypatch, attack):
    from signer_agent import bitcoin_trust, bootstrap_custody
    from signer_agent.dkg_protocol import Manifest
    address = Web3.to_checksum_address("0x" + "ab" * 20)
    registry = Web3.to_checksum_address("0x" + "bc" * 20)
    epoch = b"e" * 32
    bundle = dict(onchain_board=address, onboarding=dict(invitation=dict(sessions=dict(conf_ceremony=epoch.hex()))))
    def call(value): return NS(call=lambda **kwargs: value)
    board = NS(functions=NS(custodyPublisherEpoch=lambda: call(b"x" * 32 if attack == "wrong_epoch" else epoch),
        registry=lambda: call(address if attack == "wrong_registry" else registry),
        intentCoordinator=lambda:call(address if attack=="wrong_coordinator" else registry)))
    w3 = NS(eth=NS(contract=lambda **kw: board))
    monkeypatch.setattr(bitcoin_trust, "checked_runtime", lambda *args: None)
    monkeypatch.setattr(Manifest, "from_dict", lambda _: NS(registry=registry))
    if attack == "missing_board": del bundle["onchain_board"]
    if attack:
        with pytest.raises(ValueError, match="BITCOIN_CUSTODY_PUBLISHER_EPOCH"):
            bootstrap_custody.review_publishers(w3, bundle, 123, registry)
    else:
        bootstrap_custody.review_publishers(w3, bundle, 123, registry)


@pytest.mark.parametrize("attack", [None, "bitcoin_epoch", "deposit_authority", "confirmations", "mining_name", "sender", "spv"])
def test_destination_constructor_graph_is_authenticated_before_binding(monkeypatch, attack):
    """Self-consistent getters do not authenticate constructor storage or SPV."""
    from signer_agent import bootstrap_custody, bootstrap_tokens, bitcoin_trust, contract_provenance, mint_admission, zk_spv
    from signer_agent.dkg_protocol import Manifest
    invite = custody_invite()
    address = lambda n: Web3.to_checksum_address(f"0x{n:040x}")
    mining, auth, batches, escrow, deposits, spv, admission, coordinator, registry = [address(n) for n in range(101, 110)]
    conf_epoch = bytes.fromhex(invite["sessions"]["conf_ceremony"])
    btc_epoch = bytes.fromhex(invite["sessions"]["btc_ceremony"])
    manifest = NS(registry=registry, session_id=conf_epoch, participants=invite["participants"][1:],
                  threshold=int(invite["bitcoin_custody"]["threshold"]))
    names = bootstrap_tokens.resolve(invite)["mining"]
    values = {mining: dict(authorization=auth, escrow=escrow),
        auth: dict(getParticipants=manifest.participants, threshold=manifest.threshold,
                   dkgId=conf_epoch, registry=registry, coordinator=coordinator),
        escrow: dict(batches=batches, depositRegistry=deposits, spv=spv),
        registry: dict(getCompletedResult=[None] * 4 + [b"g" * 32])}
    class Functions:
        def __init__(self, target): self.target = target
        def __getattr__(self, getter):
            return lambda *args: NS(call=lambda **kwargs: values[self.target][getter])
    w3 = NS(eth=NS(contract=lambda **kwargs: NS(address=kwargs["address"], functions=Functions(kwargs["address"]))))
    constructors = {auth: [coordinator, conf_epoch, b"g" * 32, manifest.participants, manifest.threshold],
        batches: [auth, registry, b"x" * 32 if attack == "bitcoin_epoch" else btc_epoch],
        escrow: [batches, spv, 1 if attack == "confirmations" else int(invite["bitcoin_confirmations"]),
                 int(invite["min_p2wpkh_sats"]), int(invite["min_p2tr_sats"])],
        deposits: [spv, address(999) if attack == "deposit_authority" else auth, int(invite["bitcoin_confirmations"]), escrow],
        mining: ["integration-pool", deposits, escrow, "Fake" if attack == "mining_name" else names["name"], names["symbol"]]}
    proofs, backends = [], []
    def prove(w3, target, kind, args, at):
        assert at == 123
        if args != constructors[target]: raise ValueError("CONTRACT_CREATION_ARGUMENTS_MISMATCH")
        proofs.append(kind)
        return dict(method="direct", sender=address(999) if attack == "sender" else invite["operator"])
    def consumer(*args):
        backends.append(args[1])
        if attack == "spv": raise ValueError("ENROLLED_SPV_MISMATCH")
    monkeypatch.setattr(Manifest, "from_dict", lambda _: manifest)
    monkeypatch.setattr(bitcoin_trust, "checked_runtime", lambda *args: None)
    monkeypatch.setattr(bootstrap_custody, "review_publishers", lambda *args: None)
    monkeypatch.setattr(contract_provenance, "prove_creation", prove)
    monkeypatch.setattr(zk_spv, "review_consumer", consumer)
    monkeypatch.setattr(mint_admission, "configuration", lambda *args, **kwargs: None)
    bundle = dict(role="conf_ceremony", onboarding=dict(invitation=invite))
    intent = dict(artifact="MiningPoolV1", hash_method="getSetMintAdmissionOperationHash", target=mining,
                  coordinator=coordinator, hash_args=[admission])
    if attack:
        with pytest.raises(ValueError, match="CONTRACT_CREATION_ARGUMENTS_MISMATCH|BITCOIN_CUSTODY_GATEWAY_BINDING|ENROLLED_SPV_MISMATCH"):
            bootstrap_custody.review_intent(w3, intent, bundle, 123)
    else:
        bootstrap_custody.review_intent(w3, intent, bundle, 123)
        assert proofs == ["DKGAuthorization", "BitcoinBatchRegistry", "BitcoinWithdrawalEscrow", "BitcoinDepositRegistry", "MiningPoolV1"]
        assert backends == [invite]
