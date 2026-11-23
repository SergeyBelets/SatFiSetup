"""Execute one allowlisted API operation with a separately supplied local EVM identity."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound
from .safe_errors import DiagnosticArgumentParser
from .operations import Journal, OperationJournal, canonical, prepare_operation, public_data
from .bitcoin_trust import _check_runtime, checked_runtime
from .bootstrap import Bootstrap, pinned_artifact
from .bootstrap_recovery import check_receipt_binding, signed_fields
from .evm_network import read_block, verify_anchor
from .operational_api import add_connection_options, connection_options, deployment_runner, operation_visible, read_view


# These methods release existing EVM balances without authorizing any Bitcoin
# action. A policy action name or additional policy field cannot qualify a call.
EVM_ONLY_EXITS = frozenset({
    ("ULRStaking", "claim"),
    ("ULRStaking", "unstake"),
    ("CustodianDAOStaking", "claim"),
    ("CustodianDAOStaking", "unstake"),
    ("ConfederationDAOStake", "unstake"),
    ("ConfederationDAOIncome", "claim"),
    ("ConfederationDAOIncome", "claimReimbursement"),
})


def read_policy(path):
    if not path:
        raise ValueError("API_EXECUTION_POLICY_REQUIRED")
    try:
        policy = public_data(json.loads(Path(path).read_text()))
        if (not isinstance(policy, dict) or not isinstance(policy.get("deployments"), list)
                or not isinstance(policy.get("actions", {}), dict)
                or any(not isinstance(spec, dict) for spec in policy.get("actions", {}).values())):
            raise ValueError()
    except (OSError, ValueError):
        raise ValueError("API_EXECUTION_POLICY_INVALID") from None
    return policy


def require_current_policy(path, operation):
    if not operation_visible(read_policy(path), operation):
        raise ValueError("ACTION_NOT_ALLOWED")


def require_supported_target(step, target):
    if (target["artifact"] in ("ULRStaking", "BitcoinMintAdmission") and target.get("artifact_hash")
            and (target["artifact"], step["method"]) not in EVM_ONLY_EXITS):
        _, previous = pinned_artifact(target)
        if previous:
            raise ValueError("API_LEGACY_ARTIFACT_EXIT_ONLY")


class OperationBootstrap(Bootstrap):
    def __init__(self, profile, state, *, operation, policy_path, evm_rpc_override):
        super().__init__(profile, state, evm_rpc_override=evm_rpc_override)
        self.operation = operation
        self.policy_path = policy_path

    def build_transaction(self, step, account, nonce):
        require_supported_target(step, self.contracts[step["contract"]])
        tx = super().build_transaction(step, account, nonce)
        # This method returns directly to sign_transaction under the identity
        # lock. Retries of already signed bytes never invoke the signing gate.
        require_current_policy(self.policy_path, self.operation)
        return tx


def execution_profile(journal, operation, base, contracts):
    request = operation["request"]
    spec = request["spec"]
    target = contracts.get(spec["contract"], {})
    if target.get("artifact") != spec["artifact"]:
        raise ValueError("API_OPERATION_ARTIFACT_MISMATCH")
    key = request.get("client_id", operation["id"])
    step = dict(id="execute", kind="call", contract=spec["contract"],
                method=spec["method"], args=request["args"])
    if "client_id" not in request:
        legacy_id = "operation-" + key
        try:
            legacy = journal.get(legacy_id)["request"]
        except KeyError:
            legacy = None
        if legacy is not None:
            profile = dict(base, deployment_id=legacy_id, contracts=contracts, steps=[step])
            if legacy == dict(profile=profile, version="1"):
                return journal.path, profile
            raise ValueError("API_LEGACY_OPERATION_PROFILE_MISMATCH")
    state = journal.path.with_name(journal.path.name + ".api-execution.sqlite")
    digest = hashlib.sha256(canonical([spec["deployment"], request["action"], key]).encode()).hexdigest()
    deployment_id = "operation-" + digest
    return state, dict(base, deployment_id=deployment_id, contracts=contracts, steps=[step])


def recorded_execution(state, profile):
    execution = profile["deployment_id"]
    if not state.is_file():
        raise ValueError("API_OPERATION_EXECUTION_JOURNAL_MISSING")
    source = Journal(state)
    try:
        saved_profile = source.get(execution)["request"]
        row = source.get(execution + ":execute")
    except KeyError:
        raise ValueError("API_OPERATION_EXECUTION_JOURNAL_MISSING") from None
    step = profile["steps"][0]
    if saved_profile != dict(profile=profile, version="1") or row["request"] != step:
        raise ValueError("API_OPERATION_EXECUTION_PROFILE_MISMATCH")
    if not row["result"].get("raw_transaction"):
        raise ValueError("API_SIGNED_TRANSACTION_NOT_RECORDED")
    return row


def check_bound_call(runner, step, target, fields):
    if (HexBytes(fields["to"]) != HexBytes(target["address"]) or fields["value"] != 0
            or HexBytes(fields["data"]) != HexBytes(runner.fn(step)._encode_transaction_data())):
        raise ValueError("EVM_SIGNED_TRANSACTION_BINDING")


def operation_receipt(journal, operation, evm_rpc):
    """Observe an exact journaled call with EVM RPC only, without advancing either journal."""
    spec = operation["request"]["spec"]
    deployment = journal.get(spec["deployment"])
    base = deployment["request"]["profile"]
    state, profile = execution_profile(journal, operation, base, deployment["result"].get("contracts", {}))
    row = recorded_execution(state, profile)
    step, saved = row["request"], row["result"]
    target = profile["contracts"][step["contract"]]
    runner = Bootstrap(profile, state, evm_rpc_override=evm_rpc)
    check_bound_call(runner, step, target, signed_fields(saved, None, int(profile["chain_id"])))
    tx_hash = saved["tx_hash"]
    try:
        receipt = runner.w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        if saved.get("block_hash") and "block_number" in saved:
            verify_anchor(runner.w3, int(saved["block_number"]), saved["block_hash"])
        return dict(status="AWAITING_EVM_RECEIPT", tx_hash=tx_hash)
    check_receipt_binding(receipt, saved)
    safe = read_block(runner.w3, runner.evm_confirmations)
    if receipt.blockNumber > safe.number:
        return dict(status="AWAITING_EVM_RECEIPT", tx_hash=tx_hash)
    verify_anchor(runner.w3, receipt.blockNumber, receipt.blockHash, safe=safe)
    pin = target.get("runtime_hash")
    code = runner.w3.eth.get_code(target["address"], block_identifier=receipt.blockNumber)
    if not pin or not code or Web3.keccak(code) != HexBytes(pin):
        raise ValueError("CODE_HASH_MISMATCH")
    verify_anchor(runner.w3, receipt.blockNumber, receipt.blockHash)
    return dict(
        status="RECEIPT_CONFIRMED" if receipt.status == 1 else "RECEIPT_REVERTED",
        tx_hash=tx_hash, block_number=str(receipt.blockNumber), block_hash=receipt.blockHash.hex(),
    )


@contextmanager
def execution_lock(journal, operation):
    storage_key = hashlib.sha256(operation["id"].encode()).hexdigest()
    path = journal.path.with_name(journal.path.name + ".api-worker-" + storage_key + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("JOURNAL_FILE_PERMISSIONS")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("API_OPERATION_EXECUTION_IN_PROGRESS") from None
        yield
    finally:
        os.close(fd)


def verified_execution(journal, operation, a):
    spec = operation["request"]["spec"]
    base = journal.get(spec["deployment"])["request"]["profile"]
    evm_exit = ((spec["artifact"], spec["method"]) in EVM_ONLY_EXITS
                and any(step["kind"] == "bitcoin_ready" for step in base["steps"]))
    runner = deployment_runner(base, journal, evm_graph_only=evm_exit, **connection_options(a))
    verified = runner.apply(verify=True, evm_graph_only=True) if evm_exit else runner.apply(verify=True)
    if verified["status"] != ("EVM_GRAPH_VERIFIED" if evm_exit else "VERIFIED"):
        raise ValueError("DEPLOYMENT_NOT_READY")
    state, profile = execution_profile(journal, operation, base, verified["manifest"]["contracts"])
    if evm_exit:
        target = profile["contracts"][spec["contract"]]
        block = read_block(runner.w3, runner.evm_confirmations)
        previous = False
        if spec["artifact"] == "ULRStaking" and target.get("artifact_hash"):
            compiled, previous = pinned_artifact(target)
        if previous:
            code = runner.w3.eth.get_code(target["address"], block_identifier=block.number)
            _check_runtime(code, compiled["deployedBytecode"])
            code_hash = Web3.keccak(code)
        else:
            code_hash = checked_runtime(runner.w3, target["address"], spec["artifact"], block.number)
        if HexBytes(code_hash) != HexBytes(target["runtime_hash"]):
            raise ValueError("CODE_HASH_MISMATCH")
        verify_anchor(runner.w3, block.number, block.hash)
    return state, profile


def replace_operation(journal, requests, a, deployments):
    """Require the same policy and approved call for each fee-only attempt."""
    from .bootstrap_replacement import replace_pending

    operation = requests.find(a.id, deployments)
    require_current_policy(a.policy, operation)
    state, profile = verified_execution(journal, operation, a)
    row = recorded_execution(state, profile)
    step = row["request"]
    target = profile["contracts"][step["contract"]]
    runner = Bootstrap(profile, state, evm_rpc_override=a.rpc)

    def review(w3, source, entry, saved_profile, fields):
        if (source.path.resolve() != state.resolve() or entry["id"] != row["id"]
                or entry["request"] != step or saved_profile != profile):
            raise ValueError("API_OPERATION_EXECUTION_PROFILE_MISMATCH")
        current = requests.find(a.id, deployments)
        if current["request"] != operation["request"]:
            raise ValueError("API_OPERATION_EXECUTION_PROFILE_MISMATCH")
        check_bound_call(runner, step, target, fields)
        require_supported_target(step, target)
        latest = w3.eth.get_block("latest")
        for block in (latest.number, "pending"):
            code = w3.eth.get_code(target["address"], block_identifier=block)
            if not target.get("runtime_hash") or not code or Web3.keccak(code) != HexBytes(target["runtime_hash"]):
                raise ValueError("CODE_HASH_MISMATCH")
        verify_anchor(w3, latest.number, latest.hash)
        # Read policy last, under both the execution and identity nonce locks.
        require_current_policy(a.policy, current)

    return replace_pending(a.identity_file, a.rpc or profile["evm_rpc"], row["result"]["tx_hash"],
                           fee=a.max_fee_per_gas_wei, priority=a.max_priority_fee_per_gas_wei,
                           max_cost=a.max_cost_wei, confirmed=a.confirm_replacement, api_guard=review)


def execute(journal, requests, a, deployments):
    operation = requests.find(a.id, deployments)
    if operation["status"] == "PREPARED" or a.dry_run:
        require_current_policy(a.policy, operation)
    state, profile = verified_execution(journal, operation, a)
    if operation["status"] != "PREPARED":
        # An API row can survive loss of the separate signed transaction journal.
        if not state.is_file():
            raise ValueError("API_OPERATION_EXECUTION_JOURNAL_MISSING")
        try:
            Journal(state).get(profile["deployment_id"])
        except KeyError:
            raise ValueError("API_OPERATION_EXECUTION_JOURNAL_MISSING") from None
    operation_bootstrap = OperationBootstrap(profile, state, operation=operation, policy_path=a.policy,
                                             evm_rpc_override=a.rpc)
    if a.dry_run:
        if not a.sender:
            raise ValueError("SIMULATION_SENDER_REQUIRED")
        fn = operation_bootstrap.fn(profile["steps"][0])
        sender = operation_bootstrap.w3.to_checksum_address(a.sender)
        value = fn.call({"from": sender})
        return dict(status="SIMULATED", result=Bootstrap.wire(value), gas=str(fn.estimate_gas({"from": sender})))
    requests.update(a.id, "EXECUTING", {}, deployments=deployments)
    try:
        result = operation_bootstrap.apply_confirmed(a.identity_file)
    except Exception as error:
        from .transaction_wait import TransactionPending, observation_pending

        if type(error) is ValueError and str(error) in (
                "API_EXECUTION_POLICY_REQUIRED", "API_EXECUTION_POLICY_INVALID", "ACTION_NOT_ALLOWED",
                "API_LEGACY_ARTIFACT_EXIT_ONLY"):
            requests.update(a.id, operation["status"], operation["result"], deployments=deployments)
            raise
        if isinstance(error, TransactionPending) or observation_pending(error):
            result = error.result if isinstance(error, TransactionPending) else dict(status="AWAITING_EVM_RPC")
            if operation["status"] == "VERIFIED":
                requests.update(a.id, operation["status"], operation["result"], deployments=deployments)
            else:
                requests.update(a.id, result["status"], result, deployments=deployments)
            return result
        requests.update(
            a.id, "RECOVERY_REQUIRED", {"code": "EXECUTION_FAILED_RECONCILE_JOURNAL"}, deployments=deployments
        )
        raise
    requests.update(a.id, result["status"], result, deployments=deployments)
    return result


def main(argv):
    p = DiagnosticArgumentParser()
    p.add_argument("action", choices=["prepare", "execute", "status", "view", "receipt", "replace"])
    p.add_argument("--id")
    add_connection_options(p)
    p.add_argument("--deployment", help="Deployment ID when multiple deployments use the same operation key")
    p.add_argument("--state", required=True)
    p.add_argument("--identity-file")
    p.add_argument("--policy", help="Current local policy for prepare, view, signing, simulation and replacement")
    p.add_argument(
        "--params", help="Public JSON request, or ABI argument array for view"
    )
    p.add_argument("--view")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sender", help="Public EVM address used for eth_call simulation")
    p.add_argument("--confirm-replacement", metavar="FINGERPRINT", help="Sign and send the reviewed fee replacement")
    p.add_argument("--max-fee-per-gas-wei")
    p.add_argument("--max-priority-fee-per-gas-wei")
    p.add_argument("--max-cost-wei")
    a = p.parse_args(argv)
    fees = (a.max_fee_per_gas_wei, a.max_priority_fee_per_gas_wei, a.max_cost_wei)
    if a.action == "replace":
        if a.dry_run or a.sender or not a.identity_file or not all(fees):
            raise ValueError("API_REPLACEMENT_INPUTS_REQUIRED")
    elif any(value is not None for value in (*fees, a.confirm_replacement)):
        raise ValueError("API_REPLACEMENT_INPUTS_REQUIRED")
    journal = Journal(a.state)
    requests = OperationJournal(a.state)
    if a.action in ("prepare", "view"):
        policy = read_policy(a.policy)
        params = (
            None
            if not a.params
            else public_data(json.loads(Path(a.params).read_text()))
        )
        if a.action == "prepare":
            result = prepare_operation(policy, requests, a.id, params)
        else:
            result = read_view(policy, journal, a.view, params, **connection_options(a))
        print(json.dumps(result, indent=2))
        return 0
    deployments = [a.deployment] if a.deployment else None
    operation = requests.find(a.id, deployments)
    if a.action == "status":
        print(json.dumps(requests.external(operation), indent=2))
        return 0
    if a.action == "receipt":
        try:
            result = operation_receipt(journal, operation, a.rpc)
        except ValueError as error:
            if str(error) not in ("EVM_RPC_BEHIND", "EVM_CONFIRMATIONS_PENDING"):
                raise
            result = dict(status="AWAITING_EVM_RPC", reason=str(error))
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "RECEIPT_CONFIRMED" else 2
    deployments = [operation["request"]["spec"]["deployment"]]
    try:
        if a.dry_run:
            result = execute(journal, requests, a, deployments)
        else:
            with execution_lock(journal, operation):
                run = replace_operation if a.action == "replace" else execute
                result = run(journal, requests, a, deployments)
    except ValueError as error:
        if str(error) != "EVM_RPC_BEHIND":
            raise
        result = dict(status="AWAITING_EVM_RPC", reason="EVM_RPC_BEHIND")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"SIMULATED", "VERIFIED", "EVM_REPLACEMENT_REVIEW",
                                     "EVM_REPLACEMENT_BROADCAST", "EVM_REPLACEMENT_ALREADY_MINED"} else 2
