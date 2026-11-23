import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from signer_agent.nonce_anchor import AnchorStore, NonceAnchor, _NoRedirect
from signer_agent.secure_state import SecureState


PARTICIPANT = "1" * 64
TOKEN = "2" * 64
URL = "https://nonce-anchor.invalid/v1/nonce-anchor"


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.delenv("SATOSHIFI_NONCE_ANCHOR_CONFIG", raising=False)
    remote = tmp_path / "remote"
    journal, wallet = tmp_path / "ledger", tmp_path / "wallet"
    for directory in (remote, journal, wallet):
        directory.mkdir(mode=0o700)
    store = AnchorStore(remote / "anchor.sqlite")
    ledger = journal / "spent.sqlite"
    state_path = wallet / "wallet.enc"
    requests = []
    faults = SimpleNamespace(drop_claim=False)

    def client():
        anchor = NonceAnchor(URL, store.server_id, PARTICIPANT, TOKEN)

        def request(req, *, timeout):
            assert timeout == 10 and req.full_url == URL
            assert req.get_header("Authorization") == "Bearer " + TOKEN
            content = json.loads(req.data)
            requests.append(content)
            status = store.process(content)
            if faults.drop_claim and content["operation"] == "claim":
                raise OSError("simulated response loss after database commit")
            return io.BytesIO(json.dumps(dict(version="1", server_id=store.server_id, status=status)).encode())

        anchor._opener = SimpleNamespace(open=request)
        return anchor

    anchor = client()
    state = SecureState(str(state_path), "test-only", nonce_ledger=str(ledger), nonce_anchor=anchor)
    state.initialize_nonce_ledger()
    return SimpleNamespace(anchor=anchor, client=client, state=state, state_path=state_path, ledger=ledger,
                           store=store, requests=requests, faults=faults)


def test_offsite_claim_stops_a_simultaneous_rollback_of_both_local_volumes(guard):
    g = guard
    key, transcript = "private local session", "selected participants transcript"
    g.state.put(key, dict(status="nonce-created", message_hash="11" * 32, hiding="never leaves local host"))
    original_state = g.state_path.read_bytes()
    original_ledger = g.ledger.read_bytes()
    g.state.assert_unspent(key)
    g.state.prepare_share(key, transcript, 7)
    g.state.assert_spent(key, transcript)
    with pytest.raises(RuntimeError, match="NONCE_SPEND_TRANSCRIPT_MISMATCH"):
        g.state.assert_spent(key, "different transcript")
    assert key not in repr(g.requests) and transcript not in repr(g.requests) and TOKEN not in repr(g.requests)
    assert g.state._load()["nonce_anchor"] == g.anchor.binding
    assert g.state._load()["version"] == 2

    g.state_path.write_bytes(original_state)
    g.ledger.write_bytes(original_ledger)
    restored = SecureState(str(g.state_path), "test-only", nonce_anchor=g.client())
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        restored.assert_unspent(key)
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        restored.prepare_share(key, "different transcript", 11)
    assert restored.get(key)["status"] == "nonce-created"


def test_only_one_clone_can_claim_and_lookup_permits_only_the_cached_transcript(guard):
    identity = guard.state._load()["journal_id"]
    # The local locks may belong to different computers. The remote transaction is shared.
    def candidate(value):
        try:
            guard.client().claim(identity, "same session", value)
        except RuntimeError as exc:
            assert str(exc) == "NONCE_ALREADY_SPENT"
            return None
        return value

    with ThreadPoolExecutor(max_workers=2) as workers:
        winners = [value for value in workers.map(candidate, ("first", "second")) if value is not None]
    assert len(winners) == 1
    assert candidate(winners[0]) is None
    guard.client().assert_spent(identity, "same session", winners[0])
    with pytest.raises(RuntimeError, match="NONCE_SPEND_TRANSCRIPT_MISMATCH"):
        guard.client().assert_spent(identity, "same session", "other")


def test_dropped_claim_is_not_retried_and_cannot_egress_a_share(guard):
    g = guard
    g.state.put("s", dict(status="nonce-created", message_hash="x"))
    g.faults.drop_claim = True
    with pytest.raises(RuntimeError, match="NONCE_ANCHOR_UNAVAILABLE_RECOVERY_REQUIRED"):
        g.state.prepare_share("s", "intent", 7)
    assert len([r for r in g.requests if r["operation"] == "claim"]) == 1
    assert g.state.get("s")["status"] == "nonce-created"
    with pytest.raises(RuntimeError, match="NONCE_ALREADY_SPENT"):
        g.state.assert_unspent("s")
    with pytest.raises(RuntimeError, match="NONCE_SPEND_PROOF_MISSING"):
        g.state.assert_spent("s", "intent")


def test_unavailable_anchor_does_not_create_a_local_ledger(guard, monkeypatch):
    ledger = guard.ledger.with_name("not-bound.sqlite")
    state = SecureState(str(guard.state_path.with_name("not-bound.enc")), "test-only",
                        nonce_ledger=str(ledger), nonce_anchor=guard.anchor)

    def unavailable(_ledger):
        raise RuntimeError("NONCE_ANCHOR_UNAVAILABLE_RECOVERY_REQUIRED")

    monkeypatch.setattr(guard.anchor, "bind", unavailable)
    with pytest.raises(RuntimeError, match="NONCE_ANCHOR_UNAVAILABLE"):
        state.initialize_nonce_ledger()
    assert not ledger.exists() and not state.path.exists()


def test_anchor_cannot_be_omitted_reassigned_or_enabled_on_existing_state(guard):
    plain_view = SecureState(str(guard.state_path), "test-only")
    with pytest.raises(RuntimeError, match="NONCE_ANCHOR_CONFIG_REQUIRED"):
        plain_view.assert_unspent("s")
    changed = NonceAnchor(URL, "6" * 64, PARTICIPANT, TOKEN)
    with pytest.raises(RuntimeError, match="NONCE_ANCHOR_BINDING_MISMATCH"):
        SecureState(str(guard.state_path), "test-only", nonce_anchor=changed).require_nonce_ledger()
    other_ledger = guard.ledger.with_name("ordinary.sqlite")
    other_path = guard.state_path.with_name("ordinary.enc")
    SecureState(str(other_path), "test-only", nonce_ledger=str(other_ledger)).initialize_nonce_ledger()
    with pytest.raises(RuntimeError, match="NONCE_ANCHOR_FRESH_BINDING_REQUIRED"):
        SecureState(str(other_path), "test-only", nonce_ledger=str(other_ledger),
                    nonce_anchor=guard.anchor).initialize_nonce_ledger()

    changed_format = guard.state._load()
    changed_format.pop("nonce_anchor")
    guard.state._save(changed_format)
    with pytest.raises(RuntimeError, match="unsupported signer state format"):
        plain_view.assert_unspent("s")


def test_evm_companion_pins_same_remote_binding(guard):
    ledger = str(guard.ledger.with_name("evm.sqlite"))
    role = SecureState(str(guard.state_path.with_name("role.enc")), "test-only", nonce_ledger=ledger, nonce_anchor=guard.anchor)
    signing = SecureState(str(role.path) + ".signing", "test-only", nonce_anchor=guard.anchor)
    role.initialize_nonce_ledger(evm_signing_state=signing)
    assert role.evm_nonce_ledger(signing) == ledger
    signing.put("evm", dict(status="nonce-created", message_hash="x"))
    signing.prepare_share("evm", "intent", 3)
    signing.assert_spent("evm", "intent")
    assert role._load()["nonce_anchor"] == signing._load()["nonce_anchor"]


@pytest.mark.parametrize("url", ["http://anchor.invalid/v1/nonce-anchor", "https://token@anchor.invalid/v1/nonce-anchor",
                                  URL + "?key=secret", URL + "#fragment", URL.replace("/v1", "/other")])
def test_client_only_accepts_exact_https_endpoint(url):
    with pytest.raises(ValueError, match="NONCE_ANCHOR_CONFIG"):
        NonceAnchor(url, "a" * 64, PARTICIPANT, TOKEN)
    with pytest.raises(RuntimeError, match="NONCE_ANCHOR_UNAVAILABLE_RECOVERY_REQUIRED"):
        _NoRedirect().redirect_request(None, None, 307, None, {}, "https://other.invalid")


def test_loopback_https_authenticates_namespaces_and_commits_the_claim(guard, tmp_path):
    from datetime import datetime, timedelta, timezone
    from http.server import ThreadingHTTPServer
    from ipaddress import ip_address
    import ssl
    from threading import Thread
    from urllib.request import HTTPSHandler, ProxyHandler, build_opener
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from signer_agent.nonce_anchor import PATH, handler

    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local test nonce anchor")])
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(minutes=30))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), False)
                   .sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.touch(mode=0o600)
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile, keyfile)
    client_context = ssl.create_default_context(cafile=str(certfile))
    token_hash = hashlib.sha256(TOKEN.encode()).hexdigest()
    principals = {PARTICIPANT: token_hash, "5" * 64: hashlib.sha256(("7" * 64).encode()).hexdigest()}
    with ThreadingHTTPServer(("127.0.0.1", 0), handler(guard.store, principals)) as server:
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def client(participant, token):
                anchor = NonceAnchor(f"https://127.0.0.1:{server.server_port}{PATH}", guard.store.server_id, participant, token)
                anchor._opener = build_opener(ProxyHandler({}), HTTPSHandler(context=client_context), _NoRedirect())
                return anchor

            identity = guard.state._load()["journal_id"]
            origin = client(PARTICIPANT, TOKEN)
            origin.check(identity)
            origin.claim(identity, "loopback session", "transcript")
            origin.assert_spent(identity, "loopback session", "transcript")
            for participant, token in (("5" * 64, TOKEN), (PARTICIPANT, "1" * 64), ("5" * 64, "7" * 64)):
                with pytest.raises(RuntimeError, match="NONCE_ANCHOR_UNAVAILABLE"):
                    client(participant, token).check(identity)
        finally:
            server.shutdown()
            thread.join(3)
