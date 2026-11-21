from types import SimpleNamespace

import pytest

from signer_agent import confederation_dao_cli as cli


@pytest.mark.parametrize('nft', [None, '7'])
def test_s_redemption_uses_only_s_amount_and_bitcoin_terms(monkeypatch, nft):
    args = SimpleNamespace(nft=nft, amount='10000', script='0014' + '11' * 20,
                           miner_fee_cap_sats='30', pool_token=None)
    client = SimpleNamespace(stable=lambda: None)
    submitted = []
    monkeypatch.setattr(cli, '_transact', lambda *values: submitted.append(values) or dict(status='SIMULATED'))
    assert cli._withdraw(client, args) == dict(status='SIMULATED')
    expected = [10000, bytes.fromhex(args.script)]
    method = 'requestWithdrawal'
    if nft is not None:
        expected.append(7)
        method = 'requestWithdrawalWithNFT'
    expected.append(30)
    assert submitted[0][2:] == ('conf_pool', method, expected)


def test_s_redemption_rejects_a_federation_mp_parameter():
    with pytest.raises(ValueError, match='S_WITHDRAWAL_USES_CONFEDERATION_RESERVE'):
        cli._withdraw(None, SimpleNamespace(pool_token='0x' + '11' * 20))
