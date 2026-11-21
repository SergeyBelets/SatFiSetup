"""Canonical PUBLIC Bitcoin plan. Never serialize caller dictionaries unchecked."""
import json
import re
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
from .operations import canonical

DOMAIN = keccak(text="SATOHSI_BTC_ONCHAIN_PLAN_V1")
MAX_PLAN_BYTES = 16384
MAX_FEE_PLAN_BYTES = 65536
MAX_MONEY = 2_100_000_000_000_000
FIELDS = {
    "version", "network", "batch_registry", "batch_id", "signing_board",
    "wallet_result", "wallet_script", "deadline", "inputs", "outputs", "fee",
    "max_fee", "withdrawal_refs", "signers",
}


def decimal(value, maximum=2**64 - 1, *, positive=False):
    digits = 77 if maximum > 2**64-1 else 19
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,%d}" % digits, value):
        raise ValueError("BTC_PLAN_INTEGER")
    number = int(value)
    if number > maximum or (positive and number == 0):
        raise ValueError("BTC_PLAN_INTEGER")
    return number


def hex_value(value, size):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % (size * 2), value):
        raise ValueError("BTC_PLAN_HEX")
    return bytes.fromhex(value)


def address(value):
    if not isinstance(value, str) or not Web3.is_checksum_address(value) or int(value, 16) == 0:
        raise ValueError("BTC_PLAN_ADDRESS")
    return value


def script(value):
    if not isinstance(value, str) or not (
        re.fullmatch(r"0014[0-9a-f]{40}", value)
        or re.fullmatch(r"5120[0-9a-f]{64}", value)
    ):
        raise ValueError("BTC_PLAN_SCRIPT")
    return value


def validate_plan(plan, manifest=None, *, allow_commitments=False):
    if not isinstance(plan, dict):
        raise ValueError("BTC_PLAN_SCHEMA")
    from .bitcoin_input_keys import PLAN_FIELDS, validate_schema, versioned
    fields = FIELDS | (PLAN_FIELDS if versioned(plan) else set())
    fields |= {'withdrawal_fee'} if plan.get('version')=='4' else set()
    if plan.get("version") == "5":
        fields |= {"previous"}
        if hex_value(plan.get("previous"), 32) == hex_value(plan.get("batch_id"), 32):
            raise ValueError("BTC_REPLACEMENT_PLAN")
    expected = fields | ({"commitments"} if allow_commitments and "commitments" in plan else set())
    if set(plan) != expected:
        raise ValueError("BTC_PLAN_SCHEMA")
    if plan["version"] not in ("2", "3", "4", "5") or plan["network"] not in ("regtest", "mainnet", "testnet4"):
        raise ValueError("BTC_PLAN_VERSION_OR_NETWORK")
    address(plan["batch_registry"]); address(plan["signing_board"])
    for field in ("batch_id", "wallet_result"):
        if hex_value(plan[field], 32) == bytes(32):
            raise ValueError("BTC_PLAN_ZERO_ID")
    if not script(plan["wallet_script"]).startswith("5120"):
        raise ValueError("BTC_PLAN_WALLET")
    decimal(plan["deadline"], positive=True)
    fee = decimal(plan["fee"], MAX_MONEY)
    if fee > decimal(plan["max_fee"], MAX_MONEY):
        raise ValueError("BTC_PLAN_FEE")
    signers = plan["signers"]
    if not isinstance(signers, list) or not 2 <= len(signers) <= 16:
        raise ValueError("SIGNER_SUBSET_INVALID")
    ids = [decimal(x, 16, positive=True) for x in signers]
    if ids != sorted(set(ids)):
        raise ValueError("SIGNER_SUBSET_INVALID")
    if manifest and (len(ids) != manifest.threshold or max(ids) > len(manifest.participants)):
        raise ValueError("SIGNER_SUBSET_INVALID")
    if not isinstance(plan["inputs"], list) or not 1 <= len(plan["inputs"]) <= 16:
        raise ValueError("BTC_PLAN_INPUTS")
    seen = set(); total_in = 0
    for item in plan["inputs"]:
        if not isinstance(item, dict) or set(item) != {"outpoint", "amount", "script"}:
            raise ValueError("BTC_PLAN_INPUT_SCHEMA")
        if not isinstance(item["outpoint"], str) or ":" not in item["outpoint"]:
            raise ValueError("BTC_PLAN_OUTPOINT")
        txid, vout = item["outpoint"].split(":", 1)
        hex_value(txid, 32); decimal(vout, 2**32 - 1)
        if item["outpoint"] in seen or (not versioned(plan) and item["script"] != plan["wallet_script"]):
            raise ValueError("BTC_PLAN_INPUT_BINDING")
        seen.add(item["outpoint"])
        total_in += decimal(item["amount"], MAX_MONEY, positive=True)
    if not isinstance(plan["outputs"], list) or not 1 <= len(plan["outputs"]) <= 32:
        raise ValueError("BTC_PLAN_OUTPUTS")
    total_out = 0
    for item in plan["outputs"]:
        if not isinstance(item, dict) or set(item) != {"amount", "script"}:
            raise ValueError("BTC_PLAN_OUTPUT_SCHEMA")
        script(item["script"])
        total_out += decimal(item["amount"], MAX_MONEY, positive=True)
    if total_in > MAX_MONEY or total_out + fee != total_in:
        raise ValueError("BTC_PLAN_BALANCE")
    refs = plan["withdrawal_refs"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
        raise ValueError("BTC_PLAN_WITHDRAWALS")
    for ref in refs:
        hex_value(ref, 32)
    if len(set(refs)) != len(refs):
        raise ValueError("BTC_PLAN_WITHDRAWALS")
    if versioned(plan): validate_schema(plan)
    if plan['version']=='4':
        from .withdrawal_fee import validate_metadata
        validate_metadata(plan)
    return {field: plan[field] for field in fields}


def serialize_plan(plan, manifest=None, *, allow_commitments=False):
    raw = canonical(validate_plan(plan, manifest, allow_commitments=allow_commitments)).encode()
    if len(raw) > (MAX_FEE_PLAN_BYTES if plan.get('version')=='4' else MAX_PLAN_BYTES):
        raise ValueError("BTC_PLAN_TOO_LARGE")
    return raw


def decode_plan(raw, manifest=None):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_FEE_PLAN_BYTES:
        raise ValueError("BTC_PLAN_LENGTH")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("BTC_PLAN_ENCODING") from None
    if serialize_plan(value, manifest) != raw:
        raise ValueError("BTC_PLAN_NONCANONICAL")
    return value


def onchain_plan_hash(manifest, plan):
    raw = serialize_plan(plan, manifest, allow_commitments=True)
    return keccak(encode(
        ["bytes32", "uint256", "address", "bytes32", "bytes32", "bytes32"],
        [DOMAIN, manifest.chain_id, plan["signing_board"],
         bytes.fromhex(plan["wallet_result"]), bytes.fromhex(plan["batch_id"]), keccak(raw)],
    ))
