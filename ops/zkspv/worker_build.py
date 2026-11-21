"""Build the native witness host against a verified prepared mainnet tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[2]

def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("prepared", "source-manifest", "cargo", "rustc", "cargo-home", "target", "report"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument('--bitcoin-network',choices=('mainnet','testnet4'),default='mainnet')
    a = p.parse_args()
    if a.bitcoin_network=='testnet4':
        from .testnet4_build import build
        a.action='host';a.output=a.report.parent/(a.report.stem+'-logs')
        a.guest_rustc=None;a.r0vm=None
        result=build(a)
        a.report.parent.mkdir(parents=True,exist_ok=True)
        a.report.write_text(json.dumps(result,indent=2)+'\n')
        return
    manifest = json.loads(a.source_manifest.read_text())
    for name, digest in manifest["upstream_files"].items():
        if sha(a.prepared / "vendor-native" / name) != digest:
            raise ValueError("PREPARED_VENDOR_DRIFT:" + name)
    sources = {}
    for component in ("time-policy", "fork-policy", "checkpoint-policy", "resume-policy", "resume-transition", "guest-integration"):
        for source in (ROOT / "rust/zkspv" / component).rglob("*.rs"):
            if any(part in ("evidence", "target") for part in source.parts): continue
            target = a.prepared / source.relative_to(ROOT / "rust/zkspv")
            if sha(source) != sha(target): raise ValueError("PREPARED_SOURCE_DRIFT:" + str(source))
            sources[str(source.relative_to(ROOT))] = sha(source)
    folder = ROOT / "rust/zkspv/worker-host"
    shutil.copytree(folder, a.prepared / "worker-host", dirs_exist_ok=True)
    env = dict(os.environ, CARGO_HOME=str(a.cargo_home), CARGO_TARGET_DIR=str(a.target), RUSTC=str(a.rustc))
    subprocess.run([a.cargo, "build", "--offline", "--locked", "--release", "--manifest-path",
                    a.prepared / "worker-host/Cargo.toml"], env=env, check=True)
    parent = tomllib.loads((ROOT / "rust/zkspv/guest-integration/Cargo.lock").read_text())["package"]
    pins = {(v["name"], v["version"], v.get("source")): v.get("checksum") for v in parent}
    for v in tomllib.loads((folder / "Cargo.lock").read_text())["package"]:
        if "source" in v and pins.get((v["name"], v["version"], v["source"])) != v.get("checksum"):
            raise ValueError("EXTERNAL_DEPENDENCY_DRIFT")
    for source in list(folder.rglob("*")) + [Path(__file__)]:
        if source.is_file(): sources[str(source.relative_to(ROOT))] = sha(source)
    binary = a.target / "release/satoshifi-zk-worker-host"
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(dict(status="BUILT", binary=str(binary), binary_sha256=sha(binary),
        compiler_sha256=sha(a.rustc), source_sha256=sources, upstream_files=manifest["upstream_files"]), indent=2) + "\n")

if __name__ == "__main__": main()
