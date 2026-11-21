"""New confederations deploy the in-kind graph with real bootstrap authorizations."""

import json

import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.logs import DISCARD

from signer_agent.bootstrap import encode_args
from signer_agent.confederation_dao_report import prepare
from test_full_bootstrap import exercise_bootstrap
from test_ulr_distribution_pause import send

CUSTODY = dict(version="1", mode="custodians", threshold="2", sources=[])


def test_new_graph_bootstrap_and_isolated_stake(tmp_path, record_property):
    policy = dict(version="3", mode="create", bitcoin_backend="relay",
                  candidate_threshold_wei=str(200_000 * 10**18), default_miner_fee_sats="100")
    first = tmp_path / "first"
    first.mkdir()
    context = exercise_bootstrap(first, record_property, custody=CUSTODY, in_kind=policy)
    bootstrap = context["bootstrap"]
    result = context["manifest"]
    assert result["ulr"]["graph_configured"]
    assert not result["ulr"]["ready"]
    assert "converter" not in result["contracts"]
    assert "ulr_treasury" not in result["contracts"]
    assert "mp" not in result["contracts"]
    assert result["contracts"]["mining"]["artifact"] == "ConfederationBitcoinReserve"
    assert bootstrap.contract("conf_pool").functions.bitcoinReserve().call() == bootstrap.contract("mining").address
    assert bootstrap.contract("dao_income").functions.rewardTokens().call() == [
        bootstrap.contract("conf_shares").address, bootstrap.contract("usd").address]
    assert bootstrap.contract("escrow").functions.defaultMinerFeeLimitSats().call() == 100
    manifest = first / "manifest.json"
    manifest.write_text(json.dumps(result))
    discovery = context["command"]("dao", "in-kind", "discover",
        "--rpc", bootstrap.w3.provider.endpoint_uri, "--confirmations", "1",
        "--chain-id", result["chain_id"], "--factory", bootstrap.contract("confederations").address,
        "--confederation-id", result["ulr"]["confederation_id"])
    assert discovery["contracts"]["decisions"]["address"] == bootstrap.contract("decisions").address
    user = context["addresses"][1]
    for action in ("approve", "stake"):
        status = context["command"]("dao", "in-kind", action,
            "--manifest", manifest, "--rpc", bootstrap.w3.provider.endpoint_uri,
            "--confirmations", "1", "--amount", str(200_000 * 10**18),
            "--identity-file", context["identities"][1], "--state", first / "wallet.sqlite",
            "--id", action, "--send", member=1)
        assert status["status"] == "VERIFIED", status
    assert bootstrap.contract("staking").functions.stakedBalance(user).call() == 200_000 * 10**18
    inspected = context["participant"]("btc_ceremony", 1, "inspect-bitcoin-registry")
    assert inspected["graph"]["contracts"]["escrow"]["artifact"] == "BitcoinWithdrawalEscrowV2"

    second = tmp_path / "second"
    second.mkdir()
    reused = exercise_bootstrap(second, record_property, custody=CUSTODY,
        in_kind=result["ulr"]["reuse_policy"], participant_keys=(4, 5, 6))
    other = reused["bootstrap"]
    assert other.contract("dao_token").address == bootstrap.contract("dao_token").address
    assert other.contract("usd").address == bootstrap.contract("usd").address
    assert other.contract("staking").address != bootstrap.contract("staking").address
    assert other.contract("staking").functions.stakedBalance(user).call() == 0
    settle_first_day(context, manifest, first)


def test_stakers_claim_net_confirmed_income(tmp_path, record_property):
    policy = dict(version="3", mode="create", bitcoin_backend="relay",
                  candidate_threshold_wei=str(200_000 * 10**18), default_miner_fee_sats="100")
    context = exercise_bootstrap(tmp_path, record_property, custody=CUSTODY, in_kind=policy)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(context["manifest"]))
    bootstrap = context["bootstrap"]
    w3 = bootstrap.w3
    staking = bootstrap.contract("staking")
    governance = bootstrap.contract("dao_token")
    income = bootstrap.contract("dao_income")
    usd = bootstrap.contract("usd")
    owners = context["addresses"][1:]
    stakes = (200_000 * 10**18, 600_000 * 10**18)
    issuer = Account.from_key(json.loads(context["identities"][1].read_text())["private_key"])
    send(w3, issuer, governance.functions.transfer(owners[1], stakes[1]))
    wallets = [in_kind_wallet(context, manifest, tmp_path, member) for member in (1, 2)]
    for wallet, amount in zip(wallets, stakes):
        assert wallet("approve", "approve", "--amount", str(amount))["status"] == "VERIFIED"
        assert wallet("stake", "stake", "--amount", str(amount))["status"] == "VERIFIED"

    first_day = income.functions.nextSettlementDay().call()
    assert [staking.functions.eligibleStake(owner, first_day).call() for owner in owners] == [0, 0]
    ingress = 1_000_000_001
    usd_rate = 2_000 * 10**usd.functions.decimals().call()
    net, rates = settle_first_day(context, manifest, tmp_path, amount=ingress, usd_rate=usd_rate)
    assert 0 < net < ingress
    assert income.functions.dayReward(first_day, usd.address).call() == 0
    assert income.functions.availableIncome(usd.address).call() == net

    reward_day = first_day + 1
    assert "error" not in w3.provider.make_request("evm_setNextBlockTimestamp", [(reward_day + 1) * 86400])
    assert "error" not in w3.provider.make_request("evm_mine", [])
    operator = in_kind_wallet(context, manifest, tmp_path, 0)
    close_reported_day(context, tmp_path, operator, reward_day,
                       dict(day=str(reward_day), units_per_eth=rates, transactions=[]))
    assert income.functions.dayEligibleTotal(reward_day).call() == sum(stakes)
    assert [staking.functions.eligibleStake(owner, reward_day).call() for owner in owners] == list(stakes)
    assert income.functions.dayReward(reward_day, usd.address).call() == net
    claims = []
    args = ("--token", usd.address, "--days", str(reward_day))
    for wallet, owner, weight in zip(wallets, owners, stakes):
        before = usd.functions.balanceOf(owner).call()
        claimed = wallet("claim", "claim", *args)
        assert claimed["status"] == "VERIFIED", claimed
        reward = usd.functions.balanceOf(owner).call() - before
        assert reward == net * weight // sum(stakes)
        assert income.functions.claimed(owner, usd.address, reward_day).call()
        assert wallet("claim", "claim", *args)["tx_hash"] == claimed["tx_hash"]
        assert usd.functions.balanceOf(owner).call() == before + reward
        claims.append(reward)
    remainder = net - sum(claims)
    assert usd.functions.balanceOf(income.address).call() == remainder
    record_property("staker_claim", json.dumps(dict(
        token=usd.functions.symbol().call(), decimals=usd.functions.decimals().call(),
        ingress=ingress, reimbursement=ingress - net, net=net,
        stake_proportion=[1, 3], claims=claims, remainder=remainder)))


def in_kind_wallet(context, manifest, workdir, member):
    def wallet(action, operation, *args):
        return context["command"]("dao", "in-kind", action,
            "--rpc", context["bootstrap"].w3.provider.endpoint_uri, "--confirmations", "1",
            "--manifest", manifest, "--identity-file", context["identities"][member],
            "--state", workdir / f"wallet-{member}.sqlite", "--send", "--id", operation,
            *args, member=member)

    return wallet


def settle_first_day(context, manifest, workdir, *, amount=10**18, usd_rate=10**18):
    bootstrap = context["bootstrap"]
    w3 = bootstrap.w3
    income = bootstrap.contract("dao_income")
    usd = bootstrap.contract("usd")
    payer = context["addresses"][0]
    wallet = in_kind_wallet(context, manifest, workdir, 0)

    day = income.functions.nextSettlementDay().call()
    funding = usd.functions.mint(income.address, amount).transact({"from": w3.eth.accounts[0]})
    swept = wallet("sweep", "first-sweep", "--token", usd.address)
    assert swept["status"] == "VERIFIED"
    receipt = w3.eth.get_transaction_receipt(swept["tx_hash"])
    assert w3.eth.get_block(receipt.blockNumber).timestamp // 86400 == day
    assert "error" not in w3.provider.make_request("evm_setNextBlockTimestamp", [(day + 1) * 86400])
    assert "error" not in w3.provider.make_request("evm_mine", [])

    rates = {token: str(usd_rate if token == usd.address else 10**18)
             for token in income.functions.rewardTokens().call()}
    inputs = dict(day=str(day), units_per_eth=rates, transactions=[Web3.to_hex(receipt.transactionHash)])
    with pytest.raises(ValueError, match="DAO_EXPENSE_RECEIPT"):
        prepare(w3, income.address, dict(inputs, transactions=[Web3.to_hex(funding)]), w3.eth.get_block("latest"))
    close_reported_day(context, workdir, wallet, day, inputs)
    expenses = receipt.gasUsed * receipt.effectiveGasPrice * usd_rate // 10**18
    assert income.functions.reimbursement(payer, usd.address).call() == expenses
    balance = usd.functions.balanceOf(payer).call()
    assert wallet("reimburse", "first-reimburse", "--token", usd.address)["status"] == "VERIFIED"
    assert usd.functions.balanceOf(payer).call() == balance + expenses
    return amount - expenses, rates


def close_reported_day(context, workdir, wallet, day, inputs):
    bootstrap = context["bootstrap"]
    w3 = bootstrap.w3
    rpc = w3.provider.endpoint_uri
    income = bootstrap.contract("dao_income")
    paths = {name: workdir / f"day-{day}-{name}.json" for name in ("inputs", "evidence", "intent")}
    paths["inputs"].write_text(json.dumps(inputs))
    report = context["command"]("dao-report", "--rpc", rpc, "--confirmations", "1",
        "--in-kind", "--settlement", income.address, "--inputs", paths["inputs"],
        "--evidence", paths["evidence"], "--intent", paths["intent"])
    proposal = wallet("propose", f"propose-{day}", "--board", context["board"], "--intent", paths["intent"],
        "--deadline", str(w3.eth.get_block("latest").timestamp + 3600))
    assert proposal["status"] == "AWAITING_PARTICIPANTS"
    assert proposal["operation_hash"] == report["operation_hash"]
    approve_dao_intent(context, proposal, workdir, f"report-{day}")
    assert wallet("execute", f"execute-{day}", "--board", context["board"], "--intent", paths["intent"],
        "--session", proposal["session"])["status"] == "VERIFIED"
    assert wallet("process", f"process-{day}")["status"] == "VERIFIED"
    assert income.functions.nextSettlementDay().call() == day + 1


def approve_dao_intent(context, proposal, workdir, operation):
    rpc = context["bootstrap"].w3.provider.endpoint_uri
    for expected in ("committed", "signed"):
        for member in (1, 2):
            signed = context["participant"]("conf_ceremony", member, "sign-evm", "--session", proposal["session"])
            assert signed["status"] in (expected, "finalized")
    finalized = context["participant"]("conf_ceremony", 1, "sign-evm", "--session", proposal["session"])
    assert finalized["status"] == "finalized"
    for member in (1, 2):
        approved = context["setup"]("approve-custodian", "--board", context["board"], "--rpc", rpc,
            "--session", proposal["session"], "--operation-hash", proposal["operation_hash"],
            "--identity-file", context["identities"][member], "--state", workdir / f"{operation}-{member}.sqlite",
            member=member)
        assert approved["status"] == "CUSTODIAN_APPROVED"


def buy_native_pass(context, manifest, buyer, buyer_file, workdir, *, price=1000 * 10**10):
    bootstrap = context["bootstrap"]
    w3 = bootstrap.w3
    sale = bootstrap.contract("nft_pass")
    shares = bootstrap.contract("conf_shares")
    income = bootstrap.contract("dao_income")
    operator_file = context["identities"][0]
    intent, quote = workdir / "pass-intent.json", workdir / "pass-quote.json"
    deadline = str(w3.eth.get_block("latest").timestamp + 3600)

    def dao(action, *args):
        return context["command"]("dao", "in-kind", action, "--rpc", w3.provider.endpoint_uri,
            "--confirmations", "1", "--manifest", manifest, *args)

    def send(action, operation_id, *args, identity=operator_file):
        journal = workdir / ("pass-operator.sqlite" if identity == operator_file else "pass-buyer.sqlite")
        return dao(action, "--send", "--id", operation_id, "--state", journal,
                   "--identity-file", identity, *args)

    tariff = dao("tariff-prepare", "--duration", "86400", "--discount-bps", "5000",
        "--volume-limit", str(shares.functions.totalSupply().call()), "--output", intent)
    assert tariff["status"] == "PREPARED"
    approval = ("--board", context["board"], "--intent", intent)
    proposal = send("propose", "tariff-propose", *approval, "--deadline", deadline)
    assert proposal["status"] == "AWAITING_PARTICIPANTS"
    assert proposal["operation_hash"] == tariff["operation_hash"]
    approve_dao_intent(context, proposal, workdir, "pass-tariff")
    execution = ("--session", proposal["session"], *approval)
    assert send("execute", "tariff-execute", *execution)["status"] == "VERIFIED"
    assert send("execute", "tariff-execute", *execution)["status"] == "ALREADY_EXECUTED"

    prepared = dao("price", "--identity-file", operator_file, "--tariff", tariff["tariff_id"],
        "--buyer", buyer, "--amount", str(price), "--nonce", "1", "--deadline", deadline, "--output", quote)
    assert prepared["status"] == "PREPARED"
    quote_data = json.loads(quote.read_text())
    buy_abi = next(row["inputs"] for row in sale.abi if row.get("name") == "buy")
    buy = sale.functions.buy(*encode_args(buy_abi, [quote_data["quote"], quote_data["signature"]]))
    with pytest.raises(ContractLogicError):
        buy.call({"from": context["addresses"][0]})

    assert send("approve-pass", "pass-allowance", "--amount", str(price), identity=buyer_file)["status"] == "VERIFIED"
    buyer_balance = shares.functions.balanceOf(buyer).call()
    dao_balance = shares.functions.balanceOf(income.address).call()
    supply = shares.functions.totalSupply().call()
    assert buyer_balance >= price
    bought = send("buy-pass", "pass-buy", "--quote", quote, identity=buyer_file)
    assert bought["status"] == "VERIFIED"
    receipt = w3.eth.get_transaction_receipt(bought["tx_hash"])
    assert receipt.gasUsed * receipt.effectiveGasPrice > 0
    events = sale.events.PassPurchased().process_receipt(receipt, errors=DISCARD)
    assert len(events) == 1
    event = events[0]["args"]
    token_id = event["tokenId"]
    assert event["buyer"] == buyer and event["price"] == price
    assert sale.functions.ownerOf(token_id).call() == buyer
    assert sale.functions.usedQuote(buyer, 1).call()
    assert shares.functions.balanceOf(buyer).call() == buyer_balance - price
    assert shares.functions.balanceOf(income.address).call() == dao_balance + price
    day = w3.eth.get_block(receipt.blockNumber).timestamp // 86400
    assert income.functions.dayIncome(day, shares.address, 2).call() == price
    assert shares.functions.totalSupply().call() == supply
    assert send("buy-pass", "pass-buy", "--quote", quote, identity=buyer_file)["tx_hash"] == bought["tx_hash"]
    with pytest.raises(ContractLogicError):
        buy.call({"from": buyer})
    return token_id
