"""Two independent native FROST wallets; real Core handover and custodian-only exit."""
import json
import os
import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.logs import DISCARD
from signer_agent.bitcoin_rpc import BitcoinRPC, satoshis
from signer_agent.bootstrap import Bootstrap
from test_full_bootstrap import exercise_bootstrap
from test_dkg_processes import RPC, tx
from test_bitcoin_processes import BTC_RPC, COOKIE
from lifecycle_helpers import contract, relay_headers, committed_deposit


def sign(env, session):
    roster = env["config"]["conf_ceremony"]["participants"]
    members = [i for i, address in enumerate(env["addresses"]) if address in roster]
    extra = []
    admission = env["bootstrap"].contract("mint_admission")
    if (bytes(admission.functions.attempts(session).call()[0]) != bytes(32)
            or int(admission.functions.operatorTransfers(session).call()[0], 16) != 0):
        extra = ["--mint-admission", admission.address]
    for sweep in range(3):
        for member in members:
            status = env["participant"]("conf_ceremony", member, "sign-evm", "--session", str(session),
                "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE, *extra)["status"]
            assert status in ("committed", "signed", "finalized", "ALREADY_EXECUTED"), status


def approve(env, target, kind, helper, values, custodians=False):
    b = env["bootstrap"]; w3 = b.w3
    coordinator, auth = b.contract("coordinator"), b.contract("conf_auth")
    op = getattr(target.functions, helper)(*values).call()
    publisher = 1 if "bitcoin_custody" in env["config"] else 0
    w3.provider.make_request("anvil_impersonateAccount", [env["addresses"][publisher]])
    receipt = tx(w3, coordinator.functions.createSessionFromDKG(bytes.fromhex(env["config"]["conf_ceremony"]["id"][2:]),
        op, target.address, w3.eth.get_block("latest").timestamp + 3600),sender=env["addresses"][publisher])
    session = coordinator.events.SessionCreated().process_receipt(receipt)[0]["args"]["sessionId"]
    intent = dict(chain_id="31337", coordinator=coordinator.address, role="conf_ceremony", target=target.address,
        operation_hash=op.hex(), artifact=kind, hash_method=helper, hash_args=Bootstrap.wire(values), session=str(session))
    board = contract(w3, "DKGOnchainBoard", env["board"])
    tx(w3, board.functions.publishIntent(session, json.dumps(intent).encode()), sender=env["addresses"][publisher])
    sign(env, session)
    tx(w3, auth.functions.authorizeOperation(session, op, target.address))
    if custodians:
        for member in (1,2):
            assert env["setup"]("approve-custodian", "--board", env["board"], "--rpc", RPC,
                "--session", str(session), "--operation-hash", op.hex(), "--identity-file", env["identities"][member],
                "--state", env["homes"][member] / "custodian.sqlite", member=member)["status"] == "CUSTODIAN_APPROVED"
    return op


def wallet_address(btc, script):
    group = bytes(script)[2:].hex()
    descriptor = btc.call("getdescriptorinfo", "rawtr(" + group + ")")["descriptor"]
    return btc.call("deriveaddresses", descriptor)[0]


def bitcoin_payment(env, core, miner, signing_board, inputs, request, *, local_members):
    owner = local_members[0]
    batch = os.urandom(32).hex()
    options = ["--signing-board", signing_board, "--batch", batch,
        "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE]
    def call(member, action, *extra):
        return env["participant"]("btc_ceremony", member, action, *options, *extra)
    arguments = []
    for outpoint in inputs: arguments += ["--input", outpoint]
    prepared = call(owner, "btc-prepare", *arguments, "--withdrawal", request.hex(), "--fee", "1000",
        "--max-fee", "2000", "--deadline", str(env["bootstrap"].w3.eth.get_block("latest").timestamp + 3600),
        "--signers", *map(str, range(1, len(local_members) + 1)))
    assert prepared["status"] == "BTC_PLAN_PREPARED", prepared
    for operation in ("reserve", "bind"):
        proposal = call(owner, "btc-propose", "--operation", operation)
        sign(env, int(proposal["session"]))
        call(owner, "btc-execute", "--operation", operation)
        if operation == "reserve": call(owner, "btc-publish-plan")
    for member in local_members: assert call(member, "btc-commit")["status"] == "COMMITTED"
    for member in local_members: assert call(member, "btc-sign")["status"] == "SIGNED"
    result = call(owner, "btc-broadcast")
    assert result["status"] == "BROADCAST", result
    core.call("generatetoaddress", 6, miner)
    relay_headers(env["bootstrap"].w3, env["bootstrap"].contract("relay"), core)
    settled = call(owner, "btc-settle")
    assert settled["status"] in ("PAID", "BTC_PAYMENT_SETTLED"), settled
    return bytes.fromhex(batch), result


@pytest.mark.parametrize("native_s,use_pass", [(False, False), (True, False), (True, True)],
                         ids=["legacy", "native-s", "native-s-pass"])
def test_transfer_then_exit_without_either_federation_representative(tmp_path, record_property, native_s, use_pass):
    one = tmp_path / "source"; one.mkdir()
    source = exercise_bootstrap(one, record_property, participant_keys=(1,2,3), return_context=True)
    b = source["bootstrap"]; w3 = b.w3
    exported = one / "source.json"
    source["command"]("custody-transfer", "source", "--rpc", RPC, "--mining", b.contract("mining").address,
        "--confirmations", "1", "--output", exported)
    descriptor = json.loads(exported.read_text())
    two = tmp_path / "destination"; two.mkdir()
    policy = dict(version="3", mode="create", bitcoin_backend="relay",
                  candidate_threshold_wei=str(200_000 * 10**18), default_miner_fee_sats="1000")
    destination = exercise_bootstrap(two, record_property, participant_keys=(4,5,6),
        custody=dict(version="1", mode="custodians", threshold="2", sources=[descriptor]),
        in_kind=policy if native_s else None)
    d = destination["bootstrap"]
    assert d.contract("conf_auth").functions.getParticipants().call() == destination["addresses"][1:]
    assert d.contract("dkg").functions.getSessionParticipants(bytes.fromhex(destination["config"]["btc_ceremony"]["id"][2:])).call() == destination["addresses"][1:]
    assert bytes(b.contract("mining").functions.payoutScript().call()) != bytes(d.contract("mining").functions.payoutScript().call())
    owner = Account.from_key((7).to_bytes(32, "big"))
    owner_file = tmp_path / "owner.json"; owner_file.write_text(json.dumps(dict(private_key="%064x" % 7))); owner_file.chmod(0o600)
    w3.provider.make_request("anvil_setBalance", [owner.address, hex(10**22)])
    w3.provider.make_request("anvil_impersonateAccount", [owner.address])
    core = BitcoinRPC(BTC_RPC, COOKIE)
    wallet_name = "custody-" + os.urandom(5).hex()
    core.call("createwallet", wallet_name)
    wallet = BitcoinRPC(BTC_RPC + "/wallet/" + wallet_name, COOKIE)
    miner = wallet.call("getnewaddress")
    core.call("generatetoaddress", 101, miner)
    for env in (source, destination):
        relay_headers(w3, env["bootstrap"].contract("relay"), core)
        indices = (0,1,2) if env is source else (1,2)
        for member in indices:
            reviewed = env["participant"]("btc_ceremony", member, "inspect-bitcoin-registry")
            env["participant"]("btc_ceremony", member, "trust-bitcoin-registry", "--accept-fingerprint", reviewed["fingerprint"])
        deployed = env["participant"]("btc_ceremony", indices[0], "btc-deploy-board", "--max-packet-bytes", "4096")
        env["signing_board"] = deployed["signing_board"]
    source_address = wallet_address(core, b.contract("mining").functions.payoutScript().call())
    destination_address = wallet_address(core, d.contract("mining").functions.payoutScript().call())
    commitment = source["command"]("bitcoin", "deposit-commitment", "--chain-id", "31337",
        "--mining-pool", b.contract("mining").address, "--vout", "0", "--recipient", owner.address,
        "--amount", str(1_000_000 * 10**10))
    deposit = committed_deposit(wallet, source_address, "0.01000000", commitment["data"])
    treasury = wallet.call("sendtoaddress", source_address, "0.00003000", "", "", False, True, None, "unset", None, "2")
    if not native_s:
        destination_treasury = wallet.call("sendtoaddress", destination_address, "0.00003000", "", "", False, True, None, "unset", None, "2")
    core.call("generatetoaddress", 6, miner)
    for env in (source, destination): relay_headers(w3, env["bootstrap"].contract("relay"), core)
    def vout(txid, script):
        return next(row["n"] for row in core.call("getrawtransaction", txid, True)["vout"]
                    if row["scriptPubKey"]["hex"] == bytes(script).hex())
    mint_options = ["--signing-board", source["signing_board"], "--bitcoin-rpc", BTC_RPC,
        "--bitcoin-cookie", COOKIE, "--txid", deposit, "--vout", "0", "--recipient", owner.address,
        "--amount", str(1_000_000 * 10**10), "--deadline", str(w3.eth.get_block("latest").timestamp + 3600)]
    proposal = source["participant"]("btc_ceremony", 0, "btc-mint-propose", *mint_options)
    sign(source, int(proposal["session"]))
    assert source["participant"]("btc_ceremony", 0, "btc-mint-execute", *mint_options)["status"] == "BTC_FUNDING_EXECUTED"
    config = destination["command"]("custody-transfer", "inspect", "--rpc", RPC, "--board", destination["board"])
    base = ["--rpc", RPC, "--board", destination["board"], "--accept-fingerprint", config["fingerprint"]]
    owner_args = ["--identity-file", owner_file, "--state", tmp_path / "owner.sqlite", "--send"]
    if native_s:
        manifest = two / "manifest.json"
        manifest.write_text(json.dumps(destination["manifest"]))
        def dao(action, operation, *args):
            return destination["command"]("dao", "in-kind", action, "--manifest", manifest,
                "--rpc", RPC, "--confirmations", "1", *owner_args, "--id", operation, *args)
        for action in ("approve-wrap", "wrap"):
            options = ["--source", "0", "--amount", str(1_000_000 * 10**10)]
            if action == "wrap":
                options += ["--minimum-s", str(998_000 * 10**10), "--recipient", owner.address]
            result = dao(action, action, *options)
            assert result["status"] == "VERIFIED", result
            assert dao(action, action, *options)["tx_hash"] == result["tx_hash"]
        gateway = d.contract("custody_transfer")
        receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
        event = gateway.events.Requested().process_receipt(receipt, errors=DISCARD)[0]["args"]
        transfer_id, request = event["id"], event["requestKey"]
        shares = d.contract("conf_shares")
        assert "mp" not in destination["manifest"]["contracts"]
    else:
        for action in ("approve", "request"):
            options = [*base, "--source", "0", "--satoshis", "1000000", *owner_args, "--id", action]
            if action == "request": options += ["--minimum-shares", "0.00998", "--recipient", owner.address]
            result = destination["command"]("custody-transfer", action, *options)
            assert result["status"] == "VERIFIED", result
            assert destination["command"]("custody-transfer", action, *options)["tx_hash"] == result["tx_hash"]
        transfer_id, request = int(result["transfer_id"]), bytes.fromhex(result["source_request"][2:])
        shares = contract(w3, "ConfederationToken", d.contract("conf_pool").functions.shareToken().call())
    assert shares.functions.balanceOf(owner.address).call() == 0
    paid_batch, broadcast = bitcoin_payment(source, core, miner, source["signing_board"],
        [deposit + ":0", treasury + ":" + str(vout(treasury, b.contract("mining").functions.payoutScript().call()))],
        request, local_members=(0,1,2))
    assert b.contract("mp").functions.totalSupply().call() == 0
    assert b.contract("deposits").functions.totalBackingSats().call() == 0
    relay_headers(w3, d.contract("relay"), core)
    paid = b.contract("escrow").functions.payment(paid_batch).call()
    transfer_txid = bytes(paid[0])[::-1].hex()
    destination_vout = vout(transfer_txid, d.contract("mining").functions.payoutScript().call())
    from signer_agent.bitcoin_onchain_payment import protect_unminted_deposit
    incoming = dict(outpoint=transfer_txid + ":" + str(destination_vout),
                    script=bytes(d.contract("mining").functions.payoutScript().call()).hex())
    with pytest.raises(ValueError, match="BTC_UNACCEPTED_CUSTODY_TRANSFER"):
        protect_unminted_deposit(d.contract("deposits"), incoming, core, w3.eth.block_number)
    final_options = [*base, "--transfer-id", str(transfer_id), "--paid-batch", paid_batch.hex(),
        "--vout", str(destination_vout), "--bitcoin-rpc", BTC_RPC, "--bitcoin-cookie", COOKIE,
        *owner_args, "--id", "finalize"]
    final = destination["command"]("custody-transfer", "finalize", *final_options)
    assert final["status"] == "VERIFIED"
    assert shares.functions.balanceOf(owner.address).call() == 998300 * 10**10
    assert d.contract("deposits").functions.totalBackingSats().call() == 1_000_000
    pass_id = None
    if native_s:
        income = d.contract("dao_income")
        assert shares.functions.balanceOf(income.address).call() == 1700 * 10**10
        assert shares.functions.totalSupply().call() == 1_000_000 * 10**10
        paid_transaction = core.call("getrawtransaction", transfer_txid, True)
        source_change_vout = vout(transfer_txid, b.contract("mining").functions.payoutScript().call())
        received_sats = satoshis(paid_transaction["vout"][destination_vout]["value"])
        source_change = satoshis(paid_transaction["vout"][source_change_vout]["value"])
        source_fee = 1_003_000 - sum(satoshis(output["value"]) for output in paid_transaction["vout"])
        assert received_sats == 1_000_000
        assert source_change == 2000  # Source fee 1000 of its own 3000 sat fee input.
        assert source_fee == 1000
        assert destination["command"]("custody-transfer", "finalize", *final_options)["tx_hash"] == final["tx_hash"]
        calldata = w3.eth.get_transaction(final["tx_hash"])["input"]
        function, arguments = gateway.decode_function_input(calldata)
        with pytest.raises(ContractLogicError, match="transfer state"):
            function(**arguments).call({"from": owner.address})
        assert shares.functions.totalSupply().call() == 1_000_000 * 10**10
        assert d.contract("deposits").functions.totalBackingSats().call() == 1_000_000
        if use_pass:
            from test_in_kind_bootstrap import buy_native_pass

            pass_id = buy_native_pass(destination, manifest, owner.address, owner_file, two)
            configured = dao("wrap-pass", "configure-pass", "--nft", str(pass_id))
            assert configured["status"] == "VERIFIED"
            pass_pool = d.contract("conf_pool")
            assert pass_id == pass_pool.functions.wrapPass(owner.address).call() == 0
            assert pass_pool.functions.wrapPassSelected(owner.address).call()
            assert shares.functions.balanceOf(owner.address).call() == 997300 * 10**10
            assert shares.functions.balanceOf(income.address).call() == 2700 * 10**10
            assert shares.functions.totalSupply().call() == 1_000_000 * 10**10
    protect_unminted_deposit(d.contract("deposits"), incoming, core, w3.eth.block_number)
    # From this point no source participant, source operator or destination
    # federation representative can be invoked by this acceptance harness.
    def stopped(*args, **kwargs): raise AssertionError("OFFLINE_FEDERATION_WAS_REQUIRED")
    source["participant"] = stopped; source["command"] = stopped; source["setup"] = stopped
    original = destination["participant"]
    def custodians_only(role, member, *args):
        assert member in (1,2), "destination representative was required"
        return original(role, member, *args)
    destination["participant"] = custodians_only
    recipient = wallet.call("getnewaddress", "", "bech32m")
    recipient_script = wallet.call("getaddressinfo", recipient)["scriptPubKey"]
    pool = d.contract("conf_pool")
    locked = shares.functions.balanceOf(owner.address).call()
    if native_s:
        withdraw_args = ["--amount", str(locked), "--script", recipient_script, "--miner-fee-cap-sats", "1000"]
        if use_pass:
            sale = d.contract("nft_pass")
            not_owner = Web3.to_hex(Web3.keccak(text="NotTokenOwner()")[:4])
            with pytest.raises(ContractLogicError, match=not_owner):
                pool.functions.requestWithdrawalWithNFT(locked, bytes.fromhex(recipient_script), pass_id, 1000).call(
                    {"from": destination["addresses"][1]})
            assert sale.functions.nftMetadata(pass_id).call()[2] == 0
            withdraw_args += ["--nft", str(pass_id)]
        eth_before = w3.eth.get_balance(owner.address)
        result = dao("withdraw", "exit", *withdraw_args)
        assert result["status"] == "VERIFIED", result
        assert dao("withdraw", "exit", *withdraw_args)["tx_hash"] == result["tx_hash"]
        receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
        assert receipt["from"] == owner.address
        gas_paid = receipt["gasUsed"] * receipt["effectiveGasPrice"]
        assert gas_paid > 0 and eth_before - w3.eth.get_balance(owner.address) == gas_paid
        if use_pass:
            assert sale.functions.nftMetadata(pass_id).call()[2] == locked
        pool_kind = "ConfederationPoolV2"
    else:
        tx(w3, pool.functions.requestWithdrawal(d.contract("mp").address, locked,
            bytes.fromhex(recipient_script)), sender=owner.address)
        pool_kind = "ConfederationMultiPool"
    withdrawal_id = pool.functions.withdrawalCounter().call() - 1
    approve(destination, pool, pool_kind, "getProcessWithdrawalOperationHash", [withdrawal_id], custodians=True)
    tx(w3, pool.functions.processWithdrawal(withdrawal_id))
    due = pool.functions.getWithdrawal(withdrawal_id).call()[3] // 10**10
    destination_request = d.contract("escrow").functions.requestKey(pool.address, withdrawal_id).call()
    inputs = [transfer_txid + ":" + str(destination_vout)]
    if not native_s:
        inputs.append(destination_treasury + ":" +
            str(vout(destination_treasury, d.contract("mining").functions.payoutScript().call())))
    withdrawal_batch, withdrawal = bitcoin_payment(destination, core, miner, destination["signing_board"], inputs,
        destination_request, local_members=(1,2))
    assert shares.functions.balanceOf(owner.address).call() == 0
    assert d.contract("escrow").functions.getRequest(destination_request).call()[7] == 3
    assert due == (996153 if use_pass else 996003)
    payout = satoshis(core.call("getreceivedbyaddress", recipient, 6))
    if native_s:
        destination_fee = d.contract("escrow").functions.minerFee(withdrawal_batch).call()
        assert destination_fee == 1000
        assert payout == due - destination_fee
        change_vout = vout(withdrawal["txid"], d.contract("mining").functions.payoutScript().call())
        change = satoshis(core.call("gettxout", withdrawal["txid"], change_vout)["value"])
        assert payout + destination_fee + change == received_sats
        assert change == (3847 if use_pass else 3997) == d.contract("deposits").functions.totalBackingSats().call()
        assert shares.functions.totalSupply().call() == shares.functions.balanceOf(income.address).call() == change * 10**10
        assert pool.functions.reservedS().call() == 0
        assert shares.functions.balanceOf(d.contract("escrow").address).call() == 0
        record_property("native_s_accounting", json.dumps(dict(transferred_sats=received_sats,
            source_miner_fee_sats=source_fee, destination_miner_fee_sats=destination_fee,
            payout_sats=payout, reserve_sats=change, dao_share_units=change * 10**10,
            pass_price_sats=1000 if use_pass else 0, withdrawal_evm_gas_wei=gas_paid)))
        return
    assert payout == due
    # Regression: a custodian also proposes and pays a surplus-treasury request,
    # with the original operator/representative still unavailable.
    treasury_recipient=wallet.call("getnewaddress","","bech32")
    treasury_script=wallet.call("getaddressinfo",treasury_recipient)["scriptPubKey"]
    treasury_options=["--signing-board",destination["signing_board"],"--bitcoin-rpc",BTC_RPC,
        "--bitcoin-cookie",COOKIE,"--request-id","88001","--satoshis","400",
        "--destination-script",treasury_script,"--deadline",str(w3.eth.get_block("latest").timestamp+3600)]
    proposal=destination["participant"]("btc_ceremony",1,"btc-treasury-propose",*treasury_options)
    assert proposal["status"]=="AWAITING_PARTICIPANTS"
    sign(destination,int(proposal["session"]))
    executed=destination["participant"]("btc_ceremony",1,"btc-treasury-execute",*treasury_options)
    assert executed["status"]=="BTC_FUNDING_EXECUTED"
    change_txid=bytes(d.contract("escrow").functions.payment(withdrawal_batch).call()[0])[::-1].hex()
    change_vout=vout(change_txid,d.contract("mining").functions.payoutScript().call())
    backing_before=d.contract("deposits").functions.totalBackingSats().call()
    bitcoin_payment(destination,core,miner,destination["signing_board"],[change_txid+":"+str(change_vout)],
        bytes.fromhex(executed["withdrawal_ref"].removeprefix("0x")),local_members=(1,2))
    assert d.contract("deposits").functions.totalBackingSats().call()==backing_before
    assert satoshis(core.call("getreceivedbyaddress",treasury_recipient,6))==400
    record_property("custody_transfer", json.dumps(dict(real_core=True, native_frost=True,
        separate_bitcoin_keys=True, source_federation_offline_for_exit=True,
        destination_representative_offline_for_exit=True, transferred_sats=1_000_000, payout_sats=due,
        custodian_treasury_payout_sats=400)))
