"""Exercise durable mint recovery through the real funding dispatcher."""
from types import SimpleNamespace

import pytest
from eth_utils import keccak
from web3 import Web3

from signer_agent import bitcoin_funding as funding, bitcoin_onchain, custody_mint
from signer_agent.bootstrap import Bootstrap
from signer_agent.operations import canonical
from signer_agent.secure_state import SecureState


def address(value):
    return Web3.to_checksum_address("0x" + f"{value:040x}")


def value(result):
    return SimpleNamespace(call=lambda **kwargs: result)


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    state = SecureState(str(tmp_path / "recovery.enc"), "test-only")
    manifest = SimpleNamespace(chain_id=31337, context=b"context")
    graph = dict(evm_dkg_id="aa" * 32, evm_result="bb" * 32)
    args = [b"t" * 32, 0, [address(3)], [10**10]]
    digest = b"\x22" * 32
    expected = dict(chain_id="31337", coordinator=address(2), role="conf_ceremony",
        target=address(1), artifact="MiningPoolV1", hash_method="getMintOperationHash",
        hash_args=Bootstrap.wire(args), operation_hash=digest.hex())
    key = "btc-funding:" + manifest.context.hex() + ":" + keccak(canonical(
        [address(1), "getMintOperationHash", Bootstrap.wire(args)]).encode()).hex()
    view = SimpleNamespace(complete=False, nonce_reads=[], sends=[], sessions={
        7: dict(birth=10, deadline=1000, original=1000, state=2, digest=b"\x11" * 32),
        8: dict(birth=20, deadline=1000, original=1000, state=2, digest=digest)})

    def block(number):
        number = 30 if number == "latest" else int(number)
        return SimpleNamespace(number=number, hash=number.to_bytes(32, "big"), timestamp=100)

    def session(identifier):
        def read(block_identifier="latest"):
            row = view.sessions[identifier]
            info = [0] * 20
            if block_identifier != "latest" and int(block_identifier) < row["birth"]:
                return info
            info[0], info[3], info[4] = identifier, row["digest"], True
            info[7], info[10], info[15] = row["deadline"], row["state"], address(1)
            return info
        return SimpleNamespace(call=read)

    def logs(from_block, to_block, argument_filters):
        identifier = argument_filters["sessionId"]
        row = view.sessions[identifier]
        assert from_block == to_block == row["birth"]
        return [dict(args=dict(sessionId=identifier, deadline=row["original"]),
                     blockHash=block(from_block).hash)]

    coordinator = SimpleNamespace(address=address(2), functions=SimpleNamespace(
        getSession=session, sessionDKGId=lambda _: value(bytes.fromhex(graph["evm_dkg_id"])),
        sessionDKGResult=lambda _: value(bytes.fromhex(graph["evm_result"]))),
        events=SimpleNamespace(SessionCreated=lambda: SimpleNamespace(get_logs=logs)))

    def nonce():
        def read(block_identifier):
            view.nonce_reads.append(block_identifier)
            assert block_identifier == 30, "legacy receipt nonce must not be inferred"
            return 5
        return SimpleNamespace(call=read)

    target = SimpleNamespace(address=address(1), functions=SimpleNamespace(
        getMintOperationHash=lambda *_: value(digest), nonce=nonce,
        mintedAddressIntents=lambda _: value(bytes(32)),
        mintProofVersion=lambda: value(getattr(view, "mint_version", 1)),
        mintAdmission=lambda: value(address(0))))
    contracts = dict(coordinator=coordinator, deposits=SimpleNamespace(functions=SimpleNamespace(
        consumed=lambda _:value(view.complete))), authorization=SimpleNamespace(address=address(5),
        functions=SimpleNamespace(isOperationApproved=lambda *_: value(True))))
    current = dict(session="8", intent=dict(expected, session="8"),
                   anchor=dict(number="25", hash=block(25).hash.hex()))
    transport = SimpleNamespace(address=address(6), functions=SimpleNamespace(
        operator=lambda: value(address(9)), intents=lambda _: value(canonical(current["intent"]).encode())))
    registry = SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(get_block=block)),
                               account=SimpleNamespace(address=address(9)))
    monkeypatch.setattr(funding, "_graph", lambda *_: (graph, contracts))
    monkeypatch.setattr(funding, "confirmed_block", lambda *_: block(30))
    monkeypatch.setattr(funding, "_mining", lambda *_: target)
    monkeypatch.setattr(funding, "_mint_args", lambda _: args)
    monkeypatch.setattr(funding, "_mint_complete", lambda *_: view.complete)
    monkeypatch.setattr(custody_mint, "_mint_complete", lambda *_: view.complete)
    monkeypatch.setattr(funding, "_bitcoin", lambda *_: object())
    monkeypatch.setattr(funding, "check_bitcoin_graph", lambda *_: None)
    monkeypatch.setattr(funding, "_mint_proof", lambda *_: args)
    monkeypatch.setattr(funding, "_auth_board", lambda *_: (transport, "conf_ceremony"))
    monkeypatch.setattr(funding, "_find_session", lambda *_: current)
    monkeypatch.setattr(funding, "birth_block", lambda *_: 5)
    monkeypatch.setattr(bitcoin_onchain, "snapshot_check", lambda *_: None)

    def send(*args, **kwargs):
        view.sends.append(args)
        assert args[3] == "mintFromProof"
        view.complete = True
    monkeypatch.setattr(bitcoin_onchain, "send", send)

    def dispatch(deadline=1000):
        return funding.dispatch(SimpleNamespace(action="btc-mint-execute", deadline=deadline),
            manifest, dict(result=dict(group="02" + "11" * 32)), registry, state, object())

    return SimpleNamespace(state=state, key=key, expected=expected, view=view, dispatch=dispatch,
        old=dict(session="7", intent=dict(expected, operation_hash="11" * 32, session="7"),
                 anchor=dict(number="15", hash=block(15).hash.hex())))


def test_client_refuses_unknown_generation_profile_before_paid_work_or_signing(recovery):
    # V2 is supported by custody_mint; an unknown version must still fail closed.
    recovery.view.mint_version = 3
    with pytest.raises(ValueError, match="BTC_MINT_GENERATION_PROFILE_REQUIRED"):
        recovery.dispatch()
    assert recovery.view.sends == []


def test_exact_completed_allocation_recovers_even_when_profile_requires_new_client(recovery):
    recovery.view.mint_version = 2
    recovery.view.complete = True
    assert recovery.dispatch()["status"] == "BTC_FUNDING_EXECUTED"
    assert recovery.view.sends == []


@pytest.mark.parametrize("old_state", [2, 3])
def test_receipt_nonce_race_migrates_exact_legacy_history_without_inventing_nonce(recovery, old_state):
    env = recovery
    env.state.put(env.key, env.old)
    env.view.sessions[7]["state"] = old_state
    result = env.dispatch()
    assert result["status"] == "BTC_FUNDING_EXECUTED"
    assert env.state.get(env.key) == env.old
    rows = env.state.get(env.key + ":attempts-v1")["attempts"]
    assert len(rows) == 2
    assert rows[0]["operation_nonce"] is None and rows[0]["status"] == "LEGACY_NONCE_UNKNOWN"
    assert env.state.get(rows[0]["cache_key"]) == env.old
    assert rows[1]["operation_nonce"] == "5"
    assert env.view.nonce_reads == [30]


def test_adoption_wrong_deadline_rejects_before_persisting_attempt(recovery):
    env = recovery
    with pytest.raises(ValueError, match="BTC_MINT_ORIGINAL_DEADLINE_REQUIRED"):
        env.dispatch(2000)
    assert env.state.get(env.key + ":attempts-v1") is None
    assert not env.view.sends
    assert env.dispatch(1000)["status"] == "BTC_FUNDING_EXECUTED"
    assert env.state.get(env.key + ":attempts-v1")["deadline"] == "1000"


def test_creation_event_prevents_adopting_coordinator_extended_deadline(recovery):
    env = recovery
    env.view.sessions[8]["deadline"] = 2000
    with pytest.raises(ValueError, match="BTC_MINT_ORIGINAL_DEADLINE_REQUIRED"):
        env.dispatch(2000)
    assert env.state.get(env.key + ":attempts-v1") is None
    assert not env.view.sends


def test_legacy_extended_or_aborted_session_preserves_original_deadline_for_new_attempt(recovery):
    env = recovery
    env.state.put(env.key, env.old)
    env.view.sessions[7].update(deadline=2000, state=3)
    assert env.dispatch(1000)["status"] == "BTC_FUNDING_EXECUTED"
    assert env.state.get(env.key + ":attempts-v1")["deadline"] == "1000"
