# SPDX-License-Identifier: Apache-2.0
"""Texts that wallets and keys sign (wrapper spec, section 5.3).

Every builder has one exact output for given inputs; every parser accepts only
text that the matching builder would produce (parse -> build -> compare).
"""

import re
from datetime import datetime, timezone

from eth_utils import is_checksum_address

from .canon import jcs, keccak_hex


class FormatError(ValueError):
    """Input does not match the signed-text format."""


DEPLOYMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CODE_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
API_NONCE_RE = re.compile(r"^0x[0-9a-f]{32}$")
DOMAIN_RE = re.compile(r"^[a-z0-9.-]+(:[0-9]{1,5})?$")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

REQUEST_HEADER = "SatoshiFi confederation request v1"
WSIG_HEADER = "SatoshiFi work binding v1"
API_HEADER = "SatoshiFi api v1"
LOGIN_STATEMENT = "Sign in to SatoshiFi."


def require_checksum(address: str, what: str = "address") -> str:
    if not isinstance(address, str) or not is_checksum_address(address):
        raise FormatError("%s must be a checksum address" % what)
    return address


def require_unix(value, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FormatError("%s must be a non-negative integer" % what)
    return value


def require_chain_id(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FormatError("chain_id must be a positive integer")
    return value


def require_match(regex, value, what: str) -> str:
    if not isinstance(value, str) or not regex.match(value):
        raise FormatError("bad %s" % what)
    return value


def rfc3339(unix: int) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_rfc3339(text: str) -> int:
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise FormatError("time must be YYYY-MM-DDTHH:MM:SSZ") from exc
    return int(moment.timestamp())


# --- Confederation request (P-07) -------------------------------------------

def request_hash(body: dict) -> str:
    return keccak_hex(jcs(body))


def request_message(domain: str, chain_id: int, issued_at: int, body: dict) -> str:
    require_match(DOMAIN_RE, domain, "domain")
    require_chain_id(chain_id)
    require_unix(issued_at, "issued_at")
    return "\n".join([
        REQUEST_HEADER,
        "Domain: %s" % domain,
        "Chain ID: %d" % chain_id,
        "Issued at: %d" % issued_at,
        "Request hash: %s" % request_hash(body),
    ])


# --- Work key signature (wsig) ----------------------------------------------

def wsig_message(deployment: str, chain_id: int, work: str, code: str, exp: int) -> str:
    require_match(DEPLOYMENT_RE, deployment, "deployment")
    require_chain_id(chain_id)
    require_checksum(work, "work address")
    require_match(CODE_RE, code, "code")
    require_unix(exp, "exp")
    return "\n".join([
        WSIG_HEADER,
        "Deployment: %s" % deployment,
        "Chain ID: %d" % chain_id,
        "Work address: %s" % work,
        "Code: %s" % code,
        "Expires: %d" % exp,
    ])


def wsig_resource(wsig_signature: str) -> str:
    sig = wsig_signature.lower()
    if not re.match(r"^0x[0-9a-f]{130}$", sig):
        raise FormatError("bad wsig signature")
    return "urn:satoshifi:wsig:" + keccak_hex(bytes.fromhex(sig[2:]))


# --- SIWE (EIP-4361) ---------------------------------------------------------

def siwe_message(domain: str, address: str, statement: str, uri: str, chain_id: int,
                 nonce: str, issued_at: int, expiration: int, resources=()) -> str:
    require_match(DOMAIN_RE, domain, "domain")
    require_checksum(address)
    if not isinstance(statement, str) or not statement or "\n" in statement:
        raise FormatError("bad statement")
    if not isinstance(uri, str) or not uri.startswith("https://") or any(c.isspace() for c in uri):
        raise FormatError("uri must be an https URL without spaces")
    require_chain_id(chain_id)
    if not isinstance(nonce, str) or not re.match(r"^[A-Za-z0-9]{8,}$", nonce):
        raise FormatError("bad nonce")
    require_unix(issued_at, "issued_at")
    require_unix(expiration, "expiration")
    lines = [
        "%s wants you to sign in with your Ethereum account:" % domain,
        address,
        "",
        statement,
        "",
        "URI: %s" % uri,
        "Version: 1",
        "Chain ID: %d" % chain_id,
        "Nonce: %s" % nonce,
        "Issued At: %s" % rfc3339(issued_at),
        "Expiration Time: %s" % rfc3339(expiration),
    ]
    resources = list(resources)
    if resources:
        lines.append("Resources:")
        for item in resources:
            if not isinstance(item, str) or not item or any(c.isspace() for c in item):
                raise FormatError("bad resource")
            lines.append("- %s" % item)
    return "\n".join(lines)


_SIWE_RE = re.compile(
    r"^(?P<domain>\S+) wants you to sign in with your Ethereum account:\n"
    r"(?P<address>0x[0-9a-fA-F]{40})\n\n"
    r"(?P<statement>[^\n]+)\n\n"
    r"URI: (?P<uri>\S+)\n"
    r"Version: 1\n"
    r"Chain ID: (?P<chain_id>[1-9][0-9]*)\n"
    r"Nonce: (?P<nonce>[A-Za-z0-9]{8,})\n"
    r"Issued At: (?P<issued_at>\S+)\n"
    r"Expiration Time: (?P<expiration>\S+)"
    r"(?P<resources>\nResources:(?:\n- \S+)+)?$"
)


def siwe_parse(text: str) -> dict:
    """Parse a SIWE message produced by siwe_message; anything else is rejected."""
    if not isinstance(text, str):
        raise FormatError("SIWE message must be a string")
    match = _SIWE_RE.match(text)
    if not match:
        raise FormatError("SIWE message does not match the expected layout")
    fields = {
        "domain": match["domain"],
        "address": match["address"],
        "statement": match["statement"],
        "uri": match["uri"],
        "chain_id": int(match["chain_id"]),
        "nonce": match["nonce"],
        "issued_at": from_rfc3339(match["issued_at"]),
        "expiration": from_rfc3339(match["expiration"]),
        "resources": [],
    }
    if match["resources"]:
        fields["resources"] = [line[2:] for line in match["resources"].split("\n")[2:]]
    if siwe_message(**fields) != text:
        raise FormatError("SIWE message is not in canonical form")
    return fields


def login_statement() -> str:
    return LOGIN_STATEMENT


def binding_statement(deployment: str, work: str) -> str:
    require_match(DEPLOYMENT_RE, deployment, "deployment")
    require_checksum(work, "work address")
    return "My work address for %s is %s." % (deployment, work)


# --- Signed API requests (services and operator) -----------------------------

def api_string(method: str, path: str, timestamp: int, nonce: str, body: bytes) -> str:
    if method not in METHODS:
        raise FormatError("bad method")
    if not isinstance(path, str) or not path.startswith("/v1/") or any(c.isspace() for c in path):
        raise FormatError("bad path")
    require_unix(timestamp, "timestamp")
    require_match(API_NONCE_RE, nonce, "nonce")
    if not isinstance(body, (bytes, bytearray)):
        raise FormatError("body must be bytes")
    return "\n".join([
        API_HEADER,
        "%s %s" % (method, path),
        "%d" % timestamp,
        nonce,
        keccak_hex(bytes(body)),
    ])
