"""Create the participant distribution: protocol code and pins, never ready ZK proofs.

Historical evidence stays in the developer checkout. This archive is the delivery
boundary; an ordinary full git clone is not the participant distribution.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
STATIC_GUEST = 'rust/zkspv/guest-integration/evidence/mainnet-transition.r0bf'
TESTNET4_GUEST = 'rust/zkspv/testnet4-integration/evidence/testnet4-transition.r0bf'
STATIC_PATCH = 'rust/zkspv/header-qualification/evidence/native-test-adjustment.patch'
STATIC_LOCKS = {'ops/artifacts/zkspv/upstream-lock.json', 'ops/artifacts/zkspv/candidate-manifest.json'}
EXCLUDED_PARTS = {'.git','.venv','__pycache__','.pytest_cache','node_modules','target','out','cache',
    'broadcast','.local-tools','.state','runtime','receipt-cache','attempts','verified','local-result'}
NEW_SOURCES = ('signer-agent/signer_agent/bootstrap_custody.py',
    'signer-agent/signer_agent/custody_transfer_cli.py','signer-agent/tests/test_bootstrap_custody.py',
    'web3/contracts/src/federation/BitcoinCustodyTransfer.sol','web3/test/BitcoinCustodyTransfer.t.sol',
    'web3/test/CustodianMonetaryEpoch.t.sol','tests/e2e/test_custody_transfer.py',
    'web3/test/IntentSessionFixture.sol',
    'config/bitcoin-custody.example.json','docs/BITCOIN_CUSTODY_TRANSFER.md',
    'signer-agent/contracts/BitcoinCustodyTransfer.json',
    'signer-agent/signer_agent/bootstrap_tokens.py',
    'signer-agent/tests/test_bootstrap_tokens.py','config/token-metadata.example.json',
    'tests/e2e/test_bootstrap_tokens.py','ops/package_distribution.py','ops/zkspv/participant_build.py','docs/DISTRIBUTION.md',
    'signer-agent/signer_agent/zk_local_proofs.py','tests/ops/test_package_distribution.py',
    'signer-agent/tests/test_zk_local_proofs.py','tests/ops/test_participant_build.py',
    'docs/PEER_TESTING.md','docs/ZK_PROVER_LINUX.md','config/zk-prover-linux-x86_64.json',
    'signer-agent/signer_agent/_zk_linux_entry.py','signer-agent/signer_agent/zk_isolation.py',
    'signer-agent/signer_agent/zk_prover_platform.py','ops/zkspv/linux_tools.py',
    'ops/zkspv/risc0_release.py','config/zk-prover-3.1.0.json',
    'rust/zkspv/risc0-release/Cargo.toml','rust/zkspv/risc0-release/Cargo.lock',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/LICENSE','rust/zkspv/vendor/risc0-ethereum-3.1.0/pin.json',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/IRiscZeroVerifier.sol',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/IRiscZeroSelectable.sol',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/StructHash.sol',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/Util.sol',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/groth16/ControlID.sol',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/groth16/Groth16Verifier.sol',
    'rust/zkspv/vendor/risc0-ethereum-3.1.0/contracts/src/groth16/RiscZeroGroth16Verifier.sol',
    'signer-agent/contracts/ULRConfederationTreasuryLegacy.json',
    'signer-agent/contracts/ULRConfederationTreasuryV1.json',
    'signer-agent/contracts/ULRCustodianSnapshotsLegacy.json',
    'signer-agent/tests/test_zk_linux_port.py',
    'signer-agent/tests/test_signing_target_substitution.py',
    'tests/e2e/test_signing_verifier_substitution.py',
    'docs/SECURITY_LOGIC_REVIEW_2026_11_03.md',
    'web3/test/SecurityLogicReview20261103.t.sol',
    'signer-agent/signer_agent/bootstrap_funding.py',
    'signer-agent/tests/test_bootstrap_funding.py',
    'tests/e2e/test_startup_funding.py',
    'signer-agent/signer_agent/bootstrap_distribution.py',
    'signer-agent/tests/test_bootstrap_distribution.py',
    'tests/e2e/test_ulr_distribution_pause.py')
NEW_SOURCES += ('signer-agent/signer_agent/launch_economics.py','signer-agent/signer_agent/launch_configurator.py',
    'signer-agent/signer_agent/startup_rewards_cli.py','signer-agent/tests/test_launch_configurator.py',
    'web3/contracts/src/staking/ULRStartupRewards.sol','web3/test/ULRLaunchEconomics.t.sol',
    'signer-agent/contracts/ULRStartupRewards.json','config/launch.example.json','docs/LAUNCH_CONFIGURATOR.md',
    'docs/LAUNCH_MATHEMATICS.md','docs/FROST_ZK_LIFECYCLE.md','docs/COLD_START.md',
    'docs/FEDERATION_CONFEDERATION_SETUP.md','docs/NFT_CONFIGURATION.md','ops/check_launch_math.py')
NEW_SOURCES += ('signer-agent/signer_agent/nft_governance.py','signer-agent/signer_agent/nft_cli.py',
    'signer-agent/tests/test_nft_governance.py','web3/test/NFTGovernance.t.sol',
    'tests/e2e/test_nft_governance.py')
NEW_SOURCES += ('signer-agent/signer_agent/bootstrap_recovery.py', 'signer-agent/signer_agent/bootstrap_nonce_close.py',
    'signer-agent/tests/test_bootstrap_recovery.py','ops/zkspv/r0vm-3.0.3-segment-limit.patch')
NEW_SOURCES += ('signer-agent/signer_agent/bootstrap_nonce_legacy.py',
    'signer-agent/tests/test_bootstrap_nonce_legacy.py')
NEW_SOURCES += ('signer-agent/signer_agent/zk_submission.py',)
NEW_SOURCES += ('signer-agent/signer_agent/launch_rpc.py', 'signer-agent/tests/test_launch_rpc.py')
NEW_SOURCES += ('signer-agent/signer_agent/finalization_recovery.py',
    'signer-agent/tests/test_finalization_recovery.py', 'tests/e2e/test_frost_finalize_race.py',
    'signer-agent/signer_agent/bootstrap_replacement.py', 'signer-agent/tests/test_bootstrap_replacement.py',
    'tests/e2e/test_evm_pending_replacement.py')
NEW_SOURCES += (
    'config/ulr-in-kind.example.json', 'docs/CONFEDERATION_DAO_IN_KIND.md',
    'signer-agent/signer_agent/bitcoin_issuer.py',
    'signer-agent/signer_agent/bootstrap_confederation_dao.py',
    'signer-agent/signer_agent/bootstrap_native_s.py',
    'signer-agent/signer_agent/confederation_dao_cli.py',
    'signer-agent/signer_agent/confederation_dao_report.py',
    'signer-agent/tests/test_confederation_dao_report.py',
    'signer-agent/tests/test_in_kind_bitcoin_payment.py',
    'signer-agent/tests/test_in_kind_withdrawal.py',
    'tests/e2e/test_in_kind_bootstrap.py',
    'web3/contracts/src/federation/BitcoinCustodyTransferV2.sol',
    'web3/contracts/src/federation/BitcoinWithdrawalEscrowV2.sol',
    'web3/contracts/src/federation/ConfederationBitcoinReserve.sol',
    'web3/contracts/src/federation/ConfederationDAOIncome.sol',
    'web3/contracts/src/federation/ConfederationDAOStake.sol',
    'web3/contracts/src/federation/ConfederationNFTPass.sol',
    'web3/contracts/src/federation/ConfederationPoolV2.sol',
    'web3/contracts/src/federation/ConfederationPoolV2Deployer.sol',
    'web3/contracts/src/federation/ConfederationShareV2Deployer.sol',
    'web3/contracts/src/tokens/ConfederationShareV2.sol',
    'web3/test/BitcoinWithdrawalEscrowV2.t.sol',
    'web3/test/ConfederationBacking.t.sol',
    'web3/test/ConfederationDAOIncome.t.sol')
NEW_SOURCES += ('docs/RECOVERY.md',)
NEW_SOURCES += (
    'signer-agent/contracts/FundedFeeSale.json',
    'signer-agent/signer_agent/fee_sale_cli.py',
    'signer-agent/signer_agent/fee_sale_governance.py',
    'signer-agent/signer_agent/zk_peer_bundle.py',
    'signer-agent/tests/test_fee_sale_governance.py',
    'signer-agent/tests/test_zk_peer_bundle.py',
    'web3/contracts/src/federation/FundedFeeSale.sol',
    'web3/test/FundedFeeSale.t.sol',
)
TEXT_SUFFIXES = {'.py','.rs','.sol','.js','.cjs','.mjs','.ts','.tsx','.jsx','.toml','.lock','.md',
    '.json','.yml','.yaml','.sh','.patch','.txt','.csv','.tsv','.svg','.hex','.raw','.example','.c','.h','.cpp','.S'}


def sha(data): return hashlib.sha256(data).hexdigest()


def reason(name):
    path=PurePosixPath(name)
    if (not name or path.is_absolute() or str(path)!=name or '\\' in name
            or any(p in ('.','..') for p in path.parts)):return 'unsafe_path'
    if name=='distribution-manifest.json':return 'generated_manifest'
    if name in (STATIC_GUEST,TESTNET4_GUEST,STATIC_PATCH) or name in STATIC_LOCKS:return None
    if len(path.parts)>1 and path.parts[0] not in ('.github','.deps','config','docs','frost-crypto','js','ops','rust','signer-agent','tests','web3'):
        return 'not_protocol_source'
    if any(p in EXCLUDED_PARTS for p in path.parts):return 'local_or_build_output'
    if path.parts[:2]==('ops','artifacts'):return 'local_evidence_or_old_package'
    if 'evidence' in path.parts:return 'generated_evidence'
    if path.name.startswith('.env') and path.name!='.env.example':return 'private_configuration'
    if path.suffix in {'.sqlite','.db','.enc','.cookie','.log','.xml','.r0','.elf','.r0bf','.journal','.input','.key','.seal'}:return 'local_generated_data'
    if path.name.endswith(('.tar','.tar.gz','.tgz','.zip')):return 'nested_archive'
    if path.suffix=='.bin':
        if name.startswith(('web3/test/fixtures/','tests/vectors/')) and not any(t in path.name.lower() for t in ('receipt','seal','proof','journal')):return None
        return 'binary_or_proof'
    if path.name.endswith('.bin.gz') and name.startswith('web3/test/fixtures/') and path.name.startswith('headers-'):return None
    if path.name.startswith('.') and path.name not in ('.gitignore','.gitattributes','.env.example','.gitmodules'):return 'local_metadata'
    if path.suffix not in TEXT_SUFFIXES and not path.name.upper().startswith(('LICENSE','COPYING','NOTICE')) and path.name not in ('Dockerfile','Makefile','Cargo.lock','.gitignore','.gitattributes','.env.example','.gitmodules'):
        return 'not_protocol_source'
    return None


def public_content(name,data):
    """Do not let ready proof bytes leak through a renamed JSON fixture."""
    if PurePosixPath(name).suffix!='.json':return
    value=json.loads(data)
    def visit(item):
        if isinstance(item,dict):
            for key,child in item.items():
                if key=='private_key' or key in ('receipt_bytes','proof_bytes','journal_bytes') and not isinstance(child,int):
                    raise ValueError('PRIVATE_OR_PROOF_CONTENT:'+name)
                if key in ('pi_a','pi_b','pi_c') or key in ('seal','receipt','proof') and isinstance(child,(list,dict)) and child:
                    raise ValueError('PROOF_CONTENT:'+name)
                if key in ('seal','receipt','proof') and isinstance(child,str):
                    raw=child.removeprefix('0x')
                    if len(raw)>=64 and all(c in '0123456789abcdefABCDEF' for c in raw):raise ValueError('PROOF_CONTENT:'+name)
                visit(child)
        elif isinstance(item,list):
            for child in item:visit(child)
    visit(value)


def inventory(root,extra=()):
    names=set(subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0'))-{''}
    names.update(extra)
    names.update(name for name in NEW_SOURCES if (root/name).is_file())
    for folder in ('rust/zkspv/testnet4-integration','rust/zkspv/testnet4-worker-host'):
        names.update(str(p.relative_to(root)) for p in (root/folder).rglob('*') if p.is_file())
    for pattern in ('ops/zkspv/testnet4_*.py','signer-agent/signer_agent/zk_profiles.py',
                    'signer-agent/tests/test_zk_testnet4.py','tests/e2e/test_zk_testnet4_e2e.py',
                    'tests/ops/test_testnet4*.py','config/*testnet4*.json','docs/ZK_TESTNET4_SEPOLIA.md',
                    'rust/zkspv/state-adapter/contracts/*Testnet4*.sol',
                    'rust/zkspv/state-adapter/fixtures/testnet4-state-v2.json',
                    'rust/zkspv/state-adapter/test/Testnet4State.t.sol',
                    'signer-agent/contracts/*Testnet4*.json'):
        names.update(str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file())
    selected={};excluded=[]
    for name in sorted(names):
        why=reason(name)
        if why:excluded.append(dict(path=name,reason=why));continue
        path=root/name
        if not path.exists():continue  # deleted worktree files are not delivered
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('SOURCE_SYMLINK:'+name)
        data=path.read_bytes()
        try:public_content(name,data)
        except ValueError as exc:
            if str(exc).startswith(('PROOF_CONTENT:','PRIVATE_OR_PROOF_CONTENT:')):
                excluded.append(dict(path=name,reason='private_or_proof_payload'));continue
            raise
        selected[name]=data
    if STATIC_GUEST in selected:
        pins=json.loads((root/'rust/zkspv/guest-integration/report.json').read_text())
        if sha(selected[STATIC_GUEST])!=pins['evidence_sha256']['mainnet-transition.r0bf']:raise ValueError('STATIC_GUEST_DRIFT')
    if TESTNET4_GUEST in selected:
        pins=json.loads((root/'rust/zkspv/testnet4-integration/report.json').read_text())
        if sha(selected[TESTNET4_GUEST])!=pins['evidence_sha256']['testnet4-transition.r0bf']:
            raise ValueError('TESTNET4_STATIC_GUEST_DRIFT')
    return selected,excluded


def build(output,*,root=ROOT,extra=()):
    root=Path(root).resolve();output=Path(output)
    files,excluded=inventory(root,extra)
    manifest=dict(schema='satoshifi.participant-distribution.v1',
        files={n:dict(sha256=sha(data),bytes=len(data)) for n,data in files.items()},
        generated_proofs_included=False,proof_generation='EACH_PARTICIPANT_LOCAL',
        static_guest='Pinned program code, not a transition receipt',excluded=excluded,
        qualified=False,production_ready=False,
        source_revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        worktree_snapshot=True)
    raw=(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream:
        with gzip.GzipFile(fileobj=stream,mode='wb',mtime=0,filename='') as zipped:
            with tarfile.open(fileobj=zipped,mode='w') as archive:
                for name,data in [*files.items(),('distribution-manifest.json',raw)]:
                    info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o644;info.mtime=0
                    archive.addfile(info,io.BytesIO(data))
    return dict(status='DISTRIBUTION_PREPARED',archive=str(output),archive_sha256=sha(output.read_bytes()),
        manifest_sha256=sha(raw),files=len(files),excluded=len(excluded),proofs_included=False,qualified=False)


def verify(archive_path,expected_sha256=None):
    if expected_sha256 and sha(Path(archive_path).read_bytes())!=expected_sha256:raise ValueError('DISTRIBUTION_ARCHIVE_HASH')
    files={}
    with tarfile.open(archive_path,'r:gz') as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name in files:raise ValueError('DISTRIBUTION_MEMBER')
            if member.name!='distribution-manifest.json' and reason(member.name):raise ValueError('FORBIDDEN_DISTRIBUTION_MEMBER:'+member.name)
            files[member.name]=archive.extractfile(member).read()
    raw=files.pop('distribution-manifest.json');manifest=json.loads(raw)
    if (manifest.get('schema')!='satoshifi.participant-distribution.v1' or manifest.get('generated_proofs_included') is not False
            or manifest.get('proof_generation')!='EACH_PARTICIPANT_LOCAL' or manifest.get('qualified') is not False
            or manifest.get('production_ready') is not False or set(files)!=set(manifest['files'])):
        raise ValueError('DISTRIBUTION_MANIFEST')
    for name,data in files.items():
        public_content(name,data)
        if dict(sha256=sha(data),bytes=len(data))!=manifest['files'][name]:raise ValueError('DISTRIBUTION_FILE_CHANGED:'+name)
    return dict(status='DISTRIBUTION_INTEGRITY_VERIFIED',files=len(files),manifest_sha256=sha(raw),
        trusted_archive_hash_supplied=expected_sha256 is not None,proofs_included=False,qualified=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('build');p.add_argument('--output',type=Path,required=True)
    p.add_argument('--extra-source',action='append',default=[],help='Explicit newly added public source file, before commit')
    p=sub.add_parser('verify');p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--sha256')
    a=parser.parse_args(argv)
    print(json.dumps(build(a.output,extra=a.extra_source) if a.action=='build' else verify(a.archive,a.sha256),indent=2))


if __name__=='__main__':main()
