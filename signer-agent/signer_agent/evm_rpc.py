"""Transport policy shared by EVM preflight and runtime clients."""

from ipaddress import ip_address
import logging
import re
from urllib.parse import urlsplit

import requests
from web3 import HTTPProvider, Web3


_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
                   r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?", re.IGNORECASE | re.ASCII)


def _loopback(host):
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def endpoint(url):
    """Validate a local configuration without disclosing its URI or connecting."""
    if (not isinstance(url, str) or not url or not url.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 or c in "\\#" for c in url)):
        raise ValueError("EVM_RPC_ENDPOINT")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("EVM_RPC_ENDPOINT") from None
    if (parsed.scheme not in ("http", "https") or not host or port == 0
            or parsed.netloc.endswith(":") or "%" in host or len(host) > 253):
        raise ValueError("EVM_RPC_ENDPOINT")
    try:
        ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host):
            raise ValueError("EVM_RPC_ENDPOINT") from None
    if parsed.scheme == "http" and not _loopback(host):
        raise ValueError("EVM_RPC_HTTPS_REQUIRED")
    return parsed


class _DirectSession(requests.Session):
    def __init__(self):
        super().__init__()
        # Disable environment/desktop proxies, netrc credentials and CA overrides.
        self.trust_env = False

    def post(self, url, **kwargs):
        try:
            response = super().post(url, allow_redirects=False, verify=True, **kwargs)
        except requests.RequestException:
            raise requests.ConnectionError("EVM_RPC_TRANSPORT") from None
        if not 200 <= response.status_code < 300:
            response.close()
            # Only the status is public evidence. The URI, headers and response body may contain credentials.
            public = requests.Response()
            public.status_code = response.status_code
            code = "EVM_RPC_REDIRECT" if 300 <= public.status_code < 400 else "EVM_RPC_HTTP_STATUS"
            raise requests.HTTPError(code, response=public)
        return response


class _Provider(HTTPProvider):
    # The parent logger includes the full URI and RPC response bodies at DEBUG.
    logger = logging.Logger(__name__, level=logging.WARNING)

    def __str__(self):
        return "EVM RPC connection"


def http_provider(url, *, timeout=10):
    endpoint(url)
    return _Provider(url, session=_DirectSession(), request_kwargs={"timeout": timeout},
                     exception_retry_configuration=None)


def evm_web3(url, *, timeout=10):
    return Web3(http_provider(url, timeout=timeout))


def witness_endpoint(config):
    """Allow one explicit, independently operated witness on a different authority."""
    primary = endpoint(config["evm_rpc"])
    if "evm_witness_rpc" not in config:
        return None
    url = config["evm_witness_rpc"]
    witness = endpoint(url)
    # Distinct provider credentials or paths on the same remote host do not add independence.
    if _loopback(primary.hostname) and _loopback(witness.hostname):
        same_authority = (primary.port or (80 if primary.scheme == "http" else 443)
                          ) == (witness.port or (80 if witness.scheme == "http" else 443))
    else:
        same_authority = primary.hostname.rstrip(".") == witness.hostname.rstrip(".")
    if same_authority:
        raise ValueError("EVM_RPC_WITNESS_NOT_INDEPENDENT")
    return url


def witness_web3(config, *, timeout=10):
    url = witness_endpoint(config)
    return evm_web3(url, timeout=timeout) if url else None
