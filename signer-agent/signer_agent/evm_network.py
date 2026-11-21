"""Ethereum mainnet finality and operator-local transaction spending limits.

A confirmation count is never a substitute for Ethereum consensus finality.
RPC failure or missing finalized support fails closed; no test flag weakens it.
"""
import os
import re

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
    canonical = w3.eth.get_block(number)
    if bytes(canonical.hash) != bytes(block_hash):
        raise ValueError("EVM_REORG_RECOVERY_REQUIRED")
    return number <= read_block(w3, confirmations)["number"]


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


class GasLimitExceeded(ValueError):
    """Unsigned EVM call exceeds the local gas budget; all units are gas."""
    def __init__(self, required, limit, estimate=None):
        super().__init__("ETHEREUM_TRANSACTION_GAS_LIMIT")
        self.required = required
        self.limit = limit
        self.estimate = estimate


def check_transaction_gas(w3, transaction, *, estimate=None):
    if not is_mainnet(w3):
        return
    transaction_policy(w3)
    gas = transaction.get("gas")
    if type(gas) is not int or gas <= 0:
        raise ValueError("ETHEREUM_TRANSACTION_GAS_LIMIT")
    limit = min(_limit("SATOSHIFI_MAX_TX_GAS"), MAX_TRANSACTION_GAS)
    if gas > limit:
        raise GasLimitExceeded(gas, limit, estimate)


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
    padded = dict(transaction, gas=max((transaction["gas"] * 130 + 99) // 100,
                                      transaction["gas"] + 125_000))
    check_transaction_gas(w3, padded, estimate=transaction["gas"])
    return padded
