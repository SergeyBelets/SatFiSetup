"""Public immutable launch economics, included in the enrolled ULR policy."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

DEFAULTS=dict(candidate_threshold='200000',minimum_stake='0.000000000000000001',
    treasury_reserve_usd='1000',treasury_payout_bps='9000',
    startup=dict(enabled=False,apy_bps='0',unit_price_usd='0',duration_days='0',start_day='0',budget_usd='0'))


def units(value,decimals):
    if not isinstance(value,str) or re.fullmatch(r'(0|[1-9][0-9]*)(\.[0-9]+)?',value) is None:
        raise ValueError('LAUNCH_DECIMAL_REQUIRED')
    whole,_,fraction=value.partition('.')
    if len(fraction)>decimals:raise ValueError('LAUNCH_DECIMAL_PRECISION')
    result=int(whole)*10**decimals+int((fraction+'0'*decimals)[:decimals] or '0')
    if result>=2**128:raise ValueError('LAUNCH_AMOUNT_RANGE')
    return result


def integer(value,low,high):
    if not isinstance(value,str) or re.fullmatch(r'0|[1-9][0-9]*',value) is None or not low<=int(value)<=high:
        raise ValueError('LAUNCH_INTEGER_RANGE')
    return int(value)


def economics(policy):
    if policy.get('version')=='1':return deepcopy(DEFAULTS)
    if policy.get('version')!='2':raise ValueError('ULR_BOOTSTRAP_POLICY')
    value=deepcopy(policy.get('economics'))
    if not isinstance(value,dict) or set(value)!=set(DEFAULTS):raise ValueError('LAUNCH_ECONOMICS_SCHEMA')
    if not 1<=units(value['candidate_threshold'],18)<=10_000_000*10**18:raise ValueError('LAUNCH_CANDIDATE_THRESHOLD')
    if not 1<=units(value['minimum_stake'],18)<=10_000_000*10**18:raise ValueError('LAUNCH_MINIMUM_STAKE')
    units(value['treasury_reserve_usd'],18);integer(value['treasury_payout_bps'],0,10000)
    s=value['startup']
    if not isinstance(s,dict) or set(s)!=set(DEFAULTS['startup']) or type(s['enabled']) is not bool:raise ValueError('LAUNCH_STARTUP_SCHEMA')
    if not s['enabled']:
        if s!=DEFAULTS['startup']:raise ValueError('LAUNCH_DISABLED_STARTUP_VALUES')
    else:
        integer(s['apy_bps'],1,100000);integer(s['duration_days'],1,3650);integer(s['start_day'],1,2**32-3651)
        if units(s['unit_price_usd'],18)==0 or units(s['budget_usd'],18)==0:raise ValueError('LAUNCH_STARTUP_FUNDING')
    return value


def encoded(policy,usd_decimals):
    e=economics(policy);s=e['startup']
    return dict(candidate_threshold=units(e['candidate_threshold'],18),minimum_stake=units(e['minimum_stake'],18),
        treasury_reserve=units(e['treasury_reserve_usd'],usd_decimals),treasury_payout_bps=int(e['treasury_payout_bps']),
        startup=dict(enabled=s['enabled'],apy_bps=int(s['apy_bps']),unit_price=units(s['unit_price_usd'],usd_decimals),
            duration_days=int(s['duration_days']),start_day=int(s['start_day']),budget=units(s['budget_usd'],usd_decimals)))
