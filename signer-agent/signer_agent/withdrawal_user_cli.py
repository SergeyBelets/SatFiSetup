"""Generate, explicitly accept, resume and inspect an owner withdrawal quote."""
import json
from pathlib import Path

from .safe_errors import DiagnosticArgumentParser
from .btc_fee_reserve import Inspector, load_config
from .onboarding import write_public, read_public
from .withdrawal_user import Withdrawal


def main(argv=None):
    p = DiagnosticArgumentParser(prog="frost-signer bitcoin withdrawal")
    sub = p.add_subparsers(dest="action", required=True)
    for action in ("prepare", "submit", "status", "cancel", "version-quote", "approve-version"):
        c = sub.add_parser(action)
        c.add_argument("--config", required=True)
        if action == "prepare":
            c.add_argument("--calculation", choices=("payout", "total"), required=True)
            c.add_argument("--asset", choices=("MP", "S"), required=True)
            c.add_argument("--fee-mode", choices=("BTC", "MP", "S"))
            c.add_argument("--payout-sats")
            c.add_argument("--total-units")
            c.add_argument("--fee-cap-sats", required=True)
            c.add_argument("--destination", required=True)
            c.add_argument("--pool")
            c.add_argument("--nft")
            c.add_argument("--deadline", help="absolute Unix deadline; default: current EVM time + --valid-for-seconds")
            c.add_argument("--valid-for-seconds", default="3600")
            c.add_argument("--max-rate", required=True)
            c.add_argument("--refund-fee-cap", default="0")
            c.add_argument("--refund-address")
            c.add_argument("--ttl", default="300")
            c.add_argument("--out", required=True, help="new generated public quote file")
        elif action == "submit":
            c.add_argument("--quote", required=True)
            c.add_argument("--accept", required=True, help="exact displayed quote_hash; repeat to resume")
        else:
            c.add_argument("--operation-id", required=True)
            if action not in ("status","cancel"):
                c.add_argument("--batch", required=True)
            if action == "approve-version":
                c.add_argument("--accept", required=True, help="exact terms_hash from version-quote")
    a = p.parse_args(argv)
    w = Withdrawal(Inspector(load_config(a.config)))
    if a.action == "prepare":
        if Path(a.out).exists() or Path(a.out).is_symlink():
            raise ValueError("WITHDRAWAL_USER_QUOTE_EXISTS")
        from .btc_fee_reserve import number
        deadline = a.deadline or str(w.i.w3.eth.get_block("latest").timestamp + number(a.valid_for_seconds, 60, 604800))
        options = dict(calculation=a.calculation, asset=a.asset, fee_mode=a.fee_mode or a.asset,
            payout_sats=a.payout_sats, total_units=a.total_units, fee_cap_sats=a.fee_cap_sats,
            destination=a.destination, pool=a.pool, nft=a.nft, deadline=deadline,
            max_rate=a.max_rate, refund_fee_cap=a.refund_fee_cap, refund_address=a.refund_address)
        result = w.prepare(options, ttl=a.ttl)
        write_public(a.out, result)
    elif a.action == "submit":
        body = read_public(a.quote)
        if body.get("quote_hash") != a.accept:
            raise ValueError("WITHDRAWAL_USER_QUOTE")
        result = w.submit(body["quote"], a.accept)
    elif a.action == "status":
        result = w.status(a.operation_id)
    elif a.action == 'cancel':result=w.cancel(a.operation_id)
    else:
        batch = bytes.fromhex(a.batch.removeprefix("0x"))
        result = w.version_quote(a.operation_id, batch) if a.action == "version-quote" else w.approve_version(a.operation_id, batch, a.accept)
    print(json.dumps(result, sort_keys=True))
    return 0
