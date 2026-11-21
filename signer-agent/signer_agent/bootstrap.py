"""Versioned, resumable deployment execution; signed EVM transactions are journaled before broadcast.
Profiles are operator-owned local files, never accepted from HTTP. Secrets live in a separate
0600 EVM identity file. FROST operations wait for external participants, never auto-approve.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound, ContractLogicError
from .operations import Journal, canonical, public_data
from .deployment_limits import check_deployment
from .safe_errors import DiagnosticArgumentParser, emit_debug

ROOT = Path(__file__).resolve().parents[2]


def artifact(name):
    if not name.replace("_", "").isalnum():
        raise ValueError("ARTIFACT_NAME")
    # The pinned verifier/state build has separate remappings and metadata.
    # Never resolve a test guest/state or an arbitrary external artifact path.
    folder = ("rust/zkspv/state-adapter/out" if name in (
        "ZkBitcoinStateV2", "ZkSpvAdapter", "RiscZeroGroth16Verifier",
        "ZkBitcoinTestnet4StateV2", "ZkTestnet4SpvAdapter", "ZkTestnet4SPVServiceStateView"
    ) else "web3/contracts/out")
    paths = list((ROOT / folder).glob(f"*/{name}.json"))
    if len(paths) != 1:
        raise ValueError("ARTIFACT_NOT_UNIQUE")
    return json.loads(paths[0].read_text())


def artifact_fingerprint(compiled, *, legacy=False):
    """Pin executable artifact data, with whole-file hashing for old journals."""
    if legacy:
        return hashlib.sha256(canonical(compiled).encode()).hexdigest()
    executable = {"abi": compiled["abi"]}
    for kind in ("bytecode", "deployedBytecode"):
        code = compiled[kind]
        executable[kind] = {
            "object": code["object"],
            "linkReferences": code.get("linkReferences", {}),
            "immutableReferences": code.get("immutableReferences", {}),
        }
    return "evm-v1:" + hashlib.sha256(canonical(executable).encode()).hexdigest()


def encode_args(inputs, values):
    import re
    from eth_abi import encode
    from eth_utils.abi import collapse_if_tuple

    if not isinstance(values, (list, tuple)) or len(inputs) != len(values):
        raise ValueError("ARGUMENT_COUNT")

    def convert(spec, value):
        t = spec["type"]
        array = re.fullmatch(r"(.+)\[([0-9]*)\]", t)
        if array:
            if not isinstance(value, (list, tuple)) or (
                array[2] and len(value) != int(array[2])
            ):
                raise ValueError("ABI_ARRAY_LENGTH")
            return [convert(dict(spec, type=array[1]), v) for v in value]
        if t.startswith("uint") or t.startswith("int"):
            if not isinstance(value, str):
                raise ValueError("INTEGER_ARGUMENT_STRING_REQUIRED")
            if not re.fullmatch(r"-?(0|[1-9][0-9]*)", value):
                raise ValueError("ABI_INTEGER_FORMAT")
            return int(value)
        if t == "tuple":
            return encode_args(spec["components"], value)
        if t == "address":
            return Web3.to_checksum_address(value)
        if t.startswith("bytes"):
            if not isinstance(value, str):
                raise ValueError("ABI_BYTES_REQUIRED")
            data = bytes.fromhex(value.removeprefix("0x"))
            if t != "bytes" and len(data) != int(t[5:]):
                raise ValueError("ABI_BYTES_LENGTH")
            return data
        return value

    try:
        result = [convert(s, v) for s, v in zip(inputs, values)]
        encode([collapse_if_tuple(s) for s in inputs], result)
        return result
    except ValueError:
        raise
    except Exception:
        raise ValueError("ABI_ARGUMENT_INVALID") from None


class Bootstrap:
    def __init__(self, profile, state, bitcoin=None, *, evm_confirmation_floor=None, evm_rpc_override=None):
        self.profile = public_data(profile)
        self.bitcoin = bitcoin
        if profile.get("mode") not in ("mock", "integration", "backed"):
            raise ValueError("EXPLICIT_MODE_REQUIRED")
        public_bitcoin = profile.get("bitcoin_network") in ("mainnet", "testnet4")
        needs_backing = int(profile["chain_id"]) == 1 or public_bitcoin
        if (
            int(profile["chain_id"]) not in (1, 31337, 11155111)
            or (int(profile["chain_id"]) == 1 and int(profile.get("evm_confirmations", "0")) < 12)
            or profile.get("bitcoin_network") not in ("mainnet", "testnet4", "regtest")
            or (needs_backing and profile["mode"] != "backed")
            or (not needs_backing and profile["mode"] == "backed")
            or (not public_bitcoin and "bitcoin_policy" in profile)
        ):
            raise ValueError("INTEGRATION_NETWORK_NOT_ALLOWED")
        self.w3 = Web3(
            Web3.HTTPProvider(evm_rpc_override or profile["evm_rpc"], request_kwargs={"timeout": 20})
        )
        self.journal = Journal(state)
        self.contracts = dict(profile.get("contracts", {}))
        self.values = {}
        self.prefix = profile["deployment_id"]
        request = dict(profile=profile, version="1")
        prior, changed_rpc = None, False
        if evm_confirmation_floor is not None:
            if type(evm_confirmation_floor) is not int or not 1 <= evm_confirmation_floor <= 256:
                raise ValueError("EVM_CONFIRMATIONS_POLICY")
            self._evm_confirmation_floor = evm_confirmation_floor
        if evm_confirmation_floor is not None or evm_rpc_override is not None:
            try:
                prior = self.journal.get(self.prefix)["request"]
            except KeyError:
                prior = None
            if prior is not None and prior.get("version") == "1":
                saved = prior["profile"]
                # Signing retries use the same operation and nonce journal when
                # local receipt depth or transport changes.
                allowed = set()
                if evm_confirmation_floor is not None:
                    allowed.add("evm_confirmations")
                if evm_rpc_override is not None:
                    allowed.add("evm_rpc")
                if ({k: v for k, v in saved.items() if k not in allowed}
                        == {k: v for k, v in profile.items() if k not in allowed}):
                    self._evm_confirmation_floor = max(evm_confirmation_floor or 1, int(saved.get("evm_confirmations", "1")))
                    request = prior
                    changed_rpc = evm_rpc_override is not None and evm_rpc_override != saved["evm_rpc"]
        if self.w3.eth.chain_id != int(profile["chain_id"]):
            raise ValueError("CHAIN_MISMATCH")
        if changed_rpc:
            from .bootstrap_parallel import verify_source_anchors
            verify_source_anchors(self.w3, self.journal, self.prefix, allow_recovery=True)
        self.journal.put(self.prefix, request)
        if self.w3.eth.chain_id == 1 or self.evm_confirmations != 1:
            from .evm_network import read_block
            read_block(self.w3, self.evm_confirmations)
        self.check_external_components()

    @property
    def evm_confirmations(self):
        return max(int(self.profile.get("evm_confirmations", "1")), getattr(self, "_evm_confirmation_floor", 1))

    def check_external_components(self):
        """Fail before any bootstrap transaction if an enrolled dependency changed."""
        if "ulr" in self.profile and self.profile["ulr"]["mode"] == "reuse":
            from .bootstrap_ulr import inspect_roots
            inspect_roots(self.w3, self.profile,
                {name: spec["address"] for name, spec in self.profile["ulr"]["roots"].items()}, self.read_number())
        fee=self.profile.get('withdrawal_fee',{})
        if fee.get('version')=='2' and fee['family_root']['version']!='create-1':
            from .family_registry import inspect
            inspect(self.w3,fee['family_root'],self.read_number())
        if self.profile["mode"] != "backed":
            return
        for name, policy_field, forbidden in (
            ("usd", "reward_token", "DAOTestUSD"),
            ("feed", "btc_usd_feed", "DAOMockFeed"),
        ):
            if policy_field not in self.profile:
                continue  # Small transaction-only profiles do not construct the DAO.
            expected = self.profile[policy_field]
            component = self.contracts.get(name)
            if not component or component["address"] != expected["address"]:
                raise ValueError("EXTERNAL_COMPONENT_BINDING")
            code = bytes(self.w3.eth.get_code(expected["address"], block_identifier=self.read_number()))
            if not code or "0x" + Web3.keccak(code).hex().removeprefix("0x") != expected["runtime_hash"]:
                raise ValueError("EXTERNAL_COMPONENT_CODE_HASH")
            fixture_code = bytes.fromhex(artifact(forbidden)["deployedBytecode"]["object"].removeprefix("0x"))
            if self.w3.eth.chain_id == 1 and code == fixture_code:
                raise ValueError("MAINNET_TEST_COMPONENT_FORBIDDEN")
            wanted_decimals = int(expected["decimals"]) if name == "usd" else 8
            if self.read(self.contract(name).functions.decimals()) != wanted_decimals:
                raise ValueError("EXTERNAL_COMPONENT_DECIMALS")

    def bitcoin_ready(self, identity, *, readonly=False):
        from .bitcoin_network import inspect_relay, sync_relay

        if self.bitcoin is None:
            raise ValueError("MAINNET_CORE_REQUIRED")
        policy = self.profile["bitcoin_policy"]
        from .zk_spv import is_zk, inspect_binding, inspect_live
        if is_zk(policy):
            from .evm_network import read_block
            block = read_block(self.w3, self.evm_confirmations)
            state, _, _, _ = inspect_binding(self.w3, self.contract("relay").address, policy,
                block.number, adapter_address=self.contract("spv_guard").address)
            result = inspect_live(self.bitcoin, state, policy, block_identifier=block.number)
            if self.w3.eth.get_block(block.number).hash != block.hash:
                raise ValueError("BTC_REGISTRY_READ_REORG")
            return result
        relay = self.contract("relay")
        confirmations = self.evm_confirmations

        def checked_ready():
            from .evm_network import read_block
            number = read_block(self.w3, confirmations).number
            if number < 0:
                raise ValueError("MAINNET_EVM_CONFIRMATIONS")
            block = self.w3.eth.get_block(number)
            result = inspect_relay(self.bitcoin, relay, policy, block_identifier=number)
            if not self.contract("spv_guard").functions.isFresh().call(block_identifier=number):
                raise ValueError("MAINNET_RELAY_STALE")
            if self.w3.eth.get_block(number).hash != block.hash:
                raise ValueError("BTC_REGISTRY_READ_REORG")
            return result

        if readonly:
            return checked_ready()

        from .relay_batch import DurableRelayBatch
        if not identity:
            raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
        durable_send = DurableRelayBatch.for_rpc(identity, self.w3, relay.address, policy, confirmations)
        result = sync_relay(self.bitcoin, relay, policy, lambda raw: durable_send([raw]),
                            send_headers=durable_send, evm_confirmations=confirmations)
        if result["status"] == "MAINNET_RELAY_READY":
            try:
                checked_ready()
            except ValueError as exc:
                if str(exc) != "MAINNET_RELAY_STALE":
                    raise
                return dict(result, status="IN_PROGRESS", reason="SPV_GUARD_NOT_FRESH")
        return result

    def bitcoin_binding(self):
        """Structural ZK bootstrap only; no freshness or monetary readiness claim."""
        from .zk_spv import is_zk, inspect_binding
        from .evm_network import read_block
        from .bitcoin_network import check_core
        policy = self.profile.get("bitcoin_policy")
        if self.profile.get("bitcoin_network") not in ("mainnet","testnet4") or not is_zk(policy) or self.bitcoin is None:
            raise ValueError("BOOTSTRAP_ZK_BINDING_PROFILE")
        block = read_block(self.w3, self.evm_confirmations)
        state, adapter, _, _ = inspect_binding(self.w3, self.contract("relay").address, policy,
            block.number, adapter_address=self.contract("spv_guard").address)
        check_core(self.bitcoin, self.profile['bitcoin_network'], policy)
        if self.w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("BTC_REGISTRY_READ_REORG")
        return dict(status="ZK_BINDING_VERIFIED_FRESHNESS_DEFERRED", state=state.address, adapter=adapter.address,
            block_number=str(block.number), block_hash=block.hash.hex(), ready=False)

    def verify_token_funding(self, step, result, receipt):
        transfer = step.get("verify_token_transfer")
        if not transfer:
            return
        balance = self.contract(transfer["token"]).functions.balanceOf(
            self.resolve(transfer["recipient"])
        ).call(block_identifier=receipt.blockNumber)
        if balance != int(result["transfer_balance_before"]) + int(transfer["amount"]):
            raise ValueError("INEXACT_BOOTSTRAP_FUNDING")

    def resolve(self, x):
        if isinstance(x, str) and x.startswith("$"):
            key = x[1:]
            if key in self.contracts:
                return self.contracts[key]["address"]
            return self.values[key]
        if isinstance(x, list):
            return [self.resolve(v) for v in x]
        if isinstance(x, dict):
            if set(x) == {"$json_hex"}:
                return "0x" + canonical(self.resolve(x["$json_hex"])).encode().hex()
            return {k: self.resolve(v) for k, v in x.items()}
        return x

    def contract(self, name):
        item = self.contracts[name]
        return self.w3.eth.contract(
            address=item["address"], abi=artifact(item["artifact"])["abi"]
        )

    def fn(self, spec):
        c = self.contract(spec["contract"])
        name = spec["method"]
        entries = [
            x for x in c.abi if x.get("type") == "function" and x.get("name") == name
        ]
        if len(entries) != 1:
            raise ValueError("AMBIGUOUS_FUNCTION")
        args = encode_args(entries[0]["inputs"], self.resolve(spec.get("args", [])))
        return getattr(c.functions, name)(*args)

    @staticmethod
    def wire(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return str(value)
        if isinstance(value, bytes):
            return "0x" + value.hex()
        if isinstance(value, (list, tuple)):
            return [Bootstrap.wire(x) for x in value]
        return value

    def read_number(self):
        pinned = getattr(self, "_read_snapshot", None)
        if pinned is not None:
            return pinned.number
        if self.w3.eth.chain_id == 1 or self.evm_confirmations != 1:
            from .evm_network import read_block
            return read_block(self.w3, self.evm_confirmations)["number"]
        return "latest"

    def read(self, fn):
        pinned = getattr(self, "_read_snapshot", None)
        if pinned is None and self.w3.eth.chain_id != 1 and self.evm_confirmations == 1:
            return fn.call(block_identifier="latest")
        from .evm_network import read_block
        block = pinned or read_block(self.w3, self.evm_confirmations)
        value = fn.call(block_identifier=block.number)
        if self.w3.eth.get_block(block.number).hash != block.hash:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        return value

    def observe_read_step(self, step, result):
        """Evaluate a wait/read and its outputs at one enrolled-depth snapshot."""
        from .evm_network import read_block
        block = read_block(self.w3, self.evm_confirmations)
        previous = getattr(self, "_read_snapshot", None)
        self._read_snapshot = block
        try:
            failure = None
            if step["kind"] == "wait":
                if step["method"] == "getCompletedResult":
                    info = self.contract(step["contract"]).functions.getSessionInfo(
                        *self.resolve(step["args"])).call(block_identifier=block.number)
                    if info[4] in (5, 6):
                        failure = "CEREMONY_TERMINAL_FAILURE"
                    elif info[4] != 4 and block.timestamp > info[5]:
                        failure = "CEREMONY_TIMEOUT"
                elif step["method"] == "getAuthorizationResult":
                    coordinator = self.contract(step["contract"])
                    session_id, = [int(value) for value in self.resolve(step["args"])]
                    session = coordinator.functions.getSession(session_id).call(block_identifier=block.number)
                    if session[10] == 3:
                        failure = "SIGNING_TERMINAL_FAILURE"
                    elif (session[10] != 2 and block.timestamp > session[7]
                            and coordinator.functions.getNonceSetStatus(session_id).call(
                                block_identifier=block.number)[4] < session[5]):
                        failure = "SIGNING_DEADLINE_EXPIRED"
                try:
                    value = self.read(self.fn(step)) if failure is None else None
                except ContractLogicError:
                    value = None
                for index in step.get("index", []):
                    if value is not None:
                        value = value[int(index)]
                ready = failure is None and self.wire(value) == self.resolve(step["equals"])
            else:
                ready = True
            if ready:
                self.outputs(step, result)
                self.verify_checks(step)
            if self.w3.eth.get_block(block.number).hash != block.hash:
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
            if ready:
                result["read_anchor"] = dict(number=str(block.number), hash=block.hash.hex())
            return ready, failure
        finally:
            self._read_snapshot = previous

    def verify_read_step(self, step, result, key):
        """Keep historical observations valid without reusing an orphaned view.

        One-time approvals may legitimately change after execution. A saved
        canonical anchor proves what was observed then, rather than requiring
        that condition to remain true forever. Legacy unanchored observations
        need one conservative revalidation before they may open the barrier.
        """
        from .evm_network import read_block, verify_anchor
        from .transaction_wait import observation_pending
        anchor = result.get("read_anchor")
        if anchor is not None:
            verify_anchor(self.w3, int(anchor["number"]), anchor["hash"],
                          safe=read_block(self.w3, self.evm_confirmations))
            return
        probe = dict(result)
        probe.pop("outputs", None)
        old_values, old_contracts = dict(self.values), dict(self.contracts)
        try:
            ready, failure = self.observe_read_step(step, probe)
            if not ready or failure or probe.get("outputs", {}) != result.get("outputs", {}):
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        except Exception as error:
            self.values, self.contracts = old_values, old_contracts
            if not observation_pending(error):
                self.journal.update(key, "RECOVERY_REQUIRED", result)
            raise
        result.update(probe)
        self.journal.update(key, "CONFIRMED", result)

    def outputs(self, step, result):
        if step["kind"] == "deploy":
            item = result["contract"]
            from .transaction_wait import retry_observation
            def check_runtime():
                if item.get("runtime_hash") != Web3.keccak(self.w3.eth.get_code(item["address"])).hex():
                    raise ValueError("CODE_HASH_MISMATCH")
            retry_observation(check_runtime)
            self.contracts[step["id"]] = item
        if "outputs" in result:
            for name, item in result["outputs"].items():
                if isinstance(item, dict) and "artifact" in item:
                    self.contracts[name] = item
                else:
                    self.values[name] = item
            return
        saved = {}

        for out in step.get("outputs", []):
            if out.get("event"):
                receipt = self.w3.eth.get_transaction_receipt(result["tx_hash"])
                events = getattr(
                    self.contract(out["contract"]).events, out["event"]
                )().process_receipt(receipt)
                if len(events) != 1:
                    raise ValueError("EXPECTED_SINGLE_EVENT")
                value = events[0]["args"][out["field"]]
            else:
                value = self.read(self.fn(out))
            for index in out.get("index", []):
                value = value[int(index)]
            if out.get("artifact"):
                self.contracts[out["name"]] = dict(
                    address=Web3.to_checksum_address(value),
                    artifact=out["artifact"],
                    artifact_hash=artifact_fingerprint(artifact(out["artifact"])),
                    runtime_hash=Web3.keccak(self.w3.eth.get_code(value)).hex(),
                )
                saved[out["name"]] = self.contracts[out["name"]]
            else:
                self.values[out["name"]] = self.wire(value)
                saved[out["name"]] = self.values[out["name"]]
        result["outputs"] = saved

    def manifest(self):
        contracts = {}
        for name, item in self.contracts.items():
            pin = item.get("artifact_hash")
            if pin and pin != artifact_fingerprint(artifact(item["artifact"]), legacy=not pin.startswith("evm-v1:")):
                raise ValueError("ARTIFACT_HASH_MISMATCH")
            code = self.w3.eth.get_code(item["address"])
            if not code:
                raise ValueError("CONTRACT_CODE_MISSING")
            runtime_hash = Web3.keccak(code).hex()
            if item.get("runtime_hash", runtime_hash).removeprefix(
                "0x"
            ) != runtime_hash.removeprefix("0x"):
                raise ValueError("CODE_HASH_MISMATCH")
            contracts[name] = dict(item, runtime_hash=runtime_hash)
        step_keys = {self.prefix + ":" + step["id"] for step in self.profile["steps"]}
        steps = [
            dict(row, result={key: value for key, value in row["result"].items() if key != "raw_transaction"})
            for row in self.journal.list() if row["id"] in step_keys
        ]
        result = dict(
            version="1",
            deployment_id=self.prefix,
            chain_id=str(self.w3.eth.chain_id),
            mode=self.profile["mode"],
            bitcoin_network=self.profile["bitcoin_network"],
            contracts=contracts,
            values=self.values,
            steps=steps,
        )
        if "withdrawal_fee" in self.profile:
            from .bootstrap_withdrawal_fee import manifest
            result["withdrawal_fee"] = manifest(self)
        if "ulr" in self.profile:
            from .bootstrap_ulr import manifest
            result["ulr"] = manifest(self)
        if "system_bootstrap" in self.profile:
            from .bootstrap_system import manifest
            result["system"] = manifest(self, result["ulr"], result["withdrawal_fee"])
        return result

    def broadcast(self, raw):
        encoded = bytes.fromhex(raw)
        expected = Web3.keccak(encoded)
        try:
            submitted = self.w3.eth.send_raw_transaction(encoded)
        except Exception as exc:
            if not any(
                code in str(exc).lower()
                for code in ("nonce too low", "already imported", "already known", "known transaction")
            ):
                raise
            # A duplicate or a mining race is safe only for these exact bytes.
            try:
                submitted = self.w3.eth.get_transaction(expected.hex())["hash"]
            except TransactionNotFound:
                raise exc
        if HexBytes(submitted) != expected:
            raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")
        return expected

    def verify_receipt(self, result, *, receipt=None):
        from .bootstrap_recovery import check_receipt_binding
        from .evm_network import verify_anchor

        if receipt is None:
            try:
                receipt = self.w3.eth.get_transaction_receipt(result["tx_hash"])
            except TransactionNotFound:
                if "block_number" in result and result.get("block_hash"):
                    verify_anchor(self.w3, int(result["block_number"]), result["block_hash"])
                raise
        check_receipt_binding(receipt, result)
        verify_anchor(self.w3, receipt.blockNumber, receipt.blockHash)
        if receipt.status != 1:
            raise ValueError("TRANSACTION_REVERTED")
        if self.w3.eth.block_number - receipt.blockNumber + 1 < self.evm_confirmations:
            return None
        if self.w3.eth.chain_id == 1:
            from .evm_network import receipt_finalized
            if not receipt_finalized(self.w3, receipt.blockNumber, receipt.blockHash, self.evm_confirmations):
                return None
        return receipt

    def observe_receipt(self, result):
        """Observe a known transaction without treating RPC availability as a fork."""
        from .transaction_wait import observation_pending

        try:
            return self.verify_receipt(result)
        except Exception as error:
            if not observation_pending(error):
                raise
            return None

    def verify_checks(self, step, receipt=None):
        """Recheck persistent bindings now and transient effects at their receipt.

        An exact token allowance is consumed by a later deployment. Its original
        postcondition remains verifiable at the canonical confirmed receipt
        block, including after process restarts; it is not a perpetual binding.
        """
        if not step.get("checks"):
            return
        from .transaction_wait import retry_observation
        retry_observation(lambda: self._verify_checks(step, receipt))

    def _verify_checks(self, step, receipt):
        historical = [check for check in step.get("checks", []) if check.get("at_receipt")]
        if any(type(check.get("at_receipt", False)) is not bool for check in step.get("checks", [])):
            raise ValueError("BINDING_CHECK_SNAPSHOT_POLICY")
        if historical:
            if receipt is None:
                raise ValueError("BINDING_CHECK_RECEIPT_REQUIRED")
            if self.w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash:
                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
        previous = getattr(self, "_read_snapshot", None)
        try:
            if (receipt is not None and previous is None and len(historical) < len(step["checks"])
                    and self.w3.eth.chain_id == 11155111 and self.evm_confirmations == 1):
                from .evm_network import read_block
                block = read_block(self.w3, self.evm_confirmations)
                if block.number < receipt.blockNumber:
                    raise ValueError("EVM_RECEIPT_HEAD_BEHIND")
                self._read_snapshot = block
            for check in step["checks"]:
                fn = self.fn(check)
                value = (fn.call(block_identifier=receipt.blockNumber) if check.get("at_receipt") else self.read(fn))
                if self.wire(value) != self.resolve(check["equals"]):
                    raise ValueError("BINDING_CHECK_FAILED")
        finally:
            self._read_snapshot = previous
        if receipt is not None and self.w3.eth.get_block(receipt.blockNumber).hash != receipt.blockHash:
            raise ValueError("EVM_REORG_RECOVERY_REQUIRED")

    def build_transaction(self, step, account, nonce, *, simulation_gas=None):
        if step["kind"] == "deploy":
            a = artifact(step["artifact"])
            if step["artifact"] in ("ZkBitcoinStateV2", "ZkBitcoinTestnet4StateV2"):
                from .zk_spv import require_patched_build
                require_patched_build(a)
            constructor = next((entry for entry in a["abi"] if entry["type"] == "constructor"), {"inputs": []})
            function = self.w3.eth.contract(abi=a["abi"], bytecode=a["bytecode"]["object"]).constructor(
                *encode_args(constructor["inputs"], self.resolve(step.get("args", []))))
            check_deployment(a["deployedBytecode"]["object"], function.data_in_transaction)
        elif step["kind"] == "call":
            function = self.fn(step)
        else:
            raise ValueError("STEP_KIND")
        from .evm_network import transaction_policy, transaction_gas_with_margin
        chain_id = self.w3.eth.chain_id
        if int(self.profile.get("chain_id", chain_id)) != chain_id:
            raise ValueError("CHAIN_MISMATCH")
        transaction = dict(from_=account.address, nonce=nonce, chainId=chain_id,
                           value=int(step.get("value", "0")), **transaction_policy(self.w3))
        transaction["from"] = transaction.pop("from_")
        if simulation_gas is not None:
            transaction["gas"] = simulation_gas
            return function.build_transaction(transaction)
        if step.get("create_group"):
            transaction["gas"] = int(step["gas"])
        elif self.w3.eth.chain_id == 1:
            transaction["gas"] = function.estimate_gas(transaction, block_identifier="pending")
        return transaction_gas_with_margin(self.w3, function.build_transaction(transaction))

    def record_deployment(self, step, result, receipt):
        if step["kind"] != "deploy" or "contract" in result:
            return
        from .transaction_wait import retry_observation
        retry_observation(lambda: self._record_deployment(step, result, receipt))

    def _record_deployment(self, step, result, receipt):
        if step.get("create_group"):
            from .bootstrap_create import create_address
            sender = Account.recover_transaction(bytes.fromhex(result["raw_transaction"]))
            if receipt.contractAddress != create_address(sender, int(result["nonce"])):
                raise ValueError("BOOTSTRAP_CREATE_ADDRESS")
        a = artifact(step["artifact"])
        code = self.w3.eth.get_code(receipt.contractAddress, block_identifier=receipt.blockNumber)
        transaction = self.w3.eth.get_transaction(result["tx_hash"])
        check_deployment(code, transaction["input"])
        from .bitcoin_trust import checked_runtime
        checked_runtime(self.w3, receipt.contractAddress, step["artifact"], receipt.blockNumber)
        result["contract"] = dict(address=receipt.contractAddress, artifact=step["artifact"],
            artifact_hash=artifact_fingerprint(a),
            runtime_hash=Web3.keccak(code).hex())

    def apply_confirmed(self, identity, **options):
        from .transaction_wait import apply_confirmed
        return apply_confirmed(self, identity, **options)

    def apply(self, identity=None, dry_run=False, verify=False, *, before_submit=None, defer_zk_freshness=False,
              pause_for_funding=False, funding_complete=False, funding_min_wei=None, funding_operator_min_wei=None,
              ulr_distribution_complete=False, accept_ulr_distribution=None):
        if defer_zk_freshness:
            from .zk_spv import is_zk
            if self.profile.get('bitcoin_network') not in ('mainnet','testnet4') or not is_zk(self.profile.get('bitcoin_policy')):
                raise ValueError('BOOTSTRAP_ZK_BINDING_PROFILE')
        from .bootstrap_parallel import parallel_groups, apply_group, recover_identity, restore_signed, save_signed
        from .bootstrap_create import creation_groups, apply_group as apply_creation
        from .bootstrap_recovery import (ReplacementAwaitingFinality, check_receipt_binding,
                                         require_resume_lane, signed_nonce)
        groups = parallel_groups(self.profile["steps"])
        creates = creation_groups(self.profile["steps"])
        self.check_external_components()
        account = None
        if identity:
            from .onboarding import read_private

            account = Account.from_key(
                json.loads(read_private(identity))["private_key"]
            )
        lock = Path(str(self.journal.path) + ".lock")
        with lock.open("a") as locked:
            fcntl.flock(locked, fcntl.LOCK_EX)
            stopped = self.recovery_status()
            if stopped is not None:
                return stopped
            if account is not None and "withdrawal-fee-constructors" in creates and not (dry_run or verify):
                require_resume_lane(self, identity, account)
            from .bootstrap_distribution import validate_release, checkpoint as distribution_checkpoint
            validate_release(self, account, ulr_distribution_complete, accept_ulr_distribution, dry_run or verify)
            from .bootstrap_funding import checkpoint
            funding = checkpoint(self, account, pause=pause_for_funding, release=funding_complete,
                participant_minimum=funding_min_wei, operator_minimum=funding_operator_min_wei,
                readonly=dry_run or verify)
            if funding is not None:
                return funding
            handled_groups = set()
            for step in self.profile["steps"]:
                if step.get("create_group"):
                    group = step["create_group"]
                    if group in handled_groups:
                        continue
                    try:
                        pending = apply_creation(self, creates[group], account, identity, dry_run=dry_run, verify=verify,
                            before_submit=before_submit)
                    except ReplacementAwaitingFinality as error:
                        return dict(step=step["id"], create_group=group, **error.outcome)
                    except ValueError as error:
                        if str(error) != "TRANSACTION_REVERTED":
                            raise
                        emit_debug(error, code="TRANSACTION_REVERTED")
                        return self.recovery_status()
                    if pending is not None:
                        return pending
                    handled_groups.add(group)
                    continue
                if step.get("parallel_group"):
                    group = step["parallel_group"]
                    if group in handled_groups:
                        continue
                    try:
                        pending = apply_group(self, groups[group], account, identity, dry_run=dry_run, verify=verify,
                            before_submit=before_submit)
                    except ReplacementAwaitingFinality as error:
                        return dict(step=step["id"], parallel_group=group, **error.outcome)
                    except ValueError as error:
                        if str(error) != "TRANSACTION_REVERTED":
                            raise
                        emit_debug(error, code="TRANSACTION_REVERTED")
                        return self.recovery_status()
                    if pending is not None:
                        return pending
                    handled_groups.add(group)
                    continue
                key = self.prefix + ":" + step["id"]
                entry = self.journal.put(key, step)
                result = entry["result"]
                if step["kind"] == "ulr_distribution":
                    pending = distribution_checkpoint(self, step, entry, account,
                        release=ulr_distribution_complete, accepted=accept_ulr_distribution, readonly=dry_run or verify)
                    if pending is not None:
                        return pending
                    continue
                if step["kind"] == "bitcoin_ready":
                    if defer_zk_freshness:
                        result = self.bitcoin_binding()
                        # Retain an original successful observation; a structural
                        # run cannot turn deferred freshness into CONFIRMED.
                        if entry["status"] != "CONFIRMED":
                            self.journal.update(key, "ZK_FRESHNESS_DEFERRED", result)
                        continue
                    # A previously successful observation is never a permanent
                    # freshness permission. Recheck on every resume/verify.
                    try:
                        result = self.bitcoin_ready(identity, readonly=dry_run or verify or not step.get("sync", True))
                    except ValueError as exc:
                        if verify or dry_run or str(exc) != "MAINNET_RELAY_STALE":
                            raise
                        from .zk_spv import is_zk
                        result = dict(status="IN_PROGRESS", reason=("AWAITING_ZK_PROOF"
                            if is_zk(self.profile.get("bitcoin_policy")) else "RELAY_NOT_FRESH"))
                    if result["status"] != "MAINNET_RELAY_READY":
                        self.journal.update(key, "AWAITING_BITCOIN_RELAY", result)
                        manifest = self.manifest()
                        self.journal.update(self.prefix, "AWAITING_BITCOIN_RELAY", manifest)
                        return dict(status="AWAITING_BITCOIN_RELAY", step=step["id"],
                                    bitcoin=result, manifest=manifest)
                    self.journal.update(key, "CONFIRMED", result)
                    continue
                if (step["kind"] in ("wait", "read") and entry["status"] == "RECOVERY_REQUIRED"
                        and result.get("reason") != "SIGNING_DEADLINE_EXPIRED"):
                    raise ValueError("PRIOR_TRANSACTION_RECOVERY_REQUIRED")
                if entry["status"] == "CONFIRMED":
                    if step["kind"] in ("wait", "read"):
                        self.verify_read_step(step, result, key)
                    receipt = None
                    if result.get("tx_hash"):
                        try:
                            receipt = self.verify_receipt(result)
                        except TransactionNotFound:
                            raise ValueError("EVM_RPC_BEHIND") from None
                        if receipt is None:
                            if self.evm_confirmations > int(self.profile.get("evm_confirmations", "1")):
                                return dict(status="CONFIRMING", step=step["id"], tx_hash=result["tx_hash"])
                            raise ValueError("EVM_RPC_BEHIND")
                    self.outputs(step, result)
                    self.verify_checks(step, receipt)
                    continue
                if verify:
                    return dict(status="INCOMPLETE", step=step["id"])
                if step["kind"] in ("wait", "read"):
                    ready, failure = self.observe_read_step(step, result)
                    if failure:
                        manifest = self.manifest()
                        self.journal.update(key, "RECOVERY_REQUIRED", dict(reason=failure))
                        self.journal.update(self.prefix, "RECOVERY_REQUIRED", manifest)
                        return dict(status="RECOVERY_REQUIRED", reason=failure, step=step["id"], manifest=manifest)
                    if not ready:
                        status = step.get("pending_status", "AWAITING_PARTICIPANTS")
                        self.journal.update(key, status, {})
                        manifest = self.manifest()
                        self.journal.update(self.prefix, status, manifest)
                        return dict(status=status, step=step["id"], manifest=manifest)
                    self.journal.update(key, "CONFIRMED", result)
                    continue
                if dry_run:
                    return dict(
                        status="DRY_RUN", next_step=step, manifest=self.manifest()
                    )
                if account is None:
                    raise ValueError("LOCAL_EVM_IDENTITY_REQUIRED")
                recover_only = bool(before_submit([step])) if before_submit is not None else False
                # Shared identity lock across all journals prevents parallel nonce allocation.
                nonce_lock = Path(identity).resolve().with_suffix(".evm-nonce.lock")
                with nonce_lock.open("a") as nl:
                    fcntl.flock(nl, fcntl.LOCK_EX)
                    identity_journal = Journal(str(nonce_lock) + ".sqlite")
                    saved = restore_signed(identity_journal, self, key, step, result)
                    if saved is not None and result.get("tx_hash") != saved["tx_hash"]:
                        result.update(saved)
                        self.journal.update(key, "SIGNED", result)
                    if recover_only and "raw_transaction" not in result:
                        return dict(status="NOT_SIGNED", step=step["id"])
                    if "raw_transaction" in result:
                        signed_nonce(result, account, self.w3.eth.chain_id)
                    replacements = {}
                    reservations = set()
                    try:
                        floor = recover_identity(identity_journal, self, account, replacements=replacements,
                                                 reservations=reservations)
                    except ReplacementAwaitingFinality as error:
                        return dict(step=step["id"], **error.outcome)
                    replacement = replacements.get((str(self.journal.path.resolve()), key))
                    if replacement:
                        if replacement["status"] == "BROADCAST":
                            return dict(step=step["id"], **replacement)
                        result = replacement["result"]
                    if "raw_transaction" not in result:
                        transfer_before = None
                        if step.get("verify_token_transfer"):
                            transfer = step["verify_token_transfer"]
                            transfer_before = self.contract(transfer["token"]).functions.balanceOf(
                                self.resolve(transfer["recipient"])
                            ).call()
                        nonce = self.w3.eth.get_transaction_count(account.address, "pending")
                        if nonce < max(floor, max(reservations, default=-1) + 1):
                            waiting = next((item for item in replacements.values() if item["status"] == "BROADCAST"), None)
                            if waiting is not None:
                                return dict(step=step["id"], **waiting)
                            raise ValueError("EVM_RPC_BEHIND")
                        tx = self.build_transaction(step, account, nonce)
                        signed = account.sign_transaction(tx)
                        raw = bytes(signed.raw_transaction)
                        result = dict(
                            tx_hash=Web3.keccak(raw).hex(),
                            raw_transaction=raw.hex(),
                            nonce=str(tx["nonce"]),
                        )
                        if transfer_before is not None:
                            result["transfer_balance_before"] = str(transfer_before)
                        save_signed(identity_journal, self, key, step, result, allow_fee_replacement=(
                            type(self) is Bootstrap and before_submit is None and step["kind"] == "call"))
                    else:
                        # Adopt an older per-step reservation whose process
                        # crashed before the legacy global-journal write.
                        try:
                            identity_journal.get(result["tx_hash"])
                        except KeyError:
                            save_signed(identity_journal, self, key, step, result)
                    try:
                        receipt = self.w3.eth.get_transaction_receipt(result["tx_hash"])
                    except TransactionNotFound:
                        try:
                            self.w3.eth.get_transaction(result["tx_hash"])
                        except TransactionNotFound:
                            if signed_nonce(result, account, self.w3.eth.chain_id) < floor:
                                raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
                            self.broadcast(result["raw_transaction"])
                        self.journal.update(key, "BROADCAST", result)
                        try:
                            receipt = self.w3.eth.wait_for_transaction_receipt(
                                result["tx_hash"], timeout=30
                            )
                        except Exception:
                            return dict(status="BROADCAST", step=step["id"], tx_hash=result["tx_hash"])
                    check_receipt_binding(receipt, result)
                    if receipt.status == 0:
                        from .evm_network import verify_anchor
                        verify_anchor(self.w3, receipt.blockNumber, receipt.blockHash)
                    result.update(
                        block_number=str(receipt.blockNumber),
                        block_hash=receipt.blockHash.hex(),
                    )
                    if receipt.status != 1:
                        self.journal.update(key, "REVERTED", result)
                        return self.recovery_status()
                    if self.verify_receipt(result, receipt=receipt) is None:
                        self.journal.update(key, "CONFIRMING", result)
                        return dict(status="CONFIRMING", step=step["id"], tx_hash=result["tx_hash"])
                    if step.get("verify_token_transfer"):
                        try:
                            self.verify_token_funding(step, result, receipt)
                        except ValueError:
                            self.journal.update(key, "RECOVERY_REQUIRED", result)
                            raise
                    self.record_deployment(step, result, receipt)
                    self.outputs(step, result)
                    try:
                        self.verify_checks(step, receipt)
                    except Exception as error:
                        from .transaction_wait import observation_pending
                        if observation_pending(error):
                            self.journal.update(key, "CONFIRMING", result)
                            return dict(status="CONFIRMING", step=step["id"], reason="RPC_OBSERVATION_PENDING",
                                        tx_hash=result["tx_hash"])
                        self.journal.update(key, "RECOVERY_REQUIRED", result)
                        raise
                    self.journal.update(key, "CONFIRMED", result)
                    identity_journal.update(result["tx_hash"], "CONFIRMED",
                        dict(block_hash=result["block_hash"], block_number=result["block_number"]))
            manifest = self.manifest()
            status = "GRAPH_CONFIGURED_FRESHNESS_PENDING" if defer_zk_freshness else "VERIFIED"
            self.journal.update(self.prefix, status, manifest)
            return dict(status=status, manifest=manifest)

    def recovery_status(self):
        prefix = self.prefix + ":"
        with self.journal.db() as db:
            row = db.execute(
                "SELECT id,status,result FROM operations WHERE substr(id,1,?)=? "
                "AND status IN ('REVERTED','RECOVERY_REQUIRED') "
                "AND coalesce(json_extract(result,'$.reason'),'')!='SIGNING_DEADLINE_EXPIRED' ORDER BY rowid LIMIT 1",
                (len(prefix), prefix),
            ).fetchone()
        if row is None:
            return None
        details = json.loads(row["result"])
        result = dict(status="RECOVERY_REQUIRED", step=row["id"][len(prefix):],
                      reason="TRANSACTION_REVERTED" if row["status"] == "REVERTED" else "POSTCONDITION_REVIEW_REQUIRED")
        if "tx_hash" in details:
            result["tx_hash"] = details["tx_hash"]
        return result


def main(argv):
    p = DiagnosticArgumentParser()
    p.add_argument("action", choices=["plan", "apply", "resume", "verify", "acknowledge-revert",
                                      "recover-create-group", "recheck", "replace-pending", "bind-legacy-nonce"])
    p.add_argument("--profile")
    p.add_argument("--deployment")
    p.add_argument("--state", default="ops/deployments.sqlite")
    p.add_argument("--identity-file")
    p.add_argument("--tx-hash")
    p.add_argument("--rpc", help="Override the local EVM RPC transport without changing the saved deployment")
    p.add_argument("--source-journal", help="Original durable FROST transaction journal for bind-legacy-nonce")
    recovery = p.add_mutually_exclusive_group()
    recovery.add_argument("--confirm-recovery", metavar="FINGERPRINT", help="Retire a reviewed failed CREATE group")
    recovery.add_argument("--prepare-nonce-close", action="store_true", help="Review an exact zero-value nonce close")
    recovery.add_argument("--confirm-nonce-close", metavar="FINGERPRINT", help="Authorize that exact nonce close")
    p.add_argument("--confirm-replacement", metavar="FINGERPRINT", help="Send the reviewed fee replacement for this call")
    p.add_argument("--max-fee-per-gas-wei", help="Replacement EIP-1559 fee cap in wei")
    p.add_argument("--max-priority-fee-per-gas-wei", help="Replacement EIP-1559 tip cap in wei")
    p.add_argument("--gas-limit", help="Replacement gas limit, defaults to the previous attempt's limit")
    p.add_argument("--max-cost-wei", help="Explicit maximum gas cost plus transferred value in wei")
    p.add_argument("--step")
    p.add_argument("--bitcoin-rpc")
    p.add_argument("--bitcoin-cookie")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--defer-zk-freshness", action="store_true",
                   help="Configure a ZK graph before its initial own proof; never marks it verified")
    gate = p.add_mutually_exclusive_group()
    gate.add_argument("--pause-for-funding", action="store_true")
    gate.add_argument("--funding-complete", action="store_true")
    p.add_argument("--funding-min-wei")
    p.add_argument("--funding-operator-min-wei")
    p.add_argument("--ulr-distribution-complete", action="store_true")
    p.add_argument("--accept-ulr-distribution")
    a = p.parse_args(argv)
    try:
        return _run(a)
    except ValueError as error:
        if str(error) != "EVM_RPC_BEHIND":
            raise
        print(json.dumps(dict(status="AWAITING_EVM_RPC", reason="EVM_RPC_BEHIND")))
        return 2


def _run(a):
    if a.action != "recover-create-group" and (a.confirm_recovery is not None
            or a.prepare_nonce_close or a.confirm_nonce_close is not None):
        raise ValueError("RECOVERY_INPUTS_REQUIRED")
    if a.source_journal is not None and a.action != "bind-legacy-nonce":
        raise ValueError("EVM_LEGACY_NONCE_INPUTS_REQUIRED")
    replacement = (a.confirm_replacement, a.max_fee_per_gas_wei,
                   a.max_priority_fee_per_gas_wei, a.gas_limit, a.max_cost_wei)
    if a.action == "replace-pending":
        if (not all((a.identity_file, a.rpc, a.tx_hash, a.max_fee_per_gas_wei,
                     a.max_priority_fee_per_gas_wei, a.max_cost_wei)) or a.profile or a.deployment or a.dry_run):
            raise ValueError("EVM_REPLACEMENT_INPUTS_REQUIRED")
        from .bootstrap_replacement import replace_pending

        result = replace_pending(a.identity_file, a.rpc, a.tx_hash, fee=a.max_fee_per_gas_wei,
            priority=a.max_priority_fee_per_gas_wei, max_cost=a.max_cost_wei, gas=a.gas_limit,
            confirmed=a.confirm_replacement)
        print(json.dumps(result, indent=2))
        return 2 if result["status"] == "EVM_REPLACEMENT_SEND_UNCERTAIN" else 0
    if any(value is not None for value in replacement):
        raise ValueError("EVM_REPLACEMENT_INPUTS_REQUIRED")
    if a.action == "bind-legacy-nonce":
        if not all((a.identity_file, a.rpc, a.tx_hash, a.source_journal)) or a.profile or a.deployment or a.dry_run:
            raise ValueError("EVM_LEGACY_NONCE_INPUTS_REQUIRED")
        from .bootstrap_nonce_legacy import bind_legacy_nonce

        print(json.dumps(bind_legacy_nonce(a.identity_file, a.source_journal, a.rpc, a.tx_hash), indent=2))
        return 0
    if a.action == "acknowledge-revert":
        if not (a.identity_file and a.rpc and a.tx_hash):
            raise ValueError("RECOVERY_INPUTS_REQUIRED")
        from .bootstrap_recovery import acknowledge_revert
        print(json.dumps(acknowledge_revert(a.identity_file, a.tx_hash, a.rpc), indent=2))
        return 0
    if a.action == "recover-create-group":
        if not (a.identity_file and a.deployment) or a.profile or a.dry_run:
            raise ValueError("RECOVERY_INPUTS_REQUIRED")
        from .bootstrap_recovery import recover_create_group
        if not Path(a.state).is_file():
            raise ValueError("CREATE_GROUP_RECOVERY_JOURNAL")
        try:
            profile = Journal(a.state).get(a.deployment)["request"]["profile"]
        except KeyError:
            raise ValueError("CREATE_GROUP_RECOVERY_JOURNAL") from None
        result = recover_create_group(Bootstrap(profile, a.state, evm_rpc_override=a.rpc), a.identity_file, a.confirm_recovery,
                                      prepare_close=a.prepare_nonce_close, confirm_close=a.confirm_nonce_close)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "CREATE_GROUP_RETRY_READY" else 2
    if a.action == "recheck":
        if not (a.deployment and a.step) or a.profile:
            raise ValueError("RECOVERY_INPUTS_REQUIRED")
        from .bootstrap_recovery import recheck_receipt
        profile = Journal(a.state).get(a.deployment)["request"]["profile"]
        print(json.dumps(recheck_receipt(Bootstrap(profile, a.state, evm_rpc_override=a.rpc), a.step), indent=2))
        return 0
    if a.profile:
        path = Path(a.profile)
        if not path.exists():
            path = ROOT / "config" / (a.profile + ".json")
        profile = json.loads(path.read_text())
        if "steps" not in profile:
            from .bootstrap_plan import build

            profile = build(profile)
    else:
        profile = Journal(a.state).get(a.deployment)["request"]["profile"]
    if a.defer_zk_freshness and (a.action not in ("apply", "resume") or a.dry_run):
        raise ValueError("BOOTSTRAP_ZK_BINDING_OPTIONS")
    if a.action == "plan":
        if (a.pause_for_funding or a.funding_complete or a.funding_min_wei or a.funding_operator_min_wei
                or a.ulr_distribution_complete or a.accept_ulr_distribution):
            raise ValueError("BOOTSTRAP_FUNDING_OPTIONS")
        print(json.dumps(public_data(profile), indent=2))
        return 0
    bitcoin = None
    if profile.get("bitcoin_network") in ("mainnet", "testnet4"):
        if not a.bitcoin_rpc or not a.bitcoin_cookie:
            raise ValueError("MAINNET_CORE_REQUIRED")
        from .bitcoin_rpc import BitcoinRPC
        bitcoin = BitcoinRPC(a.bitcoin_rpc, a.bitcoin_cookie, profile["bitcoin_network"], profile["bitcoin_policy"])
    elif a.bitcoin_rpc or a.bitcoin_cookie:
        raise ValueError("REGTEST_MAINNET_POLICY_MIXED")
    result = Bootstrap(profile, a.state, bitcoin=bitcoin, evm_rpc_override=a.rpc).apply(
        a.identity_file, a.dry_run, a.action == "verify", defer_zk_freshness=a.defer_zk_freshness,
        pause_for_funding=a.pause_for_funding,
        funding_complete=a.funding_complete, funding_min_wei=a.funding_min_wei,
        funding_operator_min_wei=a.funding_operator_min_wei, ulr_distribution_complete=a.ulr_distribution_complete,
        accept_ulr_distribution=a.accept_ulr_distribution
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("VERIFIED", "DRY_RUN") else 2
