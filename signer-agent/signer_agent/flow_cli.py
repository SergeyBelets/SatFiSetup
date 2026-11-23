"""Bounded polling of explicitly selected operations using the normal checked CLI.

There is no approval policy here: DKG uses the enrolled invitation; payment
signers opt into one exact batch/board, and bootstrap still pauses for reviewed
FROST/custodian authorizations. All durable retries remain in the existing
transaction and cryptographic journals.
"""
import contextlib
import io
import json
import time
from pathlib import Path

from .safe_errors import DiagnosticArgumentParser
from .participant_errors import participant_exception_code

ROLES = ("evm_ceremony", "conf_ceremony", "btc_ceremony")
PENDING_ERRORS = frozenset((
    "TRANSACTION_UNCONFIRMED", "BOARD_TRANSACTION_UNCONFIRMED",
    "BTC_TRANSACTION_UNCONFIRMED", "EVM_CONFIRMATIONS_PENDING",
    "REGISTRY_NOT_BOUND", "BTC_COMMITMENTS_MISSING",
    "BTC_SIGNATURE_PACKETS_MISSING", "PAYMENT_DEPTH", "MAINNET_RELAY_STALE",
    "PAYMENT_NOT_CONFIRMED", "BITCOIN_OBSERVATION_UNAVAILABLE",
    "MAINNET_CORE_NOT_SYNCED", "MAINNET_TXINDEX_NOT_SYNCED",
    "CREATE_SIMULATION_WAIT_FOR_PRIOR_NONCE", "EVM_RPC_BEHIND", "BlockNotFound",
))
BITCOIN_POLL_ACTIONS = frozenset(("btc-commit", "btc-sign", "btc-broadcast", "btc-settle", "btc-relay"))
BITCOIN_POLL_ERRORS = frozenset(("BITCOIN_RPC_FAILED", "BITCOIN_RPC_REJECTED", "MAINNET_CORE_REORG_DURING_CHECK",
                               "BTC_RECOVERY_CHANGED", "BTC_PLAN_BELOW_MEMPOOL_FEE", "BTC_MEMPOOL_POLICY_UNAVAILABLE"))


def invoke(group, arguments):
    # Never launch a shell, write secrets to argv, or expose provider exceptions.
    from .cli import main
    stream = io.StringIO()
    from .transaction_wait import nonblocking_transactions
    with contextlib.redirect_stdout(stream), nonblocking_transactions():
        code = main([group, *map(str, arguments)])
    try:
        result = json.loads(stream.getvalue())
        if not isinstance(result, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("FLOW_INVALID_RESULT") from None
    if result.get("status") == "ERROR":
        code = result.get("code")
        if code in PENDING_ERRORS or (
            group == "dkg" and arguments and arguments[0] in BITCOIN_POLL_ACTIONS and code in BITCOIN_POLL_ERRORS
        ):
            return dict(status="WAITING", reason=code)
        # The nested CLI has already sanitized it, but keep this boundary strict.
        raise ValueError(participant_exception_code(ValueError(result.get("code"))))
    if code not in (0, 2):
        raise ValueError("FLOW_COMMAND_FAILED")
    return result


def _flags(a, names):
    result = []
    for name in names:
        value = getattr(a, name, None)
        if value is not None:
            result += ["--" + name.replace("_", "-"), value]
    return result


def dkg_tick(a):
    status = invoke("setup", ["status", "--board", a.board, "--rpc", a.rpc])
    if status.get("status") == "WAITING":
        return status, False
    rows = {}
    # Round-robin submission advances every independent ceremony on each poll.
    # Receipt/round finality is enforced by run-once, not inferred from status.
    for role in ROLES:
        if role in status.get("role_participants", {}):
            from .spv_automation import identity
            if identity(a.identity_file) not in status["role_participants"][role]:
                rows[role] = dict(status="NOT_A_PARTICIPANT")
                continue
        phase = status["ceremonies"][role]["status"]
        if phase == "FAILED":
            raise ValueError("CEREMONY_TERMINAL_FAILURE")
        if phase == "NOT_STARTED":
            rows[role] = dict(status="NOT_STARTED")
            continue
        state = Path(a.directory) / (role + ".enc")
        if not state.is_file() or state.is_symlink():
            raise ValueError("FLOW_ENROLLED_STATE_REQUIRED")
        rows[role] = invoke("dkg", ["run-once", "--role", role,
            "--state", state, "--password-file", Path(a.directory) / "password",
            *_flags(a, ("board", "rpc", "identity_file", "bitcoin_rpc", "bitcoin_cookie"))])
    if any(row["status"] == "DKG_REVIEW_REQUIRED" for row in rows.values()):
        return dict(status="DKG_REVIEW_REQUIRED", ceremonies=rows), False
    complete = all(row["status"] in ("ACTIVE", "NOT_A_PARTICIPANT") for row in rows.values())
    return dict(status="ACTIVE" if complete else "IN_PROGRESS", ceremonies=rows), complete


def payment_tick(a, phase):
    common = ["--role", "btc_ceremony", *_flags(a, (
        "board", "rpc", "identity_file", "state", "password_file",
        "signing_board", "batch", "bitcoin_rpc", "bitcoin_cookie"))]
    actions = ("btc-commit", "btc-sign") if a.actor == "signer" else ("btc-broadcast", "btc-settle")
    successes = ({"COMMITTED"}, {"SIGNED"}) if a.actor == "signer" else ({"BROADCAST"}, {"PAID", "BTC_PAYMENT_SETTLED"})
    result = invoke("dkg", [actions[phase], *common])
    if result["status"] == "RECOVERY_REQUIRED":
        raise ValueError("PAYMENTS_PAUSED_FOR_RECOVERY")
    if a.actor == "operator" and result["status"] in successes[1]:
        return result, len(actions), True
    if result["status"] in successes[phase]:
        phase += 1
    return result, phase, phase == len(actions)


def main(argv):
    p = DiagnosticArgumentParser(prog="frost-signer flow")
    sub = p.add_subparsers(dest="action", required=True)
    for action in ("bootstrap", "dkg", "payment", "relay"):
        c = sub.add_parser(action)
        c.add_argument("--interval", type=int, default=5)
        c.add_argument("--timeout", type=int, default=3600)
        c.add_argument("--once", action="store_true")
        c.add_argument("--identity-file", required=True)
        c.add_argument("--bitcoin-rpc")
        c.add_argument("--bitcoin-cookie")
        if action == "bootstrap":
            source = c.add_mutually_exclusive_group(required=True)
            source.add_argument("--profile")
            source.add_argument("--deployment")
            c.add_argument("--state", required=True)
            c.add_argument("--rpc", help="Override the EVM RPC transport for this deployment")
            funding = c.add_mutually_exclusive_group()
            funding.add_argument("--pause-for-funding", action="store_true")
            funding.add_argument("--funding-complete", action="store_true")
            c.add_argument("--funding-min-wei")
            c.add_argument("--funding-operator-min-wei")
            c.add_argument("--ulr-distribution-complete", action="store_true")
            c.add_argument("--accept-ulr-distribution")
        else:
            c.add_argument("--board", required=True)
            c.add_argument("--rpc", required=True)
            if action == "dkg":
                c.add_argument("--directory", required=True)
            else:
                c.add_argument("--state", required=True)
                c.add_argument("--password-file", required=True)
            if action == "payment":
                c.add_argument("--actor", choices=("signer", "operator"), required=True)
                c.add_argument("--signing-board", required=True)
                c.add_argument("--batch", required=True)
    a = p.parse_args(argv)
    if not 1 <= a.interval <= 300 or not 1 <= a.timeout <= 604800:
        p.error("interval must be 1..300 seconds; timeout must be 1..604800 seconds")
    if a.action == "payment":
        from .bitcoin_onchain_plan import hex_value
        hex_value(a.batch.removeprefix("0x"), 32)
    deadline, phase, previous = time.monotonic() + a.timeout, 0, None
    funding_requested = False
    while True:
        if a.action == "dkg":
            result, complete = dkg_tick(a)
        elif a.action == "bootstrap":
            options = _flags(a, ("profile", "deployment", "state", "identity_file", "rpc", "bitcoin_rpc", "bitcoin_cookie"))
            if not funding_requested:
                if a.pause_for_funding: options += ["--pause-for-funding"]
                if a.funding_complete: options += ["--funding-complete"]
                if a.ulr_distribution_complete: options += ["--ulr-distribution-complete"]
                options += _flags(a, ("funding_min_wei", "funding_operator_min_wei"))
                options += _flags(a, ("accept_ulr_distribution",))
            result = invoke("bootstrap", ["resume", *options])
            funding_requested |= result["status"] != "AWAITING_EVM_RPC"
            complete = result["status"] == "VERIFIED"
        elif a.action == "payment":
            result, phase, complete = payment_tick(a, phase)
            if phase == 1 and result["status"] in ("COMMITTED", "BROADCAST"):
                # A --once sweep should also resume after a previously completed
                # first stage; it must not get stuck replaying commit forever.
                result, phase, complete = payment_tick(a, phase)
        else:
            result = invoke("dkg", ["btc-relay", "--role", "btc_ceremony", *_flags(a, (
                "board", "rpc", "state", "password_file", "identity_file", "bitcoin_rpc", "bitcoin_cookie"))])
            # Keeper keeps polling even when caught up; --once is useful for cron.
            complete = a.once and result["status"] in ("MAINNET_RELAY_READY", "REGTEST_NETWORK_READY")
        output = json.dumps(result, sort_keys=True)
        if output != previous:
            print(output, flush=True)
            previous = output
        if complete:
            return 0
        if result["status"] in ("RECOVERY_REQUIRED", "DKG_REVIEW_REQUIRED"):
            return 2
        remaining = deadline - time.monotonic()
        if a.once or remaining <= 0:
            if not a.once:
                print(json.dumps(dict(status="TIMEOUT", action=a.action)), flush=True)
            return 2
        time.sleep(min(a.interval, remaining))
