import argparse
import json
import logging
import sys
import time
from typing import Iterable, Optional

from .config import Config
from .service import ActionResult, SigningService
from .safe_errors import DiagnosticArgumentParser, debug_diagnostics, emit_debug
from .participant_errors import participant_failure


LOG = logging.getLogger("signer-agent")


def _protect_transport_logs():
    # HTTP clients log complete endpoints in retry warnings as well as DEBUG.
    # Suppress their logger trees at the CLI boundary, including child loggers
    # with explicit levels; signer diagnostics still report safe failure codes.
    for name in ("urllib3", "requests", "web3", "aiohttp", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False


def _parser() -> argparse.ArgumentParser:
    parser = DiagnosticArgumentParser(description="FROST signer agent for enforced coordinator sessions")
    parser.add_argument("--debug", action="store_true", help="write sanitized error locations as JSON to stderr")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, description in (("relay-keeper", "keep the pinned Bitcoin relay current"),
                                 ("setup", "public invitation, enrollment and checked manifests"),
                                 ("dkg", "local DKG, encrypted keys and mailbox exchange"),
                                 ("bootstrap", "plan, apply, resume, verify infrastructure"),
                                 ("ops", "prepare, simulate and execute typed operations"),
                                 ("api", "authenticated loopback operational API"),
                                 ("bitcoin", "Bitcoin plans, broadcasts and proofs"),
                                 ("flow", "bounded automatic progress for enrolled DKG and explicit operations"),
                                 ("index", "confirmed EVM event indexing"),
                                 ("dao", "user wallet, staking, rewards and DAO governance"),
                                 ("ulr-treasury", "shared ULR snapshots and daily treasury payouts"),
                                 ("system-profile", "prepare one signed Bitcoin and ULR bootstrap policy"),
                                 ("zk-worker", "prepare, prove, verify and publish Bitcoin ZK-SPV transitions"),
                                 ("configure", "launch wizard and remote RPC checks"),
                                 ("startup-rewards", "funded introductory APY inspection and daily payout"),
                                 ("nft", "FROST-governed discount collections and mint"),
                                 ("fee-sale", "approve and settle funded sales of S or MP fees"),
                                 ("custody-transfer", "move Bitcoin backing before issuing confederation shares"),
                                 ("family-root", "inspect/pin original Bitcoin root and prepare new graph enrollment"),
                                 ("dao-report", "prepare funded daily settlement")):
        subparsers.add_parser(command, help=description)
    subparsers.add_parser("doctor", help="validate configuration, ABI, RPC and coordinator address")

    pending = subparsers.add_parser("pending", help="list actionable sessions without changing chain state")
    pending.add_argument("--session", type=int)
    pending.add_argument("--json", action="store_true")

    run_once = subparsers.add_parser("run-once", help="process accumulated requests once and exit")
    run_once.add_argument("--session", type=int)
    policy = run_once.add_mutually_exclusive_group()
    policy.add_argument("--approve-all", action="store_true")
    policy.add_argument("--reject-all", action="store_true")
    run_once.add_argument("--reason", default="rejected by signer policy")

    decide = subparsers.add_parser("decide", help="process one session non-interactively")
    decide.add_argument("session", type=int)
    decide.add_argument("decision", choices=["approve", "reject", "skip"])
    decide.add_argument("--reason", default="rejected by signer")

    watch = subparsers.add_parser("watch", help="poll continuously; AUTO_SIGN controls approval")
    watch.add_argument("--interval", type=int)

    relay = subparsers.add_parser("relay", help="authorize a target operation from a finalized FROST session")
    relay.add_argument("session", type=int)
    relay.add_argument("authorization")
    relay.add_argument("target")

    finalize = subparsers.add_parser("finalize", help="aggregate verified on-chain shares as any participant")
    finalize.add_argument("session", type=int)
    return parser


def _show(service: SigningService, sessions: Iterable[object], as_json: bool = False) -> None:
    descriptions = [service.describe(session) for session in sessions]
    if as_json:
        print(json.dumps(descriptions, indent=2, sort_keys=True))
        return
    if not descriptions:
        print("No pending signing sessions.")
        return
    for item in descriptions:
        print(
            "session={session_id} purpose={purpose} message={message_hash} "
            "origin={origin_contract}:{origin_id} threshold={threshold}/{total} "
            "commits={commits} shares={shares} refusals={refusals} expires_in={seconds_remaining}s".format(
                **item
            )
        )


def _print_result(result: ActionResult) -> None:
    suffix = " tx=%s" % result.tx_hash if result.tx_hash else ""
    print("session=%s status=%s detail=%s%s" % (result.session_id, result.status, result.detail, suffix))


def _interactive_decision(session_id: int) -> str:
    while True:
        value = input("Session %s: [a]pprove, [r]eject, [s]kip? " % session_id).strip().lower()
        if value in {"a", "approve", "y", "yes"}:
            return "approve"
        if value in {"r", "reject", "n", "no"}:
            return "reject"
        if value in {"s", "skip", ""}:
            return "skip"


def _process(
    service: SigningService,
    session: object,
    decision: str,
    reason: str,
) -> ActionResult:
    if decision == "approve":
        return service.approve(session)
    if decision == "reject":
        return service.reject(session, reason)
    return ActionResult(session.session_id, "skipped", "left pending")


def run_once(
    service: SigningService,
    session_id: Optional[int] = None,
    approve_all: bool = False,
    reject_all: bool = False,
    reason: str = "rejected by signer policy",
    interactive: bool = True,
) -> int:
    sessions = service.pending(session_id)
    if not sessions:
        print("No pending signing sessions.")
        return 0
    exit_code = 0
    for session in sessions:
        _show(service, [session])
        if approve_all:
            decision = "approve"
        elif reject_all:
            decision = "reject"
        elif interactive:
            decision = _interactive_decision(session.session_id)
        else:
            decision = "skip"
        try:
            result = _process(service, session, decision, reason)
            _print_result(result)
            if result.status in {"blocked", "unsupported"}:
                exit_code = 2
        except Exception as exc:
            failure = participant_failure(exc)
            code = failure["code"]
            emit_debug(exc, code=code)
            LOG.error("session %s failed code=%s", session.session_id, code)
            print("session=%s status=error code=%s %s" % (session.session_id, code, failure["message"]), file=sys.stderr)
            exit_code = 1
    return exit_code


def main(argv: Optional[Iterable[str]] = None) -> int:
    # Provider/configuration exceptions can contain RPC credentials, key values,
    # calldata and local paths. This boundary also covers pending/decide/watch.
    _protect_transport_logs()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    end = arguments.index("--") if "--" in arguments else len(arguments)
    options = arguments[:end]
    with debug_diagnostics("--debug" in options):
        try:
            return _main([option for option in options if option != "--debug"] + arguments[end:])
        except Exception as exc:
            failure = participant_failure(exc)
            emit_debug(exc, code=failure["code"])
            print(json.dumps(failure))
            return 2


def _main(argv: Optional[Iterable[str]] = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    if arguments and arguments[0] in ("setup","bootstrap","api","ops","bitcoin","index","dao-report","dao","ulr-treasury","system-profile","configure","startup-rewards","nft","fee-sale","custody-transfer","family-root","flow","relay-keeper","zk-worker"):
        if arguments[0] == "relay-keeper":
            from .relay_keeper import main as operational_main
        elif arguments[0] == "flow":
            from .flow_cli import main as operational_main
        elif arguments[0] == "setup":
            from .onboarding import main as operational_main
        elif arguments[0] == "bootstrap":
            from .bootstrap import main as operational_main
        elif arguments[0] == "dao-report":
            from .dao_report import main as operational_main
        elif arguments[0] == "dao":
            from .dao_cli import main as operational_main
        elif arguments[0] == "startup-rewards":
            from .startup_rewards_cli import main as operational_main
        elif arguments[0] == "nft":
            from .nft_cli import main as operational_main
        elif arguments[0] == "fee-sale":
            from .fee_sale_cli import main as operational_main
        elif arguments[0] == "zk-worker":
            from .zk_worker import main as operational_main
        elif arguments[0] == "configure":
            from .launch_configurator import main as operational_main
        elif arguments[0] == "system-profile":
            from .bootstrap_system import main as operational_main
        elif arguments[0] == "custody-transfer":
            from .custody_transfer_cli import main as operational_main
        elif arguments[0] == "family-root":
            from .family_registry_cli import main as operational_main
        elif arguments[0] == "ulr-treasury":
            from .ulr_treasury_cli import main as operational_main
        elif arguments[0] == "index":
            from .indexer import main as operational_main
        elif arguments[0] == "bitcoin":
            from .bitcoin_cli import main as operational_main
        elif arguments[0] == "ops":
            from .operation_worker import main as operational_main
        else:
            from .operational_api import main as operational_main
        try:
            return operational_main(arguments[1:])
        except Exception as exc:
            from .transaction_wait import TransactionPending, TransactionRecoveryRequired
            if isinstance(exc, TransactionPending):
                print(json.dumps(exc.result, sort_keys=True))
                return 0
            if isinstance(exc, TransactionRecoveryRequired):
                emit_debug(exc, code="RECOVERY_REQUIRED")
                print(json.dumps(exc.result, sort_keys=True))
                return 2
            failure = participant_failure(exc)
            emit_debug(exc, code=failure["code"])
            print(json.dumps(failure))
            return 2
    if arguments and arguments[0] == "doctor" and "--profile" in arguments:
        from .doctor import main as doctor_main
        return doctor_main(arguments[1:])
    if arguments and arguments[0] == "dkg":
        from .participant_cli import main as participant_main
        return participant_main(arguments[1:])
    args = _parser().parse_args(arguments)
    # DEBUG is for signer diagnostics, not HTTP transports that log full URLs.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    LOG.setLevel(getattr(logging, args.log_level))
    try:
        config = Config.from_env()
        service = SigningService(config)
    except Exception as exc:
        failure = participant_failure(exc)
        emit_debug(exc, code=failure["code"])
        print("Signer configuration error (%s). %s" % (failure["code"], failure["message"]), file=sys.stderr)
        return 2

    if args.command == "doctor":
        print("Configuration: OK")
        print("RPC chain ID: %s" % service.client.chain_id)
        print("Coordinator runtime/ABI: OK")
        print("Signer address: %s" % service.client.sender)
        print("Local public-key share X: 0x%064x" % service.public_key_share.x)
        return 0

    if args.command == "pending":
        sessions = service.pending(args.session)
        _show(service, sessions, args.json)
        return 0
    if args.command == "run-once":
        return run_once(
            service,
            session_id=args.session,
            approve_all=args.approve_all,
            reject_all=args.reject_all,
            reason=args.reason,
        )
    if args.command == "decide":
        sessions = service.pending(args.session)
        if not sessions:
            print("Session is not actionable.", file=sys.stderr)
            return 1
        result = _process(service, sessions[0], args.decision, args.reason)
        _print_result(result)
        return 2 if result.status in {"blocked", "unsupported"} else 0
    if args.command == "relay":
        try:
            tx_hash = service.client.relay_authorization(args.session, args.authorization, args.target)
        except Exception as exc:
            failure = participant_failure(exc)
            emit_debug(exc, code=failure["code"])
            print("Relay failed (%s). %s" % (failure["code"], failure["message"]), file=sys.stderr)
            return 1
        print("session=%s status=relayed tx=%s" % (args.session, tx_hash))
        return 0
    if args.command == "finalize":
        try:
            session = service.client.session(args.session)
            result = service.try_finalize(session)
        except Exception as exc:
            failure = participant_failure(exc)
            emit_debug(exc, code=failure["code"])
            print("Finalization failed (%s). %s" % (failure["code"], failure["message"]), file=sys.stderr)
            return 1
        if result is None:
            print("Session does not have a complete verified share set.", file=sys.stderr)
            return 1
        _print_result(result)
        return 0
    if args.command == "watch":
        interval = args.interval or config.poll_interval
        LOG.info("watching FROST sessions every %s seconds", interval)
        while True:
            run_once(service, approve_all=config.auto_sign, interactive=False)
            time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
