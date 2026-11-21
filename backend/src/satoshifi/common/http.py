# SPDX-License-Identifier: Apache-2.0
"""HTTP API responses in the API-02 shape and error codes from spec 5.4."""

import json

STATUS_BY_CODE = {
    "BAD_REQUEST": 400,
    "BAD_SIGNATURE": 401,
    "STALE_TIMESTAMP": 401,
    "REPLAY": 401,
    "FORBIDDEN": 403,
    "NOT_ALLOWLISTED": 403,
    "UNKNOWN_DEPLOYMENT": 404,
    "CODE_USED": 409,
    "ALREADY_BOUND": 409,
    "CODE_EXPIRED": 410,
    "PAYLOAD_TOO_LARGE": 413,
    "RATE_LIMITED": 429,
    "INTERNAL": 500,
    "NOT_IMPLEMENTED": 501,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str = ""):
        if code not in STATUS_BY_CODE:
            raise ValueError("unknown error code %s" % code)
        super().__init__(message or code)
        self.code = code
        self.message = message


def respond(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        "body": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def ok(**fields) -> dict:
    return respond(200, {"status": "OK", **fields})


def error(code: str, message: str = "", **fields) -> dict:
    payload = {"status": "ERROR", "code": code}
    if message:
        payload["message"] = message
    payload.update(fields)
    return respond(STATUS_BY_CODE[code], payload)
