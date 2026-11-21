"""Fee sale signing reviews pin the approved, funded operation."""

from types import SimpleNamespace

import pytest

from signer_agent import fee_sale_governance as policy


def addr(byte):
    return "0x" + byte * 40


@pytest.fixture
def review_scope(monkeypatch):
    sale = SimpleNamespace(address=addr("1"), abi=[dict(
        name="getFillBidOperationHash", inputs=[dict(type="uint256")])])
    source = SimpleNamespace(address=addr("2"))
    auth = SimpleNamespace(address=addr("3"))
    block = SimpleNamespace(number=11, timestamp=1000, hash=bytes(32))
    ctx = dict(sale=sale, source=source, authority=auth, validator=addr("4"), bound=sale.address, block=block)
    pin = dict(chain_id="31337", stablecoin=addr("5"), fee_token=addr("6"))
    buyer, coordinator = addr("7"), addr("8")
    digest = bytes.fromhex("11" * 32)
    values = {
        (auth.address, "coordinator"): coordinator,
        (auth.address, "executedOperations"): False,
        (sale.address, "getFillBidOperationHash"): digest,
        (sale.address, "bids"): (buyer, 15, 30, 1100),
        (sale.address, "inventory"): 20,
        (sale.address, "stableEscrow"): 40,
        (pin["stablecoin"], "balanceOf"): 40,
        (pin["fee_token"], "balanceOf"): 20,
    }
    monkeypatch.setattr(policy, "context", lambda *_a, **_kw: ctx)
    monkeypatch.setattr(policy, "contract", lambda _w, _kind, target: SimpleNamespace(address=target))
    monkeypatch.setattr(policy, "read", lambda node, at, name, *_: values[node.address, name])
    w3 = SimpleNamespace(eth=SimpleNamespace(get_block=lambda _: block))
    intent = dict(chain_id="31337", coordinator=coordinator, role="conf_ceremony", fee_sale="fill",
                  artifact=policy.SALE, target=sale.address, hash_method="getFillBidOperationHash",
                  hash_args=["1"], operation_hash="0x" + digest.hex())
    return w3, intent, pin, ctx, values


def test_reviews_funded_exact_bid(review_scope):
    w3, intent, pin, _, _ = review_scope
    result = policy.review(w3, intent, {"chain_id": "31337"}, 11, pin)
    assert result["method"] == "fillBid" and result["arguments"] == [1]


@pytest.mark.parametrize("change", ["insolvent", "short_inventory", "expired", "wrong_hash", "unbound"])
def test_signer_rejects_unavailable_or_changed_sale(review_scope, change):
    w3, intent, pin, ctx, values = review_scope
    sale = ctx["sale"].address
    if change == "insolvent":
        values[pin["stablecoin"], "balanceOf"] = 29
    elif change == "short_inventory":
        values[sale, "inventory"] = 14
    elif change == "expired":
        values[sale, "bids"] = (addr("7"), 15, 30, 999)
    elif change == "wrong_hash":
        values[sale, "getFillBidOperationHash"] = bytes(32)
    else:
        ctx["bound"] = policy.ZERO
    with pytest.raises(ValueError, match="^FEE_SALE_(BID_UNAVAILABLE|OPERATION_HASH|SOURCE)$"):
        policy.review(w3, intent, {"chain_id": "31337"}, 11, pin)


def test_mainnet_pin_requires_local_finality_floor():
    pin = dict(version="1", chain_id="1", confirmations="12", role="evm_ceremony", board=addr("1"),
               factory=addr("2"), sale=addr("3"), fee_token=addr("4"), stablecoin=addr("5"), treasury=addr("6"))
    assert policy.validate_pin(pin) == pin
    with pytest.raises(ValueError, match="^FEE_SALE_PIN$"):
        policy.validate_pin(dict(pin, confirmations="11"))
