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
    assert funding._deposit_allocation(target,args,transaction,42)
    args[2] = [ADDRESS]
    with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_COMMITMENT'):
        funding._deposit_allocation(target,args,transaction,42)
    args[2] = [OTHER]
    for prefix in ('6a4c38', '6a4d3800', '6a4e38000000'):
        transaction['vout'][0]['scriptPubKey']['hex'] = prefix + data['data']
        with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_COMMITMENT'):
            funding._deposit_allocation(target,args,transaction,42)
    transaction['vout'][0]['scriptPubKey']['hex'] = '6a38534d5031'
    with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_COMMITMENT'):
        funding._deposit_allocation(target,args,transaction,42)


def test_unmarked_coinbase_keeps_multi_recipient_reward_policy_with_output_bound():
    target = SimpleNamespace(address=ADDRESS, w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1)))
    args = [b'a'*32, 0, [ADDRESS, OTHER], [500,500]]
    transaction = dict(vin=[{'coinbase':'01'}],vout=[dict(value=0,scriptPubKey=dict(hex='6a00'))])
    assert funding._deposit_allocation(target,args,transaction,42)
    transaction['vout'] *= 33
    with pytest.raises(ValueError, match='BTC_MINT_DEPOSIT_OUTPUTS'):
        funding._deposit_allocation(target,args,transaction,42)


def test_v3_deposit_checks_locked_terms_at_confirmed_block_before_requesting_zk(monkeypatch):
    calls = []
    sats = 1200
    gross = sats * 10**10
    txid = "01" * 31 + "ff"
    args = [bytes.fromhex(txid)[::-1], 3, [OTHER], [gross]]
    terms = [0, 0]

    def quote(*values):
        def pinned_call(block_identifier):
            calls.append((values, block_identifier))
            return terms
        return SimpleNamespace(call=pinned_call)

    reserve = SimpleNamespace(address=ADDRESS, w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1)),
        abi=[{"type": "function", "name": "directDepositQuote"}],
        functions=SimpleNamespace(directDepositQuote=quote))
    data = funding.deposit_commitment("1", ADDRESS, "3", [OTHER], [str(gross)])
    tx = dict(vin=[{"txid": "aa" * 32}], vout=[dict(value=0, scriptPubKey=dict(hex=data["script"]))])
    bitcoin = SimpleNamespace(call=lambda *unused: tx)
    a = SimpleNamespace(zk_worker_config="worker.json", txid=txid)
    from signer_agent import zk_worker
    monkeypatch.setattr(zk_worker, "request_from_consumer", lambda *unused: {"status": "READY"})

    with pytest.raises(ValueError, match="BTC_S_DEPOSIT_QUOTE_REQUIRED"):
        funding.consumer_readiness(a, {}, bitcoin, reserve, args, SimpleNamespace(number=42))
    assert calls == [((args[0], 3, OTHER, sats), 42)]
    terms[:] = [1, gross]
    with pytest.raises(ValueError, match="BTC_S_DEPOSIT_QUOTE_MISMATCH"):
        funding.consumer_readiness(a, {}, bitcoin, reserve, args, SimpleNamespace(number=42))
    terms[:] = [0, gross]
    assert funding.consumer_readiness(a, {}, bitcoin, reserve, args, SimpleNamespace(number=42)) == {"status": "READY"}


def test_v3_deposit_quote_cli_uses_exact_wire_outpoint_and_confirms_terms(monkeypatch, capsys):
    import json
    from signer_agent import confederation_dao_cli as cli

    txid = "01" * 31 + "ff"
    key = (bytes.fromhex(txid)[::-1], 2, OTHER, 1200)
    gross = key[-1] * 10**10
    reads = []
    sends = []
    terms = [0, 0]

    def lookup(*values):
        assert values == key
        def read(*, block_identifier):
            reads.append(block_identifier)
            return terms
        return SimpleNamespace(call=read)

    def transact(name, method, values, sender, identity, send, state, operation_id):
        sends.append((name, method, values, sender, send))
        return {"status": "SIMULATED"}

    reserve = SimpleNamespace(address=ADDRESS, functions=SimpleNamespace(directDepositQuote=lookup))
    client = SimpleNamespace(contracts={"mining": reserve}, block=SimpleNamespace(number=42),
                             stable=lambda: reads.append("canonical"), transact=transact)
    monkeypatch.setattr(cli, "_client", lambda a: client)
    base = ["--manifest", "launch.json", "--rpc", "https://rpc.invalid", "--txid", txid,
            "--vout", "2", "--satoshis", "1200"]
    assert cli.main(["deposit-quote", *base, "--sender", OTHER, "--minimum-s", str(gross - 1)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "SIMULATED"
    assert sends == [("mining", "lockDirectDepositQuote", [key[0], 2, 1200, gross - 1], OTHER, False)]

    lookup_args = ["deposit-quote-status", *base, "--recipient", OTHER]
    assert cli.main(lookup_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "QUOTE_NOT_FOUND"
    terms[:] = [1, gross - 1]
    assert cli.main(lookup_args) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed == dict(status="LOCKED", reserve=ADDRESS, txid=txid, vout="2", recipient=OTHER,
        satoshis="1200", block_number="42", fee_s="1", net_s=str(gross - 1))
    assert reads == [42, "canonical", 42, "canonical"]


def test_v3_direct_deposit_reviews_private_psbt_and_final_bytes_before_payment(tmp_path, monkeypatch):
    import hashlib
    from signer_agent import bitcoin_rpc, confederation_dao_cli as cli
    from signer_agent.bootstrap_withdrawal_fee import genesis
    from test_smp1_deposit import psbt_fixture, signed_raw

    sats, wallet_script = 150_000, "5120" + "78" * 32
    gross = sats * 10**10
    marker = funding.deposit_commitment("31337", ADDRESS, "0", [OTHER], [str(gross)])
    psbt, unsigned = psbt_fixture([(sats, wallet_script), (0, marker["script"]),
                                   (49_000, "0014" + "67" * 20)])
    txid = hashlib.sha256(hashlib.sha256(unsigned).digest()).digest()[::-1]
    psbt_file = tmp_path / "funding.psbt"
    raw_file = tmp_path / "funding.hex"
    for path, value in ((psbt_file, psbt), (raw_file, signed_raw(unsigned))):
        path.write_text(value + "\n")
        path.chmod(0o600)
    terms = [30, gross - 30]
    reserve = SimpleNamespace(address=ADDRESS, functions=SimpleNamespace(
        directDepositQuote=lambda *values: call(terms)))
    gateway = object()
    observed = []
    sends = []

    class Core:
        network = "regtest"
        previous = None

        def call(self, method, *args):
            if method == "getblockchaininfo":
                return {"chain": self.network}
            if method == "getblockhash":
                return genesis("regtest")[2:]
            if method == "gettxspendingprevout":
                assert args == ([{"txid": "11" * 32, "vout": 0}],)
                return [dict(args[0][0], spendingtxid=self.previous)]
            assert method == "gettxout" and args[:2] == ("11" * 32, 0)
            if args[2] and self.previous:
                return None
            return dict(value=Decimal("0.002"), scriptPubKey={"hex": "5120" + "33" * 32},
                        confirmations=2, coinbase=False)

    core = Core()
    monkeypatch.setattr(bitcoin_rpc, "BitcoinRPC", lambda *args: core)

    def read(node, method, *args):
        if node is reserve and method == "payoutScript":
            return bytes.fromhex(wallet_script)
        if node is gateway and method == "bitcoinGenesis":
            return bytes.fromhex(genesis("regtest")[2:])
        assert node is gateway and method == "isSourceFunding"
        observed.append(args[0])
        return False

    client = SimpleNamespace(contracts={"mining": reserve, "custody_transfer": gateway},
        block=SimpleNamespace(number=42), stable=lambda: None, read=read,
        config={"chain_id": "31337"})
    monkeypatch.setattr(cli, "_transact", lambda *args: sends.append(args) or {"status": "VERIFIED"})
    opts = dict(txid=txid.hex(), vout="0", satoshis=str(sats), minimum_s=str(gross - 30),
                bitcoin_rpc="http://127.0.0.1:18332", bitcoin_cookie="/private/cookie", identity_file=None)
    quote = SimpleNamespace(**opts, action="deposit-quote", sender=OTHER, recipient=None, send=True,
                            psbt_file=psbt_file, raw_tx_file=None)
    assert cli._deposit_quote(client, quote)["status"] == "VERIFIED"
    assert len(sends) == 1
    assert sends[0][-1] == [txid[::-1], 0, sats, gross - 30]
    quote.psbt_file = None
    with pytest.raises(ValueError, match="BTC_S_DEPOSIT_TRANSACTION_REQUIRED"):
        cli._deposit_quote(client, quote)

    final = SimpleNamespace(**opts, action="deposit-preflight", sender=None, recipient=OTHER, send=False,
                            psbt_file=None, raw_tx_file=raw_file)
    result = cli._deposit_quote(client, final)
    assert result["status"] == "PREFLIGHT_VERIFIED"
    assert result["bitcoin_network"] == "regtest" and result["miner_fee_sats"] == "1000"
    assert observed == [unsigned, unsigned]
    final.minimum_s = str(gross - 29)
    with pytest.raises(ValueError, match="BTC_S_DEPOSIT_MINIMUM"):
        cli._deposit_quote(client, final)
    final.minimum_s = str(gross - 30)
    core.network = "testnet4"
    with pytest.raises(ValueError, match="BITCOIN_NETWORK_MISMATCH"):
        cli._deposit_quote(client, final)
    assert len(sends) == 1 and len(observed) == 2
    core.network = "regtest"
    core.previous = "42" * 32
    final.previous_txid = "13" * 32
    with pytest.raises(ValueError, match="BTC_S_DEPOSIT_REPLACEMENT"):
        cli._deposit_quote(client, final)
    final.previous_txid = core.previous
    assert cli._deposit_quote(client, final)["previous_txid"] == core.previous
    assert observed == [unsigned, unsigned, unsigned]


@pytest.fixture
def paid_s_deposit(monkeypatch):
    import hashlib
    from signer_agent import bitcoin_rpc, confederation_dao_cli as cli, custody_network
    from signer_agent.bitcoin_network import PUBLIC_NETWORKS
    from signer_agent.bootstrap_withdrawal_fee import genesis
    from test_smp1_deposit import psbt_fixture, signed_raw

    sats, script, block = 150_000, "5120" + "78" * 32, "27" * 32
    gross = sats * 10**10
    marker = funding.deposit_commitment("31337", ADDRESS, "0", [OTHER], [str(gross)])["script"]
    _, unsigned = psbt_fixture([(sats, script), (0, marker)])
    txid = hashlib.sha256(hashlib.sha256(unsigned).digest()).digest()[::-1]
    txid_hex = txid.hex()
    at = 42
    graph = {kind: SimpleNamespace(address=Web3.to_checksum_address(f"0x{index:040x}"))
             for index, kind in enumerate(("BitcoinWithdrawalEscrowV2", "BitcoinDepositRegistry", "BitcoinBatchRegistry"), 10)}
    escrow, deposits, batches = graph.values()
    reserve, gateway, pool = SimpleNamespace(address=ADDRESS), object(), SimpleNamespace(address=ZERO)
    spv = OTHER
    records = dict(network="regtest", spv_network="regtest", source_funding=False, depth=6,
                   consumed=set(), wallet_outputs=set(), custody_transactions=set(), reservations=set(),
                   paid=dict(confirmations=6, blockhash=block, hex=signed_raw(unsigned)), active_blocks=[block] * 2,
                   utxo=dict(confirmations=6, coinbase=False, scriptPubKey=dict(hex=script), value=Decimal("0.0015")),
                   proof_matches=[txid_hex], pool_registered=True)
    requests = []
    sends = []

    def read(node, method, *args):
        if node is reserve:
            return {"payoutScript": bytes.fromhex(script), "escrow": escrow.address,
                    "deposits": deposits.address}[method]
        if node is gateway:
            if method == "bitcoinGenesis":
                return bytes.fromhex(genesis(records["network"])[2:])
            assert method == "isSourceFunding" and args == (unsigned,)
            return records["source_funding"]
        if node is pool:
            assert method == "withdrawalEscrow"
            return escrow.address
        if node is escrow:
            if method == "localCustodyPools":
                assert args == (pool.address,)
            return {"batches": batches.address, "spv": spv, "localCustodyPools": records["pool_registered"],
                    "depositRegistry": deposits.address}[method]
        if node is deposits:
            if method == "poolScripts":
                assert args == (reserve.address,)
            flags = {"consumed": "consumed", "walletOutput": "wallet_outputs", "custodyTransactions": "custody_transactions"}
            if method in flags:
                return args[0] in records[flags[method]]
            return {"spendRecorder": escrow.address, "spv": spv, "poolScripts": keccak(bytes.fromhex(script)),
                    "depth": records["depth"]}[method]
        assert node is batches
        if method == "withdrawalEscrow":
            return escrow.address
        assert method == "reservations"
        return b"\x01" * 32 if args[0] in records["reservations"] else bytes(32)

    client = SimpleNamespace(w3=object(), contracts={"mining": reserve, "custody_transfer": gateway, "conf_pool": pool},
                             block=SimpleNamespace(number=at), stable=lambda: requests.append("stable"), read=read,
                             config={"chain_id": "31337"})

    def load(w3, target, kind, block_number):
        assert w3 is client.w3 and target == graph[kind].address and block_number == at
        return graph[kind]

    def network(w3, endpoint, block_number):
        assert w3 is client.w3 and endpoint == spv and block_number == at
        return records["spv_network"]

    class Core:
        def call(self, method, *args):
            requests.append((method, args))
            if method == "getblockchaininfo":
                network = records["network"]
                chain = "regtest" if network == "regtest" else PUBLIC_NETWORKS[network][0]
                return dict(chain=chain, initialblockdownload=False, blocks=106, headers=106)
            responses = {
                "getblockhash": {(0,): genesis(records["network"])[2:]},
                "getrawtransaction": {(txid_hex, True): records["paid"]},
                "getblockheader": {(block,): dict(height=101)},
                "gettxoutproof": {((txid_hex,), block): "aa"},
                "verifytxoutproof": {("aa",): records["proof_matches"]},
                "gettxout": {(txid_hex, 0, True): records["utxo"]},
            }
            if method == "getblockhash" and args == (101,):
                return records["active_blocks"].pop(0)
            if method == "gettxoutproof":
                args = (tuple(args[0]), args[1])
            return responses[method][args]

    monkeypatch.setattr(bitcoin_rpc, "BitcoinRPC", lambda *args: Core())
    monkeypatch.setattr(cli, "_client", lambda a: client)
    monkeypatch.setattr(cli, "_contract", load)
    monkeypatch.setattr(custody_network, "spv_network", network)
    monkeypatch.setattr(cli, "_transact", lambda *args: sends.append(args) or {"status": "SIMULATED"})
    argv = ["deposit-recovery-quote", "--rpc", "http://127.0.0.1:8545", "--factory", ADDRESS,
            "--confederation-id", "c1", "--chain-id", "31337", "--sender", OTHER, "--txid", txid_hex, "--vout", "0",
            "--satoshis", str(sats), "--minimum-s", str(gross - 30), "--bitcoin-rpc", "http://127.0.0.1:18332",
            "--bitcoin-cookie", "/private/cookie"]
    return SimpleNamespace(cli=cli, records=records, argv=argv, requests=requests, sends=sends,
                           txid=txid, sats=sats, gross=gross, block=block)


@pytest.mark.parametrize("network", ["regtest", "testnet4", "mainnet"])
def test_v3_direct_recovery_quotes_only_the_confirmed_recipient_output(paid_s_deposit, network, capsys):
    import json

    d = paid_s_deposit
    d.records.update(network=network, spv_network=network)
    assert d.cli.main(d.argv) == 0
    assert json.loads(capsys.readouterr().out) == dict(status="SIMULATED", bitcoin_deposit=dict(
        network=network, block_hash=d.block, confirmations="6"))
    assert len(d.sends) == 1
    assert d.sends[0][2:] == ("mining", "lockDirectDepositQuote", [d.txid[::-1], 0, d.sats, d.gross - 30])
    assert [request for request in d.requests if request[0] == "gettxout"] == [
        ("gettxout", (d.txid.hex(), 0, True))]
    assert d.requests[-2:] == [("getblockhash", (101,)), "stable"]


@pytest.mark.parametrize(("case", "expected"), [
    ("unconfirmed", "PAYMENT_NOT_CONFIRMED"), ("immature", "BTC_MINT_PROOF_OR_ALLOCATION"),
    ("missing-proof", "INVALID_PAYMENT_PROOF"), ("reorg", "BITCOIN_PAYMENT_REORG"),
    ("spent", "BTC_MINT_OUTPOINT_UNAVAILABLE"), ("changed-value", "BTC_MINT_OUTPOINT_UNAVAILABLE"),
    ("consumed", "BTC_MINT_OUTPOINT_UNAVAILABLE"), ("wallet-change", "BTC_MINT_OUTPOINT_UNAVAILABLE"),
    ("reserved", "BTC_MINT_OUTPOINT_UNAVAILABLE"), ("source-gateway", "BTC_S_DEPOSIT_CUSTODY_FLOW_REQUIRED"),
    ("custody-transaction", "BTC_S_DEPOSIT_CUSTODY_FLOW_REQUIRED"),
    ("custody-input", "BTC_S_DEPOSIT_CUSTODY_FLOW_REQUIRED"), ("spv-network", "BITCOIN_CUSTODY_NETWORK_MISMATCH"),
    ("pool-binding", "CONFEDERATION_DAO_BINDING"), ("caller", "BTC_MINT_DEPOSIT_COMMITMENT"),
    ("recipient-override", "BTC_S_DEPOSIT_QUOTE_RECIPIENT_IS_SENDER"), ("minimum", "BTC_S_DEPOSIT_MINIMUM"),
    ("local-raw", "BTC_S_DEPOSIT_TRANSACTION_SHAPE"),
])
def test_v3_direct_recovery_checks_chain_and_scope_before_requesting_quote(paid_s_deposit, case, expected):
    d = paid_s_deposit
    r = d.records
    wire_key = keccak(encode(["bytes32", "uint32"], [d.txid[::-1], 0]))
    display_key = keccak(encode(["bytes32", "uint32"], [d.txid, 0]))
    cases = {
        "unconfirmed": lambda: r["paid"].update(confirmations=0), "immature": lambda: r.update(depth=7),
        "missing-proof": lambda: r.update(proof_matches=[]), "reorg": lambda: r["active_blocks"].__setitem__(1, "69" * 32),
        "spent": lambda: r.update(utxo=None), "changed-value": lambda: r["utxo"].update(value=0),
        "consumed": lambda: r["consumed"].add(wire_key), "wallet-change": lambda: r["wallet_outputs"].add(wire_key),
        "reserved": lambda: r["reservations"].add(display_key), "source-gateway": lambda: r.update(source_funding=True),
        "custody-transaction": lambda: r["custody_transactions"].add(d.txid[::-1]),
        "custody-input": lambda: r["consumed"].add(keccak(encode(["bytes32", "uint32"], [b"\x11" * 32, 0]))),
        "spv-network": lambda: r.update(spv_network="mainnet"), "pool-binding": lambda: r.update(pool_registered=False),
        "caller": lambda: d.argv.extend(["--sender", ADDRESS]), "recipient-override": lambda: d.argv.extend(["--recipient", ADDRESS]),
        "minimum": lambda: d.argv.extend(["--minimum-s", str(d.gross + 1)]),
        "local-raw": lambda: d.argv.extend(["--raw-tx-file", "/private/local-raw"]),
    }
    cases[case]()
    with pytest.raises(ValueError, match=expected):
        d.cli.main(d.argv)
    assert not d.sends


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

