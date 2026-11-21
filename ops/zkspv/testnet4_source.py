"""Derive an isolated testnet4 build from the frozen, source-verified kernel.

The original mainnet sources, image and reports remain byte-for-byte intact.
Network domains are changed only in prepared copies, never by witness input.
"""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
ZK = ROOT / 'rust/zkspv'
DOMAINS = {
    'fork-policy/src/lib.rs': [('b"mainnet-anchor"', 'b"testnet4-anchor"'), ('b"bitcoin-mainnet"', 'b"bitcoin-testnet4"')],
    'checkpoint-policy/src/checkpoint.rs': [('b"mainnet-checkpoint-v1"', 'b"testnet4-checkpoint-v1"'), ('b"bitcoin-mainnet"', 'b"bitcoin-testnet4"')],
    'resume-policy/src/bindings.rs': [('b"mainnet-anchor"', 'b"testnet4-anchor"'), ('b"bitcoin-mainnet"', 'b"bitcoin-testnet4"')],
    'resume-transition/src/transition.rs': [('b"bitcoin-mainnet"', 'b"bitcoin-testnet4"')],
}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def expected_files(upstream):
    """Authenticate supplied manifests against source pins, not against themselves."""
    from .participant_build import load_module
    patch=load_module('testnet4_pinned_patch',ZK/'header-qualification/prepare.py')
    corrected=patch.checked_sources()[2].encode()
    newer=corrected.replace(b'latest_block_time + 1200',b'latest_block_time.saturating_add(1200)')
    verifier='crates/bitcoin-da/src/verifier.rs'
    if upstream.get(verifier)!=hashlib.sha256(newer).hexdigest():
        raise ValueError('TESTNET4_UPSTREAM_PIN')
    qualified=dict(upstream);qualified[verifier]=hashlib.sha256(corrected).hexdigest()
    original=json.loads((ZK/'guest-integration/report.json').read_text())
    if hashlib.sha256(json.dumps(qualified,sort_keys=True).encode()).hexdigest()!=original['prepared_upstream_sha256']:
        raise ValueError('TESTNET4_UPSTREAM_PIN')
    result={'vendor-native/'+name:digest for name,digest in upstream.items()}
    for name in ('time-policy','fork-policy','checkpoint-policy','resume-policy','resume-transition',
                 'testnet4-integration','testnet4-worker-host'):
        for p in (ZK/name).rglob('*'):
            if not p.is_file() or any(v in ('target','evidence','__pycache__') for v in p.relative_to(ZK/name).parts):continue
            if p.name.endswith('report.json'):continue
            relative=str(p.relative_to(ZK));data=p.read_bytes()
            for old,new in DOMAINS.get(relative,[]):
                if data.count(old.encode())!=1:raise ValueError('TESTNET4_DERIVATION_DRIFT:'+relative)
                data=data.replace(old.encode(),new.encode())
            result[relative]=hashlib.sha256(data).hexdigest()
    for p in (ZK/'guest-integration/image-tool').rglob('*'):
        if p.is_file() and 'target' not in p.relative_to(ZK/'guest-integration/image-tool').parts:
            result['testnet4-integration/image-tool/'+str(p.relative_to(ZK/'guest-integration/image-tool'))]=sha(p)
    for name in ('headers','expected'):
        filename=f'{name}-2006-4050.bin'
        result['testnet4-integration/fixtures/'+filename]=sha(ROOT/'web3/test/fixtures/testnet4'/filename)
    return result


def prepare(source, archive, output):
    from .participant_build import prepare as prepare_mainnet
    output = Path(output).resolve()
    original = prepare_mainnet(source, archive, output)
    derivations = []
    for relative, replacements in DOMAINS.items():
        path = output / relative
        before = sha(path)
        text = path.read_text()
        for old, new in replacements:
            if text.count(old) != 1:
                raise ValueError('TESTNET4_DERIVATION_DRIFT:' + relative)
            text = text.replace(old, new)
        path.write_text(text)
        derivations.append(dict(path=relative, before=before, after=sha(path)))
    # Consensus uses u64 timestamps for the 20-minute comparison in Core.
    # Saturation is equivalent for u32 header timestamps and cannot wrap.
    verifier = output / 'vendor-native/crates/bitcoin-da/src/verifier.rs'
    text = verifier.read_text()
    old = 'latest_block_time + 1200'
    if text.count(old) != 1:
        raise ValueError('TESTNET4_TIME_COMPARISON_DRIFT')
    verifier.write_text(text.replace(old, 'latest_block_time.saturating_add(1200)'))
    original['upstream_files']['crates/bitcoin-da/src/verifier.rs'] = sha(verifier)
    # Only the fixed testnet4 kernel is buildable in this prepared profile.
    shutil.rmtree(output / 'guest-integration')
    shutil.rmtree(output / 'worker-host')
    sources = dict(original['source_sha256'])
    for name in ('testnet4-integration', 'testnet4-worker-host'):
        for path in (ZK / name).rglob('*'):
            if not path.is_file() or any(v in ('target', 'evidence', '__pycache__') for v in path.relative_to(ZK / name).parts):
                continue
            if path.name == 'report.json':
                continue
            dest = output / path.relative_to(ZK)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            sources[str(path.relative_to(ROOT))] = sha(path)
    image = ZK / 'guest-integration/image-tool'
    shutil.copytree(image, output / 'testnet4-integration/image-tool', ignore=shutil.ignore_patterns('target'))
    for name in ('headers', 'expected'):
        path = ROOT / f'web3/test/fixtures/testnet4/{name}-2006-4050.bin'
        dest = output / 'testnet4-integration/fixtures' / path.name
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, dest)
        sources[str(path.relative_to(ROOT))] = sha(path)
    manifest = dict(original, schema='satoshifi.testnet4-prepared-source.v1', bitcoin_network='testnet4',
        source_sha256=sources, derivations=derivations,
        additional_consensus_rules=['BIP94 epoch timestamp guard in static adapter', 'overflow-safe 20-minute comparison'])
    manifest['prepared_sha256'] = {str(p.relative_to(output)): sha(p) for p in output.rglob('*')
        if p.is_file() and p.name != 'prepared-source.json'}
    (output / 'prepared-source.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def verify_prepared(prepared, manifest):
    prepared = Path(prepared)
    if manifest.get('schema') != 'satoshifi.testnet4-prepared-source.v1' or manifest.get('bitcoin_network') != 'testnet4':
        raise ValueError('TESTNET4_PREPARED_PROFILE')
    expected=expected_files(manifest['upstream_files'])
    if manifest['prepared_sha256']!=expected:
        raise ValueError('TESTNET4_PREPARED_MANIFEST_UNTRUSTED')
    actual={str(p.relative_to(prepared)) for p in prepared.rglob('*') if p.is_file() and p.name!='prepared-source.json'}
    if actual!=set(expected):raise ValueError('TESTNET4_PREPARED_INVENTORY')
    for name, digest in manifest['source_sha256'].items():
        if sha(ROOT / name) != digest:
            raise ValueError('TESTNET4_SOURCE_DRIFT:' + name)
    for name, digest in manifest['prepared_sha256'].items():
        path=prepared/name
        if path.is_symlink() or not path.resolve().is_relative_to(prepared.resolve()) or sha(path) != digest:
            raise ValueError('TESTNET4_PREPARED_DRIFT:' + name)
