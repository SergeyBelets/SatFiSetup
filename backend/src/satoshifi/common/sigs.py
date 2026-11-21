# SPDX-License-Identifier: Apache-2.0
"""EIP-191 personal_sign recovery."""

import re

from eth_account import Account
from eth_account.messages import encode_defunct

SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")


class SignatureError(ValueError):
    """Signature is malformed or does not belong to the expected address."""


def recover(message: str, signature: str) -> str:
    """Return the checksum address that signed message with personal_sign."""
    if not isinstance(message, str) or not isinstance(signature, str):
        raise SignatureError("message and signature must be strings")
    if not SIGNATURE_RE.match(signature):
        raise SignatureError("signature must be 65 bytes in 0x hex")
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception as exc:  # eth_keys raises several types for invalid points
        raise SignatureError("signature does not recover: %s" % exc) from exc


def verify(message: str, signature: str, expected: str) -> None:
    """Raise SignatureError unless message was signed by expected."""
    signer = recover(message, signature)
    if signer != expected:
        raise SignatureError("signed by %s, expected %s" % (signer, expected))
