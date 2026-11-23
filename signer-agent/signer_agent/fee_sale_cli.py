"""Review, fund and execute sales of actual S or MP fees on a pinned graph."""

import hashlib
import json
import re

from web3 import Web3
from .evm_rpc import evm_web3
from web3.logs import DISCARD

from . import fee_sale_governance as policy
from .confederation_dao_cli import _add
from .dao_client import account_at, address
from .evm_network import read_block
from .onboarding import read_public
from .onchain_transport import can_publish_intent, ceremony_bundle
from .operations import canonical
from .safe_errors import DiagnosticArgumentParser
from .ulr_treasury import contract, intent_for, number, read
from .ulr_treasury_cli import _executor, _write


def _required(args, *names):
    if any(getattr(args, name) is None for name in names):
        raise ValueError("FEE_SALE_REQUIRED_ARGUMENTS_" + "_".join(names).upper())


def _initial_pin(args):
    _required(args, "board", "role", "factory", "fee_token", "stablecoin", "treasury", "chain_id", "output")
    if args.action == "init":
        _required(args, "sale")
    return policy.validate_pin(dict(
        version="1", chain_id=args.chain_id, confirmations=args.confirmations, role=args.role,
        board=address(args.board), factory=address(args.factory), sale=address(args.sale or args.factory),
        fee_token=address(args.fee_token), stablecoin=address(args.stablecoin), treasury=address(args.treasury),
    ))


def _deployment(args, pin, bundle, ctx):
    _required(args, "source", "bps", "deployment")
    bps = number(args.bps)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.deployment) or bps > 10_000:
        raise ValueError("FEE_SALE_DEPLOYMENT")
    if address(args.source) != ctx["source"].address or ctx["bound"] != policy.ZERO:
        raise ValueError("FEE_SALE_SOURCE")
    invitation = bundle["onboarding"]["invitation"]
    plan = dict(
        deployment_id=args.deployment, mode=invitation["mode"], bitcoin_network=invitation["bitcoin_network"],
        chain_id=pin["chain_id"], evm_confirmations=pin["confirmations"], evm_rpc="http://127.0.0.1:0",
        steps=[dict(id="fee_sale", kind="deploy", artifact=policy.SALE, args=[
            ctx["source"].address, pin["fee_token"], pin["stablecoin"], pin["treasury"], str(bps),
        ])],
    )
    if "bitcoin_policy" in invitation:
        plan["bitcoin_policy"] = invitation["bitcoin_policy"]
    _write(args.output, plan)
    return dict(status="FEE_SALE_DEPLOYMENT_PREPARED", profile=args.output,
                required_deployer=invitation["operator"], rpc_override_required=True)


def _status(args, pin, ctx):
    at, sale, source = ctx["block"], ctx["sale"], ctx["source"]
    stablecoin = contract(sale.w3, "IERC20Metadata", pin["stablecoin"])
    balance = read(stablecoin, at.number, "balanceOf", sale.address)
    escrowed = read(sale, at.number, "stableEscrow")
    result = dict(status="FEE_SALE_STATUS", block=str(at.number), sale=sale.address, source=source.address,
                  source_active=ctx["source_active"],
                  fee_token=pin["fee_token"], stablecoin=pin["stablecoin"], treasury=pin["treasury"],
                  authority=ctx["authority"].address, validator=ctx["validator"],
                  sell_bps=str(read(sale, at.number, "sellBps")), inventory=str(read(sale, at.number, "inventory")),
                  stable_escrow=str(escrowed), stable_balance=str(balance),
                  stable_shortfall=str(max(0, escrowed - balance)),
                  operation_nonce=str(read(sale, at.number, "operationNonce")),
                  bid_count=str(read(sale, at.number, "bidCount")))
    if ctx["validator"] != policy.ZERO:
        result["bound"] = ctx["bound"] == sale.address
    if args.bid is not None:
        bid_id = number(args.bid)
        buyer, fee_amount, stable_amount, deadline = read(sale, at.number, "bids", bid_id)
        if buyer == policy.ZERO:
            bid_status = "CLOSED" if 1 <= bid_id <= int(result["bid_count"]) else "UNKNOWN"
        else:
            bid_status = "REFUNDABLE" if deadline < at.timestamp else "OPEN"
        result["bid"] = dict(id=str(bid_id), status=bid_status, buyer=buyer, fee_amount=str(fee_amount),
                             stable_amount=str(stable_amount), deadline=str(deadline))
    if bytes(sale.w3.eth.get_block(at.number).hash) != bytes(at.hash):
        raise ValueError("FEE_SALE_SNAPSHOT_REORG")
    return result


def _prepare(w3, args, pin, bundle, ctx):
    _required(args, "operation", "intent")
    action = args.operation
    at, sale, source = ctx["block"].number, ctx["sale"], ctx["source"]
    confederation = ctx["validator"] != policy.ZERO
    if (action == "bind" and not confederation) or (confederation and action in ("claim", "collect")):
        raise ValueError("FEE_SALE_SOURCE")
    kind, helper, _ = policy.OPERATIONS[action]
    target = sale if kind == policy.SALE else source
    claim_nonce = None
    if action == "bind":
        values = [sale.address]
    elif action == "share":
        _required(args, "bps")
        values = [number(args.bps)]
    elif action in ("claim", "collect"):
        _required(args, "stream", "gross")
        is_wrap, gross = args.stream == "wrap", number(args.gross)
        if action == "claim":
            if not 0 < gross <= read(source, at, "accruedFees", pin["fee_token"], is_wrap):
                raise ValueError("FEE_SALE_CLAIM")
            claim_nonce = read(source, at, "operationNonce")
            values = [pin["fee_token"], is_wrap, sale.address, gross]
        else:
            _required(args, "claim_nonce")
            claim_nonce = number(args.claim_nonce)
            values = [is_wrap, gross, claim_nonce]
    else:
        _required(args, "bid")
        values = [number(args.bid)]

    intent = intent_for(w3, target, kind, helper, values, ctx["authority"].address, pin["role"], at, fee_sale=action)
    policy.review(w3, intent, bundle, at, pin)
    _write(args.intent, intent)
    result = dict(status="FEE_SALE_OPERATION_PREPARED", operation_hash=intent["operation_hash"], intent=intent)
    if claim_nonce is not None:
        result["claim_nonce"] = str(claim_nonce)
    return result


def _sender(args):
    explicit = address(args.sender) if args.sender is not None else None
    sender = account_at(args.identity_file).address if args.identity_file else explicit
    if sender is None:
        raise ValueError("FEE_SALE_SENDER_REQUIRED")
    if explicit is not None and explicit != sender:
        raise ValueError("SENDER_IDENTITY_MISMATCH")
    if args.send and (not args.identity_file or not args.state):
        raise ValueError("FEE_SALE_SEND_REQUIRES_IDENTITY_AND_STATE")
    if args.id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.id):
        raise ValueError("INVALID_OPERATION_ID")
    return sender


def _transact(executor, args, sender, identifier, name, method, values):
    executor.stable()
    return executor.transact(name, method, values, sender, args.identity_file, args.send, args.state, identifier)


def _governance(w3, args, pin, bundle, at):
    _required(args, "intent")
    intent = read_public(args.intent)
    ctx = policy.review(w3, intent, bundle, at.number, pin)
    op = ctx["operation_hash"]
    summary = dict(operation_hash=Web3.to_hex(op))
    if ctx["complete"]:
        return dict(status="ALREADY_EXECUTED", **summary)
    sender = _sender(args)
    target, auth, board = ctx["target"], ctx["authority"], ctx["board"]
    executor = _executor(w3, dict(pin, snapshots=ctx["sale"].address), at, target, auth.address, intent["artifact"])
    identifier = "fs-" + (args.id or op.hex())

    if args.action == "propose":
        _required(args, "deadline")
        deadline = number(args.deadline)
        if deadline <= at.timestamp:
            raise ValueError("FEE_SALE_SESSION_DEADLINE")
        if not can_publish_intent(board, sender, at.number):
            raise ValueError("FEE_SALE_BOARD_PUBLISHER_REQUIRED")
        coordinator = executor.contract("FROSTCoordinator", read(auth, at.number, "coordinator"))
        _add(executor, "coordinator", coordinator, "FROSTCoordinator")
        _add(executor, "board", board, "DKGOnchainBoard")
        if len(canonical(dict(intent, session=str(2**256 - 1))).encode()) > 32768:
            raise ValueError("FEE_SALE_INTENT_TOO_LARGE")
        result = _transact(executor, args, sender, identifier + "-open", "coordinator", "createSessionFromDKG",
                           [read(auth, at.number, "dkgId"), op, target.address, deadline])
        if result["status"] != "VERIFIED":
            return result
        receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
        events = [row for row in coordinator.events.SessionCreated().process_receipt(receipt, errors=DISCARD)
                  if row["address"] == coordinator.address]
        if len(events) != 1:
            raise ValueError("FEE_SALE_SESSION_RECEIPT")
        session = int(events[0]["args"]["sessionId"])
        publication = _transact(executor, args, sender, identifier + "-publish", "board", "publishIntent",
                                [session, canonical(dict(intent, session=str(session))).encode()])
        if publication["status"] != "VERIFIED":
            return publication
        return dict(status="AWAITING_PARTICIPANTS", session=str(session), **summary)

    approved = read(auth, at.number, "isOperationApproved", op, target.address)
    if not approved:
        if not args.send:
            return dict(status="AUTHORIZATION_REQUIRED", **summary)
        _required(args, "session")
        session = number(args.session)
        public = canonical(dict(intent, session=str(session))).encode()
        if bytes(read(board, at.number, "intents", session)) != public:
            raise ValueError("FEE_SALE_PUBLIC_INTENT")
        result = _transact(executor, args, sender, identifier + "-auth", "authorization", "authorizeOperation",
                           [session, op, target.address])
        if result["status"] != "VERIFIED":
            return result
    if intent["fee_sale"] == "claim":
        return dict(status="CLAIM_AUTHORIZED", **summary)
    return _transact(executor, args, sender, identifier + "-execute", "target", ctx["method"],
                     [*ctx["arguments"], op])


def _bid(w3, args, pin, ctx):
    sender = _sender(args)
    if args.send and not args.id:
        raise ValueError("INVALID_OPERATION_ID")
    at, sale = ctx["block"], ctx["sale"]
    executor = _executor(w3, dict(pin, snapshots=sale.address), at, sale, kind=policy.SALE)
    identifier = "fs-" + (args.id or "preview") + "-" + args.action
    if args.action == "approve":
        _required(args, "stable_amount")
        stable = executor.contract("IERC20Metadata", pin["stablecoin"])
        _add(executor, "stablecoin", stable, "IERC20Metadata")
        amount = number(args.stable_amount)
        result = _transact(executor, args, sender, identifier, "stablecoin", "approve", [sale.address, amount])
        if result["status"] == "VERIFIED":
            receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
            events = stable.events.Approval().process_receipt(receipt, errors=DISCARD)
            if not any(row["address"] == stable.address and row["args"]["owner"] == sender
                       and row["args"]["spender"] == sale.address and row["args"]["value"] == amount for row in events):
                raise ValueError("FEE_SALE_APPROVAL_RECEIPT")
        return result
    if args.action == "cancel":
        _required(args, "bid")
        bid_id = number(args.bid)
        result = _transact(executor, args, sender, identifier, "target", "cancelBid", [bid_id])
        if result["status"] == "VERIFIED":
            receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
            events = [row["args"] for row in sale.events.BidCancelled().process_receipt(receipt, errors=DISCARD)
                      if row["address"] == sale.address]
            if (len(events) != 1 or events[0]["id"] != bid_id or events[0]["buyer"] != sender
                    or events[0]["stableAmount"] <= 0):
                raise ValueError("FEE_SALE_BID_RECEIPT")
            result.update(bid=str(bid_id), refunded_stable_amount=str(events[0]["stableAmount"]))
        return result
    _required(args, "fee_amount", "stable_amount", "deadline")
    values = [number(args.fee_amount), number(args.stable_amount), number(args.deadline)]
    result = _transact(executor, args, sender, identifier, "target", "fundBid", values)
    if result["status"] == "VERIFIED":
        receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
        events = [row["args"] for row in sale.events.BidFunded().process_receipt(receipt, errors=DISCARD)
                  if row["address"] == sale.address]
        if (len(events) != 1 or events[0]["buyer"] != sender
                or [events[0][key] for key in ("feeAmount", "stableAmount", "deadline")] != values):
            raise ValueError("FEE_SALE_BID_RECEIPT")
        result["bid"] = str(events[0]["id"])
    return result


def main(argv=None):
    p = DiagnosticArgumentParser(prog="frost-signer fee-sale", description=__doc__)
    p.add_argument("action", choices=("init", "deploy-plan", "status", "prepare", "propose", "execute",
                                      "approve", "fund", "cancel"))
    p.add_argument("--rpc", required=True)
    p.add_argument("--config")
    p.add_argument("--board")
    p.add_argument("--role", choices=("evm_ceremony", "conf_ceremony"))
    p.add_argument("--factory")
    p.add_argument("--sale")
    p.add_argument("--source")
    p.add_argument("--fee-token")
    p.add_argument("--stablecoin")
    p.add_argument("--treasury")
    p.add_argument("--chain-id", choices=("1", "11155111", "31337"))
    p.add_argument("--confirmations", default="12")
    p.add_argument("--deployment")
    p.add_argument("--output")
    p.add_argument("--operation", choices=tuple(policy.OPERATIONS))
    p.add_argument("--intent")
    p.add_argument("--bps")
    p.add_argument("--stream", choices=("wrap", "unwrap"))
    p.add_argument("--gross", help="Accrued MP in token base units")
    p.add_argument("--claim-nonce")
    p.add_argument("--bid", help="Bid ID from the funding receipt")
    p.add_argument("--fee-amount", help="S or MP base units requested by the bid")
    p.add_argument("--stable-amount", help="Stablecoin base units to approve or deposit")
    p.add_argument("--deadline", help="Inclusive bid or signing session deadline in Unix seconds")
    p.add_argument("--session")
    p.add_argument("--sender")
    p.add_argument("--identity-file")
    p.add_argument("--state")
    p.add_argument("--id")
    p.add_argument("--send", action="store_true")
    args = p.parse_args(argv)

    if args.action in ("init", "deploy-plan"):
        pin = _initial_pin(args)
    else:
        _required(args, "config")
        pin = policy.validate_pin(read_public(args.config))
    w3 = evm_web3(args.rpc, timeout=20)
    if str(w3.eth.chain_id) != pin["chain_id"]:
        raise ValueError("CHAIN_MISMATCH")
    at = read_block(w3, number(pin["confirmations"]))
    refund_context = args.action in ("status", "cancel")
    options = {"legacy_dao_exit": True} if refund_context else {}
    bundle = ceremony_bundle(w3, pin["board"], pin["role"], **options)
    if args.action in ("propose", "execute"):
        result = _governance(w3, args, pin, bundle, at)
    else:
        ctx = policy.context(w3, bundle, pin, at.number, require_sale=args.action != "deploy-plan",
                             refund_context=refund_context)
        if args.action == "deploy-plan":
            result = _deployment(args, pin, bundle, ctx)
        elif args.action == "init":
            _write(args.output, pin)
            result = dict(status="FEE_SALE_PIN_PREPARED", pin=pin,
                          fingerprint=hashlib.sha256(canonical(pin).encode()).hexdigest())
        elif args.action == "status":
            result = _status(args, pin, ctx)
        elif args.action == "prepare":
            result = _prepare(w3, args, pin, bundle, ctx)
        else:
            result = _bid(w3, args, pin, ctx)
    print(json.dumps(result))
    return 0
