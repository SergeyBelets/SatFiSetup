"""Wallet and keeper commands for the separate in-kind DAO profile."""

import json
import re

from web3 import Web3
from web3.logs import DISCARD

from .bootstrap import encode_args
from .confederation_dao_report import (
    ARTIFACT as REPORT_ARTIFACT, CLOSING_RATES_HELPER, REPORT_HELPERS, _contract, _read, validate_closing_rates,
)
from .dao_client import DAOClient, account_at, address, claim_days
from .evm_network import read_block
from .onboarding import read_private, read_public, write_public
from .operations import canonical
from .safe_errors import DiagnosticArgumentParser
from .ulr_treasury import contract, number

METHODS = {
    ("ConfederationDAOIncome", "getSettlementOperationHash"): "openDay",
    ("ConfederationDAOIncome", "getStageExpensesOperationHash"): "stageExpenses",
    ("ConfederationDAOIncome", CLOSING_RATES_HELPER): "setClosingRates",
    ("ConfederationDAOIncome", "getAddStablecoinOperationHash"): "addStablecoin",
    ("ConfederationNFTPass", "getApproveTariffOperationHash"): "approveTariff",
    ("ConfederationNFTPass", "getSetPriceControlOperationHash"): "setPriceControl",
    ("CustodianDAODecisionBoard", "getCreateOperationHash"): "createDecision",
    ("CustodianValidator", "getAddCustodianOperationHash"): "addCustodian",
    ("CustodianValidator", "getRemoveCustodianOperationHash"): "removeCustodian",
}
NODES = dict(staking="ConfederationDAOStake", dao_income="ConfederationDAOIncome",
             conf_pool="ConfederationPoolV2", dao_token="CustodianDAOToken", nft_pass="ConfederationNFTPass",
             conf_shares="ConfederationShareV2", decisions="CustodianDAODecisionBoard",
             validator="CustodianValidator", mining="ConfederationBitcoinReserve",
             custody_transfer="BitcoinCustodyTransferV2")


def _client(a):
    exit_context = a.action in ("unstake", "claim", "reimburse", "cancel", "process")
    closing = a.action in ("closing-status", "closing-sweep", "closing-process", "closing-claim", "closing-rates-prepare")
    report = False
    if a.action in ("propose", "execute") and a.intent:
        a.reviewed_intent = read_public(a.intent)
        report = (a.reviewed_intent.get("artifact") == REPORT_ARTIFACT
                  and a.reviewed_intent.get("hash_method") in REPORT_HELPERS | {CLOSING_RATES_HELPER})
    if a.manifest:
        manifest = read_public(a.manifest)
        if manifest.get("ulr", {}).get("status") != "CONFEDERATION_DAO_GRAPH_CONFIGURED":
            raise ValueError("CONFEDERATION_DAO_MANIFEST")
        factory_address = manifest["contracts"]["confederations"]["address"]
        conf_id = manifest["ulr"]["confederation_id"]
        chain_id = manifest["chain_id"]
    else:
        manifest = None
        factory_address, conf_id, chain_id = a.factory, a.confederation_id, a.chain_id
        if not conf_id or not chain_id:
            raise ValueError("CONFEDERATION_DAO_DISCOVERY")
    client = DAOClient(a.rpc, chain_id)
    client.block = read_block(client.w3, a.confirmations)
    at = client.block.number
    factory = _contract(client.w3, address(factory_address), "ConfederationFactory", at,
                        legacy_dao_exit=exit_context)
    client.config = dict(chain_id=chain_id, confederation_id=conf_id, factory=factory.address,
                         confirmations=a.confirmations, contracts={})

    def load(name, target):
        kind = NODES[name]
        target = address(target)
        try:
            node = _contract(client.w3, target, kind, at, legacy_dao_exit=exit_context)
        except ValueError as error:
            if (str(error) != "BTC_CONTRACT_BUILD_MISMATCH" or name != "mining"
                    or a.action not in ("discover", "status", "withdraw", "withdraw-quote")):
                raise
            node = _previous_reserve(client, target, kind)
        _add(client, name, node, kind)
        if manifest:
            item = manifest["contracts"][name]
            if (node.address != item["address"] or client.config["contracts"][name]["runtime_hash"]
                    != Web3.to_hex(bytes.fromhex(item["runtime_hash"].removeprefix("0x")))):
                raise ValueError("CONFEDERATION_DAO_MANIFEST")
        return node

    conf = client.read(factory, "getConfederation", conf_id)
    if not conf[-1] and not (exit_context or report or closing):
        raise ValueError("CONFEDERATION_INACTIVE")
    if conf[-1] and closing:
        raise ValueError("CONFEDERATION_DAO_CLOSING")
    pool = load("conf_pool", conf[9])
    validator = load("validator", conf[8])
    shares = load("conf_shares", conf[10])
    income = load("dao_income", client.read(pool, "daoIncome"))
    staking = load("staking", client.read(income, "staking"))
    load("dao_token", client.read(staking, "stakeToken"))
    if (client.read(pool, "confederationId") != conf_id
            or client.read(pool, "shareToken") != shares.address
            or client.read(shares, "confederationPool") != pool.address
            or client.read(income, "pool") != pool.address or client.read(income, "authorization") != conf[7]
            or client.read(income, "shareToken") != shares.address or client.read(income, "validator") != validator.address
            or client.read(validator, "candidateStaking") != staking.address
            or client.read(staking, "custodianValidator") != validator.address):
        raise ValueError("CONFEDERATION_DAO_BINDING")
    if not conf[-1] and (report or closing):
        limit = client.read(income, "settlementDayLimit")
        if (client.read(income, "factory") != factory.address
                or not 0 < limit <= client.block.timestamp // 86400 + 1):
            raise ValueError("CONFEDERATION_DAO_BINDING")
        client.config["last_settlement_day"] = str(limit - 1)
    if conf[-1] and not exit_context:
        _check_active_graph(client, load, pool, shares, income, staking, conf[7])
    client.stable()
    return client


def _previous_reserve(client, target, kind):
    from .bitcoin_trust import runtime_artifact
    from .contract_provenance import prove_creation

    w3, at = client.w3, client.block.number
    _, compiled = runtime_artifact(w3, target, kind, at, previous_build=True)
    reserve = w3.eth.contract(address=target, abi=compiled["abi"])
    pool = client.contracts["conf_pool"]
    escrow = client.read(pool, "withdrawalEscrow")
    if (client.read(reserve, "confederationPool") != pool.address
            or client.read(reserve, "escrow") != escrow
            or client.read(reserve, "authorization") != client.read(pool, "frostMultisig")):
        raise ValueError("CONFEDERATION_DAO_BINDING")
    args = [client.read(reserve, "poolId"), client.read(reserve, "deposits"), escrow, pool.address]
    prove_creation(w3, reserve.address, kind, args, at, previous_build=True)
    return reserve


def _check_active_graph(client, load, pool, shares, income, staking, auth):
    decisions = load("decisions", client.read(income, "decisions"))
    sale = load("nft_pass", client.read(income, "passSale"))
    reserve = load("mining", client.read(pool, "bitcoinReserve"))
    gateway = load("custody_transfer", client.read(reserve, "custodyTransfer"))
    if (client.read(decisions, "staking") != staking.address or client.read(decisions, "authorization") != auth
            or client.read(sale, "income") != income.address
            or client.read(reserve, "confederationPool") != pool.address
            or client.read(reserve, "shareToken") != shares.address
            or client.read(gateway, "mining") != reserve.address or client.read(gateway, "pool") != pool.address):
        raise ValueError("CONFEDERATION_DAO_BINDING")


def _add(client, name, node, kind):
    client.contracts[name] = node
    client.config["contracts"][name] = dict(address=node.address, artifact=kind,
        runtime_hash=Web3.to_hex(Web3.keccak(client.w3.eth.get_code(node.address, client.block.number))))


def _income_coverage(client, tokens):
    income = client.contracts["dao_income"]
    rows = []
    for token in tokens:
        asset = contract(client.w3, "IERC20Metadata", token)
        held = client.read(asset, "balanceOf", income.address)
        accounted = client.read(income, "accountedBalance", token)
        rows.append(dict(token=token, balance=str(held), accounted=str(accounted),
                         shortfall=str(max(0, accounted - held))))
    return rows


def _caller(a):
    caller = account_at(a.identity_file).address if a.identity_file else address(a.sender)
    if a.sender and caller != address(a.sender):
        raise ValueError("SENDER_IDENTITY_MISMATCH")
    return caller


def _transact(client, a, name, method, values, *, suffix=""):
    caller = _caller(a)
    if a.send and not a.id:
        raise ValueError("INVALID_OPERATION_ID")
    return client.transact(name, method, values, caller, a.identity_file, a.send, a.state,
                           (a.id or "dao-preview") + suffix)


def _deposit_raw(raw, txid):
    from . import crypto

    if not isinstance(raw, str) or len(raw) > 200_000 or not re.fullmatch(r"(?:[0-9a-f]{2})+", raw):
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_SHAPE")
    transaction = crypto.call("bitcoin_funding", raw=raw, txid=txid.hex())
    if transaction["coinbase"] or not 0 < int(transaction["weight"]) <= 400_000:
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_SHAPE")
    return transaction


def _deposit_output(client, txid, vout, sats, recipient, transaction):
    from .bitcoin_funding import _has_deposit_marker, deposit_commitment

    inputs, outputs = transaction["inputs"], transaction["outputs"]
    if transaction["txid"] != txid.hex():
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_MISMATCH")
    if not 1 <= len(inputs) <= 256 or not 1 <= len(outputs) <= 32 or vout >= len(outputs):
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_SHAPE")
    reserve = client.contracts["mining"]
    expected_script = bytes(client.read(reserve, "payoutScript"))
    selected = outputs[vout]
    if (selected["vout"] != str(vout) or selected["script"] != expected_script.hex()
            or int(selected["amount"]) != sats):
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_MISMATCH")
    marker = deposit_commitment(client.config["chain_id"], reserve.address, vout,
                                [recipient], [str(sats * 10**10)])["script"]
    marked = [item for item in outputs if _has_deposit_marker(item["script"])]
    if len(marked) != 1 or marked[0]["script"] != marker or int(marked[0]["amount"]) != 0:
        raise ValueError("BTC_MINT_DEPOSIT_COMMITMENT")
    return expected_script


def _deposit_core(client, a):
    from .bitcoin_network import PUBLIC_NETWORKS
    from .bitcoin_rpc import BitcoinRPC
    from .bootstrap_withdrawal_fee import genesis

    expected_genesis = bytes(client.read(client.contracts["custody_transfer"], "bitcoinGenesis")).hex()
    network = next((name for name in ("regtest", *PUBLIC_NETWORKS) if genesis(name)[2:] == expected_genesis), None)
    if network is None:
        raise ValueError("BITCOIN_CUSTODY_NETWORK_MISMATCH")
    bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie, network)
    chain = bitcoin.call("getblockchaininfo")
    expected_chain = "regtest" if network == "regtest" else PUBLIC_NETWORKS[network][0]
    if (not isinstance(chain, dict) or chain.get("chain") != expected_chain
            or bitcoin.call("getblockhash", 0) != expected_genesis):
        raise ValueError("BITCOIN_NETWORK_MISMATCH")
    if (network != "regtest" and (chain.get("initialblockdownload") is not False
                                 or type(chain.get("blocks")) is not int or chain["blocks"] < 1
                                 or chain["blocks"] != chain.get("headers"))):
        raise ValueError("MAINNET_CORE_NOT_SYNCED")
    return bitcoin, network


def _deposit_transaction(client, a, txid, vout, sats, recipient):
    """Review the exact local transaction before locking a quote or paying it."""
    from .bitcoin_onchain_plan import MAX_MONEY, hex_value
    from .bitcoin_rpc import satoshis
    from .smp1_deposit import inspect

    if not a.bitcoin_rpc or not a.bitcoin_cookie:
        raise ValueError("BITCOIN_RPC_REQUIRED")
    previous = getattr(a, "previous_txid", None)
    if previous:
        previous = hex_value(previous.lower().removeprefix("0x"), 32)
        if not any(previous) or previous == txid or not a.raw_tx_file:
            raise ValueError("BTC_S_DEPOSIT_REPLACEMENT")
    if a.raw_tx_file:
        raw = read_private(a.raw_tx_file, max_bytes=200_002).strip().lower()
        transaction = _deposit_raw(raw, txid)
        if raw == transaction["stripped"]:
            raise ValueError("BTC_S_DEPOSIT_FINAL_TRANSACTION_REQUIRED")
        if any(item["script_sig"] for item in transaction["inputs"]):
            raise ValueError("BTC_S_DEPOSIT_NATIVE_SEGWIT_REQUIRED")
        stripped = transaction["stripped"]
    elif a.psbt_file and a.action != "deposit-preflight":
        transaction = inspect(read_private(a.psbt_file, max_bytes=1_400_002).strip())
        stripped = transaction["unsigned"]
    else:
        raise ValueError("BTC_S_DEPOSIT_FINAL_TRANSACTION_REQUIRED" if a.action == "deposit-preflight"
                         else "BTC_S_DEPOSIT_TRANSACTION_REQUIRED")
    _deposit_output(client, txid, vout, sats, recipient, transaction)
    gateway = client.contracts["custody_transfer"]
    bitcoin, network = _deposit_core(client, a)

    # Only public outpoints go to the local Core endpoint. The EVM view sees
    # stripped native SegWit bytes, which cannot spend the inputs on their own.
    total_in, witness_bound = 0, 2
    replacement_seen = False
    seen = set()
    for item in transaction["inputs"]:
        outpoint = item["outpoint"]
        if outpoint in seen:
            raise ValueError("BTC_S_DEPOSIT_TRANSACTION_SHAPE")
        seen.add(outpoint)
        input_txid, input_vout = outpoint.split(":")
        input_vout = int(input_vout)
        utxo = bitcoin.call("gettxout", input_txid, input_vout, True)
        if utxo is None and previous:
            out = dict(txid=input_txid, vout=input_vout)
            spender = bitcoin.call("gettxspendingprevout", [out])
            if spender != [dict(out, spendingtxid=previous.hex())]:
                raise ValueError("BTC_S_DEPOSIT_REPLACEMENT")
            utxo = bitcoin.call("gettxout", input_txid, input_vout, False)
            replacement_seen = True
        if (not isinstance(utxo, dict) or type(utxo.get("confirmations")) is not int
                or utxo["confirmations"] < 0 or type(utxo.get("coinbase")) is not bool):
            raise ValueError("UTXO_MISMATCH")
        if utxo["coinbase"] and utxo["confirmations"] < 101:
            raise ValueError("UTXO_IMMATURE")
        script_info = utxo.get("scriptPubKey")
        prev_script = script_info.get("hex") if isinstance(script_info, dict) else None
        if not isinstance(prev_script, str) or not re.fullmatch(r"(?:0014[0-9a-f]{40}|5120[0-9a-f]{64})", prev_script):
            raise ValueError("BTC_S_DEPOSIT_NATIVE_SEGWIT_REQUIRED")
        previous_amount = satoshis(utxo.get("value"))
        total_in += previous_amount
        witness_bound += 109 if prev_script.startswith("0014") else 67
        if not a.raw_tx_file and (item["script"] != prev_script or int(item["amount"]) != previous_amount):
            raise ValueError("UTXO_MISMATCH")
    if previous and not replacement_seen:
        raise ValueError("BTC_S_DEPOSIT_REPLACEMENT")
    total_out = sum(int(item["amount"]) for item in transaction["outputs"])
    if not 0 < total_out < total_in <= MAX_MONEY:
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_SHAPE")
    if not a.raw_tx_file and len(stripped) // 2 + witness_bound > 100_000:
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_SHAPE")
    if client.read(gateway, "isSourceFunding", bytes.fromhex(stripped)):
        raise ValueError("BTC_S_DEPOSIT_CUSTODY_FLOW_REQUIRED")
    client.stable()
    result = dict(bitcoin_network=network, miner_fee_sats=str(total_in - total_out))
    if previous:
        result["previous_txid"] = previous.hex()
    return result


def _recovery_deposit_graph(client, script, network):
    from .custody_network import spv_network

    at = client.block.number
    reserve, pool = client.contracts["mining"], client.contracts["conf_pool"]
    escrow = _contract(client.w3, client.read(reserve, "escrow"), "BitcoinWithdrawalEscrowV2", at)
    deposits = _contract(client.w3, client.read(reserve, "deposits"), "BitcoinDepositRegistry", at)
    batches = _contract(client.w3, client.read(escrow, "batches"), "BitcoinBatchRegistry", at)
    spv = client.read(escrow, "spv")
    if (client.read(pool, "withdrawalEscrow") != escrow.address
            or not client.read(escrow, "localCustodyPools", pool.address)
            or client.read(escrow, "depositRegistry") != deposits.address
            or client.read(deposits, "spendRecorder") != escrow.address
            or client.read(batches, "withdrawalEscrow") != escrow.address
            or client.read(deposits, "spv") != spv
            or bytes(client.read(deposits, "poolScripts", reserve.address)) != Web3.keccak(script)):
        raise ValueError("CONFEDERATION_DAO_BINDING")
    if spv_network(client.w3, spv, at) != network:
        raise ValueError("BITCOIN_CUSTODY_NETWORK_MISMATCH")
    return deposits, batches


def _recovery_deposit(client, a, txid, vout, sats, recipient):
    """Review a paid output without asking Core for its already-spent inputs."""
    from eth_abi import encode
    from .bitcoin_onchain_plan import hex_value
    from .bitcoin_rpc import check_payment_block, payment_block_height, satoshis, verify_payment_proof

    if a.psbt_file or a.raw_tx_file or a.previous_txid:
        raise ValueError("BTC_S_DEPOSIT_TRANSACTION_SHAPE")
    if not a.bitcoin_rpc or not a.bitcoin_cookie:
        raise ValueError("BITCOIN_RPC_REQUIRED")
    bitcoin, network = _deposit_core(client, a)
    paid = bitcoin.call("getrawtransaction", txid.hex(), True)
    if (not isinstance(paid, dict) or type(paid.get("confirmations")) is not int or paid["confirmations"] < 1
            or not isinstance(paid.get("blockhash"), str) or not re.fullmatch(r"[0-9a-f]{64}", paid["blockhash"])):
        raise ValueError("PAYMENT_NOT_CONFIRMED")
    block = paid["blockhash"]
    height = payment_block_height(bitcoin, block)
    transaction = _deposit_raw(paid.get("hex"), txid)
    script = _deposit_output(client, txid, vout, sats, recipient, transaction)
    deposits, batches = _recovery_deposit_graph(client, script, network)
    depth = client.read(deposits, "depth")
    if paid["confirmations"] < depth:
        raise ValueError("BTC_MINT_PROOF_OR_ALLOCATION")
    verify_payment_proof(bitcoin, txid.hex(), block)
    utxo = bitcoin.call("gettxout", txid.hex(), vout, True)
    script_info = utxo.get("scriptPubKey") if isinstance(utxo, dict) else None
    if (not isinstance(script_info, dict) or script_info.get("hex") != script.hex()
            or utxo.get("coinbase") is not False or type(utxo.get("confirmations")) is not int
            or utxo["confirmations"] < depth or satoshis(utxo.get("value")) != sats):
        raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")

    def unavailable(display, index):
        types = ["bytes32", "uint32"]
        wire_key = Web3.keccak(encode(types, [display[::-1], index]))
        batch_key = Web3.keccak(encode(types, [display, index]))
        return (client.read(deposits, "consumed", wire_key) or client.read(deposits, "walletOutput", wire_key)
                or bytes(client.read(batches, "reservations", batch_key)) != bytes(32))

    if unavailable(txid, vout):
        raise ValueError("BTC_MINT_OUTPOINT_UNAVAILABLE")
    gateway = client.contracts["custody_transfer"]
    if (client.read(gateway, "isSourceFunding", bytes.fromhex(transaction["stripped"]))
            or client.read(deposits, "custodyTransactions", txid[::-1])):
        raise ValueError("BTC_S_DEPOSIT_CUSTODY_FLOW_REQUIRED")
    for item in transaction["inputs"]:
        previous, index = item["outpoint"].split(":")
        if unavailable(hex_value(previous, 32), int(index)):
            raise ValueError("BTC_S_DEPOSIT_CUSTODY_FLOW_REQUIRED")
    check_payment_block(bitcoin, block, height)
    client.stable()
    return dict(network=network, block_hash=block, confirmations=str(utxo["confirmations"]))


def _deposit_quote(client, a):
    from .bitcoin_funding import direct_deposit_quote
    from .bitcoin_onchain_plan import MAX_MONEY, decimal, hex_value

    txid = hex_value((a.txid or "").removeprefix("0x"), 32)
    if txid == bytes(32):
        raise ValueError("BTC_S_DEPOSIT_TXID_REQUIRED")
    vout = decimal(a.vout, 2**32 - 1)
    sats = decimal(a.satoshis, MAX_MONEY, positive=True)
    if a.action in ("deposit-quote", "deposit-preflight", "deposit-recovery-quote"):
        minimum = number(a.minimum_s)
        if not 0 < minimum <= sats * 10**10:
            raise ValueError("BTC_S_DEPOSIT_MINIMUM")
    if a.action in ("deposit-quote", "deposit-recovery-quote"):
        if a.recipient is not None:
            raise ValueError("BTC_S_DEPOSIT_QUOTE_RECIPIENT_IS_SENDER")
        if a.action == "deposit-recovery-quote":
            paid = _recovery_deposit(client, a, txid, vout, sats, _caller(a))
            result = _transact(client, a, "mining", "lockDirectDepositQuote", [txid[::-1], vout, sats, minimum])
            return dict(result, bitcoin_deposit=paid)
        if a.send or a.psbt_file or a.raw_tx_file or a.previous_txid:
            _deposit_transaction(client, a, txid, vout, sats, _caller(a))
        return _transact(client, a, "mining", "lockDirectDepositQuote", [txid[::-1], vout, sats, minimum])

    if a.send:
        raise ValueError("BTC_S_DEPOSIT_STATUS_READ_ONLY")
    reserve = client.contracts["mining"]
    recipient = address(a.recipient)
    terms = direct_deposit_quote(reserve, client.block.number, txid[::-1], vout, recipient, sats)
    if a.action == "deposit-preflight":
        if terms is None:
            raise ValueError("BTC_S_DEPOSIT_QUOTE_REQUIRED")
        if int(terms["net_s"]) < minimum:
            raise ValueError("BTC_S_DEPOSIT_MINIMUM")
        reviewed = _deposit_transaction(client, a, txid, vout, sats, recipient)
        status = "PREFLIGHT_VERIFIED"
    else:
        reviewed = {}
        status = "LOCKED" if terms else "QUOTE_NOT_FOUND"
    client.stable()
    return dict(status=status, reserve=reserve.address,
                txid=txid.hex(), vout=str(vout), recipient=recipient, satoshis=str(sats),
                block_number=str(client.block.number), **(terms or {}), **reviewed)


def _withdraw(client, a):
    from .bitcoin_onchain_plan import MAX_MONEY, decimal, script

    if a.pool_token:
        raise ValueError("S_WITHDRAWAL_USES_CONFEDERATION_RESERVE")
    requested_minimum = (decimal(a.minimum_btc_sats, MAX_MONEY, positive=True)
                         if a.minimum_btc_sats is not None else None)
    shares = number(a.amount)
    destination = bytes.fromhex(script(a.script))
    cap = decimal(a.miner_fee_cap_sats)
    if a.action == "withdraw-quote":
        if a.send:
            raise ValueError("S_WITHDRAWAL_QUOTE_READ_ONLY")
        pool = client.contracts["conf_pool"]
        nft = number(a.nft) if a.nft is not None else 0
        block = client.block.number
        fee, gross, effective_cap, script_minimum = pool.functions.quoteWithdrawal(
            shares, destination, cap, a.nft is not None, nft
        ).call({"from": _caller(a)}, block_identifier=block)
        client.stable()
        minimum = script_minimum if requested_minimum is None else requested_minimum
        return dict(status="QUOTED", block_number=str(block), protocol_fee_s=str(fee),
                    gross_sats=str(gross), fee_cap_sats=str(effective_cap),
                    script_minimum_sats=str(script_minimum), minimum_btc_sats=str(minimum),
                    max_miner_fee_share_sats=str(max(0, gross - minimum)),
                    payout_at_cap_sats=str(max(0, gross - effective_cap)),
                    fits=minimum >= script_minimum and gross >= minimum + effective_cap)

    if requested_minimum is None or a.deadline is None:
        raise ValueError("S_WITHDRAWAL_MINIMUM_AND_DEADLINE_REQUIRED")
    deadline = decimal(a.deadline, positive=True)
    method = "requestWithdrawal" if a.nft is None else "requestWithdrawalWithNFT"
    values = [shares, destination]
    if a.nft is not None:
        values.append(number(a.nft))
    values.extend([cap, requested_minimum, deadline])
    client.stable()
    return _transact(client, a, "conf_pool", method, values)


def _operation(client, a):
    from .bootstrap_confederation_dao import review
    from .onchain_transport import board_at, ceremony_bundle, can_publish_intent

    w3, at = client.w3, client.block.number
    intent = getattr(a, "reviewed_intent", None) or read_public(a.intent)
    kind, helper = intent["artifact"], intent["hash_method"]
    method = METHODS.get((kind, helper))
    if method is None:
        raise ValueError("CONFEDERATION_DAO_METHOD")
    name = next(name for name, artifact in NODES.items() if artifact == kind)
    target = client.contracts[name]
    bundle = ceremony_bundle(w3, a.board, "conf_ceremony")
    if target.address != intent["target"] or intent["chain_id"] != str(w3.eth.chain_id):
        raise ValueError("CONFEDERATION_DAO_BINDING")
    auth_address = client.read("dao_income", "authorization")
    auth = _contract(w3, auth_address, "DKGAuthorization", at)
    _add(client, "authorization", auth, "DKGAuthorization")
    coordinator = _contract(w3, _read(auth, at, "coordinator"), "FROSTCoordinator", at)
    _add(client, "coordinator", coordinator, "FROSTCoordinator")
    intent.update(role="conf_ceremony", coordinator=coordinator.address)
    op = bytes.fromhex(intent["operation_hash"].removeprefix("0x"))
    if _read(auth, at, "executedOperations", op):
        if _read(auth, at, "operationTargets", op) != target.address:
            raise ValueError("CONFEDERATION_DAO_BINDING")
        return dict(status="ALREADY_EXECUTED", operation_hash=Web3.to_hex(op))
    reviewed = review(w3, intent, bundle, at)
    if reviewed is None or reviewed[0] != auth_address:
        raise ValueError("CONFEDERATION_DAO_BINDING")
    abi = next(row["inputs"] for row in target.abi if row.get("name") == helper)
    values = encode_args(abi, intent["hash_args"])
    if bytes(_read(target, at, helper, *values)) != op:
        raise ValueError("OPERATION_PARAMETERS_MISMATCH")

    if a.action == "execute":
        if not _read(auth, at, "isOperationApproved", op, target.address):
            if not a.send:
                return dict(status="AUTHORIZATION_REQUIRED", operation_hash=Web3.to_hex(op))
            authorization = _transact(client, a, "authorization", "authorizeOperation",
                                      [number(a.session), op, target.address], suffix="-auth")
            if authorization["status"] != "VERIFIED":
                return authorization
        return _transact(client, a, name, method, [*values, op], suffix="-exec")

    board = board_at(w3, a.board, client.block)
    sender = account_at(a.identity_file).address if a.identity_file else address(a.sender)
    if not can_publish_intent(board, sender, at):
        raise ValueError("ULR_BOARD_OPERATOR_REQUIRED")
    if len(canonical(dict(intent, session=str(2**256 - 1))).encode()) > 32768:
        raise ValueError("ULR_PUBLIC_INTENT_TOO_LARGE")
    _add(client, "board", board, "DKGOnchainBoard")
    result = _transact(client, a, "coordinator", "createSessionFromDKG",
        [_read(auth, at, "dkgId"), op, target.address, number(a.deadline)], suffix="-open")
    if result["status"] != "VERIFIED":
        return result
    receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
    events = coordinator.events.SessionCreated().process_receipt(receipt, errors=DISCARD)
    if len(events) != 1:
        raise ValueError("ULR_SESSION_RECEIPT")
    session = int(events[0]["args"]["sessionId"])
    published = _transact(client, a, "board", "publishIntent",
                          [session, canonical(dict(intent, session=str(session))).encode()], suffix="-publish")
    if published["status"] != "VERIFIED":
        return published
    return dict(status="AWAITING_PARTICIPANTS", session=str(session), operation_hash=Web3.to_hex(op))


def _prepare_intent(client, a):
    from .bootstrap_confederation_dao import validate_pass_pricing

    if a.action == "tariff-prepare":
        name, helper = "nft_pass", "getApproveTariffOperationHash"
        args = [[a.duration, a.discount_bps, a.volume_limit, a.minimum_price, a.sales_until]]
    elif a.action == "price-control-prepare":
        name, helper = "nft_pass", "getSetPriceControlOperationHash"
        if a.sales_paused is None:
            raise ValueError("CONFEDERATION_DAO_PRICE_CONTROL")
        args = [address(a.price_signer), a.sales_paused == "true"]
    elif a.action == "stablecoin-prepare":
        name, helper = "dao_income", "getAddStablecoinOperationHash"
        args = [address(a.token)]
    elif a.action == "closing-rates-prepare":
        name, helper = "dao_income", CLOSING_RATES_HELPER
        tokens = client.read(name, "rewardTokens")
        rates = read_public(a.rates) if a.rates else None
        if not isinstance(rates, dict) or set(rates) != set(tokens):
            raise ValueError("CONFEDERATION_DAO_CLOSING_RATES")
        args = [str(client.block.timestamp // 86400), tokens, [rates[token] for token in tokens]]
    elif a.action == "decision-prepare":
        name, helper = "decisions", "getCreateOperationHash"
        args = [a.metadata_uri, a.content_hash, a.duration, a.quorum_bps]
    else:
        name = "validator"
        helper = ("getAddCustodianOperationHash" if a.action == "custodian-add-prepare"
                  else "getRemoveCustodianOperationHash")
        args = [address(a.custodian)]
    node = client.contracts[name]
    abi = next(row["inputs"] for row in node.abi if row.get("name") == helper)
    values = encode_args(abi, args)
    if name == "nft_pass":
        validate_pass_pricing(helper, values, client.block.timestamp, lambda method: client.read(node, method))
    elif helper == CLOSING_RATES_HELPER:
        validate_closing_rates(node, client.block, values)
    op = client.read(node, helper, *values)
    client.stable()
    intent = dict(chain_id=client.config["chain_id"], target=node.address, artifact=NODES[name], hash_method=helper,
                  hash_args=args, operation_hash=Web3.to_hex(op))
    if name == "decisions":
        intent["dao_pool"] = client.contracts["conf_pool"].address
    from pathlib import Path
    if Path(a.output).exists() or Path(a.output).is_symlink():
        raise ValueError("OUTPUT_EXISTS")
    write_public(a.output, intent)
    result = dict(status="PREPARED", operation_hash=intent["operation_hash"])
    if a.action == "tariff-prepare":
        from eth_utils.abi import collapse_if_tuple

        result["tariff_id"] = Web3.to_hex(Web3.keccak(client.w3.codec.encode([collapse_if_tuple(abi[0])], values)))
    return result


def _quote(client, a):
    if not a.identity_file:
        raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
    from pathlib import Path
    from eth_account.messages import encode_typed_data

    sale = client.contracts["nft_pass"]
    account = account_at(a.identity_file)
    if account.address != client.read(sale, "priceSigner"):
        raise ValueError("SENDER_IDENTITY_MISMATCH")
    if client.read(sale, "salesPaused"):
        raise ValueError("CONFEDERATION_DAO_PRICE_CONTROL")
    epoch = str(client.read(sale, "priceEpoch"))
    quote = ["0x" + bytes.fromhex(a.tariff.removeprefix("0x")).hex(), address(a.buyer),
             a.amount, a.nonce, a.deadline, epoch]
    abi = next(row["inputs"] for row in sale.abi if row.get("name") == "priceDigest")
    typed, = encode_args(abi, [quote])
    duration, _, _, minimum_price, sales_until = client.read(sale, "tariffs", typed[0])
    if (duration == 0 or typed[2] < minimum_price or not client.block.timestamp < typed[4] <= sales_until
            or client.read(sale, "usedQuote", typed[1], typed[3])):
        raise ValueError("CONFEDERATION_DAO_QUOTE")
    digest = client.read(sale, "priceDigest", typed)
    domain = dict(name="SatoshiFi Confederation Pass", version="2",
                  chainId=int(client.config["chain_id"]), verifyingContract=sale.address)
    fields = (("tariffId", "bytes32"), ("buyer", "address"), ("token", "address"),
              ("price", "uint256"), ("nonce", "uint256"), ("expiresAt", "uint64"), ("priceEpoch", "uint64"))
    types = {"Price": [dict(name=name, type=type_) for name, type_ in fields]}
    message = encode_typed_data(domain, types, dict(zip((name for name, _ in fields),
        (*typed[:2], client.contracts["conf_shares"].address, *typed[2:]))))
    if Web3.keccak(b"\x19" + message.version + message.header + message.body) != digest:
        raise ValueError("CONFEDERATION_DAO_QUOTE")
    client.stable()
    if Path(a.output).exists() or Path(a.output).is_symlink():
        raise ValueError("OUTPUT_EXISTS")
    signature = account.sign_message(message).signature
    write_public(a.output, dict(quote=quote, signature=Web3.to_hex(signature)))
    return dict(status="PREPARED", digest=Web3.to_hex(digest), price_epoch=epoch)


def main(argv):
    p = DiagnosticArgumentParser(prog="frost-signer dao in-kind", description=__doc__)
    p.add_argument("action", choices=("discover", "status", "approve", "stake", "unstake", "sweep", "process",
        "closing-status", "closing-sweep", "closing-process", "closing-claim", "closing-rates-prepare",
        "claim", "reimburse", "approve-pass", "buy-pass", "tariff-prepare", "price-control-prepare",
        "stablecoin-prepare", "price",
        "approve-wrap", "wrap", "deposit-quote", "deposit-quote-status", "deposit-preflight", "deposit-recovery-quote",
        "withdraw", "withdraw-quote", "cancel", "delegate", "propose", "execute", "vote", "finalize",
        "decision-prepare", "custodian-add-prepare", "custodian-remove-prepare", "wrap-pass", "clear-wrap-pass"))
    p.add_argument("--rpc", required=True)
    graph = p.add_mutually_exclusive_group(required=True)
    graph.add_argument("--manifest")
    graph.add_argument("--factory")
    p.add_argument("--confederation-id")
    p.add_argument("--chain-id")
    p.add_argument("--confirmations", type=int, default=12)
    p.add_argument("--token")
    p.add_argument("--rates", help="JSON mapping approved token addresses to base units per ETH for closing-day payment")
    p.add_argument("--amount")
    p.add_argument("--days", nargs="+")
    p.add_argument("--rows", default="8")
    p.add_argument("--pool-token", help="MP token for wrap")
    p.add_argument("--source", type=int, help="Enrolled federation source index for MP-to-S handover")
    p.add_argument("--minimum-s", help="Minimum net S base units for the MP handover or direct BTC deposit")
    p.add_argument("--recipient", help="S recipient for the BTC handover or the direct deposit quote lookup")
    p.add_argument("--txid", help="Final Bitcoin txid in display order for the direct S deposit")
    p.add_argument("--vout", help="Bitcoin output index for the direct S deposit")
    p.add_argument("--satoshis", help="Exact Bitcoin output amount for the direct S deposit")
    deposit_source = p.add_mutually_exclusive_group()
    deposit_source.add_argument("--psbt-file", help="Private native SegWit PSBT file for direct deposit quote")
    deposit_source.add_argument("--raw-tx-file", help="Private native SegWit signed Bitcoin hex file")
    p.add_argument("--previous-txid", help="Exact previous Bitcoin deposit seen in the mempool for a replacement")
    p.add_argument("--bitcoin-rpc", help="Local Bitcoin Core probe for the direct S deposit")
    p.add_argument("--bitcoin-cookie", help="Private Bitcoin Core cookie file")
    p.add_argument("--script")
    p.add_argument("--miner-fee-cap-sats", default=str(2**64 - 1),
                   help="Maximum sats this request pays to miners; defaults to the escrow configuration")
    p.add_argument("--minimum-btc-sats", help="Minimum net BTC in satoshis for V3 withdrawal")
    p.add_argument("--nft")
    p.add_argument("--withdrawal")
    p.add_argument("--quote")
    p.add_argument("--output")
    p.add_argument("--duration")
    p.add_argument("--discount-bps")
    p.add_argument("--volume-limit")
    p.add_argument("--minimum-price", help="Minimum S price in base units for this tariff")
    p.add_argument("--sales-until", help="Exclusive Unix timestamp when new purchases under this tariff stop")
    p.add_argument("--price-signer", help="Next NFT price signer, or the current address to change the pause status")
    p.add_argument("--sales-paused", choices=("true", "false"), help="Set whether new NFT purchases are paused")
    p.add_argument("--tariff")
    p.add_argument("--buyer")
    p.add_argument("--nonce")
    p.add_argument("--delegate")
    p.add_argument("--consent", default="0x")
    p.add_argument("--custodian")
    p.add_argument("--decision")
    p.add_argument("--owner")
    p.add_argument("--choice", choices=("against", "for", "abstain"))
    p.add_argument("--metadata-uri")
    p.add_argument("--content-hash")
    p.add_argument("--quorum-bps")
    p.add_argument("--board")
    p.add_argument("--intent")
    p.add_argument("--deadline", help="Latest Unix timestamp at which a withdrawal may be accepted")
    p.add_argument("--session")
    p.add_argument("--sender")
    p.add_argument("--identity-file")
    p.add_argument("--state")
    p.add_argument("--id")
    p.add_argument("--send", action="store_true")
    a = p.parse_args(argv)
    client = _client(a)
    if a.action == "discover":
        result = dict(status="OK", **client.config)
    elif a.action == "closing-status":
        income = client.contracts["dao_income"]
        tokens = client.read(income, "rewardTokens")
        next_day = client.read(income, "nextSettlementDay")
        last_day = int(client.config["last_settlement_day"])
        result = dict(status="OK", last_day=str(last_day), next_day=str(next_day),
            processing=client.read(income, "processing"), ready=next_day == last_day + 1,
            debt_head=str(client.read(income, "debtHead")), debt_tail=str(client.read(income, "debtTail")),
            closing_rates_day=str(client.read(income, "closingRatesDay")),
            tokens=[dict(token=token, pending=str(client.read(income, "closingIncome", token)),
                         available=str(client.read(income, "availableIncome", token)),
                         reward=str(client.read(income, "closingReward", token))) for token in tokens])
    elif a.action in ("closing-sweep", "closing-process", "closing-claim"):
        method = {"closing-sweep": "sweep", "closing-process": "processClosingIncome",
                  "closing-claim": "claimClosingReward"}[a.action]
        values = [number(a.rows)] if a.action == "closing-process" else [address(a.token)]
        result = _transact(client, a, "dao_income", method, values)
    elif a.action == "status":
        reward_tokens = client.read("dao_income", "rewardTokens")
        result = dict(status="OK", next_day=str(client.read("dao_income", "nextSettlementDay")),
            processing=client.read("dao_income", "processing"),
            staged_expenses=str(client.read("dao_income", "stagedExpenseCount")),
            staged_expense_position=str(client.read("dao_income", "stagedExpensePosition")),
            staged_expense_commitment=Web3.to_hex(client.read("dao_income", "stagedExpenseCommitment")),
            reward_tokens=reward_tokens, coverage=_income_coverage(client, reward_tokens),
            pass_pricing=dict(signer=client.read("nft_pass", "priceSigner"),
                              epoch=str(client.read("nft_pass", "priceEpoch")),
                              paused=client.read("nft_pass", "salesPaused")))
    elif a.action.endswith("-prepare"):
        result = _prepare_intent(client, a)
    elif a.action == "price":
        result = _quote(client, a)
    elif a.action in ("deposit-quote", "deposit-quote-status", "deposit-preflight", "deposit-recovery-quote"):
        result = _deposit_quote(client, a)
    elif a.action in ("propose", "execute"):
        result = _operation(client, a)
    elif a.action in ("approve", "approve-pass"):
        share = a.action == "approve-pass"
        target = client.contracts["nft_pass" if share else "staking"]
        result = _transact(client, a, "conf_shares" if share else "dao_token", "approve",
                           [target.address, number(a.amount)])
    elif a.action in ("stake", "unstake"):
        result = _transact(client, a, "staking", a.action, [number(a.amount)])
    elif a.action == "wrap-pass":
        result = _transact(client, a, "conf_pool", "setWrapPass", [number(a.nft)])
    elif a.action == "clear-wrap-pass":
        result = _transact(client, a, "conf_pool", "clearWrapPass", [])
    elif a.action in ("vote", "finalize"):
        values = [number(a.decision)]
        if a.action == "vote":
            values.extend([address(a.owner), ("against", "for", "abstain").index(a.choice)])
        result = _transact(client, a, "decisions", a.action, values)
    elif a.action in ("sweep", "process", "claim", "reimburse"):
        method = {"reimburse": "claimReimbursement", "process": "processDay"}.get(a.action, a.action)
        values = [number(a.rows)] if a.action == "process" else [address(a.token)]
        if a.action == "claim":
            values.append(claim_days(a.days or []))
        result = _transact(client, a, "dao_income", method, values)
    elif a.action == "buy-pass":
        doc = read_public(a.quote)
        node = client.contracts["nft_pass"]
        abi = next(row["inputs"] for row in node.abi if row.get("name") == "buy")
        result = _transact(client, a, "nft_pass", "buy", encode_args(abi, [doc["quote"], doc["signature"]]))
    elif a.action in ("approve-wrap", "wrap"):
        gateway = client.contracts["custody_transfer"]
        source = a.source
        if source is None and client.read(gateway, "sourceCount") == 1:
            source = 0
        if source is None or not 0 <= source < client.read(gateway, "sourceCount"):
            raise ValueError("BITCOIN_CUSTODY_SOURCE")
        entry = client.read(gateway, "sourceAt", source)
        if a.pool_token and address(a.pool_token) != entry[1]:
            raise ValueError("BITCOIN_CUSTODY_SOURCE")
        if a.action == "approve-wrap":
            token = _contract(client.w3, entry[1], "PoolMpToken", client.block.number)
            _add(client, "mp", token, "PoolMpToken")
            result = _transact(client, a, "mp", "approve", [gateway.address, number(a.amount)])
        else:
            recipient = address(a.recipient) if a.recipient else (account_at(a.identity_file).address
                        if a.identity_file else address(a.sender))
            if a.nft is not None:
                caller = account_at(a.identity_file).address if a.identity_file else address(a.sender)
                if recipient != caller:
                    raise ValueError("S_WRAP_PASS_OWNER")
                configured = _transact(client, a, "conf_pool", "setWrapPass", [number(a.nft)], suffix="-pass")
                if configured["status"] != "VERIFIED":
                    print(json.dumps(configured))
                    return 0
            result = _transact(client, a, "custody_transfer", "request",
                [source, number(a.amount), number(a.minimum_s), recipient])
    elif a.action == "delegate":
        result = _transact(client, a, "staking", "setDAOSigner",
                           [address(a.delegate), bytes.fromhex(a.consent.removeprefix("0x"))])
    elif a.action in ("withdraw", "withdraw-quote"):
        result = _withdraw(client, a)
    else:
        result = _transact(client, a, "conf_pool", "cancelWithdrawal", [number(a.withdrawal)])
    print(json.dumps(result))
    return 0
