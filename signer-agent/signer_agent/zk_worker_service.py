"""Service admission and voluntary confederation funding for the local worker.

A paid job is read from the canonical registry; voluntary funding never writes a
paid marker or a reimbursement claim. Treasury balance alone is not a stop signal.
"""
import json
from eth_account import Account
from web3 import Web3
from .gas_treasury import GasTreasuryClient
from .onboarding import read_private
from .operations import canonical


def validate_service(c):
    service = c["service"]
    if (set(service) != {"mode", "binding", "max_self_funded_cost_wei", "max_self_funded_job_cost_wei"}
            or service["mode"] not in ("confederation", "paid_or_confederation", "paid")):
        raise ValueError("ZK_SERVICE_POLICY")
    for name in ("max_self_funded_cost_wei", "max_self_funded_job_cost_wei"):
        value = service[name]
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or str(int(value)) != value or not 0 < int(value) < 2**256:
            raise ValueError("ZK_SERVICE_BUDGET")
    if service["mode"] != "confederation":
        binding = service["binding"]
        if (not isinstance(binding, dict) or binding.get("bitcoin_policy") != c["bitcoin_policy"]
                or any(binding.get(k) != c[k] for k in ("chain_id", "evm_confirmations"))):
            raise ValueError("ZK_SERVICE_BINDING")
    elif service["binding"] is not None:
        raise ValueError("ZK_SERVICE_BINDING")


def semantic_key(worker, need_ids):
    # Attempts, old state and root are intentionally absent: catch-up/rebase does
    # not create a second bill. Multi-need grouping is immutable once attached.
    return Web3.keccak(text=canonical(["SATOSHIFI_ZK_SERVICE_NEEDS_V1", worker.inbox.get("scope"), sorted(need_ids)]))


def attach(worker, request):
    if set(request) != {"service_job_id", "need_ids"}: raise ValueError("ZK_SERVICE_LINK_SCHEMA")
    ids = request["need_ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 256 or len(ids) != len(set(ids)):
        raise ValueError("ZK_SERVICE_LINK_NEEDS")
    known = {k for k, _, _ in worker.inbox.needs()}
    if any(k not in known for k in ids): raise ValueError("ZK_SERVICE_UNKNOWN_NEED")
    job = request["service_job_id"]
    if not isinstance(job, str) or len(job) != 64 or any(c not in "0123456789abcdef" for c in job):
        raise ValueError("ZK_SERVICE_JOB_ID")
    service = worker.c.get("service")
    if not service or service["mode"] == "confederation": raise ValueError("ZK_SERVICE_BINDING")
    client = GasTreasuryClient(worker.w3, service["binding"])
    block = client._binding(); view = client.service_profile(block)
    if client._call(view, "state", block) != worker.c["zk_state"]: raise ValueError("ZK_SERVICE_BINDING")
    terms = client._call(client.registry, "jobs", block, bytes.fromhex(job))
    if bytes(terms[0]).hex() != job or bytes(terms[1]) != semantic_key(worker, ids): raise ValueError("ZK_SERVICE_TERMS")
    record = dict(service_job_id=job, need_ids=sorted(ids), deadline=str(terms[5]))
    client._anchor(block)
    worker.inbox.db.execute("BEGIN IMMEDIATE")
    try:
        for need in ids:
            previous = worker.inbox.get("service:" + need)
            if previous is not None and previous != record: raise ValueError("ZK_SERVICE_LINK_CONFLICT")
            worker.inbox.put("service:" + need, record)
        worker.inbox.db.commit()
    except BaseException:
        worker.inbox.db.rollback(); raise
    return record


def source(worker, need_ids, *, allow_unpaid=False):
    policy = worker.c.get("service")
    if not policy: return dict(source="CONFEDERATION", jobs=[])
    consumer_needs = [need for need in need_ids if not need.startswith(("heartbeat:", "maintenance:"))]
    records = [worker.inbox.get("service:" + need) for need in consumer_needs]
    if policy["mode"] != "confederation" and records and all(records):
        client = GasTreasuryClient(worker.w3, policy["binding"])
        block = client._binding(); view = client.service_profile(block)
        if client._call(view, "state", block) != worker.c["zk_state"]: raise ValueError("ZK_SERVICE_BINDING")
        jobs = sorted({r["service_job_id"] for r in records})
        paid = True
        for record in {r["service_job_id"]: r for r in records}.values():
            job = bytes.fromhex(record["service_job_id"])
            terms = client._call(client.registry, "jobs", block, job)
            if (bytes(terms[0]) != job or bytes(terms[1]) != semantic_key(worker, record["need_ids"])
                    or str(terms[5]) != record["deadline"]):
                raise ValueError("ZK_SERVICE_TERMS_REVALIDATION")
            paid = client._call(client.registry, "paid", block, job) and paid
        client._anchor(block)
        if paid: return dict(source="PAID_SERVICE", jobs=jobs)
    security_work = bool(need_ids) and all(need.startswith(("heartbeat:", "maintenance:")) for need in need_ids)
    if policy["mode"] == "paid" and not allow_unpaid and not security_work: raise ValueError("ZK_SERVICE_AWAITING_PAYMENT")
    return dict(source="CONFEDERATION", jobs=sorted({r["service_job_id"] for r in records if r}))


def preflight(worker, need_ids):
    decision = source(worker, need_ids)
    policy = worker.c.get("service")
    if decision["source"] == "CONFEDERATION" and policy:
        rows = worker.inbox.db.execute("SELECT body FROM meta WHERE key LIKE 'self-cost:%'")
        entries = [json.loads(r[0]) for r in rows]
        if sum(int(r["cost"]) for r in entries) >= int(policy["max_self_funded_cost_wei"]):
            raise ValueError("ZK_SELF_FUNDING_BUDGET")
        for need in need_ids:
            if sum(int(r["cost"]) for r in entries if need in r["need_ids"]) >= int(policy["max_self_funded_job_cost_wei"]):
                raise ValueError("ZK_SELF_FUNDING_JOB_BUDGET")
    account = Account.from_key(json.loads(read_private(worker.c["identity_file"]))["private_key"])
    # Paid status and future claimable funds are not spendable native balance.
    if worker.w3.eth.get_balance(account.address, "pending") < int(worker.c["max_transaction_cost_wei"]):
        raise ValueError("ZK_PUBLISHER_BALANCE_REQUIRED")
    return decision


def reserve(worker, operation, cost, need_ids):
    db = worker.inbox.db
    db.execute("BEGIN IMMEDIATE")
    try:
        # Funding class is immutable after reservation, even if payment appears
        # later. Reclassifying it must never refund already reserved own budget.
        existing = worker.inbox.get("self-cost:" + operation)
        prior = worker.inbox.get("funding:" + operation)
        funding = prior if prior and prior["source"] == "CONFEDERATION" else source(worker, need_ids)
        if funding["source"] == "CONFEDERATION" and worker.c.get("service"):
            policy = worker.c["service"]
            value = max(cost, int(existing["cost"]) if existing else 0)
            entries = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM meta WHERE key LIKE 'self-cost:%' AND key!=?", ("self-cost:"+operation,))]
            if sum(int(r["cost"]) for r in entries) + value > int(policy["max_self_funded_cost_wei"]):
                raise ValueError("ZK_SELF_FUNDING_BUDGET")
            for need in need_ids:
                if sum(int(r["cost"]) for r in entries if need in r["need_ids"]) + value > int(policy["max_self_funded_job_cost_wei"]):
                    raise ValueError("ZK_SELF_FUNDING_JOB_BUDGET")
            worker.inbox.put("self-cost:"+operation, dict(cost=str(value), need_ids=sorted(need_ids)))
        worker.inbox.put("funding:"+operation, funding)
        # Global/per-transaction budget in the same transaction, never resetting
        # the existing gas reservation when switching sources.
        worker.inbox.reserve(operation, cost, transaction=False)
        db.commit()
    except BaseException:
        db.rollback(); raise
