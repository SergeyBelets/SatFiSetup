"""Mainnet RPC routing and responses must never turn cookies into public output."""

import base64
from decimal import Decimal
from email.message import Message
import io
import json
import os
from types import SimpleNamespace
import urllib.request
import urllib.response

import pytest

from signer_agent import bitcoin_rpc
from signer_agent.bitcoin_rpc import BitcoinRPC


@pytest.fixture
def cookie(tmp_path):
    path = tmp_path / ".cookie"
    path.write_text("__cookie__:synthetic-credential\n")
    path.chmod(0o600)
    return path


def responding(rpc, response):
    requests = []

    def open_request(request, timeout):
        body = json.loads(request.data)
        requests.append((request, body, timeout))
        data = response(body) if callable(response) else response
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        return io.BytesIO(data)

    rpc._opener = SimpleNamespace(open=open_request)
    return requests


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8332", "http://[::1]:8332", "http://localhost:8332",
    "https://node.example:8332/wallet/watch-only",
])
def test_mainnet_accepts_private_loopback_or_verified_tls_endpoints(cookie, url):
    rpc = BitcoinRPC(url, cookie, "mainnet")
    assert rpc.network == "mainnet"
    assert rpc.policy is None  # read-only checkpoint discovery precedes policy creation


@pytest.mark.parametrize("url", [
    "http://node.example:8332", "http://192.168.1.2:8332", "http://8.8.8.8:8332",
    "http://[2001:db8::1]:8332", "http://localhost.evil:8332",
])
def test_mainnet_refuses_cleartext_remote_cookie_transport(cookie, url):
    with pytest.raises(ValueError, match="^BITCOIN_RPC_REMOTE_HTTP_FORBIDDEN$"):
        BitcoinRPC(url, cookie, "mainnet")


@pytest.mark.parametrize("url", [
    "https://username:synthetic-password@node.example/", "http://user@127.0.0.1/",
    "https://node.example/?token=synthetic-token", "https://node.example/#secret",
    "file:///tmp/private", "ftp://node.example/", "https:///missing-host",
    "https://node.example:999999/", "http://localhost:0", "http://localhost:\t8332",
    "https://node.example/\nsecret", "https://node.example/ secret",
])
def test_routing_cannot_embed_credentials_or_ambiguous_url_syntax(cookie, url):
    with pytest.raises(ValueError) as caught:
        BitcoinRPC(url, cookie, "mainnet")
    assert str(caught.value) == "BITCOIN_RPC_URL_INVALID"
    assert "synthetic" not in str(caught.value)


@pytest.mark.parametrize("network", ["main", "testnet", "signet", "testnet3", "", None])
def test_rpc_does_not_infer_a_network_from_node_or_url(cookie, network):
    with pytest.raises(ValueError, match="^BITCOIN_NETWORK_UNSUPPORTED$"):
        BitcoinRPC("http://localhost", cookie, network)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "public", "long", "empty", "no-password", "controls"])
def test_mainnet_cookie_is_bounded_owned_private_regular_single_inode(cookie, tmp_path, kind):
    selected = cookie
    if kind == "symlink":
        selected = tmp_path / "symlink"; selected.symlink_to(cookie)
    elif kind == "hardlink":
        os.link(cookie, tmp_path / "second-name")
    elif kind == "directory":
        selected = tmp_path
    elif kind == "fifo":
        selected = tmp_path / "fifo"; os.mkfifo(selected, 0o600)
    elif kind == "public":
        cookie.chmod(0o644)
    elif kind == "long":
        cookie.write_bytes(b"u:" + b"x" * bitcoin_rpc.MAX_COOKIE_BYTES)
    elif kind == "empty":
        cookie.write_bytes(b"")
    elif kind == "no-password":
        cookie.write_bytes(b"u:")
    else:
        cookie.write_bytes(b"u:password\nAuthorization: malicious")
    with pytest.raises(ValueError, match="^BITCOIN_RPC_COOKIE_INVALID$"):
        BitcoinRPC("http://localhost", selected, "mainnet")


def test_mainnet_cookie_owner_checked_before_credentials_are_read(cookie, monkeypatch):
    original = bitcoin_rpc.os.fstat
    def other_owner(fd):
        value = original(fd)
        return SimpleNamespace(st_mode=value.st_mode, st_uid=os.getuid() + 1,
                               st_nlink=value.st_nlink, st_size=value.st_size)
    monkeypatch.setattr(bitcoin_rpc.os, "fstat", other_owner)
    with pytest.raises(ValueError, match="^BITCOIN_RPC_COOKIE_INVALID$"):
        BitcoinRPC("http://localhost", cookie, "mainnet")


def test_regtest_retains_legacy_readable_fixture_cookie_permission(cookie):
    cookie.chmod(0o644)
    assert BitcoinRPC("http://fixture.example", cookie).network == "regtest"


def test_environment_proxy_cannot_receive_cookie(cookie, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic-proxy-password@attacker.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:8080")
    captured = []
    real_builder = bitcoin_rpc.urllib.request.build_opener
    def build(*handlers):
        captured.extend(handlers)
        return real_builder(*handlers)
    monkeypatch.setattr(bitcoin_rpc.urllib.request, "build_opener", build)
    BitcoinRPC("https://node.example", cookie, "mainnet")
    proxies = [handler for handler in captured if isinstance(handler, urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_sends_second_authenticated_request(cookie, status):
    calls = []
    class RedirectingServer(urllib.request.HTTPHandler):
        def http_open(self, request):
            calls.append(request)
            headers = Message()
            headers["Location"] = "http://attacker.example/collect-cookie"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, status)
            response.msg = "Redirect"
            return response
    rpc = BitcoinRPC("http://127.0.0.1:8332", cookie, "mainnet")
    rpc._opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), RedirectingServer(), bitcoin_rpc._NoRedirect())
    with pytest.raises(RuntimeError) as caught:
        rpc.call("getblockchaininfo")
    assert len(calls) == 1
    assert calls[0].full_url == "http://127.0.0.1:8332"
    assert "synthetic" not in str(caught.value)
    assert "attacker" not in str(caught.value)


def test_exact_request_id_decimal_amounts_and_null_result(cookie):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    requests = responding(rpc, lambda request:
        ('{"jsonrpc":"2.0","id":"%s","result":{"value":0.00000001}}' % request["id"]).encode())
    result = rpc.call("gettxout", "aa" * 32, 0, True)
    assert result == {"value": Decimal("0.00000001")}
    request, body, timeout = requests[0]
    assert body["params"] == ["aa" * 32, 0, True] and body["method"] == "gettxout"
    assert timeout == 30 and request.get_method() == "POST"
    assert base64.b64decode(request.get_header("Authorization").split()[1]) == b"__cookie__:synthetic-credential"
    responding(rpc, lambda request: dict(jsonrpc="2.0", id=request["id"], result=None))
    assert rpc.call("gettxout", "aa" * 32, 0) is None


@pytest.mark.parametrize("mutation", [
    "wrong-id", "missing-id", "wrong-version", "missing-result", "extra-field", "list",
    "bad-error", "boolean-error-code", "result-and-error", "duplicate-key", "nan", "invalid-json",
])
def test_ambiguous_or_mismatched_rpc_envelopes_fail_closed(cookie, mutation):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    def response(request):
        data = dict(jsonrpc="2.0", id=request["id"], result={"chain": "main"})
        if mutation == "wrong-id": data["id"] = "another-request"
        elif mutation == "missing-id": del data["id"]
        elif mutation == "wrong-version": data["jsonrpc"] = "1.0"
        elif mutation == "missing-result": del data["result"]
        elif mutation == "extra-field": data["secret"] = "synthetic-credential"
        elif mutation == "list": return b"[]"
        elif mutation == "bad-error": data["error"] = "synthetic-credential"
        elif mutation == "boolean-error-code":
            data["result"] = None; data["error"] = {"code": True, "message": "synthetic-credential"}
        elif mutation == "result-and-error": data["error"] = {"code": -1, "message": "synthetic-credential"}
        elif mutation == "duplicate-key":
            return json.dumps(data).replace('"chain": "main"', '"chain":"main","chain":"regtest"').encode()
        elif mutation == "nan":
            return json.dumps(data).replace('{"chain": "main"}', 'NaN').encode()
        elif mutation == "invalid-json": return b"synthetic-credential:invalid-json"
        return data
    responding(rpc, response)
    with pytest.raises(RuntimeError) as caught:
        rpc.call("getblockchaininfo")
    assert str(caught.value) in ("BITCOIN_RPC_RESPONSE_INVALID", "BITCOIN_RPC_FAILED")


def test_response_limit_bounds_untrusted_rpc_memory(cookie, monkeypatch):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    monkeypatch.setattr(bitcoin_rpc, "MAX_RESPONSE_BYTES", 100)
    responding(rpc, b"x" * 101)
    with pytest.raises(RuntimeError, match="^BITCOIN_RPC_RESPONSE_TOO_LARGE$"):
        rpc.call("getblockchaininfo")


def test_remote_and_transport_errors_do_not_escape_credentials(cookie):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    responding(rpc, lambda request: dict(jsonrpc="2.0", id=request["id"], error={
        "code": -1, "message": "synthetic-credential https://user:password@example/", "data": "/private/key"}))
    with pytest.raises(bitcoin_rpc.BitcoinRPCError, match="^BITCOIN_RPC_REJECTED$") as rejected:
        rpc.call("getblockchaininfo")
    assert bitcoin_rpc.observation_error(rejected.value) == {
        "error_code": "BITCOIN_RPC_REJECTED", "rpc_error_code": -1}
    assert "synthetic-credential" not in repr(rejected.value.__dict__)
    def failure(*args, **kwargs):
        raise OSError("synthetic-credential https://user:password@example/")
    rpc._opener = SimpleNamespace(open=failure)
    with pytest.raises(RuntimeError, match="^BITCOIN_RPC_FAILED$"):
        rpc.call("getblockchaininfo")


def test_http_error_retains_only_bounded_rpc_code_not_sensitive_body(cookie):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    def response(request, timeout):
        body = json.loads(request.data)
        raw = json.dumps(dict(jsonrpc="2.0", id=body["id"], error=dict(
            code=-5, message="synthetic-credential", data="/private/key"))).encode()
        raise urllib.error.HTTPError(request.full_url, 500, "private-reason", {}, io.BytesIO(raw))
    rpc._opener = SimpleNamespace(open=response)
    with pytest.raises(bitcoin_rpc.BitcoinRPCError) as caught:
        rpc.call("getrawtransaction", "txid", True)
    assert bitcoin_rpc.observation_error(caught.value) == dict(
        error_code="BITCOIN_RPC_REJECTED", rpc_error_code=-5, http_status=500)
    assert "synthetic-credential" not in repr(caught.value.__dict__)
    assert str(caught.value) == "BITCOIN_RPC_REJECTED"


def test_http_proxy_error_never_publishes_body_or_url(cookie):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    def response(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "private-reason", {}, io.BytesIO(b"credential-private-body"))
    rpc._opener = SimpleNamespace(open=response)
    with pytest.raises(bitcoin_rpc.BitcoinRPCError) as caught:
        rpc.call("getblockchaininfo")
    assert bitcoin_rpc.observation_error(caught.value) == dict(error_code="BITCOIN_RPC_FAILED", http_status=429)
    assert "credential" not in str(caught.value)


def test_check_network_uses_explicit_policy_and_never_infers_mainnet(cookie, monkeypatch):
    from signer_agent import bitcoin_network
    policy = {"network": "mainnet", "immutable-input": ["first"]}
    rpc = BitcoinRPC("https://node.example", cookie, "mainnet", policy)
    policy["immutable-input"][0] = "changed-outside-client"
    seen = []
    def check(client, network, bound_policy):
        seen.append((client, network, bound_policy))
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    monkeypatch.setattr(bitcoin_network, "check_core", check)
    with pytest.raises(ValueError, match="^BITCOIN_NETWORK_MISMATCH$"):
        rpc.check_network()
    assert seen == [(rpc, "mainnet", {"network": "mainnet", "immutable-input": ["first"]})]


@pytest.mark.parametrize("method", [None, "", "getblockchaininfo\nsecret", "synthetic secret", {}])
def test_invalid_rpc_methods_are_rejected_before_network(cookie, method):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    with pytest.raises(ValueError, match="^BITCOIN_RPC_METHOD_INVALID$"):
        rpc.call(method)


def test_non_json_parameter_and_nan_are_rejected_before_network(cookie):
    rpc = BitcoinRPC("http://localhost", cookie, "mainnet")
    for value in (object(), float("nan")):
        with pytest.raises(ValueError, match="^BITCOIN_RPC_REQUEST_INVALID$"):
            rpc.call("anymethod", value)


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 100_000_000), (21_000_000, 2_100_000_000_000_000),
    (Decimal("0.00000001"), 1), (Decimal("1.23456789"), 123456789),
    (Decimal("1.2345678900000"), 123456789), (Decimal("0E-999999999"), 0),
    (Decimal("21000000.00000000"), 2_100_000_000_000_000),
])
def test_core_amount_conversion_is_exact_without_decimal_context_rounding(value, expected):
    assert bitcoin_rpc.satoshis(value) == expected


@pytest.mark.parametrize("value", [
    "0.1", b"0.1", 0.1, True, None, {}, -1, 21_000_001,
    Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("-0.1"),
    Decimal("0.000000001"), Decimal("1E-999999999"), Decimal("1E+999999999"),
    Decimal("0.0000000100000000000000000000000000000000000000000000000001"),
])
def test_core_amount_rejects_type_confusion_sub_satoshi_and_huge_exponents(value):
    with pytest.raises(ValueError, match="^UTXO_AMOUNT_INVALID$"):
        bitcoin_rpc.satoshis(value)


def test_core_amount_accepts_long_zero_tail_without_repeated_tuple_copy():
    assert bitcoin_rpc.satoshis(Decimal("1." + "0" * 1_000_000)) == 100_000_000


def test_core_amount_does_not_round_away_inexact_digit_after_long_zero_tail():
    with pytest.raises(ValueError, match="^UTXO_AMOUNT_INVALID$"):
        bitcoin_rpc.satoshis(Decimal("1." + "0" * 1_000_000 + "1"))


def test_certificate_error_is_not_a_retryable_outage(cookie):
    import ssl
    rpc = BitcoinRPC("https://localhost", cookie, "mainnet")
    def failure(*args, **kwargs):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("sensitive endpoint"))
    rpc._opener = SimpleNamespace(open=failure)
    with pytest.raises(bitcoin_rpc.BitcoinRPCError, match="^BITCOIN_RPC_TLS_FAILED$") as caught:
        rpc.call("getblockchaininfo")
    assert not bitcoin_rpc.retryable_payment_observation(caught.value)
