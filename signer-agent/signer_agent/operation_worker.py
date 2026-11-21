"""Execute one allowlisted API operation with a separately supplied local EVM identity."""

import hashlib
import json
from pathlib import Path
from .safe_errors import DiagnosticArgumentParser
from .operations import Journal, OperationJournal, canonical, prepare_operation, public_data
from .bootstrap import Bootstrap


def execution_profile(journal, operation, base, contracts):
    request = operation["request"]
    spec = request["spec"]
    key = operation["id"]
    step = dict(id="execute", kind="call", contract=spec["contract"],
                method=spec["method"], args=request["args"])
    legacy_id = "operation-" + key
    try:
        legacy = journal.get(legacy_id)["request"].get("profile", {})
    except KeyError:
        legacy = {}
    if legacy == dict(base, deployment_id=legacy_id, contracts=contracts, steps=[step]):
        # Resume the exact legacy profile and nonce lane under the original
        # journal path. Bootstrap still verifies the complete profile digest.
        state, deployment_id = journal.path, legacy_id
    else:
        prior_target = legacy.get("contracts", {}).get(spec["contract"], {}).get("address")
        if prior_target and prior_target == contracts.get(spec["contract"], {}).get("address") and legacy.get("steps") == [step]:
            raise ValueError("API_LEGACY_OPERATION_PROFILE_MISMATCH")
        state = journal.path.with_name(journal.path.name + ".api-execution.sqlite")
        digest = hashlib.sha256(canonical([spec["deployment"], request["action"], key]).encode()).hexdigest()
        deployment_id = "operation-" + digest
    return state, dict(base, deployment_id=deployment_id, contracts=contracts, steps=[step])


def main(argv):
    p = DiagnosticArgumentParser()
    p.add_argument("action", choices=["prepare", "execute", "status", "view"])
    p.add_argument("--id")
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
    operation = requests.get(a.id, deployments=deployments)
    if a.action == "status":
        print(json.dumps(operation, indent=2))
        return 0
    request = operation["request"]
    spec = request["spec"]
    deployments = [spec["deployment"]]
    base = journal.get(spec["deployment"])["request"]["profile"]
    bootstrap = Bootstrap(base, a.state)
    verified = bootstrap.apply(verify=True)
    if verified["status"] != "VERIFIED":
        raise ValueError("DEPLOYMENT_NOT_READY")
    state, profile = execution_profile(journal, operation, base, verified["manifest"]["contracts"])
    operation_bootstrap = Bootstrap(profile, state)
    if a.dry_run:
        if not a.sender:
            raise ValueError("SIMULATION_SENDER_REQUIRED")
        fn = operation_bootstrap.fn(profile["steps"][0])
        sender = operation_bootstrap.w3.to_checksum_address(a.sender)
        value = fn.call({"from": sender})
        print(
            json.dumps(
                dict(
                    status="SIMULATED",
                    result=Bootstrap.wire(value),
                    gas=str(fn.estimate_gas({"from": sender})),
                ),
                indent=2,
            )
        )
        return 0
    requests.update(a.id, "EXECUTING", {}, deployments=deployments)
    try:
        result = operation_bootstrap.apply_confirmed(a.identity_file)
        requests.update(a.id, result["status"], result, deployments=deployments)
    except Exception:
        requests.update(
            a.id, "RECOVERY_REQUIRED", {"code": "EXECUTION_FAILED_RECONCILE_JOURNAL"}, deployments=deployments
        )
        raise
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "VERIFIED" else 2
