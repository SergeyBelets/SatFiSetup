"""Shared EVM transport and same-height agreement for independent RPC reads."""

import json
from types import SimpleNamespace

import pytest
import requests
from web3.exceptions import BlockNotFound

from signer_agent.evm_network import read_common_block
from signer_agent.evm_rpc import http_provider, witness_endpoint


@pytest.mark.parametrize("url", [
    "http://rpc.example.test/v3/private",
    "http://localhost.example.test",
    "http://192.0.2.3",
    "http://[2001:db8::1]",
    "http://user:private@rpc.example.test",
])
def test_plaintext_denied_before_session_creation(url, monkeypatch):
    monkeypatch.setattr(requests.Session, "__init__", lambda _: pytest.fail("No session needed"))
    with pytest.raises(ValueError, match="^EVM_RPC_HTTPS_REQUIRED$"):
        http_provider(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8545/",
    "http://[::1]:8545/",
    "http://localhost:8545/",
    "http://fixture-user:fixture-password@127.0.0.1:8545/",
    "https://rpc.example.test/path?key=synthetic-key",
    "https://example-user:example-password@rpc.example.test/path",
])
def test_shared_runtime_transport_does_not_inherit_proxy_or_ca_settings(url, monkeypatch):
    received = []

    def send(_adapter, request, **options):
        received.append((request.url, options["proxies"], options["verify"], options["timeout"]))
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(dict(jsonrpc="2.0", id=1, result="0x1")).encode()
        result._content_consumed = True
        return result

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.test")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "untrusted-ca.pem")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    provider = http_provider(url, timeout=13)
    assert provider.make_request("eth_chainId", [])["result"] == "0x1"
    # Batch uses the very same transport.
    provider.make_batch_request([("eth_chainId", [])])
    assert received == [(url, {}, True, 13)] * 2
    assert url not in str(provider)


@pytest.mark.parametrize("status, code", [(301, "REDIRECT"), (307, "REDIRECT"), (401, "HTTP_STATUS")])
def test_rpc_rejects_redirects_and_reports_only_the_status(monkeypatch, status, code):
    requests_seen = []

    def send(_adapter, request, **_options):
        requests_seen.append(request.url)
        result = requests.Response()
        result.status_code = status
        result.headers["Location"] = "https://other.example.test/other-credential"
        result.url = request.url
        result._content = b"synthetic provider credential"
        result._content_consumed = True
        return result

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    endpoint = "https://rpc.example.test/synthetic-credential"
    with pytest.raises(requests.HTTPError, match=f"^EVM_RPC_{code}$") as raised:
        http_provider(endpoint).make_request("eth_chainId", [])
    assert requests_seen == [endpoint]
    assert raised.value.response.status_code == status
    assert not raised.value.response.url and not raised.value.response.headers


def test_transport_errors_hide_endpoint_and_requests_details(monkeypatch):
    def fail(*_args, **_kwargs):
        raise requests.exceptions.SSLError("bad TLS certificate for a synthetic credential")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fail)
    with pytest.raises(requests.ConnectionError, match="^EVM_RPC_TRANSPORT$"):
        http_provider("https://rpc.example.test/synthetic-credential").make_request("eth_chainId", [])


@pytest.mark.parametrize("url", [None, "https://", "https://host.test:99999",
                                "https://host.test/path#fragment", "https://host.test/\nsecret"])
def test_bad_endpoints_report_a_static_code(url):
    with pytest.raises(ValueError, match="^EVM_RPC_ENDPOINT$"):
        http_provider(url)


def test_witness_requires_distinct_provider_and_a_secure_endpoint():
    primary = "https://rpc.example.test/key-a"
    assert witness_endpoint(dict(evm_rpc=primary)) is None
    with pytest.raises(ValueError, match="^EVM_RPC_WITNESS_NOT_INDEPENDENT$"):
        witness_endpoint(dict(evm_rpc=primary, evm_witness_rpc="https://RPC.example.test./key-b"))
    with pytest.raises(ValueError, match="^EVM_RPC_HTTPS_REQUIRED$"):
        witness_endpoint(dict(evm_rpc=primary, evm_witness_rpc="http://another.example.test"))
    assert witness_endpoint(dict(evm_rpc=primary, evm_witness_rpc="https://another.example.test"))
    assert witness_endpoint(dict(evm_rpc="http://127.0.0.1:8545", evm_witness_rpc="http://127.0.0.1:9545"))


def node(tip, *, finalized=None, fork=None, chain_id=11155111):
    def read(number):
        if number == "latest":
            number = tip
        elif number == "finalized":
            number = finalized
        if number > tip:
            raise BlockNotFound()
        block_hash = number if fork is None or number < fork else number + 10_000
        return SimpleNamespace(number=number, hash=block_hash.to_bytes(32, "big"))

    return SimpleNamespace(eth=SimpleNamespace(chain_id=chain_id, get_block=read))


@pytest.mark.parametrize("heads", [(120, 110), (110, 120), (110, 110)])
def test_common_block_observes_different_heads_and_confirms_both(heads):
    first, second = map(node, heads)
    assert read_common_block(first, second, 4, 11155111).number == 107
    with pytest.raises(ValueError, match="^EVM_RPC_BEHIND$"):
        read_common_block(first, second, 4, 11155111, minimum=108)


def test_witness_disagreement_at_one_anchor_is_distinct_from_lag():
    with pytest.raises(ValueError, match="^EVM_RPC_WITNESS_MISMATCH$"):
        read_common_block(node(120), node(110, fork=107), 4, 11155111)
    with pytest.raises(ValueError, match="^EVM_RPC_WITNESS_MISMATCH$"):
        read_common_block(node(120), node(110, chain_id=31337), 4, 11155111)


def test_mainnet_common_block_retains_finality_for_both_providers():
    assert read_common_block(node(120, finalized=105, chain_id=1),
                             node(118, finalized=104, chain_id=1), 12, 1).number == 104
