"""Cookie-authenticated Bitcoin Core RPC with explicit local network selection.

Endpoint routing and credentials are participant-local. They never belong in an
invitation or signing packet. In particular, redirects and environment proxies
must not forward the cookie to another endpoint.
"""

import base64
from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
import ipaddress
import json
import os
import re
import ssl
import stat
import urllib.parse
import urllib.error
import urllib.request
import uuid

import certifi


MAX_COOKIE_BYTES = 4096
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


@lru_cache(maxsize=1)
def _public_tls():
    return ssl.create_default_context(cafile=certifi.where())


def satoshis(value):
    """Convert a Core JSON amount without floating point, truncation or allocation attacks."""
    if type(value) is int:
        if not 0 <= value <= 21_000_000:
            raise ValueError("UTXO_AMOUNT_INVALID")
        return value * 100_000_000
    if type(value) is not Decimal or not value.is_finite() or value < 0 or value > 21_000_000:
        raise ValueError("UTXO_AMOUNT_INVALID")
    _, digits, exponent = value.as_tuple()
    if not any(digits):
        return 0
    # Do not round in the process-wide Decimal context. In particular, a value
    # just above one satoshi may contain significant digits beyond its precision.
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
        exponent += 1
    if exponent < -8:
        raise ValueError("UTXO_AMOUNT_INVALID")
    coefficient = 0
    for digit in digits[:end]:
        coefficient = coefficient * 10 + digit
    return coefficient * 10 ** (exponent + 8)


class _RPCFailure(Exception):
    """Internal errors carry only a fixed public code, never remote text."""


class BitcoinRPCError(RuntimeError):
    """Redacted public failure retaining only the numeric JSON-RPC error code."""

    def __init__(self, code, rpc_code=None, http_status=None):
        super().__init__(code)
        self.rpc_code = rpc_code
        self.http_status = http_status


def observation_error(error):
    """Safe structured diagnostics; never persist provider text or request data."""
    from .participant_errors import participant_exception_code

    code_error = (RuntimeError(error.args[0]) if type(error) is BitcoinRPCError
                  and len(error.args) == 1 else error)
    result = {"error_code": participant_exception_code(code_error)}
    if type(error) is BitcoinRPCError:
        # The constructor is public; repeat validation at the logging boundary.
        if type(error.rpc_code) is int and -(2**31) <= error.rpc_code < 2**31:
            result["rpc_error_code"] = error.rpc_code
        if type(error.http_status) is int and 100 <= error.http_status <= 599:
            result["http_status"] = error.http_status
    return result


def payment_in_mempool(bitcoin, txid):
    try:
        entry = bitcoin.call("getmempoolentry", txid)
    except BitcoinRPCError as error:
        if error.rpc_code != -5:
            raise
        return False
    if not isinstance(entry, dict) or type(entry.get("vsize")) is not int or entry["vsize"] <= 0:
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    return True


def payment_block(transaction, txid, depth, *, bitcoin):
    """Return the payment's named block, or None for an observed loss of depth."""
    if (not isinstance(transaction, dict) or transaction.get("txid") != txid
            or not isinstance(transaction.get("hex"), str) or not transaction["hex"]):
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    block = transaction.get("blockhash")
    confirmations = transaction.get("confirmations")
    if "confirmations" not in transaction and "blockhash" not in transaction:
        # Core omits both fields for mempool transactions, but a partial RPC
        # response can have the same shape. Confirm mempool membership first.
        if payment_in_mempool(bitcoin, txid):
            return None
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    if (type(confirmations) is not int
            or confirmations > 0 and (not isinstance(block, str) or not re.fullmatch(r"[0-9a-f]{64}", block))):
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    return block if confirmations >= depth else None


def payment_block_height(bitcoin, block):
    header = bitcoin.call("getblockheader", block)
    height = header.get("height") if isinstance(header, dict) else None
    if type(height) is not int or height < 0:
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    check_payment_block(bitcoin, block, height)
    return height


def check_payment_block(bitcoin, block, height):
    current = bitcoin.call("getblockhash", height)
    if not isinstance(current, str) or not re.fullmatch(r"[0-9a-f]{64}", current):
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    if current != block:
        raise ValueError("BITCOIN_PAYMENT_REORG")


def check_known_payment_anchor(bitcoin, block):
    """Check the original settlement block even when Core cannot look up its txid."""
    if not isinstance(block, str) or not re.fullmatch(r"[0-9a-f]{64}", block) or block == "0" * 64:
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    header = bitcoin.call("getblockheader", block)
    if not isinstance(header, dict) or header.get("hash") != block:
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    height = header.get("height")
    if type(height) is not int or height < 0:
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    # A node can know the old header before it has caught up to that height.
    # A side-header status alone does not establish a conflicting active block.
    check_payment_block(bitcoin, block, height)


def verify_payment_proof(bitcoin, txid, block):
    proof = bitcoin.call("gettxoutproof", [txid], block)
    if not isinstance(proof, str) or not proof:
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    matches = bitcoin.call("verifytxoutproof", proof)
    if not isinstance(matches, list) or any(not isinstance(item, str) for item in matches):
        raise ValueError("BITCOIN_OBSERVATION_UNAVAILABLE")
    if txid not in matches:
        raise ValueError("INVALID_PAYMENT_PROOF")
    return proof


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RPCFailure("BITCOIN_RPC_REDIRECT_REJECTED")


def _endpoint(url, network):
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("BITCOIN_RPC_URL_INVALID")
    try:
        parsed = urllib.parse.urlsplit(url)
        # Accessing port also validates malformed or out-of-range port numbers.
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme not in ("http", "https") or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or port == 0):
            raise ValueError
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if network in ("mainnet", "testnet4") and parsed.scheme == "http" and not loopback:
            raise _RPCFailure("BITCOIN_RPC_REMOTE_HTTP_FORBIDDEN")
    except _RPCFailure as error:
        raise ValueError(str(error)) from None
    except (TypeError, ValueError):
        raise ValueError("BITCOIN_RPC_URL_INVALID") from None
    return url


def _cookie(path, network):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > MAX_COOKIE_BYTES
                or (network in ("mainnet", "testnet4") and info.st_mode & 0o077)):
            raise ValueError
        value = os.read(fd, MAX_COOKIE_BYTES + 1)
        if len(value) > MAX_COOKIE_BYTES:
            raise ValueError
        value = value.strip()
        username, separator, password = value.partition(b":")
        if (not separator or not username or not password
                or any(c < 33 or c > 126 for c in value)):
            raise ValueError
        return base64.b64encode(value).decode("ascii")
    except (OSError, TypeError, ValueError):
        raise ValueError("BITCOIN_RPC_COOKIE_INVALID") from None
    finally:
        if fd is not None:
            os.close(fd)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _RPCFailure("BITCOIN_RPC_RESPONSE_INVALID")
        result[key] = value
    return result


def _invalid_number(_value):
    raise _RPCFailure("BITCOIN_RPC_RESPONSE_INVALID")


class BitcoinRPC:
    def __init__(self, url, cookie_file, network="regtest", policy=None):
        if network not in ("regtest", "mainnet", "testnet4"):
            raise ValueError("BITCOIN_NETWORK_UNSUPPORTED")
        self.network = network
        self.policy = deepcopy(policy)
        self.url = _endpoint(url, network)
        self._authorization = "Basic " + _cookie(cookie_file, network)
        # Empty proxy configuration deliberately ignores HTTP(S)_PROXY, ALL_PROXY
        # and a desktop proxy that might otherwise receive cookie credentials.
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if self.is_https:
            handlers.append(urllib.request.HTTPSHandler(context=_public_tls()))
        self._opener = urllib.request.build_opener(*handlers)

    @property
    def is_loopback(self):
        """Fast watch needs a numeric loopback address, without DNS resolution."""
        try:
            return ipaddress.ip_address(urllib.parse.urlsplit(self.url).hostname).is_loopback
        except ValueError:
            return False

    @property
    def is_https(self):
        return urllib.parse.urlsplit(self.url).scheme == "https"

    def call(self, method, *params):
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", method):
            raise ValueError("BITCOIN_RPC_METHOD_INVALID")
        request_id = uuid.uuid4().hex
        try:
            body = json.dumps(
                dict(jsonrpc="2.0", id=request_id, method=method, params=params),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError):
            raise ValueError("BITCOIN_RPC_REQUEST_INVALID") from None
        request = urllib.request.Request(
            self.url, body,
            {"Content-Type": "application/json", "Authorization": self._authorization},
        )
        http_status = None
        try:
            try:
                with self._opener.open(request, timeout=30) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as error:
                # Some Core/proxy deployments use HTTP 500 for JSON-RPC errors.
                # Read only the bounded envelope; never retain its message/data.
                http_status = error.code
                with error:
                    raw = error.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise _RPCFailure("BITCOIN_RPC_RESPONSE_TOO_LARGE")
            data = json.loads(raw, parse_float=Decimal, parse_constant=_invalid_number,
                              object_pairs_hook=_unique_object)
            if (not isinstance(data, dict) or data.get("jsonrpc") != "2.0"
                    or data.get("id") != request_id
                    or set(data) - {"jsonrpc", "id", "result", "error"}):
                raise _RPCFailure("BITCOIN_RPC_RESPONSE_INVALID")
            if data.get("error") is not None:
                error = data["error"]
                if ("result" in data and data["result"] is not None
                        or not isinstance(error, dict)
                        or type(error.get("code")) is not int
                        or not -(2**31) <= error["code"] < 2**31
                        or not isinstance(error.get("message"), str)
                        or set(error) - {"code", "message", "data"}):
                    raise _RPCFailure("BITCOIN_RPC_RESPONSE_INVALID")
                raise BitcoinRPCError("BITCOIN_RPC_REJECTED", error["code"], http_status)
            if "result" not in data:
                raise _RPCFailure("BITCOIN_RPC_RESPONSE_INVALID")
            if http_status is not None:
                raise BitcoinRPCError("BITCOIN_RPC_FAILED", http_status=http_status)
            return data["result"]
        except BitcoinRPCError:
            raise
        except _RPCFailure as error:
            raise BitcoinRPCError(str(error), http_status=http_status) from None
        except Exception:
            # Core/proxies can include arbitrary strings, paths, URLs and
            # credentials in errors. None of that crosses the CLI boundary.
            raise BitcoinRPCError("BITCOIN_RPC_FAILED", http_status=http_status) from None

    def check_network(self):
        from .bitcoin_network import check_core

        return check_core(self, self.network, self.policy)
