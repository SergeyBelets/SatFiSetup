"""Submit an existing signed payout with a recipient-funded CPFP child."""

import json
from pathlib import Path

from .bitcoin_network import is_public_network, validate_policy
from .bitcoin_onchain_plan import MAX_MONEY, decimal, script
from .bitcoin_rpc import BitcoinRPC, BitcoinRPCError, payment_block, payment_block_height, payment_in_mempool, satoshis
from .funding_envelope import inspect_funding
from .onboarding import read_public
from .safe_errors import DiagnosticArgumentParser


def _signed(path, txid):
    with Path(path).open("rb") as stream:
        data = stream.read(801_001)
    if len(data) > 801_000:
        raise ValueError("BTC_FUNDING_RAW_ENVELOPE")
    data = data.decode("ascii").strip()
    envelope = json.loads(data) if data.startswith("{") else None
    if envelope is not None:
        if set(envelope) != {"signed", "txid", "wtxid"} or envelope["txid"] != txid:
            raise ValueError("BTC_CPFP_SIGNED_REQUIRED")
        data = envelope["signed"]
    transaction = inspect_funding(data, txid)
    if (transaction["coinbase"] or transaction["wtxid"] == txid
            or envelope is not None and transaction["wtxid"] != envelope["wtxid"]):
        raise ValueError("BTC_CPFP_SIGNED_REQUIRED")
    return transaction


def _parent_confirmed(bitcoin, txid):
    try:
        details = bitcoin.call("getrawtransaction", txid, True)
    except BitcoinRPCError as error:
        if error.rpc_code != -5:
            raise
        return False
    block = payment_block(details, txid, 1, bitcoin=bitcoin)
    if block is None:
        return False
    payment_block_height(bitcoin, block)
    return True


def payout_package(bitcoin, parent, child, vout, payout_script, return_script, fee_cap, *, submit=False):
    if (not 0 <= vout < len(parent["outputs"]) or parent["outputs"][vout]["script"] != payout_script
            or len(child["inputs"]) != 1
            or child["inputs"][0]["outpoint"] != f"{parent['txid']}:{vout}"
            or len(child["outputs"]) != 1 or child["outputs"][0]["script"] != return_script):
        raise ValueError("BTC_CPFP_PAYOUT_BINDING")
    amount = decimal(parent["outputs"][vout]["amount"], MAX_MONEY, positive=True)
    returned = decimal(child["outputs"][0]["amount"], MAX_MONEY, positive=True)
    fee = amount - returned
    if not 0 < fee <= fee_cap:
        raise ValueError("BTC_CPFP_FEE_LIMIT")

    bitcoin.check_network()
    result = dict(parent_txid=parent["txid"], child_txid=child["txid"], payout_vout=str(vout), child_fee_sats=str(fee))
    if _parent_confirmed(bitcoin, parent["txid"]):
        return dict(result, status="PAYOUT_CONFIRMED")
    if payment_in_mempool(bitcoin, child["txid"]):
        return dict(result, status="PACKAGE_IN_MEMPOOL")

    transactions = [child] if payment_in_mempool(bitcoin, parent["txid"]) else [parent, child]
    raws = [transaction["raw"] for transaction in transactions]
    acceptance = bitcoin.call("testmempoolaccept", raws)
    if not isinstance(acceptance, list) or len(acceptance) != len(transactions):
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    for observed, transaction in zip(acceptance, transactions):
        if (not isinstance(observed, dict) or observed.get("txid") != transaction["txid"]
                or observed.get("wtxid") != transaction["wtxid"]):
            raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
        if observed.get("allowed") is not True:
            raise ValueError("MEMPOOL_REJECTED")
    fees = acceptance[-1].get("fees")
    if not isinstance(fees, dict):
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    if satoshis(fees.get("base")) != fee:
        raise ValueError("BTC_CPFP_FEE_LIMIT")
    if not submit:
        return dict(result, status="PACKAGE_ACCEPTABLE")

    # Core may accept the parent even if it rejects the full package.
    receipt = bitcoin.call("submitpackage", raws)
    if not isinstance(receipt, dict) or receipt.get("package_msg") != "success":
        raise ValueError("MEMPOOL_REJECTED")
    accepted = receipt.get("tx-results")
    if not isinstance(accepted, dict):
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    for transaction in transactions:
        observed = accepted.get(transaction["wtxid"])
        if (not isinstance(observed, dict) or observed.get("txid") != transaction["txid"]
                or "error" in observed or "other-wtxid" in observed):
            raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    return dict(result, status="PACKAGE_IN_MEMPOOL")


def main(argv):
    parser = DiagnosticArgumentParser(prog="frost-signer bitcoin cpfp-payout", description=__doc__)
    parser.add_argument("--bitcoin-rpc", required=True)
    parser.add_argument("--bitcoin-cookie", required=True)
    parser.add_argument("--bitcoin-network", choices=("regtest", "testnet4", "mainnet"), required=True)
    parser.add_argument("--bitcoin-policy")
    parser.add_argument("--parent-raw", required=True, help="signed payout hex or btc-broadcast export")
    parser.add_argument("--parent-txid", required=True, help="previously reviewed payout txid (display order)")
    parser.add_argument("--payout-vout", required=True)
    parser.add_argument("--payout-script", required=True)
    parser.add_argument("--child-raw", required=True, help="recipient-signed child in a raw hex file")
    parser.add_argument("--child-txid", required=True, help="reviewed child txid (display order)")
    parser.add_argument("--return-script", required=True)
    parser.add_argument("--max-child-fee-sats", required=True)
    parser.add_argument("--submit", action="store_true", help="submit the checked signed transaction package")
    a = parser.parse_args(argv)
    if bool(a.bitcoin_policy) != is_public_network(a.bitcoin_network):
        parser.error("mainnet and testnet4 require --bitcoin-policy; regtest forbids it")
    policy = validate_policy(read_public(a.bitcoin_policy), a.bitcoin_network) if a.bitcoin_policy else None
    bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie, network=a.bitcoin_network, policy=policy)
    result = payout_package(
        bitcoin, _signed(a.parent_raw, a.parent_txid), _signed(a.child_raw, a.child_txid),
        decimal(a.payout_vout, 2**32 - 1), script(a.payout_script), script(a.return_script),
        decimal(a.max_child_fee_sats, MAX_MONEY, positive=True), submit=a.submit,
    )
    print(json.dumps(result, sort_keys=True))
    return 0
