"""Actual bootstrap, isolated participant CLIs and public admission failover."""
import os

from signer_agent.bitcoin_rpc import BitcoinRPC
from test_bitcoin_onchain import _bootstrap, _sign_session
from test_bitcoin_processes import BTC_RPC, COOKIE
from lifecycle_helpers import contract, relay_headers, committed_deposit


def test_canonical_mint_and_operator_failover_across_participant_homes(tmp_path, record_property):
    env = _bootstrap(tmp_path)
    w3 = env.w3
    admission = env.bootstrap.contract("mint_admission")
    mining = env.bootstrap.contract("mining")
    coordinator = env.bootstrap.contract("coordinator")
    transport = contract(w3, "DKGOnchainBoard", env.board)
    assert mining.functions.mintAdmission().call() == admission.address
    assert admission.functions.operator().call() == env.addresses[0]
    btc = BitcoinRPC(BTC_RPC, COOKIE)
    name = "mint-admission-" + os.urandom(5).hex()
    btc.call("createwallet", name)
    wallet = BitcoinRPC(BTC_RPC + "/wallet/" + name, COOKIE)
    reward = wallet.call("getnewaddress")
    btc.call("generatetoaddress", 101, reward)
    dkg_id = bytes.fromhex(env.config["btc_ceremony"]["id"].removeprefix("0x"))
    group = bytes(env.bootstrap.contract("dkg").functions.getCompletedResult(dkg_id).call()[4]).hex()
    descriptor = btc.call("getdescriptorinfo", "rawtr(" + group + ")")["descriptor"]
    destination = btc.call("deriveaddresses", descriptor)[0]
    for member in range(3):
        reviewed = env.participant("btc_ceremony", member, "inspect-bitcoin-registry")
        assert env.participant("btc_ceremony", member, "trust-bitcoin-registry",
            "--accept-fingerprint", reviewed["fingerprint"])["status"] == "BTC_REGISTRY_PINNED"
    signing_board = env.participant("btc_ceremony", 0, "btc-deploy-board", "--max-packet-bytes", "4096")["signing_board"]

    def funding():
        amount = 1_000_000 * 10**10
        commitment = mining.functions.getDepositData(0, [w3.eth.accounts[0]], [amount]).call()
        txid = committed_deposit(wallet, destination, "0.01000000", bytes(commitment).hex())
        btc.call("generatetoaddress", 6, reward)
        relay_headers(w3, env.bootstrap.contract("relay"), btc)
        output = next(row for row in btc.call("getrawtransaction", txid, True)["vout"]
            if row["scriptPubKey"]["hex"] == "5120" + group)
        return ["--signing-board", signing_board, "--bitcoin-rpc", BTC_RPC,
            "--bitcoin-cookie", COOKIE, "--txid", txid, "--vout", output["n"],
            "--recipient", w3.eth.accounts[0], "--amount", amount,
            "--deadline", w3.eth.get_block("latest").timestamp + 3600]

    first_args = funding()
    first = env.participant("btc_ceremony", 0, "btc-mint-propose", *first_args)
    session = int(first["session"])
    assert coordinator.functions.getSession(session).call()[1] == admission.address
    assert bytes(transport.functions.intents(session).call()) == b""
    session_count = coordinator.functions.nextSessionId().call()
    nonce = w3.eth.get_transaction_count(env.addresses[1])
    adopted = env.participant("btc_ceremony", 1, "btc-mint-propose", *first_args)
    assert adopted["session"] == first["session"]
    assert coordinator.functions.nextSessionId().call() == session_count
    assert w3.eth.get_transaction_count(env.addresses[1]) == nonce

    # A different enrolled friend proposes operator failover without the original
    # operator's transport privileges. The published typed source is admission.
    transfer = env.participant("btc_ceremony", 2, "btc-mint-transfer-propose",
        "--mint-admission", admission.address, "--new-operator", env.addresses[1],
        "--deadline", w3.eth.get_block("latest").timestamp + 3600)
    transfer_session = int(transfer["session"])
    assert bytes(transport.functions.intents(transfer_session).call()) == b""
    _sign_session(env, transfer_session)
    transfer_args = ["--mint-admission", admission.address, "--session", transfer_session]
    assert env.participant("btc_ceremony", 1, "btc-mint-transfer-execute", *transfer_args)["status"] == "BTC_MINT_OPERATOR_TRANSFERRED"
    assert admission.functions.operator().call() == env.addresses[1]
    assert transport.functions.operator().call() == env.addresses[0]
    assert mining.functions.nonce().call() == 0
    before_retry = w3.eth.get_transaction_count(env.addresses[1])
    assert env.participant("btc_ceremony", 1, "btc-mint-transfer-execute", *transfer_args)["status"] == "BTC_MINT_OPERATOR_TRANSFERRED"
    assert w3.eth.get_transaction_count(env.addresses[1]) == before_retry

    # Immutable existing claim survives operator transfer; a third host executes.
    _sign_session(env, session)
    assert env.participant("btc_ceremony", 2, "btc-mint-execute", *first_args)["status"] == "BTC_FUNDING_EXECUTED"
    assert env.participant("conf_ceremony", 2, "sign-evm", "--session", session,
        "--mint-admission", admission.address)["status"] == "ALREADY_EXECUTED"
    old_claim = admission.functions.attempts(session).call()

    # A new request is opened by the replacement operator, independently of the
    # immutable DKG transport operator, then executed from another participant.
    second_args = funding()
    second = env.participant("btc_ceremony", 1, "btc-mint-propose", *second_args)
    second_session = int(second["session"])
    assert second_session != session
    assert bytes(transport.functions.intents(second_session).call()) == b""
    _sign_session(env, second_session)
    assert env.participant("btc_ceremony", 0, "btc-mint-execute", *second_args)["status"] == "BTC_FUNDING_EXECUTED"
    assert admission.functions.attempts(session).call() == old_claim
    assert mining.functions.nonce().call() == 2
    record_property("mint_sessions", 2)
    record_property("operator_transfer_sessions", 1)
    record_property("isolated_participant_homes", 3)
    record_property("legacy_transport_operator_unchanged", True)
