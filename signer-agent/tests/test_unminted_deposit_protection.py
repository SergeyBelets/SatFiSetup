from types import SimpleNamespace

import pytest

from signer_agent.bitcoin_onchain_payment import protect_unminted_deposit


def fixture(*, backing=0, change=False, scripts=(), txid="aa" * 32):
    reads = []
    def call(value):
        def execute(**kwargs):
            assert kwargs == {"block_identifier": 123}
            return value
        return SimpleNamespace(call=execute)
    deposits = SimpleNamespace(functions=SimpleNamespace(
        backingSats=lambda _: call(backing), walletOutput=lambda _: call(change)))
    def rpc(*args):
        reads.append(args)
        assert args == ("getrawtransaction", "aa" * 32, True)
        return dict(txid=txid, vout=[dict(scriptPubKey={"hex": script}) for script in scripts])
    return deposits, dict(outpoint="aa" * 32 + ":0"), SimpleNamespace(call=rpc), reads


@pytest.mark.parametrize("prefix", ["6a38", "6a4c38", "6a4d3800", "6a4e38000000"])
def test_committed_unissued_input_cannot_fund_treasury_or_miner_fee(prefix):
    deposits, item, bitcoin, reads = fixture(scripts=[prefix + "534d5031" + "11" * 52])
    with pytest.raises(ValueError, match="^BTC_UNMINTED_COMMITTED_DEPOSIT$"):
        protect_unminted_deposit(deposits, item, bitcoin, 123)
    assert len(reads) == 1


@pytest.mark.parametrize("options", [dict(backing=100), dict(change=True)])
def test_minted_backing_or_recorded_change_needs_no_history(options):
    deposits, item, bitcoin, reads = fixture(**options, scripts=["6a38534d5031"])
    protect_unminted_deposit(deposits, item, bitcoin, 123)
    assert reads == []


def test_unmarked_donation_is_still_usable_with_threshold_review():
    deposits, item, bitcoin, reads = fixture(scripts=["5120" + "11" * 32, "6a0454455354"])
    protect_unminted_deposit(deposits, item, bitcoin, 123)
    assert len(reads) == 1


def test_wrong_rpc_transaction_is_rejected():
    deposits, item, bitcoin, _ = fixture(txid="bb" * 32)
    with pytest.raises(ValueError, match="^UTXO_MISMATCH$"):
        protect_unminted_deposit(deposits, item, bitcoin, 123)


def test_rpc_failure_does_not_turn_unknown_deposit_into_treasury():
    deposits, item, bitcoin, _ = fixture()
    def failed(*args):
        raise ConnectionError("fixture")
    bitcoin.call = failed
    with pytest.raises(ConnectionError):
        protect_unminted_deposit(deposits, item, bitcoin, 123)
