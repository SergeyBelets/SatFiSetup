"""Retired graphs still settle wallet liabilities and preserve durable retries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from hexbytes import HexBytes
from web3 import Web3

from signer_agent import bootstrap, confederation_dao_cli as dao_cli, dao_client, fee_sale_cli
from signer_agent import fee_sale_governance as sale_policy
from signer_agent import bitcoin_trust
from signer_agent.operations import Journal


def address(index):
    return Web3.to_checksum_address(f"0x{index:040x}")


@pytest.fixture
def dao_graph(monkeypatch):
    labels = ("factory", "auth", *dao_cli.NODES)
    nodes = {name: SimpleNamespace(address=address(i + 1)) for i, name in enumerate(labels)}
    by_address = {node.address: node for node in nodes.values()}
    kind_by_address = {nodes[name].address: kind for name, kind in dao_cli.NODES.items()}
    block = SimpleNamespace(number=80, hash=b"a" * 32, timestamp=10 * 86400)
    opened, sent = [], []
    good = nodes["dao_income"].address
    registered = (
        *([None] * 7), nodes["auth"].address, nodes["validator"].address,
        nodes["conf_pool"].address, nodes["conf_shares"].address, False,
    )
    values = {
        ("factory", "getConfederation"): registered,
        ("conf_pool", "daoIncome"): good,
        ("conf_pool", "confederationId"): "retired",
        ("conf_pool", "shareToken"): nodes["conf_shares"].address,
        ("conf_shares", "confederationPool"): nodes["conf_pool"].address,
        ("dao_income", "staking"): nodes["staking"].address,
        ("dao_income", "authorization"): nodes["auth"].address,
        ("dao_income", "pool"): nodes["conf_pool"].address,
        ("dao_income", "validator"): nodes["validator"].address,
        ("dao_income", "shareToken"): nodes["conf_shares"].address,
        ("dao_income", "factory"): nodes["factory"].address,
        ("dao_income", "settlementDayLimit"): 10,
        ("validator", "candidateStaking"): nodes["staking"].address,
        ("staking", "custodianValidator"): nodes["validator"].address,
        ("staking", "stakeToken"): nodes["dao_token"].address,
    }
    reads = {(nodes[name].address, method): value for (name, method), value in values.items()}

    class Client:
        def __init__(self, rpc, chain):
            assert (rpc, chain) == ("https://rpc.example", "11155111")
            self.contracts = {}
            self.w3 = SimpleNamespace(eth=SimpleNamespace(get_code=lambda target, _: bytes.fromhex(target[2:])))

        def read(self, node, method, *args):
            return reads[node.address, method]

        def stable(self):
            pass

        def transact(self, *args):
            sent.append(args)
            return {"status": "SIMULATED"}

    def contract(w3, target, kind, at, *, legacy_dao_exit):
        assert at == block.number
        assert kind == kind_by_address.get(target, "ConfederationFactory")
        opened.append((kind, legacy_dao_exit))
        return by_address[target]

    monkeypatch.setattr(dao_cli, "DAOClient", Client)
    monkeypatch.setattr(dao_cli, "read_block", lambda *_: block)
    monkeypatch.setattr(dao_cli, "_contract", contract)
    options = ["--rpc", "https://rpc.example", "--factory", nodes["factory"].address,
               "--chain-id", "11155111", "--confederation-id", "retired", "--sender", address(80)]
    return options, nodes, reads, opened, sent


@pytest.mark.parametrize("action,contract,method,args,values", [
    ("unstake", "staking", "unstake", ["--amount", "15"], [15]),
    ("claim", "dao_income", "claim", ["--token", address(81), "--days", "7"], [address(81), [7]]),
    ("reimburse", "dao_income", "claimReimbursement", ["--token", address(81)], [address(81)]),
    ("cancel", "conf_pool", "cancelWithdrawal", ["--withdrawal", "42"], [42]),
    ("process", "dao_income", "processDay", ["--rows", "3"], [3]),
])
def test_dao_exit_context_after_deactivation(dao_graph, action, contract, method, args, values):
    options, _, _, opened, sent = dao_graph
    assert dao_cli.main([action, *options, *args]) == 0
    assert sent == [(contract, method, values, address(80), None, False, None, "dao-preview")]
    assert all(legacy for _, legacy in opened)
    assert {"ConfederationNFTPass", "BitcoinCustodyTransferV2"}.isdisjoint(kind for kind, _ in opened)


@pytest.mark.parametrize("action", ["stake", "withdraw", "wrap", "deposit-quote", "sweep", "propose", "execute"])
def test_deactivated_dao_rejects_new_activity(dao_graph, action):
    options, _, _, opened, sent = dao_graph
    with pytest.raises(ValueError, match="^CONFEDERATION_INACTIVE$"):
        dao_cli.main([action, *options])
    assert opened == [("ConfederationFactory", False)]
    assert not sent


@pytest.mark.parametrize("action,method,extra,values", [
    ("closing-sweep", "sweep", ["--token", address(81)], [address(81)]),
    ("closing-process", "processClosingIncome", ["--rows", "8"], [8]),
    ("closing-claim", "claimClosingReward", ["--token", address(81)], [address(81)]),
])
def test_closing_cli_requires_the_new_linked_factory(dao_graph, action, method, extra, values):
    options, nodes, reads, opened, sent = dao_graph
    assert dao_cli.main([action, *options, *extra]) == 0
    assert sent == [("dao_income", method, values, address(80), None, False, None, "dao-preview")]
    assert all(not legacy for _, legacy in opened)
    reads[nodes["dao_income"].address, "factory"] = address(99)
    with pytest.raises(ValueError, match="CONFEDERATION_DAO_BINDING"):
        dao_cli.main([action, *options, *extra])


@pytest.mark.parametrize("helper", ["getSettlementOperationHash", "getStageExpensesOperationHash",
                                     "getSetClosingRatesOperationHash",
                                     "getAddStablecoinOperationHash"])
def test_only_report_governance_is_routed_for_retired_dao(dao_graph, monkeypatch, tmp_path, helper):
    options, _, _, opened, sent = dao_graph
    intent_file = tmp_path / "intent.json"
    intent_file.write_text(json.dumps(dict(artifact="ConfederationDAOIncome", hash_method=helper)))

    def run(client, args):
        assert client.config["last_settlement_day"] == "9"
        assert args.reviewed_intent == json.loads(intent_file.read_text())
        return {"status": "REVIEWED"}

    monkeypatch.setattr(dao_cli, "_operation", run)
    if helper == "getAddStablecoinOperationHash":
        with pytest.raises(ValueError, match="CONFEDERATION_INACTIVE"):
            dao_cli.main(["propose", *options, "--intent", str(intent_file)])
    else:
        assert dao_cli.main(["propose", *options, "--intent", str(intent_file)]) == 0
        assert all(not legacy for _, legacy in opened)
    assert not sent


@pytest.mark.parametrize("action,args,binding", [
    ("reimburse", ["--token", address(81)], ("dao_income", "pool")),
    ("cancel", ["--withdrawal", "42"], ("conf_shares", "confederationPool")),
    ("process", ["--rows", "3"], ("dao_income", "authorization")),
])
def test_deactivated_dao_exit_still_rejects_wrong_graph(dao_graph, action, args, binding):
    options, nodes, reads, _, sent = dao_graph
    name, method = binding
    reads[nodes[name].address, method] = address(99)
    with pytest.raises(ValueError, match="^CONFEDERATION_DAO_BINDING$"):
        dao_cli.main([action, *options, *args])
    assert not sent


@pytest.mark.parametrize("failure,error", [
    (None, None),
    ("runtime", "BTC_CONTRACT_BUILD_MISMATCH"),
    ("escrow", "CONFEDERATION_DAO_BINDING"),
    ("creation", "BTC_CONSTRUCTOR_INPUT_MISMATCH"),
])
def test_v3_withdrawal_checks_exact_previous_reserve(dao_graph, monkeypatch, failure, error):
    from signer_agent import contract_provenance

    options, nodes, reads, _, sent = dao_graph
    reserve, pool = nodes["mining"], nodes["conf_pool"]
    escrow, deposits, auth = address(90), address(91), nodes["auth"].address
    registered = reads[nodes["factory"].address, "getConfederation"]
    reads[nodes["factory"].address, "getConfederation"] = (*registered[:-1], True)
    values = {
        ("dao_income", "decisions"): nodes["decisions"].address,
        ("dao_income", "passSale"): nodes["nft_pass"].address,
        ("decisions", "staking"): nodes["staking"].address,
        ("decisions", "authorization"): auth,
        ("nft_pass", "income"): nodes["dao_income"].address,
        ("conf_pool", "bitcoinReserve"): reserve.address,
        ("conf_pool", "withdrawalEscrow"): escrow,
        ("conf_pool", "frostMultisig"): auth,
        ("mining", "poolId"): "previous",
        ("mining", "confederationPool"): pool.address,
        ("mining", "escrow"): address(92) if failure == "escrow" else escrow,
        ("mining", "authorization"): auth,
        ("mining", "deposits"): deposits,
        ("mining", "shareToken"): nodes["conf_shares"].address,
        ("mining", "custodyTransfer"): nodes["custody_transfer"].address,
        ("custody_transfer", "mining"): reserve.address,
        ("custody_transfer", "pool"): pool.address,
    }
    reads.update({(nodes[name].address, method): value for (name, method), value in values.items()})
    current = dao_cli._contract
    kind = "ConfederationBitcoinReserve"
    archived = bitcoin_trust.previous_artifact(kind)
    proof_arguments = []

    def strict(w3, target, artifact, at, *, legacy_dao_exit):
        if target == reserve.address:
            raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")
        return current(w3, target, artifact, at, legacy_dao_exit=legacy_dao_exit)

    def runtime(w3, target, artifact, at, *, previous_build):
        assert (target, artifact, at, previous_build) == (reserve.address, kind, 80, True)
        if failure == "runtime":
            raise ValueError("BTC_CONTRACT_BUILD_MISMATCH")
        def contract(address, abi):
            assert address == reserve.address and abi == archived["abi"]
            return reserve
        w3.eth.contract = contract
        return "verified", archived

    def prove(w3, target, artifact, args, at, *, previous_build):
        assert (target, artifact, at, previous_build) == (reserve.address, kind, 80, True)
        proof_arguments.append(args)
        if failure == "creation":
            raise ValueError("BTC_CONSTRUCTOR_INPUT_MISMATCH")

    monkeypatch.setattr(dao_cli, "_contract", strict)
    monkeypatch.setattr(bitcoin_trust, "runtime_artifact", runtime)
    monkeypatch.setattr(contract_provenance, "prove_creation", prove)
    user_args = ["withdraw", *options, "--amount", "10000000000000", "--script", "5120" + "11" * 32,
                 "--minimum-btc-sats", "330", "--deadline", "3600"]
    if error is None:
        assert dao_cli.main(user_args) == 0
        assert [(entry[0], entry[1]) for entry in sent] == [("conf_pool", "requestWithdrawal")]
    else:
        with pytest.raises(ValueError, match="^" + error + "$"):
            dao_cli.main(user_args)
        assert sent == []
    assert proof_arguments == ([["previous", deposits, escrow, pool.address]]
                               if failure in (None, "creation") else [])


@pytest.fixture
def sale_graph(monkeypatch):
    names = ("board", "factory", "sale", "fee_token", "stablecoin", "treasury", "auth",
             "operator", "registry", "coordinator", "pool", "validator", "previous_sale")
    points = {name: address(i + 20) for i, name in enumerate(names)}
    pin = {key: points[key] for key in names[:6]}
    pin.update(version="1", chain_id="11155111", confirmations="12", role="conf_ceremony")
    session = b"a" * 32
    invite = dict(sessions={"conf_ceremony": session.hex()}, evm_confirmations="12",
                  ulr={"version": "3"}, confederation_id="retired", operator=points["operator"])
    bundle = dict(onchain_board=pin["board"], role=pin["role"], onboarding={"invitation": invite})
    block = SimpleNamespace(number=80, hash=b"b" * 32)
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=11155111, get_block=lambda _: block,
                                           get_code=lambda *a, **kw: b"token"))
    manifest = SimpleNamespace(chain_id=11155111, purpose="EVM_AUTH", session_id=session,
                               registry=points["registry"])
    entity = (*([None] * 7), points["auth"], points["validator"], points["pool"], points["fee_token"], False)
    rows = {
        "board": dict(registry=points["registry"]),
        "factory": dict(getConfederation=entity, dkgCoordinator=points["registry"],
                        frostCoordinator=points["coordinator"]),
        "pool": dict(daoIncome=points["treasury"]),
        "treasury": dict(pool=points["pool"], shareToken=points["fee_token"],
                         authorization=points["auth"], validator=points["validator"],
                         approvedToken=True, feeSale=points["previous_sale"]),
        "auth": dict(registry=points["registry"], dkgId=session, coordinator=points["coordinator"]),
        "sale": dict(source=points["treasury"], feeToken=pin["fee_token"], stablecoin=pin["stablecoin"],
                     treasury=pin["treasury"], authorization=points["auth"], validator=points["validator"]),
    }
    reads = {points[name]: row for name, row in rows.items()}

    def node(target):
        return SimpleNamespace(address=target)

    monkeypatch.setattr(sale_policy.Manifest, "from_dict", lambda _: manifest)
    monkeypatch.setattr(sale_policy, "_board_and_invitation", lambda *a, **kw: (node(pin["board"]), invite))
    monkeypatch.setattr(sale_policy, "contract", lambda _w, _kind, target: node(target))
    monkeypatch.setattr(sale_policy, "authority_context", lambda _w, target, _, **kw: node(target))
    monkeypatch.setattr(sale_policy, "read_block", lambda *_: block)
    monkeypatch.setattr(sale_policy, "checked_runtime", lambda *_: None)
    monkeypatch.setattr(sale_policy, "runtime_artifact", lambda *_a, **_kw: None)
    monkeypatch.setattr(sale_policy, "read", lambda contract, _at, method, *_: reads[contract.address][method])
    monkeypatch.setattr(sale_policy, "prove_creation",
                        lambda *_a, **_kw: {"method": "direct", "sender": points["operator"]})
    return w3, bundle, pin, block, rows


def test_buyer_can_simulate_refund_from_original_sale_after_deactivation(sale_graph, monkeypatch):
    w3, bundle, pin, block, _ = sale_graph
    options = ["--rpc", "https://rpc.example", "--config", "fee-sale.json", "--bid", "3",
               "--sender", address(88)]
    monkeypatch.setattr(fee_sale_cli, "evm_web3", lambda *a, **kw: w3)
    monkeypatch.setattr(fee_sale_cli, "read_block", lambda *_: block)
    monkeypatch.setattr(fee_sale_cli, "read_public", lambda _: pin)
    monkeypatch.setattr(fee_sale_cli, "ceremony_bundle", lambda *_a, **_kw: bundle)
    monkeypatch.setattr(fee_sale_cli, "_executor", lambda *a, **kw: object())
    observed = []

    def transact(executor, args, buyer, identifier, name, method, values):
        observed.append((buyer, name, method, values))
        return dict(status="SIMULATED")

    monkeypatch.setattr(fee_sale_cli, "_transact", transact)
    assert fee_sale_cli.main(["cancel", *options]) == 0
    assert observed == [(address(88), "target", "cancelBid", [3])]

    with pytest.raises(ValueError, match="^FEE_SALE_SOURCE$"):
        fee_sale_cli.main(["fund", *options])


@pytest.mark.parametrize("matching_owner", [True, False])
def test_cancel_receipt_identifies_original_buyer_and_amount(monkeypatch, matching_owner):
    buyer = address(80)
    sale = SimpleNamespace(address=address(81))
    events = [dict(address=sale.address, args=dict(id=3,
        buyer=buyer if matching_owner else address(82), stableAmount=55))]
    sale.events = SimpleNamespace(BidCancelled=lambda: SimpleNamespace(
        process_receipt=lambda *a, **kw: events))
    w3 = SimpleNamespace(eth=SimpleNamespace(get_transaction_receipt=lambda tx: tx))
    args = SimpleNamespace(action="cancel", id="refund", bid="3", send=True)
    monkeypatch.setattr(fee_sale_cli, "_executor", lambda *a, **kw: None)
    monkeypatch.setattr(fee_sale_cli, "_sender", lambda _: buyer)
    monkeypatch.setattr(fee_sale_cli, "_transact", lambda *a: dict(status="VERIFIED", tx_hash="01"))
    ctx = dict(block=80, sale=sale)
    if matching_owner:
        result = fee_sale_cli._bid(w3, args, {}, ctx)
        assert result["bid"] == "3" and result["refunded_stable_amount"] == "55"
    else:
        with pytest.raises(ValueError, match="^FEE_SALE_BID_RECEIPT$"):
            fee_sale_cli._bid(w3, args, {}, ctx)


def test_original_sale_exit_still_checks_immutable_deployment_bindings(sale_graph):
    w3, bundle, pin, block, reads = sale_graph
    ctx = sale_policy.context(w3, bundle, pin, block.number, refund_context=True)
    assert ctx["source_active"] is False
    assert ctx["sale"].address == pin["sale"] and ctx["bound"] != pin["sale"]
    reads["sale"]["treasury"] = address(101)
    with pytest.raises(ValueError, match="^FEE_SALE_BINDING$"):
        sale_policy.context(w3, bundle, pin, block.number, refund_context=True)


@pytest.mark.parametrize("kind", sorted(bitcoin_trust.LEGACY_DAO_EXIT_REFERENCES))
def test_previous_contract_artifact_is_accepted_only_for_exit(monkeypatch, kind):
    folder = Path(bitcoin_trust.__file__).resolve().parents[1] / "contracts"
    reference = folder / (bitcoin_trust.LEGACY_DAO_EXIT_REFERENCES[kind] + ".json")
    previous = json.loads(reference.read_text())
    bytes_ = bytes.fromhex(previous["deployedBytecode"]["object"].removeprefix("0x"))
    w3 = SimpleNamespace(eth=SimpleNamespace(get_code=lambda *a, **kw: bytes_))
    monkeypatch.setattr(bitcoin_trust, "artifact", lambda _: dict(abi=[], deployedBytecode=dict(object="0xff")))
    with pytest.raises(ValueError, match="^BTC_CONTRACT_BUILD_MISMATCH$"):
        bitcoin_trust.checked_runtime(w3, address(20), kind, 80)
    _, actual = bitcoin_trust.runtime_artifact(w3, address(20), kind, 80, legacy_dao_exit=True)
    assert actual["legacy_reference"] == reference.stem
    assert actual.get("bytecode") == previous.get("bytecode")


@pytest.mark.parametrize("view", ["healthy", "behind", "different_anchor"])
def test_resume_reuses_original_operation_with_current_routing_and_floor(tmp_path, monkeypatch, view):
    old_rpc = "https://old-rpc.example"
    rpc = "https://new-rpc.example"
    account = SimpleNamespace(address=address(1))
    factory, target = address(2), address(3)
    function = SimpleNamespace(call=lambda *_: pytest.fail("resuming an operation ran fresh simulation"))
    contract = SimpleNamespace(address=target, functions=SimpleNamespace(cancelBid=lambda _: function))
    latest = 91 if view == "behind" else 100
    conf_at_write, conf_now = "2", "3"
    tx_hash = "0x" + "aa" * 32
    at_write = 90
    anchor = HexBytes(at_write.to_bytes(32, "big"))

    def get_block(number):
        if number == "latest":
            number = latest
        block_hash = b"a" * 32 if view == "different_anchor" and number == at_write else number.to_bytes(32, "big")
        return SimpleNamespace(number=number, hash=HexBytes(block_hash))

    current_w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=11155111, get_block=get_block),
                                 provider=SimpleNamespace(endpoint_uri=rpc))
    client = dao_client.DAOClient.__new__(dao_client.DAOClient)
    client.w3, client.contracts = current_w3, {"target": contract}
    specs = {"target": dict(address=target, artifact="FundedFeeSale", runtime_hash="01" * 32)}
    client.config = dict(chain_id="11155111", factory=factory, confederation_id="ULR",
                         confirmations=conf_now, contracts=specs)
    path = tmp_path / "operations.sqlite"
    state = Journal(path)
    prefix = "dao-tx-refund"
    step = dict(id="send", kind="call", contract="target", method="cancelBid", args=["3"])
    saved_profile = dict(deployment_id=prefix, mode="integration", bitcoin_network="regtest",
                         chain_id="11155111", evm_rpc=old_rpc, evm_confirmations=conf_at_write,
                         contracts=specs, steps=[step])
    state.put(prefix, dict(profile=saved_profile, version="1"))
    state.put(prefix + ":send", step)
    state.update(prefix + ":send", "CONFIRMED",
                 dict(tx_hash=tx_hash, block_hash=anchor.hex(), block_number=str(at_write), nonce="8"))
    prior_step = state.get(prefix + ":send")
    selected_transport = []
    applied = []

    def provider(url, **_):
        selected_transport.append(url)
        assert url == rpc
        return current_w3

    def apply(self, identity):
        assert identity == "same-identity"
        assert self.w3 is current_w3 and self.evm_confirmations == 3
        assert self.profile == saved_profile and self.prefix == prefix
        applied.append(self)
        return dict(status="VERIFIED")

    monkeypatch.setattr(bootstrap, "evm_web3", provider)
    monkeypatch.setattr(bootstrap.Bootstrap, "apply_confirmed", apply)
    monkeypatch.setattr(dao_client, "account_at", lambda _: account)

    def resume():
        return client.transact("target", "cancelBid", [3], account.address,
                               "same-identity", True, path, "refund")

    if view == "healthy":
        assert resume()["tx_hash"] == tx_hash
        assert len(applied) == 1
    elif view == "behind":
        assert resume() == dict(status="AWAITING_EVM_RPC", reason="EVM_RPC_BEHIND", id="refund")
        assert not applied
    else:
        with pytest.raises(ValueError, match="^EVM_REORG_RECOVERY_REQUIRED$"):
            resume()
        assert not applied
    assert selected_transport == [rpc]
    assert state.get(prefix)["request"]["profile"] == saved_profile
    assert state.get(prefix + ":send") == prior_step
