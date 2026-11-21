"""Prepare the pinned native witness-host source tree on a new participant host.

Verifies source, not historical proofs. Does not modify old qualification reports
or infer that a new platform/toolchain is qualified for proving.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[2]


def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module);return module


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(source,archive,output):
    zk=ROOT/'rust/zkspv';source=Path(source).resolve();archive=Path(archive).resolve();output=Path(output).resolve()
    if output.exists() or output.is_relative_to(source):raise ValueError('PARTICIPANT_BUILD_NEW_DIRECTORY_REQUIRED')
    guest=json.loads((zk/'guest-integration/report.json').read_text())
    # Immutable guest/Rust inputs remain exact; unrelated historical SDK/test
    # revisions do not become a prerequisite for preparing a fresh native host.
    for name,digest in guest['source_sha256'].items():
        if name.startswith('rust/') and not name.startswith('rust/zkspv/state-adapter/') and sha(ROOT/name)!=digest:
            raise ValueError('ZK_GUEST_SOURCE_DRIFT:'+name)
    native=load_module('participant_native',zk/'header-qualification/qualify_native.py')
    original=native.verify_source(archive,source)
    patch=load_module('participant_patch',zk/'header-qualification/prepare.py')
    _,_,corrected=patch.checked_sources()
    output.mkdir(parents=True)
    upstream=output/'vendor-native';shutil.copytree(source,upstream)
    changed='crates/bitcoin-da/src/verifier.rs';(upstream/changed).write_text(corrected)
    expected=dict(original);expected[changed]=sha(upstream/changed)
    actual={str(p.relative_to(upstream)):sha(p) for p in upstream.rglob('*') if p.is_file()}
    if actual!=expected:raise ValueError('PARTICIPANT_UPSTREAM_DRIFT')
    inputs={}
    for name in ('time-policy','fork-policy','checkpoint-policy','resume-policy','resume-transition','guest-integration','worker-host'):
        folder=zk/name
        for path in folder.rglob('*'):
            if not path.is_file() or any(p in ('evidence','target','__pycache__') for p in path.relative_to(folder).parts):continue
            if path.name.endswith('report.json'):continue
            destination=output/path.relative_to(zk);destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,destination);inputs[str(path.relative_to(ROOT))]=sha(path)
    for name in ('headers','expected'):
        path=ROOT/f'web3/test/fixtures/mainnet/{name}-32246-36288.bin'
        destination=output/'guest-integration/fixtures'/path.name;destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(path,destination);inputs[str(path.relative_to(ROOT))]=sha(path)
    for name,digest in guest['source_sha256'].items():
        if name.startswith('rust/') and not name.startswith('rust/zkspv/state-adapter/') and sha(ROOT/name)!=digest:
            raise ValueError('ZK_GUEST_SOURCE_DRIFT:'+name)
    for name,digest in inputs.items():
        if sha(ROOT/name)!=digest:raise ValueError('PARTICIPANT_SOURCE_CHANGED:'+name)
    manifest=dict(schema='satoshifi.participant-prepared-source.v1',upstream_commit=native.COMMIT,
        archive_sha256=native.ARCHIVE_SHA256,upstream_files=expected,source_sha256=inputs,
        modified_upstream_files=[changed],proofs_included=False,qualified=False,production_ready=False)
    (output/'prepared-source.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','archive','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--bitcoin-network',choices=('mainnet','testnet4'),default='mainnet')
    a=parser.parse_args(argv)
    if a.bitcoin_network=='testnet4':
        from .testnet4_source import prepare as prepare_selected
    else:prepare_selected=prepare
    result=prepare_selected(a.source,a.archive,a.output)
    print(json.dumps(dict(status='NATIVE_SOURCE_PREPARED',files=len(result['upstream_files']),proofs_generated=False,qualified=False)))


if __name__=='__main__':main()
