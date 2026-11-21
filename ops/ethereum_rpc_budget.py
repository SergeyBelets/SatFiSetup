"""Local Forge RPC boundary enforcing budgets on the actual signed transaction.

Forge's block/transaction gas flags also constrain its large local script
scaffold. They cannot safely express a child-transaction spending limit. This
proxy checks EIP-1559 envelopes immediately before forwarding a send, including
cached transactions resumed by Forge. It never retains raw signed transactions.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from threading import Thread

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

READ_METHODS = frozenset({
    "web3_clientVersion", "net_version", "eth_chainId", "eth_blockNumber",
    "eth_getBalance", "eth_getCode", "eth_getStorageAt", "eth_getProof",
    "eth_getTransactionCount", "eth_getBlockByNumber", "eth_getBlockByHash",
    "eth_getTransactionReceipt", "eth_getTransactionByHash", "eth_call",
    "eth_estimateGas", "eth_gasPrice", "eth_maxPriorityFeePerGas", "eth_feeHistory",
    "eth_getLogs", "eth_getBlockTransactionCountByHash", "eth_getBlockTransactionCountByNumber",
})
MAX_REQUEST_BYTES = 2 * 1024 * 1024


def checked_request(request, sender, fees, maximum_gas):
    """Validate the whole envelope before the backend can receive any mutation."""
    if (not isinstance(request, dict) or not {"jsonrpc", "id", "method"} <= set(request)
            or set(request) - {"jsonrpc", "id", "method", "params"}
            or request["jsonrpc"] != "2.0" or type(request["id"]) not in (str, int)
            or not isinstance(request.get("params", []), list)):
        raise ValueError("ETHEREUM_RPC_REQUEST_REJECTED")
    method = request["method"]
    if method in READ_METHODS:
        return
    if method != "eth_sendRawTransaction" or len(request.get("params", [])) != 1:
        raise ValueError("ETHEREUM_RPC_METHOD_REJECTED")
    try:
        raw = HexBytes(request["params"][0])
        if len(raw) > 131072 or not raw or raw[0] != 2:
            raise ValueError()
        transaction = TypedTransaction.from_bytes(raw).as_dict()
        if (transaction["type"] != 2 or transaction["chainId"] != 1
                or transaction["value"] != 0
                or not 0 < transaction["gas"] <= min(maximum_gas, 2**24)
                or not 0 <= transaction["maxPriorityFeePerGas"] <= fees["maxPriorityFeePerGas"]
                or not transaction["maxPriorityFeePerGas"] <= transaction["maxFeePerGas"] <= fees["maxFeePerGas"]
                or Account.recover_transaction(raw).lower() != sender.lower()):
            raise ValueError()
    except Exception:
        raise ValueError("ETHEREUM_SIGNED_TRANSACTION_BUDGET_REJECTED") from None


@contextmanager
def budget_rpc(w3, sender, fees, maximum_gas):
    """Forward only read RPCs and locally budget-checked signed Ethereum sends."""
    route = "/" + secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_):
            pass

        def do_POST(self):
            request_id = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != route or not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError()
                def unique(pairs):
                    output = {}
                    for key, value in pairs:
                        if key in output:
                            raise ValueError()
                        output[key] = value
                    return output
                body = json.loads(self.rfile.read(length), object_pairs_hook=unique)
                batch = body if isinstance(body, list) else [body]
                if not 0 < len(batch) <= 128:
                    raise ValueError()
                for request in batch:
                    checked_request(request, sender, fees, maximum_gas)
                result = []
                for request in batch:
                    request_id = request["id"]
                    # JSON-RPC permits omitted params for no-argument calls;
                    # Forge uses that shape for its initial chain-ID request.
                    response = w3.provider.make_request(request["method"], request.get("params", []))
                    if "error" in response:
                        item = {"error": {"code": -32000, "message": "ETHEREUM_UPSTREAM_RPC_REJECTED"}}
                    else:
                        item = {"result": response["result"]}
                    result.append(dict(jsonrpc="2.0", id=request_id, **item))
                response = result if isinstance(body, list) else result[0]
            except Exception:
                response = {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32000, "message": "ETHEREUM_RPC_BUDGET_REJECTED"}}
            encoded = json.dumps(response, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{route}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main(argv=None):
    """Run a standalone Forge leaf script behind the same Ethereum send gate."""
    import argparse
    from pathlib import Path
    import os
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "signer-agent"))
    sys.path.insert(0, str(root / "ops"))
    from signer_agent.evm_network import read_block, transaction_policy, check_transaction_gas
    from deployment_stages import connect_rpc, protect_transport_logs, redact, address
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--sender", required=True)
    parser.add_argument("--confirmations", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    protect_transport_logs()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if (len(command) < 3 or Path(command[0]).name != "forge" or command[1] != "script"
            or any(item == "--rpc-url" or item.startswith("--rpc-url=") for item in command)):
        raise ValueError("ETHEREUM_FORGE_COMMAND_REQUIRED")
    sender = address(args.sender)
    w3 = connect_rpc(args.rpc_url)
    if w3.eth.chain_id != 1:
        raise ValueError("ETHEREUM_MAINNET_CHAIN_REQUIRED")
    read_block(w3, args.confirmations)
    fees = transaction_policy(w3)
    maximum_gas = int(os.environ["SATOSHIFI_MAX_TX_GAS"])
    check_transaction_gas(w3, {"gas": maximum_gas})
    with budget_rpc(w3, sender, fees, maximum_gas) as local_rpc:
        # Retain a simulation margin: refunding calls (e.g. renounceRole) can
        # need more gas to execute than their net gas usage. The send gate still
        # enforces the operator's exact ceiling on the signed gas limit.
        command = command + ["--rpc-url", local_rpc, "--with-gas-price", str(fees["maxFeePerGas"]),
            "--priority-gas-price", str(fees["maxPriorityFeePerGas"]), "--gas-estimate-multiplier", "130"]
        result = subprocess.run(command, text=True, capture_output=True)
    print(redact((result.stdout + result.stderr).replace(local_rpc, args.rpc_url), args.rpc_url), end="")
    return result.returncode


if __name__ == "__main__":
    import sys
    try:
        sys.exit(main())
    except Exception:
        print("ETHEREUM_FORGE_BUDGET_EXECUTION_FAILED", file=sys.stderr)
        sys.exit(1)
