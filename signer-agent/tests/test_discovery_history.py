"""Bounded invitation discovery retains complete evidence checks and RPC failures."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests.exceptions import ConnectionError
from web3 import Web3

from signer_agent import launch_configurator, onboarding, onchain_transport


OPERATOR = Web3.to_checksum_address("0x" + "11" * 20)
BOARD = Web3.to_checksum_address("0x" + "22" * 20)


@pytest.fixture
def search(monkeypatch):
    requests = []
    w3 = SimpleNamespace(eth=SimpleNamespace(block_number=30_000_000))
    w3.eth.get_logs = lambda query: requests.append(query) or []
    w3.eth.get_block = lambda number: dict(number=number, hash=b"a" * 32)
    verify = Mock(return_value=(SimpleNamespace(address=BOARD), {"operator": OPERATOR}))
    monkeypatch.setattr(onchain_transport, "invitation", verify)
    return w3, requests, verify


def test_default_discovery_bounds_history_and_reports_exact_scope(search):
    w3, requests, verify = search
    result = onboarding.discover_invitations(w3, OPERATOR)
    assert result == dict(status="NOT_FOUND_IN_RANGE", invitations=[],
        searched_from_block="29991809", searched_to_block="30000000", complete_history=False)
    assert requests[0]["fromBlock"] == 29_991_809 and requests[-1]["toBlock"] == 30_000_000
    assert sum(r["toBlock"] - r["fromBlock"] + 1 for r in requests) == 8192
    assert len(requests) == 5 and not verify.called


@pytest.mark.parametrize("first,status,complete", [(0, "NOT_FOUND", True), (5000, "NOT_FOUND_IN_RANGE", False)])
def test_explicit_range_preserves_full_history_option(search, first, status, complete):
    w3, requests, _ = search
    w3.eth.block_number = 6000
    result = onboarding.discover_invitations(w3, OPERATOR, from_block=first)
    assert result["status"] == status and result["complete_history"] is complete
    assert requests[0]["fromBlock"] == first and requests[-1]["toBlock"] == 6000


def test_creation_block_from_event_is_passed_only_as_locator(search):
    w3, _, verify = search
    event = dict(address=BOARD, blockNumber=29_999_999)
    w3.eth.get_logs = lambda _: [event, event]
    result = onboarding.discover_invitations(w3, OPERATOR, recent_blocks=2)
    assert result["status"] == "FOUND" and len(result["invitations"]) == 1
    verify.assert_called_with(w3, BOARD, birth_hint=29_999_999)


@pytest.mark.parametrize("address,number", [(OPERATOR,29_999_999),(BOARD,29_999_998),(BOARD,30_000_001)])
def test_rpc_cannot_escape_discovery_board_or_block_scope(search,address,number):
    w3,_,verify=search
    w3.eth.get_logs=lambda _: [dict(address=address,blockNumber=number)]
    with pytest.raises(ValueError,match="^DISCOVERY_EVENT_SCOPE$"):
        onboarding.discover_invitations(w3,OPERATOR,from_block=29_999_999,board_address=BOARD)
    verify.assert_not_called()


@pytest.mark.parametrize("failure", [
    ValueError("BTC_CREATION_ANCHOR_UNAVAILABLE"), ValueError("BTC_CREATION_ANCHOR_REORG"),
    ValueError("BTC_CREATION_RECEIPT_MISMATCH"), ValueError("BTC_CREATION_BLOCK_MISMATCH"),
    ValueError("EVM_CONFIRMATIONS_PENDING"), ValueError("ETHEREUM_FINALITY_UNAVAILABLE"),
    ValueError({"code": -32000, "message": "historical state unavailable"}),
    ValueError("unclassified failure"), RuntimeError("provider failed"), ConnectionError("provider failed"),
])
def test_failed_candidate_verification_is_never_reported_as_not_found(search, failure):
    w3, _, verify = search
    w3.eth.get_logs = lambda _: [dict(address=BOARD, blockNumber=29_999_999)]
    verify.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        onboarding.discover_invitations(w3, OPERATOR, recent_blocks=2)
    assert caught.value is failure


def test_invalid_candidate_does_not_hide_later_authentic_invitation(search):
    w3, _, verify = search
    w3.eth.get_logs = lambda _: [dict(address=BOARD, blockNumber=29_999_999)] * 2
    verify.side_effect = [ValueError("BOARD_INVITATION_SCHEMA"), verify.return_value]
    result = onboarding.discover_invitations(w3, OPERATOR, recent_blocks=2)
    assert result["status"] == "FOUND" and len(result["invitations"]) == 1


def test_reorg_during_empty_search_never_becomes_not_found(search):
    w3, _, _ = search
    w3.eth.get_block = Mock(side_effect=[dict(hash=b"a" * 32), dict(hash=b"b" * 32)])
    with pytest.raises(ValueError, match="ONCHAIN_ORIGIN_REORG"):
        onboarding.discover_invitations(w3, OPERATOR, recent_blocks=2)


@pytest.mark.parametrize("options", [dict(recent_blocks=0), dict(recent_blocks=True),
    dict(from_block=-1), dict(from_block=True), dict(from_block=30_000_001)])
def test_invalid_discovery_scope_makes_no_log_requests(search, options):
    w3, requests, _ = search
    with pytest.raises(ValueError, match="DISCOVERY_RANGE_INVALID"):
        onboarding.discover_invitations(w3, OPERATOR, **options)
    assert not requests


@pytest.mark.parametrize("first,rpc_chain,expected", [
    ("29999993", 11155111, 29_999_993),
    (None, 11155111, 29_991_809),
    ("29999993", 31337, None),
])
def test_cli_uses_configured_floor_only_on_configured_evm(
    search, monkeypatch, tmp_path, capsys, first, rpc_chain, expected
):
    w3, requests, _ = search
    w3.eth.chain_id = rpc_chain
    monkeypatch.setattr(onboarding, "evm_web3", Mock(return_value=w3))
    config = launch_configurator.defaults()
    config.update(ethereum_network="sepolia", evm_discovery_from_block=first)
    config_path = tmp_path / "launch.json"
    config_path.write_text(json.dumps(config))
    args = ["discover", "--operator", OPERATOR, "--rpc", "http://example.invalid",
            "--launch-config", str(config_path)]
    if expected is None:
        with pytest.raises(ValueError, match="^CHAIN_MISMATCH$"):
            onboarding.main(args)
        assert not requests
        return
    assert onboarding.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["searched_from_block"] == str(expected)
    assert requests[0]["fromBlock"] == expected
    assert requests[-1]["toBlock"] == w3.eth.block_number
