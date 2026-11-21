"""Diagnostic identifiers that never stringify exceptions carrying credentials."""

import argparse
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import sys


_debug = ContextVar("cli_debug", default=False)
_PACKAGE = Path(__file__).resolve().parent


class DiagnosticArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        if not _debug.get():
            super().error(message)
        _emit(dict(status="DEBUG_ERROR", code="INVALID_CLI_ARGUMENTS", frames=[]))
        self.exit(2)


def exception_code(exc):
    name = type(exc).__name__
    return (
        name
        if name.isascii() and name.isidentifier() and len(name) <= 64
        else "CLIENT_ERROR"
    )


@contextmanager
def debug_diagnostics(enabled):
    token = _debug.set(enabled or _debug.get())
    try:
        yield
    finally:
        _debug.reset(token)


def _locations(trace):
    frames = deque(maxlen=12)
    while trace is not None:
        code = trace.tb_frame.f_code
        path = Path(code.co_filename)
        if path.is_relative_to(_PACKAGE) and path.suffix == ".py":
            parts = path.relative_to(_PACKAGE).with_suffix("").parts
            if all(part.isascii() and part.isidentifier() for part in parts):
                name = code.co_name
                frames.append(dict(
                    module=".".join(("signer_agent", *parts)),
                    function=name if name.isascii() and name.isidentifier() else "<module>",
                    line=trace.tb_lineno,
                ))
        trace = trace.tb_next
    return list(frames)


def _transport_codes(exc):
    from requests.exceptions import HTTPError
    from web3.exceptions import Web3RPCError
    from .bitcoin_rpc import BitcoinRPCError

    status = rpc_code = None
    if isinstance(exc, HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    elif type(exc) is BitcoinRPCError:
        status, rpc_code = exc.http_status, exc.rpc_code
    elif isinstance(exc, Web3RPCError):
        response = getattr(exc, "rpc_response", None)
        error = response.get("error") if type(response) is dict else None
        if type(error) is dict:
            rpc_code = error.get("code")
    result = {}
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    if type(rpc_code) is int and -40000 <= rpc_code < 0:
        result["rpc_error_code"] = rpc_code
    return result


def _emit(record):
    try:
        print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)
    except OSError:
        # A failed diagnostic sink must not change the operation's result.
        pass


def emit_debug(exc, *, code=None):
    """Emit only error identifiers, SDK code locations and bounded numeric codes."""
    if not _debug.get():
        return
    record = dict(status="DEBUG_ERROR", code=code or exception_code(exc),
                  frames=_locations(exc.__traceback__), **_transport_codes(exc))
    seen = {id(exc)}
    causes = []
    cause = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen and len(causes) < 3:
        seen.add(id(cause))
        causes.append(dict(code=exception_code(cause), frames=_locations(cause.__traceback__),
                           **_transport_codes(cause)))
        cause = cause.__cause__ or cause.__context__
    if causes:
        record["causes"] = causes
    _emit(record)
