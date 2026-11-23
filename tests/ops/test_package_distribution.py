import io
import json
from pathlib import Path
import tarfile

import pytest
from ops import package_distribution as d


@pytest.mark.parametrize('name',[
    'rust/guest/evidence/receipt.bin','rust/guest/evidence/state.journal','seal.bin',
    'ops/artifacts/old-package.tar.gz','runtime/alice/receipt-cache/proof.bin',
    'ops/transition-for-peers.zip','ops/groth16.seal',
    'web3/.env','private.sqlite','../escape.py','/absolute.py','frost-crypto/target/debug/native',
    'rust/state/out/Verifier.json','work/local-result/origin.json'])
def test_proofs_state_and_old_packages_are_not_distribution_files(name):assert d.reason(name)


def test_program_and_verifier_code_are_not_transition_proofs():
    assert d.reason(d.STATIC_GUEST) is None
    assert d.reason('rust/zkspv/state-adapter/contracts/ZkBitcoinStateV2.sol') is None
    assert d.reason('rust/zkspv/groth16-integration/src/main.rs') is None
    assert d.reason('rust/zkspv/state-adapter/contracts/vendor/groth16/Verifier.sol') is None


def test_report_byte_counts_are_metadata_not_proof_payloads():
    d.public_content('config/report.json',b'{"journal_bytes":1216,"proof_bytes":256}')


def test_renamed_ready_seal_cannot_hide_in_json():
    with pytest.raises(ValueError,match='PROOF_CONTENT'):d.public_content('config/example.json',json.dumps({'seal':'00'*260}).encode())


def test_inventory_never_reads_excluded_receipt_or_secret(tmp_path,monkeypatch):
    src=tmp_path/'code.py';src.write_text('# public code\n')
    proof=tmp_path/'receipt.bin';proof.write_bytes(b'do not ship')
    monkeypatch.setattr(d.subprocess,'check_output',lambda *a,**k:b'code.py\0receipt.bin\0')
    files,excluded=d.inventory(tmp_path)
    assert set(files)=={'code.py'} and excluded==[{'path':'receipt.bin','reason':'binary_or_proof'}]


def test_testnet4_inventory_only_auto_selects_exact_public_config_names(tmp_path, monkeypatch):
    local = 'config/testnet4-operator.json'
    public = 'config/zkspv-testnet4-worker.example.json'
    (tmp_path / 'config').mkdir()
    (tmp_path / local).write_text('{"evm_rpc":"https://rpc.example.invalid/private"}')
    (tmp_path / public).write_text('{"evm_rpc":"REPLACE_WITH_EVM_RPC","bitcoin_rpc":"http://127.0.0.1:48332"}')
    monkeypatch.setattr(d.subprocess, 'check_output', lambda *a, **k: b'')

    files, excluded = d.inventory(tmp_path)
    assert set(files) == {public}
    assert excluded == []

    files, excluded = d.inventory(tmp_path, extra=[local])
    assert set(files) == {public}
    assert excluded == [{'path': local, 'reason': 'private_or_proof_payload'}]


@pytest.mark.parametrize('endpoint', [
    'https://rpc.example.invalid',
    'http://user:fixture@localhost:48332',
    'http://127.0.0.1:48332/?access=fixture',
])
def test_even_an_allowlisted_example_cannot_ship_a_private_rpc(endpoint):
    with pytest.raises(ValueError, match='PRIVATE_OR_PROOF_CONTENT'):
        d.public_content('config/zkspv-testnet4-worker.example.json',
                         json.dumps({'rpc_sources': [{'bitcoin_rpc': endpoint}]}).encode())


def test_remote_launch_module_is_delivered_before_it_is_tracked(tmp_path, monkeypatch):
    paths = ('signer-agent/signer_agent/launch_configurator.py', 'signer-agent/signer_agent/launch_rpc.py')
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# public source\n')
    monkeypatch.setattr(d.subprocess, 'check_output', lambda *a, **k: (paths[0] + '\0').encode())
    files, excluded = d.inventory(tmp_path)
    assert set(files) == set(paths)
    assert excluded == []


def test_native_s_delivery_contains_its_untracked_dependencies(tmp_path, monkeypatch):
    paths = (
        'signer-agent/signer_agent/bootstrap_native_s.py',
        'signer-agent/signer_agent/bitcoin_issuer.py',
        'signer-agent/signer_agent/bootstrap_confederation_dao.py',
        'signer-agent/signer_agent/confederation_dao_cli.py',
        'signer-agent/signer_agent/confederation_dao_report.py',
        'web3/contracts/src/federation/ConfederationBitcoinReserve.sol',
        'web3/contracts/src/federation/ConfederationPoolV2.sol',
        'web3/contracts/src/federation/BitcoinCustodyTransferV2.sol',
        'web3/contracts/src/federation/BitcoinWithdrawalEscrowV2.sol',
        'web3/contracts/src/tokens/ConfederationShareV2.sol',
        'config/ulr-in-kind.example.json',
        'docs/CONFEDERATION_DAO_IN_KIND.md',
    )
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}' if path.suffix == '.json' else '// public source\n')
    monkeypatch.setattr(d.subprocess, 'check_output', lambda *a, **k: b'')
    files, excluded = d.inventory(tmp_path)
    assert set(files) == set(paths)
    assert excluded == []


def test_archive_round_trip_excludes_evidence_and_detects_trusted_hash_change(tmp_path,monkeypatch):
    root=tmp_path/'source';root.mkdir();(root/'code.py').write_text('# source\n')
    def git(command,**kwargs):return 'a'*40+'\n' if kwargs.get('text') else b'code.py\0'
    monkeypatch.setattr(d.subprocess,'check_output',git)
    archive=tmp_path/'delivery.tar.gz';r=d.build(archive,root=root)
    assert d.verify(archive,r['archive_sha256'])['proofs_included'] is False
    with pytest.raises(ValueError,match='ARCHIVE_HASH'):d.verify(archive,'00'*32)


def test_even_a_self_consistent_manifest_cannot_allow_a_receipt(tmp_path):
    archive=tmp_path/'bad.tar.gz';payload=b'proof'
    manifest=dict(schema='satoshifi.participant-distribution.v1',generated_proofs_included=False,
        proof_generation='EACH_PARTICIPANT_LOCAL',qualified=False,production_ready=False,
        files={'receipt.bin':dict(sha256=d.sha(payload),bytes=len(payload))})
    with tarfile.open(archive,'w:gz') as out:
        for name,data in [('receipt.bin',payload),('distribution-manifest.json',json.dumps(manifest).encode())]:
            info=tarfile.TarInfo(name);info.size=len(data);out.addfile(info,io.BytesIO(data))
    with pytest.raises(ValueError,match='FORBIDDEN_DISTRIBUTION_MEMBER'):d.verify(archive)


def test_default_release_cli_builds_and_verifies_source_delivery_without_old_receipts(tmp_path,monkeypatch,capsys):
    import sys
    from ops import prepare_release as release
    root=tmp_path/'source';root.mkdir();(root/'code.py').write_text('# source')
    def git(command,**kwargs):return 'a'*40+'\n' if kwargs.get('text') else b'code.py\0'
    monkeypatch.setattr(d.subprocess,'check_output',git)
    build=d.build
    monkeypatch.setattr(d,'build',lambda output:build(output,root=root))
    monkeypatch.setattr(release,'prepare',lambda *a,**k:pytest.fail('historical evidence must not be loaded'))
    config=tmp_path/'config.json';config.write_text(json.dumps(dict(version='1',purpose='PREPARATION_ONLY',
        ethereum_network='sepolia',bitcoin_network='mainnet',bitcoin_backend='zk-state-v2',deployment_intent='NEW_EMPTY',
        operator_inputs={key:None for key in release.INPUTS})))
    output=tmp_path/'delivery'
    monkeypatch.setattr(sys,'argv',['prepare_release','prepare','--config',str(config),'--output',str(output)])
    release.main();result=json.loads(capsys.readouterr().out)
    assert (output/'participant-source.tar.gz').is_file() and not result['proofs_included']
    metadata=json.loads((output/'distribution.json').read_text())
    assert metadata['proof_generation']=='EACH_PARTICIPANT_LOCAL'
    monkeypatch.setattr(sys,'argv',['prepare_release','verify','--directory',str(output),'--manifest-sha256',result['manifest_sha256']])
    release.main();assert json.loads(capsys.readouterr().out)['status']=='DISTRIBUTION_INTEGRITY_VERIFIED'
