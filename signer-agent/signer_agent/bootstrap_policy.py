"""Explicit public policy committed by each V3 bootstrap enrollment."""

import re

from web3 import Web3


UINT256_MAX = 2**256 - 1
NUMBER_BOUNDS = {
    "min_p2wpkh_sats": (294, UINT256_MAX),
    "min_p2tr_sats": (330, UINT256_MAX),
    "min_custodians": (1, UINT256_MAX),
    "bitcoin_confirmations": (6, 2**32 - 1),
    "wrap_fee_bps": (0, 10000),
    "unwrap_fee_bps": (0, 10000),
    "wrap_staker_bps": (0, 10000),
    "deploy_staker_bps": (0, 10000),
    "converter_numerator": (1, UINT256_MAX),
    "converter_denominator": (1, UINT256_MAX),
    "converter_funding": (1, UINT256_MAX),
}
BOOLEAN_FIELDS = ("dao_use_reserve", "integration_fee_exemption")
ADDRESS_FIELDS = ("dao_token_recipient",)
POLICY_FIELDS = (*NUMBER_BOUNDS, *BOOLEAN_FIELDS, *ADDRESS_FIELDS)
MAINNET_FIELDS = ("bitcoin_policy", "reward_token", "btc_usd_feed", "evm_confirmations")


def effective_fee_policy(policy):
    """Public explanation; does not alter the enrolled policy or its fingerprint."""
    return dict(
        deployment_exempt=policy["integration_fee_exemption"],
        deployment_fee="0" if policy["integration_fee_exemption"] else policy["deployment_fee"]["amount"],
        wrap_bps=policy["wrap_fee_bps"], unwrap_bps=policy["unwrap_fee_bps"],
        user_fees_exempt=policy["wrap_fee_bps"] == "0" and policy["unwrap_fee_bps"] == "0",
        exemption_scope="DEPLOY_ONLY",
    )


def validate_network_policy(policy, error_prefix):
    """V6 separates EVM economic/finality policy from Bitcoin consensus selection."""
    from .bitcoin_network import is_public_network, validate_policy as bitcoin_policy
    network, chain = policy.get("bitcoin_network"), policy.get("chain_id")
    if chain not in ("1", "31337", "11155111") or network not in ("mainnet", "testnet4", "regtest"):
        raise ValueError(error_prefix + "network")
    backed = chain == "1" or is_public_network(network)
    if policy.get("mode") != ("backed" if backed else "integration"):
        raise ValueError(error_prefix + "mode")
    if is_public_network(network):
        bitcoin_policy(policy.get("bitcoin_policy"), network)
    elif "bitcoin_policy" in policy:
        raise ValueError(error_prefix + "bitcoin_policy")
    validate_confirmations(policy, error_prefix)
    if backed:
        validate_mainnet_policy(policy, error_prefix)


def validate_confirmations(policy, error_prefix):
    confirmations = policy.get("evm_confirmations")
    if (not isinstance(confirmations, str)
            or not re.fullmatch(r"[1-9][0-9]*", confirmations)
            or int(confirmations) > 256
            or policy.get("chain_id") == "1" and int(confirmations) < 12):
        raise ValueError(error_prefix + "evm_confirmations")


def validate_mainnet_policy(policy, error_prefix):
    """Validate shared V4 inputs; RPC routing and credentials never enter them."""
    from .bitcoin_network import is_public_network, validate_policy as validate_bitcoin_policy

    if is_public_network(policy.get("bitcoin_network")):
        validate_bitcoin_policy(policy.get("bitcoin_policy"), policy["bitcoin_network"])
        from .zk_spv import validate_confederation
        validate_confederation(policy["bitcoin_policy"], policy.get("confederation_id", ""))
    elif policy.get("bitcoin_network") != "regtest" or "bitcoin_policy" in policy:
        raise ValueError(error_prefix + "bitcoin_policy")
    validate_confirmations(policy, error_prefix)
    for field in (("reward_token",) if "ulr" in policy else ("reward_token", "btc_usd_feed")):
        component = policy.get(field)
        keys = {"address", "runtime_hash", "decimals"} if field == "reward_token" else {"address", "runtime_hash"}
        if not isinstance(component, dict) or set(component) != keys:
            raise ValueError(error_prefix + field)
        address = component["address"]
        code_hash = component["runtime_hash"]
        if (not isinstance(address, str) or not Web3.is_checksum_address(address)
                or int(address, 16) == 0 or not isinstance(code_hash, str)
                or not re.fullmatch(r"0x[0-9a-f]{64}", code_hash)
                or int(code_hash, 16) == 0):
            raise ValueError(error_prefix + field)
        if field == "reward_token":
            decimals = component["decimals"]
            if (not isinstance(decimals, str) or not re.fullmatch(r"0|[1-9][0-9]*", decimals)
                    or int(decimals) > 18):
                raise ValueError(error_prefix + field)
    if "btc_usd_feed" in policy and policy["reward_token"]["address"] == policy["btc_usd_feed"]["address"]:
        raise ValueError(error_prefix + "economic_component_overlap")


def validate_policy(policy, error_prefix):
    """Reject absent, ambiguous or out-of-range policy before building any steps."""
    for field, (minimum, maximum) in NUMBER_BOUNDS.items():
        value = policy.get(field)
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"0|[1-9][0-9]*", value)
            or not minimum <= int(value) <= maximum
        ):
            raise ValueError(error_prefix + field)
    for field in BOOLEAN_FIELDS:
        if type(policy.get(field)) is not bool:
            raise ValueError(error_prefix + field)
    ethereum = policy.get("chain_id") == "1"
    if policy["integration_fee_exemption"] is not (not ethereum):
        raise ValueError(error_prefix + "integration_fee_exemption")
    if ethereum:
        fee = policy.get("deployment_fee")
        if (not isinstance(fee, dict) or set(fee) != {"amount", "treasury"}
                or not isinstance(fee["amount"], str)
                or not re.fullmatch(r"[1-9][0-9]*", fee["amount"])
                or not 0 < int(fee["amount"]) <= UINT256_MAX // 2
                or not isinstance(fee["treasury"], str)
                or not Web3.is_checksum_address(fee["treasury"])
                or int(fee["treasury"], 16) == 0):
            raise ValueError(error_prefix + "deployment_fee")
        if 2 * int(fee["amount"]) + int(policy["converter_funding"]) > UINT256_MAX:
            raise ValueError(error_prefix + "funding_overflow")
    for field in ADDRESS_FIELDS:
        value = policy.get(field)
        if not isinstance(value, str) or not Web3.is_address(value) or int(value, 16) == 0:
            raise ValueError(error_prefix + field)
