"""Fee inversion and owner workflow recovery, with public EVM/Core fixtures."""
from copy import deepcopy
from types import SimpleNamespace
import pytest

from signer_agent import crypto
from signer_agent.withdrawal_user import Q, split, fee_function, Withdrawal, ZERO
from signer_agent.btc_fee_reserve import digest
from signer_agent.operations import Journal
from web3.datastructures import AttributeDict
from hexbytes import HexBytes
from test_address_mint import Functions, address


@pytest.mark.parametrize("asset", ["MP", "S"])
@pytest.mark.parametrize("calculation", ["payout", "total"])
@pytest.mark.parametrize("rate,minimum,discount", [(0, 0, 0), (23, 0, 0), (100, 123456, 0), (5000, 0, 5000), (10000, 0, 10000)])
def test_two_modes_preserve_net_payout_and_exact_total_with_fee_only_tranche(asset, calculation, rate, minimum, discount):
    fee = fee_function([1, 0, minimum, rate], discount)
    cap = 1000
    result = split(calculation, asset, asset, cap, fee, 100000 * Q,
                   payout=10000 if calculation == "payout" else None,
                   total=20000 * Q + 17 if calculation == "total" else None)
    main, service, rounding, principal = [int(result[k]) for k in ("main_units", "service_fee_units", "rounding_units", "principal_sats")]
    assert main == service + rounding + principal * Q
    assert int(result["total_lock_units"]) == main + cap * Q
    assert service == (fee(main) if asset == "S" else 0)
    assert 0 <= rounding < Q
    if calculation == "payout":
        assert principal == 10000
        if asset == "S":
            assert main - 1 - fee(main - 1) < principal * Q
    elif asset == "MP":
        assert result["unspent_total_units"] == "17"
    else:
        assert int(result["total_lock_units"]) == 20000 * Q + 17


def test_btc_budget_does_not_escrow_token_fee_tranche_or_subtract_principal():
    result = split("total", "MP", "BTC", 10000, lambda _: 0, 5000 * Q + 33, total=5000 * Q + 33)
    assert result["principal_sats"] == "5000"
    assert result["fee_units"] == "0" and result["unspent_total_units"] == "33"


@pytest.mark.parametrize("args", [
    dict(calculation="total", asset="MP", fee_mode="S", cap=1, total=100 * Q),
    dict(calculation="total", asset="MP", fee_mode="MP", cap=1000, total=100 * Q),
    dict(calculation="payout", asset="S", fee_mode="S", cap=1000, payout=1000000),
    dict(calculation="payout", asset="MP", fee_mode="MP", cap=1000, payout=1, total=100 * Q),
])
def test_invalid_or_underfunded_split_never_shrinks_the_requested_payout(args):
    with pytest.raises(ValueError):
        split(**args, fee=lambda amount: amount // 10, available=100000 * Q)


def test_full_fee_with_no_discount_cannot_produce_an_s_payout():
    with pytest.raises(ValueError):
        split("payout", "S", "S", 1000, fee_function([0, 10000, 0, 0]), 100000 * Q, payout=100)


@pytest.mark.parametrize("network", ["regtest", "mainnet", "testnet4"])
def test_native_destination_parser_roundtrips_and_rejects_other_network(network):
    script = "5120" + "11" * 32
    address = crypto.call("bitcoin_script_address_v1", script=script, network=network)["address"]
    assert crypto.call("bitcoin_address_script_v1", address=address, network=network) == dict(address=address, script=script)
    with pytest.raises(RuntimeError):
        crypto.call("bitcoin_address_script_v1", address=address, network="mainnet" if network != "mainnet" else "regtest")


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    from signer_agent import withdrawal_fee_cli
    w = Withdrawal.__new__(Withdrawal)
    owner, token, pool = address(1), address(2), address(3)
    block = AttributeDict(dict(number=7, timestamp=1000, hash=HexBytes("77" * 32)))
    storage = dict(request=[ZERO, 0, token, owner, 10000 * Q, bytes.fromhex("5120" + "11" * 32), bytes(32), 1],
                   budget=[owner, token, ZERO, 0, 0, 0, 0, 1, 0, 5000, bytes(32), bytes(32), b"", False], cg=1, dg=1)
    def node(where, values):
        return SimpleNamespace(address=where, functions=Functions(values))
    escrow = node(address(4), dict(getRequest=lambda _: storage["request"]))
    escrow.events = SimpleNamespace(Escrowed=lambda: SimpleNamespace(process_receipt=lambda r, **kw:r.events))
    credits = node(address(5), dict(getBudget=lambda _: storage["budget"]))
    ctx = dict(block=block, escrow=escrow, pool=node(pool, dict(mpToken=token)), fee=node(address(6), {}),
               registry=node(address(7), dict(custodyGeneration=lambda:storage['cg'], derivationGeneration=lambda:storage['dg'])),
               withdrawal_fees=dict(credits=credits))
    journal = Journal(tmp_path / "user.sqlite")
    w.i = SimpleNamespace(c=dict(chain_id="31337", bitcoin_network="regtest"), journal=journal,
        context=lambda:ctx, fence=lambda _:None,
        w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda _:block,
            contract=lambda **_:node(token, dict(allowance=lambda *_:0)))))
    w.identity = lambda:owner
    w.destination = lambda a:dict(address=a, script="5120" + "11" * 32)
    cost = split("payout", "MP", "MP", 1000, lambda _:0, 20000 * Q, payout=10000)
    options = dict(calculation="payout", asset="MP", fee_mode="MP", payout_sats="10000", total_units=None,
        fee_cap_sats="1000", destination="bitcoin-address", pool=None, nft=None, deadline="5000", max_rate="10", refund_fee_cap="0", refund_address=None)
    quote = dict(version="1", operation_id="11" * 32, owner=owner, chain_id="31337", bitcoin_network="regtest",
        graph=w.graph(ctx), options=options, cost=cost, custody_generation="1", derivation_generation="1",
        created_at="1000", expires_at="1300", anchor=dict(number="7", hash=block.hash.hex()), fee_estimate={})
    storage['request'][1]=int(quote['operation_id'],16)
    w.evaluate = lambda *a:(dict(cost), {}, None)
    monkeypatch.setattr(withdrawal_fee_cli, "status", lambda *_:dict(revision=str(storage['budget'][4])))
    class Sender:
        def __init__(self):
            self.sent = {};self.consents=[];self.fail_consent=False
        def send(self, key, kind, target, method, args, guard):
            if key not in self.sent:
                guard()
                self.sent[key] = AttributeDict(dict(blockNumber=7, blockHash=block.hash,
                    events=[dict(address=escrow.address, args=dict(id=b'r'*32))]))
            return self.sent[key]
        def call(self, method, args):
            if self.fail_consent:
                raise ValueError("fixture crash before consent")
            self.consents.append((method, args))
            c=args[0]
            storage['budget'][3:10]=[int(c[2]),1,int(c[5]),int(c[4]),int(c[6]),int(c[7]),int(c[8])]
    w.owner=Sender()
    return w, quote, storage, ctx


def test_primary_then_exact_fee_consent_resumes_without_second_primary_or_debit(workflow):
    w,q,s,_=workflow
    w.owner.fail_consent=True
    with pytest.raises(ValueError, match='fixture crash'):
        w.submit(q,digest(q))
    assert w.i.journal.get('withdrawal-user:'+q['operation_id'])['status']=='PRIMARY_CONFIRMED'
    assert len(w.owner.sent)==2 and not w.owner.consents
    assert w.status(q['operation_id'])['status']=='WITHDRAWAL_FEE_CONSENT_REQUIRED'
    w.owner.fail_consent=False
    result=w.submit(q,digest(q))
    assert result['request']=='72'*32 and result['cost']['principal_sats']=='10000'
    assert len(w.owner.sent)==2 and len(w.owner.consents)==1
    consent=w.owner.consents[0][1][0]
    assert consent[2:6]==['2',ZERO,'1000',str(1000*Q)]
    assert w.submit(q,digest(q))==result
    assert len(w.owner.consents)==1


def test_lost_primary_receipt_journal_write_reuses_exact_original_send(workflow,monkeypatch):
    w,q,s,_=workflow
    original=Journal.update
    def crash(journal,key,status,result):
        if status=='PRIMARY_CONFIRMED':raise SystemExit('receipt write crash')
        return original(journal,key,status,result)
    monkeypatch.setattr(Journal,'update',crash)
    with pytest.raises(SystemExit):w.submit(q,digest(q))
    assert len(w.owner.sent)==2
    monkeypatch.setattr(Journal,'update',original)
    assert w.submit(q,digest(q))['status']=='WITHDRAWAL_REQUESTED'
    assert len(w.owner.sent)==2 and len(w.owner.consents)==1


@pytest.mark.parametrize('change',['expiry','generation','principal','hash'])
def test_partial_primary_is_retained_when_resume_fails(workflow,change):
    w,q,s,ctx=workflow
    w.owner.fail_consent=True
    with pytest.raises(ValueError):w.submit(q,digest(q))
    w.owner.fail_consent=False
    accepted=digest(q)
    if change=='expiry':ctx['block']=AttributeDict(dict(ctx['block'],timestamp=6000))
    elif change=='generation':s['cg']=2
    elif change=='principal':s['request'][4]-=Q
    else:accepted='00'*32
    with pytest.raises(ValueError):w.submit(q,accepted)
    assert len(w.owner.sent)==2 and not w.owner.consents
    assert w.i.journal.get('withdrawal-user:'+q['operation_id'])['result']['request']=='72'*32


def test_expired_estimate_does_not_block_explicit_resume_of_existing_principal(workflow):
    w,q,s,ctx=workflow
    w.owner.fail_consent=True
    with pytest.raises(ValueError):w.submit(q,digest(q))
    ctx['block']=AttributeDict(dict(ctx['block'],timestamp=1400))
    w.owner.fail_consent=False
    assert w.submit(q,digest(q))['status']=='WITHDRAWAL_REQUESTED'
    assert len(w.owner.sent)==2


def test_exact_version_approval_checks_terms_and_preserves_primary(workflow,monkeypatch):
    from signer_agent import withdrawal_fee_client
    w,q,_,_=workflow
    w.submit(q,digest(q))
    exact=dict(request='72'*32,principal_sats='10000',terms_hash='aa'*32,revision='1')
    monkeypatch.setattr(withdrawal_fee_client,'quote',lambda *_:exact)
    assert w.version_quote(q['operation_id'],b'b'*32)['quote']==exact
    with pytest.raises(ValueError):w.approve_version(q['operation_id'],b'b'*32,'bb'*32)
    calls=[]
    w.owner.call=lambda method,args:calls.append((method,args))
    w.approve_version(q['operation_id'],b'b'*32,'aa'*32)
    assert calls==[('approveVersion',['0x'+'62'*32,'0x'+'72'*32,'1','0x'+'aa'*32])]
    exact['principal_sats']='9999'
    with pytest.raises(ValueError):w.version_quote(q['operation_id'],b'b'*32)


def test_high_level_cli_generates_file_and_requires_its_exact_acceptance(tmp_path,monkeypatch,capsys):
    from signer_agent import withdrawal_user_cli as cli, bitcoin_cli
    body=dict(status='WITHDRAWAL_QUOTE',quote=dict(terms='public'),quote_hash='ab'*32)
    calls=[]
    w=SimpleNamespace(prepare=lambda options,ttl:calls.append(options) or body,
        submit=lambda quote,accepted:dict(status='WITHDRAWAL_REQUESTED',accepted=accepted))
    monkeypatch.setattr(cli,'Inspector',lambda _:None)
    monkeypatch.setattr(cli,'load_config',lambda _:None)
    monkeypatch.setattr(cli,'Withdrawal',lambda _:w)
    path=tmp_path/'quote.json'
    args=['withdrawal','prepare','--config','owner','--calculation','payout','--asset','MP',
        '--payout-sats','1000','--fee-cap-sats','100','--destination','address','--max-rate','10',
        '--deadline','5000','--out',str(path)]
    assert bitcoin_cli.main(args)==0
    assert calls[0]['fee_mode']=='MP' and calls[0]['payout_sats']=='1000'
    assert path.is_file() and path.stat().st_mode&0o077==0
    with pytest.raises(ValueError,match='QUOTE_EXISTS'):bitcoin_cli.main(args)
    with pytest.raises(ValueError,match='WITHDRAWAL_USER_QUOTE'):
        cli.main(['submit','--config','owner','--quote',str(path),'--accept','bb'*32])
    assert cli.main(['submit','--config','owner','--quote',str(path),'--accept','ab'*32])==0


def test_simple_pool_uses_authenticated_callback_creation_record(workflow,monkeypatch):
    from signer_agent import bitcoin_trust,contract_provenance,callback_admission
    w,_,_,ctx=workflow
    admitted=SimpleNamespace(functions=Functions(dict(isSimplePool=lambda _:True)))
    candidate=SimpleNamespace(address=address(30),functions=Functions(dict(withdrawalEscrow=ctx['escrow'].address)))
    monkeypatch.setattr(bitcoin_trust,'checked_runtime',lambda *a:None)
    def unsupported(*a,**kw):raise AssertionError('factory pool cannot be proven as direct CREATE')
    monkeypatch.setattr(contract_provenance,'prove_creation',unsupported)
    monkeypatch.setattr(callback_admission,'review_callback_configuration',lambda *a:(admitted,None,None))
    ctx['escrow'].functions=Functions(dict(callbackPoolRegistry=address(40)))
    w.i.w3.eth.contract=lambda **kw:candidate
    assert w.simple(ctx,address(30))==candidate
    admitted.functions=Functions(dict(isSimplePool=lambda _:False))
    with pytest.raises(ValueError,match='WITHDRAWAL_FEE_OWNING_POOL'):w.simple(ctx,address(30))
