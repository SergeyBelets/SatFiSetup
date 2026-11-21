from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent import bitcoin_onchain_payment as payment, callback_admission


@pytest.mark.parametrize("fee,first_limit,gross,error", [
    (29, 15, 350, None),
    (31, 15, 350, "BTC_WITHDRAWAL_FEE_CAP"),
    (49, 24, 350, "BTC_WITHDRAWAL_FEE_CAP"),
    (29, 15, 308, "BTC_WITHDRAWAL_BELOW_MINIMUM"),
])
def test_user_funded_miner_fee_uses_individual_limits_and_aggregates_same_destination(
        monkeypatch, fee, first_limit, gross, error):
    address = Web3.to_checksum_address(f"0x{1:040x}")
    zero = Web3.to_checksum_address(f"0x{0:040x}")
    destination = "0014" + "11" * 20
    wallet = "5120" + "22" * 32
    refs = ["11" * 32, "22" * 32]
    rows = {bytes.fromhex(ref): [address, index, address, address, gross * 10**10,
            bytes.fromhex(destination), bytes(32), 1] for index, ref in enumerate(refs)}

    def call(value):
        def read(*, block_identifier):
            assert block_identifier == 7
            return value
        return SimpleNamespace(call=read)

    pool = SimpleNamespace(functions=SimpleNamespace(minerFeeLimitSats=lambda index:
        call(first_limit if index == 0 else 2**64 - 1)))
    escrow = SimpleNamespace(abi=[{"name": "defaultMinerFeeLimitSats"}],
        w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **_: pool)),
        functions=SimpleNamespace(requests=lambda ref: call(rows[ref]), minP2WPKHSats=lambda: call(294),
                                  defaultMinerFeeLimitSats=lambda: call(24)))
    deposits = SimpleNamespace(functions=SimpleNamespace(scriptPools=lambda _: call(zero)))
    contracts = dict(escrow=escrow, deposits=deposits)
    monkeypatch.setattr(payment, "artifact", lambda _: dict(abi=[]))
    validated = []
    monkeypatch.setattr(callback_admission, "validate_callback", lambda _, row, block: validated.append(row[1]))
    if error:
        with pytest.raises(ValueError, match=error):
            payment._requests(contracts, refs, wallet, 7, fee=fee)
        return
    assert payment._requests(contracts, refs, wallet, 7, fee=fee) == {destination: 2 * gross - fee}
    assert validated == [0, 1]
