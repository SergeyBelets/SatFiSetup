#!/usr/bin/env python3
"""Apply the exact experimental series to a temporary copy, never to vendor.

Both every input/output source hash and every patch hash must match series.json.
The resulting file is for review/testing only, not a qualified verifier.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT.parent / "vendor/citrea-2ed54463/crates/bitcoin-da/src/verifier.rs"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def patched_source(source=DEFAULT_SOURCE):
    manifest = json.loads((ROOT / "series.json").read_text())
    original = Path(source).read_bytes()
    if digest(original) != manifest["original_sha256"]:
        raise ValueError("Pinned upstream source hash mismatch")
    with tempfile.TemporaryDirectory(prefix="satoshifi-arithmetic-patch-") as folder:
        destination = Path(folder) / manifest["source_path"]
        destination.parent.mkdir(parents=True)
        destination.write_bytes(original)
        for item in manifest["series"]:
            patch = ROOT / item["file"]
            if digest(patch.read_bytes()) != item["sha256"]:
                raise ValueError("Patch hash mismatch: " + item["file"])
            if digest(destination.read_bytes()) != item["input_sha256"]:
                raise ValueError("Patch input mismatch: " + item["file"])
            subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-d", folder,
                            "-i", str(patch)], check=True, capture_output=True)
            if digest(destination.read_bytes()) != item["output_sha256"]:
                raise ValueError("Patch output mismatch: " + item["file"])
        result = destination.read_bytes()
    if digest(result) != manifest["patched_sha256"]:
        raise ValueError("Patched source hash mismatch")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, required=True,
                        help="new file; existing files are never overwritten")
    args = parser.parse_args()
    result = patched_source(args.source)
    with args.output.open("xb") as output:
        output.write(result)
    print(f"Wrote test-only patched source: {digest(result)}")


if __name__ == "__main__":
    main()
