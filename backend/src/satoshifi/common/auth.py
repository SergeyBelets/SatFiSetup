# SPDX-License-Identifier: Apache-2.0
"""Signed service and operator requests (spec 5.3, X-SatoshiFi-* headers)."""

import base64
import time

from botocore.exceptions import ClientError

from . import messages, settings, sigs, store
from .http import ApiError

NONCE_TTL_S = 600
MAX_BODY = 16 * 1024


def raw_body(event) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        data = base64.b64decode(body)
    else:
        data = body.encode("utf-8")
    if len(data) > MAX_BODY:
        raise ApiError("PAYLOAD_TOO_LARGE")
    return data


def request_path(event) -> str:
    path = event.get("rawPath", "")
    query = event.get("rawQueryString", "")
    return path + ("?" + query if query else "")


def verify_signed(event) -> tuple:
    """Return (signer_address, raw_body). Checks signature, clock skew and nonce reuse."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    try:
        address = headers["x-satoshifi-address"]
        timestamp = int(headers["x-satoshifi-timestamp"])
        nonce = headers["x-satoshifi-nonce"]
        signature = headers["x-satoshifi-signature"]
    except (KeyError, ValueError):
        raise ApiError("BAD_REQUEST", "X-SatoshiFi-Address, -Timestamp, -Nonce and -Signature are required")
    try:
        messages.require_checksum(address)
    except messages.FormatError as exc:
        raise ApiError("BAD_REQUEST", str(exc))
    now = int(time.time())
    if abs(now - timestamp) > settings.runtime()["clock_skew_s"]:
        raise ApiError("STALE_TIMESTAMP")
    body = raw_body(event)
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    try:
        text = messages.api_string(method, request_path(event), timestamp, nonce, body)
        signer = sigs.recover(text, signature)
    except (messages.FormatError, sigs.SignatureError) as exc:
        raise ApiError("BAD_SIGNATURE", str(exc))
    if signer != address:
        raise ApiError("BAD_SIGNATURE", "signer does not match X-SatoshiFi-Address")
    try:
        store.nonces().put_item(
            Item={"scope": signer, "nonce": nonce, "used_at": now, "ttl": now + NONCE_TTL_S},
            ConditionExpression="attribute_not_exists(nonce)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError("REPLAY")
        raise
    return signer, body


def require_operator(signer: str) -> None:
    if signer != settings.runtime()["operator_address"]:
        raise ApiError("FORBIDDEN", "operator signature required")
