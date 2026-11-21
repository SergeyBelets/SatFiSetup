"""Execute one allowlisted API operation with a separately supplied local EVM identity."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .operations import Journal, OperationJournal, canonical, prepare_operation, public_data
from .bootstrap import Bootstrap


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


def execute(journal, requests, a, deployments):
    operation = requests.find(a.id, deployments)
    spec = operation["request"]["spec"]
    base = journal.get(spec["deployment"])["request"]["profile"]
    verified = Bootstrap(base, a.state, evm_rpc_override=a.rpc).apply(verify=True)
    if verified["status"] != "VERIFIED":
        raise ValueError("DEPLOYMENT_NOT_READY")
    state, profile = execution_profile(journal, operation, base, verified["manifest"]["contracts"])
    if operation["status"] != "PREPARED":
        # An API row can survive loss of the separate signed transaction journal.
        if not state.is_file():
            raise ValueError("API_OPERATION_EXECUTION_JOURNAL_MISSING")
        try:
            Journal(state).get(profile["deployment_id"])
        except KeyError:
            raise ValueError("API_OPERATION_EXECUTION_JOURNAL_MISSING") from None
    operation_bootstrap = Bootstrap(profile, state, evm_rpc_override=a.rpc)
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
    p.add_argument("action", choices=["prepare", "execute", "status", "view"])
    p.add_argument("--id")
    p.add_argument("--rpc", help="Override the local EVM RPC for execution or simulation")
    p.add_argument("--deployment", help="Deployment ID when multiple deployments use the same operation key")
    p.add_argument("--state", required=True)
    p.add_argument("--identity-file")
    p.add_argument("--policy")
    p.add_argument(
        "--params", help="Public JSON request, or ABI argument array for view"
    )
    p.add_argument("--view")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--sender", help="Public EVM address used for eth_call simulation")
    a = p.parse_args(argv)
    journal = Journal(a.state)
    requests = OperationJournal(a.state)
    if a.action in ("prepare", "view"):
        policy = public_data(json.loads(Path(a.policy).read_text()))
        params = (
            None
            if not a.params
            else public_data(json.loads(Path(a.params).read_text()))
        )
        if a.action == "prepare":
            result = prepare_operation(policy, requests, a.id, params)
        else:
            from .operational_api import read_view

            result = read_view(policy, journal, a.view, params)
        print(json.dumps(result, indent=2))
        return 0
    deployments = [a.deployment] if a.deployment else None
    operation = requests.find(a.id, deployments)
    if a.action == "status":
        print(json.dumps(requests.external(operation), indent=2))
        return 0
    deployments = [operation["request"]["spec"]["deployment"]]
    try:
        if a.dry_run:
            result = execute(journal, requests, a, deployments)
        else:
            with execution_lock(journal, operation):
                result = execute(journal, requests, a, deployments)
    except ValueError as error:
        if str(error) != "EVM_RPC_BEHIND":
            raise
        result = dict(status="AWAITING_EVM_RPC", reason="EVM_RPC_BEHIND")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"SIMULATED", "VERIFIED"} else 2
