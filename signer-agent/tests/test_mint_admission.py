"""Canonical snapshots, immutable history and normal admission recovery."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3

from signer_agent import mint_admission as admission, mint_admission_client as sdk
from signer_agent import bitcoin_funding, bitcoin_onchain, bitcoin_onchain_payment
from signer_agent.secure_state import SecureState


def address(number):
    return Web3.to_checksum_address(f"0x{number:040x}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    names = {name: address(i) for i, name in enumerate(("board", "pool", "auth", "coordinator", "operator", "next"), 1)}
    view = SimpleNamespace(timestamp=100, session=7, state=1, approved=False, complete=False,
        absent=False, transfers=False, sends=[], reads=[], origins=[])
    arguments = [b"t" * 32, 0, [names["operator"]], [10**10]]
    reference = admission.claim_id(names["pool"], *arguments[:2])
    data = dict(
        board=dict(pool=names["pool"], authorization=names["auth"], coordinator=names["coordinator"],
            dkgId=b"d" * 32, resultHash=b"r" * 32, operator=names["operator"], operatorNonce=0,
            operatorTransferSession=7, latestOperatorTransferSession=7, operatorTransferRetired=False,
            activeOperatorTransfer=True, slotSession=7, active=True, claimModes=0),
        pool=dict(authorization=names["auth"], mintAdmission=names["board"], nonce=4, mintProofVersion=1,
                  custodyTransfer=address(0)),
        auth=dict(coordinator=names["coordinator"], dkgId=b"d" * 32, resultHash=b"r" * 32),
        coordinator=dict(sessionDKGId=b"d" * 32, sessionDKGResult=b"r" * 32),
    )

    def operation(target, selector, params, nonce):
        return keccak(encode(["address", "bytes4", "bytes32", "uint256"], [target, selector, params, nonce]))

    def block(number):
        return SimpleNamespace(number=123, hash=b"b" * 32, timestamp=view.timestamp)

    class Functions:
        def __init__(self, name): self.name = name
        def __getattr__(self, method):
            def invoke(*args):
                def call(block_identifier="latest"):
                    view.reads.append(block_identifier)
                    if method == "hashOperation": return operation(*args)
                    if method == "isOperationApproved": return view.approved
                    if method in ("getMintOperationHash", "getTransferOperatorOperationHash"):
                        return digest()
                    if method == "getClaim":
                        return [*arguments, 0 if view.absent else 1000, 1, view.session]
                    if method == "attempts":
                        return [bytes(32), bytes(32), 0, 0, False] if view.transfers else [reference, digest(), 4, 1000, False]
                    if method == "operatorTransfers":
                        return [names["next"], 0, 1000, digest()]
                    if method == "getSession":
                        result = [0] * 20
                        result[0], result[1], result[3], result[4] = view.session, names["board"], digest(), True
                        result[7], result[8], result[9], result[10] = getattr(view, "mutable_deadline", 1000), True, address(0), view.state
                        result[15] = names["board"] if view.transfers else names["pool"]
                        return result
                    return data[self.name][method]
                return SimpleNamespace(call=call)
            return invoke

    contracts = {name: SimpleNamespace(address=where, functions=Functions(name)) for name, where in names.items()}
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=block,
        contract=lambda address, **_:next(c for c in contracts.values() if c.address == address)))
    monkeypatch.setattr(admission, "checked_runtime", lambda *args, **kwargs:None)
    monkeypatch.setattr(admission, "prove_creation", lambda *args, **kwargs:view.origins.append(args))
    def context():
        return dict(board=contracts["board"], pool=contracts["pool"], authorization=contracts["auth"],
            coordinator=contracts["coordinator"], anchor=block(123), dkg_id=b"d" * 32, result_hash=b"r" * 32)
    def digest():
        return admission._operation_hash(context(), "BitcoinMintAdmission" if view.transfers else "MiningPoolV1",
            "transferOperator" if view.transfers else "mintFromProof", [names["next"]] if view.transfers else arguments,
            0 if view.transfers else 4)
    registry = SimpleNamespace(w3=w3, account=SimpleNamespace(address=names["operator"]))
    state = SecureState(str(tmp_path / "admission.enc"), "test-only")
    graph = dict(evm_dkg_id=(b"d" * 32).hex(), contracts=dict(authorization=dict(address=names["auth"])))
    monkeypatch.setattr(bitcoin_onchain_payment, "_auth_board", lambda *_:(None, "conf_ceremony"))
    monkeypatch.setattr(bitcoin_onchain, "snapshot_check", lambda *_:None)
    monkeypatch.setattr(sdk, "confirmed_block", lambda *_:block(123))
    monkeypatch.setattr(bitcoin_funding, "_mint_complete", lambda *_:view.complete)
    monkeypatch.setattr(bitcoin_funding, "_mint_proof", lambda *_:arguments)
    def send(registry, target, kind, method, args, **kwargs):
        view.sends.append((method, deepcopy(args)))
        if method in ("retryMint", "openMint", "openOperatorTransfer"):
            view.session += 1
            view.state, view.absent = 1, False
            data["board"].update(slotSession=view.session, operatorTransferSession=view.session)
        elif method == "mintFromProof": view.complete = True
        elif method == "authorizeOperation": view.approved = True
        return dict(blockNumber=123)
    monkeypatch.setattr(bitcoin_onchain, "send", send)
    def record(**kwargs):
        return admission.session_record(w3, names["board"], view.session, 123, role="conf_ceremony", **kwargs)
    def dispatch(action, deadline=1000):
        return sdk.dispatch_funding(SimpleNamespace(action=action, deadline=deadline), None, None, registry,
            state, graph, contracts, contracts["pool"], arguments, object(), block(123), names["board"])
    return SimpleNamespace(**locals())


def test_canonical_source_and_all_reads_use_same_snapshot(env):
    record = env.record(signing=True)
    assert record["intent"]["deadline"] == "1000"
    assert record["intent"]["mint_admission"] == env.names["board"]
    assert set(env.view.reads) == {123}
    assert [args[2] for args in env.view.origins] == ["BitcoinMintAdmission", "MiningPoolV1", "DKGAuthorization", "FROSTCoordinator"]


def test_recovery_mint_reviews_public_ingress_wallet_after_rotation(env, monkeypatch):
    from signer_agent import bitcoin_trust

    original_id, active_id = b"o" * 32, b"n" * 32
    context = env.context()
    members = [env.names["operator"], env.names["next"]]
    invite = dict(version="6", bitcoin_network="regtest", evm_confirmations="1",
                  sessions=dict(btc_ceremony=original_id.hex(), conf_ceremony=context["dkg_id"].hex()),
                  bitcoin_recovery={"height": "900"}, participants=[address(30), *members],
                  bitcoin_custody={"mode": "custodians", "threshold": "2"})
    escrow, batches, registry = (SimpleNamespace(address=address(i)) for i in (21, 22, 23))
    nodes = {node.address: node for node in (escrow, batches, registry)}
    reads = {
        (context["pool"].address, "escrow"): escrow.address,
        (context["authorization"].address, "registry"): registry.address,
        (escrow.address, "batches"): batches.address,
        (batches.address, "initialWalletId"): original_id,
        (batches.address, "walletId"): active_id,
        (registry.address, "getCompletedResult", original_id): [b"c" * 32, b"r" * 32, 1, b"b" * 32,
                                                                 b"k" * 32, members, 2],
    }
    context["pool"].w3 = env.w3

    def read(node, number, method, *args):
        assert number == 123
        return reads[(node.address, method, *args)]

    def inspect(manifest, key, reg, target, **options):
        assert manifest.session_id == original_id and manifest.participants == tuple(members)
        assert reg.w3 is env.w3 and target == batches.address
        assert key == {"result_hash": (b"r" * 32).hex(), "result": {"group": "02" + (b"k" * 32).hex()}}
        assert options["public_wallet"] is True and options["bitcoin_recovery"] == invite["bitcoin_recovery"]
        return {"graph": "verified"}

    monkeypatch.setattr(admission, "_contract", lambda w3, address, kind, block: nodes[address])
    monkeypatch.setattr(admission, "_read", read)
    monkeypatch.setattr(bitcoin_trust, "inspect_registry", inspect)
    assert admission._base_signing_graph(dict(role="conf_ceremony", registry=registry.address,
        onboarding={"invitation": invite}), context) == "verified"


def test_renewed_deadline_does_not_change_persisted_claim_economics(env):
    initial = env.record()
    first = sdk.remember(env.state, env.registry, initial)
    renewed = dict(initial, intent=dict(initial["intent"], session="8", deadline="2000", admission_nonce="5"),
                   deadline=2000, nonce=5)
    second = sdk.remember(env.state, env.registry, renewed)
    assert env.state.get(first)["intent"]["deadline"] == "1000"
    assert env.state.get(second)["intent"]["deadline"] == "2000"
    assert sdk.remember(env.state, env.registry, renewed) == second


def test_base_mint_requires_local_core_before_signing(env):
    client = SimpleNamespace(w3=env.w3, address=env.names["coordinator"], _read_snapshot=env.block(123))
    session = SimpleNamespace(origin_contract=env.names["pool"], session_id=7, message_hash=env.digest())
    with pytest.raises(ValueError, match="BTC_MINT_FUNDING_REVIEW_REQUIRED"):
        admission.guard_signing_session(client, session)


def test_base_mint_rechecks_slot_after_local_funding(env, monkeypatch):
    client = SimpleNamespace(w3=env.w3, address=env.names["coordinator"], _read_snapshot=env.block(123),
                             check_read_snapshot=lambda: None)
    session = SimpleNamespace(origin_contract=env.names["pool"], session_id=7, message_hash=env.digest())
    calls = []

    def review(client_arg, record):
        assert client_arg is client
        assert record["arguments"] == env.arguments
        calls.append(record["claim_id"])

    monkeypatch.setattr(admission, "_review_base_funding", review)
    admission.guard_signing_session(client, session)
    assert calls == [env.reference.hex()]

    def review_during_slot_change(*args):
        review(*args)
        env.data["board"]["slotSession"] = 8

    monkeypatch.setattr(admission, "_review_base_funding", review_during_slot_change)
    with pytest.raises(ValueError, match="BTC_MINT_ADMISSION_NOT_ACTIVE"):
        admission.guard_signing_session(client, session)


@pytest.mark.parametrize("amount, gateway_kind, source_funding, error", [
    (1, None, False, None),
    (2, None, False, "BTC_MINT_PROOF_OR_ALLOCATION"),
    (1, "BitcoinCustodyTransfer", False, None),
    (1, "BitcoinCustodyTransfer", True, "SMP1_CUSTODY_SOURCE"),
    (1, "BitcoinCustodyTransferV2", False, None),
    (1, "BitcoinCustodyTransferV2", True, "SMP1_CUSTODY_SOURCE"),
])
def test_base_mint_simulates_exact_registry_and_gateway_claim(env, monkeypatch, amount, gateway_kind, source_funding, error):
    from signer_agent import bitcoin_trust

    record = env.record()
    record["context"]["pool"].w3 = env.w3
    addresses = dict(escrow=address(80), deposits=address(81), batches=address(82), relay=address(83),
                     authorization=env.names["auth"])
    env.data["pool"].update({name: addresses[name] for name in ("escrow", "deposits")})
    graph = dict(evm_dkg_id=record["context"]["dkg_id"].hex(), contracts={
        name: dict(address=where, artifact=name) for name, where in addresses.items()
    })
    calls = []
    proof = [b"public", 0, b"b" * 32, [], 0, *env.arguments[2:]]
    bitcoin = object()

    def proof_review(args, core, contracts, pool, claim, block):
        assert core is bitcoin and args.txid == env.arguments[0][::-1].hex()
        assert pool is record["context"]["pool"] and claim == env.arguments and block.number == 123
        assert set(contracts) == {"batches", "deposits", "relay"}
        calls.append("core")
        return proof

    def consume(*args):
        assert list(args) == proof[:5]
        def call(options, *, block_identifier):
            assert options == {"from": env.names["pool"]}
            assert block_identifier == 123
            calls.append("registry")
            return amount
        return SimpleNamespace(call=call)

    nodes = {name: SimpleNamespace(functions=SimpleNamespace(consume=consume)) for name in addresses}
    if gateway_kind:
        env.data["pool"]["custodyTransfer"] = address(84)
        if gateway_kind == "BitcoinCustodyTransferV2":
            record["context"]["pool_kind"] = "ConfederationBitcoinReserve"

        def funding(raw):
            assert raw == proof[0]

            def call(*, block_identifier):
                assert block_identifier == 123
                calls.append("gateway")
                return source_funding

            return SimpleNamespace(call=call)

        nodes[gateway_kind] = SimpleNamespace(functions=SimpleNamespace(
            mining=lambda: SimpleNamespace(call=lambda **_: env.names["pool"]), isSourceFunding=funding))
    monkeypatch.setattr(admission, "_base_signing_graph", lambda *_: graph)
    monkeypatch.setattr(admission, "_contract", lambda _w3, _where, name, _block: nodes[name])
    monkeypatch.setattr(bitcoin_trust, "bitcoin_for_graph", lambda *_: bitcoin)
    monkeypatch.setattr(bitcoin_funding, "_mint_proof", proof_review)
    client = SimpleNamespace(w3=env.w3, base_mint_funding=({}, "rpc", "cookie"))
    if error is None:
        admission._review_base_funding(client, record)
    else:
        with pytest.raises(ValueError, match=error):
            admission._review_base_funding(client, record)
    assert calls == ["core"] + (["gateway"] if gateway_kind else []) + ([] if source_funding else ["registry"])


@pytest.mark.parametrize("accepted", [False, True])
def test_base_mint_reads_historical_gateway_only_after_enrolled_binding(env, monkeypatch, accepted):
    from signer_agent import custody_network

    context = env.record()["context"]
    raw, op = b"public", b"h" * 32
    gateway = env.contracts["next"]
    env.data["pool"].update(custodyTransfer=gateway.address, getSetCustodyTransferOperationHash=op)
    env.data["next"] = dict(mining=env.names["pool"])
    checks = []

    def exact_runtime(*args):
        assert args == (env.w3, gateway.address, "BitcoinCustodyTransfer", 123)
        raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")

    def enrolled(*args):
        assert args == (env.w3, gateway, "BitcoinCustodyTransfer", env.contracts["pool"], op, 123)
        checks.append("enrolled")
        if not accepted:
            raise ValueError("BITCOIN_CUSTODY_GATEWAY_BINDING")

    def funding(data):
        assert data == raw
        checks.append("source")
        return SimpleNamespace(call=lambda **_: False)

    monkeypatch.setattr(admission, "_contract", exact_runtime)
    monkeypatch.setattr(custody_network, "require_enrolled_legacy", enrolled)
    monkeypatch.setattr(gateway.functions, "isSourceFunding", funding)
    if accepted:
        admission._review_mint_source(env.w3, context, raw)
    else:
        with pytest.raises(ValueError, match="BITCOIN_CUSTODY_GATEWAY_BINDING"):
            admission._review_mint_source(env.w3, context, raw)
    assert checks == (["enrolled", "source"] if accepted else ["enrolled"])


@pytest.mark.parametrize("attack", [None, "message", "missing_enrollment", "sources"])
def test_custody_configuration_has_its_own_guard_and_never_bypasses_mint_admission(env, monkeypatch, attack):
    from signer_agent import bootstrap_custody
    e = env; op = b"c" * 32
    e.data["pool"]["getSetCustodyTransferOperationHash"] = op
    intent = dict(artifact="MiningPoolV1", hash_method="getSetCustodyTransferOperationHash",
                  target=e.names["pool"], hash_args=[e.names["next"]])
    invite = dict(bitcoin_custody={}) if attack != "missing_enrollment" else {}
    client = SimpleNamespace(w3=e.w3, _read_snapshot=e.block(123),
        custody_context=(intent, dict(onboarding=dict(invitation=invite)), "1"))
    session = SimpleNamespace(origin_contract=e.names["pool"], session_id=7,
                              message_hash=b"x" * 32 if attack == "message" else op)
    calls = []
    def reviewed(*args):
        calls.append("custody")
        if attack == "sources": raise ValueError("BITCOIN_CUSTODY_SOURCE_CODE")
    monkeypatch.setattr(bootstrap_custody, "guard_signing_session", reviewed)
    monkeypatch.setattr(admission, "session_record", lambda *args, **kwargs: pytest.fail("configuration treated as mint claim"))
    if attack:
        with pytest.raises(ValueError, match="CONFIGURATION_INTENT|CUSTODY_SOURCE_CODE"):
            admission.guard_signing_session(client, session)
    else:
        admission.guard_signing_session(client, session)
        assert calls == ["custody"]


@pytest.mark.parametrize("field,value", [("nonce", 5), ("slotSession", 8), ("active", False)])
def test_changed_canonical_slot_stops_signing(env, field, value):
    env.data["pool" if field == "nonce" else "board"][field] = value
    with pytest.raises(ValueError, match="BTC_MINT_ADMISSION_NOT_ACTIVE"):
        env.record(signing=True)
    assert env.record(require_active=False)["active"] is False


@pytest.mark.parametrize("timestamp,mutable", [(1000, 1000), (100, 2000)])
def test_original_deadline_remains_binding_for_private_work(env, timestamp, mutable):
    env.view.timestamp, env.view.mutable_deadline = timestamp, mutable
    with pytest.raises(ValueError, match="BTC_MINT_ORIGINAL_DEADLINE_REQUIRED"):
        env.record(signing=True)


def test_finalized_approval_remains_available_after_original_deadline(env):
    env.view.state, env.view.timestamp = 2, 2000
    assert env.record(signing=True)["state"] == 2


def test_transfer_proposals_keep_separate_immutable_history_at_same_admin_nonce(env):
    env.view.transfers = True
    first = env.record()
    first_key = sdk.remember(env.state, env.registry, first)
    env.view.session = 8
    env.data["board"]["operatorTransferSession"] = 8
    env.names["next"] = address(20)
    second = env.record()
    second_key = sdk.remember(env.state, env.registry, second)
    assert first_key != second_key
    assert env.state.get(first_key)["intent"] == first["intent"]
    assert env.state.get(second_key)["intent"] == second["intent"]


def test_claim_history_never_replaces_economic_terms_or_nonce_journal(env):
    first = env.record()
    key = sdk.remember(env.state, env.registry, first)
    env.state.put("unrelated-signing-session", dict(status="consumed", share="test-only"))
    changed = deepcopy(first)
    changed["intent"].update(session="8", deadline="2000")
    with pytest.raises(ValueError, match="BTC_MINT_ECONOMIC_INTENT_CHANGED"):
        sdk.remember(env.state, env.registry, changed)
    assert env.state.get(key)["intent"] == first["intent"]
    assert env.state.get("unrelated-signing-session")["status"] == "consumed"


def test_existing_claim_adopted_without_any_paid_session(env):
    assert env.dispatch("btc-mint-propose")["session"] == "7"
    assert env.view.sends == []


def test_wrong_original_deadline_never_persists_or_sends(env):
    with pytest.raises(ValueError, match="BTC_MINT_ECONOMIC_INTENT_CHANGED"):
        env.dispatch("btc-mint-propose", 2000)
    assert env.state._load()["sessions"] == {}
    assert env.view.sends == []


def test_each_same_nonce_retry_has_distinct_durable_transaction_arguments(env):
    env.view.state = 3
    assert env.dispatch("btc-mint-retry")["session"] == "8"
    env.view.state = 3
    assert env.dispatch("btc-mint-retry")["session"] == "9"
    retries = [args for method, args in env.view.sends if method == "retryMint"]
    assert retries == [[env.reference, 4, 7], [env.reference, 4, 8]]
    assert len(env.state._load()["sessions"]) == 4


def test_exact_existing_approval_executes_aborted_claim_without_new_session(env):
    env.view.state, env.view.approved, env.view.timestamp = 3, True, 2000
    assert env.dispatch("btc-mint-execute")["status"] == "BTC_FUNDING_EXECUTED"
    assert [row[0] for row in env.view.sends] == ["mintFromProof"]


def test_aborted_unapproved_claim_cannot_execute(env):
    env.view.state = 3
    with pytest.raises(ValueError, match="BTC_MINT_ADMISSION_NOT_ACTIVE"):
        env.dispatch("btc-mint-execute")
    assert env.view.sends == []


def test_approved_claim_requests_execution_instead_of_paid_retry(env):
    env.view.state, env.view.approved = 3, True
    with pytest.raises(ValueError, match="BTC_MINT_OPERATION_ALREADY_APPROVED_EXECUTE_REQUIRED"):
        env.dispatch("btc-mint-retry")
    assert env.view.sends == []


def test_no_claim_existing_approval_explicitly_blocks_paid_open(env):
    env.view.absent, env.view.approved = True, True
    with pytest.raises(ValueError, match="BTC_MINT_OPERATION_ALREADY_APPROVED_LEGACY_RECOVERY_REQUIRED"):
        env.dispatch("btc-mint-propose")
    assert env.view.sends == []


def test_transfer_proposal_binds_admin_nonce_and_persistent_predecessor(env):
    env.view.transfers = True
    args = SimpleNamespace(action="btc-mint-transfer-propose", mint_admission=env.names["board"],
        new_operator=env.names["next"], deadline=1000)
    sdk.management(args, None, None, env.registry, env.state, env.graph)
    assert env.view.sends == [("openOperatorTransfer", [env.names["next"], 1000, 0, 7])]


def test_service_guard_precedes_any_private_work():
    from signer_agent.service import SigningService
    service = object.__new__(SigningService)
    touched = []
    def guard(_):
        touched.append("guard")
        raise ValueError("BTC_MINT_ADMISSION_NOT_ACTIVE")
    service.client = SimpleNamespace(chain_id=31337, guard_mint_session=guard)
    session = SimpleNamespace(state=1, purpose=1)
    with pytest.raises(ValueError, match="BTC_MINT_ADMISSION_NOT_ACTIVE"):
        service._approve_locked(session)
    assert touched == ["guard"]
