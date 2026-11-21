"""Build and independently verify an in-kind DAO daily report from canonical receipts."""

import hashlib

from web3 import Web3
from web3.logs import DISCARD

from .bootstrap import artifact, encode_args
from .bitcoin_trust import checked_runtime
from .evm_network import read_block
from .launch_economics import integer
from .operations import canonical


ARTIFACT = "ConfederationDAOIncome"
HELPER = "getSettlementOperationHash"
MAX_UINT128 = 2**128 - 1


def _contract(w3, address, kind, block):
    checked_runtime(w3, address, kind, block)
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=artifact(kind)["abi"])


def _read(contract, block, method, *args):
    return getattr(contract.functions, method)(*args).call(block_identifier=block)


def _targets(w3, income, at):
    def getter(node, method, *args):
        return _read(node, at, method, *args)

    pool = _contract(w3, getter(income, "pool"), "ConfederationPoolV2", at)
    escrow = _contract(w3, getter(pool, "withdrawalEscrow"), "BitcoinWithdrawalEscrowV2", at)
    authorization = _contract(w3, getter(income, "authorization"), "DKGAuthorization", at)
    validator = _contract(w3, getter(income, "validator"), "CustodianValidator", at)
    coordinator = _contract(w3, getter(authorization, "coordinator"), "FROSTCoordinator", at)
    registry = _contract(w3, getter(escrow, "depositRegistry"), "BitcoinDepositRegistry", at)
    if (getter(pool, "daoIncome") != income.address or getter(pool, "frostMultisig") != authorization.address
            or getter(pool, "custodianValidator") != validator.address
            or getter(registry, "authorization") != authorization.address):
        raise ValueError("DAO_EXPENSE_GRAPH")
    targets = {
        income.address: (income, {"sweep", "openDay", "processDay"}),
        escrow.address: (escrow, {"bindUserFunded", "bindUserFundedReplacement", "settle"}),
        registry.address: (registry, {"cacheValue"}),
        validator.address: (validator, {"registerCustodianSignature"}),
        authorization.address: (authorization, {"authorizeOperation"}),
        coordinator.address: (coordinator, {"submitNonceCommitments", "submitVerifiableSignatureShare", "finalizeSession"}),
        pool.address: (pool, {"processWithdrawal"}),
    }
    for token_address in getter(pool, "getAllPools"):
        token = _contract(w3, token_address, "PoolMpToken", at)
        mining = _contract(w3, getter(token, "poolDAO"), "MiningPoolV1", at)
        if (getter(mining, "mpToken") != token.address or getter(mining, "deposits") != registry.address
                or getter(mining, "escrow") != escrow.address):
            raise ValueError("DAO_EXPENSE_GRAPH")
        targets[mining.address] = (mining, {"mintFromProof", "mintFromProofV2", "mintFromAddressProof"})
    spv = getter(escrow, "spv")
    for kind in ("RegtestHeaderRelay", "MainnetSPVGuard", "ZkSpvAdapter", "ZkTestnet4SpvAdapter"):
        try:
            node = _contract(w3, spv, kind, at)
        except ValueError as error:
            if str(error) != "BTC_CONTRACT_BUILD_MISMATCH":
                raise
            continue
        if kind == "RegtestHeaderRelay":
            targets[spv] = (node, {"addBlockHeader", "addBlockHeaders"})
        elif kind == "MainnetSPVGuard":
            address = getter(node, "relay")
            for relay_kind in ("MainnetHeaderRelay", "Testnet4HeaderRelay"):
                try:
                    relay = _contract(w3, address, relay_kind, at)
                except ValueError as error:
                    if str(error) != "BTC_CONTRACT_BUILD_MISMATCH":
                        raise
                    continue
                targets[address] = (relay, {"addBlockHeader", "addBlockHeaders"})
                break
        else:
            state_kind = "ZkBitcoinStateV2" if kind == "ZkSpvAdapter" else "ZkBitcoinTestnet4StateV2"
            state = _contract(w3, getter(node, "state"), state_kind, at)
            targets[spv] = (node, {"materializeHeader"})
            targets[state.address] = (state, {"submitTransition"})
        break
    return targets, coordinator, bytes(getter(authorization, "dkgId"))


def _receipt(w3, digest, day, block, targets, coordinator, epoch, anchors):
    if not isinstance(digest, str) or not digest.startswith("0x") or len(digest) != 66:
        raise ValueError("DAO_EXPENSE_HASH")
    tx = w3.eth.get_transaction(digest)
    receipt = w3.eth.get_transaction_receipt(digest)
    block_number = receipt["blockNumber"]
    if block_number > block.number:
        raise ValueError("DAO_EXPENSE_UNCONFIRMED")
    if block_number not in anchors:
        anchors[block_number] = w3.eth.get_block(block_number)
    anchor = anchors[block_number]
    index = receipt["transactionIndex"]
    if (receipt["status"] != 1 or Web3.to_hex(tx["hash"]) != digest.lower()
            or receipt["transactionHash"] != tx["hash"] or receipt["blockHash"] != anchor.hash
            or tx["blockHash"] != anchor.hash or tx["blockNumber"] != block_number or tx["transactionIndex"] != index
            or not 0 <= index < len(anchor.transactions) or anchor.transactions[index] != tx["hash"]
            or anchor.timestamp // 86400 != day or tx["to"] not in targets or tx["to"] != receipt["to"]
            or tx["from"] != receipt["from"]):
        raise ValueError("DAO_EXPENSE_RECEIPT")
    target, methods = targets[tx["to"]]
    calldata = Web3.to_bytes(hexstr=tx["input"]) if isinstance(tx["input"], str) else bytes(tx["input"])
    fn, args = target.decode_function_input(calldata)
    if fn.fn_name not in methods:
        raise ValueError("DAO_EXPENSE_METHOD")
    if fn.fn_name == "sweep" and not any(
            item["address"] == target.address and item["args"]["stream"] == 3 and item["args"]["amount"] > 0
            for item in target.events.Income().process_receipt(receipt, errors=DISCARD)):
        raise ValueError("DAO_EXPENSE_EMPTY_SWEEP")
    if target.address == coordinator.address:
        session = args["sessionId"]
        if bytes(_read(coordinator, block.number, "sessionDKGId", session)) != epoch:
            raise ValueError("DAO_EXPENSE_EPOCH")
    wei_cost = receipt["gasUsed"] * receipt["effectiveGasPrice"]
    if not 0 < wei_cost <= MAX_UINT128:
        raise ValueError("DAO_EXPENSE_AMOUNT")
    return dict(transaction=Web3.to_hex(tx["hash"]), block=Web3.to_hex(anchor.hash), block_number=str(block_number),
                transaction_index=str(index), payer=tx["from"], target=target.address,
                selector=Web3.to_hex(calldata[:4]), gas_used=str(receipt["gasUsed"]),
                effective_gas_price=str(receipt["effectiveGasPrice"]), wei=str(wei_cost))


def prepare(w3, settlement, inputs, block):
    node = _contract(w3, settlement, ARTIFACT, block.number)
    if set(inputs) != {"day", "transactions", "units_per_eth"} or not isinstance(inputs["transactions"], list):
        raise ValueError("DAO_REPORT_INPUTS")
    day = integer(inputs["day"], 0, 2**64 - 1)
    if day != _read(node, block.number, "nextSettlementDay") or day >= block.timestamp // 86400:
        raise ValueError("DAO_REPORT_DAY")
    tokens = _read(node, block.number, "rewardTokens")
    rates = inputs["units_per_eth"]
    if not isinstance(rates, dict) or set(rates) != set(tokens):
        raise ValueError("DAO_REPORT_RATES")
    ordered_rates = [str(integer(rates[token], 1, MAX_UINT128)) for token in tokens]
    transactions = inputs["transactions"]
    if not all(isinstance(digest, str) for digest in transactions):
        raise ValueError("DAO_EXPENSE_HASH")
    if (len(transactions) > _read(node, block.number, "MAX_EXPENSES")
            or len({digest.lower() for digest in transactions}) != len(transactions)):
        raise ValueError("DAO_EXPENSE_LIMIT")
    targets, coordinator, epoch = _targets(w3, node, block.number) if transactions else ({}, None, None)
    anchors = {block.number: block}
    expenses = [_receipt(w3, tx, day, block, targets, coordinator, epoch, anchors) for tx in transactions]
    expenses.sort(key=lambda row: (int(row["block_number"]), int(row["transaction_index"])))
    income = [[str(_read(node, block.number, "dayIncome", day, token, stream)) for stream in range(4)]
              for token in tokens]
    evidence = canonical(dict(version="2", chain_id=str(w3.eth.chain_id), settlement=node.address, day=str(day),
        tokens=tokens, income=income, units_per_eth=ordered_rates, expenses=expenses)).encode()
    report = [str(day), "0x" + hashlib.sha256(evidence).hexdigest(), tokens, income, ordered_rates,
              [[row["payer"], row["transaction"], row["wei"]] for row in expenses]]
    abi = next(entry["inputs"] for entry in node.abi if entry.get("name") == HELPER)
    op = _read(node, block.number, HELPER, *encode_args(abi, [report]))
    if w3.eth.get_block(block.number).hash != block.hash:
        raise ValueError("REPORT_REORG")
    return evidence, dict(chain_id=str(w3.eth.chain_id), target=node.address, artifact=ARTIFACT, hash_method=HELPER,
                          hash_args=[report], operation_hash=Web3.to_hex(op))


def review(w3, intent, confirmations, at):
    block = w3.eth.get_block(at)
    if block.number > read_block(w3, int(confirmations)).number:
        raise ValueError("DAO_EXPENSE_UNCONFIRMED")
    report, = intent["hash_args"]
    day, _, tokens, _, rates, expenses = report
    _, expected = prepare(w3, intent["target"], dict(day=day, units_per_eth=dict(zip(tokens, rates)),
        transactions=[item[1] for item in expenses]), block)
    if expected["hash_args"] != [report] or expected["operation_hash"] != "0x" + intent[
            "operation_hash"].removeprefix("0x"):
        raise ValueError("DAO_REPORT_MISMATCH")

