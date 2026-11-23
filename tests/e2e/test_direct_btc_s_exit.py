"""Local audit witness: ordinary direct V3 deposit followed by a plain S redemption."""
import json

from eth_account import Account
from signer_agent.bitcoin_rpc import BitcoinRPC, satoshis
from lifecycle_helpers import relay_headers
from test_bitcoin_processes import BTC_RPC, COOKIE
from test_custody_transfer import approve, bitcoin_payment, sign, wallet_address
from test_dkg_processes import RPC, tx
from test_full_bootstrap import exercise_bootstrap

Q = 10**10


def test_direct_btc_s_btc_without_pass(tmp_path, record_property):
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    env = exercise_bootstrap(
        deployment, record_property, return_context=True,
        custody=dict(version="1", mode="custodians", threshold="2", sources=[]),
        in_kind=dict(version="3", mode="create", bitcoin_backend="relay",
                     candidate_threshold_wei=str(200_000 * 10**18), default_miner_fee_sats="1000"),
    )
    b = env["bootstrap"]
    w3 = b.w3
    owner = Account.from_key((7).to_bytes(32, "big"))
    identity = tmp_path / "owner.json"
    identity.write_text(json.dumps({"private_key": "0x" + owner.key.hex().removeprefix("0x")}))
    identity.chmod(0o600)
    assert "error" not in w3.provider.make_request("anvil_setBalance", [owner.address, hex(10**22)])
    assert "error" not in w3.provider.make_request("anvil_impersonateAccount", [owner.address])
    manifest = tmp_path / "verified.json"
    manifest.write_text(json.dumps(env["manifest"]))

    core = BitcoinRPC(BTC_RPC, COOKIE)
    core.call("createwallet", "direct-audit")
    wallet = BitcoinRPC(BTC_RPC + "/wallet/direct-audit", COOKIE)
    miner = wallet.call("getnewaddress", "", "bech32")
    core.call("generatetoaddress", 101, miner)
    relay = b.contract("relay")
    relay_headers(w3, relay, core)
    for member in (1, 2):
        reviewed = env["participant"]("btc_ceremony", member, "inspect-bitcoin-registry")
        env["participant"]("btc_ceremony", member, "trust-bitcoin-registry", "--accept-fingerprint", reviewed["fingerprint"])
    board = env["participant"]("btc_ceremony", 1, "btc-deploy-board", "--max-packet-bytes", "4096")["signing_board"]

    reserve = b.contract("mining")
    destination = wallet_address(core, reserve.functions.payoutScript().call())
    gross_sats = 1_000_000
    gross = gross_sats * Q
    commitment = env["command"]("bitcoin", "deposit-commitment", "--chain-id", "31337",
        "--mining-pool", reserve.address, "--vout", "0", "--recipient", owner.address, "--amount", str(gross))
    raw = wallet.call("createrawtransaction", [], [{destination: "0.01000000"}, {"data": commitment["data"]}], 0, False)
    funded = wallet.call("fundrawtransaction", raw, {"changePosition": 2, "fee_rate": 2, "replaceable": False})
    signed = wallet.call("signrawtransactionwithwallet", funded["hex"])
    assert signed["complete"]
    decoded = core.call("decoderawtransaction", signed["hex"])
    deposit_txid = decoded["txid"]
    payment = tmp_path / "deposit.hex"
    payment.write_text(signed["hex"])
    payment.chmod(0o600)

    def dao(action, *args):
        return env["command"]("dao", "in-kind", action, "--manifest", manifest, "--rpc", RPC,
                              "--confirmations", "1", *args)
    sender = ["--identity-file", identity, "--state", tmp_path / "owner.sqlite", "--send"]
    proof_input = ["--txid", deposit_txid, "--vout", "0", "--satoshis", str(gross_sats),
                   "--minimum-s", str(998000 * Q), "--raw-tx-file", payment,
                   "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE]
    quote = dao("deposit-quote", *proof_input, "--sender", owner.address, *sender, "--id", "quote")
    assert quote["status"] == "VERIFIED", quote
    preflight = dao("deposit-preflight", *proof_input, "--recipient", owner.address)
    assert preflight["status"] == "PREFLIGHT_VERIFIED", preflight
    assert core.call("sendrawtransaction", signed["hex"]) == deposit_txid
    core.call("generatetoaddress", 6, miner)
    relay_headers(w3, relay, core)
    assert core.call("getrawtransaction", deposit_txid, True)["confirmations"] == 6
    mint = ["--signing-board", board, "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE,
            "--txid", deposit_txid, "--vout", "0", "--recipient", owner.address, "--amount", str(gross),
            "--deadline", w3.eth.get_block("latest").timestamp + 3600]
    proposal = env["participant"]("btc_ceremony", 1, "btc-mint-propose", *mint)
    sign(env, int(proposal["session"]))
    assert env["participant"]("btc_ceremony", 1, "btc-mint-execute", *mint)["status"] == "BTC_FUNDING_EXECUTED"
    shares = b.contract("conf_shares")
    dao_income = b.contract("dao_income")
    locked = shares.functions.balanceOf(owner.address).call()
    assert locked == 998300 * Q
    assert shares.functions.balanceOf(dao_income.address).call() == 1700 * Q
    deposits = b.contract("deposits")
    assert deposits.functions.totalBackingSats().call() == gross_sats

    recipient = wallet.call("getnewaddress", "", "bech32")
    script = wallet.call("getaddressinfo", recipient)["scriptPubKey"]
    result = dao("withdraw", "--amount", str(locked), "--script", script, "--miner-fee-cap-sats", "1000",
                 "--minimum-btc-sats", "995003", "--deadline", w3.eth.get_block("latest").timestamp + 3600,
                 *sender, "--id", "exit")
    assert result["status"] == "VERIFIED", result
    pool = b.contract("conf_pool")
    idx = pool.functions.withdrawalCounter().call() - 1
    approve(env, pool, "ConfederationPoolV2", "getProcessWithdrawalOperationHash", [idx], custodians=True)
    tx(w3, pool.functions.processWithdrawal(idx))
    escrow = b.contract("escrow")
    request = escrow.functions.requestKey(pool.address, idx).call()
    batch, response = bitcoin_payment(env, core, miner, board, [deposit_txid + ":0"], request,
                                      local_members=(1, 2), resume_expired=True)
    actual = core.call("getrawtransaction", response["txid"], True)
    paid = sum(satoshis(o["value"]) for o in actual["vout"] if o["scriptPubKey"]["hex"] == script)
    change = sum(satoshis(o["value"]) for o in actual["vout"] if bytes.fromhex(o["scriptPubKey"]["hex"]) == reserve.functions.payoutScript().call())
    fee = gross_sats - sum(satoshis(o["value"]) for o in actual["vout"])
    assert actual["confirmations"] == 6
    assert (paid, fee, change) == (995003, 1000, 3997)
    assert fee == escrow.functions.minerFee(batch).call()
    assert shares.functions.balanceOf(owner.address).call() == 0
    assert shares.functions.totalSupply().call() == shares.functions.balanceOf(dao_income.address).call() == change * Q
    assert deposits.functions.totalBackingSats().call() == change
    record_property("direct_v3_btc_accounting", json.dumps(dict(
        depth=actual["confirmations"], deposit_sats=gross_sats, deposit_miner_fee_sats=satoshis(funded["fee"]),
        payout_sats=paid, payout_miner_fee_sats=fee, reserve_sats=change, protocol_wrap_sats=1700, no_nft=True)))
