"""Bounded transport grouping must preserve durable operation identity across retries."""

from copy import deepcopy
from types import SimpleNamespace
from dataclasses import replace

import pytest
from eth_account import Account
from web3.datastructures import AttributeDict

from signer_agent import onchain_transport as transport
from signer_agent.dkg_protocol import Manifest
from signer_agent.dkg_transport import Mailbox, sign
from signer_agent.operations import canonical
from signer_agent.dkg import Registry


@pytest.fixture
def batching(monkeypatch):
    accounts = [Account.from_key(i.to_bytes(32, "big")) for i in (1, 2, 3, 4)]
    manifest = Manifest(31337, "0x" + "11" * 20, bytes.fromhex("22" * 32), 1,
                        "EVM_AUTH", tuple(a.address for a in accounts),
                        tuple(i.to_bytes(32, "big") for i in range(1, 5)), 2)
    values, reads, snapshots, calls = {}, [], [], []
    block = dict(number=12, hash=b"a" * 32)

    def packet(session, kind, sender, recipient):
        def call(block_identifier):
            reads.append((session, kind, sender, recipient, block_identifier))
            return values.get((kind, sender, recipient), b"")
        return SimpleNamespace(call=call)

    box = object.__new__(transport.OnchainMailbox)
    box.board = SimpleNamespace(address="0x" + "33" * 20, functions=SimpleNamespace(packet=packet))
    box.registry = SimpleNamespace(account=accounts[0], w3=SimpleNamespace(
        eth=SimpleNamespace(get_block=lambda n: dict(block))))
    box.identity, box.state = "identity.json", "journal.sqlite"

    def snapshot(value):
        assert value is manifest
        snapshots.append(dict(block))
        return dict(block)

    box._checked_snapshot = snapshot
    monkeypatch.setattr(transport, "transact", lambda *args: calls.append(deepcopy(args[4:])))
    return SimpleNamespace(box=box, accounts=accounts, manifest=manifest,
                           values=values, reads=reads, snapshots=snapshots, calls=calls, block=block)


def initial(f):
    return [("round1", f.accounts[0].address, "", dict(commitments=["02aa"], package="aabb")),
            ("key", f.accounts[0].address, "", dict(signature="1234"))]


def envelopes(f, size=8):
    return [("envelope", f.accounts[0].address, a.address,
             dict(envelope=str(i) * size, hash=str(i) * 64, signature="aa" * 65))
            for i, a in enumerate(f.accounts[1:], 1)]


def test_batch_uses_one_authenticated_snapshot_and_deterministic_order(batching):
    f = batching
    f.box.put_many(f.manifest, initial(f))
    assert len(f.snapshots) == 1 and len(f.calls) == 1
    assert all(row[-1] == 12 for row in f.reads)
    method, args = f.calls[0]
    assert method == "publishBatch" and args[0] == "0x" + f.manifest.session_id.hex()
    assert args[1] == ["0", "1"] and args[2] == [transport.ZERO, transport.ZERO]
    assert args[3] == ["0x" + canonical(x[3]).encode().hex() for x in reversed(initial(f))]
    f.box.put_many(f.manifest, list(reversed(initial(f))))
    assert f.calls[1] == f.calls[0]


def test_partially_published_batch_keeps_original_whole_operation(batching):
    f = batching
    messages = initial(f)
    f.box.put_many(f.manifest, messages)
    f.values[(0, f.accounts[0].address, transport.ZERO)] = canonical(messages[1][3]).encode()
    f.box.put_many(f.manifest, list(reversed(messages)))
    assert f.calls[0] == f.calls[1]
    f.values[(1, f.accounts[0].address, transport.ZERO)] = canonical(messages[0][3]).encode()
    f.box.put_many(f.manifest, messages)
    assert len(f.calls) == 2  # both finalized, no new transaction or nonce


def test_unconfirmed_batch_retry_uses_identical_durable_arguments(batching, monkeypatch):
    f = batching

    def send(*args):
        f.calls.append(deepcopy(args[4:]))
        raise ValueError("BOARD_TRANSACTION_UNCONFIRMED")

    monkeypatch.setattr(transport, "transact", send)
    for messages in (envelopes(f), list(reversed(envelopes(f)))):
        with pytest.raises(ValueError, match="BOARD_TRANSACTION_UNCONFIRMED"):
            f.box.put_many(f.manifest, messages)
    assert f.calls[0] == f.calls[1]


def test_byte_chunks_are_bounded_and_independent_of_existing_entries(batching):
    f = batching
    messages = envelopes(f, 8000)
    f.box.put_many(f.manifest, messages)
    assert len(f.calls) == 3
    original = deepcopy(f.calls)
    assert all(sum(len(bytes.fromhex(p[2:])) for p in args[3]) <= 16384 for _, args in f.calls)
    f.values[(2, f.accounts[0].address, messages[1][2])] = canonical(messages[1][3]).encode()
    f.calls.clear()
    f.box.put_many(f.manifest, list(reversed(messages)))
    assert f.calls == [original[0], original[2]]


def test_conflict_in_later_chunk_prevents_any_new_publication(batching):
    f = batching
    messages = envelopes(f, 8000)
    f.values[(2, f.accounts[0].address, messages[-1][2])] = b'{"different":"bytes"}'
    with pytest.raises(ValueError, match="CONFLICTING_RETRY"):
        f.box.put_many(f.manifest, messages)
    assert not f.calls


def test_reorg_between_snapshot_and_publication_fails_closed(batching):
    f = batching
    f.box.registry.w3.eth.get_block = lambda n: dict(number=n, hash=b"b" * 32)
    with pytest.raises(ValueError, match="ONCHAIN_ORIGIN_REORG"):
        f.box.put_many(f.manifest, initial(f))
    assert not f.calls


@pytest.mark.parametrize("change", ["different_sender", "self", "outsider", "duplicate", "private_field", "oversize", "empty", "too_many", "unknown_kind", "misdirected_public"])
def test_batch_rejects_unauthenticated_ambiguous_or_private_packets(batching, change):
    f = batching
    messages = envelopes(f)
    kind, sender, recipient, body = messages[0]
    if change == "different_sender": sender = f.accounts[1].address
    if change == "self": recipient = sender
    if change == "outsider": recipient = "0x" + "99" * 20
    if change == "private_field": body["secret"] = "do not publish"
    if change == "oversize": body["envelope"] = "a" * 16384
    if change == "unknown_kind": kind = "secret"
    if change == "misdirected_public": kind, body = "key", {"signature": "aa"}
    messages[0] = (kind, sender, recipient, body)
    if change == "duplicate": messages.append(messages[0])
    if change == "empty": messages = []
    if change == "too_many": messages *= 6
    with pytest.raises(ValueError):
        f.box.put_many(f.manifest, messages)
    assert not f.calls and not f.snapshots


def test_local_batch_conflict_rolls_back_and_real_signature_is_authenticated(batching, tmp_path):
    f = batching
    box = Mailbox(tmp_path / "mail.sqlite")
    body = dict(signature=sign(f.manifest, "DKGTransportKey", f.accounts[0].key,
                              participant=f.accounts[0].address,
                              transportPublicKey=f.manifest.transport_keys[0]))
    box.put(f.manifest, "round1", f.accounts[0].address, "", dict(package="original"))
    messages = [("key", f.accounts[0].address, "", body),
                ("round1", f.accounts[0].address, "", dict(package="replacement"))]
    with pytest.raises(ValueError, match="CONFLICTING_RETRY"):
        box.put_many(f.manifest, messages)
    assert box.get(f.manifest, "key", f.accounts[0].address) is None
    messages[1] = ("round1", f.accounts[0].address, "", dict(package="original"))
    box.put_many(f.manifest, messages)
    box.put_many(f.manifest, messages)
    assert box.get(f.manifest, "key", f.accounts[0].address) == body
    body["signature"] = sign(f.manifest, "DKGTransportKey", f.accounts[1].key,
                              participant=f.accounts[0].address,
                              transportPublicKey=f.manifest.transport_keys[0])
    with pytest.raises(ValueError, match="SENDER_MISMATCH"):
        box.put_many(f.manifest, messages)


def depth_chain(chain, head=100, finalized=80):
    reads = []
    def get_block(key):
        reads.append(key)
        number = head if key == "latest" else finalized if key == "finalized" else int(key)
        return AttributeDict(dict(number=number, hash=number.to_bytes(32, "big"), timestamp=1000 + number))
    return SimpleNamespace(eth=SimpleNamespace(chain_id=chain, get_block=get_block)), reads


@pytest.mark.parametrize("chain,expected", [(1, 80), (31337, 89), (11155111, 89)])
def test_registry_enrolled_depth_applies_to_each_evm_network(chain, expected):
    w3, reads = depth_chain(chain)
    client = Registry.__new__(Registry)
    client.w3 = w3
    client.bitcoin_graph = dict(evm_confirmations="12")
    client.contract = SimpleNamespace(functions=SimpleNamespace(
        round1Hashes=lambda *args: SimpleNamespace(call=lambda **kwargs: kwargs)))
    assert client.read("round1Hashes", b"session", "sender") == dict(block_identifier=expected)
    assert ("finalized" in reads) == (chain == 1)
    client.read_block_identifier = 70
    assert client.read("round1Hashes", b"session", "sender") == dict(block_identifier=70)


@pytest.mark.parametrize("chain,expected", [(1, 80), (31337, 89), (11155111, 89)])
def test_v6_mailbox_uses_enrolled_depth_on_each_network(batching, monkeypatch, chain, expected):
    f = batching
    w3, _ = depth_chain(chain)
    m = replace(f.manifest, chain_id=chain)
    f.box.registry.w3 = w3
    f.box.registry.contract = SimpleNamespace(address=m.registry)
    f.box.board.functions.registry = lambda: SimpleNamespace(call=lambda **kwargs: m.registry)
    invite = dict(version="6", chain_id=str(chain), evm_confirmations="12",
                  sessions=dict(evm_ceremony=m.session_id.hex()), participants=list(m.participants),
                  threshold=str(m.threshold), epoch=str(m.epoch))
    monkeypatch.setattr(transport, "_board_and_invitation", lambda *args: (f.box.board, invite))
    origins = []
    monkeypatch.setattr(transport, "registry_at", lambda w3, address, block: origins.append(block.number))
    del f.box._checked_snapshot
    assert f.box.get(m, "key", m.participants[0]) is None
    assert origins == [expected] and f.reads[-1][-1] == expected


@pytest.mark.parametrize("chain", [31337, 11155111])
def test_transport_default_nonmainnet_is_one_block_but_explicit_depth_is_enforced(chain):
    w3, _ = depth_chain(chain)
    assert transport._transport_block(w3).number == 100
    assert transport._transport_block(w3, 12).number == 89
    with pytest.raises(ValueError, match="EVM_CONFIRMATIONS_PENDING"):
        transport._transport_block(w3, 256)
