"""Wallet and keeper commands for the separate in-kind DAO profile."""

import json

from web3 import Web3
from web3.logs import DISCARD

from .bootstrap import encode_args
from .confederation_dao_report import _contract, _read
from .dao_client import DAOClient, account_at, address, claim_days
from .evm_network import read_block
from .onboarding import read_public, write_public
from .operations import canonical
from .safe_errors import DiagnosticArgumentParser
from .ulr_treasury import number

METHODS = {
    ("ConfederationDAOIncome", "getSettlementOperationHash"): "openDay",
    ("ConfederationDAOIncome", "getStageExpensesOperationHash"): "stageExpenses",
    ("ConfederationDAOIncome", "getAddStablecoinOperationHash"): "addStablecoin",
    ("ConfederationNFTPass", "getApproveTariffOperationHash"): "approveTariff",
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
    factory = _contract(client.w3, address(factory_address), "ConfederationFactory", at)
    client.config = dict(chain_id=chain_id, confederation_id=conf_id, factory=factory.address,
                         confirmations=a.confirmations, contracts={})

    def load(name, target):
        kind = NODES[name]
        node = _contract(client.w3, address(target), kind, at)
        _add(client, name, node, kind)
        if manifest:
            item = manifest["contracts"][name]
            if (node.address != item["address"] or client.config["contracts"][name]["runtime_hash"]
                    != Web3.to_hex(bytes.fromhex(item["runtime_hash"].removeprefix("0x")))):
                raise ValueError("CONFEDERATION_DAO_MANIFEST")
        return node

    conf = client.read(factory, "getConfederation", conf_id)
    if not conf[-1]:
        raise ValueError("CONFEDERATION_INACTIVE")
    pool = load("conf_pool", conf[9])
    validator = load("validator", conf[8])
    shares = load("conf_shares", conf[10])
    income = load("dao_income", client.read(pool, "daoIncome"))
    staking = load("staking", client.read(income, "staking"))
    decisions = load("decisions", client.read(income, "decisions"))
    sale = load("nft_pass", client.read(income, "passSale"))
    reserve = load("mining", client.read(pool, "bitcoinReserve"))
    gateway = load("custody_transfer", client.read(reserve, "custodyTransfer"))
    load("dao_token", client.read(staking, "stakeToken"))
    if (client.read(pool, "confederationId") != conf_id
            or client.read(income, "pool") != pool.address or client.read(income, "authorization") != conf[7]
            or client.read(income, "shareToken") != shares.address or client.read(income, "validator") != validator.address
            or client.read(validator, "candidateStaking") != staking.address
            or client.read(decisions, "staking") != staking.address or client.read(decisions, "authorization") != conf[7]
            or client.read(sale, "income") != income.address
            or client.read(reserve, "confederationPool") != pool.address
            or client.read(reserve, "shareToken") != shares.address
            or client.read(gateway, "mining") != reserve.address or client.read(gateway, "pool") != pool.address):
        raise ValueError("CONFEDERATION_DAO_BINDING")
    client.stable()
    return client


def _add(client, name, node, kind):
    client.contracts[name] = node
    client.config["contracts"][name] = dict(address=node.address, artifact=kind,
        runtime_hash=Web3.to_hex(Web3.keccak(client.w3.eth.get_code(node.address, client.block.number))))


def _transact(client, a, name, method, values, *, suffix=""):
    caller = account_at(a.identity_file).address if a.identity_file else address(a.sender)
    if a.sender and caller != address(a.sender):
        raise ValueError("SENDER_IDENTITY_MISMATCH")
    if a.send and not a.id:
        raise ValueError("INVALID_OPERATION_ID")
    return client.transact(name, method, values, caller, a.identity_file, a.send, a.state,
                           (a.id or "dao-preview") + suffix)


def _withdraw(client, a):
    from .bitcoin_onchain_plan import script

    if a.pool_token:
        raise ValueError("S_WITHDRAWAL_USES_CONFEDERATION_RESERVE")
    method = "requestWithdrawal" if a.nft is None else "requestWithdrawalWithNFT"
    values = [number(a.amount), bytes.fromhex(script(a.script))]
    if a.nft is not None:
        values.append(number(a.nft))
    values.append(number(a.miner_fee_cap_sats))
    client.stable()
    return _transact(client, a, "conf_pool", method, values)


def _operation(client, a):
    from .bootstrap_confederation_dao import review
    from .onchain_transport import board_at, ceremony_bundle, can_publish_intent

    w3, at = client.w3, client.block.number
    intent = read_public(a.intent)
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
    if a.action == "tariff-prepare":
        name, helper = "nft_pass", "getApproveTariffOperationHash"
        args = [[a.duration, a.discount_bps, a.volume_limit]]
    elif a.action == "stablecoin-prepare":
        name, helper = "dao_income", "getAddStablecoinOperationHash"
        args = [address(a.token)]
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
    if name == "nft_pass":
        result["tariff_id"] = Web3.to_hex(Web3.keccak(client.w3.codec.encode(["(uint64,uint16,uint256)"], values)))
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
    quote = ["0x" + bytes.fromhex(a.tariff.removeprefix("0x")).hex(), address(a.buyer), a.amount, a.nonce, a.deadline]
    abi = next(row["inputs"] for row in sale.abi if row.get("name") == "priceDigest")
    typed, = encode_args(abi, [quote])
    if client.read(sale, "tariffs", typed[0])[0] == 0 or typed[2] <= 0 or typed[4] <= client.block.timestamp:
        raise ValueError("CONFEDERATION_DAO_QUOTE")
    digest = client.read(sale, "priceDigest", typed)
    domain = dict(name="SatoshiFi Confederation Pass", version="1",
                  chainId=int(client.config["chain_id"]), verifyingContract=sale.address)
    fields = (("tariffId", "bytes32"), ("buyer", "address"), ("token", "address"),
              ("price", "uint256"), ("nonce", "uint256"), ("expiresAt", "uint64"))
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
    return dict(status="PREPARED", digest=Web3.to_hex(digest))


def main(argv):
    p = DiagnosticArgumentParser(prog="frost-signer dao in-kind", description=__doc__)
    p.add_argument("action", choices=("discover", "status", "approve", "stake", "unstake", "sweep", "process",
        "claim", "reimburse", "approve-pass", "buy-pass", "tariff-prepare", "stablecoin-prepare", "price",
        "approve-wrap", "wrap", "withdraw", "cancel", "delegate", "propose", "execute", "vote", "finalize",
        "decision-prepare", "custodian-add-prepare", "custodian-remove-prepare", "wrap-pass", "clear-wrap-pass"))
    p.add_argument("--rpc", required=True)
    graph = p.add_mutually_exclusive_group(required=True)
    graph.add_argument("--manifest")
    graph.add_argument("--factory")
    p.add_argument("--confederation-id")
    p.add_argument("--chain-id")
    p.add_argument("--confirmations", type=int, default=12)
    p.add_argument("--token")
    p.add_argument("--amount")
    p.add_argument("--days", nargs="+")
    p.add_argument("--rows", default="8")
    p.add_argument("--pool-token", help="MP token for wrap")
    p.add_argument("--source", type=int, help="Enrolled federation source index for MP-to-S handover")
    p.add_argument("--minimum-s", help="Minimum S base units accepted after the BTC handover")
    p.add_argument("--recipient", help="S recipient after the BTC handover")
    p.add_argument("--script")
    p.add_argument("--miner-fee-cap-sats", default=str(2**64 - 1))
    p.add_argument("--nft")
    p.add_argument("--withdrawal")
    p.add_argument("--quote")
    p.add_argument("--output")
    p.add_argument("--duration")
    p.add_argument("--discount-bps")
    p.add_argument("--volume-limit")
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
    p.add_argument("--deadline")
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
    elif a.action == "status":
        result = dict(status="OK", next_day=str(client.read("dao_income", "nextSettlementDay")),
            processing=client.read("dao_income", "processing"),
            staged_expenses=str(client.read("dao_income", "stagedExpenseCount")),
            staged_expense_position=str(client.read("dao_income", "stagedExpensePosition")),
            staged_expense_commitment=Web3.to_hex(client.read("dao_income", "stagedExpenseCommitment")),
            reward_tokens=client.read("dao_income", "rewardTokens"))
    elif a.action.endswith("-prepare"):
        result = _prepare_intent(client, a)
    elif a.action == "price":
        result = _quote(client, a)
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
    elif a.action == "withdraw":
        result = _withdraw(client, a)
    else:
        result = _transact(client, a, "conf_pool", "cancelWithdrawal", [number(a.withdrawal)])
    print(json.dumps(result))
    return 0
