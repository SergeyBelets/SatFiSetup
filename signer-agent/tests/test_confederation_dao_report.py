from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent.confederation_dao_report import _receipt


@pytest.mark.parametrize("attack", [None, "unconfirmed", "different_hash", "wrong_index",
                                    "unlisted_method", "empty_sweep"])
def test_expense_requires_canonical_successful_receipt_with_actual_income(attack):
    tx_hash = b"t" * 32
    block_hash = b"b" * 32
    payer = Web3.to_checksum_address(f"0x{1:040x}")
    target_address = Web3.to_checksum_address(f"0x{2:040x}")
    tx = dict(hash=tx_hash, blockNumber=6, blockHash=block_hash, transactionIndex=0,
              input=b"abcd", to=target_address)
    tx["from"] = payer
    receipt = dict(tx, status=1, transactionHash=tx_hash, gasUsed=21000, effectiveGasPrice=7)
    events = [] if attack == "empty_sweep" else [
        dict(address=target_address, args=dict(stream=3, amount=1))]
    target = SimpleNamespace(address=target_address, events=SimpleNamespace(
        Income=lambda: SimpleNamespace(process_receipt=lambda *args, **kwargs: events)))
    method = "unused" if attack == "unlisted_method" else "sweep"
    target.decode_function_input = lambda _: (SimpleNamespace(fn_name=method), {})
    included = b"x" * 32 if attack == "wrong_index" else tx_hash
    canonical = SimpleNamespace(hash=block_hash, timestamp=86400, transactions=[included])
    reads = []

    def block_at(number):
        reads.append(number)
        return canonical

    rpc = SimpleNamespace(eth=SimpleNamespace(get_block=block_at, get_transaction=lambda _: tx,
                           get_transaction_receipt=lambda _: receipt))
    head = SimpleNamespace(number=5 if attack == "unconfirmed" else 7)
    digest = Web3.to_hex(b"x" * 32 if attack == "different_hash" else tx_hash)
    targets = {target_address: (target, {"sweep"})}
    coordinator = SimpleNamespace(address=payer)
    anchors = {}
    if attack:
        with pytest.raises(ValueError, match="DAO_EXPENSE_"):
            _receipt(rpc, digest, 1, head, targets, coordinator, None, anchors)
        return

    row = _receipt(rpc, digest, 1, head, targets, coordinator, None, anchors)
    assert row["payer"] == payer and row["wei"] == str(21000 * 7)
    assert _receipt(rpc, digest, 1, head, targets, coordinator, None, anchors) == row
    assert reads == [6]
