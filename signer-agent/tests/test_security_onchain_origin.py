"""First-DKG entry points must authenticate constructors before trusting storage."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from web3 import Web3
from web3.datastructures import AttributeDict

from signer_agent import contract_provenance, onchain_transport
from signer_agent.bootstrap import artifact
from signer_agent.dkg_protocol import Manifest, PROTOCOL_ID
from signer_agent.onboarding import validate_bundle
from signer_agent.operations import canonical


def address(number):
    return Web3.to_checksum_address("0x" + format(number, "040x"))


@pytest.fixture
def chain(monkeypatch):
    members = [address(i) for i in (1, 2, 3)]
    board_address, registry_address = address(101), address(102)
    invite = dict(
        version="2", chain_id="31337", bitcoin_network="regtest",
        mode="integration", deployment_id="friends", operator=members[0],
        treasury=members[2], approval_deadline="2000000000",
        federation_id="friends-fed", confederation_id="friends-conf",
        participants=members, threshold="2", epoch="1",
        sessions={role: bytes([i]).hex() * 32 for i, role in enumerate(
            ("evm_ceremony", "conf_ceremony", "btc_ceremony"), 1)},
        protocol_id=PROTOCOL_ID.hex(), phase_duration="3600",
    )
    raw = canonical(invite).encode()
    current = dict(number=20, hash=bytes.fromhex("ab" * 32))
    calls, proofs = [], []
    state = dict(invitation=raw, invitationHash=Web3.keccak(raw),
                 operator=members[0], registry=registry_address, packet=b"")

    class Functions:
        def __getattr__(self, method):
            def invoke(*args):
                def call(block_identifier=None):
                    calls.append((method, args, block_identifier))
                    if method == "invited":
                        return args[0] in members
                    return state[method]
                return SimpleNamespace(call=call)
            return invoke

    board = SimpleNamespace(address=board_address, functions=Functions())
    registry_contract = SimpleNamespace(address=registry_address)
    contracts = {board_address: board, registry_address: registry_contract}
    kinds = {board_address: "DKGOnchainBoard", registry_address: "DKGCoordinator"}
    eth = SimpleNamespace(
        chain_id=31337,
        get_block=lambda _: AttributeDict(current),
        get_code=lambda addr, block_identifier=None: bytes.fromhex(
            artifact(kinds[addr])["deployedBytecode"]["object"].removeprefix("0x")),
        contract=lambda address, abi: contracts[address],
    )
    w3 = SimpleNamespace(eth=eth)

    def prove(w3_arg, addr, kind, args, latest):
        proofs.append((w3_arg, addr, kind, deepcopy(args), latest))
        return dict(sender=members[0])

    monkeypatch.setattr(contract_provenance, "prove_creation", prove)
    registry = SimpleNamespace(w3=w3, contract=registry_contract,
                               account=SimpleNamespace(address=members[0]))
    manifest = Manifest(31337, registry_address, bytes.fromhex(invite["sessions"]["evm_ceremony"]),
                        1, "EVM_AUTH", tuple(members),
                        tuple(i.to_bytes(32, "big") for i in (1, 2, 3)), 2)
    return SimpleNamespace(w3=w3, board=board, registry=registry, invite=invite,
                           raw=raw, calls=calls, proofs=proofs, state=state,
                           current=current, manifest=manifest)


def test_board_origin_binds_exact_invitation_roster_and_operator(chain):
    board, invite = onchain_transport.invitation(chain.w3, chain.board.address)
    assert board is chain.board and invite == chain.invite
    assert chain.proofs == [(chain.w3, chain.board.address, "DKGOnchainBoard",
                             [chain.raw, chain.invite["participants"]], 20)]
    assert all(block == 20 for _, _, block in chain.calls)


def test_board_creation_locator_still_requires_exact_constructor_and_sender(chain, monkeypatch):
    calls = []
    def prove(w3, address, kind, args, latest, **kwargs):
        calls.append((kind, args, latest, kwargs))
        return dict(sender=chain.invite["operator"])
    monkeypatch.setattr(contract_provenance, "prove_creation", prove)
    onchain_transport.invitation(chain.w3, chain.board.address, birth_hint=5)
    assert calls == [("DKGOnchainBoard", [chain.raw, chain.invite["participants"]], 20, {"birth_hint": 5})]
    monkeypatch.setattr(contract_provenance, "prove_creation", lambda *a, **k: dict(sender=address(99)))
    with pytest.raises(ValueError, match="BOARD_CREATOR_MISMATCH"):
        onchain_transport.invitation(chain.w3, chain.board.address, birth_hint=5)


@pytest.mark.parametrize("payload", [b"{broken", b"\xff", b"[" * 2000])
def test_invalid_invitation_bytes_are_normalized_without_hiding_rpc_errors(chain, payload):
    chain.state["invitation"] = payload
    with pytest.raises(ValueError, match="BOARD_INVITATION_SCHEMA"):
        onchain_transport.invitation(chain.w3, chain.board.address)


@pytest.mark.parametrize("target", ["board", "registry"])
def test_matching_runtime_and_getters_cannot_replace_origin(chain, monkeypatch, target):
    def reject(*args):
        raise ValueError("BTC_CONSTRUCTOR_INPUT_MISMATCH")
    monkeypatch.setattr(contract_provenance, "prove_creation", reject)
    with pytest.raises(ValueError, match="BTC_CONSTRUCTOR_INPUT_MISMATCH"):
        if target == "board":
            onchain_transport.invitation(chain.w3, chain.board.address)
        else:
            onchain_transport.registry_at(chain.w3, chain.registry.contract.address)


def test_board_constructor_sender_must_be_invited_operator(chain, monkeypatch):
    monkeypatch.setattr(contract_provenance, "prove_creation",
                        lambda *args: dict(sender=chain.invite["participants"][1]))
    with pytest.raises(ValueError, match="BOARD_CREATOR_MISMATCH"):
        onchain_transport.board_at(chain.w3, chain.board.address)


def test_board_invitation_hash_is_checked_before_origin(chain):
    chain.state["invitationHash"] = bytes(32)
    with pytest.raises(ValueError, match="BOARD_INVITATION_MISMATCH"):
        onchain_transport.board_at(chain.w3, chain.board.address)
    assert not chain.proofs


@pytest.mark.parametrize("payload", [[], None, 7, "invitation"])
def test_nonobject_board_invitation_is_discovery_skippable(chain, payload):
    chain.state["invitation"] = canonical(payload).encode()
    with pytest.raises(ValueError, match="BOARD_INVITATION_SCHEMA"):
        onchain_transport.invitation(chain.w3, chain.board.address)
    assert not chain.proofs


@pytest.mark.parametrize("changes", [
    dict(participants=[]), dict(participants=None), dict(threshold={}),
    dict(sessions={role: {} for role in
                   ("evm_ceremony", "conf_ceremony", "btc_ceremony")}),
])
def test_malformed_nested_board_invitation_is_discovery_skippable(chain, changes):
    chain.state["invitation"] = canonical(dict(chain.invite, **changes)).encode()
    with pytest.raises(ValueError, match="BOARD_INVITATION_SCHEMA"):
        onchain_transport.invitation(chain.w3, chain.board.address)
    assert not chain.proofs


def test_registry_requires_canonical_no_argument_constructor(chain):
    assert onchain_transport.registry_at(chain.w3, chain.registry.contract.address) is chain.registry.contract
    assert chain.proofs == [(chain.w3, chain.registry.contract.address, "DKGCoordinator", [], 20)]


@pytest.mark.parametrize("creation,ready", [(12, True), (13, False)])
def test_invitation_creation_depth_is_applied_once_and_pending_creation_is_rejected(chain, monkeypatch, creation, ready):
    from signer_agent import onboarding
    # Isolate snapshot policy here; complete V6 schema validation has its own tests.
    invite = dict(chain.invite, version="6", evm_confirmations="12")
    raw = canonical(invite).encode()
    chain.state.update(invitation=raw, invitationHash=Web3.keccak(raw))
    monkeypatch.setattr(onboarding, "validate_invite", lambda value: value)
    chain.w3.eth.get_block = lambda key: AttributeDict(dict(
        number=23 if key == "latest" else int(key), hash=b"a" * 32))
    monkeypatch.setattr(contract_provenance, "prove_creation", lambda *args: dict(
        sender=invite["operator"], block_number=creation))
    if ready:
        assert onchain_transport.invitation(chain.w3, chain.board.address)[1] == invite
        assert any(block == 12 for _, _, block in chain.calls)
    else:
        with pytest.raises(ValueError, match="EVM_CONFIRMATIONS_PENDING"):
            onchain_transport.invitation(chain.w3, chain.board.address)


def test_reorg_during_origin_read_is_rejected(chain, monkeypatch):
    original = contract_provenance.prove_creation

    def reorg(*args):
        result = original(*args)
        chain.current["hash"] = bytes.fromhex("cd" * 32)
        return result

    monkeypatch.setattr(contract_provenance, "prove_creation", reorg)
    with pytest.raises(ValueError, match="ONCHAIN_ORIGIN_REORG"):
        onchain_transport.board_at(chain.w3, chain.board.address)


def test_packet_reads_recheck_origin_and_use_one_block_snapshot(chain, monkeypatch):
    mailbox = onchain_transport.OnchainMailbox(chain.registry, chain.board.address, None, None)
    chain.proofs.clear()
    chain.calls.clear()
    assert mailbox.get(chain.manifest, "key", chain.invite["participants"][0]) is None
    assert [p[2] for p in chain.proofs] == ["DKGOnchainBoard", "DKGCoordinator"]
    assert all(block == 20 for _, _, block in chain.calls)
    chain.calls.clear()

    def reorg(*args):
        raise ValueError("BTC_CREATION_ANCHOR_REORG")
    monkeypatch.setattr(contract_provenance, "prove_creation", reorg)
    with pytest.raises(ValueError, match="BTC_CREATION_ANCHOR_REORG"):
        mailbox.get(chain.manifest, "key", chain.invite["participants"][0])
    assert not any(name == "packet" for name, _, _ in chain.calls)


@pytest.mark.parametrize("change", [
    dict(session_id=bytes.fromhex("99" * 32)),
    dict(purpose="BTC_SPEND"), dict(epoch=2), dict(threshold=3),
    dict(participants=tuple(address(i) for i in (1, 2, 4))),
])
def test_mailbox_rejects_manifest_outside_invitation(chain, change):
    mailbox = onchain_transport.OnchainMailbox(chain.registry, chain.board.address, None, None)
    with pytest.raises(ValueError, match="BOARD_MANIFEST_MISMATCH"):
        mailbox.get(replace(chain.manifest, **change), "key", chain.invite["participants"][0])
    assert not any(name == "packet" for name, _, _ in chain.calls)


def test_unenrolled_legacy_bundle_still_authenticates_registry(chain, monkeypatch):
    validate_bundle(chain.manifest.to_dict(), {}, chain.registry)
    assert chain.proofs == [(chain.w3, chain.registry.contract.address, "DKGCoordinator", [], 20)]

    def reject(*args):
        raise ValueError("BTC_CANONICAL_CREATION_NOT_FOUND")
    monkeypatch.setattr(contract_provenance, "prove_creation", reject)
    with pytest.raises(ValueError, match="BTC_CANONICAL_CREATION_NOT_FOUND"):
        validate_bundle(chain.manifest.to_dict(), {}, chain.registry)
