"""Budget enforcement checks actual signed envelopes, not Forge estimates."""
import importlib.util
from pathlib import Path

from eth_account import Account
import pytest

SPEC = importlib.util.spec_from_file_location("rpc_budget", Path(__file__).resolve().parents[2] / "ops/ethereum_rpc_budget.py")
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)
KEY = "0x" + "00" * 31 + "01"
SENDER = Account.from_key(KEY).address
FEES = dict(maxFeePerGas=100, maxPriorityFeePerGas=10)


def request(method, params):
    return dict(jsonrpc="2.0", id=1, method=method, params=params)


def signed(**changes):
    transaction = dict(chainId=1, type=2, nonce=0, gas=21000, maxFeePerGas=100,
        maxPriorityFeePerGas=10, to="0x" + "11" * 20, value=0, data=b"")
    transaction.update(changes)
    return request("eth_sendRawTransaction", [Account.sign_transaction(transaction, KEY).raw_transaction.hex()])


def test_budget_accepts_signed_eip1559_transaction_and_public_reads():
    budget.checked_request(signed(), SENDER, FEES, 21000)
    budget.checked_request(request("eth_getBlockByNumber", ["finalized", False]), SENDER, FEES, 21000)


def test_budget_accepts_forge_no_argument_rpc_without_params():
    budget.checked_request(dict(jsonrpc="2.0", id=1, method="eth_chainId"), SENDER, FEES, 21000)
    with pytest.raises(ValueError, match="RPC_METHOD_REJECTED"):
        budget.checked_request(dict(jsonrpc="2.0", id=1, method="eth_sendRawTransaction"), SENDER, FEES, 21000)


@pytest.mark.parametrize("change", [dict(chainId=11155111), dict(gas=21001),
    dict(maxFeePerGas=101), dict(maxPriorityFeePerGas=11), dict(maxPriorityFeePerGas=101), dict(gas=2**24+1), dict(value=1)])
def test_budget_rejects_signed_transaction_above_any_bound(change):
    with pytest.raises(ValueError, match="SIGNED_TRANSACTION_BUDGET_REJECTED"):
        budget.checked_request(signed(**change), SENDER, FEES, 21000)


def test_budget_rejects_wrong_signer_and_legacy_transactions():
    with pytest.raises(ValueError, match="SIGNED_TRANSACTION_BUDGET_REJECTED"):
        budget.checked_request(signed(), "0x" + "22" * 20, FEES, 21000)
    raw = Account.sign_transaction(dict(chainId=1, nonce=0, gas=21000, gasPrice=10,
        to="0x" + "11" * 20, value=0), KEY).raw_transaction.hex()
    with pytest.raises(ValueError, match="SIGNED_TRANSACTION_BUDGET_REJECTED"):
        budget.checked_request(request("eth_sendRawTransaction", [raw]), SENDER, FEES, 21000)


@pytest.mark.parametrize("method", ["eth_sendTransaction", "wallet_sendTransaction", "personal_sendTransaction",
    "eth_sendUserOperation", "anvil_setBalance", "hardhat_setCode", "admin_addPeer"])
def test_budget_blocks_other_mutating_rpc_methods(method):
    with pytest.raises(ValueError, match="RPC_METHOD_REJECTED"):
        budget.checked_request(request(method, []), SENDER, FEES, 21000)


@pytest.mark.parametrize("value", [None, {}, [], dict(jsonrpc="2.0", id=True, method="eth_chainId", params=[]),
    dict(jsonrpc="2.0", id=1, method="eth_chainId", params=[], extra="ignored")])
def test_budget_rejects_ambiguous_rpc_envelopes(value):
    with pytest.raises(ValueError, match="RPC_REQUEST_REJECTED"):
        budget.checked_request(value, SENDER, FEES, 21000)
