import pytest
from types import SimpleNamespace

from signer_agent.evm_network import transaction_gas_with_margin
from signer_agent.participant_errors import participant_exception_code
from signer_agent.safe_errors import operator_error, pending_notice


@pytest.mark.parametrize("code", [
    "CONFEDERATION_DAO_CANDIDATE", "CONFEDERATION_DAO_CUSTODIAN", "CONFEDERATION_DAO_BINDING",
    "BITCOIN_RPC_TLS_FAILED", "BITCOIN_RPC_AUTH_REJECTED",
])
def test_local_custodian_error_keeps_its_code_and_action(code):
    result = operator_error(participant_exception_code(ValueError(code)), "dkg")
    assert result["code"] == code and result["stage"] == "dkg"
    assert result["status"] == "ERROR" and result["message"] and result["next_action"]


def test_pre_sign_gas_budget_reports_estimate_padded_limit_and_cap(monkeypatch):
    monkeypatch.setenv("SATOSHIFI_MAX_FEE_PER_GAS_WEI", "20")
    monkeypatch.setenv("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI", "2")
    monkeypatch.setenv("SATOSHIFI_MAX_TX_GAS", "225000")
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=1))
    assert transaction_gas_with_margin(w3, {"gas": 100000})["gas"] == 225000
    with pytest.raises(ValueError) as error:
        transaction_gas_with_margin(w3, {"gas": 100001})
    code = participant_exception_code(error.value)
    result = operator_error(code, "dkg", error.value)
    assert result["code"] == "ETHEREUM_TRANSACTION_GAS_LIMIT"
    assert (result["estimated_gas"], result["required_gas"], result["max_tx_gas"]) == (100001, 225001, 225000)
    assert "tx_hash" not in result


def test_pending_notice_keeps_the_original_hash_and_state():
    result = {"status": "TRANSACTION_PENDING", "tx_hash": "example", "step": "send"}
    notice = pending_notice(result)
    assert all(notice[key] == value for key, value in result.items())
    assert "message" in notice and "next_action" in notice
    assert "next_action" not in result
