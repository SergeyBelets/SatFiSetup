"""Real first DKG and Bitcoin payments with public traffic exclusively on chain.

Each installed CLI invocation runs in the respective friend's working directory.
Only the harness knows all test identities; no participant command receives another
participant's directory, a shared mailbox, manifest, plan or signature package.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_abi import encode
from web3 import Web3

from signer_agent.bootstrap import Bootstrap
from signer_agent.bootstrap_plan import build
from signer_agent.bitcoin_worker import BitcoinRPC
from signer_agent.secure_state import SecureState
from test_dkg_processes import RPC, deploy, tx
from test_bitcoin_processes import BTC_RPC, COOKIE
from lifecycle_helpers import contract, proof, relay_headers, committed_deposit


def _bootstrap(tmp_path):
    w3 = Web3(Web3.HTTPProvider(RPC))
    output_corpus = []
    command_arguments = []
    addresses = [Account.from_key(i.to_bytes(32, "big")).address for i in (1, 2, 3)]
    for a in addresses:
        w3.provider.make_request("anvil_setBalance", [a, hex(10**23)])

    # Exercise the installed entry point from separate working directories, as in
    # FIRST_DKG_ONCHAIN.md. No internal Bootstrap.apply calls drive the workflow.
    executable = str(Path(sys.executable).with_name("frost-signer"))
    homes = [tmp_path / ("terminal-" + str(i)) for i in range(3)]
    for home in homes:
        home.mkdir(mode=0o700)
    operator_rpc = RPC.replace("://", "://fixture-user:fixture-rpc-token@", 1)
    friend_rpcs = [RPC, RPC.replace("127.0.0.1", "localhost"), RPC + "/"]

    def command(group, *args, member=0, expected=(0,)):
        command_arguments.append([group, *map(str, args)])
        process = subprocess.run(
            [executable, group, *map(str, args)],
            cwd=homes[member],
            capture_output=True,
            text=True,
            timeout=300,
        )
        output_corpus.extend((process.stdout, process.stderr))
        assert process.returncode in expected, process.stdout + process.stderr
        return json.loads(process.stdout)

    def setup(*args, member=0):
        return command("setup", *args, member=member)

    identities = []
    for i in (1, 2, 3):
        path = homes[i - 1] / "identity.json"
        path.write_text(json.dumps({"private_key": "%064x" % i}))
        os.chmod(path, 0o600)
        identities.append(path)
    invite = homes[0] / "invitation.json"
    setup(
        "invite",
        "--participants",
        *addresses,
        "--threshold",
        "3",
        "--min-custodians",
        "2",
        "--min-p2wpkh-sats",
        "294",
        "--min-p2tr-sats",
        "330",
        "--treasury",
        addresses[2],
        "--bitcoin-confirmations", "6",
        "--wrap-fee-bps", "17", "--unwrap-fee-bps", "23",
        "--wrap-staker-bps", "1234", "--deploy-staker-bps", "6789",
        "--converter-numerator", "3", "--converter-denominator", "9999",
        "--converter-funding", "7654321",
        "--dao-token-recipient", addresses[1],
        "--dao-use-reserve", "true", "--integration-fee-exemption", "true",
        "--deployment-id",
        "bitcoin-onchain-e2e",
        "--output",
        invite
    )
    published = setup(
        "publish",
        "--rpc",
        operator_rpc,
        "--invite",
        invite,
        "--identity-file",
        identities[0],
        "--state",
        tmp_path / "invitation.sqlite",
    )
    board = published["board"]
    assert "evm_rpc" not in json.loads(invite.read_text())
    initial = setup("status", "--board", board, "--rpc", RPC)
    assert not any(initial["enrolled"].values())
    assert all(c["status"] == "NOT_STARTED" for c in initial["ceremonies"].values())
    # Each participant starts with code, RPC and the operator's known address only.
    for i in range(3):
        discovered = setup(
            "discover", "--operator", addresses[0], "--rpc", friend_rpcs[i], member=i
        )
        candidate = next(x for x in discovered["invitations"] if x["board"] == board)
        setup(
            "join-chain",
            "--board",
            candidate["board"],
            "--rpc",
            friend_rpcs[i],
            "--accept-fingerprint",
            candidate["fingerprint"],
            "--identity-file",
            identities[i],
            "--directory",
            homes[i] / "friend",
            member=i,
        )
    config_path = homes[0] / "profile.json"
    setup("assemble-chain", "--board", board, "--rpc", RPC, "--output", config_path)
    config = json.loads(config_path.read_text())
    assert config["evm_rpc"] == RPC  # Never inherits the operator's provider URL.
    profile = command("bootstrap", "plan", "--profile", config_path)
    assert profile == build(config)
    journal = tmp_path / "bootstrap.sqlite"
    identity = identities[0]

    def run_bootstrap(action):
        options = (
            ["--profile", config_path]
            if action == "apply"
            else ["--deployment", "bitcoin-onchain-e2e"]
        )
        return command(
            "bootstrap",
            action,
            *options,
            "--state",
            journal,
            "--identity-file",
            identity,
            expected=(0, 2)
        )

    def participant(role, i, action, *extra):
        directory = homes[i] / "friend"
        return command(
            "dkg",
            action,
            "--board",
            board,
            "--role",
            role,
            "--rpc",
            friend_rpcs[i],
            "--identity-file",
            identities[i],
            "--state",
            directory / (role + ".enc"),
            "--password-file",
            directory / "password",
            *extra,
            member=i
        )

    for _ in range(100):
        result = run_bootstrap("apply" if _ == 0 else "resume")
        if result["status"] == "VERIFIED":
            break
        step = next(s for s in profile["steps"] if s["id"] == result["step"])
        progress = setup("status", "--board", board, "--rpc", friend_rpcs[1], member=1)
        active_roles = [
            r for r, c in progress["ceremonies"].items() if c["status"] == "IN_PROGRESS"
        ]
        if active_roles:
            assert len(active_roles) == 3
            # All three independent sessions exist before any participant starts.
            # Each friend drives all roles with one command and isolated secrets.
            for __ in range(16):
                statuses = [command("flow", "dkg", "--once", "--board", board,
                    "--rpc", friend_rpcs[i], "--identity-file", identities[i],
                    "--directory", homes[i] / "friend",
                    member=i, expected=(0, 2))["status"] for i in range(3)]
                if statuses == ["ACTIVE"] * 3:
                    break
            assert statuses == ["ACTIVE"] * 3
        elif result["status"] == "AWAITING_PARTICIPANTS":
            pending = setup("pending", "--board", board, "--rpc", RPC)
            role = pending["intent"]["role"]
            options = ["--session", pending["session"]]
            # The share sweep retains its pre-submit snapshot; a third sweep
            # may finalize this same session before bootstrap can advance.
            for i in range(int(config[role]["threshold"])):
                status = participant(role, i, "sign-evm", *options)["status"]
                assert status in ("committed", "finalized"), (result["step"], pending, status)
            for i in range(int(config[role]["threshold"])):
                assert participant(role, i, "sign-evm", *options)["status"] in (
                    "signed",
                    "finalized",
                )
        elif result["status"] in ("CONFIRMING", "BROADCAST"):
            continue
        else:
            assert result["status"] == "AWAITING_CUSTODIAN_APPROVALS"
            pending = setup("pending", "--board", board, "--rpc", RPC)
            for i in (1, 2):
                options = [
                    "approve-custodian",
                    "--board",
                    board,
                    "--rpc",
                    RPC,
                    "--session",
                    pending["session"],
                    "--operation-hash",
                    pending["intent"]["operation_hash"],
                    "--identity-file",
                    identities[i],
                    "--state",
                    tmp_path / ("custodian-" + str(i) + ".sqlite"),
                ]
                assert setup(*options)["status"] == "CUSTODIAN_APPROVED"
                assert setup(*options)["status"] == "CUSTODIAN_APPROVED"

    else:
        raise AssertionError("bootstrap did not finish")
    assert run_bootstrap("verify")["status"] == "VERIFIED"
    final_progress = setup("status", "--board", board, "--rpc", RPC)
    assert all(
        c["status"] == "COMPLETED" for c in final_progress["ceremonies"].values()
    )
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        for i in range(3):
            assert participant(role, i, "verify")["status"] == "VERIFIED"
    bootstrap = Bootstrap(profile, journal)
    assert bootstrap.apply(verify=True)["status"] == "VERIFIED"
    return SimpleNamespace(
        w3=w3, board=board, addresses=addresses, homes=homes,
        identities=identities, config=config, bootstrap=bootstrap,
        command=command, participant=participant, output_corpus=output_corpus,
        command_arguments=command_arguments,
    )


def _state(env, member, role="btc_ceremony"):
    directory = env.homes[member] / "friend"
    return SecureState(
        str(directory / (role + ".enc")),
        (directory / "password").read_text().strip(),
    )


def _sign_session(env, session):
    options = ["--session", session, "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE]
    admission = env.bootstrap.contract("mint_admission")
    if (bytes(admission.functions.attempts(session).call()[0]) != bytes(32)
            or int(admission.functions.operatorTransfers(session).call()[0], 16) != 0):
        options += ["--mint-admission", admission.address]
    for member in range(3):
        assert env.participant(
            "conf_ceremony", member, "sign-evm", *options
        )["status"] == "committed"
    for member in range(3):
        assert env.participant(
            "conf_ceremony", member, "sign-evm", *options
        )["status"] in ("signed", "finalized")
    # Every signing call uses one confirmed snapshot. The last share becomes
    # visible on this next sweep; require actual finalization before authorizing.
    for member in range(3):
        assert env.participant(
            "conf_ceremony", member, "sign-evm", *options
        )["status"] == "finalized"


def _approve(env, target, kind, helper, args, *, custodian=False):
    """Publish typed public intent; all three approvals read it from the chain."""
    w3 = env.w3
    coordinator = env.bootstrap.contract("coordinator")
    authorization = env.bootstrap.contract("conf_auth")
    message = bytes(getattr(target.functions, helper)(*args).call())
    w3.provider.make_request("anvil_impersonateAccount", [env.addresses[0]])
    tx(w3, coordinator.functions.createSessionFromDKG(
        authorization.functions.dkgId().call(), message, target.address,
        w3.eth.get_block("latest").timestamp + 3600,
    ),sender=env.addresses[0])
    session = coordinator.functions.nextSessionId().call()

    def public(value):
        if isinstance(value, bytes):
            return "0x" + value.hex()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, (list, tuple)):
            return [public(item) for item in value]
        return value

    intent = dict(
        chain_id="31337", coordinator=coordinator.address,
        role="conf_ceremony", target=target.address,
        operation_hash=message.hex(), artifact=kind,
        hash_method=helper, hash_args=public(args),
    )
    w3.provider.make_request("anvil_impersonateAccount", [env.addresses[0]])
    board = contract(w3, "DKGOnchainBoard", env.board)
    tx(w3, board.functions.publishIntent(session, json.dumps(intent).encode()),
       sender=env.addresses[0])
    _sign_session(env, session)
    tx(w3, authorization.functions.authorizeOperation(session, message, target.address))
    if custodian:
        for member in (1, 2):
            env.command(
                "setup", "approve-custodian", "--board", env.board,
                "--rpc", RPC, "--session", session,
                "--operation-hash", message.hex(),
                "--identity-file", env.identities[member],
                "--state", env.homes[member] / "custodian.sqlite", member=member,
            )
    return message


def _secret_values(value):
    """Extract test-local secrets without ever printing them in failure output."""
    secrets = []
    if isinstance(value, dict):
        for name, item in value.items():
            if name in {"secret", "signing_share", "key_package", "nonces", "hiding", "binding"}:
                if isinstance(item, str) and len(item) >= 64:
                    secrets.append(item)
            secrets.extend(_secret_values(item))
    elif isinstance(value, list):
        for item in value:
            secrets.extend(_secret_values(item))
    return secrets


def _nonce_rows(env, member, batch):
    return {
        key: value for key, value in _state(env, member)._load()["sessions"].items()
        if key.startswith("btc:") and ":" + batch + ":" in key
    }


def test_first_dkg_then_bitcoin_payments_without_file_exchange(tmp_path, record_property):
    env = _bootstrap(tmp_path)
    w3 = env.w3
    assert w3.eth.chain_id == 31337
    btc = BitcoinRPC(BTC_RPC, COOKIE)
    btc.check_network()
    wallet_name = "onchain-" + os.urandom(5).hex()
    btc.call("createwallet", wallet_name)
    wallet = BitcoinRPC(BTC_RPC + "/wallet/" + wallet_name, COOKIE)
    mining_address = wallet.call("getnewaddress")
    btc.call("generatetoaddress", 101, mining_address)
    relay = env.bootstrap.contract("relay")
    mining = env.bootstrap.contract("mining")
    mp = env.bootstrap.contract("mp")
    pool = env.bootstrap.contract("conf_pool")
    batches = env.bootstrap.contract("batches")
    escrow = env.bootstrap.contract("escrow")
    deposits = env.bootstrap.contract("deposits")
    shares = contract(w3, "ConfederationToken", pool.functions.getShareToken().call())
    other_board = deploy(w3, "BitcoinSigningBoard", batches.address, 4096)
    dkg_id = bytes.fromhex(env.config["btc_ceremony"]["id"].removeprefix("0x"))
    dkg = env.bootstrap.contract("dkg")
    group = bytes(dkg.functions.getCompletedResult(dkg_id).call()[4]).hex()
    wallet_script = "5120" + group
    descriptor = btc.call("getdescriptorinfo", "rawtr(" + group + ")")["descriptor"]
    address = btc.call("deriveaddresses", descriptor)[0]
    relay_headers(w3, relay, btc)

    for member in range(3):
        reviewed = env.participant("btc_ceremony", member, "inspect-bitcoin-registry")
        assert reviewed["graph"]["contracts"]["batches"]["address"] == batches.address
        assert env.participant(
            "btc_ceremony", member, "trust-bitcoin-registry",
            "--accept-fingerprint", reviewed["fingerprint"],
        )["status"] == "BTC_REGISTRY_PINNED"

    deployed = env.participant("btc_ceremony", 0, "btc-deploy-board",
                               "--max-packet-bytes", "4096")
    signing_board = contract(w3, "BitcoinSigningBoard", deployed["signing_board"])
    before_deploy_retry = w3.eth.get_transaction_count(env.addresses[0])
    assert env.participant("btc_ceremony", 0, "btc-deploy-board",
                           "--max-packet-bytes", "4096")["signing_board"] == signing_board.address
    assert w3.eth.get_transaction_count(env.addresses[0]) == before_deploy_retry

    public_corpus = env.output_corpus
    secrets = []
    payments = []
    for recipient_type in ("bech32", "bech32m"):
        commitment = env.command("bitcoin", "deposit-commitment", "--chain-id", "31337",
                                 "--mining-pool", mining.address, "--vout", "0",
                                 "--recipient", w3.eth.accounts[0], "--amount", str(1_000_000 * 10**10))
        assert commitment["allocation_hash"] == bytes(mining.functions.getDepositCommitment(
            0, [w3.eth.accounts[0]], [1_000_000 * 10**10]).call()).hex()
        assert commitment["data"] == bytes(mining.functions.getDepositData(
            0, [w3.eth.accounts[0]], [1_000_000 * 10**10]).call()).hex()
        assert commitment["data"][8:48] == w3.eth.accounts[0][2:].lower()
        funded_txids = [committed_deposit(wallet, address, "0.01000000", commitment["data"]),
                       wallet.call("sendtoaddress", address, "0.00003000", "", "", False, True, None, "unset", None, "2")]
        btc.call("generatetoaddress", 6, mining_address)
        funding = [next(
            output for output in btc.call("getrawtransaction", txid, True)["vout"]
            if output["scriptPubKey"]["hex"] == wallet_script
        ) for txid in funded_txids]
        relay_headers(w3, relay, btc)
        from signer_agent.bitcoin_onchain_payment import protect_unminted_deposit
        committed_input = dict(outpoint=funded_txids[0] + ":" + str(funding[0]["n"]), script=wallet_script)
        with pytest.raises(ValueError, match="^BTC_UNMINTED_COMMITTED_DEPOSIT$"):
            protect_unminted_deposit(deposits, committed_input, btc, w3.eth.block_number)
        raw, block, siblings, index = proof(btc, funded_txids[0])
        amount = 1_000_000 * 10**10
        mint_options = ["--signing-board", signing_board.address, "--bitcoin-rpc", BTC_RPC,
                        "--bitcoin-cookie", COOKIE, "--txid", funded_txids[0], "--vout", funding[0]["n"],
                        "--recipient", w3.eth.accounts[0], "--amount", str(amount),
                        "--deadline", w3.eth.get_block("latest").timestamp + 3600]
        proposed_mint = env.participant("btc_ceremony", 0, "btc-mint-propose", *mint_options)
        _sign_session(env, int(proposed_mint["session"]))
        assert env.participant("btc_ceremony", 1, "btc-mint-execute", *mint_options)["status"] == "BTC_FUNDING_EXECUTED"
        mint_nonce = w3.eth.get_transaction_count(env.addresses[1])
        assert env.participant("btc_ceremony", 1, "btc-mint-execute", *mint_options)["status"] == "BTC_FUNDING_EXECUTED"
        assert w3.eth.get_transaction_count(env.addresses[1]) == mint_nonce
        protect_unminted_deposit(deposits, committed_input, btc, w3.eth.block_number)
        tx(w3, mp.functions.approve(pool.address, amount))
        tx(w3, pool.functions.deposit(mp.address, amount))
        recipient = wallet.call("getnewaddress", "", recipient_type)
        recipient_script = wallet.call("getaddressinfo", recipient)["scriptPubKey"]
        withdrawal_id = pool.functions.withdrawalCounter().call()
        requested_shares = 600_000 * 10**10 + 123
        share_supply = shares.functions.totalSupply().call()
        mp_supply = mp.functions.totalSupply().call()
        tx(w3, pool.functions.requestWithdrawal(
            mp.address, requested_shares, bytes.fromhex(recipient_script),
        ))
        withdrawal = pool.functions.getWithdrawal(withdrawal_id).call()
        payout = withdrawal[3]
        assert payout % 10**10 == 0
        _approve(env, pool, "ConfederationMultiPool", "getProcessWithdrawalOperationHash",
                 [withdrawal_id], custodian=True)
        tx(w3, pool.functions.processWithdrawal(withdrawal_id))
        request_key = escrow.functions.requestKey(pool.address, withdrawal_id).call()
        batch = os.urandom(32).hex()
        options = ["--signing-board", signing_board.address, "--batch", batch,
                   "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE]

        def bitcoin(member, action, *args):
            # Friends discover the selected batch and signing board from the DKG
            # board; the publisher never hands them identifiers or a plan file.
            selected = options if member == 0 else options[4:]
            return env.participant("btc_ceremony", member, action, *selected, *args)

        def payment_flow(member, actor, expected=(0,)):
            directory = env.homes[member] / "friend"
            return env.command("flow", "payment", "--once", "--actor", actor,
                "--board", env.board, "--rpc", RPC, "--identity-file", env.identities[member],
                "--state", directory / "btc_ceremony.enc", "--password-file", directory / "password",
                *options, member=member, expected=expected)

        prepared = bitcoin(0, "btc-prepare", "--input",
                           funded_txids[0] + ":" + str(funding[0]["n"]),
                           "--input", funded_txids[1] + ":" + str(funding[1]["n"]),
                           "--withdrawal", request_key.hex(), "--fee", "1000",
                           "--max-fee", "2000", "--deadline",
                           w3.eth.get_block("latest").timestamp + 3600,
                           "--signers", "1", "2", "3")
        plan = prepared["plan"]
        assert plan["version"] == "2" and plan["signers"] == ["1", "2", "3"]
        assert plan["outputs"][0] == dict(script=recipient_script, amount=str(payout // 10**10))
        assert sum(int(item["amount"]) for item in plan["outputs"]) == 1_002_000
        for operation in ("reserve", "bind"):
            proposed = bitcoin(0, "btc-propose", "--operation", operation)
            for member in (1, 2):
                discovered = env.participant("btc_ceremony", member, "btc-pending")
                assert discovered["batch"] == batch
                assert discovered["signing_board"] == signing_board.address
            _sign_session(env, int(proposed["session"]))
            # Execute on another machine: reserve must discover the public DKG
            # intent even before the Bitcoin board has a published plan.
            bitcoin(1 if operation == "reserve" else 2, "btc-execute", "--operation", operation)
            nonce_before_retry = w3.eth.get_transaction_count(env.addresses[0])
            assert env.participant("conf_ceremony", 0, "sign-evm", "--session", proposed["session"],
                                   "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE)["status"] == "ALREADY_EXECUTED"
            assert w3.eth.get_transaction_count(env.addresses[0]) == nonce_before_retry
            if operation == "reserve":
                bitcoin(0, "btc-publish-plan")

        assert batches.functions.batchState(bytes.fromhex(batch)).call() == 1
        assert shares.functions.totalSupply().call() == share_supply
        assert mp.functions.totalSupply().call() == mp_supply
        # Public discovery yields the exact plan, without any package argument.
        for member in range(3):
            assert bitcoin(member, "btc-status")["plan"] == plan
        # A canonical board for the same registry is still the wrong plan domain.
        before = _state(env, 1)._load()
        with pytest.raises(AssertionError):
            bitcoin(1, "btc-commit", "--signing-board", other_board.address)
        unchanged = _state(env, 1)._load() == before
        assert unchanged, "REJECTED_BOARD_CHANGED_PRIVATE_STATE"

        bitcoin(0, "btc-commit")
        first_nonce = _nonce_rows(env, 0, batch)
        assert len(first_nonce) == 2
        secrets.extend(_secret_values(first_nonce))
        before_tx_count = w3.eth.get_transaction_count(env.addresses[0])
        bitcoin(0, "btc-commit")
        unchanged = _nonce_rows(env, 0, batch) == first_nonce
        assert unchanged, "COMMIT_RETRY_CHANGED_NONCE"
        assert w3.eth.get_transaction_count(env.addresses[0]) == before_tx_count
        with pytest.raises(AssertionError):
            bitcoin(0, "btc-sign")
        unchanged = _nonce_rows(env, 0, batch) == first_nonce
        assert unchanged, "MISSING_COMMITMENTS_CHANGED_NONCE"
        for member in (1, 2):
            bitcoin(member, "btc-commit")
            secrets.extend(_secret_values(_nonce_rows(env, member, batch)))

        # The contract rejects conflicting retry bytes independently of CLI policy.
        original_packet = bytes(signing_board.functions.packet(
            bytes.fromhex(batch), 0, 0, env.addresses[0],
        ).call())
        assert original_packet
        with pytest.raises(Exception):
            signing_board.functions.publish(bytes.fromhex(batch), 0, 0, b"conflict").call(
                {"from": env.addresses[0]})
        assert bytes(signing_board.functions.packet(
            bytes.fromhex(batch), 0, 0, env.addresses[0],
        ).call()) == original_packet
        bitcoin(0, "btc-sign")
        with pytest.raises(AssertionError):
            bitcoin(0, "btc-broadcast")
        for member in (1, 2):
            assert payment_flow(member, "signer")["status"] == "SIGNED"
        for member in range(3):
            signed_rows = _nonce_rows(env, member, batch)
            assert all(row["status"] == "share-prepared" and "nonces" not in row
                       for row in signed_rows.values())
            before_tx_count = w3.eth.get_transaction_count(env.addresses[member])
            bitcoin(member, "btc-sign")
            unchanged = _nonce_rows(env, member, batch) == signed_rows
            assert unchanged, "SIGN_RETRY_CHANGED_PRIVATE_STATE"
            assert w3.eth.get_transaction_count(env.addresses[member]) == before_tx_count
            for input_index in range(2):
                for kind in (0, 1):
                    payload = bytes(signing_board.functions.packet(
                        bytes.fromhex(batch), input_index, kind, env.addresses[member],
                    ).call()).decode()
                    public_corpus.append(payload)

        if recipient_type == "bech32m":
            # Delivering an already prepared packet must not become dependent on
            # Core availability or on a deadline that limits creation of new shares.
            w3.provider.make_request("evm_setNextBlockTimestamp", [int(plan["deadline"]) + 1])
            w3.provider.make_request("evm_mine", [])
            assert w3.eth.get_block("latest").timestamp > int(plan["deadline"])
            cached_rows = _nonce_rows(env, 0, batch)
            before_tx_count = w3.eth.get_transaction_count(env.addresses[0])
            missing_cookie = env.homes[0] / "nonexistent-bitcoin-cookie"
            assert not missing_cookie.exists()
            for action in ("btc-commit", "btc-sign"):
                bitcoin(0, action, "--bitcoin-cookie", missing_cookie)
                unchanged = _nonce_rows(env, 0, batch) == cached_rows
                assert unchanged, "CACHED_DELIVERY_CHANGED_PRIVATE_STATE"
                assert w3.eth.get_transaction_count(env.addresses[0]) == before_tx_count

        sent = bitcoin(2, "btc-broadcast")
        txid = sent["txid"]
        assert bitcoin(2, "btc-broadcast")["txid"] == txid
        assert bitcoin(0, "btc-broadcast")["txid"] == txid
        assert txid in btc.call("getrawmempool")
        if recipient_type == "bech32m":
            for item in plan["inputs"]:
                previous_txid, vout = item["outpoint"].split(":")
                assert btc.call("gettxout", previous_txid, int(vout), True) is None
            cached_rows = _nonce_rows(env, 1, batch)
            before_tx_count = w3.eth.get_transaction_count(env.addresses[1])
            bitcoin(1, "btc-sign")
            unchanged = _nonce_rows(env, 1, batch) == cached_rows
            assert unchanged, "SPENT_INPUT_RETRY_CHANGED_PRIVATE_STATE"
            assert w3.eth.get_transaction_count(env.addresses[1]) == before_tx_count
        with pytest.raises(AssertionError):
            bitcoin(1, "btc-settle")
        assert mp.functions.totalSupply().call() == mp_supply
        btc.call("generatetoaddress", 6, mining_address)
        settled = payment_flow(1, "operator")
        assert settled["txid"] == txid
        assert shares.functions.totalSupply().call() == share_supply - requested_shares
        assert mp.functions.totalSupply().call() == mp_supply - payout
        assert escrow.functions.payment(bytes.fromhex(batch)).call()[2] is True
        assert mp.functions.balanceOf(escrow.address).call() == 0
        supply_after = mp.functions.totalSupply().call()
        bitcoin(0, "btc-settle")
        assert mp.functions.totalSupply().call() == supply_after
        decoded = btc.call("getrawtransaction", txid, True)
        assert decoded["vout"][0]["scriptPubKey"]["hex"] == recipient_script
        assert int(decoded["vout"][0]["value"] * 100_000_000) == payout // 10**10
        change_key = Web3.keccak(encode(["bytes32", "uint32"], [bytes.fromhex(txid)[::-1], 1]))
        assert deposits.functions.backingSats(change_key).call() == 1_000_000 - payout // 10**10
        payments.append(dict(recipient_type=recipient_type, batch=batch, txid=txid,
                             principal_burned_sats=str(payout // 10**10)))

    assert bitcoin(1, "btc-status")["status"] == "PAID"
    paid_block = btc.call("getrawtransaction", payments[-1]["txid"], True)["blockhash"]
    btc.call("invalidateblock", paid_block)
    try:
        assert bitcoin(1, "btc-status")["status"] == "OBSERVATION_UNAVAILABLE"
        # A replacement at the payment height establishes a conflicting hash.
        btc.call("generatetoaddress", 1, mining_address)
        assert bitcoin(1, "btc-status")["status"] == "RECOVERY_REQUIRED"
        for action in ("btc-commit", "btc-sign", "btc-broadcast"):
            with pytest.raises(AssertionError, match="PAYMENTS_PAUSED_FOR_RECOVERY"):
                bitcoin(1, action)
        # EVM token burns cannot be reversed by a later Bitcoin reorganization.
        assert escrow.functions.payment(bytes.fromhex(batch)).call()[2] is True
        assert mp.functions.totalSupply().call() == supply_after
    finally:
        btc.call("reconsiderblock", paid_block)
    assert bitcoin(1, "btc-status")["status"] == "RECOVERY_REQUIRED"
    recovered = bitcoin(1, "btc-recover")
    assert recovered["status"] == "PAYMENTS_RESUMED"
    assert recovered["txid"] == payments[-1]["txid"]
    assert bitcoin(1, "btc-status")["status"] == "PAID"
    assert bitcoin(1, "btc-recover")["status"] == "PAYMENTS_NOT_PAUSED"
    assert mp.functions.totalSupply().call() == supply_after

    # Include invitation, on-chain DKG traffic and all CLI diagnostics in the scan.
    dkg_board = contract(w3, "DKGOnchainBoard", env.board)
    public_corpus.append(bytes(dkg_board.functions.invitation().call()).decode())
    envelopes = 0
    for role in ("evm_ceremony", "conf_ceremony", "btc_ceremony"):
        session = bytes.fromhex(env.config[role]["id"].removeprefix("0x"))
        for member, sender in enumerate(env.addresses):
            secrets.extend(_secret_values(_state(env, member, role)._load()))
            for kind in (0, 1):
                public_corpus.append(bytes(dkg_board.functions.packet(
                    session, kind, sender, "0x" + "00" * 20,
                ).call()).decode())
            for recipient in env.addresses:
                if recipient != sender:
                    packet = bytes(dkg_board.functions.packet(session, 2, sender, recipient).call()).decode()
                    assert set(json.loads(packet)) == {"envelope", "hash", "signature"}
                    public_corpus.append(packet)
                    envelopes += 1
    corpus = "\n".join(public_corpus)
    boundary_passed = bool(secrets) and not any(secret in corpus for secret in secrets)
    assert boundary_passed, "SECRET_BOUNDARY_FAILED"
    assert "fixture-rpc-token" not in corpus
    assert not list(tmp_path.rglob("plan.json"))  # Plans live in encrypted local state.
    assert all(not {"--package", "--manifest", "--mailbox"}.intersection(args)
               for args in env.command_arguments)
    record_property("bitcoin_onchain_lifecycle", json.dumps(dict(
        payments=payments, separate_working_directories=3, threshold="3/3",
        hpke_envelopes=envelopes, public_bitcoin_packets=24,
        local_secret_values_checked=len(secrets), secret_values_published=False,
        participant_package_arguments=0, shared_mailboxes=0,
        settled_bitcoin_reorg_blocks_new_signatures=True,
        cached_delivery_after_deadline_and_spent_input=True,
    )))
