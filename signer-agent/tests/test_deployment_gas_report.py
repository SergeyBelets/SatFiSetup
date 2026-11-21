import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from hexbytes import HexBytes


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("gas_report", ROOT / "ops/deployment_gas.py")
gas_report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gas_report)


def fixture(monkeypatch):
    hashes = ["0x" + f"{i:064x}" for i in range(1, 7)]
    address, block_hash = "0x" + "11" * 20, b"a" * 32
    block = SimpleNamespace(number=20, hash=block_hash)
    state = dict(schema="satoshifi-deployment-v2", chain_id=1, confirmations=12, sender=address,
        transactions={h: dict(nonce=i, contract="component") for i, h in enumerate(hashes)})
    transactions = {h: dict(gas=10_000_000, nonce=i, **{"from": address}) for i, h in enumerate(hashes)}
    receipts = {h: dict(transactionHash=HexBytes(h), blockNumber=19, blockHash=block_hash,
        gasUsed=8_000_000, effectiveGasPrice=2_000_000_000, status=1) for h in hashes}
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=1,
        get_block=lambda _: block, get_transaction=transactions.__getitem__,
        get_transaction_receipt=receipts.__getitem__))
    monkeypatch.setattr(gas_report, "read_block", lambda *args: block)
    return w3, state, transactions, receipts


def test_total_gas_above_transaction_cap_is_not_an_oversized_transaction(monkeypatch):
    w3, state, _, _ = fixture(monkeypatch)
    result = gas_report.report(w3, state)
    assert result["total_gas_used"] == 48_000_000
    assert result["maximum_transaction_gas_limit"] == 10_000_000 < result["protocol_transaction_cap"]
    assert result["total_fee_paid_wei"] == str(48_000_000 * 2_000_000_000)


@pytest.mark.parametrize("change", ["pending", "fork", "wrong_sender", "wrong_nonce", "cap"])
def test_only_final_matching_receipts_with_valid_signed_limit_are_counted(monkeypatch, change):
    w3, state, transactions, receipts = fixture(monkeypatch)
    tx, receipt = next(iter(transactions.values())), next(iter(receipts.values()))
    if change == "pending":
        receipt["blockNumber"] = 21
    elif change == "fork":
        receipt["blockHash"] = b"b" * 32
    elif change == "wrong_sender":
        tx["from"] = "0x" + "22" * 20
    elif change == "wrong_nonce":
        tx["nonce"] += 1
    else:
        tx["gas"] = 2 ** 24 + 1
    with pytest.raises(ValueError):
        gas_report.report(w3, state)
