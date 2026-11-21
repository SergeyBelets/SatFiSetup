"""§10 receipt accounting and Decimal TCO. Unknown is never interpreted as zero."""
from decimal import Decimal, InvalidOperation

KINDS = {"MEASURED", "QUOTE", "ASSUMPTION", "MISSING"}
CATEGORIES = {"state_update", "message", "application", "materialize", "gas_funding", "deployment"}


def decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError("USE_EXACT_DECIMAL_STRINGS")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("INVALID_DECIMAL") from None
    if not result.is_finite() or result < 0:
        raise ValueError("NONNEGATIVE_FINITE_COST_REQUIRED")
    return result


def economic_input(record, unit):
    if record.get("kind") not in KINDS or record.get("unit") != unit:
        raise ValueError("ECONOMIC_INPUT_SCHEMA")
    if record["kind"] == "MISSING":
        if record.get("value") is not None:
            raise ValueError("MISSING_MUST_BE_NULL")
        return None
    if any(not record.get(key) for key in ("as_of", "source", "payer")):
        raise ValueError("COST_PROVENANCE_REQUIRED")
    return decimal(record["value"])


def receipt_costs(receipts):
    """Each actual transaction is one cost even when it serves multiple reasons.

    status=0 receipts still incur gas. Internal transfers/cap/refunds are cash
    flows, not another miner fee. L2 data/operator fees must explicitly be known;
    missing fields propagate instead of silently becoming free.
    """
    seen, details, totals = set(), [], {}
    for receipt in receipts:
        key = (receipt["chain_id"], receipt["transaction_hash"])
        if key in seen:
            raise ValueError("DUPLICATE_RECEIPT")
        seen.add(key)
        if (receipt["category"] not in CATEGORIES or receipt["status"] not in (0, 1)
                or not receipt.get("payer") or not receipt.get("source")):
            raise ValueError("INVALID_RECEIPT_METADATA")
        parts = [receipt.get(k) for k in ("gas_used", "effective_gas_price_wei", "l1_fee_wei", "operator_fee_wei")]
        known = all(part is not None for part in parts)
        # Validate known values even if a different component is unavailable.
        parsed = [decimal(x) if x is not None else None for x in parts]
        cost = parsed[0] * parsed[1] + parsed[2] + parsed[3] if known else None
        category = receipt["category"]
        old = totals.get(category, Decimal(0))
        totals[category] = None if old is None or cost is None else old + cost
        details.append({"chain_id": key[0], "transaction_hash": key[1], "category": category,
                        "status": receipt["status"], "payer": receipt["payer"],
                        "reasons": receipt.get("reasons", []), "wei": str(cost) if cost is not None else None})
    return {"receipts": details, "wei_by_category": {k: str(v) if v is not None else None for k, v in totals.items()}}


MONTHLY = ("state_updates", "messages", "applications", "operations", "compute_fixed", "proving_variable")


def tco(inputs):
    """Compare baseline/common-ordinary/ZK on the same declared replay window.

    Native principals, reserve and user subsidy belong in cash_flows only. They
    are deliberately not summed into costs. Actual BTC miner fees, when present,
    belong once in applications with an explicit common accounting currency.
    """
    unit = inputs["currency"] + "/month"
    modes = inputs["modes"]
    if set(modes) != {"baseline", "shared_ordinary", "zk"}:
        raise ValueError("THREE_COMPARABLE_MODES_REQUIRED")
    parsed, monthly, missing, assumptions = {}, {}, [], []
    for mode, costs in modes.items():
        if set(costs) != set(MONTHLY):
            raise ValueError("ALL_RECURRING_COST_CATEGORIES_REQUIRED")
        parsed[mode] = {}
        for category in MONTHLY:
            item = economic_input(costs[category], unit)
            parsed[mode][category] = item
            if item is None:
                missing.append(mode + "." + category)
            if costs[category]["kind"] == "ASSUMPTION":
                assumptions.append(mode + "." + category)
        monthly[mode] = None if any(x is None for x in parsed[mode].values()) else sum(parsed[mode].values(), Decimal(0))
    investment = economic_input(inputs["incremental_investment"], inputs["currency"])
    horizon = economic_input(inputs["required_payback_months"], "month")
    jobs = economic_input(inputs["proving_jobs"], "job/month")
    if jobs is not None and jobs != jobs.to_integral_value():
        raise ValueError("JOB_COUNT_MUST_BE_INTEGRAL")
    for name, value in (("incremental_investment", investment), ("required_payback_months", horizon), ("proving_jobs", jobs)):
        if value is None:
            missing.append(name)
        if inputs[name]["kind"] == "ASSUMPTION":
            assumptions.append(name)
    if horizon is not None and horizon <= 0:
        raise ValueError("POSITIVE_PAYBACK_HORIZON_REQUIRED")
    savings = (monthly["baseline"] - monthly["zk"]
               if monthly["baseline"] is not None and monthly["zk"] is not None else None)
    zk_savings = (monthly["shared_ordinary"] - monthly["zk"]
                  if monthly["shared_ordinary"] is not None and monthly["zk"] is not None else None)
    payback = investment / savings if investment is not None and savings is not None and savings > 0 else None
    other_zk = [v for k, v in parsed["zk"].items() if k != "proving_variable"]
    residual = monthly["baseline"] - sum(other_zk, Decimal(0)) if monthly["baseline"] is not None and all(v is not None for v in other_zk) else None
    p_operating = residual / jobs if residual is not None and jobs is not None and jobs > 0 else None
    p_payback = ((residual - investment / horizon) / jobs
                 if residual is not None and investment is not None and horizon is not None and jobs is not None and jobs > 0 else None)
    blockers = list(missing)
    if not inputs.get("target_network"):
        blockers.append("target_network")
    for condition in ("real_local_proof_measured", "sla_measured", "same_policy_replay"):
        if inputs.get(condition) is not True:
            blockers.append(condition)
    if assumptions:
        blockers.append("assumption_scenario_is_not_measured_acceptance")
    status = "UNKNOWN" if blockers else "PASS"
    arithmetic_status = "UNKNOWN" if savings is None else "POSITIVE"
    if savings is not None and savings <= 0:
        arithmetic_status = "UNPROFITABLE"
        status = "UNKNOWN" if blockers else "FAIL"
    elif payback is not None and horizon is not None and payback > horizon:
        arithmetic_status = "PAYBACK_EXCEEDED"
        status = "UNKNOWN" if blockers else "FAIL"
    elif jobs == 0:
        status = "UNKNOWN"
        blockers.append("no_measured_proving_jobs")

    def out(value):
        return str(value) if value is not None else None

    return {"schema": "satoshifi.zkspv.tco.v1", "status": status, "arithmetic_status": arithmetic_status, "currency": inputs["currency"],
            "monthly_costs": {k: out(v) for k, v in monthly.items()}, "monthly_savings_vs_baseline": out(savings),
            "zk_compression_savings_vs_same_schedule_ordinary": out(zk_savings), "payback_months": out(payback),
            "max_monthly_variable_proving_cost": out(residual), "max_average_proving_cost_operating": out(p_operating),
            "max_average_proving_cost_for_payback": out(p_payback), "missing_or_unverified": blockers,
            "assumptions": assumptions, "cash_flows_excluded_from_cost": inputs.get("cash_flows", []),
            "formula_scope": "stationary monthly scenario; nonstationary acceptance needs cumulative month-by-month flows"}
