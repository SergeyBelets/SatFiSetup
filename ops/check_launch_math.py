"""Reproduce launch documentation examples with integers and Decimal, offline.

This is an independent calculation of the published algorithms, not an EVM
execution or a proof of global protocol safety. Run: python ops/check_launch_math.py
"""
import json
from decimal import Decimal, localcontext

W = 10**18
USD = 10**6


def annual_factor(rate):
    result, base, exponent = W, W + rate, 365
    while exponent:
        if exponent & 1:
            result = result * base // W
        exponent >>= 1
        if exponent:
            base = base * base // W
    return result


def daily_rate(apy):
    target = W + apy * W // 10000
    low, high = 0, apy * W // 10000 // 365 + 1
    assert annual_factor(low) <= target < annual_factor(high)
    while high - low > 1:
        mid = (low + high) // 2
        if annual_factor(mid) <= target:
            low = mid
        else:
            high = mid
    assert annual_factor(low) <= target < annual_factor(low + 1)
    return low


def check():
    # Every allowed integer APY: the constructor's search upper bound brackets
    # its target under the floored evaluator (no Decimal approximation here).
    for apy in range(1, 100001):
        high = apy * W // 10000 // 365 + 1
        assert annual_factor(high) > W + apy * W // 10000
    samples = []
    with localcontext() as context:
        context.prec = 90
        for apy in (1, 10, 100, 1000, 1234, 5000, 10000, 50000, 100000):
            rate = daily_rate(apy)
            ideal = (Decimal(1) + Decimal(apy) / 10000) ** (Decimal(1) / 365) - 1
            samples.append(dict(apy_bps=apy, daily_rate_wad=rate,
                ideal_daily_rate=str(ideal),
                difference_from_ideal_wad=str(Decimal(rate) - ideal * W)))
        ideal10 = (Decimal('1.1') ** (Decimal(1) / 365) - 1)
        linear_year = str(365 * ideal10)
    payout = lambda balance, reserve, bps: (balance-reserve)*600000*bps//(1000000*10000)
    first = payout(11000*USD, 1000*USD, 9000)
    second = payout(11000*USD-first, 1000*USD, 9000)
    custom = payout(11000*USD, 500*USD, 7500)
    assert (first, second, custom) == (5400*USD, 2484*USD, 4725*USD)
    rate10 = daily_rate(1000)
    requested = ((1000*W)*(2*USD)//W)*rate10//W
    assert (rate10, requested) == (261157876067812, 522315)
    assert (min(requested,USD), min(requested,USD-requested)) == (522315,477685)
    assert requested*30 == 15669450
    alice_first, alice_total, bob = 400*USD*100//400, 1200*USD*100//400, 1200*USD*300//400
    assert (alice_first, alice_total-alice_first, bob) == (100*USD,200*USD,900*USD)
    assert 1*1//2 == 0  # one raw unit / two equal owners: all remains dust
    fee = (100*W)*100//10000
    assert fee == W and 100*W-fee == 99*W
    assert W*1//10**10 == 100*USD
    return dict(status='LAUNCH_MATH_EXAMPLES_PASS',scope='Independent integer/Decimal calculation; not EVM execution',
        apy_upper_bounds_checked=100000, decimal_precision=90, apy_samples=samples,
        treasury_raw=dict(first=first, second=second, custom=custom),
        startup_raw=dict(daily_rate_wad=rate10, daily_requested=requested,
            thirty_days=requested*30, refund_from_1000_usd=1000*USD-requested*30,
            one_usd_budget_days=[522315,477685,0]),
        ideal_10_percent_apy_without_reinvestment_linear_year=linear_year,
        claims_raw=dict(alice_first=alice_first,alice_later=alice_total-alice_first,bob=bob,dust=1),
        fees_raw=dict(wrap_amount=100*W,wrap_bps=100,fee=fee,net=99*W,
            one_mp_usd_at_ratio_1_over_10_pow_10=W//10**10),
        qualified=False, production_ready=False)


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
