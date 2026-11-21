from types import SimpleNamespace
from decimal import Decimal
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
import pytest

from signer_agent import bitcoin_funding as funding
from signer_agent.bootstrap_policy import effective_fee_policy


ADDRESS = Web3.to_checksum_address('0x' + '11' * 20)
OTHER = Web3.to_checksum_address('0x' + '22' * 20)
ZERO = '0x' + '00' * 20


def call(value):
    return SimpleNamespace(call=lambda **kw: value)


def test_deployment_exemption_never_silently_zeroes_enrolled_user_fees():
    value = effective_fee_policy(dict(integration_fee_exemption=True, wrap_fee_bps='17', unwrap_fee_bps='23'))
    assert value == dict(deployment_exempt=True, deployment_fee='0', wrap_bps='17', unwrap_bps='23',
                         user_fees_exempt=False, exemption_scope='DEPLOY_ONLY')
    assert effective_fee_policy(dict(integration_fee_exemption=True, wrap_fee_bps='0', unwrap_fee_bps='0'))['user_fees_exempt']
    assert effective_fee_policy(dict(integration_fee_exemption=False, deployment_fee={'amount':'500'}, wrap_fee_bps='0', unwrap_fee_bps='0'))['deployment_fee'] == '500'


@pytest.mark.parametrize('amount', ['0', '-1', '1.0', '01', 100, str(funding.MAX_MONEY * 10**10 + 1)])
def test_mint_rejects_ambiguous_or_out_of_supply_amounts(amount):
    with pytest.raises(ValueError, match='BTC_MINT_AMOUNT'):
        funding._distribution(SimpleNamespace(recipients=[ADDRESS], amounts=[amount]))


def test_mint_supports_full_mp_units_without_float_or_satoshi_rounding():
    recipients, amounts = funding._distribution(SimpleNamespace(recipients=[ADDRESS], amounts=[str(funding.MAX_MONEY * 10**10)]))
    assert recipients == [ADDRESS] and amounts == [funding.MAX_MONEY * 10**10]
    args = funding._mint_args(SimpleNamespace(txid='01' * 31 + 'ff', vout='2', recipients=[ADDRESS], amounts=['123']))
    assert args[0] == bytes.fromhex('ff' + '01' * 31) and args[1:] == [2, [ADDRESS], [123]]


def test_public_deposit_commitment_binds_every_allocation_dimension(capsys):
    original = funding.deposit_commitment('1', ADDRESS, '0', [OTHER], ['10000000000000000'])
    variants = [funding.deposit_commitment('11155111', ADDRESS, '0', [OTHER], ['10000000000000000']),
                funding.deposit_commitment('1', OTHER, '0', [OTHER], ['10000000000000000']),
                funding.deposit_commitment('1', ADDRESS, '1', [OTHER], ['10000000000000000']),
                funding.deposit_commitment('1', ADDRESS, '0', [ADDRESS], ['10000000000000000']),
                funding.deposit_commitment('1', ADDRESS, '0', [OTHER], ['10000000000000001'])]
    assert all(item['data'] != original['data'] for item in variants)
    assert original['script'] == '6a38' + original['data']
    assert len(bytes.fromhex(original['data'])) == 56
    assert bytes.fromhex(original['data'][:8]) == b'SMP1'
    assert original['data'][8:48] == OTHER[2:].lower()
    assert original['data'][48:] == original['allocation_hash']
    from signer_agent.bitcoin_cli import main
    assert main(['deposit-commitment', '--chain-id', '1', '--mining-pool', ADDRESS, '--vout', '0',
                 '--recipient', OTHER, '--amount', '10000000000000000']) == 0
    import json
    assert json.loads(capsys.readouterr().out) == original


def test_ordinary_deposit_has_one_explicit_recipient_while_reward_distribution_can_have_many():
    with pytest.raises(ValueError, match='BTC_MINT_SINGLE_RECIPIENT'):
        funding.deposit_commitment('1', ADDRESS, '0', [ADDRESS, OTHER], ['500', '500'])
    assert funding._distribution(SimpleNamespace(recipients=[ADDRESS, OTHER], amounts=['500', '500'])) == ([ADDRESS, OTHER], [500, 500])


def test_marked_coinbase_allocation_is_checked_instead_of_silently_ignored():
    target = SimpleNamespace(address=ADDRESS, w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1)))
    args = [b'a'*32, 0, [OTHER], [1000]]
    data = funding.deposit_commitment('1', ADDRESS, '0', [OTHER], ['1000'])
    transaction = dict(vin=[{'coinbase':'01'}],vout=[dict(value=0,scriptPubKey=dict(hex=data['script']))])
    assert funding._deposit_allocation(target,args,transaction)
    args[2] = [ADDRESS]
    with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_COMMITMENT'):
        funding._deposit_allocation(target,args,transaction)
    args[2] = [OTHER]
    for prefix in ('6a4c38', '6a4d3800', '6a4e38000000'):
        transaction['vout'][0]['scriptPubKey']['hex'] = prefix + data['data']
        with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_COMMITMENT'):
            funding._deposit_allocation(target,args,transaction)
    transaction['vout'][0]['scriptPubKey']['hex'] = '6a38534d5031'
    with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_COMMITMENT'):
        funding._deposit_allocation(target,args,transaction)


def test_unmarked_coinbase_keeps_multi_recipient_reward_policy_with_output_bound():
    target = SimpleNamespace(address=ADDRESS, w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1)))
    args = [b'a'*32, 0, [ADDRESS, OTHER], [500,500]]
    transaction = dict(vin=[{'coinbase':'01'}],vout=[dict(value=0,scriptPubKey=dict(hex='6a00'))])
    assert funding._deposit_allocation(target,args,transaction)
    transaction['vout'] *= 33
    with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_OUTPUTS'):
        funding._deposit_allocation(target,args,transaction)


@pytest.mark.parametrize('recipients,amounts', [([], []), ([ZERO], ['1']), ([ADDRESS], []), ([ADDRESS]*65, ['1']*65)])
def test_mint_rejects_empty_zero_or_unbounded_recipient_lists(recipients, amounts):
    with pytest.raises(ValueError, match='BTC_MINT_'):
        funding._distribution(SimpleNamespace(recipients=recipients, amounts=amounts))


def test_mint_retry_only_reports_exact_executed_allocation():
    args = [bytes.fromhex('aa'*32), 3, [ADDRESS], [5000 * 10**10]]
    digest = keccak(encode(['address[]', 'uint256[]'], args[2:]))
    mining = SimpleNamespace(functions=SimpleNamespace(mintedAllocations=lambda _: call(digest)))
    assert funding._mint_complete(mining, args, SimpleNamespace(number=10))
    args[2] = [OTHER]
    with pytest.raises(ValueError, match='BTC_MINT_ALLOCATION_MISMATCH'):
        funding._mint_complete(mining, args, SimpleNamespace(number=10))


@pytest.mark.parametrize("configured_depth", [6, 100, 120])
@pytest.mark.parametrize("coinbase", [False, True])
def test_actual_mint_preflight_matches_contract_maturity(configured_depth, coinbase, monkeypatch):
    wallet_script = "5120" + "11" * 32
    txid = "aa" * 32
    args = [bytes.fromhex(txid), 0, [OTHER], [1000 * 10**10]]
    target = SimpleNamespace(address=ADDRESS, w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1)),
        functions=SimpleNamespace(payoutScript=lambda:call(bytes.fromhex(wallet_script))))
    transaction = dict(vin=[dict(coinbase="01") if coinbase else dict(txid="bb" * 32, vout=0)],
        vout=[dict(n=0, value=Decimal("0.00001000"), scriptPubKey=dict(hex=wallet_script))])
    if not coinbase:
        commitment = funding.deposit_commitment(1, ADDRESS, 0, [OTHER], [str(1000 * 10**10)])
        transaction["vout"].append(dict(n=1, value=0, scriptPubKey=dict(hex=commitment["script"])))
    required = 100 if coinbase else configured_depth
    proof = dict(raw="00", block_hash="cc" * 32, siblings=[], index="0", confirmations=str(required))
    monkeypatch.setattr(funding, "inclusion", lambda *_: proof)
    bitcoin = SimpleNamespace(call=lambda method, *args: transaction if method == "getrawtransaction" else
        dict(value=Decimal("0.00001000"), scriptPubKey=dict(hex=wallet_script)))
    status = [True, required]
    contracts = dict(deposits=SimpleNamespace(functions=SimpleNamespace(depth=lambda:call(configured_depth),
        consumed=lambda _:call(False), walletOutput=lambda _:call(False))),
        relay=SimpleNamespace(functions=SimpleNamespace(getBlockStatus=lambda _:call(status),
            checkTxInclusion=lambda *_:call(True))),
        batches=SimpleNamespace(functions=SimpleNamespace(reservations=lambda _:call(bytes(32)))))
    result = funding._mint_proof(SimpleNamespace(txid=txid), bitcoin, contracts, target, args, SimpleNamespace(number=10))
    assert result[-1] == args[3]
    status[1] -= 1
    with pytest.raises(ValueError, match="BTC_MINT_PROOF_OR_ALLOCATION"):
        funding._mint_proof(SimpleNamespace(txid=txid), bitcoin, contracts, target, args, SimpleNamespace(number=10))


def test_treasury_retry_checks_destination_owner_amount_and_terminal_cancellation():
    args = [17, ADDRESS, 400, bytes.fromhex('0014'+'dd'*20)]
    request = [ZERO, 17, ZERO, ADDRESS, 400*10**10, args[3], bytes(32), 1]
    target = SimpleNamespace(address=OTHER, functions=SimpleNamespace(
        requestKey=lambda *_: call(b'r'*32), requests=lambda _:call(request)))
    assert funding._treasury_complete(target, args, SimpleNamespace(number=10)) == (True, (b'r'*32).hex())
    request[4] = 401*10**10
    with pytest.raises(ValueError, match='BTC_TREASURY_REQUEST_MISMATCH'):
        funding._treasury_complete(target, args, SimpleNamespace(number=10))
    request[4], request[-1] = 400*10**10, 4
    with pytest.raises(ValueError, match='BTC_TREASURY_REQUEST_CANCELLED'):
        funding._treasury_complete(target, args, SimpleNamespace(number=10))


def test_treasury_obligation_does_not_reduce_mp_backing():
    from signer_agent.bitcoin_onchain_payment import _principal
    rows = {bytes.fromhex('aa'*32): [ZERO, 1, ADDRESS, ADDRESS, 1000*10**10],
            bytes.fromhex('bb'*32): [ZERO, 2, ZERO, ADDRESS, 400*10**10]}
    contracts = dict(escrow=SimpleNamespace(functions=SimpleNamespace(requests=lambda ref:call(rows[ref]))))
    assert _principal(contracts, ['aa'*32, 'bb'*32], 123) == 1000
@pytest.mark.parametrize("arguments,flag", [
    (["btc-treasury-propose", "--request-id", "1", "--satoshis", "400", "--destination-script", "00", "--deadline", "100", "--amount", "123"], "--amount"),
    (["btc-mint-propose", "--txid", "00", "--vout", "0", "--recipient", "alice", "--amount", "123", "--deadline", "100", "--satoshis", "400"], "--satoshis"),
])
def test_funding_cli_rejects_ignored_financial_flags(arguments, flag, capsys):
    from signer_agent.participant_cli import main
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2
    assert "unrecognized arguments: " + flag in capsys.readouterr().err


@pytest.mark.parametrize("actor,allowed", [("legacy_operator", True), ("custodian", True),
    ("outsider", False), ("not_delegated", False), ("no_identity", False)])
def test_treasury_proposal_uses_delegated_publisher_without_granting_execution(monkeypatch, actor, allowed):
    """Real treasury dispatch reaches publication, never authorization or spend."""
    from contextlib import nullcontext
    from signer_agent import bitcoin_onchain
    address = lambda n: Web3.to_checksum_address(f"0x{n:040x}")
    operator, custodian, outsider, escrow_address, board_address, coordinator = [address(n) for n in range(1, 7)]
    sender = operator if actor == "legacy_operator" else outsider if actor == "outsider" else custodian
    messages, publisher_reads, private_boundary = [], [], []
    def delegated(who):
        assert actor != "legacy_operator", "legacy operator should not require a delegated-publisher ABI"
        publisher_reads.append(who)
        return call(actor == "custodian" and who == custodian)
    transport = SimpleNamespace(address=board_address, functions=SimpleNamespace(
        operator=lambda:call(operator), canPublishIntent=delegated, intents=lambda _:call(b"")))
    digest = b"d" * 32
    escrow = SimpleNamespace(address=escrow_address, functions=SimpleNamespace(
        minP2WPKHSats=lambda:call(294), minP2TRSats=lambda:call(330),
        getTreasuryOperationHash=lambda *args:call(digest)))
    contracts = dict(escrow=escrow, coordinator=SimpleNamespace(address=coordinator))
    registry = SimpleNamespace(account=None if actor == "no_identity" else SimpleNamespace(address=sender),
        w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda _:SimpleNamespace(timestamp=100))))
    arguments = [17, sender, 400, bytes.fromhex("0014" + "aa" * 20)]
    intent = dict(chain_id="31337", coordinator=coordinator, role="conf_ceremony", target=escrow_address,
        artifact="BitcoinWithdrawalEscrow", hash_method="getTreasuryOperationHash",
        hash_args=funding.Bootstrap.wire(arguments), operation_hash=digest.hex(), session="8")
    cached = dict(session="8", intent=intent)
    def exclusive():
        private_boundary.append(True)
        return nullcontext()
    state = SimpleNamespace(exclusive=exclusive, get=lambda _:cached)
    graph = dict(evm_dkg_id=(b"e" * 32).hex())
    monkeypatch.setattr(funding, "_graph", lambda *args:(graph, contracts))
    monkeypatch.setattr(funding, "confirmed_block", lambda *args:SimpleNamespace(number=12))
    monkeypatch.setattr(funding, "_treasury_complete", lambda *args:(False, None))
    monkeypatch.setattr(funding, "_bitcoin", lambda *args:object())
    monkeypatch.setattr(funding, "check_bitcoin_graph", lambda *args:None)
    monkeypatch.setattr(funding, "_auth_board", lambda *args:(transport, "conf_ceremony"))
    def checked(registry, contracts, graph, actual, expected):
        assert actual == cached and dict(expected, session="8") == intent
        return 8
    monkeypatch.setattr(funding, "_check_cached", checked)
    monkeypatch.setattr(bitcoin_onchain, "snapshot_check", lambda *args:None)
    def send(registry, target, kind, method, values, **kwargs):
        assert (target, kind, method) == (board_address, "DKGOnchainBoard", "publishIntent")
        messages.append(values)
    monkeypatch.setattr(bitcoin_onchain, "send", send)
    args = SimpleNamespace(action="btc-treasury-propose", request_id="17", satoshis="400",
        destination_script="0014" + "aa" * 20, deadline="1000")
    def run():
        return funding.dispatch(args, SimpleNamespace(chain_id=31337, context=b"c" * 32),
            dict(result=dict(group="02" + "44" * 32)), registry, state, None)
    if allowed:
        result = run()
        assert result["status"] == "AWAITING_PARTICIPANTS"
        assert result["operation"] == "treasury" and result["intent"] == intent
        assert len(messages) == 1 and messages[0][0] == 8
        assert publisher_reads == ([] if actor == "legacy_operator" else [sender])
    else:
        error = "LOCAL_EVM_IDENTITY_REQUIRED" if actor == "no_identity" else "BTC_PROPOSAL_REQUIRES_BOARD_OPERATOR"
        with pytest.raises(ValueError, match=error): run()
        assert not messages and not private_boundary

