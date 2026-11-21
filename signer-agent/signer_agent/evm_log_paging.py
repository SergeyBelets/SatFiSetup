"""Complete, bounded log pages on RPCs with smaller range/response-size limits.

Provider text is used only to classify a known limit; it is never logged. A
failed page is never skipped or converted into an empty successful result.
"""
import re

from requests.exceptions import HTTPError
from web3.exceptions import Web3RPCError


def range_limit(error):
    if isinstance(error, HTTPError):
        return getattr(error.response, "status_code", None) == 413
    if isinstance(error, Web3RPCError):
        response = error.rpc_response
        detail = response.get("error") if isinstance(response, dict) else None
    elif type(error) is ValueError and len(error.args) == 1:
        # Older Web3 providers expose the structured JSON-RPC error as args[0].
        detail = error.args[0]
    else:
        return False
    if (not isinstance(detail, dict) or type(detail.get("code")) is not int
            or not isinstance(detail.get("message"), str)):
        return False
    message = detail["message"][:4096].lower()
    if any(phrase in message for phrase in (
        "query returned more than", "too many results", "result set too large",
        "response size exceeded", "log response size exceeded", "response too large",
        "response size is larger", "query exceeds max results", "response size limit",
    )):
        return True
    block_range = re.search(r"\bblocks?\s+range\b|\brange\s+of\s+blocks?\b|\bblocks?\s+per\s+query\b", message)
    return bool(block_range and any(term in message for term in (
        "limit", "exceed", "maximum", "max ", "too wide", "too large", "up to", "at most")))


def log_pages(w3, first, last, *, topics, initial_span=2000, address=None):
    if type(first) is not int or type(last) is not int or not 0 <= first <= last:
        raise ValueError("INVALID_FROM_BLOCK")
    if type(initial_span) is not int or initial_span < 1:
        raise ValueError("INVALID_LIST_PAGE")
    start, span = first, initial_span
    filters = {"topics": topics}
    if address is not None:
        filters["address"] = address
    while start <= last:
        end = min(start + span - 1, last)
        try:
            rows = w3.eth.get_logs(dict(fromBlock=start, toBlock=end, **filters))
        except Exception as error:
            if end == start or not range_limit(error):
                raise
            span = max(1, (end - start + 1) // 2)
            continue
        yield rows
        start = end + 1
