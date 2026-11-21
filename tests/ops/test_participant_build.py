import hashlib
import json
from types import SimpleNamespace

import pytest
from ops.zkspv import participant_build as b


def fixture(tmp_path,monkeypatch):
    root=tmp_path/'repo';zk=root/'rust/zkspv';source=tmp_path/'upstream';source.mkdir()
    verifier=source/'crates/bitcoin-da/src/verifier.rs';verifier.parent.mkdir(parents=True)
    verifier.write_text('original verifier')
    original={'crates/bitcoin-da/src/verifier.rs':b.sha(verifier)}
    for name in ('time-policy','fork-policy','checkpoint-policy','resume-policy','resume-transition','guest-integration','worker-host'):
        folder=zk/name;folder.mkdir(parents=True);(folder/'Cargo.toml').write_text('# source')
    (zk/'guest-integration/evidence').mkdir();(zk/'guest-integration/evidence/receipt.bin').write_bytes(b'proof must not ship')
    (zk/'guest-integration/report.json').write_text(json.dumps({'source_sha256':{
        'rust/zkspv/worker-host/Cargo.toml':b.sha(zk/'worker-host/Cargo.toml')}}))
    fixtures=root/'web3/test/fixtures/mainnet';fixtures.mkdir(parents=True)
    for name in ('headers','expected'):(fixtures/f'{name}-32246-36288.bin').write_bytes(b'public bitcoin fixture')
    native=SimpleNamespace(COMMIT='pinned',ARCHIVE_SHA256='pinned-archive',verify_source=lambda a,s:original)
    patch=SimpleNamespace(checked_sources=lambda:('unused','unused','corrected verifier'))
    monkeypatch.setattr(b,'ROOT',root)
    monkeypatch.setattr(b,'load_module',lambda name,path:native if name=='participant_native' else patch)
    return root,source,tmp_path/'archive.tar.gz',tmp_path/'prepared'


def test_cold_preparation_copies_sources_and_public_fixtures_without_proofs(tmp_path,monkeypatch):
    root,source,archive,output=fixture(tmp_path,monkeypatch)
    manifest=b.prepare(source,archive,output)
    assert not manifest['proofs_included'] and not manifest['qualified']
    assert (output/'vendor-native/crates/bitcoin-da/src/verifier.rs').read_text()=='corrected verifier'
    assert not list(output.rglob('receipt.bin'))
    assert (output/'worker-host/Cargo.toml').read_bytes()==(root/'rust/zkspv/worker-host/Cargo.toml').read_bytes()
    with pytest.raises(ValueError,match='NEW_DIRECTORY_REQUIRED'):b.prepare(source,archive,output)


def test_cold_preparation_rejects_drift_before_creating_output(tmp_path,monkeypatch):
    root,source,archive,output=fixture(tmp_path,monkeypatch)
    (root/'rust/zkspv/worker-host/Cargo.toml').write_text('changed')
    with pytest.raises(ValueError,match='ZK_GUEST_SOURCE_DRIFT'):b.prepare(source,archive,output)
    assert not output.exists()
