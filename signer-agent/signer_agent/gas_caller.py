"""Bounded claimFor automation. Run once or poll a reviewed local configuration.

This spends only the caller's native gas. It cannot activate a treasury, pay a
service job, sign FROST, or infer SPV readiness from a claim/payment receipt.
"""
import argparse
import fcntl
import json
import time
from pathlib import Path

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.logs import DISCARD

from .bootstrap import Bootstrap
from .bootstrap_parallel import restore_signed
from .evm_network import read_block, receipt_finalized, transaction_policy, check_transaction_gas
from .gas_caller_budget import CallerBudget, uint
from .gas_treasury import GasTreasuryClient, TREASURY
from .onboarding import read_private
from .operations import Journal


def eligible(balance, claimable, payee, policy, maximum):
    floor, minimum = int(payee["balance_floor_wei"]), int(payee["min_claim_wei"])
    if balance >= floor:
        return "FUNDED"
    if claimable < max(minimum, maximum * policy["min_claim_to_fee_ratio"]):
        return "CLAIM_TOO_SMALL"
    if balance + claimable < floor:
        return "INSUFFICIENT_ENTITLEMENT"
    return "READY"


class ClaimRunner(Bootstrap):
    def build_transaction(self, step, account, nonce):
        self.preflight()
        p = self.caller_policy
        fees = dict(maxFeePerGas=p["max_fee_per_gas_wei"],
                    maxPriorityFeePerGas=p["max_priority_fee_per_gas_wei"])
        network = transaction_policy(self.w3)
        if any(fees[key] > maximum for key, maximum in network.items()):
            raise ValueError("GAS_CALLER_NETWORK_FEE_CAP")
        tx = self.fn(step).build_transaction({"from": account.address, "nonce": nonce,
            "chainId": self.w3.eth.chain_id, "value": 0, "gas": p["gas_limit"], **fees})
        check_transaction_gas(self.w3, tx)
        # A fixed, explicit gas limit bounds execution as well as estimation.
        self.w3.eth.call({key: tx[key] for key in ("from", "to", "value", "data", "gas")}, "latest")
        return tx


class GasCaller:
    def __init__(self, w3, config, identity_file):
        self.w3, self.config = w3, config
        if (config.get("schema") != "satoshifi.spv-gas-caller.v1"
                or set(config) != {"schema", "chain_id", "evm_confirmations", "budget", "routes"}):
            raise ValueError("GAS_CALLER_POLICY")
        uint(config["chain_id"], 1)
        self.confirmations = uint(config["evm_confirmations"], 1, 256)
        if str(w3.eth.chain_id) != config["chain_id"]:
            raise ValueError("CHAIN_MISMATCH")
        self.identity = Path(identity_file).resolve()
        self.account = Account.from_key(json.loads(read_private(self.identity))["private_key"])
        self.budget = CallerBudget(str(self.identity) + ".gas-caller.sqlite", config["budget"],
            config["chain_id"], self.account.address, config["evm_confirmations"])
        self.nonce_lock = self.identity.with_suffix(".evm-nonce.lock")
        self.identity_journal = Journal(str(self.nonce_lock) + ".sqlite")
        self.candidates = []
        seen = set()
        if not isinstance(config["routes"], list) or not 1 <= len(config["routes"]) <= 64:
            raise ValueError("GAS_CALLER_POLICY")
        for route in config["routes"]:
            if set(route) != {"binding", "treasury", "epoch", "roster_hash", "payees"}:
                raise ValueError("GAS_CALLER_POLICY")
            if any(route["binding"].get(k) != config[k] for k in ("chain_id", "evm_confirmations")):
                raise ValueError("GAS_CALLER_POLICY")
            uint(route["epoch"], 1, 2**64 - 1)
            if len(HexBytes(route["roster_hash"])) != 32 or not 1 <= len(route["payees"]) <= 32:
                raise ValueError("GAS_CALLER_POLICY")
            for payee in route["payees"]:
                if set(payee) != {"address", "balance_floor_wei", "min_claim_wei"}:
                    raise ValueError("GAS_CALLER_POLICY")
                uint(payee["balance_floor_wei"], 1)
                uint(payee["min_claim_wei"], 1)
                identity = (Web3.to_checksum_address(route["treasury"]), Web3.to_checksum_address(payee["address"]))
                if identity in seen:
                    raise ValueError("GAS_CALLER_POLICY")
                seen.add(identity)
                self.candidates.append((route, payee))
        if len(self.candidates) > 64:
            raise ValueError("GAS_CALLER_POLICY")

    def inspect(self, route, payee, *, latest=False):
        client = GasTreasuryClient(self.w3, route["binding"])
        block = client._binding()
        treasury = client._treasury(route["treasury"], block)
        if (str(client._call(treasury, "epoch", block)) != route["epoch"]
                or HexBytes(client._call(treasury, "rosterHash", block)) != HexBytes(route["roster_hash"])):
            raise ValueError("GAS_CALLER_ROSTER_BINDING")
        address = Web3.to_checksum_address(payee["address"])
        if not client._call(treasury, "weight", block, address):
            raise ValueError("GAS_CALLER_PAYEE_BINDING")
        # Shared across alternative canonical proposals for the same scope/epoch.
        pair = ":".join((Web3.to_hex(client._call(treasury, "confederationScope", block)),
                         route["epoch"], address.lower()))
        if latest:
            block = self.w3.eth.get_block("latest")
        balance = self.w3.eth.get_balance(address, block_identifier=block.number)
        claimable = client._call(treasury, "claimable", block, address)
        released = client._call(treasury, "released", block, address)
        client._anchor(block)
        return treasury, block, dict(payee=address, treasury=treasury.address, pair=pair,
            balance_wei=str(balance), claimable_wei=str(claimable), released_wei=str(released),
            status=eligible(balance, claimable, payee, self.budget.policy, self.budget.maximum))

    def preflight(self, payload):
        _, _, state = self.inspect(payload["route"], payload["payee"], latest=True)
        if state["status"] != "READY":
            raise ValueError("GAS_CALLER_NEED_CHANGED")
        if self.w3.eth.get_balance(self.account.address, "pending") < (
                self.budget.maximum + self.budget.policy["min_caller_balance_wei"]):
            raise ValueError("GAS_CALLER_RESERVE_SHORTAGE")

    def runner(self, entry):
        payload = entry["request"]["payload"]
        profile = dict(payload["profile"], deployment_id="gas-caller-" + entry["id"].split(":")[1])
        # The operation's original RPC/profile is immutable for signed recovery.
        runner = ClaimRunner(profile, str(self.identity) + ".gas-caller-transactions.sqlite")
        runner.caller_policy = self.budget.policy
        runner.preflight = lambda: self.preflight(payload)
        step = profile["steps"][0]
        key = runner.prefix + ":send"
        runner.journal.put(key, step)
        return runner, key, step

    def signed(self, runner, key, step):
        return restore_signed(self.identity_journal, runner, key, step)

    def settle(self, entry, runner, key, step):
        """Acknowledge only this caller's exact, finalized claim nonce, even on revert.

        The shared journal's CONFIRMED means the nonce is consumed. Preserve the
        failed source operation and explicit receipt_status; never call it paid.
        """
        with self.nonce_lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            signed = self.signed(runner, key, step)
            if not signed:
                return None
            raw = HexBytes(signed["raw_transaction"])
            if (Web3.keccak(raw).hex() != signed["tx_hash"]
                    or Account.recover_transaction(raw) != self.account.address):
                raise ValueError("GAS_CALLER_TRANSACTION_BINDING")
            tx = TypedTransaction.from_bytes(raw).as_dict()
            p = self.budget.policy
            expected = runner.fn(step)
            if (int(tx["type"]) != 2 or tx["chainId"] != self.w3.eth.chain_id
                    or bytes(tx["to"]) != bytes.fromhex(expected.address[2:])
                    or bytes(tx["data"]) != HexBytes(expected._encode_transaction_data())
                    or tx["value"] != 0 or tx["gas"] != p["gas_limit"]
                    or tx["maxFeePerGas"] != p["max_fee_per_gas_wei"]
                    or tx["maxPriorityFeePerGas"] != p["max_priority_fee_per_gas_wei"]
                    or str(tx["nonce"]) != str(signed["nonce"])):
                raise ValueError("GAS_CALLER_TRANSACTION_BINDING")
            try:
                receipt = self.w3.eth.get_transaction_receipt(signed["tx_hash"])
            except TransactionNotFound:
                if entry["result"].get("block_hash"):
                    raise ValueError("GAS_CALLER_REORG_RECOVERY_REQUIRED")
                return dict(status="PENDING", transaction_hash=Web3.to_hex(Web3.keccak(raw)))
            if (receipt.transactionHash != Web3.keccak(raw)
                    or receipt.status not in (0, 1) or not 0 < receipt.gasUsed <= tx["gas"]
                    or not 0 <= receipt.effectiveGasPrice <= tx["maxFeePerGas"]):
                raise ValueError("GAS_CALLER_RECEIPT_BINDING")
            if (entry["result"].get("block_hash")
                    and entry["result"]["block_hash"] != receipt.blockHash.hex()):
                raise ValueError("GAS_CALLER_REORG_RECOVERY_REQUIRED")
            anchor = dict(block_hash=receipt.blockHash.hex(), block_number=str(receipt.blockNumber))
            self.budget.journal.update(entry["id"], "MINED", anchor)
            if not receipt_finalized(self.w3, receipt.blockNumber, receipt.blockHash, self.confirmations):
                return dict(status="CONFIRMING", **anchor)
            payload = entry["request"]["payload"]
            treasury, block, state = self.inspect(payload["route"], payload["payee"])
            self.budget.observe(self.w3, block)
            cost = receipt.gasUsed * receipt.effectiveGasPrice
            period = self.w3.eth.get_block(receipt.blockNumber).timestamp // p["period_seconds"]
            claimed = sum(event["args"]["amount"] for event in
                treasury.events.NativeClaimed().process_receipt(receipt, errors=DISCARD)
                if event["address"] == treasury.address and event["args"]["payee"] == state["payee"]
                and event["args"]["caller"] == self.account.address)
            result = dict(status=("CLAIM_CONFIRMED" if claimed else "CLAIM_NO_PAYOUT")
                          if receipt.status else "CLAIM_REVERTED", claimed_wei=str(claimed),
                receipt_status=str(receipt.status), transaction_hash=Web3.to_hex(receipt.transactionHash),
                cost_wei=str(cost), cost_period=str(period), gas_used=str(receipt.gasUsed),
                effective_gas_price=str(receipt.effectiveGasPrice), recipient=state, **anchor)
            # Keep the worst-case reservation until both nonce/source journals
            # acknowledge the canonical receipt. Every crash boundary is replayable.
            self.budget.journal.update(entry["id"], "SETTLING", result)
            runner.journal.update(key, "CONFIRMED" if receipt.status else "REVERTED", dict(signed, **anchor))
            self.identity_journal.update(signed["tx_hash"], "CONFIRMED",
                dict(anchor, receipt_status=str(receipt.status), outcome=result["status"]))
            self.budget.finish(entry, result)
            return result

    def advance(self, entry):
        runner, key, step = self.runner(entry)
        settled = self.settle(entry, runner, key, step)
        if settled and settled["status"] != "PENDING":
            return settled
        error = None
        try:
            runner.apply(self.identity)
        except Exception as exc:
            error = exc
        result = self.settle(self.budget.journal.get(entry["id"]), runner, key, step)
        if result:
            return result
        # No raw signature exists in the shared journal: release money, retain
        # the attempt/backoff. Unknown signed outcomes never reach this branch.
        with self.nonce_lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.signed(runner, key, step):
                raise ValueError("GAS_CALLER_RECOVERY_REQUIRED")
            from .relay_keeper import error_code
            reason = (str(error) if type(error) is ValueError and str(error) in ERRORS
                      else error_code(error) if error is not None else "NO_TRANSACTION")
            result = dict(status="UNSENT", reason=reason, cost_wei="0",
                          cost_period=entry["request"]["period"])
            self.budget.finish(entry, result)
        return result

    def tick(self):
        with Path(str(self.budget.journal.path) + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            block = read_block(self.w3, self.confirmations)
            self.budget.observe(self.w3, block)
            pending = self.budget.pending()
            if pending:
                return self.advance(pending)
            observations = []
            previous = self.budget.last()
            last = previous["request"]["payload"].get("candidate") if previous else None
            candidates = list(range(len(self.candidates)))
            if last in candidates:
                candidates = candidates[last + 1:] + candidates[:last + 1]
            for index in candidates:
                route, payee = self.candidates[index]
                treasury, observed, state = self.inspect(route, payee)
                self.budget.observe(self.w3, observed)
                observations.append(state)
                if state["status"] != "READY":
                    continue
                limit = self.budget.limits(state["pair"], observed.timestamp)
                if limit:
                    state["status"] = limit
                    continue
                if self.w3.eth.get_balance(self.account.address, observed.number) < (
                        self.budget.maximum + self.budget.policy["min_caller_balance_wei"]):
                    state["status"] = "CALLER_RESERVE_SHORTAGE"
                    continue
                step = dict(id="send", kind="call", contract="target", method="claimFor",
                            args=[state["payee"]], value="0")
                mainnet = self.w3.eth.chain_id == 1
                profile = dict(mode="backed" if mainnet else "integration",
                    bitcoin_network="mainnet" if mainnet else "regtest", chain_id=self.config["chain_id"],
                    evm_confirmations=self.config["evm_confirmations"], evm_rpc=self.w3.provider.endpoint_uri,
                    contracts={"target": dict(address=treasury.address, artifact=TREASURY,
                        runtime_hash=Web3.keccak(self.w3.eth.get_code(treasury.address, observed.number)).hex())},
                    steps=[step])
                entry = self.budget.reserve(state["pair"], observed.timestamp,
                    dict(route=route, payee=payee, candidate=index, profile=profile))
                return self.advance(entry)
            return dict(status="IDLE", recipients=observations)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        if not 5 <= args.interval_seconds <= 3600:
            raise ValueError("GAS_CALLER_POLICY")
        caller = GasCaller(Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20})),
            json.loads(Path(args.config).read_text()), args.identity_file)
        while True:
            print(json.dumps(caller.tick(), sort_keys=True), flush=True)
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    except Exception as exc:
        from .relay_keeper import error_code
        # Fixed error codes only; never echo RPC payloads or a local secret.
        reason = str(exc) if type(exc) is ValueError and str(exc) in ERRORS else error_code(exc)
        print(json.dumps(dict(status="BLOCKED", reason=reason)), flush=True)
        return 1


ERRORS = frozenset("""GAS_CALLER_POLICY GAS_CALLER_REORG_RECOVERY_REQUIRED
GAS_CALLER_RECOVERY_REQUIRED GAS_CALLER_TRANSACTION_BINDING GAS_CALLER_RECEIPT_BINDING
GAS_CALLER_ROSTER_BINDING GAS_CALLER_PAYEE_BINDING GAS_CALLER_PENDING GAS_CALLER_BUDGET
GAS_CALLER_NETWORK_FEE_CAP GAS_CALLER_NEED_CHANGED GAS_CALLER_RESERVE_SHORTAGE""".split())

if __name__ == "__main__":
    raise SystemExit(main())
