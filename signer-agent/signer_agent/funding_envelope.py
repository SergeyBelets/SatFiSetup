"""Public funding extraction using the pinned rust-bitcoin consensus codec.

This is transaction serialization, not SPV, script validation, UTXO proof or an
address-issuance qualification. Preserve the full transaction for recovery.
"""
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from . import crypto
from .bitcoin_onchain_plan import hex_value

PROFILE = "FUNDING_STRIPPED_100K_V1"


def inspect_funding(raw, txid):
    hex_value(txid, 32)
    if not isinstance(raw, str) or len(raw) > 8_000_000:
        raise ValueError("BTC_FUNDING_RAW_ENVELOPE")
    result = crypto.call("bitcoin_funding", raw=raw, txid=txid)
    if result.get("profile") != PROFILE or result.get("txid") != txid:
        raise ValueError("BTC_FUNDING_PARSER_BINDING")
    return result


def main(argv):
    parser = DiagnosticArgumentParser(prog="frost-signer bitcoin inspect-funding")
    parser.add_argument("--raw-file", required=True, help="file containing full raw transaction hex")
    parser.add_argument("--txid", required=True, help="expected display-order Bitcoin txid, not wtxid")
    args = parser.parse_args(argv)
    path = Path(args.raw_file)
    if path.stat().st_size > 8_000_002:
        raise ValueError("BTC_FUNDING_RAW_ENVELOPE")
    result = inspect_funding(path.read_text().strip(), args.txid)
    print(json.dumps(result, sort_keys=True))
    return 0
