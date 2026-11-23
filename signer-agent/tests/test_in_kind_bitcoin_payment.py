from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent import bitcoin_onchain_payment as payment, callback_admission


@pytest.mark.parametrize("fee,first_limit,gross,owner_minima,error", [
    (29, 15, 350, None, None),
    (31, 15, 350, None, "BTC_WITHDRAWAL_FEE_CAP"),
    (49, 24, 350, None, "BTC_WITHDRAWAL_FEE_CAP"),
    (29, 15, 308, None, "BTC_WITHDRAWAL_BELOW_MINIMUM"),
    (29, 15, 350, (335, 336), None),
    (29, 15, 350, (336, 335), "BTC_WITHDRAWAL_BELOW_MINIMUM"),
    (29, 15, 350, (335, 337), "BTC_WITHDRAWAL_BELOW_MINIMUM"),
    (29, 15, 350, (0, 336), "BTC_WITHDRAWAL_MINIMUM_INVALID"),
    (29, 15, 350, (RuntimeError("RPC_UNAVAILABLE"), 336), "RPC_UNAVAILABLE"),
])
@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_user_funded_miner_fee_uses_individual_limits_and_aggregates_same_destination(
        monkeypatch, fee, first_limit, gross, bound, owner_minima, error):
    address = Web3.to_checksum_address(f"0x{1:040x}")
    zero = Web3.to_checksum_address(f"0x{0:040x}")
    destination = "0014" + "11" * 20
    wallet = "5120" + "22" * 32
    refs = ["11" * 32, "22" * 32]
    root = bytes.fromhex("33" * 32) if bound else None
    rows = {bytes.fromhex(ref): [address, index, address, address, gross * 10**10,
            bytes.fromhex(destination), root if bound else bytes(32), 2 if bound else 1]
            for index, ref in enumerate(refs)}

    def call(value):
        def read(*, block_identifier):
            assert block_identifier == 7
            if isinstance(value, RuntimeError):
                raise value
            return value
        return SimpleNamespace(call=read)

    pool = SimpleNamespace(functions=SimpleNamespace(minerFeeLimitSats=lambda index:
        call(first_limit if index == 0 else 2**64 - 1)))
    pool_abi = []
    if owner_minima is not None:
        pool_abi.append({"type": "function", "name": "minimumPayoutSats"})
        pool.functions.minimumPayoutSats = lambda index: call(owner_minima[index])
    escrow = SimpleNamespace(abi=[{"name": "defaultMinerFeeLimitSats"}],
        w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **_: pool)),
        functions=SimpleNamespace(requests=lambda ref: call(rows[ref]), minP2WPKHSats=lambda: call(294),
                                  defaultMinerFeeLimitSats=lambda: call(24)))
    deposits = SimpleNamespace(functions=SimpleNamespace(scriptPools=lambda _: call(zero)))
    contracts = dict(escrow=escrow, deposits=deposits)
    monkeypatch.setattr(payment, "artifact", lambda _: dict(abi=pool_abi))
    validated = []
    monkeypatch.setattr(callback_admission, "validate_callback", lambda _, row, block: validated.append(row[1]))
    if error:
        with pytest.raises(RuntimeError if error == "RPC_UNAVAILABLE" else ValueError, match=error):
            payment._requests(contracts, refs, wallet, 7, root=root, fee=fee)
        return
    assert payment._requests(contracts, refs, wallet, 7, root=root, fee=fee) == {destination: 2 * gross - fee}
    assert validated == [0, 1]


def test_registered_destination_is_allowed_only_for_an_existing_v3_binding(monkeypatch):
    address = Web3.to_checksum_address(f"0x{1:040x}")
    destination = "0014" + "11" * 20
    wallet = "5120" + "22" * 32
    ref = "44" * 32
    root = bytes.fromhex("55" * 32)
    row = [address, 1, address, address, 350 * 10**10, bytes.fromhex(destination), bytes(32), 1]

    def call(value):
        return SimpleNamespace(call=lambda **_: value)

    pool = SimpleNamespace(functions=SimpleNamespace(minerFeeLimitSats=lambda _: call(30)))
    escrow = SimpleNamespace(
        abi=[{"name": "defaultMinerFeeLimitSats"}],
        w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **_: pool)),
        functions=SimpleNamespace(requests=lambda _: call(row), minP2WPKHSats=lambda: call(294)),
    )
    deposits = SimpleNamespace(functions=SimpleNamespace(scriptPools=lambda _: call(address)))
    contracts = dict(escrow=escrow, deposits=deposits)
    monkeypatch.setattr(payment, "artifact", lambda _: dict(abi=[]))
    monkeypatch.setattr(callback_admission, "validate_callback", lambda *_: None)

    with pytest.raises(ValueError, match="BTC_WITHDRAWAL_TO_WALLET_UNSUPPORTED"):
        payment._requests(contracts, [ref], wallet, 7, root=root, fee=29)
    row[-2:] = [root, 2]
    with pytest.raises(ValueError, match="ESCROW_REQUEST_STATE"):
        payment._requests(contracts, [ref], wallet, 7, root=bytes(32), fee=29)
    with pytest.raises(ValueError, match="BTC_WITHDRAWAL_TO_WALLET_UNSUPPORTED"):
        payment._requests(dict(contracts, address_registry=object()), [ref], wallet, 7, root=root)
    assert payment._requests(contracts, [ref], wallet, 7, root=root, fee=29) == {destination: 321}


def test_source_handover_keeps_one_exact_output_with_no_unrelated_requests(monkeypatch):
    addresses = [Web3.to_checksum_address(f"0x{i:040x}") for i in range(5)]
    zero, gateway, legacy_owner, token, other_token = addresses
    script = "5120" + "ab" * 32
    wallet = "5120" + "cd" * 32
    refs = ["11" * 32, "22" * 32, "33" * 32]
    rows = {
        bytes.fromhex(ref): (zero, index, token, owner, amount * 10**10, bytes.fromhex(script), bytes(32), 1)
        for index, (ref, owner, amount) in enumerate(zip(refs, [gateway, gateway, legacy_owner], [500, 700, 500]))
    }

    def call(value):
        return SimpleNamespace(call=lambda **_: value)

    quotes = {0: 495 * 10**10, 1: 695 * 10**10}
    transfers = {i: (legacy_owner, legacy_owner, 0, row[4], row[4] - 10 * 10**10, 1)
                 for i, row in enumerate(rows.values())}
    gateway_contract = SimpleNamespace(functions=SimpleNamespace(
        transfers=lambda i: call(transfers[i]), quotedShares=lambda i: call(quotes[i])))
    escrow = SimpleNamespace(
        w3=SimpleNamespace(eth=SimpleNamespace(get_code=lambda *_args, **_kwargs: b"",
                                              contract=lambda **_: gateway_contract)),
        functions=SimpleNamespace(requests=lambda ref: call(rows[ref]), minP2TRSats=lambda: call(330)),
    )

    def profile(w3, address, block):
        assert w3 is escrow.w3 and block == 7
        return "quoted" if address == gateway else None

    monkeypatch.setattr(payment, "_custody_gateway_profile", profile)
    monkeypatch.setattr(payment, "artifact", lambda _: dict(abi=[]))
    assert payment._requests({"escrow": escrow}, refs[:2], wallet, 7) == {script: 1200}
    with pytest.raises(ValueError, match="BTC_CUSTODY_OUTPUT_GROUP"):
        payment._requests({"escrow": escrow}, refs, wallet, 7)

    def outputs(*amounts):
        return [dict(script=script, amount=str(amount)) for amount in amounts]

    assert payment._requests({"escrow": escrow}, refs[:2], wallet, 7, outputs=outputs(1200)) == {script: 1200}
    for malformed in (outputs(500, 700), outputs(1300)):
        with pytest.raises(ValueError, match="BTC_CUSTODY_OUTPUT_GROUP"):
            payment._requests({"escrow": escrow}, refs[:2], wallet, 7, outputs=malformed)
    payment.validate_custody_outputs(escrow, [rows[bytes.fromhex(refs[2])]], 7, outputs(500))
    row_a, row_b = [rows[bytes.fromhex(x)] for x in refs[:2]]
    with pytest.raises(ValueError, match="BTC_CUSTODY_OUTPUT_GROUP"):
        payment.validate_custody_outputs(escrow, [row_a, (*row_b[:2], other_token, *row_b[3:])], 7, outputs(1200))
    quotes[1] = 0
    with pytest.raises(ValueError, match="BTC_CUSTODY_ISSUANCE_QUOTE"):
        payment.validate_custody_outputs(escrow, [row_a, row_b], 7, outputs(1200))
