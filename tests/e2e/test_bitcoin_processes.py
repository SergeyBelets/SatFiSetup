"""Real DKG -> factory -> proof mint -> MP/S escrow -> Bitcoin payment -> proof burn -> DAO claim."""

import hashlib
import json
import pytest
import os
from pathlib import Path
from web3 import Web3
from signer_agent import crypto
from signer_agent.bitcoin_worker import (
    BitcoinRPC,
    BitcoinWorker,
    plan_hash,
    outpoint_hash,
)
from test_dkg_processes import deploy, tx, ceremony, RPC
from lifecycle_helpers import federation, contract, relay_headers, proof, committed_deposit

BTC_RPC = os.environ.get("SATOHSI_BTC_RPC", "http://127.0.0.1:18443")
COOKIE = os.environ.get(
    "SATOHSI_BTC_COOKIE", "/tmp/satohsi-bitcoin-regtest/regtest/.cookie"
)


def authorize(
    w3, coordinator, auth, participant, tmp_path, target, artifact, helper, args,
    *, approve_onchain=True,
):
    target_contract = w3.eth.contract(
        address=target, abi=__import__("test_dkg_processes").artifact(artifact)["abi"]
    )
    from signer_agent.bootstrap import encode_args

    def strings(x):
        if isinstance(x, bool):
            return x
        if isinstance(x, int):
            return str(x)
        if isinstance(x, list):
            return [strings(v) for v in x]
        return x

    helper_abi = next(
        x
        for x in target_contract.abi
        if x.get("type") == "function" and x.get("name") == helper
    )
    message = getattr(target_contract.functions, helper)(
        *encode_args(helper_abi["inputs"], strings(args))
    ).call()
    tx(
        w3,
        coordinator.functions.createSessionFromDKG(
            auth.functions.dkgId().call(),
            message,
            target,
            w3.eth.get_block("latest").timestamp + 3600,
        ),
    )
    sid = coordinator.functions.nextSessionId().call()
    intent = tmp_path / "intent.json"
    intent.write_text(
        json.dumps(
            dict(
                chain_id=str(w3.eth.chain_id),
                target=target,
                operation_hash=message.hex(),
                artifact=artifact,
                hash_method=helper,
                hash_args=args,
            )
        )
    )
    options = [
        "--coordinator",
        coordinator.address,
        "--session",
        sid,
        "--package",
        intent,
    ]
    # The real authorization determines how many independent shares are needed.
    # Confederation fixtures now require ceil(70% * 3) == 3 participants.
    selected = range(auth.functions.threshold().call())
    for i in selected:
        assert participant(i, "sign-evm", *options)["status"] == "committed"
    for i in selected:
        assert participant(i, "sign-evm", *options)["status"] in ("signed", "finalized")
    if approve_onchain:
        tx(w3, auth.functions.authorizeOperation(sid, message, target))
    return message


def test_dkg_taproot_pays_p2wpkh_and_p2tr(tmp_path, record_property):
    w3 = Web3(Web3.HTTPProvider(RPC))
    assert w3.eth.chain_id == 31337
    btc = BitcoinRPC(BTC_RPC, COOKIE)
    btc.check_network()
    wallet_name = "satohsi-" + os.urandom(5).hex()
    btc.call("createwallet", wallet_name)
    wallet = BitcoinRPC(BTC_RPC + "/wallet/" + wallet_name, COOKIE)
    mining_address = wallet.call("getnewaddress")
    btc.call("generatetoaddress", 101, mining_address)
    relay = deploy(w3, "RegtestHeaderRelay")
    relay_headers(w3, relay, btc)
    registry = deploy(w3, "DKGCoordinator")
    verifier = deploy(w3, "FROSTVerifier")
    coordinator = deploy(w3, "FROSTCoordinator", verifier.address)
    tx(w3, coordinator.functions.bindDKGRegistry(registry.address))
    evm_dir = tmp_path / "evm"
    evm_dir.mkdir()
    btc_dir = tmp_path / "btc"
    btc_dir.mkdir()
    evm, evm_participant, evm_public = ceremony(evm_dir, w3, registry, 3, 2, "EVM_AUTH")
    m, participant, public = ceremony(btc_dir, w3, registry, 3, 2, "BTC_SPEND")
    factory, auth, pool = federation(w3, registry, coordinator, evm)
    batch_registry = deploy(
        w3, "BitcoinBatchRegistry", auth.address, registry.address, m.session_id
    )

    def approve(target, artifact, helper, args):
        return authorize(
            w3,
            coordinator,
            auth,
            evm_participant,
            tmp_path,
            target.address,
            artifact,
            helper,
            args,
        )

    escrow = deploy(
        w3, "BitcoinWithdrawalEscrow", batch_registry.address, relay.address, 6, 294, 330
    )
    op = approve(
        batch_registry,
        "BitcoinBatchRegistry",
        "getSetWithdrawalEscrowOperationHash",
        [escrow.address],
    )
    tx(w3, batch_registry.functions.setWithdrawalEscrow(escrow.address, op))
    op = approve(
        pool, "SimpleMultiPool", "getSetWithdrawalEscrowOperationHash", [escrow.address]
    )
    tx(w3, pool.functions.setWithdrawalEscrow(escrow.address, op))
    deposits = deploy(
        w3, "BitcoinDepositRegistry", relay.address, auth.address, 6, escrow.address
    )
    op = approve(
        escrow,
        "BitcoinWithdrawalEscrow",
        "getSetDepositRegistryOperationHash",
        [deposits.address],
    )
    tx(w3, escrow.functions.setDepositRegistry(deposits.address, op))
    # The callback source is the real canonical factory child created by federation(),
    # not a contract admitted merely because it repeats public escrow/auth getters.
    federation_deployer = contract(w3, "FederationDeployer", factory.functions.deployer().call())
    simple_origin = federation_deployer.functions.multiPoolDeployer().call()
    callbacks = deploy(w3, "CallbackPoolRegistry", escrow.address, auth.address,
                       simple_origin, "0x" + "00" * 20)
    op = approve(escrow, "BitcoinWithdrawalEscrow", "getSetCallbackPoolRegistryOperationHash",
                 [callbacks.address])
    tx(w3, escrow.functions.setCallbackPoolRegistry(callbacks.address, op))
    assert escrow.functions.isCallbackPoolAdmitted(pool.address).call()
    # Each independent signer reviews and pins the canonical graph before any nonce.
    for i in range(3):
        reviewed = participant(i, "inspect-bitcoin-registry", "--batch-registry",
                               batch_registry.address)
        assert reviewed["graph"]["contracts"]["authorization"]["address"] == auth.address
        assert participant(i, "trust-bitcoin-registry", "--batch-registry",
                           batch_registry.address, "--accept-fingerprint",
                           reviewed["fingerprint"])["status"] == "BTC_REGISTRY_PINNED"
    mining = deploy(
        w3,
        "MiningPoolV1",
        "integration-proof-pool",
        deposits.address,
        escrow.address,
        "Integration Proof MP",
        "IPMP",
    )
    mp = contract(w3, "PoolMpToken", mining.functions.mpToken().call())
    shares_token = contract(
        w3, "MultiPoolShareToken", pool.functions.getShareToken().call()
    )
    payout = mining.functions.payoutScript().call()
    op = approve(
        deposits,
        "BitcoinDepositRegistry",
        "getRegisterOperationHash",
        [mining.address, "0x" + payout.hex()],
    )
    tx(w3, deposits.functions.register(mining.address, payout, op))
    op = approve(
        pool,
        "SimpleMultiPool",
        "getAddPoolOperationHash",
        [mp.address, "integration-proof-pool"],
    )
    tx(w3, pool.functions.addPool(mp.address, "integration-proof-pool", op))
    cfg = (1, 0, 50000 * 10**10, 0)
    manager = deploy(
        w3,
        "FederationFeeManager",
        auth.address,
        "0x" + "00" * 20,
        w3.eth.accounts[9],
        cfg,
        cfg,
    )
    op = approve(
        manager,
        "FederationFeeManager",
        "getSetFeeCollectorOperationHash",
        [pool.address, True],
    )
    tx(w3, manager.functions.setFeeCollector(pool.address, True, op))
    op = approve(
        pool, "SimpleMultiPool", "getSetFeeManagerOperationHash", [manager.address]
    )
    tx(w3, pool.functions.setFeeManager(manager.address, op))
    governance = deploy(
        w3, "FederationGovernance", "integration-fed", auth.address, pool.address
    )
    governance_registry = deploy(w3, "GovernanceRegistry", factory.address)
    op = approve(
        governance_registry,
        "GovernanceRegistry",
        "getRegisterOperationHash",
        ["integration-fed", governance.address],
    )
    tx(
        w3,
        governance_registry.functions.register(
            "integration-fed", governance.address, op
        ),
    )
    assert (
        governance_registry.functions.governance("integration-fed").call()
        == governance.address
    )
    # Local USD and historical feed are explicit integration fixtures; signatures and proofs are real.
    usd = deploy(w3, "DAOTestUSD")
    gov = deploy(
        w3, "CustodianDAOToken", "Custodian Governance", "CGOV", w3.eth.accounts[0]
    )
    validator = deploy(
        w3, "CustodianValidator", list(evm.participants), 2, auth.address
    )
    staking = deploy(
        w3, "CustodianDAOStaking", gov.address, usd.address, validator.address
    )
    controller = deploy(
        w3, "CustodianDAORateController", validator.address, staking.address, False
    )
    feed = deploy(w3, "DAOMockFeed")
    twap = deploy(w3, "ChainlinkBTCDailyTWAP", feed.address)
    vault = deploy(w3, "CustodianDAOIncomeVault", usd.address, auth.address)
    settlement = deploy(
        w3,
        "CustodianDAODailySettlement",
        usd.address,
        staking.address,
        controller.address,
        auth.address,
        twap.address,
        vault.address,
        w3.eth.accounts[9],
    )
    tx(w3, staking.functions.setDistributor(settlement.address))
    tx(w3, vault.functions.setSettlement(settlement.address))
    tx(w3, gov.functions.approve(staking.address, 10**18))
    tx(w3, staking.functions.stake(10**18))
    day = staking.functions.firstEligibleDay().call()
    w3.provider.make_request("evm_setNextBlockTimestamp", [day * 86400 + 1])
    w3.provider.make_request("evm_mine", [])
    router = deploy(
        w3, "CustodianDAOFeeRouter", vault.address, w3.eth.accounts[9], 2500, 5000
    )
    converter = deploy(w3, "FundedFeeConverter", mp.address, usd.address, 1, 10**10)
    pipeline = deploy(w3, "PoolFeePipeline", converter.address, router.address)
    tx(w3, usd.functions.mint(converter.address, 10000000))
    for label, stream in [("WRAP_FEE", 1), ("UNWRAP_FEE", 3), ("DEPLOY_FEE", 2)]:
        source = Web3.keccak(text=label)
        args = ["0x" + source.hex(), router.address, stream, True, "0x" + "00" * 20, 0]
        op = approve(
            vault, "CustodianDAOIncomeVault", "getSetSourceOperationHash", args
        )
        tx(
            w3,
            vault.functions.setSource(
                source, router.address, stream, True, "0x" + "00" * 20, 0, op
            ),
        )
    op = approve(
        router,
        "CustodianDAOFeeRouter",
        "getSetPayerOperationHash",
        [pipeline.address, True],
    )
    tx(w3, router.functions.setPayer(pipeline.address, True, op))

    script = "5120" + public["group"][2:]
    descriptor = btc.call("getdescriptorinfo", "rawtr(" + public["group"][2:] + ")")[
        "descriptor"
    ]
    address = btc.call("deriveaddresses", descriptor)[0]
    for recipient_type in ("bech32", "bech32m"):
        commitment = mining.functions.getDepositData(0, [w3.eth.accounts[0]], [1000000 * 10**10]).call()
        deposit = committed_deposit(wallet, address, "0.01000000", bytes(commitment).hex())
        fee_deposit = wallet.call(
            "sendtoaddress",
            address,
            "0.00003000",
            "",
            "",
            False,
            True,
            None,
            "unset",
            None,
            "2",
        )
        btc.call("generatetoaddress", 6, mining_address)
        decoded = btc.call("getrawtransaction", deposit, True)
        funded = next(x for x in decoded["vout"] if x["scriptPubKey"]["hex"] == script)
        fee_funded = next(
            x
            for x in btc.call("getrawtransaction", fee_deposit, True)["vout"]
            if x["scriptPubKey"]["hex"] == script
        )
        relay_headers(w3, relay, btc)
        raw, block, siblings, index = proof(btc, deposit)
        wire_txid = bytes.fromhex(deposit)[::-1]
        mint_args = [
            "0x" + wire_txid.hex(),
            funded["n"],
            [w3.eth.accounts[0]],
            [1000000 * 10**10],
        ]
        op = approve(mining, "MiningPoolV1", "getMintOperationHash", mint_args)
        tx(
            w3,
            mining.functions.mintFromProof(
                raw,
                funded["n"],
                block,
                siblings,
                index,
                [w3.eth.accounts[0]],
                [1000000 * 10**10],
                op,
            ),
        )
        with pytest.raises(Exception):
            mining.functions.mintFromProof(
                raw,
                funded["n"],
                block,
                siblings,
                index,
                [w3.eth.accounts[0]],
                [1000000 * 10**10],
                op,
            ).call({"from": w3.eth.accounts[0]})
        with pytest.raises(Exception):
            deposits.functions.consume(raw, funded["n"], block, siblings, index).call(
                {"from": mining.address}
            )
        tx(w3, mp.functions.approve(escrow.address, 1000 * 10**10))
        tx(
            w3,
            escrow.functions.requestDirect(
                100 + pool.functions.withdrawalCounter().call(),
                mp.address,
                1000 * 10**10,
                bytes.fromhex(script),
            ),
        )
        safe_key = escrow.functions.requestKey(
            w3.eth.accounts[0], 100 + pool.functions.withdrawalCounter().call()
        ).call()
        before_refund = mp.functions.balanceOf(w3.eth.accounts[0]).call()
        tx(w3, escrow.functions.refund(safe_key))
        assert (
            mp.functions.balanceOf(w3.eth.accounts[0]).call()
            == before_refund + 1000 * 10**10
        )
        with pytest.raises(Exception):
            escrow.functions.refund(safe_key).call({"from": w3.eth.accounts[0]})
        direct_amount = 100000 if recipient_type == "bech32m" else 0
        tx(w3, mp.functions.approve(pool.address, (1000000 - direct_amount) * 10**10))
        tx(w3, pool.functions.deposit(mp.address, (1000000 - direct_amount) * 10**10))
        recipient = wallet.call("getnewaddress", "", recipient_type)
        recipient_script = wallet.call("getaddressinfo", recipient)["scriptPubKey"]
        wid = pool.functions.withdrawalCounter().call()
        supply_before = shares_token.functions.totalSupply().call()
        mp_before = mp.functions.totalSupply().call()
        tx(
            w3,
            pool.functions.requestWithdrawal(
                mp.address, 650000 * 10**10, bytes.fromhex(recipient_script)
            ),
        )
        assert shares_token.functions.balanceOf(pool.address).call() == 650000 * 10**10
        assert mp.functions.balanceOf(escrow.address).call() == 600000 * 10**10
        op = approve(
            pool, "SimpleMultiPool", "getProcessWithdrawalOperationHash", [wid]
        )
        tx(w3, pool.functions.processWithdrawal(wid, op))
        assert shares_token.functions.totalSupply().call() == supply_before
        assert mp.functions.totalSupply().call() == mp_before
        keys = [escrow.functions.requestKey(pool.address, wid).call()]
        if direct_amount:
            tx(w3, mp.functions.approve(escrow.address, direct_amount * 10**10))
            tx(
                w3,
                escrow.functions.requestDirect(
                    99,
                    mp.address,
                    direct_amount * 10**10,
                    bytes.fromhex(recipient_script),
                ),
            )
            keys.append(escrow.functions.requestKey(w3.eth.accounts[0], 99).call())
        plan = dict(
            wallet_script=script,
            network="regtest",
            batch_registry=batch_registry.address,
            batch_id=os.urandom(32).hex(),
            deadline=str(w3.eth.get_block("latest").timestamp + 3600),
            withdrawal_refs=[k.hex() for k in keys],
            inputs=[
                dict(
                    outpoint=deposit + ":" + str(funded["n"]),
                    amount="1000000",
                    script=script,
                ),
                dict(
                    outpoint=fee_deposit + ":" + str(fee_funded["n"]),
                    amount="3000",
                    script=script,
                ),
            ],
            outputs=[
                dict(script=recipient_script, amount=str(600000 + direct_amount)),
                dict(script=script, amount=str(402000 - direct_amount)),
            ],
            fee="1000",
            max_fee="2000",
        )
        built = crypto.call(
            "bitcoin_build",
            **{
                k: plan[k]
                for k in ("inputs", "outputs", "wallet_script", "fee", "max_fee")
            }
        )
        ph = plan_hash(m, plan, built)
        outpoints = [outpoint_hash(x["outpoint"]) for x in plan["inputs"]]
        args = [
            "0x" + plan["batch_id"],
            "0x" + ph.hex(),
            ["0x" + x.hex() for x in outpoints],
            int(plan["deadline"]),
        ]
        approved = authorize(
            w3,
            coordinator,
            auth,
            evm_participant,
            tmp_path,
            batch_registry.address,
            "BitcoinBatchRegistry",
            "operationHash",
            args,
        )
        tx(
            w3,
            batch_registry.functions.reserve(
                bytes.fromhex(plan["batch_id"]),
                ph,
                outpoints,
                int(plan["deadline"]),
                approved,
            ),
        )
        expected_txid = hashlib.sha256(
            hashlib.sha256(bytes.fromhex(built["unsigned"])).digest()
        ).digest()
        request_key = escrow.functions.requestKey(pool.address, wid).call()
        op = approve(
            escrow,
            "BitcoinWithdrawalEscrow",
            "getBindOperationHash",
            [
                "0x" + plan["batch_id"],
                "0x" + expected_txid.hex(),
                ["0x" + k.hex() for k in keys],
            ],
        )
        tx(
            w3,
            escrow.functions.bind(
                bytes.fromhex(plan["batch_id"]), expected_txid, keys, op
            ),
        )
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        options = [
            "--package",
            path,
            "--bitcoin-rpc",
            BTC_RPC,
            "--bitcoin-cookie",
            COOKIE,
        ]
        commitments = [participant(i, "btc-commit", *options) for i in (0, 1)]
        plan["commitments"] = [
            {str(c["participant_index"]): c["values"][j] for c in commitments}
            for j in range(2)
        ]
        path.write_text(json.dumps(plan))
        original = plan["outputs"][0]["amount"]
        plan["outputs"][0]["amount"] = str(599999 + direct_amount)
        plan["outputs"][1]["amount"] = str(402001 - direct_amount)
        path.write_text(json.dumps(plan))
        with pytest.raises(AssertionError, match="BATCH_APPROVAL_MISMATCH"):
            participant(0, "btc-sign", *options)
        plan["outputs"][0]["amount"] = original
        plan["outputs"][1]["amount"] = str(402000 - direct_amount)
        path.write_text(json.dumps(plan))
        shares = [participant(i, "btc-sign", *options) for i in (0, 1)]
        # Retry emits the same cached scalar; the nonce cannot be rerun on another transcript.
        assert participant(0, "btc-sign", *options)["values"] == shares[0]["values"]
        signatures = []
        for j in range(2):
            aggregate = crypto.call(
                "aggregate",
                public_package=public["public_package"],
                message=built["sighashes"][j],
                commitments=plan["commitments"][j],
                shares={str(s["participant_index"]): s["values"][j] for s in shares},
            )
            signatures.append(aggregate["signature"])
        signed = crypto.call(
            "bitcoin_attach",
            unsigned=built["unsigned"],
            package=plan,
            signatures=signatures,
        )
        with pytest.raises(Exception):
            pool.functions.cancelWithdrawal(wid).call({"from": w3.eth.accounts[0]})
        if direct_amount:
            with pytest.raises(Exception):
                escrow.functions.refund(keys[1]).call({"from": w3.eth.accounts[0]})
        worker = BitcoinWorker(tmp_path / "worker.sqlite", btc)
        assert worker.broadcast(plan["batch_id"], signed) == signed["txid"]
        assert worker.broadcast(plan["batch_id"], signed) == signed["txid"]
        assert worker.confirmation(plan["batch_id"])["status"] == "CONFIRMING"
        if recipient_type == "bech32":
            previous = plan["batch_id"]
            previous_signed = signed
            old_commitments = plan.pop("commitments")
            plan["batch_id"] = os.urandom(32).hex()
            plan["fee"] = "1500"
            plan["outputs"][1]["amount"] = "401500"
            built = crypto.call(
                "bitcoin_build",
                **{
                    k: plan[k]
                    for k in ("inputs", "outputs", "wallet_script", "fee", "max_fee")
                }
            )
            replacement_hash = plan_hash(m, plan, built)
            op = approve(
                batch_registry,
                "BitcoinBatchRegistry",
                "getReplacementOperationHash",
                [
                    "0x" + previous,
                    "0x" + plan["batch_id"],
                    "0x" + replacement_hash.hex(),
                    plan["deadline"],
                ],
            )
            tx(
                w3,
                batch_registry.functions.reserveReplacement(
                    bytes.fromhex(previous),
                    bytes.fromhex(plan["batch_id"]),
                    replacement_hash,
                    int(plan["deadline"]),
                    op,
                ),
            )
            expected_txid = hashlib.sha256(
                hashlib.sha256(bytes.fromhex(built["unsigned"])).digest()
            ).digest()
            op = approve(
                escrow,
                "BitcoinWithdrawalEscrow",
                "getBindReplacementOperationHash",
                ["0x" + previous, "0x" + plan["batch_id"], "0x" + expected_txid.hex()],
            )
            tx(
                w3,
                escrow.functions.bindReplacement(
                    bytes.fromhex(previous),
                    bytes.fromhex(plan["batch_id"]),
                    expected_txid,
                    op,
                ),
            )
            assert batch_registry.functions.reservationRoot(
                bytes.fromhex(plan["batch_id"])
            ).call() == bytes.fromhex(previous)
            path.write_text(json.dumps(plan))
            commitments = [participant(i, "btc-commit", *options) for i in (0, 1)]
            plan["commitments"] = [
                {str(c["participant_index"]): c["values"][j] for c in commitments}
                for j in range(2)
            ]
            assert plan["commitments"] != old_commitments
            path.write_text(json.dumps(plan))
            shares = [participant(i, "btc-sign", *options) for i in (0, 1)]
            signatures = [
                crypto.call(
                    "aggregate",
                    public_package=public["public_package"],
                    message=built["sighashes"][j],
                    commitments=plan["commitments"][j],
                    shares={
                        str(x["participant_index"]): x["values"][j] for x in shares
                    },
                )["signature"]
                for j in range(2)
            ]
            signed = crypto.call(
                "bitcoin_attach",
                unsigned=built["unsigned"],
                package=plan,
                signatures=signatures,
            )
            assert (
                worker.broadcast(plan["batch_id"], signed, replaces=previous)
                == signed["txid"]
            )
            assert worker.confirmation(previous)["status"] == "REPLACED_PENDING"
            with pytest.raises(Exception):
                pool.functions.cancelWithdrawal(wid).call({"from": w3.eth.accounts[0]})
            record_property(
                "fee_replacement",
                json.dumps(
                    dict(
                        original_batch=previous,
                        replacement_batch=plan["batch_id"],
                        original_txid=previous_signed["txid"],
                        replacement_txid=signed["txid"],
                        original_fee="1000",
                        replacement_fee="1500",
                    )
                ),
            )
        btc.call("generatetoaddress", 6, mining_address)
        result = worker.confirmation(plan["batch_id"])
        assert result["status"] == "CONFIRMED"
        record_property("payment_" + recipient_type, json.dumps(result))
        relay_headers(w3, relay, btc)
        raw, block, siblings, index = proof(btc, signed["txid"])
        with pytest.raises(Exception):
            escrow.functions.settle(
                bytes.fromhex(plan["batch_id"]), raw, block, [bytes(32)], index
            ).call()
        tx(
            w3,
            escrow.functions.settle(
                bytes.fromhex(plan["batch_id"]), raw, block, siblings, index
            ),
        )
        assert (
            shares_token.functions.totalSupply().call()
            == supply_before - 650000 * 10**10
        )
        assert (
            mp.functions.totalSupply().call()
            == mp_before - (600000 + direct_amount) * 10**10
        )
        assert shares_token.functions.balanceOf(pool.address).call() == 0
        assert mp.functions.balanceOf(escrow.address).call() == 0
        change_key = Web3.keccak(
            __import__("eth_abi").encode(
                ["bytes32", "uint32"], [bytes.fromhex(signed["txid"])[::-1], 1]
            )
        )
        assert (
            deposits.functions.backingSats(change_key).call() == 400000 - direct_amount
        )
        for item in plan["inputs"]:
            prev, vout = item["outpoint"].split(":")
            spent_key = Web3.keccak(
                __import__("eth_abi").encode(
                    ["bytes32", "uint32"], [bytes.fromhex(prev)[::-1], int(vout)]
                )
            )
            assert deposits.functions.walletOutput(spent_key).call()
            assert deposits.functions.backingSats(spent_key).call() == 0
        if direct_amount:
            assert escrow.functions.requests(keys[1]).call()[-1] == 3
        assert deposits.functions.walletOutput(
            Web3.keccak(
                __import__("eth_abi").encode(
                    ["bytes32", "uint32"], [bytes.fromhex(signed["txid"])[::-1], 1]
                )
            )
        ).call()
        assert pool.functions.getWithdrawal(wid).call()[6] is True
        with pytest.raises(Exception):
            escrow.functions.settle(
                bytes.fromhex(plan["batch_id"]), raw, block, siblings, index
            ).call()
        for stream, is_wrap in [(0, True), (1, False)]:
            amount = pool.functions.accruedFees(mp.address, is_wrap).call()
            claim = approve(
                pool,
                "SimpleMultiPool",
                "getClaimFeesOperationHash",
                [mp.address, is_wrap, pipeline.address, amount],
            )
            deadline = w3.eth.get_block("latest").timestamp + 3600
            op = approve(
                pipeline,
                "PoolFeePipeline",
                "operationHash",
                [pool.address, stream, amount, day, 50000, deadline],
            )
            tx(
                w3,
                pipeline.functions.route(
                    pool.address, stream, amount, day, 50000, deadline, claim, op
                ),
            )
            assert pool.functions.accruedFees(mp.address, is_wrap).call() == 0
        assert mp.functions.allowance(pipeline.address, converter.address).call() == 0

        assert (
            btc.call("getrawtransaction", signed["txid"], True)["vout"][0][
                "scriptPubKey"
            ]["hex"]
            == recipient_script
        )
        assert (
            batch_registry.functions.batches(bytes.fromhex(plan["batch_id"])).call()[3]
            == 2
        )
    assert vault.functions.dayUnwrapFeeIncome(day).call() == 25000
    funding = vault.functions.dayWrapFeeIncome(day).call()
    assert funding == 50000 and usd.functions.balanceOf(vault.address).call() == funding
    assert usd.functions.balanceOf(w3.eth.accounts[9]).call() == 150000
    w3.provider.make_request("evm_setNextBlockTimestamp", [(day + 1) * 86400 + 1])
    w3.provider.make_request("evm_mine", [])
    tx(w3, feed.functions.setRound(1, 60000 * 10**8, day * 86400))
    tx(w3, feed.functions.setRound(2, 60000 * 10**8, (day + 1) * 86400))
    from signer_agent.dao_report import prepare

    report_inputs = dict(
        day=str(day),
        round_ids=["1", "2"],
        reward_token=usd.address,
        average_btc_sats="0",
        stablecoin_tvl="1000000",
        opening_reserve_sats="0",
        providers=[[w3.eth.accounts[9], "1"]],
    )
    evidence_bytes, intent = prepare(w3, settlement.address, report_inputs)
    assert hashlib.sha256(evidence_bytes).hexdigest() == intent["hash_args"][0][6][2:]
    report = intent["hash_args"][0]
    op = approve(
        settlement,
        "CustodianDAODailySettlement",
        "getSettlementOperationHash",
        [report, [1, 2]],
    )
    from signer_agent.bootstrap import encode_args

    report_abi = next(
        x for x in settlement.abi if x.get("name") == "getSettlementOperationHash"
    )["inputs"]
    typed_report = encode_args(report_abi, [report, ["1", "2"]])[0]
    tx(w3, settlement.functions.settleDay(typed_report, [1, 2], op))
    tx(w3, staking.functions.claim([day]))
    assert usd.functions.balanceOf(w3.eth.accounts[0]).call() == funding
    assert usd.functions.balanceOf(vault.address).call() == 0
    record_property(
        "settled_lifecycle",
        json.dumps(
            dict(
                pool=pool.address,
                mp=mp.address,
                escrow=escrow.address,
                dao=settlement.address,
                claimed=str(funding),
            )
        ),
    )
    btc.call("invalidateblock", result["block_hash"])
    assert worker.confirmation(plan["batch_id"])["status"] == "RECOVERY_REQUIRED"
    with pytest.raises(ValueError, match="PAYMENTS_PAUSED_FOR_RECOVERY"):
        worker.broadcast(plan["batch_id"], signed)
    assert (
        batch_registry.functions.batches(bytes.fromhex(plan["batch_id"])).call()[3] == 2
    )
