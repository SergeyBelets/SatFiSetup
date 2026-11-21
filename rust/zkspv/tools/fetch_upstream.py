#!/usr/bin/env python3
"""Download the exact qualification archive, verify it, and extract to a new directory."""
import argparse
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

COMMIT = "2ed54463bb7c4c3f1ff29dcd66d4b28c26fb63c8"
SHA256 = "591197b7a10896859d873a757d53ae39edd541414702b17c15a82d3f4d0b12c6"
URL = "https://codeload.github.com/chainwayxyz/citrea/tar.gz/" + COMMIT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new directory; never overwrite an existing checkout")
    args = parser.parse_args()
    if args.destination.exists():
        raise SystemExit("Destination already exists; refusing to overwrite")
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != SHA256:
        raise SystemExit("Archive SHA-256 mismatch; no source extracted")
    prefix = "citrea-" + COMMIT + "/"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        members = [member for member in members if not (member.isdir() and member.name == prefix[:-1])]
        for member in members:
            relative = member.name.removeprefix(prefix)
            if not member.name.startswith(prefix) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise SystemExit("Unsafe archive path")
            if not member.isdir() and not member.isfile():
                raise SystemExit("Unsupported archive entry type")
        args.destination.mkdir(parents=True, exist_ok=False)
        for member in members:
            path = args.destination / member.name.removeprefix(prefix)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as stream:
                    path.write_bytes(stream.read())
                path.chmod(member.mode & 0o777)
    print(f"Verified {SHA256}; extracted unchanged commit {COMMIT} into {args.destination}")


if __name__ == "__main__":
    main()
