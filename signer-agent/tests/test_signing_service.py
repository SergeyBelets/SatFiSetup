import json
import time
from dataclasses import replace
from types import SimpleNamespace

from eth_utils import to_checksum_address
import pytest

from signer_agent.config import Config
from signer_agent.coordinator import CoordinatorClient, Session
from web3.exceptions import ContractLogicError
from signer_agent.frost_math import (
    SECP256K1_ORDER,
    Point,
    aggregate_nonce,
    build_transcript,
    negate_point,
    scalar_base_mult,
)
from signer_agent.secure_state import SecureState
from signer_agent.service import SigningService


class FakeClient:
    chain_id = 31337
    address = to_checksum_address("0x0000000000000000000000000000000000003001")

    def __init__(self, sender, participants, public_key_shares, shared):
        self.sender = sender
        self._participants = participants
        self._public_key_shares = public_key_shares
        self.shared = shared
        # Pure signing-math fixture: this target has no deployed contract code.
        self.w3 = SimpleNamespace(eth=SimpleNamespace(
            get_block=lambda _:SimpleNamespace(number=1), get_code=lambda *a,**k:b''))

    def participants(self, session_id):
        return list(self._participants)

    def public_key_share(self, session_id, address=None):
        return self._public_key_shares[to_checksum_address(address or self.sender)], True

    def all_nonce_commitments(self, session_id, participants):
        return dict(self.shared["commitments"])

    def has_committed(self, session_id, address=None):
        return to_checksum_address(address or self.sender) in self.shared["commitments"]

    def has_submitted_share(self, session_id, address=None):
        return to_checksum_address(address or self.sender) in self.shared["shares"]

    def has_refused(self, session_id, address=None):
        return to_checksum_address(address or self.sender) in self.shared["refused"]

    def submit_nonce_commitments(self, session_id, hiding, binding):
        self.shared["commitments"][self.sender] = (hiding, binding)
        return "0xcommit"

    def nonce_set_status(self, session_id):
        if "locked_aggregate" not in self.shared:
            return False, bytes(32), Point(0, 0), 0
        return (
            True,
            self.shared["transcript"],
            self.shared["locked_aggregate"],
            len(self.shared["shares"]),
        )

    def submit_signature_share(self, session_id, share):
        self.shared["shares"][self.sender] = share
        return "0xshare"

    def verified_share(self, session_id, address):
        return self.shared["shares"].get(to_checksum_address(address))

    def finalize(self, session_id, signature, message_hash):
        self.shared["finalized"] = (signature, message_hash, self.sender)
        return "0xfinalize"

    def reject(self, session_id, reason):
        self.shared["refused"].add(self.sender)
        return "0xreject"


def _config(tmp_path, sender, signing_share, index):
    abi = tmp_path / "abi.json"
    abi.write_text(json.dumps([]))
    return Config(
        rpc_url="http://unused",
        sender=sender,
        private_key="unused",
        coordinator_address=FakeClient.address,
        coordinator_abi_path=str(abi),
        signing_share_hex="%064x" % signing_share,
        state_key=("%064x" % index),
        state_path=str(tmp_path / ("state-%s.enc" % index)),
        deployment_block=0,
        scan_chunk_size=100,
        confirmations=1,
        poll_interval=1,
        receipt_timeout=1,
        gas_limit=0,
        max_fee_gwei=None,
        priority_fee_gwei=1,
        auto_sign=False,
        auto_finalize=False,
    )


@pytest.mark.parametrize("pinned_finalization", [False, True])
@pytest.mark.parametrize("guarded", [False, True])
def test_two_round_approval_can_resume_across_runs(tmp_path, pinned_finalization, guarded):
    if guarded:
        external = tmp_path / "external"
        external.mkdir(mode=0o700)
        tmp_path = tmp_path / "local"
        tmp_path.mkdir(mode=0o700)
    group_secret = 23
    while scalar_base_mult(group_secret).y & 1:
        group_secret += 1
    slope = 17
    signing_shares = [
        (group_secret + slope) % SECP256K1_ORDER,
        (group_secret + 2 * slope) % SECP256K1_ORDER,
    ]
    participants = [
        to_checksum_address("0x0000000000000000000000000000000000001001"),
        to_checksum_address("0x0000000000000000000000000000000000001002"),
    ]
    public_key_shares = {
        participant: scalar_base_mult(share)
        for participant, share in zip(participants, signing_shares)
    }
    shared = {"commitments": {}, "shares": {}, "refused": set()}
    session = Session(
        session_id=9,
        creator=FakeClient.address,
        group_pubkey=scalar_base_mult(group_secret).x.to_bytes(32, "big"),
        message_hash=bytes.fromhex("44" * 32),
        message_bound=True,
        threshold=2,
        total=2,
        deadline=int(time.time()) + 3600,
        enforce_shares_check=True,
        verifier_override="0x0000000000000000000000000000000000000000",
        state=1,
        commits_count=0,
        shares_count=0,
        refusal_count=0,
        purpose=1,
        origin_contract=FakeClient.address,
        origin_id=12,
        network_id=0,
        pool_id=bytes(32),
        dkg_shares_count=0,
    )

    services = []
    for index, (participant, share) in enumerate(zip(participants, signing_shares), start=1):
        config = replace(_config(tmp_path, participant, share, index), auto_finalize=pinned_finalization)
        if guarded:
            config = replace(config, nonce_ledger=str(external / f"{index}.sqlite"))
            SecureState(config.state_path, config.state_key, nonce_ledger=config.nonce_ledger).initialize_nonce_ledger()
        client = FakeClient(participant, participants, public_key_shares, shared)
        services.append(SigningService(config, client))

    assert services[0].approve(session).status == "committed"
    assert services[1].approve(session).status == "committed"

    services[0] = SigningService(
        services[0].config,
        FakeClient(participants[0], participants, public_key_shares, shared),
    )
    transcript = build_transcript(
        FakeClient.chain_id,
        FakeClient.address,
        session.session_id,
        session.group_pubkey,
        session.message_hash,
        participants,
        shared["commitments"],
    )
    aggregate = aggregate_nonce(transcript, session.session_id, participants, shared["commitments"])
    shared["transcript"] = transcript
    shared["locked_aggregate"] = negate_point(aggregate) if aggregate.y & 1 else aggregate

    for service in services:
        if pinned_finalization:
            # Match CoordinatorClient's immutable per-command confirmed view:
            # post-submit finalization cannot see the share it just sent.
            shares_at_start = dict(shared["shares"])
            service.client.verified_share = lambda session_id, address, snapshot=shares_at_start: snapshot.get(to_checksum_address(address))
        assert service.approve(session).status == "signed"
    assert len(shared["shares"]) == 2
    assert "finalized" not in shared
    if pinned_finalization:
        commitments, shares = dict(shared["commitments"]), dict(shared["shares"])
        services[0].client.verified_share = lambda session_id, address: shares.get(to_checksum_address(address))
        finalized = services[0].approve(session)  # Third sweep: no new nonce/share.
        assert shared["commitments"] == commitments and shared["shares"] == shares
    else:
        finalized = services[0].try_finalize(session)
    assert finalized is not None
    assert finalized.status == "finalized"
    assert shared["finalized"][1] == session.message_hash
    assert shared["finalized"][2] == participants[0]

    for index, service in enumerate(services, start=1):
        key = SecureState.session_key(FakeClient.chain_id, FakeClient.address, session.session_id)
        assert service.state.get(key)["status"] == "consumed"


@pytest.mark.parametrize("chain,network,reviewed_session,code", [
    (11155111, "testnet4", None, "PUBLIC_EVM_TYPED_SIGNING_REQUIRED"),
    (1, "mainnet", 10, "PUBLIC_EVM_TYPED_SIGNING_REQUIRED"),
    (1, "testnet4", 9, "NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED"),
    (11155111, "mainnet", 9, "NONCE_LEDGER_NOT_BOUND_RECOVERY_REQUIRED"),
])
def test_signing_service_guards_session_binding_and_independent_journal(tmp_path, chain, network,
                                                                       reviewed_session, code):
    sender = to_checksum_address("0x0000000000000000000000000000000000001001")
    config = _config(tmp_path, sender, 23, 1)
    client = SimpleNamespace(chain_id=chain, bitcoin_graph={"bitcoin_network": network})
    service = SigningService(config, client, reviewed_session_id=reviewed_session)
    with pytest.raises((RuntimeError, ValueError), match=code):
        service.approve(SimpleNamespace(session_id=9, state=1, purpose=1))
    assert not service.state.path.exists()
    assert not (tmp_path / "state-1.enc.spent.sqlite").exists()


@pytest.mark.parametrize("entrypoint", ["approve", "try_finalize"])
@pytest.mark.parametrize("session_state,status", [(0, "blocked"), (2, "finalized"),
    (3, "blocked"), (99, "blocked")])
def test_terminal_sessions_do_not_touch_nonces_or_send_again(tmp_path, monkeypatch, entrypoint, session_state, status):
    from signer_agent import service as signing

    class NoRPC:
        def __getattr__(self, name):
            raise AssertionError("terminal session must not access RPC: " + name)

    def forbidden_crypto(*args, **kwargs):
        raise AssertionError("terminal session must not create or sign a nonce")

    sender = to_checksum_address("0x0000000000000000000000000000000000001001")
    config = _config(tmp_path, sender, 23, 1)
    state = SecureState(config.state_path, config.state_key)
    stored = dict(status="nonce-created", message_hash="11" * 32, hiding="01", binding="02")
    state.put("existing-nonce", stored)
    service = SigningService(config, NoRPC(), state)
    monkeypatch.setattr(signing, "random_nonce_pair", forbidden_crypto)
    monkeypatch.setattr(signing.crypto, "call", forbidden_crypto)
    # Only these fields should be needed for a terminal response. Accessing
    # participants/commitments or private transcript state would fail the test.
    session = SimpleNamespace(session_id=9, state=session_state)
    result = getattr(service, entrypoint)(session)
    assert result.status == status
    assert result.tx_hash is None
    assert state.get("existing-nonce") == stored


@pytest.mark.parametrize("state_at_send", ["already_finalized", "finalized_during_preflight",
                     "finalized_after_send", "different_result", "still_open"])
def test_finalize_checks_current_session_before_sending(state_at_send):
    from signer_agent.transaction_wait import TransactionRecoveryRequired

    address = FakeClient.address
    message = b"m" * 32
    signature = b"s" * 64
    state = 2 if state_at_send in ("already_finalized", "different_result") else 1
    submitted = []

    def session():
        return (1, address, b"g" * 32, message, True, 2, 2, 9999,
                True, address, state, 2, 2, 0, 1, address, 0, 0, b"p" * 32, 2)

    def preflight(tx):
        nonlocal state
        assert tx == {"from": address}
        if state_at_send == "finalized_during_preflight":
            state = 2
            raise ContractLogicError("already finalized")

    call = SimpleNamespace(call=preflight)
    functions = SimpleNamespace(getSession=lambda _: SimpleNamespace(call=session),
        finalizeSession=lambda *_: call, getResult=lambda _: SimpleNamespace(
            call=lambda: (b"x" * 64 if state_at_send == "different_result" else signature, message)))

    def send(candidate):
        nonlocal state
        submitted.append(candidate)
        if state_at_send == "finalized_after_send":
            state = 2
            raise TransactionRecoveryRequired(dict(reason="TRANSACTION_REVERTED"))
        return "tx"

    client = SimpleNamespace(sender=address, contract=SimpleNamespace(functions=functions), _send=send)
    if state_at_send == "different_result":
        with pytest.raises(ValueError, match="FROST_FINALIZATION_RESULT_MISMATCH"):
            CoordinatorClient.finalize(client, 1, signature, message)
        assert not submitted
        return
    result = CoordinatorClient.finalize(client, 1, signature, message)
    assert result == ("tx" if state_at_send == "still_open" else None)
    assert submitted == ([call] if state_at_send in ("still_open", "finalized_after_send") else [])
