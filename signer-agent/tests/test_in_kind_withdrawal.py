from types import SimpleNamespace

import pytest

from signer_agent import confederation_dao_cli as cli


@pytest.mark.parametrize('nft', [None, '7'])
def test_s_redemption_uses_only_s_amount_and_bitcoin_terms(monkeypatch, nft):
    args = SimpleNamespace(action='withdraw', nft=nft, amount='9000000000000', script='0014' + '11' * 20,
                           miner_fee_cap_sats='30', minimum_btc_sats='500', deadline='1700000000', pool_token=None)
    client = SimpleNamespace(stable=lambda: None)
    submitted = []
    monkeypatch.setattr(cli, '_transact', lambda *values: submitted.append(values) or dict(status='SIMULATED'))
    assert cli._withdraw(client, args) == dict(status='SIMULATED')
    expected = [9000000000000, bytes.fromhex(args.script)]
    method = 'requestWithdrawal'
    if nft is not None:
        expected.append(7)
        method = 'requestWithdrawalWithNFT'
    expected.extend([30, 500, 1700000000])
    assert submitted[0][2:] == ('conf_pool', method, expected)


def test_s_redemption_rejects_a_federation_mp_parameter():
    with pytest.raises(ValueError, match='S_WITHDRAWAL_USES_CONFEDERATION_RESERVE'):
        cli._withdraw(None, SimpleNamespace(pool_token='0x' + '11' * 20))


@pytest.mark.parametrize('missing', ['minimum_btc_sats', 'deadline'])
def test_s_redemption_requires_explicit_minimum_and_deadline(missing):
    args = dict(action='withdraw', nft=None, amount='9000000000000', script='0014' + '11' * 20,
                miner_fee_cap_sats='30', minimum_btc_sats='500', deadline='1700000000', pool_token=None)
    args[missing] = None
    with pytest.raises(ValueError, match='S_WITHDRAWAL_MINIMUM_AND_DEADLINE_REQUIRED'):
        cli._withdraw(None, SimpleNamespace(**args))


@pytest.mark.parametrize(('minimum', 'nft', 'fits'), [
    (None, None, True),
    ('850', '0', True),
    ('851', '0', False),
    ('293', None, False),
])
def test_withdraw_quote_reports_headroom_from_the_confirmed_block(minimum, nft, fits):
    sender = '0x' + '11' * 20
    destination = '0014' + '22' * 20
    reads = []

    def quote(*values):
        assert values == (9500 * 10**9, bytes.fromhex(destination), 2**64 - 1, nft is not None, 0)

        def read(tx, *, block_identifier):
            reads.append(block_identifier)
            assert tx == {"from": sender}
            return 500 * 10**9, 900, 50, 294

        return SimpleNamespace(call=read)

    pool = SimpleNamespace(functions=SimpleNamespace(quoteWithdrawal=quote))
    client = SimpleNamespace(contracts={"conf_pool": pool}, block=SimpleNamespace(number=42),
                             stable=lambda: reads.append("stable"))
    a = SimpleNamespace(action='withdraw-quote', amount=str(9500 * 10**9), script=destination,
                        miner_fee_cap_sats=str(2**64 - 1), minimum_btc_sats=minimum, nft=nft,
                        pool_token=None, identity_file=None, sender=sender, send=False)
    result = cli._withdraw(client, a)
    chosen_minimum = 294 if minimum is None else int(minimum)
    assert result == dict(status="QUOTED", block_number="42", protocol_fee_s=str(500 * 10**9),
                          gross_sats="900", fee_cap_sats="50", script_minimum_sats="294",
                          minimum_btc_sats=str(chosen_minimum), max_miner_fee_share_sats=str(900 - chosen_minimum),
                          payout_at_cap_sats="850", fits=fits)
    assert reads == [42, "stable"]


def test_withdraw_quote_cannot_send():
    args = SimpleNamespace(action='withdraw-quote', amount='10000', script='0014' + '11' * 20,
                           miner_fee_cap_sats='0', minimum_btc_sats=None, pool_token=None, send=True)
    with pytest.raises(ValueError, match='S_WITHDRAWAL_QUOTE_READ_ONLY'):
        cli._withdraw(None, args)
