#!/usr/bin/env python3
"""Create a new, fully checked build tree; never mutate qualified source/vendor."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
ZK = HERE.parent
ROOT = ZK.parents[1]

def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare(source, archive, output):
    native = module("native", ZK / "header-qualification/qualify_native.py")
    original = native.verify_source(archive, source)
    patch = module("patch", ZK / "header-qualification/prepare.py")
    _, _, corrected = patch.checked_sources()
    corrected = corrected.encode()
    # Preserve the complete upstream crate graph, with exactly one file changed.
    if output.exists():
        raise ValueError("Output already exists; use a new build directory")
    parent = json.loads((ZK / "state-adapter/report.json").read_text())
    if parent["status"] != "LOCAL_COMPONENT_PASS":
        raise ValueError("Parent component not qualified locally")
    for name, digest in parent["source_sha256"].items():
        if sha(ROOT / name) != digest:
            raise ValueError("Parent evidence changed: " + name)
    output.mkdir(parents=True)
    upstream = output / "vendor-native"
    shutil.copytree(source, upstream)
    relative = "crates/bitcoin-da/src/verifier.rs"
    (upstream / relative).write_bytes(corrected)
    expected = dict(original)
    expected[relative] = hashlib.sha256(corrected).hexdigest()
    actual = {str(p.relative_to(upstream)): sha(p) for p in upstream.rglob("*") if p.is_file()}
    if actual != expected:
        raise ValueError("Prepared upstream differs beyond the approved correction series")
    inputs = {}
    for component in ("time-policy", "fork-policy", "checkpoint-policy", "resume-policy", "resume-transition", "guest-integration"):
        for path in (ZK / component).rglob("*"):
            if not path.is_file() or any(p in ("target", "evidence", "__pycache__") for p in path.relative_to(ZK / component).parts):
                continue
            if path.name == "report.json":
                continue
            target = output / path.relative_to(ZK)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            inputs[str(path.relative_to(ROOT))] = sha(path)
    for name in ("headers", "expected"):
        path = ROOT / f"web3/test/fixtures/mainnet/{name}-32246-36288.bin"
        target = output / "guest-integration/fixtures" / path.name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, target)
        inputs[str(path.relative_to(ROOT))] = sha(path)
    # Seed dependency resolution from the upstream lock, never latest packages.
    for target in (output / "guest-integration/Cargo.lock", output / "guest-integration/guest/Cargo.lock"):
        if not target.exists():
            lock = "guests/risc0/light-client-proof/bitcoin/Cargo.lock" if target.parent.name == "guest" else "Cargo.lock"
            shutil.copyfile(source / lock, target)
    manifest = dict(schema="satoshifi.guest-prepared-source.v1", upstream_commit=native.COMMIT,
        archive_sha256=native.ARCHIVE_SHA256, upstream_files=expected, source_sha256=inputs,
        modified_upstream_files=[relative], production_ready=False)
    (output / "prepared-source.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "archive", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    a = p.parse_args()
    result = prepare(a.source.resolve(), a.archive.resolve(), a.output.resolve())
    print(json.dumps(dict(upstream_files=len(result["upstream_files"]), modified=result["modified_upstream_files"])))
