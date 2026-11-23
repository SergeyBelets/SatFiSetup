"""Local SQLite retention accounting shared by SPV journals."""

import re


def history_limit(config):
    value = config.get("history_max_bytes")
    if value is None:
        return None
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,12}", value)
            or not 8 * 1024**2 <= int(value) <= 2**40):
        raise ValueError("SPV_HISTORY_LIMIT")
    return int(value)


def sqlite_bytes(directory, name):
    size = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            size += (directory / (name + suffix)).stat().st_size
        except FileNotFoundError:
            continue
    return size


def require_history_space(directory, name, limit):
    if limit is not None and sqlite_bytes(directory, name) >= limit:
        raise ValueError("SPV_HISTORY_STORAGE_LIMIT")
