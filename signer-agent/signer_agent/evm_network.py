"""Ethereum mainnet finality and operator-local transaction spending limits.

A confirmation count is never a substitute for Ethereum consensus finality.
RPC failure or missing finalized support fails closed; no test flag weakens it.
"""
import os
import re
from hexbytes import HexBytes
from web3.exceptions import BlockNotFound

MIN_CONFIRMATIONS = 12
MAX_TRANSACTION_GAS = 2**24


def is_mainnet(w3):
    return w3.eth.chain_id == 1


def read_block(w3, confirmations):
    if type(confirmations) is not int or not 1 <= confirmations <= 256:
        raise ValueError("EVM_CONFIRMATIONS_POLICY")
    mainnet = is_mainnet(w3)
    if mainnet and confirmations < MIN_CONFIRMATIONS:
        raise ValueError("ETHEREUM_MAINNET_CONFIRMATIONS")
    latest = w3.eth.get_block("latest")
    number = int(latest.number) - confirmations + 1
    if number < 0:
        raise ValueError("EVM_CONFIRMATIONS_PENDING")
    if mainnet:
        try:
            finalized = w3.eth.get_block("finalized")
            if (not finalized or type(finalized.number) is not int
                    or not 0 <= finalized.number <= latest.number
                    or len(bytes(finalized.hash)) != 32):
                raise ValueError()
            canonical = w3.eth.get_block(finalized.number)
            if bytes(canonical.hash) != bytes(finalized.hash):
                raise ValueError()
        except Exception:
            raise ValueError("ETHEREUM_FINALITY_UNAVAILABLE") from None
        number = min(number, finalized.number)
    return latest if number == latest.number else w3.eth.get_block(number)


def receipt_finalized(w3, number, block_hash, confirmations):
    safe = read_block(w3, confirmations)
    verify_anchor(w3, number, block_hash)
    return number <= safe.number


def verify_anchor(w3, number, expected_hash, *, safe=None):
    """A missing or insufficiently deep observation is not evidence of a fork."""
    if safe is not None and number > safe.number:
        raise ValueError("EVM_RPC_BEHIND")
    try:
        block = w3.eth.get_block(number) if safe is None or number != safe.number else safe
    except BlockNotFound:
        raise ValueError("EVM_RPC_BEHIND") from None
    if HexBytes(block.hash) != HexBytes(expected_hash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return block


def verify_witness_anchor(witness, block, *, safe=None):
    try:
        observed = verify_anchor(witness, block.number, block.hash, safe=safe)
    except ValueError as error:
        if str(error) == "EVM_REORG_RECOVERY_REQUIRED":
            raise ValueError("EVM_RPC_WITNESS_MISMATCH") from None
        raise
    if observed.number != block.number:
        raise ValueError("EVM_RPC_WITNESS_MISMATCH")
    return observed


def read_common_block(w3, witness, confirmations, chain_id, *, minimum=None):
    """Use the highest block confirmed by both RPCs, with no mixed-height reads."""
    if w3.eth.chain_id != chain_id:
        raise ValueError("CHAIN_MISMATCH")
    if witness.eth.chain_id != chain_id:
        raise ValueError("EVM_RPC_WITNESS_MISMATCH")
    primary = read_block(w3, confirmations)
    other = read_block(witness, confirmations)
    height = min(primary.number, other.number)
    if minimum is not None and height < minimum:
        raise ValueError("EVM_RPC_BEHIND")
    if primary.number == height:
        verify_witness_anchor(witness, primary, safe=other)
        return primary
    return verify_witness_anchor(w3, other, safe=primary)


def _limit(name):
    value = os.environ.get(name, "")
    if not re.fullmatch(r"[1-9][0-9]{0,77}", value) or int(value) >= 2**256:
        raise ValueError("ETHEREUM_TRANSACTION_BUDGET_REQUIRED")
    return int(value)


def transaction_policy(w3):
    if not is_mainnet(w3):
        return {}
    maximum = _limit("SATOSHIFI_MAX_FEE_PER_GAS_WEI")
    priority = _limit("SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI")
    gas = _limit("SATOSHIFI_MAX_TX_GAS")
    if priority > maximum or gas > MAX_TRANSACTION_GAS:
        raise ValueError("ETHEREUM_TRANSACTION_BUDGET_INVALID")
    return dict(maxFeePerGas=maximum, maxPriorityFeePerGas=priority)


def check_transaction_gas(w3, transaction):
    if not is_mainnet(w3):
        return
    transaction_policy(w3)
    if (type(transaction.get("gas")) is not int
            or not 0 < transaction["gas"] <= min(_limit("SATOSHIFI_MAX_TX_GAS"), MAX_TRANSACTION_GAS)):
        raise ValueError("ETHEREUM_TRANSACTION_GAS_LIMIT")


def gas_with_margin(gas):
    return max((gas * 130 + 99) // 100, gas + 125_000)


def transaction_gas_with_margin(w3, transaction):
    """Pad a fresh estimate while preserving the mainnet spending limits.

    A new checkpoint/day at inclusion can cost more than the estimated context.
    DAO unstake crossing UTC midnight needs 110,727 additional execution gas
    versus the preceding same-timestamp estimate (two checkpoint appends and
    three daily accounting writes). Keep a 125,000-gas floor as well as 30% for
    larger transactions. Reject an over-budget padded limit, never clip it.
    Already signed recovery transactions must retain their exact original bytes.
    """
    if type(transaction.get("gas")) is not int or transaction["gas"] <= 0:
        raise ValueError("ETHEREUM_TRANSACTION_GAS_LIMIT")
    check_transaction_gas(w3, transaction)
    padded = dict(transaction, gas=gas_with_margin(transaction["gas"]))
    check_transaction_gas(w3, padded)
    return padded
