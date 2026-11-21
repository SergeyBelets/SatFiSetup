"""Read a board's append-only intent index at a canonical confirmed snapshot."""
import json
from . import verified_cache


def decode_intent(raw):
    """Bound parsing and cache serialization; missing indexed data is corruption."""
    if not raw:
        raise ValueError("BTC_OPERATION_INTENT_REQUIRED")
    if len(raw) > 131072:
        return None
    try:
        candidate = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(candidate, dict):
        return None
    if len(raw)>32768 and not (isinstance(candidate.get('bitcoin_plan'),dict) and candidate['bitcoin_plan'].get('version')=='4'
            or candidate.get('artifact') in ('WithdrawalFeePayments','WithdrawalFeeFunding')):return None
    # json.loads may accept a depth that the cache envelope cannot serialize.
    # Iterative traversal leaves ample space for that envelope and CLI output.
    stack = [(candidate, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > 32:
            return None
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return candidate


def intent_rows(w3, board, block):
    number, block_hash = int(block["number"]), block["hash"].hex()
    def read(method, *args):
        return getattr(board.functions, method)(*args).call(block_identifier=number)
    cached = verified_cache.get(w3, "board-intents", [board.address])
    rows = []
    if cached:
        anchor = cached["anchor"]
        if w3.eth.get_block(int(anchor["number"]))["hash"].hex() != anchor["hash"]:
            raise ValueError("BTC_OPERATION_REORG")
        if int(anchor["number"]) <= number:
            rows = list(cached["rows"])
    count = int(read("intentCount"))
    if count < len(rows):
        raise ValueError("BTC_OPERATION_REORG")
    anchor = dict(number=str(number), hash=block_hash)
    for index in range(len(rows), count):
        session = str(read("intentSessions", index))
        raw = bytes(read("intents", int(session)))
        # An operator can publish arbitrary bytes. Retain an index tombstone for
        # unrelated invalid content, so it cannot poison discovery or be fetched
        # again forever. Never parse/store oversized data or suppress RPC errors.
        intent = decode_intent(raw)
        rows.append(dict(session=session, intent=intent, anchor=anchor))
    if w3.eth.get_block(number)["hash"].hex() != block_hash:
        raise ValueError("BTC_OPERATION_REORG")
    verified_cache.put(w3, "board-intents", [board.address], dict(anchor=anchor, rows=rows))
    return [row for row in rows if isinstance(row["intent"], dict)]
