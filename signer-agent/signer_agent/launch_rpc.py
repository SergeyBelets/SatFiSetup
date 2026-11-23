"""Capability checks for the remote Sepolia and Bitcoin testnet4 profile."""

import os
import re

import requests

from .bitcoin_network import check_core, header_hash
from .bitcoin_rpc import BitcoinRPC, BitcoinRPCError
from .evm_rpc import endpoint, http_provider


REMOTE_PROFILE = "remote-sepolia-testnet4"
ZERO_ADDRESS = "0x" + "00" * 20
ZERO_HASH = "0x" + "00" * 32


class PreflightError(ValueError):
    def __init__(self, network, method, reason, *, rpc_code=None, http_status=None):
        super().__init__("LAUNCH_RPC_PREFLIGHT_FAILED")
        self.details = dict(network=network, method=method, reason=reason)
        if type(rpc_code) is int and -(2**31) <= rpc_code < 0:
            self.details["rpc_error_code"] = rpc_code
        if type(http_status) is int and 100 <= http_status <= 599:
            self.details["http_status"] = http_status


def _evm_checks(provider, config, simulator=None):
    checked = []

    def call(method, params, valid, *, client=provider):
        try:
            response = client.make_request(method, params)
        except requests.RequestException as error:
            raise PreflightError("sepolia", method, "RPC_TRANSPORT",
                http_status=getattr(error.response, "status_code", None)) from None
        except Exception:
            raise PreflightError("sepolia", method, "RPC_RESPONSE") from None
        if not isinstance(response, dict):
            raise PreflightError("sepolia", method, "RPC_RESPONSE")
        result, error = response.get("result"), response.get("error")
        if (error is None and "result" not in response) or (error is not None and result is not None):
            raise PreflightError("sepolia", method, "RPC_RESPONSE")
        if not valid(result, error):
            raise PreflightError("sepolia", method, "RPC_METHOD_CHECK",
                rpc_code=error.get("code") if isinstance(error, dict) else None)
        checked.append(method)
        return result

    def read(method, params, valid, **kwargs):
        return call(method, params, lambda result, error: error is None and valid(result), **kwargs)

    def quantity(value):
        return isinstance(value, str) and re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]*)", value) is not None

    def block(value):
        return (isinstance(value, dict) and quantity(value.get("number"))
                and isinstance(value.get("hash"), str) and re.fullmatch(r"0x[0-9a-f]{64}", value["hash"])
                and isinstance(value.get("transactions"), list))

    if read("eth_chainId", [], quantity) != "0xaa36a7":
        raise PreflightError("sepolia", "eth_chainId", "CHAIN_MISMATCH")
    read("eth_blockNumber", [], quantity)
    latest = read("eth_getBlockByNumber", ["latest", False], block)
    height = int(latest["number"], 16) - int(config["evm_confirmations"]) + 1
    if height < 0:
        raise PreflightError("sepolia", "eth_getBlockByNumber", "EVM_CONFIRMATIONS_PENDING")
    at = hex(height)
    anchor = read("eth_getBlockByNumber", [at, True], block)
    if anchor["number"] != at:
        raise PreflightError("sepolia", "eth_getBlockByNumber", "RPC_BLOCK_MISMATCH")
    txid = ZERO_HASH
    if anchor["transactions"]:
        first = anchor["transactions"][0]
        txid = first.get("hash") if isinstance(first, dict) else None
        if not isinstance(txid, str) or not re.fullmatch(r"0x[0-9a-f]{64}", txid):
            raise PreflightError("sepolia", "eth_getBlockByNumber", "RPC_RESPONSE")
    for method in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
        read(method, [txid], lambda value: value is None if txid == ZERO_HASH else
            isinstance(value, dict) and value.get("transactionHash", value.get("hash")) == txid)
    read("eth_getCode", [ZERO_ADDRESS, at], lambda value: isinstance(value, str) and value.startswith("0x"))
    read("eth_getBalance", [ZERO_ADDRESS, at], quantity)
    read("eth_getTransactionCount", [ZERO_ADDRESS, "pending"], quantity)
    read("eth_gasPrice", [], quantity)
    read("eth_maxPriorityFeePerGas", [], quantity)
    probe_call = {"to": ZERO_ADDRESS, "data": "0x"}
    read("eth_call", [probe_call, at], lambda value: value == "0x")
    read("eth_estimateGas", [probe_call, "pending"], quantity)
    log_height = config["evm_discovery_from_block"]
    log_block = at if log_height is None else hex(int(log_height))
    if int(log_block, 16) > int(latest["number"], 16):
        raise PreflightError("sepolia", "eth_getLogs", "LAUNCH_DISCOVERY_START_BLOCK")
    read("eth_getLogs", [{"address": ZERO_ADDRESS, "fromBlock": log_block, "toBlock": log_block}],
         lambda value: isinstance(value, list))

    def rejects_empty_tx(result, error):
        if result is not None or not isinstance(error, dict) or error.get("code") not in (-32602, -32000, -32003):
            return False
        message = error.get("message")
        return isinstance(message, str) and any(word in message[:512].lower() for word in (
            "rlp", "typed transaction too short", "empty transaction", "transaction is empty",
            "empty input", "could not decode", "unable to decode", "invalid raw transaction",
        ))

    call("eth_sendRawTransaction", ["0x"], rejects_empty_tx)
    simulation_at = at
    simulation_anchor = anchor
    if simulator is not None:
        chain = read("eth_chainId", [], quantity, client=simulator)
        if chain != "0xaa36a7":
            raise PreflightError("sepolia", "eth_simulateV1", "CREATE_SIMULATION_RPC_CHAIN_MISMATCH")
        head = read("eth_getBlockByNumber", ["latest", False], block, client=simulator)
        simulator_height = int(head["number"], 16) - int(config["evm_confirmations"]) + 1
        if simulator_height < 0:
            raise PreflightError("sepolia", "eth_simulateV1", "EVM_RPC_BEHIND")
        simulation_at = hex(min(height, simulator_height))
        if simulation_at != at:
            simulation_anchor = read("eth_getBlockByNumber", [simulation_at, False], block)
        other = read("eth_getBlockByNumber", [simulation_at, False], block, client=simulator)
        if (simulation_anchor["number"] != simulation_at or other["number"] != simulation_at
                or other["hash"] != simulation_anchor["hash"]):
            raise PreflightError("sepolia", "eth_simulateV1", "CREATE_SIMULATION_RPC_CHAIN_MISMATCH")

    def simulation(value):
        return (isinstance(value, list) and len(value) == 2
                and all(isinstance(item, dict) and isinstance(item.get("calls"), list)
                        and len(item["calls"]) == 1 and isinstance(item["calls"][0], dict)
                        and item["calls"][0].get("status") == "0x1"
                        and quantity(item["calls"][0].get("gasUsed")) for item in value))

    # Exercise the ordered simulation shape used by withdrawal-fee deployment.
    read("eth_simulateV1", [{"blockStateCalls": [{"calls": [probe_call]} for _ in range(2)],
                           "validation": False}, simulation_at], simulation, client=simulator or provider)
    if simulator is not None:
        for client in (provider, simulator):
            current = read("eth_getBlockByNumber", [simulation_at, False], block, client=client)
            if current["number"] != simulation_at or current["hash"] != simulation_anchor["hash"]:
                raise PreflightError("sepolia", "eth_simulateV1", "RPC_BLOCK_CHANGED")
    if read("eth_getBlockByNumber", [at, False], block)["hash"] != anchor["hash"]:
        raise PreflightError("sepolia", "eth_getBlockByNumber", "RPC_BLOCK_CHANGED")
    return dict(chain_id="11155111", methods=sorted(set(checked)),
                create_simulation="alternate_rpc" if simulator is not None else "infura")


class _BitcoinChecks:
    def __init__(self, rpc):
        self.rpc = rpc
        self.checked = set()
        self.method = "getblockchaininfo"

    def call(self, method, *params, reject=None):
        self.method = method
        try:
            result = self.rpc.call(method, *params)
        except BitcoinRPCError as error:
            if reject is None or error.rpc_code != reject or error.http_status not in (None, 400, 500):
                raise PreflightError("testnet4", method, "RPC_METHOD_CHECK",
                    rpc_code=error.rpc_code, http_status=error.http_status) from None
            self.checked.add(method)
            return None
        if reject is not None:
            raise PreflightError("testnet4", method, "RPC_EXPECTED_REJECTION")
        self.checked.add(method)
        return result

    def require(self, condition, reason="RPC_RESPONSE"):
        if not condition:
            raise PreflightError("testnet4", self.method, reason)

    def run(self, config, policy):
        try:
            tip = check_core(self, "testnet4", policy)
        except PreflightError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            from .participant_errors import participant_exception_code
            raise PreflightError("testnet4", self.method, participant_exception_code(error)) from None

        height = int(policy["checkpoint_height"])
        checkpoint = header_hash(bytes.fromhex(policy["checkpoint_header"]))[::-1].hex()
        oldest = self.call("getblock", policy["genesis_hash"], 0)
        self.require(isinstance(oldest, str) and len(oldest) > 160
                     and header_hash(bytes.fromhex(oldest[:160]))[::-1].hex() == policy["genesis_hash"])
        block = self.call("getblock", checkpoint, 1)
        self.require(isinstance(block, dict) and block.get("hash") == checkpoint
                     and block.get("height") == height and isinstance(block.get("tx"), list) and bool(block["tx"]))
        txid = block["tx"][0]
        tx = self.call("getrawtransaction", txid, True)
        self.require(isinstance(tx, dict) and tx.get("txid") == txid and tx.get("blockhash") == checkpoint
                     and isinstance(tx.get("hex"), str) and bool(tx["hex"]))
        decoded = self.call("decoderawtransaction", tx["hex"])
        self.require(isinstance(decoded, dict) and decoded.get("txid") == txid)
        unspent = self.call("gettxout", txid, 0, True)
        self.require(unspent is None or isinstance(unspent, dict))
        proof = self.call("gettxoutproof", [txid], checkpoint)
        self.require(isinstance(proof, str) and proof.startswith(policy["checkpoint_header"]))
        self.require(self.call("verifytxoutproof", proof) == [txid])
        self.call("getmempoolentry", txid, reject=-5)
        if config.get("income_model") == "in-kind":
            from .bitcoin_rpc import mempool_fee_rates

            info = self.call("getmempoolinfo")
            try:
                mempool_fee_rates(info)
            except ValueError:
                raise PreflightError("testnet4", self.method, "BTC_MEMPOOL_POLICY_UNAVAILABLE") from None
        self.call("scantxoutset", "status")
        # A malformed descriptor is rejected before Core traverses the UTXO set.
        self.call("scantxoutset", "start", [{"desc": "raw(not-hex)"}], reject=-5)
        self.call("testmempoolaccept", ["00"], reject=-22)
        self.call("sendrawtransaction", "00", reject=-22)
        if int(config["bitcoin_watch_start_height"]):
            self.method = "getblockchaininfo"
            self.require(int(config["bitcoin_watch_start_height"]) <= tip["blocks"] + 1, "ADDRESS_SCAN_START_HEIGHT")
            index = self.call("getindexinfo", "basic block filter index").get("basic block filter index", {})
            self.require(index.get("synced") is True and index.get("best_block_height") == tip["blocks"],
                         "ADDRESS_FILTER_INDEX_REQUIRED")
            page = self.call("scanblocks", "start", ["raw(51)"], height, height, "basic")
            self.require(isinstance(page, dict) and page.get("completed") is True
                         and page.get("from_height") == page.get("to_height") == height
                         and page.get("relevant_blocks") in ([], [checkpoint]), "ADDRESS_FILTER_SCAN_INCOMPLETE")
        self.require(self.call("getblockcount") == tip["blocks"], "MAINNET_CORE_REORG_DURING_CHECK")
        best = self.call("getbestblockhash")
        self.require(best == tip["bestblockhash"], "MAINNET_CORE_REORG_DURING_CHECK")
        return dict(network="testnet4", checkpoint_height=str(height), checkpoint_hash=checkpoint,
                    methods=sorted(self.checked))


def check_rpc(config, policy):
    """Read local connection settings and return only public capability evidence."""
    evm, bitcoin, cookie = (os.environ.get(name) for name in ("RPC", "BTC_RPC", "BTC_COOKIE"))
    if not evm:
        raise PreflightError("sepolia", "configuration", "RPC_REQUIRED")
    if not bitcoin or not cookie:
        raise PreflightError("testnet4", "configuration", "BTC_RPC_AND_COOKIE_REQUIRED")
    if not re.fullmatch(r"https://sepolia\.infura\.io/v3/[0-9a-zA-Z]{1,128}", evm):
        raise PreflightError("sepolia", "configuration", "INFURA_SEPOLIA_HTTPS_REQUIRED")
    alternate = os.environ.get("SATOSHIFI_CREATE_SIMULATION_RPC")
    if alternate:
        try:
            valid = endpoint(alternate).scheme == "https"
        except ValueError:
            valid = False
        if not valid:
            raise PreflightError("sepolia", "eth_simulateV1", "CREATE_SIMULATION_HTTPS_REQUIRED")
    try:
        core = BitcoinRPC(bitcoin, cookie, "testnet4", policy)
    except ValueError as error:
        from .participant_errors import participant_exception_code
        raise PreflightError("testnet4", "configuration", participant_exception_code(error)) from None
    if not core.is_https:
        raise PreflightError("testnet4", "configuration", "BITCOIN_HTTPS_REQUIRED")

    provider = http_provider(evm, timeout=20)
    simulator = http_provider(alternate, timeout=20) if alternate else None
    evm_report = _evm_checks(provider, config, simulator)
    probe = _BitcoinChecks(core)
    try:
        bitcoin_report = probe.run(config, policy)
    except PreflightError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError):
        raise PreflightError("testnet4", probe.method, "RPC_RESPONSE") from None
    return dict(status="LAUNCH_RPC_CHECKED", profile=REMOTE_PROFILE, sends_transactions=False,
                sepolia=evm_report, testnet4=bitcoin_report)
