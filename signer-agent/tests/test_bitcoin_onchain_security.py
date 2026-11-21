"""Public packet boundaries and independent nonce-journal recovery checks.

All scalars and credentials in these tests are synthetic test values.
"""

import json
import shutil
import copy
import os
from types import SimpleNamespace
from web3.datastructures import AttributeDict

import pytest

from signer_agent import crypto
from signer_agent.secure_state import SecureState
from test_security_crypto_review import graph


@pytest.fixture
def spent_state(tmp_path):
    state = SecureState(str(tmp_path / "participant.enc"), "test-only-state-key")
    state.put(
        "btc-session",
        dict(status="nonce-created", message_hash="11" * 32,
             hiding="test-hiding-nonce", binding="test-binding-nonce"),
    )
    state.prepare_share("btc-session", "22" * 32, 123)
    return state


def test_cached_bitcoin_share_requires_exact_durable_spend_proof(spent_state):
    spent_state.assert_spent("btc-session", "22" * 32)
    with pytest.raises(RuntimeError, match="NONCE_SPEND_TRANSCRIPT_MISMATCH"):
        spent_state.assert_spent("btc-session", "33" * 32)
    with pytest.raises(RuntimeError, match="NONCE_SPEND_PROOF_MISSING"):
        spent_state.assert_spent("another-session", "22" * 32)


def test_cached_bitcoin_share_refuses_missing_or_substituted_journal(spent_state, tmp_path):
    journal = spent_state.path.with_name(spent_state.path.name + ".spent.sqlite")
    journal.unlink()
    with pytest.raises(RuntimeError, match="NONCE_JOURNAL_MISSING"):
        spent_state.assert_spent("btc-session", "22" * 32)
    other = SecureState(str(tmp_path / "another.enc"), "another-test-only-key")
    other.assert_unspent("unused-session")
    shutil.copyfile(str(other.path) + ".spent.sqlite", journal)
    with pytest.raises(RuntimeError, match="NONCE_JOURNAL_MISMATCH"):
        spent_state.assert_spent("btc-session", "22" * 32)


def test_cached_bitcoin_share_refuses_deleted_spend_record(spent_state):
    with spent_state._journal() as db:
        db.execute("DELETE FROM spent WHERE session=?", ("btc-session",))
    with pytest.raises(RuntimeError, match="NONCE_SPEND_PROOF_MISSING"):
        spent_state.assert_spent("btc-session", "22" * 32)


def test_public_bitcoin_packet_inspection_does_not_return_secret_material():
    secret = "00" * 31 + "01"
    nonce = crypto.call("nonce", share=secret)
    public = crypto.call(
        "inspect_bitcoin_packet", kind="commitment", value=nonce["commitments"]
    )
    assert public == {"value": nonce["commitments"]}
    assert len(bytes.fromhex(public["value"])) == 71
    assert nonce["nonces"] not in json.dumps(public)
    for kind in ("commitment", "share"):
        with pytest.raises(RuntimeError, match="CRYPTO_VALIDATION_FAILED"):
            crypto.call("inspect_bitcoin_packet", kind=kind, value=nonce["nonces"])
        with pytest.raises(RuntimeError, match="CRYPTO_VALIDATION_FAILED"):
            crypto.call("inspect_bitcoin_packet", kind=kind,
                        value=nonce["commitments"], signing_share=secret)


@pytest.mark.parametrize("kind,value", [
    ("share", "ff" * 32),
    ("share", "00" * 31),
    ("share", "00" * 33),
    ("commitment", "00" * 71),
    ("commitment", "00" * 70),
    ("commitment", "00" * 72),
])
def test_malformed_public_bitcoin_packet_rejected(kind, value):
    with pytest.raises(RuntimeError, match="CRYPTO_VALIDATION_FAILED"):
        crypto.call("inspect_bitcoin_packet", kind=kind, value=value)


@pytest.fixture
def bitcoin_signer_boundary(tmp_path, monkeypatch):
    """Isolate durable export ordering from RPC and curve arithmetic tests."""
    from signer_agent import bitcoin_trust, bitcoin_worker

    state = SecureState(str(tmp_path / "btc.enc"), "synthetic-boundary-state-key")
    manifest = SimpleNamespace(context=b"a" * 32, threshold=2, participants=["A", "B"])
    key = dict(index=1, result=dict(signing_share="c1" * 32, key_package="c2" * 70))
    plan = dict(network="regtest", batch_registry="test-registry", batch_id="bb" * 32)
    generated = dict(nonces="d1" * 140, commitments="d2" * 71)
    calls = []

    def call(op, **params):
        calls.append(op)
        if op == "nonce":
            return dict(generated)
        if op == "sign":
            assert params["nonces"] == generated["nonces"]
            return dict(share="e1" * 32)
        pytest.fail("unexpected cryptographic operation")

    monkeypatch.setattr(bitcoin_trust, "require_registry", lambda *args: {"graph": {}})
    monkeypatch.setattr(bitcoin_worker, "validate",
                        lambda *args: (dict(sighashes=["ab" * 32]), b"h" * 32))
    monkeypatch.setattr(bitcoin_worker.crypto, "call", call)
    # This isolated export fixture has no EVM graph; fee policy is covered separately.
    monkeypatch.setattr("signer_agent.btc_fee_reserve.guard", lambda *args, **kwargs: None)

    def step(action):
        return bitcoin_worker.sign_step(action, manifest, key, plan, None, state, None)

    commitment = step("btc-commit")
    plan["commitments"] = [{"1": commitment["values"][0], "2": "d3" * 71}]
    sid = "btc:" + manifest.context.hex() + ":" + plan["batch_id"] + ":0"
    return SimpleNamespace(state=state, key=key, plan=plan, step=step, sid=sid,
                           calls=calls, generated=generated, commitment=commitment)


def test_bitcoin_export_contains_only_public_values_and_retries_do_not_sign_again(bitcoin_signer_boundary):
    signer = bitcoin_signer_boundary
    signed = signer.step("btc-sign")
    assert signer.calls == ["nonce", "sign"]
    assert signer.step("btc-sign") == signed
    assert signer.step("btc-commit") == signer.commitment
    assert signer.calls == ["nonce", "sign"]
    public = json.dumps([signer.commitment, signed])
    for secret in (signer.generated["nonces"], signer.key["result"]["signing_share"],
                   signer.key["result"]["key_package"]):
        assert secret not in public
        assert secret.encode() not in signer.state.path.read_bytes()
    stored = signer.state.get(signer.sid)
    assert "nonces" not in stored
    signer.state.assert_spent(signer.sid, stored["transcript"])
    (signer.state.path.parent / "btc.enc.spent.sqlite").unlink()
    with pytest.raises(RuntimeError, match="NONCE_JOURNAL_MISSING"):
        signer.step("btc-sign")
    assert signer.calls == ["nonce", "sign"]


def test_bitcoin_changed_peer_commitment_never_reuses_spent_nonce(bitcoin_signer_boundary):
    signer = bitcoin_signer_boundary
    signer.step("btc-sign")
    signer.plan["commitments"][0]["2"] = "d4" * 71
    with pytest.raises(ValueError, match="NONCE_TRANSCRIPT_MISMATCH"):
        signer.step("btc-sign")
    assert signer.calls == ["nonce", "sign"]


def test_bitcoin_crash_between_journal_and_encrypted_save_fails_closed(bitcoin_signer_boundary, monkeypatch):
    signer = bitcoin_signer_boundary
    save = signer.state.put

    def interrupted_save(key, value):
        if value["status"] == "share-prepared":
            raise RuntimeError("SIMULATED_DISK_CRASH")
        return save(key, value)

    monkeypatch.setattr(signer.state, "put", interrupted_save)
    with pytest.raises(RuntimeError, match="SIMULATED_DISK_CRASH"):
        signer.step("btc-sign")
    assert signer.state.get(signer.sid)["status"] == "nonce-created"
    monkeypatch.setattr(signer.state, "put", save)
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        signer.step("btc-sign")
    assert signer.calls == ["nonce", "sign"]


@pytest.fixture
def public_bitcoin_plan():
    from web3 import Web3

    def address(index):
        return Web3.to_checksum_address("0x" + format(index, "040x"))

    wallet = "5120" + "11" * 32
    plan = dict(
        version="2", network="regtest", batch_registry=address(1),
        batch_id="aa" * 32, signing_board=address(2), wallet_result="bb" * 32,
        wallet_script=wallet, deadline="2000000000", fee="1000", max_fee="2000",
        inputs=[dict(outpoint="cc" * 32 + ":0", amount="10000", script=wallet)],
        outputs=[dict(amount="9000", script="0014" + "dd" * 20)],
        withdrawal_refs=["ee" * 32], signers=["1", "2"],
    )
    manifest = SimpleNamespace(chain_id=31337, threshold=2,
                               participants=[address(3), address(4), address(5)])
    return manifest, plan


@pytest.mark.parametrize("location", ["plan", "input", "output"])
@pytest.mark.parametrize("field", ["private_key", "nonces", "signing_share", "key_package", "rpc_url"])
def test_secret_or_route_fields_never_enter_public_plan(public_bitcoin_plan, location, field):
    from signer_agent.bitcoin_onchain_plan import serialize_plan

    manifest, plan = public_bitcoin_plan
    target = plan if location == "plan" else plan[location + "s"][0]
    target[field] = "synthetic-private-value"
    with pytest.raises(ValueError, match="BTC_PLAN_.*SCHEMA"):
        serialize_plan(plan, manifest)


def test_public_plan_rejects_duplicate_keys_noncanonical_bytes_and_empty_withdrawals(public_bitcoin_plan):
    from signer_agent.bitcoin_onchain_plan import decode_plan, serialize_plan

    manifest, plan = public_bitcoin_plan
    canonical = serialize_plan(plan, manifest)
    assert decode_plan(canonical, manifest) == plan
    for bad in (canonical + b"\n", canonical.replace(b'"version":"2"', b'"version":"2","version":"2"')):
        with pytest.raises(ValueError, match="BTC_PLAN_NONCANONICAL"):
            decode_plan(bad, manifest)
    plan["withdrawal_refs"] = []
    with pytest.raises(ValueError, match="BTC_PLAN_WITHDRAWALS"):
        serialize_plan(plan, manifest)


@pytest.mark.parametrize("field,value", [
    ("batch_id", "01" * 32),
    ("wallet_result", "02" * 32),
    ("signing_board", "0x0000000000000000000000000000000000000006"),
    ("signers", ["1", "3"]),
])
def test_board_batch_wallet_and_signer_subset_are_hash_bound(public_bitcoin_plan, field, value):
    from signer_agent.bitcoin_onchain_plan import onchain_plan_hash

    manifest, plan = public_bitcoin_plan
    original = onchain_plan_hash(manifest, plan)
    changed = copy.deepcopy(plan)
    changed[field] = value
    assert onchain_plan_hash(manifest, changed) != original


def test_transport_domain_changes_do_not_change_bitcoin_sighash(public_bitcoin_plan):
    from signer_agent.bitcoin_onchain_plan import onchain_plan_hash

    manifest, plan = public_bitcoin_plan
    original_hash = onchain_plan_hash(manifest, plan)
    package = {key: plan[key] for key in ("wallet_script", "inputs", "outputs", "fee", "max_fee")}
    before = crypto.call("bitcoin_build", **package)
    manifest.chain_id = 11155111
    plan["signers"] = ["1", "3"]
    assert onchain_plan_hash(manifest, plan) != original_hash
    after = crypto.call("bitcoin_build", **package)
    assert before["unsigned"] == after["unsigned"]
    assert before["sighashes"] == after["sighashes"]


def test_secure_state_rejects_shared_writable_directory_before_creating_files(tmp_path):
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o777)
    directory.chmod(0o777)
    with pytest.raises(ValueError, match="SIGNER_STATE_DIRECTORY_PERMISSIONS"):
        SecureState(str(directory / "participant.enc"), "test-only")
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("suffix,operation", [
    ("", "read"), ("", "write"), (".lock", "lock"),
    (".spent.sqlite", "journal"), (".spent.sqlite-journal", "journal"),
    (".spent.sqlite-wal", "journal"), (".spent.sqlite-shm", "journal"),
])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_secure_state_rejects_substituted_files(tmp_path, suffix, operation, link_kind):
    state = SecureState(str(tmp_path / "participant.enc"), "test-only")
    victim = tmp_path / "unrelated-file"
    victim.write_bytes(b"must not change")
    victim.chmod(0o600)
    target = tmp_path / ("participant.enc" + suffix)
    if link_kind == "symlink":
        target.symlink_to(victim)
    else:
        os.link(victim, target)
    with pytest.raises((ValueError, OSError)):
        if operation == "read":
            state.get("none")
        elif operation == "write":
            state.put("none", dict(public="test"))
        elif operation == "lock":
            with state.exclusive():
                pytest.fail("substituted lock acquired")
        else:
            state.assert_unspent("none")
    assert victim.read_bytes() == b"must not change"


def test_secure_state_rechecks_directory_permissions_and_restricts_existing_inodes(tmp_path):
    directory = tmp_path / "participant"
    state = SecureState(str(directory / "state.enc"), "test-only")
    assert directory.stat().st_mode & 0o777 == 0o700
    state.put("test", dict(public="value"))
    state.path.chmod(0o644)
    assert state.get("test") == dict(public="value")
    assert state.path.stat().st_mode & 0o777 == 0o600
    lock = directory / "state.enc.lock"
    lock.write_text("")
    lock.chmod(0o666)
    with state.exclusive():
        assert lock.stat().st_mode & 0o777 == 0o600
    directory.chmod(0o777)
    with pytest.raises(ValueError, match="SIGNER_STATE_DIRECTORY_PERMISSIONS"):
        state.get("test")
    with pytest.raises(ValueError, match="SIGNER_STATE_DIRECTORY_PERMISSIONS"):
        with state.exclusive():
            pytest.fail("shared-writable lock acquired")


def test_evm_authorization_cached_retry_requires_nonce_journal(tmp_path, monkeypatch):
    import test_signing_service as fixture
    from signer_agent.service import SigningService

    captured = {}
    approve = SigningService.approve

    def remember(service, session):
        captured.update(service=service, session=session)
        return approve(service, session)

    def network_failure(*args):
        raise RuntimeError("SIMULATED_NETWORK_FAILURE")

    monkeypatch.setattr(SigningService, "approve", remember)
    monkeypatch.setattr(fixture.FakeClient, "submit_signature_share", network_failure)
    with pytest.raises(RuntimeError, match="SIMULATED_NETWORK_FAILURE"):
        fixture.test_two_round_approval_can_resume_across_runs(tmp_path, False)
    service = captured["service"]
    os.unlink(str(service.state.path) + ".spent.sqlite")

    def must_not_publish(*args):
        pytest.fail("cached share published without original nonce journal")

    monkeypatch.setattr(fixture.FakeClient, "submit_signature_share", must_not_publish)
    with pytest.raises(RuntimeError, match="NONCE_JOURNAL_MISSING"):
        service.approve(captured["session"])


@pytest.mark.parametrize("message", [
    "abc123" * 10, "ABC123" * 10, "BTC_SECRET_TOKEN_SHOULD_NEVER_BE_PRINTED",
    "https://user:private-password@rpc.invalid/private-token",
])
@pytest.mark.parametrize("error", [ValueError, RuntimeError])
def test_participant_cli_never_echoes_arbitrary_secret_exception_values(monkeypatch, capsys, message, error):
    from signer_agent import onboarding, participant_cli

    def fail(*args):
        raise error(message)

    monkeypatch.setattr(onboarding, "password_file", fail)
    assert participant_cli.main(["transport-init", "--state", "unused", "--password-file", "unused"]) == 2
    captured = capsys.readouterr()
    assert message not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["status"] == "ERROR" and result["code"] == error.__name__ and result["message"]


def test_participant_safe_error_allowlist_preserves_known_recovery_code():
    from signer_agent.participant_errors import participant_exception_code

    assert participant_exception_code(ValueError("DKG_ANCHOR_REORG")) == "DKG_ANCHOR_REORG"
    assert participant_exception_code(RuntimeError("NONCE_JOURNAL_MISSING_RECOVERY_REQUIRED")) == "NONCE_JOURNAL_MISSING_RECOVERY_REQUIRED"


@pytest.fixture
def bitcoin_reserve_intent(graph, public_bitcoin_plan, monkeypatch):
    """Known canonical graph; untrusted intent is independently mutated by tests."""
    from eth_abi import encode
    from web3 import Web3
    from signer_agent import bitcoin_trust, bitcoin_onchain_payment as payment
    from signer_agent.bootstrap import Bootstrap, artifact
    from signer_agent.bitcoin_onchain_plan import onchain_plan_hash
    from signer_agent.bitcoin_worker import outpoint_hash
    from signer_agent.dkg_protocol import Manifest

    _, plan = public_bitcoin_plan
    board = Web3.to_checksum_address("0x" + format(400, "040x"))
    graph.names["signing_board"] = board
    graph.codes[board] = bytes.fromhex(artifact("BitcoinSigningBoard")["deployedBytecode"]["object"].removeprefix("0x"))
    graph.methods["signing_board"] = dict(
        dkg=graph.manifest.registry, batchRegistry=graph.names["batches"],
        walletId=graph.manifest.session_id, walletResult=bytes.fromhex(graph.key["result_hash"]),
        maxPacketBytes=512, MAX_PLAN_BYTES=16384,
        getPlanHash=lambda batch, raw: Web3.keccak(encode(
            ["bytes32", "uint256", "address", "bytes32", "bytes32", "bytes32"],
            [Web3.keccak(text="SATOHSI_BTC_ONCHAIN_PLAN_V1"), graph.manifest.chain_id, board,
             bytes.fromhex(graph.key["result_hash"]), batch, Web3.keccak(raw)],
        )),
    )
    graph.methods["dkg"]["transportKeys"] = lambda session, member: graph.manifest.transport_keys[
        graph.manifest.participants.index(member)
    ]
    plan.update(batch_registry=graph.names["batches"], signing_board=board,
                wallet_result=graph.key["result_hash"])
    graph.block.timestamp = 100
    operation = lambda *args: Web3.keccak(encode(["bytes32", "bytes32", "bytes32[]", "uint64"], args))
    graph.methods["batches"]["operationHash"] = operation
    public_graph = bitcoin_trust.inspect_registry(graph.manifest, graph.key, graph.registry,
                                                  graph.names["batches"])["graph"]
    monkeypatch.setattr(bitcoin_trust, "discover_registry", lambda *args: graph.names["batches"])
    monkeypatch.setattr(bitcoin_trust, "board_options", lambda *args: {})
    checked_terms = []
    monkeypatch.setattr(payment, "_terms", lambda contracts, plan, bitcoin=None, block=None: checked_terms.append(plan))
    args = [bytes.fromhex(plan["batch_id"]), onchain_plan_hash(graph.manifest, plan),
            [outpoint_hash(item["outpoint"]) for item in plan["inputs"]], int(plan["deadline"])]
    intent = dict(chain_id=str(graph.manifest.chain_id),
                  coordinator=graph.names["coordinator"], role="conf_ceremony", session="1",
                  target=graph.names["batches"], artifact="BitcoinBatchRegistry",
                  hash_method="operationHash", hash_args=Bootstrap.wire(args),
                  operation_hash=operation(*args).hex(), bitcoin_plan=plan)
    evm = Manifest(graph.manifest.chain_id, graph.manifest.registry,
                   bytes.fromhex(public_graph["evm_dkg_id"]), 1, "EVM_AUTH",
                   graph.manifest.participants, graph.manifest.transport_keys, graph.manifest.threshold)
    return SimpleNamespace(intent=intent, manifest=evm,
                           key=dict(result_hash=public_graph["evm_result"]), registry=graph.registry,
                           checked_terms=checked_terms)


def test_evm_reserve_review_binds_public_bitcoin_plan_to_operation(bitcoin_reserve_intent):
    from signer_agent.bitcoin_onchain_payment import review_intent

    item = bitcoin_reserve_intent
    result = review_intent(item.intent, item.manifest, item.key, item.registry, None,
                           board_address="known-original-board")
    assert result["status"] == "BTC_PUBLIC_PLAN_VERIFIED"
    assert len(item.checked_terms) == 1


@pytest.mark.parametrize("field,value", [
    ("signers", ["1", "3"]), ("max_fee", "3000"),
    ("deadline", "2000000001"), ("withdrawal_refs", ["01" * 32]),
])
def test_evm_reserve_review_rejects_displayed_plan_different_from_signed_arguments(bitcoin_reserve_intent, field, value):
    from signer_agent.bitcoin_onchain_payment import review_intent

    item = bitcoin_reserve_intent
    item.intent["bitcoin_plan"][field] = value
    with pytest.raises(ValueError, match="BTC_PUBLIC_PLAN_OPERATION_MISMATCH"):
        review_intent(item.intent, item.manifest, item.key, item.registry, None,
                      board_address="known-original-board")
    assert item.checked_terms == []


def test_evm_reserve_review_rejects_missing_public_plan(bitcoin_reserve_intent):
    from signer_agent.bitcoin_onchain_payment import review_intent

    item = bitcoin_reserve_intent
    item.intent.pop("bitcoin_plan")
    with pytest.raises(ValueError, match="BTC_PUBLIC_PLAN_REQUIRED"):
        review_intent(item.intent, item.manifest, item.key, item.registry, None,
                      board_address="known-original-board")
    assert item.checked_terms == []


@pytest.fixture
def settled_payment_observer(tmp_path, monkeypatch):
    from signer_agent import bitcoin_onchain

    state = SecureState(str(tmp_path / "observation.enc"), "test-only")
    manifest = SimpleNamespace(context=b"o" * 32)
    args = SimpleNamespace(bitcoin_rpc="http://local-test.invalid", bitcoin_cookie="local-test-cookie",
                           state=str(tmp_path / "observation.enc"))
    plan = dict(network="regtest", version="1", batch_registry="known-batches", batch_id="12" * 32,
                withdrawal_refs=["99" * 32])
    state.put("btc-registry:" + manifest.context.hex(), {"graph": {}})
    anchor = bytes(range(32))
    data = dict(paid=True, bound_txid=b"p" * 32, anchor=anchor[::-1], confirmation_anchor=6,
                confirmations=6, block=anchor.hex(), canonical=anchor.hex(), outage=False, responses={})
    calls = []

    def constant(value):
        return SimpleNamespace(call=lambda **kwargs: value)

    batches = SimpleNamespace(functions=SimpleNamespace(
        withdrawalEscrow=lambda: constant("known-escrow"), batchState=lambda _: constant(2),
        replacementOf=lambda _: constant(data.get("previous", bytes(32))),
        reservationRoot=lambda _: constant(data.get("root", bytes(32))),
    ))
    escrow = SimpleNamespace(functions=SimpleNamespace(
        payment=lambda batch: constant((data["bound_txid"], data["anchor"], data["paid"], [b"\x99" * 32])),
        depth=lambda: constant(6),
    ))
    monkeypatch.setattr(bitcoin_onchain, "contract", lambda registry, address, kind:
                        batches if kind == "BitcoinBatchRegistry" else escrow)

    class Bitcoin:
        def check_network(self):
            calls.append("network")

        def call(self, method, *params):
            calls.append(method)
            if data["outage"]:
                raise RuntimeError("PRIVATE_RPC_TOKEN_MUST_NOT_BE_RETURNED")
            if method in data["responses"]:
                response = data["responses"][method]
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(*params)
                return response
            if method == "getrawtransaction":
                if data["confirmations"] is None and data["block"] is None:
                    return dict(txid=params[0], hex="signed", hash="71" * 32)
                return dict(txid=params[0], hex="signed", hash="71" * 32,
                            confirmations=data["confirmations"], blockhash=data["block"])
            if method == "getmempoolentry" and data["confirmations"] is None and data["block"] is None:
                return dict(vsize=110)
            if method == "getblockheader":
                return dict(hash=params[0], height=100, confirmations=data["confirmation_anchor"])
            if method == "getblockhash":
                return data["canonical"]
            if method == "gettxoutproof":
                return "verified-proof"
            if method == "verifytxoutproof":
                return ["70" * 32] if not data.get("invalid_proof") else []
            pytest.fail("unexpected Bitcoin RPC")

    monkeypatch.setattr(bitcoin_onchain, "bitcoin_for_graph", lambda *args: Bitcoin())
    monkeypatch.setattr(bitcoin_onchain, "send", lambda *args: pytest.fail("observation sent transaction"))

    block = SimpleNamespace(number=100, hash=b"s" * 32)
    registry = SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda n: block)))
    monkeypatch.setattr(bitcoin_onchain, "confirmed_block", lambda *args: block)

    def observe():
        return bitcoin_onchain.payment_observation(args, manifest, registry, state, plan)

    return SimpleNamespace(state=state, manifest=manifest, args=args, registry=registry, plan=plan,
                           data=data, calls=calls, observe=observe)


def test_paid_evm_record_without_bitcoin_rpc_does_not_claim_fresh_confirmation(settled_payment_observer):
    item = settled_payment_observer
    item.args.bitcoin_rpc = None
    result = item.observe()
    assert result["status"] == "PAID"
    assert result["bitcoin_observation"] == "NOT_CHECKED"
    assert item.calls == []


def test_paid_bitcoin_confirmation_can_be_repeated_and_unpaid_is_not_confirmed(settled_payment_observer):
    item = settled_payment_observer
    assert item.observe()["bitcoin_observation"] == "CONFIRMED"
    assert item.observe()["bitcoin_observation"] == "CONFIRMED"
    assert item.state.get("btc-payments-recovery:" + item.manifest.context.hex()) is None
    item.data["paid"] = False
    item.calls.clear()
    assert item.observe() is None
    assert item.calls == []


@pytest.mark.parametrize("changed", [
    dict(block=None, confirmations=None, canonical="13" * 32),
    dict(confirmations=5, canonical="13" * 32), dict(canonical="13" * 32),
])
def test_conflicting_payment_block_keeps_recovery_sticky(settled_payment_observer, changed):
    item = settled_payment_observer
    assert item.observe()["bitcoin_observation"] == "CONFIRMED"
    item.data.update(changed)
    result = item.observe()
    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["reason"] == "SETTLED_PAYMENT_NOT_INDEPENDENTLY_CONFIRMED"
    assert result["error_code"] == "BITCOIN_PAYMENT_REORG"
    assert "PRIVATE_RPC_TOKEN" not in json.dumps(result)
    marker = item.state.get("btc-payments-recovery:" + item.manifest.context.hex())
    assert marker["batch"] == item.plan["batch_id"]
    item.data.update(confirmations=20, block="12" * 32, canonical="12" * 32, outage=False)
    item.calls.clear()
    assert item.observe() == result
    assert item.calls == []
    item.args.bitcoin_rpc = None
    assert item.observe() == result


@pytest.mark.parametrize("provider_behind", [True, False])
def test_same_chain_payment_with_older_rpc_automatically_resumes(settled_payment_observer, provider_behind):
    from signer_agent.bitcoin_rpc import BitcoinRPCError

    item = settled_payment_observer
    assert item.observe()["status"] == "PAID"
    item.data["confirmations"] = 4
    if provider_behind:
        item.data["confirmation_anchor"] = -1
        item.data["responses"]["getblockhash"] = BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-8)
    pending = item.observe()
    assert pending["status"] == "OBSERVATION_UNAVAILABLE" and not pending["requires_review"]
    if not provider_behind:
        assert pending["error_code"] == "BITCOIN_PAYMENT_DEPTH_PENDING"
    item.data["responses"].clear()
    item.data.update(confirmations=6, confirmation_anchor=6)
    assert item.observe()["status"] == "PAID"
    assert not item.state.get("btc-payments-recovery:" + item.manifest.context.hex())
    assert "verifytxoutproof" in item.calls


def test_post_settlement_outage_retries_and_clears_only_after_same_payment_proof(settled_payment_observer):
    item = settled_payment_observer
    item.data["outage"] = True
    result = item.observe()
    assert result["status"] == "OBSERVATION_UNAVAILABLE"
    assert result["error_code"] == "RuntimeError"
    assert not result["requires_review"]
    assert "PRIVATE_RPC_TOKEN" not in json.dumps(result)
    item.data["outage"] = False
    assert item.observe()["status"] == "PAID"
    assert not item.state.get("btc-payments-recovery:" + item.manifest.context.hex())
    assert "verifytxoutproof" in item.calls
    audit = item.state.get("btc-payments-recovered:" + item.manifest.context.hex() + ":" + item.plan["batch_id"])
    assert audit["manually_reviewed"] is False
    assert audit["previous"]["error_code"] == "RuntimeError"


def test_concurrent_identical_soft_marker_invalidates_inflight_recovery(settled_payment_observer):
    item = settled_payment_observer
    item.data["outage"] = True
    failure = item.observe()
    assert "revision" not in failure
    recovery_key = "btc-payments-recovery:" + item.manifest.context.hex()
    previous = item.state.get(recovery_key)
    item.data["outage"] = False

    def later_outage(*params):
        assert item.state.get(recovery_key) == previous
        del item.data["responses"]["gettxoutproof"]
        item.data["outage"] = True
        assert item.observe() == failure
        item.data["outage"] = False
        return "verified-proof"

    item.data["responses"]["gettxoutproof"] = later_outage
    with pytest.raises(ValueError, match="BTC_RECOVERY_CHANGED"):
        item.observe()
    assert item.state.get(recovery_key)["revision"] != previous["revision"]


def test_first_successful_observation_cannot_report_paid_over_a_new_pause(settled_payment_observer):
    item = settled_payment_observer
    recovery_key = "btc-payments-recovery:" + item.manifest.context.hex()

    def check_block(*args):
        del item.data["responses"]["getblockhash"]
        item.data["canonical"] = "13" * 32
        assert item.observe()["status"] == "RECOVERY_REQUIRED"
        return "12" * 32

    item.data["responses"]["getblockhash"] = check_block
    with pytest.raises(ValueError, match="BTC_RECOVERY_CHANGED"):
        item.observe()
    assert item.state.get(recovery_key)["requires_review"]


def test_post_settlement_incomplete_core_response_can_be_retried_without_review(settled_payment_observer):
    item = settled_payment_observer
    item.data["responses"]["getrawtransaction"] = dict(txid="70" * 32, hex="signed", confirmations=6)
    failure = item.observe()
    assert failure["status"] == "OBSERVATION_UNAVAILABLE"
    assert failure["error_code"] == "BITCOIN_OBSERVATION_UNAVAILABLE"
    endpoint = item.args.bitcoin_rpc
    item.args.bitcoin_rpc = None
    assert item.observe() == failure
    item.data["responses"].clear()
    item.args.bitcoin_rpc = endpoint
    assert item.observe()["status"] == "PAID"
    assert "verifytxoutproof" in item.calls


@pytest.mark.parametrize("anchor_state,status", [
    ("canonical", "OBSERVATION_UNAVAILABLE"),
    ("unknown", "OBSERVATION_UNAVAILABLE"),
    ("mismatched_header", "OBSERVATION_UNAVAILABLE"),
    ("orphaned", "RECOVERY_REQUIRED"),
    ("canonical_replaced", "RECOVERY_REQUIRED"),
])
def test_missing_settled_tx_requires_real_anchor_reorg_evidence(settled_payment_observer, anchor_state, status):
    from signer_agent.bitcoin_rpc import BitcoinRPCError

    item = settled_payment_observer
    responses = item.data["responses"]
    responses["getrawtransaction"] = BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-5)
    if anchor_state == "unknown":
        responses["getblockheader"] = BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-5)
    if anchor_state == "mismatched_header":
        responses["getblockheader"] = dict(height=100, hash="44" * 32, confirmations=-1)
    if anchor_state == "orphaned":
        item.data["confirmation_anchor"] = -1
        item.data["canonical"] = "44" * 32
    if anchor_state == "canonical_replaced":
        item.data["canonical"] = "44" * 32
    failure = item.observe()
    assert "getblockheader" in item.calls
    assert failure["status"] == status
    if status == "RECOVERY_REQUIRED":
        assert failure["error_code"] == "BITCOIN_PAYMENT_REORG"
    else:
        assert failure["rpc_error_code"] == -5
        responses.clear()
        assert item.observe()["status"] == "PAID"
        assert "verifytxoutproof" in item.calls


@pytest.mark.parametrize("action,core_ready", [
    ("btc-recover", True), ("btc-sign", False), ("btc-sign", True),
])
def test_mainnet_paid_recovery_dispatch_precedes_live_relay_preflight(
        settled_payment_observer, monkeypatch, action, core_ready):
    from signer_agent import bitcoin_onchain as transport

    item = settled_payment_observer
    if action == "btc-recover":
        canonical = item.data["canonical"]
        item.data["canonical"] = "13" * 32
        assert item.observe()["status"] == "RECOVERY_REQUIRED"
        item.data["canonical"] = canonical
    else:
        item.data["outage"] = True
        assert item.observe()["status"] == "OBSERVATION_UNAVAILABLE"
        item.data["outage"] = not core_ready
    item.plan["network"] = "mainnet"
    graph = dict(contracts=dict(batches=dict(address=item.plan["batch_registry"])))
    item.state.put("btc-registry:" + item.manifest.context.hex(), dict(graph=graph))
    item.args.action = action
    item.args.batch = item.plan["batch_id"]
    item.args.signing_board = "known-board"
    monkeypatch.setattr(transport, "graph_network", lambda _: "mainnet")
    monkeypatch.setattr(transport, "require_registry", lambda *args, **kwargs: dict(graph=graph))
    monkeypatch.setattr(transport, "checked_board", lambda *args: SimpleNamespace(address=item.args.signing_board))
    monkeypatch.setattr(transport, "load_plan", lambda *args: item.plan)
    monkeypatch.setattr(transport, "cached_plan", lambda *args: item.plan)
    def stale_relay(*args):
        assert action == "btc-sign" and core_ready
        assert not item.state.get("btc-payments-recovery:" + item.manifest.context.hex())
        raise ValueError("MAINNET_RELAY_STALE")

    monkeypatch.setattr(transport, "check_bitcoin_graph", stale_relay)
    if action == "btc-sign" and core_ready:
        with pytest.raises(ValueError, match="MAINNET_RELAY_STALE"):
            transport.dispatch(item.args, item.manifest, None, item.registry, item.state)
        return
    observed = transport.dispatch(item.args, item.manifest, None, item.registry, item.state)
    assert observed["status"] == ("PAYMENTS_RESUMED" if action == "btc-recover" else "OBSERVATION_UNAVAILABLE")


@pytest.mark.parametrize("case", ["paid", "pending_mempool", "pending_block", "pending_replacement", "missing_tx",
                                  "unbound", "mismatched_tx", "mismatched_requests", "mismatched_replacement"])
def test_operator_cold_restart_checks_exact_payment_before_live_relay(
        settled_payment_observer, monkeypatch, case):
    from signer_agent import bitcoin_onchain as transport, bitcoin_onchain_payment as payment
    from signer_agent import bitcoin_worker
    from signer_agent.bitcoin_rpc import BitcoinRPCError

    item = settled_payment_observer
    item.plan["network"] = "mainnet"
    item.data["paid"] = case == "paid"
    item.args.action = "btc-broadcast"
    item.args.batch = item.plan["batch_id"]
    item.args.signing_board = "known-board"
    test_graph = dict(contracts=dict(batches=dict(address=item.plan["batch_registry"])))
    item.state.put("btc-registry:" + item.manifest.context.hex(), dict(graph=test_graph))
    monkeypatch.setattr(transport, "graph_network", lambda _: "mainnet")
    monkeypatch.setattr(transport, "require_registry", lambda *args, **kwargs: dict(graph=test_graph))
    monkeypatch.setattr(transport, "checked_board", lambda *args: SimpleNamespace(address=item.args.signing_board))
    monkeypatch.setattr(transport, "load_plan", lambda *args: item.plan)
    monkeypatch.setattr(payment, "_txid", lambda _: b"p" * 32)

    def stale_relay(*args):
        assert case in ("missing_tx", "unbound")
        item.calls.append("relay")
        raise ValueError("MAINNET_RELAY_STALE")

    def collect(*args):
        assert case in ("missing_tx", "unbound")
        return [], [], [], []

    def unexpected(*args, **kwargs):
        pytest.fail("existing-payment observation must not send or aggregate")

    remembered = []
    monkeypatch.setattr(transport, "check_bitcoin_graph", stale_relay)
    monkeypatch.setattr(transport, "collect", collect)
    monkeypatch.setattr(transport.crypto, "call", unexpected)
    monkeypatch.setattr(bitcoin_worker.BitcoinWorker, "broadcast", unexpected)
    monkeypatch.setattr(bitcoin_worker.BitcoinWorker, "remember_observed_broadcast",
                        lambda _, batch, signed, *, replacement: remembered.append((batch, signed, replacement)))
    expected_ancestry = None
    if case in ("pending_replacement", "mismatched_replacement"):
        item.plan.update(version="5", previous="20" * 32)
        item.data.update(root=b"\x21" * 32, previous=b"\x20" * 32)
        expected_ancestry = ("21" * 32, "20" * 32)
        if case == "mismatched_replacement":
            item.plan["previous"] = "22" * 32
    if case == "pending_mempool":
        item.data.update(block=None, confirmations=None)
    if case == "missing_tx":
        item.data["responses"]["getrawtransaction"] = BitcoinRPCError("BITCOIN_RPC_REJECTED", rpc_code=-5)
    if case == "unbound":
        item.data["bound_txid"] = bytes(32)
    if case == "mismatched_tx":
        item.data["bound_txid"] = b"q" * 32
    if case == "mismatched_requests":
        item.plan["withdrawal_refs"] = ["98" * 32]
    if case in ("missing_tx", "unbound"):
        with pytest.raises(ValueError, match="MAINNET_RELAY_STALE"):
            transport.dispatch(item.args, item.manifest, None, item.registry, item.state)
        assert item.calls[-1] == "relay"
        return
    if case in ("mismatched_tx", "mismatched_requests"):
        with pytest.raises(ValueError, match="ESCROW_PAYMENT_MISMATCH"):
            transport.dispatch(item.args, item.manifest, None, item.registry, item.state)
        assert not item.calls
        return
    if case == "mismatched_replacement":
        with pytest.raises(ValueError, match="BATCH_APPROVAL_MISMATCH"):
            transport.dispatch(item.args, item.manifest, None, item.registry, item.state)
        assert not remembered
        return
    observed = transport.dispatch(item.args, item.manifest, None, item.registry, item.state)
    assert observed["txid"] == "70" * 32
    if case == "paid":
        assert observed["status"] == "PAID" and observed["bitcoin_observation"] == "CONFIRMED"
        assert remembered == []
    else:
        assert observed["status"] == "BROADCAST" and observed["wtxid"] == "71" * 32
        assert remembered == [
            (item.plan["batch_id"], dict(signed="signed", txid="70" * 32, wtxid="71" * 32), expected_ancestry),
        ]
    assert item.calls.count("getrawtransaction") == 1
    assert ("getmempoolentry" in item.calls) == (case == "pending_mempool")


def test_verified_observed_transaction_is_retained_atomically_for_replacement(tmp_path):
    from signer_agent.bitcoin_proofs import sha256d
    from signer_agent.bitcoin_worker import BitcoinWorker

    def payment(value, *, prevout="ab" * 32):
        # Canonical witness envelope, parsed locally. Core is never called by this test.
        version = "02000000"
        inputs = "01" + prevout + "0000000000fdffffff"
        outputs = "01" + value.to_bytes(8, "little").hex() + "0151"
        locktime = "00000000"
        stripped = version + inputs + outputs + locktime
        raw = version + "0001" + inputs + outputs + "010101" + locktime
        return dict(signed=raw, txid=sha256d(bytes.fromhex(stripped))[::-1].hex(),
                    wtxid=sha256d(bytes.fromhex(raw))[::-1].hex())

    worker = BitcoinWorker(tmp_path / "bitcoin.sqlite", None)
    old, replacement, unrelated = payment(1000), payment(900), payment(800, prevout="cd" * 32)
    worker.remember_observed_broadcast("root", old)
    worker.remember_observed_broadcast("root", old)
    with pytest.raises(ValueError, match="TXID_MISMATCH"):
        worker.remember_observed_broadcast("bad-witness", dict(replacement, wtxid="00" * 32))
    with pytest.raises(ValueError, match="BATCH_VERSION_CONFLICT"):
        worker.remember_observed_broadcast("root", replacement)
    with pytest.raises(ValueError, match="BATCH_VERSION_CONFLICT"):
        worker.remember_observed_broadcast("different-batch", old)
    with pytest.raises(ValueError, match="REPLACEMENT_INPUTS_MISMATCH"):
        worker.remember_observed_broadcast("bad-branch", unrelated, replacement=("root", "root"))
    worker.remember_observed_broadcast("next", replacement, replacement=("root", "root"))
    with pytest.raises(ValueError, match="BATCH_VERSION_CONFLICT"):
        worker.remember_observed_broadcast("next", replacement, replacement=("other-root", "root"))
    with worker.db() as db:
        db.execute("UPDATE payments SET status='SETTLED', proof='proof' WHERE id='next'")
    worker.remember_observed_broadcast("next", replacement, replacement=("root", "root"))
    with worker.db() as db:
        assert db.execute("SELECT id,signed,status,proof FROM payments ORDER BY id").fetchall() == [
            ("next", replacement["signed"], "SETTLED", "proof"), ("root", old["signed"], "BROADCAST", None),
        ]
        assert db.execute("SELECT id,root,previous FROM versions").fetchall() == [("next", "root", "root")]


def test_post_settlement_reorg_requires_explicit_checked_recovery(settled_payment_observer):
    from signer_agent.bitcoin_onchain import payment_observation

    item = settled_payment_observer
    canonical = item.data["canonical"]
    item.data["canonical"] = "13" * 32
    assert item.observe()["status"] == "RECOVERY_REQUIRED"
    item.data["canonical"] = canonical
    assert item.observe()["status"] == "RECOVERY_REQUIRED"
    result = payment_observation(item.args, item.manifest, item.registry, item.state, item.plan, recover=True)
    assert result["status"] == "PAID"
    assert not item.state.get("btc-payments-recovery:" + item.manifest.context.hex())
    assert item.state.get("btc-payments-recovered:" + item.manifest.context.hex() + ":" + item.plan["batch_id"])["manually_reviewed"]


@pytest.mark.parametrize("changed", [dict(confirmations=0), dict(invalid_proof=True), dict(outage=True)])
def test_checked_recovery_cannot_clear_an_unverified_payment(settled_payment_observer, changed):
    from signer_agent.bitcoin_onchain import payment_observation

    item = settled_payment_observer
    canonical = item.data["canonical"]
    item.data["canonical"] = "13" * 32
    assert item.observe()["status"] == "RECOVERY_REQUIRED"
    item.data["canonical"] = canonical
    item.data.update(changed)
    result = payment_observation(item.args, item.manifest, item.registry, item.state, item.plan, recover=True)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert item.state.get("btc-payments-recovery:" + item.manifest.context.hex())


def test_recovery_must_recheck_the_original_batch(settled_payment_observer):
    from signer_agent.bitcoin_onchain import payment_observation

    item = settled_payment_observer
    item.data["canonical"] = "13" * 32
    assert item.observe()["status"] == "RECOVERY_REQUIRED"
    with pytest.raises(ValueError, match="BTC_RECOVERY_BATCH_MISMATCH"):
        payment_observation(item.args, item.manifest, item.registry, item.state,
                            dict(item.plan, batch_id="13" * 32), recover=True)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_bitcoin_worker_recovery_db_rejects_substituted_files(tmp_path, suffix, link_kind):
    from signer_agent.bitcoin_worker import BitcoinWorker

    target = tmp_path / ("payments.sqlite" + suffix)
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"private unrelated bytes")
    if link_kind == "symlink":
        target.symlink_to(victim)
    else:
        os.link(victim, target)
    with pytest.raises((ValueError, OSError)):
        BitcoinWorker(tmp_path / "payments.sqlite", None)
    assert victim.read_bytes() == b"private unrelated bytes"


def test_bitcoin_worker_recovery_db_rejects_shared_writable_directory(tmp_path):
    from signer_agent.bitcoin_worker import BitcoinWorker

    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o777)
    with pytest.raises(ValueError, match="JOURNAL_DIRECTORY_PERMISSIONS"):
        BitcoinWorker(directory / "payments.sqlite", None)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("action,extra", [
    ("btc-prepare", []), ("btc-propose", []), ("btc-execute", []),
    ("btc-publish-plan", []), ("btc-broadcast", []), ("btc-settle", []),
    ("btc-commit", ["--signing-board", "0x0000000000000000000000000000000000000001"]),
    ("btc-sign", ["--signing-board", "0x0000000000000000000000000000000000000001"]),
    ("btc-commit", ["--batch", "11" * 32]),
    ("btc-sign", ["--batch", "11" * 32]),
])
def test_cli_cannot_mix_file_package_with_onchain_payment_mode(tmp_path, monkeypatch, capsys, action, extra):
    from signer_agent import onboarding, participant_cli

    monkeypatch.setattr(onboarding, "password_file", lambda *args: pytest.fail("conflict opened secrets"))
    result = participant_cli.main([
        action, "--package", "untrusted-file.json", "--state", str(tmp_path / "never-created.enc"),
        "--password-file", "unused", *extra,
    ])
    assert result == 2
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "ERROR" and response["code"] == "BTC_FILE_AND_ONCHAIN_MODES_CONFLICT"
    assert response["message"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_legacy_bitcoin_signer_cannot_bypass_wallet_recovery_pause(bitcoin_signer_boundary, action):
    signer = bitcoin_signer_boundary
    # The legacy file mode invokes this same worker directly. Its atomic boundary
    # must check recovery again even if a caller already checked before the lock.
    context = signer.sid.split(":")[1]
    with signer.state.exclusive():
        signer.state.put("btc-payments-recovery:" + context,
                         dict(reason="SETTLED_PAYMENT_NOT_INDEPENDENTLY_CONFIRMED"))
    with pytest.raises(ValueError, match="PAYMENTS_PAUSED_FOR_RECOVERY"):
        signer.step(action)
    assert signer.calls == ["nonce"]


@pytest.fixture
def cached_bitcoin_transport(public_bitcoin_plan, tmp_path, monkeypatch):
    """Two inputs with durable public packets, already beyond the plan deadline."""
    from eth_abi import encode
    from eth_utils import keccak
    from signer_agent import bitcoin_onchain as transport
    from signer_agent.bitcoin_onchain_plan import onchain_plan_hash

    manifest, plan = public_bitcoin_plan
    manifest.context = b"t" * 32
    manifest.purpose = "BTC_SPEND"
    plan["inputs"].append(dict(outpoint="ab" * 32 + ":1", amount="10000",
                               script=plan["wallet_script"]))
    plan["outputs"][0]["amount"] = "19000"
    key = dict(index=1, participant=manifest.participants[0], result_hash=plan["wallet_result"],
               result=dict(group="02" + plan["wallet_script"][4:]))
    state = SecureState(str(tmp_path / "cached.enc"), "synthetic-cached-state-key")
    state.put("btc-registry:" + manifest.context.hex(),
              {"graph": {"contracts": {"batches": {"address": plan["batch_registry"]}}}})
    h = onchain_plan_hash(manifest, plan)
    built = crypto.call("bitcoin_build", **{k: plan[k] for k in
                        ("inputs", "outputs", "wallet_script", "fee", "max_fee")})
    commitments, sids, nonce_states = [], [], []
    for i, message in enumerate(built["sighashes"]):
        nonce = crypto.call("nonce", share="00" * 31 + "01")
        peer = crypto.call("nonce", share="00" * 31 + "02")
        values = {"1": nonce["commitments"], "2": peer["commitments"]}
        commitments.append(values)
        sid = "btc:" + manifest.context.hex() + ":" + plan["batch_id"] + ":" + str(i)
        sids.append(sid)
        nonce.update(status="nonce-created", message_hash=message, plan_hash=h.hex())
        nonce_states.append(dict(nonce))
        state.put(sid, nonce)
        transcript = keccak(encode(
            ["bytes32", "bytes32", "uint16[]", "bytes[]"],
            [h, bytes.fromhex(message), [1, 2], [bytes.fromhex(values[str(x)]) for x in (1, 2)]],
        )).hex()
        with state._journal() as db:
            db.execute("INSERT INTO spent VALUES (?,?)", (sid, transcript))
        nonce.pop("nonces")
        nonce.update(status="share-prepared", transcript=transcript, share="00" * 31 + "01")
        state.put(sid, nonce)

    block = AttributeDict(dict(number=33, hash=b"b" * 32, timestamp=int(plan["deadline"]) + 1))
    registry = SimpleNamespace(account=SimpleNamespace(address=key["participant"]),
                               w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=lambda number: block)))
    approved = [h, bytes.fromhex(key["result_hash"]), int(plan["deadline"]), 2]
    batches = SimpleNamespace(functions=SimpleNamespace(batches=lambda batch: SimpleNamespace(
        call=lambda **kwargs: approved), feeReserve=lambda: SimpleNamespace(call=lambda **kwargs: "0x"+"00"*20)))
    registry.w3.eth.contract=lambda **kwargs: batches
    monkeypatch.setattr(transport, "require_registry", lambda *args: {"graph": {}})
    monkeypatch.setattr(transport, "contract", lambda *args: batches)
    calls = []
    original_crypto = crypto.call

    def public_only(op, **params):
        calls.append(op)
        assert op in ("bitcoin_build", "inspect_bitcoin_packet"), "retry used secret crypto operation"
        return original_crypto(op, **params)

    monkeypatch.setattr(crypto, "call", public_only)
    board = SimpleNamespace(address=plan["signing_board"])
    publications = []
    monkeypatch.setattr(transport, "checked_board", lambda *args: board)
    monkeypatch.setattr(transport, "load_plan", lambda *args: plan)
    monkeypatch.setattr(transport, "collect", lambda *args: (commitments, [], [], []))
    monkeypatch.setattr(transport, "publish_values", lambda *args: publications.append(args[-1]))

    def dispatch(action):
        args = SimpleNamespace(action=action, signing_board=board.address, batch=plan["batch_id"],
                               bitcoin_rpc="http://unused.invalid", bitcoin_cookie="/nonexistent-cookie")
        return transport.dispatch(args, manifest, key, registry, state)

    def cached(action):
        work = dict(plan, commitments=commitments) if action == "btc-sign" else plan
        return transport.cached_transport_step(action, manifest, key, work, registry, state)

    return SimpleNamespace(manifest=manifest, plan=plan, key=key, state=state, sids=sids,
                           nonce_states=nonce_states, commitments=commitments, registry=registry,
                           approved=approved, block=block, calls=calls, publications=publications,
                           dispatch=dispatch, cached=cached)


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_fully_cached_transport_retry_needs_no_bitcoin_cookie_or_unspent_inputs(cached_bitcoin_transport, monkeypatch, action):
    from signer_agent import bitcoin_worker

    replay = cached_bitcoin_transport
    before = [replay.state.get(sid) for sid in replay.sids]
    monkeypatch.setattr(bitcoin_worker, "BitcoinRPC", lambda *args: pytest.fail("cached retry opened Bitcoin RPC"))
    monkeypatch.setattr(bitcoin_worker, "sign_step", lambda *args: pytest.fail("cached retry recomputed share"))
    result = replay.dispatch(action)
    expected = [value["commitments" if action == "btc-commit" else "share"] for value in before]
    assert replay.publications == [expected]
    assert result["published"] == 2
    assert result["status"] == ("COMMITTED" if action == "btc-commit" else "SIGNED")
    assert [replay.state.get(sid) for sid in replay.sids] == before
    assert replay.calls.count("bitcoin_build") == 1


def test_cached_commitment_can_resume_before_share_but_never_spends_nonce(cached_bitcoin_transport):
    replay = cached_bitcoin_transport
    with replay.state._journal() as db:
        db.execute("DELETE FROM spent")
    for sid, value in zip(replay.sids, replay.nonce_states):
        replay.state.put(sid, value)
    assert replay.cached("btc-commit")["values"] == [value["commitments"] for value in replay.nonce_states]
    assert replay.cached("btc-sign") is None
    assert [replay.state.get(sid) for sid in replay.sids] == replay.nonce_states
    with replay.state._journal() as db:
        assert db.execute("SELECT * FROM spent").fetchall() == []


@pytest.mark.parametrize("policy_error", ["BATCH_EXPIRED", "UTXO_MISMATCH"])
def test_partial_cached_share_still_requires_full_first_sign_validation(cached_bitcoin_transport, monkeypatch, policy_error):
    from signer_agent import bitcoin_trust, bitcoin_worker

    replay = cached_bitcoin_transport
    sid = replay.sids[-1]
    with replay.state._journal() as db:
        db.execute("DELETE FROM spent WHERE session=?", (sid,))
    replay.state.put(sid, replay.nonce_states[-1])
    monkeypatch.setattr(bitcoin_trust, "require_registry", lambda *args: {"graph": {}})
    from signer_agent import bitcoin_onchain
    monkeypatch.setattr(bitcoin_onchain, "bitcoin_for_graph", lambda *args: object())
    checked = []

    def first_sign_validation(*args):
        checked.append(True)
        raise ValueError(policy_error)

    monkeypatch.setattr(bitcoin_worker, "validate", first_sign_validation)
    with pytest.raises(ValueError, match=policy_error):
        replay.dispatch("btc-sign")
    assert checked == [True]
    assert replay.calls == []
    assert replay.publications == []
    assert replay.state.get(sid) == replay.nonce_states[-1]


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_cached_transport_never_bypasses_recovery_pause(cached_bitcoin_transport, action):
    replay = cached_bitcoin_transport
    replay.state.put("btc-payments-recovery:" + replay.manifest.context.hex(), dict(reason="test"))
    with pytest.raises(ValueError, match="PAYMENTS_PAUSED_FOR_RECOVERY"):
        replay.cached(action)
    assert replay.calls == []


@pytest.mark.parametrize("damage,error", [
    ("missing_journal", "NONCE_JOURNAL_MISSING"),
    ("missing_spend", "NONCE_SPEND_PROOF_MISSING"),
    ("wrong_spend", "NONCE_SPEND_TRANSCRIPT_MISMATCH"),
    ("peer_commitment", "NONCE_TRANSCRIPT_MISMATCH"),
    ("own_commitment", "NONCE_COMMITMENT_MISMATCH"),
    ("plan_hash", "NONCE_PLAN_MISMATCH"),
    ("message_hash", "NONCE_PLAN_MISMATCH"),
    ("nonce_retained", "BTC_CACHED_SIGNING_STATE_INVALID"),
    ("state_status", "BTC_CACHED_SIGNING_STATE_INVALID"),
    ("subset", "SIGNER_SUBSET_INVALID"),
    ("share", "CRYPTO_VALIDATION_FAILED"),
])
def test_cached_transport_refuses_corrupt_transcript_or_journal(cached_bitcoin_transport, damage, error):
    replay = cached_bitcoin_transport
    sid = replay.sids[0]
    value = replay.state.get(sid)
    if damage == "missing_journal":
        os.unlink(str(replay.state.path) + ".spent.sqlite")
    elif damage in ("missing_spend", "wrong_spend"):
        with replay.state._journal() as db:
            if damage == "missing_spend":
                db.execute("DELETE FROM spent WHERE session=?", (sid,))
            else:
                db.execute("UPDATE spent SET transcript=? WHERE session=?", ("44" * 32, sid))
    elif damage in ("peer_commitment", "own_commitment"):
        who = "2" if damage == "peer_commitment" else "1"
        replay.commitments[0][who] = replay.commitments[1][who]
    elif damage == "subset":
        replay.commitments[0]["3"] = replay.commitments[0].pop("2")
    else:
        if damage == "nonce_retained":
            value["nonces"] = replay.nonce_states[0]["nonces"]
        elif damage == "state_status":
            value["status"] = "unknown"
        else:
            value[damage] = "ff" * 32
        replay.state.put(sid, value)
    with pytest.raises((ValueError, RuntimeError), match=error):
        replay.cached("btc-sign")
    assert replay.publications == []


@pytest.mark.parametrize("action", ["btc-commit", "btc-sign"])
def test_cached_transport_requires_current_signing_approval(cached_bitcoin_transport, action):
    replay = cached_bitcoin_transport
    replay.approved[3] = 3
    with pytest.raises(ValueError, match="BATCH_APPROVAL_MISMATCH"):
        replay.cached(action)
    replay.approved[3] = 1
    assert replay.cached(action) is None
    replay.approved[3] = 2
    replay.approved[0] = b"x" * 32
    with pytest.raises(ValueError, match="BATCH_APPROVAL_MISMATCH"):
        replay.cached(action)
    assert replay.calls == []


def test_cached_transport_rechecks_snapshot_after_public_crypto(cached_bitcoin_transport):
    replay = cached_bitcoin_transport
    orphan = AttributeDict(dict(number=replay.block.number, hash=b"o" * 32))
    replay.registry.w3.eth.get_block = lambda number: replay.block if number == "latest" else orphan
    with pytest.raises(ValueError, match="BTC_BOARD_SNAPSHOT_REORG"):
        replay.cached("btc-sign")


def test_cached_transport_stale_nonce_without_saved_share_never_reexports_commitment(cached_bitcoin_transport):
    replay = cached_bitcoin_transport
    replay.state.put(replay.sids[0], replay.nonce_states[0])
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        replay.cached("btc-commit")


def test_review_plan_reads_confirmed_wallet_and_operation_for_regtest(bitcoin_reserve_intent, graph, monkeypatch):
    """EVM confirmation policy remains binding even when Bitcoin is regtest."""
    from signer_agent import bitcoin_onchain_payment as payment, bitcoin_trust
    item = bitcoin_reserve_intent
    item.registry.bitcoin_graph = dict(version="2", bitcoin_network="regtest", evm_confirmations="3")
    confirmed = SimpleNamespace(number=88, hash=graph.block.hash)
    monkeypatch.setattr(bitcoin_trust, "confirmed_block", lambda *args: confirmed)
    original = payment._contract
    seen = []

    class Functions:
        def __init__(self, target):
            self.target = target

        def __getattr__(self, method):
            def build(*args):
                target = getattr(self.target, method)(*args)
                def call(**kwargs):
                    assert kwargs == {"block_identifier": 88}
                    seen.append(method)
                    return target.call(**kwargs)
                return SimpleNamespace(call=call)
            return build

    def contract(*args):
        target = original(*args)
        return SimpleNamespace(address=target.address, functions=Functions(target.functions))

    monkeypatch.setattr(payment, "_contract", contract)
    monkeypatch.setattr(payment, "_terms", lambda contracts, plan, bitcoin, block:
                        seen.append("terms") if block == 88 else pytest.fail("unconfirmed obligations"))
    assert payment.review_intent(item.intent, item.manifest, item.key, item.registry, None,
                                board_address="known-original-board")["status"] == "BTC_PUBLIC_PLAN_VERIFIED"
    assert {"getCompletedResult", "transportKeys", "getPlanHash", "walletResult", "operationHash", "terms"} <= set(seen)


def test_review_reorg_after_terms_never_returns_verified(bitcoin_reserve_intent, graph, monkeypatch):
    from signer_agent import bitcoin_onchain_payment as payment
    item = bitcoin_reserve_intent
    def changed(*args):
        item.registry.w3.eth.get_block = lambda _: SimpleNamespace(timestamp=100, hash=b"reorg")
    monkeypatch.setattr(payment, "_terms", changed)
    with pytest.raises(ValueError, match="BTC_OPERATION_REORG"):
        payment.review_intent(item.intent, item.manifest, item.key, item.registry, None,
                              board_address="known-original-board")
