from types import SimpleNamespace

import pytest
from requests.exceptions import HTTPError, ReadTimeout
from web3.exceptions import Web3RPCError

from signer_agent.evm_log_paging import log_pages, range_limit


def rejected(message, code=-32602):
    return Web3RPCError("redacted", rpc_response={"error": {"code": code, "message": message}})


@pytest.mark.parametrize("message", [
    "eth_getLogs requests with up to a 10 block range are supported",
    "block range too wide", "exceed maximum block range: 5000",
    "eth_getLogs is limited to a 10,000 blocks range",
    "query returned more than 10000 results", "Log response size exceeded",
    "response size limit exceeded", "too many results",
])
def test_known_provider_limits_are_classified_without_output(message, capsys):
    assert range_limit(rejected(message + " private-provider-token"))
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error", [
    rejected("daily request quota exceeded", -32005),
    rejected("rate limit exceeded", -32005),
    rejected("invalid API key", -32602),
    ReadTimeout("private-url-and-token"),
    ValueError("block range exceeded private-token"),
])
def test_unrelated_failures_do_not_trigger_range_retries(error):
    calls = []
    def get_logs(query):
        calls.append(query)
        raise error
    w3 = SimpleNamespace(eth=SimpleNamespace(get_logs=get_logs))
    with pytest.raises(type(error)) as caught:
        list(log_pages(w3, 0, 100, topics=["topic"]))
    assert caught.value is error
    assert len(calls) == 1


def test_ten_block_provider_covers_every_block_once_after_shrinking():
    calls, successful = [], []
    def get_logs(query):
        calls.append(query)
        start, end = query["fromBlock"], query["toBlock"]
        if end - start + 1 > 10:
            raise rejected("only up to a 10 block range is supported")
        successful.append((start, end))
        return list(range(start, end + 1))
    w3 = SimpleNamespace(eth=SimpleNamespace(get_logs=get_logs))
    result = [block for page in log_pages(w3, 31, 158, topics=["topic"]) for block in page]
    assert result == list(range(31, 159))
    assert all(end - start + 1 <= 10 for start, end in successful)
    assert all(query["topics"] == ["topic"] for query in calls)
    assert successful[0][0] == 31 and successful[-1][1] == 158


def test_one_block_limit_failure_never_turns_into_empty_success():
    calls = []
    error = rejected("query returned more than 10000 results")
    def get_logs(query):
        calls.append(query)
        raise error
    w3 = SimpleNamespace(eth=SimpleNamespace(get_logs=get_logs))
    with pytest.raises(Web3RPCError) as caught:
        list(log_pages(w3, 50, 55, topics=[]))
    assert caught.value is error
    assert calls[-1]["fromBlock"] == calls[-1]["toBlock"] == 50
    assert len(calls) == 3


def test_failure_after_successful_page_cannot_report_partial_search_as_complete():
    def get_logs(query):
        if query["fromBlock"]:
            raise ReadTimeout("secret")
        return ["first-page"]
    pages = log_pages(SimpleNamespace(eth=SimpleNamespace(get_logs=get_logs)), 0, 20, topics=[], initial_span=10)
    assert next(pages) == ["first-page"]
    with pytest.raises(ReadTimeout):
        next(pages)


def test_http_413_and_legacy_json_rpc_range_errors_are_supported():
    assert range_limit(HTTPError(response=SimpleNamespace(status_code=413)))
    assert not range_limit(HTTPError(response=SimpleNamespace(status_code=429)))
    assert range_limit(ValueError({"code": -32005, "message": "block range limit exceeded"}))


@pytest.mark.parametrize("first,last", [(-1, 5), (6, 5), (True, 5), (0, "5")])
def test_invalid_discovery_range_is_rejected(first, last):
    with pytest.raises(ValueError, match="INVALID_FROM_BLOCK"):
        list(log_pages(None, first, last, topics=[]))
