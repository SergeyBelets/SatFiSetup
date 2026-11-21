"""Prepare reviewed ULR operations; simulate by default and use the durable EOA executor."""

import json
from pathlib import Path

from web3 import Web3
from web3.exceptions import TransactionNotFound

from .safe_errors import DiagnosticArgumentParser
from . import ulr_treasury as policy
from .dao_client import DAOClient, account_at, address, wire, claim_days
from .evm_network import read_block
from .operations import Journal, canonical


def _write(path, value):
    with Path(path).open("xb") as output:
        output.write(value if isinstance(value, bytes) else (canonical(value) + "\n").encode())


def _executor(w3, pin, at, target, authority=None, kind=None):
    client = object.__new__(DAOClient)
    client.w3, client.block = w3, at
    kind = kind or ("ULRCustodianSnapshots" if target.address == pin["snapshots"] else "ULRConfederationTreasury")
    client.contracts = dict(target=target)
    specs = dict(target=dict(address=target.address, artifact=kind,
        runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(target.address, at.number)))))
    if authority:
        client.contracts["authorization"] = policy.contract(w3, "DKGAuthorization", authority)
        specs["authorization"] = dict(address=authority, artifact="DKGAuthorization",
            runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(authority, at.number))))
    client.config = dict(chain_id=pin["chain_id"], factory=pin["snapshots"],
        confederation_id="ULR", confirmations=pin["confirmations"], contracts=specs)
    return client


def pay_attempt(w3, at, state, key, waiting_for_balance):
    """Resume a signed payment or advance after a canonical, completed no-op."""
    prefix = "dao-request-" + key
    with Journal(state).db() as db:
        previous = db.execute(
            "SELECT id,status FROM operations WHERE id=? OR substr(id,1,?)=? "
            "ORDER BY rowid DESC LIMIT 1", (prefix, len(prefix) + 1, prefix + "-"),
        ).fetchone()
        if previous is None:
            return None if waiting_for_balance else key
        current = previous["id"].removeprefix("dao-request-")
        if previous["status"] != "VERIFIED":
            return current
        signed = db.execute(
            "SELECT result FROM operations WHERE id=? AND status='CONFIRMED'",
            ("dao-tx-" + current + ":send",),
        ).fetchone()
    if signed is None:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    result = json.loads(signed["result"])
    number = int(result["block_number"])
    if number > at.number:
        return current  # The current snapshot may predate the prior payout.
    try:
        receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
    except TransactionNotFound:
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED") from None
    from .bootstrap_recovery import check_receipt_binding
    check_receipt_binding(receipt, result)
    if (receipt.status != 1 or w3.eth.get_block(number).hash != receipt.blockHash
            or w3.eth.get_block(at.number).hash != at.hash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    if waiting_for_balance:
        return None
    suffix = current.removeprefix(key)
    return key + "-" + str(int(suffix[1:] or "0") + 1)


def main(argv):
    parser = DiagnosticArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["snapshot-prepare", "enroll-prepare", "spend-prepare", "router-prepare",
        "candidate-bind-prepare", "candidate-admit-prepare", "candidate-remove-prepare", "voting-prepare",
        "candidates", "delegate", "undelegate", "vote", "approve", "stake", "unstake", "wallet", "claim", "propose", "execute", "pay", "status"])
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--treasury")
    parser.add_argument("--router")
    parser.add_argument("--router-action", choices=["shares", "treasury"])
    parser.add_argument("--candidate")
    parser.add_argument("--custodian")
    parser.add_argument("--owner")
    parser.add_argument("--offset", default="0")
    parser.add_argument("--limit", default="100")
    parser.add_argument("--decision-board")
    parser.add_argument("--decision")
    parser.add_argument("--choice", choices=["against", "for", "abstain"])
    parser.add_argument("--days", nargs="+")
    parser.add_argument("--metadata-uri")
    parser.add_argument("--content-hash")
    parser.add_argument("--duration")
    parser.add_argument("--quorum-bps")
    parser.add_argument("--inputs")
    parser.add_argument("--bundle")
    parser.add_argument("--intent")
    parser.add_argument("--evidence")
    parser.add_argument("--recipient")
    parser.add_argument("--amount", help="asset base units as a decimal integer; ULR has 18 decimals")
    parser.add_argument("--deadline")
    parser.add_argument("--session")
    parser.add_argument("--sender")
    parser.add_argument("--identity-file")
    parser.add_argument("--state")
    parser.add_argument("--id", help="Distinct durable operation ID; reuse only for an exact retry")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    if args.send and args.action in ("delegate", "undelegate", "vote", "claim", "approve", "stake", "unstake") and not args.id:
        raise ValueError("ULR_USER_OPERATION_ID_REQUIRED")
    pin = policy.load_pin(args.config)
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if str(w3.eth.chain_id) != pin["chain_id"]:
        raise ValueError("ULR_CHAIN_MISMATCH")
    at = read_block(w3, int(pin["confirmations"]))
    if args.action in ("claim", "approve", "stake", "unstake", "wallet"):
        staking = policy.contract(w3, "ULRStaking", pin["staking"])
        policy.proven(w3, "ULRStaking", staking.address, at.number,
            policy.staking_args(staking, at.number))
        sender = account_at(args.identity_file).address if args.identity_file else address(
            args.sender or (args.owner if args.action == "wallet" else None))
        if args.action == "wallet":
            owner = address(args.owner) if args.owner else sender
            token = policy.contract(w3, "IERC20Metadata", policy.read(staking, at.number, "stakeToken"))
            print(json.dumps(dict(owner=owner, token=token.address, staking=staking.address,
                balance=str(policy.read(token, at.number, "balanceOf", owner)),
                staked=str(policy.read(staking, at.number, "stakedBalance", owner)),
                allowance=str(policy.read(token, at.number, "allowance", owner, staking.address)))))
            return 0
        if args.action != "claim":
            if args.amount is None:
                raise ValueError("ULR_AMOUNT_REQUIRED")
            amount = policy.number(args.amount)
            if amount == 0 and args.action != "approve":
                raise ValueError("ULR_POSITIVE_AMOUNT_REQUIRED")
            target, kind, values = staking, "ULRStaking", [amount]
            if args.action == "approve":
                target = policy.proven(w3, "CustodianDAOToken", policy.read(staking, at.number, "stakeToken"), at.number)
                kind, values = "CustodianDAOToken", [staking.address, amount]
            result = _executor(w3, pin, at, target, kind=kind).transact("target", args.action, values,
                sender, args.identity_file, args.send, args.state, args.id or "ulr-owner-simulation")
            print(json.dumps(result))
            return 0
        result = _executor(w3, pin, at, staking, kind="ULRStaking").transact(
            "target", "claim", [claim_days(args.days or [])], sender, args.identity_file,
            args.send, args.state, args.id or "ulr-claim-simulation")
        print(json.dumps(result))
        return 0
    registry, _ = policy.configuration(w3, pin, at.number)
    bundle = json.loads(Path(args.bundle).read_text()) if args.bundle else None
    read = lambda target, method, *values: policy.read(target, at.number, method, *values)
    if args.action.endswith("-prepare"):
        if not bundle or not args.intent:
            raise ValueError("ULR_BUNDLE_AND_INTENT_REQUIRED")
        if args.action == "snapshot-prepare":
            if not args.inputs or not args.evidence:
                raise ValueError("ULR_SNAPSHOT_INPUTS_REQUIRED")
            inputs = json.loads(Path(args.inputs).read_text())
            values, evidence = policy.snapshot_arguments(w3, pin, inputs, at.number)
            policy.snapshot_calldata_limit(w3, registry, values, b"\xff" * 32, at.number)
            intent = policy.intent_for(w3, registry, "ULRCustodianSnapshots", "getPublishDayOperationHash",
                values, read(registry, "authorization"), bundle["role"], at.number, ulr_snapshot_inputs=inputs)
        else:
            _, _, treasury, factory, conf_id, conf = policy.treasury_context(w3, pin, args.treasury, at.number)
            if args.action == "enroll-prepare":
                intent = policy.intent_for(w3, registry, "ULRCustodianSnapshots", "getEnrollTreasuryOperationHash",
                    [factory.address, conf_id, treasury.address], conf[7], bundle["role"], at.number)
            elif args.action == "spend-prepare":
                intent = policy.intent_for(w3, treasury, "ULRConfederationTreasury", "getSpendOperationHash",
                    [address(args.recipient), policy.number(args.amount), policy.number(args.deadline)],
                    conf[7], bundle["role"], at.number)
            elif args.action.startswith("candidate-"):
                validator = policy.contract(w3, "CustodianValidator", conf[8])
                helper = {"candidate-bind-prepare": "getBindCandidateStakingOperationHash",
                    "candidate-admit-prepare": "getAddCustodianOperationHash",
                    "candidate-remove-prepare": "getRemoveCustodianOperationHash"}[args.action]
                values = [pin["staking"]] if args.action == "candidate-bind-prepare" else [address(args.candidate)]
                intent = policy.intent_for(w3, validator, "CustodianValidator", helper, values, conf[7],
                    bundle["role"], at.number, ulr_treasury=treasury.address)
            elif args.action == "voting-prepare":
                policy.voting_context(w3, pin, args.decision_board, treasury.address, at.number)
                board = policy.contract(w3, "CustodianDAODecisionBoard", args.decision_board)
                intent = policy.intent_for(w3, board, "CustodianDAODecisionBoard", "getCreateOperationHash",
                    [args.metadata_uri, policy.digest(args.content_hash), policy.number(args.duration),
                     policy.number(args.quorum_bps)], conf[7], bundle["role"], at.number, ulr_treasury=treasury.address)
            else:
                if not args.router_action:
                    raise ValueError("ULR_ROUTER_ACTION_REQUIRED")
                router = policy.contract(w3, "CustodianDAOFeeRouter", args.router)
                helper = "getSetFeeSharesOperationHash" if args.router_action == "shares" else "getSetTreasuryOperationHash"
                values = [0, 0] if args.router_action == "shares" else [treasury.address]
                intent = policy.intent_for(w3, router, "CustodianDAOFeeRouter", helper, values,
                    conf[7], bundle["role"], at.number, ulr_treasury=treasury.address)
        policy.review(w3, intent, bundle, at.number, pin)
        if args.action == "snapshot-prepare":
            _write(args.evidence, evidence)
        _write(args.intent, intent)
        print(json.dumps(dict(status="PREPARED", operation_hash=intent["operation_hash"])))
        return 0

    sender = account_at(args.identity_file).address if args.identity_file else address(args.sender) if args.sender else None
    if args.action in ("execute", "propose"):
        if not args.intent or not bundle:
            raise ValueError("ULR_BUNDLE_AND_INTENT_REQUIRED")
        intent = json.loads(Path(args.intent).read_text())
        reviewed = policy.review(w3, intent, bundle, at.number, pin)
        if reviewed["complete"]:
            print(json.dumps(dict(status="ALREADY_EXECUTED", operation_hash=intent["operation_hash"])))
            return 0
        target, authority, values = reviewed["target"], reviewed["authority"], reviewed["arguments"]
        executor = _executor(w3, pin, at, target, authority, intent["artifact"])
        op = policy.digest(intent["operation_hash"])
        auth = executor.contracts["authorization"]
        identifier = "ulr-" + op.hex()
        if args.action == "propose":
            from .dkg_protocol import Manifest
            from .onchain_transport import board_at
            from web3.logs import DISCARD
            manifest = Manifest.from_dict(bundle)
            board = board_at(w3, bundle["onchain_board"], at)
            if sender != read(board, "operator") and not read(board, "canPublishIntent", sender):
                raise ValueError("ULR_BOARD_OPERATOR_REQUIRED")
            deadline = policy.number(args.deadline)
            if deadline <= at.timestamp:
                raise ValueError("ULR_SESSION_DEADLINE")
            policy.board_intent(dict(intent, session=str(2**256 - 1)))
            coordinator = policy.contract(w3, "FROSTCoordinator", read(auth, "coordinator"))
            for name, item, kind in (("coordinator", coordinator, "FROSTCoordinator"), ("board", board, "DKGOnchainBoard")):
                executor.contracts[name] = item
                executor.config["contracts"][name] = dict(address=item.address, artifact=kind,
                    runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(item.address, at.number))))
            result = executor.transact("coordinator", "createSessionFromDKG",
                [manifest.session_id, op, target.address, deadline], sender, args.identity_file, args.send,
                args.state, identifier + "-open")
            if args.send:
                receipt = w3.eth.get_transaction_receipt(result["tx_hash"])
                events = coordinator.events.SessionCreated().process_receipt(receipt, errors=DISCARD)
                if len(events) != 1:
                    raise ValueError("ULR_SESSION_RECEIPT")
                sid = int(events[0]["args"]["sessionId"])
                envelope = policy.board_intent(dict(intent, session=str(sid)))
                public = canonical(envelope).encode()
                executor.transact("board", "publishIntent", [sid, public], sender, args.identity_file,
                    True, args.state, identifier + "-publish")
                result = dict(status="AWAITING_PARTICIPANTS", session=str(sid), operation_hash=intent["operation_hash"])
                if "ulr_snapshot_intent_sha256" in envelope:
                    result["snapshot_intent_sha256"] = envelope["ulr_snapshot_intent_sha256"]
            print(json.dumps(result))
            return 0
        method = {"getPublishDayOperationHash": "publishDay", "getEnrollTreasuryOperationHash": "enrollTreasury",
                  "getSpendOperationHash": "spend", "getSetFeeSharesOperationHash": "setFeeShares",
                  "getSetTreasuryOperationHash": "setTreasury", "getBindCandidateStakingOperationHash": "bindCandidateStaking",
                  "getAddCustodianOperationHash": "addCustodian", "getRemoveCustodianOperationHash": "removeCustodian",
                  "getCreateOperationHash": "createDecision"}[intent["hash_method"]]
        if not read(auth, "isOperationApproved", op, target.address):
            if args.session is None:
                raise ValueError("ULR_FINALIZED_SESSION_REQUIRED")
            if not args.send:
                print(json.dumps(dict(status="AUTHORIZATION_REQUIRED", operation_hash=intent["operation_hash"])))
                return 0
            executor.transact("authorization", "authorizeOperation", [policy.number(args.session), op, target.address],
                sender, args.identity_file, True, args.state, identifier + "-auth")
        result = executor.transact("target", method, [*values, op], sender, args.identity_file,
            args.send, args.state, identifier + "-exec")
        print(json.dumps(result))
        return 0

    _, staking, treasury, _, _, conf = policy.treasury_context(w3, pin, args.treasury, at.number)
    if args.action == "candidates":
        validator = policy.contract(w3, "CustodianValidator", conf[8])
        result = read(validator, "getCustodianCandidates", policy.number(args.offset), policy.number(args.limit))
        print(json.dumps(dict(candidates=result[0], next_offset=str(result[1]),
            nominee_count=str(read(staking, "candidateNomineeCount")))))
        return 0
    if args.action in ("delegate", "undelegate", "vote"):
        if sender is None:
            raise ValueError("ULR_SENDER_REQUIRED")
        if args.action in ("delegate", "undelegate"):
            values = [conf[8], address(args.custodian) if args.action == "delegate" else "0x" + "00" * 20]
            target, kind, method = staking, "ULRStaking", "setVotingDelegate"
        else:
            policy.voting_context(w3, pin, args.decision_board, treasury.address, at.number)
            target, kind, method = policy.contract(w3, "CustodianDAODecisionBoard", args.decision_board), "CustodianDAODecisionBoard", "vote"
            if args.choice is None:
                raise ValueError("ULR_VOTE_CHOICE_REQUIRED")
            values = [policy.number(args.decision), address(args.owner), ["against", "for", "abstain"].index(args.choice)]
        identifier = args.id or "ulr-user-simulation"
        result = _executor(w3, pin, at, target, kind=kind).transact("target", method, values, sender,
            args.identity_file, args.send, args.state, identifier)
        print(json.dumps(result))
        return 0
    today = at.timestamp // 86400
    if today == 0:
        raise ValueError("ULR_DAY_UNAVAILABLE")
    if args.action == "pay" and w3.eth.get_block("latest").timestamp // 86400 != today:
        raise ValueError("ULR_UTC_BOUNDARY_WAIT_FOR_FINALITY")
    day = today - 1
    receipt = read(treasury, "settlements", day)
    if args.action == "status":
        result = dict(day=str(day), treasury=treasury.address, balance=str(read(
            policy.contract(w3, "IERC20Metadata", read(staking, "rewardToken")), "balanceOf", treasury.address)),
            settlement=wire(receipt), snapshot=wire(read(registry, "daySnapshots", day)))
    elif receipt[0]:
        result = dict(status="ALREADY_PROCESSED", day=str(day), settlement=wire(receipt))
    else:
        if sender is None:
            raise ValueError("ULR_SENDER_REQUIRED")
        preview = read(treasury, "previewDay", day)
        waiting_for_balance = int(preview[1]) in (1, 4) and int(preview[5]) == 0
        attempt = "ulr-pay-" + treasury.address[2:] + "-" + str(day)
        if args.send:
            if not args.state or not args.identity_file:
                raise ValueError("SEND_REQUIRES_IDENTITY_STATE_AND_ID")
            attempt = pay_attempt(w3, at, args.state, attempt, waiting_for_balance)
        elif waiting_for_balance:
            attempt = None
        if attempt is None:
            print(json.dumps(dict(status="AWAITING_TREASURY_BALANCE", day=str(day), preview=wire(preview))))
            return 0
        executor = _executor(w3, pin, at, treasury)
        result = executor.transact("target", "payDay", [day], sender, args.identity_file, args.send,
            args.state, attempt)
        result["preview"] = wire(preview)
    print(json.dumps(result))
    return 0
