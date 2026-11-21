#!/usr/bin/env python3
"""Verify every retained upstream file against the reviewed source lock."""
import hashlib
import json
from pathlib import Path


def main():
    repo = Path(__file__).resolve().parents[3]
    lock = json.loads((repo / "ops/artifacts/zkspv/upstream-lock.json").read_text())
    failures = []
    entries = lock["files"] + lock.get("historical_verifiers", [])
    for entry in entries:
        path = (repo / entry["path"]).resolve()
        if repo not in path.parents:
            failures.append(entry["path"] + ": outside repository")
            continue
        try:
            data = path.read_bytes()
        except OSError as error:
            failures.append(entry["path"] + ": " + str(error))
            continue
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            failures.append(entry["path"] + ": bytes/hash differ")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Verified {len(entries)} pinned upstream files; this is source integrity, not qualification.")


if __name__ == "__main__":
    main()
