"""Report actual finalized deployment receipt costs, separately from gas limits.

This read-only report consumes a staged deployment manifest. A sum over many
transactions may exceed the per-transaction cap; each signed limit must still fit.
RPC URLs, private keys, and calldata never appear in the report.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "signer-agent"))

from web3 import Web3
from signer_agent.evm_rpc import evm_web3
from signer_agent.evm_network import MAX_TRANSACTION_GAS, read_block
from signer_agent.participant_errors import participant_exception_code


def report(w3, state):
    if state.get("schema") != "satoshifi-deployment-v2" or w3.eth.chain_id != state["chain_id"]:
        raise ValueError("CHAIN_MISMATCH")
    block = read_block(w3, state["confirmations"])
    rows = []
    for tx_hash, recorded in state["transactions"].items():
        tx, receipt = w3.eth.get_transaction(tx_hash), w3.eth.get_transaction_receipt(tx_hash)
        if (Web3.to_hex(receipt["transactionHash"]).lower() != tx_hash.lower()
                or tx["from"].lower() != state["sender"].lower()
                or tx["nonce"] != recorded["nonce"]
                or receipt["blockNumber"] > block.number
                or bytes(w3.eth.get_block(receipt["blockNumber"]).hash) != bytes(receipt["blockHash"])):
            raise ValueError("DEPLOYMENT_GAS_RECEIPT_UNCONFIRMED")
        limit, used, price = int(tx["gas"]), int(receipt["gasUsed"]), int(receipt["effectiveGasPrice"])
        if not 0 <= used <= limit or price < 0:
            raise ValueError("DEPLOYMENT_GAS_RECEIPT_INVALID")
        if state["chain_id"] == 1 and limit > MAX_TRANSACTION_GAS:
            raise ValueError("ETHEREUM_TRANSACTION_GAS_LIMIT")
        rows.append(dict(tx_hash=tx_hash, contract=recorded["contract"],
            status="SUCCESS" if receipt["status"] == 1 else "REVERTED", gas_limit=limit,
            gas_used=used, effective_gas_price_wei=str(price), fee_paid_wei=str(used * price)))
    if bytes(w3.eth.get_block(block.number).hash) != bytes(block.hash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return dict(chain_id=state["chain_id"], observed_block=block.number,
        transaction_count=len(rows), total_gas_used=sum(row["gas_used"] for row in rows),
        maximum_transaction_gas_limit=max((row["gas_limit"] for row in rows), default=0),
        maximum_transaction_gas_used=max((row["gas_used"] for row in rows), default=0),
        total_fee_paid_wei=str(sum(int(row["fee_paid_wei"]) for row in rows)),
        protocol_transaction_cap=MAX_TRANSACTION_GAS if state["chain_id"] == 1 else None,
        transactions=rows)


def main(argv=None):
    from signer_agent.cli import _protect_transport_logs
    _protect_transport_logs()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True)
    p.add_argument("--rpc", default=os.environ.get("ETH_RPC_URL"))
    args = p.parse_args(argv)
    if not args.rpc:
        p.error("--rpc or ETH_RPC_URL is required")
    try:
        result = report(evm_web3(args.rpc, timeout=30),
            json.loads(Path(args.manifest).read_text()))
    except Exception as error:
        fixed = {"DEPLOYMENT_GAS_RECEIPT_UNCONFIRMED", "DEPLOYMENT_GAS_RECEIPT_INVALID"}
        code = error.args[0] if type(error) is ValueError and len(error.args) == 1 and error.args[0] in fixed else participant_exception_code(error)
        print(json.dumps(dict(status="ERROR", code=code)))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
