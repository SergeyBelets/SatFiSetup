"""Full T-02 from fresh enrollment using isolated Bitcoin Core regtest and Anvil."""

from contextlib import ExitStack
import json
from pathlib import Path
from secrets import token_hex

import pytest
from coincurve import PrivateKey
from web3.exceptions import ContractLogicError

from lifecycle_helpers import proof, relay_headers
from signer_agent.bitcoin_rpc import BitcoinRPC, satoshis
from test_bitcoin_cltv_wallet import wallet, wif
from test_bitcoin_processes import BTC_RPC, COOKIE
from test_bitcoin_successor_ceremony import create_successor
from test_custody_transfer import approve, bitcoin_payment, sign
from test_dkg_processes import RPC, tx
from test_full_bootstrap import exercise_bootstrap


SAT = 10**10


def _outpoint(core, txid, script):
    output, = [output for output in core.call("getrawtransaction", txid, True)["vout"]
               if output["scriptPubKey"]["hex"] == script]
    return f"{txid}:{output['n']}", satoshis(output["value"])


def _request(env, pool, script, owner):
    w3 = env["bootstrap"].w3
    withdrawal = pool.functions.withdrawalCounter().call()
    expiry = w3.eth.get_block("latest").timestamp + 3600
    tx(w3, pool.functions.requestWithdrawal(400_000 * SAT, bytes.fromhex(script), 1000, 300_000, expiry), sender=owner)
    approve(env, pool, "ConfederationPoolV2", "getProcessWithdrawalOperationHash", [withdrawal], custodians=True)
    tx(w3, pool.functions.processWithdrawal(withdrawal))
    return withdrawal


def test_t02_full_relay_recovery_and_successor_payout(tmp_path, record_property):
    if not Path(COOKIE).is_file():
        pytest.skip("isolated Bitcoin Core regtest required")
    core = BitcoinRPC(BTC_RPC, COOKIE, network="regtest")
    core.check_network()
    participants = (51, 52, 53)
    successor_path = tmp_path / "successor"
    successor_path.mkdir()
    successor, successor_member, _, _, addresses, _ = create_successor(successor_path, participants)

    with ExitStack() as stack:
        payer = stack.enter_context(wallet(core))
        sponsor = stack.enter_context(wallet(core))
        miner = payer.call("getnewaddress")
        core.call("generatetoaddress", 101, miner)
        height = core.call("getblockcount") + 16
        cold_secrets = {key.public_key_xonly.format().hex(): key.secret for key in (PrivateKey(), PrivateKey(), PrivateKey())}
        sponsor_addr = sponsor.call("getnewaddress", "", "bech32m")
        sponsor_script = core.call("validateaddress", sponsor_addr)["scriptPubKey"]
        terms = dict(height=str(height), threshold="2", keys=sorted(cold_secrets),
                     successor_script=successor["successor_script"], successor_dkg=successor["successor_dkg"],
                     sponsor_script=sponsor_script, confirmations="12", max_fee_sats="3000", pause_guardian=addresses[0])
        custody = dict(version="1", mode="custodians", threshold="2", sources=[])
        v3 = dict(version="3", mode="create", bitcoin_backend="relay",
                  candidate_threshold_wei=str(200_000 * 10**18), default_miner_fee_sats="1000")
        primary = tmp_path / "primary"
        primary.mkdir()
        env = exercise_bootstrap(primary, record_property, custody=custody, in_kind=v3,
                                 recovery=terms, participant_keys=participants)
        payout = _exercise_funds(tmp_path, env, core, stack, payer, sponsor, miner, sponsor_addr, terms, cold_secrets,
                                 successor_member)
        for name, value in payout.items():
            record_property(name, value)


def _exercise_funds(tmp_path, env, core, stack, payer, sponsor, miner, sponsor_addr, terms, cold_secrets, successor_member):
    bootstrap = env["bootstrap"]
    w3 = bootstrap.w3
    reserve, pool, escrow, deposits, shares, relay, batches = [bootstrap.contract(name) for name in
        ("mining", "conf_pool", "escrow", "deposits", "conf_shares", "relay", "batches")]
    owner = w3.eth.accounts[0]
    export = tmp_path / "primary-wallet.json"
    env["participant"]("btc_ceremony", 1, "export", "--format", "public", "--output", export)
    descriptor = json.loads(export.read_text())["recovery_descriptor"]
    source = reserve.functions.payoutScript().call().hex()
    assert descriptor["script_pubkey"] == source
    address = core.call("deriveaddresses", core.call("getdescriptorinfo", descriptor["descriptor"])["descriptor"])[0]
    cold_wallets = []
    for public in sorted(cold_secrets)[:2]:
        cold = stack.enter_context(wallet(core))
        private = descriptor["descriptor"].replace(public, wif(cold_secrets[public]))
        checksum = core.call("getdescriptorinfo", private)["checksum"]
        imported, = cold.call("importdescriptors", [{"desc": private + "#" + checksum, "timestamp": "now"}])
        assert imported["success"]
        cold_wallets.append(cold)

    # Lock the exact mint quote before broadcasting its allocation-bound BTC output.
    allocation = [1_000_000 * SAT]
    data = reserve.functions.getDepositData(0, [owner], allocation).call().hex()
    template = payer.call("createrawtransaction", [], [{address: "0.01000000"}, {"data": data}])
    funded = payer.call("fundrawtransaction", template, {"changePosition": 2, "fee_rate": 2})
    payment = payer.call("signrawtransactionwithwallet", funded["hex"])
    assert payment["complete"]
    deposit_txid = core.call("decoderawtransaction", payment["hex"])["txid"]
    wire_txid = bytes.fromhex(deposit_txid)[::-1]
    tx(w3, reserve.functions.lockDirectDepositQuote(wire_txid, 0, 1_000_000, 1), sender=owner)
    assert core.call("sendrawtransaction", payment["hex"]) == deposit_txid
    sponsor_txid = payer.call("sendtoaddress", sponsor_addr, "0.00030000", "", "", False, True, None, "unset", None, "2")
    core.call("generatetoaddress", 12, miner)
    relay_headers(w3, relay, core)
    funding, amount = _outpoint(core, deposit_txid, source)
    sponsor_input, _ = _outpoint(core, sponsor_txid, terms["sponsor_script"])
    assert funding.endswith(":0") and amount == 1_000_000
    signing_board = _setup_old_board(env)
    _mint(env, signing_board, deposit_txid, owner, allocation[0])
    supply = shares.functions.totalSupply().call()
    balance = shares.functions.balanceOf(owner).call()
    assert supply == deposits.functions.totalBackingSats().call() * SAT == 1_000_000 * SAT

    recipient = payer.call("getnewaddress", "", "bech32m")
    script = core.call("validateaddress", recipient)["scriptPubKey"]
    withdrawal, batch = _lose_old_quorum(env, signing_board, funding, script)
    assert batches.functions.batchState(bytes.fromhex(batch)).call() == 2
    migrated = _migrate(tmp_path, env, core, miner, relay, descriptor, terms, cold_wallets, sponsor,
                        batch, funding, sponsor_input)
    assert core.call("gettxout", deposit_txid, 0) is None
    assert shares.functions.totalSupply().call() == supply and deposits.functions.backingCount().call() == 1
    assert deposits.functions.totalBackingSats().call() == 1_000_000
    tx(w3, pool.functions.cancelWithdrawal(withdrawal), sender=owner)
    assert shares.functions.balanceOf(owner).call() == balance and pool.functions.reservedS().call() == 0
    with pytest.raises(ContractLogicError):
        pool.functions.cancelWithdrawal(withdrawal).call({"from": owner})
    return _resume(tmp_path, env, core, miner, payer, terms, successor_member, batch, signing_board,
                   migrated, recipient, script, supply)


def _pin(member):
    info = member("inspect-bitcoin-registry")
    assert member("trust-bitcoin-registry", "--accept-fingerprint", info["fingerprint"])["status"] == "BTC_REGISTRY_PINNED"


def _setup_old_board(env):
    for index in (1, 2):
        _pin(lambda action, *args: env["participant"]("btc_ceremony", index, action, *args))
    return env["participant"]("btc_ceremony", 1, "btc-deploy-board", "--max-packet-bytes", "4096")["signing_board"]


def _mint(env, board, deposit_txid, owner, allocation, *, deadline=None):
    w3 = env["bootstrap"].w3
    if deadline is None:
        deadline = w3.eth.get_block("latest").timestamp + 3600
    options = ["--signing-board", board, "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE,
               "--txid", deposit_txid, "--vout", "0", "--recipient", owner, "--amount", str(allocation),
               "--deadline", str(deadline)]
    proposal = env["participant"]("btc_ceremony", 1, "btc-mint-propose", *options)
    sign(env, int(proposal["session"]))
    minted = env["participant"]("btc_ceremony", 1, "btc-mint-execute", *options)
    assert minted["status"] == "BTC_FUNDING_EXECUTED", minted


def _lose_old_quorum(env, board, funding, script):
    bootstrap = env["bootstrap"]
    w3, pool, escrow = bootstrap.w3, bootstrap.contract("conf_pool"), bootstrap.contract("escrow")

    def member(i, action, *args):
        return env["participant"]("btc_ceremony", i, action, *args)

    owner = w3.eth.accounts[0]
    withdrawal = _request(env, pool, script, owner)
    request = escrow.functions.requestKey(pool.address, withdrawal).call()
    batch = token_hex(32)
    options = ["--signing-board", board, "--batch", batch, "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE]
    prepared = member(1, "btc-prepare", *options, "--input", funding, "--withdrawal", request.hex(),
                      "--fee", "1000", "--max-fee", "1000", "--deadline",
                      w3.eth.get_block("latest").timestamp + 3600, "--signers", "1", "2")
    assert prepared["status"] == "BTC_PLAN_PREPARED"
    for operation in ("reserve", "bind"):
        proposal = member(1, "btc-propose", *options, "--operation", operation)
        sign(env, int(proposal["session"]))
        member(1, "btc-execute", *options, "--operation", operation)
        if operation == "reserve":
            member(1, "btc-publish-plan", *options)
    for i in (1, 2):
        assert member(i, "btc-commit", *options)["status"] == "COMMITTED"
    assert member(1, "btc-sign", *options)["status"] == "SIGNED"
    assert member(1, "btc-status", *options)["missing_shares"] == [{"input": "0", "signer": "2"}]

    timestamp = pool.functions.getWithdrawal(withdrawal).call()[7]
    delay = pool.functions.WITHDRAWAL_CANCEL_DELAY().call()
    assert "error" not in w3.provider.make_request("evm_setNextBlockTimestamp", [timestamp + delay])
    assert "error" not in w3.provider.make_request("evm_mine", [])
    with pytest.raises(ContractLogicError, match="signature may exist"):
        pool.functions.cancelWithdrawal(withdrawal).call({"from": owner})
    return withdrawal, batch


def _cold_commands(tmp_path, env):
    def command(action, *args):
        return env["command"]("bitcoin", "recovery", action, "--rpc", RPC, "--board", env["board"], *args)

    fingerprint = command("inspect")["fingerprint"]
    review = ["--accept-fingerprint", fingerprint]
    send = ["--identity-file", env["identities"][1], "--state", tmp_path / "recovery.sqlite", "--send"]
    return command, review, send


def _migrate(tmp_path, env, core, miner, relay, descriptor, terms, cold_wallets, sponsor,
             batch, funding, sponsor_input):
    command, review, send = _cold_commands(tmp_path, env)
    rpc = ["--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE]
    sponsor_txid, sponsor_vout = sponsor_input.split(":")
    assert command("register-sponsor", *review, *rpc, *send, "--txid", sponsor_txid, "--vout", sponsor_vout)["status"] == "CONFIRMED"
    migration = [*review, *rpc, "--root", batch, "--input", funding, "--sponsor", sponsor_input,
                 "--internal-key", descriptor["internal_key"], "--fee-sats", "2200"]
    plan, signed = tmp_path / "migration.json", tmp_path / "migration.hex"
    assert command("prepare", *migration, "--output", plan)["status"] == "BTC_RECOVERY_PREPARED"
    psbt = json.loads(plan.read_text())["psbt_base64"]
    parts = []
    for i, signer in enumerate((*cold_wallets, sponsor)):
        path = tmp_path / f"cold-{i}.psbt"
        path.write_text(signer.call("walletprocesspsbt", psbt, True, "DEFAULT", True, False)["psbt"])
        parts.extend(("--partial-psbt", path))
    migration_txid = command("combine", *migration, *parts, "--output", signed)["txid"]
    assert not core.call("testmempoolaccept", [signed.read_text().strip()])[0]["allowed"]
    core.call("generatetoaddress", int(terms["height"]) - core.call("getblockcount"), miner)
    assert command("broadcast", *migration, "--signed-raw", signed)["txid"] == migration_txid
    core.call("generatetoaddress", 12, miner)
    bootstrap = env["bootstrap"]
    relay_headers(bootstrap.w3, relay, core)
    assert command("settle", *review, *rpc, *send, "--root", batch, "--txid", migration_txid)["status"] == "CONFIRMED"
    assert bootstrap.contract("withdrawal_recovery").functions.canRefund(bytes.fromhex(batch)).call()
    migrated, sats = _outpoint(core, migration_txid, terms["successor_script"])
    assert sats == 1_000_000
    return migrated


def _resume(tmp_path, env, core, miner, payer, terms, successor_member, batch, old_board, migrated, recipient, script, supply):
    command, review, send = _cold_commands(tmp_path, env)
    bootstrap = env["bootstrap"]
    w3 = bootstrap.w3
    pool, escrow, deposits, shares = [bootstrap.contract(name) for name in ("conf_pool", "escrow", "deposits", "conf_shares")]
    intent = tmp_path / "rotate.json"
    assert command("rotate-plan", *review, "--root", batch, "--wallet-id", terms["successor_dkg"]["wallet_id"],
                   "--intent", intent)["status"] == "BTC_RECOVERY_ROTATION_PREPARED"
    deadline = w3.eth.get_block("latest").timestamp + 3600
    proposed = command("rotate-propose", *review, *send, "--deadline", deadline, "--intent", intent)
    assert proposed["status"] == "AWAITING_PARTICIPANTS"
    sign(env, int(proposed["session"]))
    assert command("rotate-execute", *review, *send, "--session", proposed["session"],
                   "--intent", intent)["status"] == "CONFIRMED"
    active = command("inspect")
    assert active["active_script"] == terms["successor_script"] and not active["withdrawals_paused"]
    assert shares.functions.totalSupply().call() == supply and deposits.functions.backingCount().call() == 1

    def member(i, action, *args):
        return successor_member(i, action, "--recovery-board", env["board"], *args)

    for i in (1, 2):
        _pin(lambda action, *args: member(i, action, *args))
    new_board = member(1, "btc-deploy-board", "--max-packet-bytes", "4096")["signing_board"]
    assert new_board != old_board
    withdrawal = _request(env, pool, script, w3.eth.accounts[0])
    request = escrow.functions.requestKey(pool.address, withdrawal).call()
    due = pool.functions.getWithdrawal(withdrawal).call()[3] // SAT

    def participant(role, i, action, *args):
        if role == "btc_ceremony":
            return member(i, action, *args)
        return env["participant"](role, i, action, *args)

    post_batch, payout = bitcoin_payment(dict(env, participant=participant), core, miner, new_board,
                                         [migrated], request, local_members=(1, 2))
    migration_txid, vout = migrated.split(":")
    assert core.call("gettxout", migration_txid, int(vout)) is None
    paid = satoshis(payer.call("getreceivedbyaddress", recipient, 6))
    assert paid == due - 1000
    confirmations = core.call("getrawtransaction", payout["txid"], True)["confirmations"]
    assert confirmations >= 6
    _, change = _outpoint(core, payout["txid"], terms["successor_script"])
    assert change == deposits.functions.totalBackingSats().call() == 1_000_000 - due
    assert shares.functions.totalSupply().call() == supply - due * SAT == pool.functions.totalPrincipal().call()
    with pytest.raises(ContractLogicError):
        escrow.functions.settle(post_batch, *proof(core, payout["txid"])).call()
    return dict(successor_payout_txid=payout["txid"], successor_payout_confirmations=confirmations,
                successor_payout_sats=paid, successor_miner_fee_sats=due - paid,
                successor_backing_sats=change)
