from copy import deepcopy
import pytest
from ops.zkspv.economics import MONTHLY, economic_input, receipt_costs, tco


def cost(value, unit="USD/month", kind="ASSUMPTION"):
    return {"value": value, "unit": unit, "kind": "MISSING" if value is None else kind,
            "as_of": "2026-10-27", "source": "test scenario", "payer": "project"}


def inputs():
    return {"currency": "USD", "modes": {mode: {k: cost("10") for k in MONTHLY}
             for mode in ("baseline", "shared_ordinary", "zk")},
            "incremental_investment": cost("100", "USD"), "required_payback_months": cost("12", "month"),
            "proving_jobs": cost("8", "job/month"), "target_network": None,
            "real_local_proof_measured": False, "sla_measured": False, "same_policy_replay": True}


def test_missing_is_not_zero_even_when_it_would_improve_savings():
    data = inputs()
    data["modes"]["zk"]["proving_variable"] = cost(None)
    result = tco(data)
    assert result["status"] == "UNKNOWN"
    assert result["monthly_costs"]["zk"] is None
    assert result["monthly_savings_vs_baseline"] is None
    assert result["payback_months"] is None
    # Break-even may be calculable when only actual proving cost is unknown.
    assert result["max_monthly_variable_proving_cost"] == "10"


def test_positive_arithmetic_scenario_does_not_claim_measured_acceptance():
    data = inputs()
    data["modes"]["baseline"]["state_updates"] = cost("100")
    data["modes"]["shared_ordinary"]["state_updates"] = cost("30")
    result = tco(data)
    assert result["monthly_savings_vs_baseline"] == "90"
    assert result["zk_compression_savings_vs_same_schedule_ordinary"] == "20"
    assert result["status"] == "UNKNOWN"


def test_zero_or_negative_savings_fail_without_fake_infinite_payback():
    data = inputs()
    result = tco(data)
    assert result["status"] == "UNKNOWN"
    assert result["arithmetic_status"] == "UNPROFITABLE"
    assert result["payback_months"] is None
    data["modes"]["zk"]["proving_variable"] = cost("200")
    assert tco(data)["arithmetic_status"] == "UNPROFITABLE"


def test_zero_jobs_not_free_proving_and_no_division():
    data = inputs()
    data["modes"]["baseline"]["state_updates"] = cost("100")
    data["proving_jobs"] = cost("0", "job/month")
    result = tco(data)
    assert result["status"] == "UNKNOWN"
    assert result["max_average_proving_cost_operating"] is None
    assert result["max_average_proving_cost_for_payback"] is None


def test_treasury_principal_and_user_caps_do_not_double_count_cost():
    a, b = inputs(), inputs()
    b["cash_flows"] = [{"kind": "treasury_contribution", "amount": "1000000"},
                       {"kind": "refundable_fee_cap", "amount": "2000000"}]
    assert tco(a)["monthly_costs"] == tco(b)["monthly_costs"]


def receipt():
    return {"chain_id": 31337, "transaction_hash": "0x"+"a"*64, "status": 1,
            "gas_used": "21000", "effective_gas_price_wei": "1000000000", "l1_fee_wei": "0",
            "operator_fee_wei": "0", "payer": "project", "source": "local Anvil receipt",
            "category": "state_update", "reasons": ["HEARTBEAT", "DEPOSIT"]}


def test_one_receipt_for_two_reasons_and_paid_failed_receipt():
    a, b = receipt(), receipt()
    b.update(transaction_hash="0x"+"b"*64, status=0)
    result = receipt_costs([a, b])
    assert result["wei_by_category"]["state_update"] == "42000000000000"
    assert len(result["receipts"]) == 2
    with pytest.raises(ValueError, match="DUPLICATE_RECEIPT"):
        receipt_costs([a, deepcopy(a)])


def test_unavailable_l2_fee_is_not_free():
    a = receipt()
    del a["l1_fee_wei"]
    assert receipt_costs([a])["wei_by_category"]["state_update"] is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", True, 0.1])
def test_invalid_costs_rejected(value):
    with pytest.raises(ValueError):
        economic_input(cost(value), "USD/month")


def test_missing_category_cannot_be_omitted_as_free():
    data = inputs()
    del data["modes"]["zk"]["compute_fixed"]
    with pytest.raises(ValueError, match="CATEGORIES"):
        tco(data)


def test_scalar_assumptions_and_string_flags_cannot_claim_acceptance():
    data = inputs()
    for mode in data["modes"].values():
        for item in mode.values():
            item["kind"] = "MEASURED"
    data["modes"]["baseline"]["state_updates"]["value"] = "100"
    data.update(target_network="scenario", real_local_proof_measured=True, sla_measured=True)
    result = tco(data)
    assert result["status"] == "UNKNOWN"
    assert set(result["assumptions"]) == {"incremental_investment", "required_payback_months", "proving_jobs"}
    for key in ("incremental_investment", "required_payback_months", "proving_jobs"):
        data[key]["kind"] = "MEASURED"
    data["real_local_proof_measured"] = "false"
    assert "real_local_proof_measured" in tco(data)["missing_or_unverified"]
