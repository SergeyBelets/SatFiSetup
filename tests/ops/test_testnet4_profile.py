"""Testnet4 preparation authenticates source inventory independently of manifests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from ops.zkspv import testnet4_source as s
from ops.zkspv.testnet4_contracts import generated
from ops import package_distribution as distribution
from signer_agent.zk_profiles import TESTNET4_IMAGE, profile


def test_generated_contracts_have_no_template_drift():
    for path,text in generated():assert path.read_text()==text


def test_static_program_is_deliverable_and_matches_verified_build_pin():
    p=profile({'network':'testnet4'})
    report=json.loads(p['report'].read_text())
    assert p['image']==TESTNET4_IMAGE
    assert hashlib.sha256(p['program'].read_bytes()).hexdigest()==report['evidence_sha256'][p['program'].name]
    assert distribution.reason(distribution.TESTNET4_GUEST) is None
    assert distribution.reason('rust/zkspv/testnet4-integration/evidence/receipt.bin')


def test_prepared_manifest_cannot_rewrite_its_own_source_hashes(tmp_path,monkeypatch):
    # Isolate expected_files: the expected inventory is independently derived
    # from repository and pinned upstream, rather than supplied by the manifest.
    data=b'authentic source';digest=hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(s,'expected_files',lambda upstream:{'kernel.rs':digest})
    manifest=dict(schema='satoshifi.testnet4-prepared-source.v1',bitcoin_network='testnet4',
        upstream_files={},source_sha256={},prepared_sha256={'kernel.rs':digest})
    (tmp_path/'kernel.rs').write_bytes(data)
    s.verify_prepared(tmp_path,manifest)
    (tmp_path/'kernel.rs').write_bytes(b'malicious verifier')
    tampered=deepcopy(manifest);tampered['prepared_sha256']['kernel.rs']=s.sha(tmp_path/'kernel.rs')
    with pytest.raises(ValueError,match='MANIFEST_UNTRUSTED'):s.verify_prepared(tmp_path,tampered)
    with pytest.raises(ValueError,match='PREPARED_DRIFT'):s.verify_prepared(tmp_path,manifest)
    (tmp_path/'kernel.rs').write_bytes(data);(tmp_path/'build.rs').write_text('unapproved build hook')
    with pytest.raises(ValueError,match='PREPARED_INVENTORY'):s.verify_prepared(tmp_path,manifest)


def test_wrong_upstream_inventory_cannot_authorize_prepared_source():
    with pytest.raises(ValueError,match='UPSTREAM_PIN'):s.expected_files({})


def test_testnet_preflight_does_not_accept_mainnet_image(tmp_path):
    from ops.zkspv.preflight import preflight
    from signer_agent.zk_profiles import MAINNET_IMAGE
    c=dict(profile='ZK_SPV_V1',evm_chain_id=11155111,bitcoin_network='testnet4',program_id=MAINNET_IMAGE)
    result=preflight(c,{},tmp_path)
    assert 'program_id:testnet4_image_required' in result['blockers']
    c['program_id']=TESTNET4_IMAGE
    result=preflight(c,{},tmp_path)
    assert 'program_id:testnet4_image_required' not in result['blockers']
    assert not result['production_ready'] and not result['package_checks_pass']
