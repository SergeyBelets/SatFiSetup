# SPDX-License-Identifier: Apache-2.0
"""DynamoDB access. Table names come from the stack environment."""

from decimal import Decimal
from functools import lru_cache

import boto3

from .settings import env


@lru_cache(maxsize=None)
def table(env_name: str):
    return boto3.resource("dynamodb").Table(env(env_name))


def deployments():
    return table("T_DEPLOYMENTS")


def reports():
    return table("T_REPORTS")


def events():
    return table("T_EVENTS")


def nonces():
    return table("T_NONCES")


def requests():
    return table("T_REQUESTS")


def allowlist():
    return table("T_ALLOWLIST")


def codes():
    return table("T_CODES")


def bindings():
    return table("T_BINDINGS")


def plain(value):
    """Convert DynamoDB Decimals back to int for JSON output."""
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def scan_all(tbl, **kwargs):
    items, start = [], None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = tbl.scan(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def query_all(tbl, **kwargs):
    items, start = [], None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = tbl.query(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items
