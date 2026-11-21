# SPDX-License-Identifier: Apache-2.0
"""Canonical JSON (RFC 8785 / JCS) restricted to the value types the API uses, and keccak256.

Allowed values: dict with str keys, list, str, bool, None, int within the
JavaScript safe-integer range. Floats are rejected: amounts travel as decimal
strings of wei (API-01), so a float in a signed body is a format error.
"""

from eth_utils import keccak

MAX_SAFE_INT = 2**53 - 1

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonError(ValueError):
    """Value cannot be represented in canonical form."""


def _string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise CanonError("lone surrogate in string")
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")


def _encode(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise CanonError("integer outside safe range; use a decimal string")
        return str(value)
    if isinstance(value, float):
        raise CanonError("floats are not allowed")
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonError("object keys must be strings")
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(_string(k) + ":" + _encode(value[k]) for k in keys) + "}"
    raise CanonError("unsupported type: %s" % type(value).__name__)


def jcs(value) -> bytes:
    """Return the RFC 8785 canonical form of value as UTF-8 bytes."""
    return _encode(value).encode("utf-8")


def keccak_hex(data: bytes) -> str:
    """Return 0x-prefixed lowercase hex keccak256 of data."""
    return "0x" + keccak(data).hex()
