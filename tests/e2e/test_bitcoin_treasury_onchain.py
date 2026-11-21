"""Three real participants withdraw a 600-sat surplus through on-chain transport."""
import os
from signer_agent.bitcoin_worker import BitcoinRPC
from test_bitcoin_processes import BTC_RPC, COOKIE
from test_bitcoin_onchain import _bootstrap, _sign_session
from lifecycle_helpers import relay_headers


def test_unminted_treasury_uses_same_board_signing_and_spv_settlement(tmp_path):
    env = _bootstrap(tmp_path)
    w3 = env.w3
    btc = BitcoinRPC(BTC_RPC, COOKIE)
    wallet_name = "treasury-" + os.urandom(5).hex()
    btc.call("createwallet", wallet_name)
    wallet = BitcoinRPC(BTC_RPC + "/wallet/" + wallet_name, COOKIE)
    mining_address = wallet.call("getnewaddress")
    btc.call("generatetoaddress", 101, mining_address)
    relay = env.bootstrap.contract("relay")
    escrow = env.bootstrap.contract("escrow")
    deposits = env.bootstrap.contract("deposits")
    mp = env.bootstrap.contract("mp")
    mining = env.bootstrap.contract("mining")
    script = bytes(mining.functions.payoutScript().call()).hex()
    descriptor = btc.call("getdescriptorinfo", "rawtr(" + script[4:] + ")")["descriptor"]
    wallet_address = btc.call("deriveaddresses", descriptor)[0]
    funded = wallet.call("sendtoaddress", wallet_address, "0.00000600", "", "", False, True, None, "unset", None, "2")
    btc.call("generatetoaddress", 6, mining_address)
    relay_headers(w3, relay, btc)
    output = next(item for item in btc.call("getrawtransaction", funded, True)["vout"] if item["scriptPubKey"]["hex"] == script)
    for member in range(3):
        reviewed = env.participant("btc_ceremony", member, "inspect-bitcoin-registry")
        env.participant("btc_ceremony", member, "trust-bitcoin-registry", "--accept-fingerprint", reviewed["fingerprint"])
    board = env.participant("btc_ceremony", 0, "btc-deploy-board", "--max-packet-bytes", "4096")["signing_board"]
    recipient = wallet.call("getnewaddress", "", "bech32")
    destination = wallet.call("getaddressinfo", recipient)["scriptPubKey"]
    common = ["--signing-board", board, "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE]
    terms = ["--request-id", "17", "--treasury-owner", env.addresses[0], "--satoshis", "400",
             "--destination-script", destination, "--deadline", w3.eth.get_block("latest").timestamp + 3600]
    proposed = env.participant("btc_ceremony", 0, "btc-treasury-propose", *common, *terms)
    _sign_session(env, int(proposed["session"]))
    requested = env.participant("btc_ceremony", 1, "btc-treasury-execute", *common, *terms)
    assert requested["status"] == "BTC_FUNDING_EXECUTED"
    assert env.participant("btc_ceremony", 2, "btc-treasury-execute", *common, *terms)["withdrawal_ref"] == requested["withdrawal_ref"]
    batch = os.urandom(32).hex()
    options = [*common, "--batch", batch]
    prepared = env.participant("btc_ceremony", 0, "btc-prepare", *options,
        "--input", funded + ":" + str(output["n"]), "--withdrawal", requested["withdrawal_ref"],
        "--fee", "200", "--max-fee", "200", "--deadline", w3.eth.get_block("latest").timestamp + 3600,
        "--signers", "1", "2", "3")
    assert prepared["plan"]["outputs"] == [dict(amount="400", script=destination)]
    for operation in ("reserve", "bind"):
        proposal = env.participant("btc_ceremony", 0, "btc-propose", *options, "--operation", operation)
        _sign_session(env, int(proposal["session"]))
        env.participant("btc_ceremony", 1, "btc-execute", *options, "--operation", operation)
        if operation == "reserve":
            env.participant("btc_ceremony", 0, "btc-publish-plan", *options)
    assert mp.functions.totalSupply().call() == 0
    for action in ("btc-commit", "btc-sign"):
        for member in range(3):
            env.participant("btc_ceremony", member, action, *options)
    payment = env.participant("btc_ceremony", 0, "btc-broadcast", *options)
    btc.call("generatetoaddress", 6, mining_address)
    relay_headers(w3, relay, btc)
    env.participant("btc_ceremony", 1, "btc-settle", *options)
    assert escrow.functions.payment(bytes.fromhex(batch)).call()[2]
    assert mp.functions.totalSupply().call() == 0
    paid = btc.call("getrawtransaction", payment["txid"], True)["vout"]
    assert len(paid) == 1 and paid[0]["scriptPubKey"]["hex"] == destination
    assert int(paid[0]["value"] * 100_000_000) == 400
    from eth_abi import encode
    from eth_utils import keccak
    spent = keccak(encode(["bytes32", "uint32"], [bytes.fromhex(funded)[::-1], output["n"]]))
    assert deposits.functions.walletOutput(spent).call()
    assert deposits.functions.backingSats(spent).call() == 0
