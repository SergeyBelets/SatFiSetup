"""Recipient component checks; no synthetic boundary becomes live acceptance."""
import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'ops/artifacts/zkspv/worker-integration'


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    names=('recipient-tests.xml','recipient-tests.log','recipient-solidity.json','recipient-solidity.log',
        'recipient-abi-check.log','recipient-fee-pins.log','recipient-sizes.json','recipient-encoded-sizes.json','recipient-tools.log')
    files=[]
    for name in names:
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());files.append(target)
    suites=list(ET.parse(OUT/'recipient-tests.xml').getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    if not counts['tests'] or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('RECIPIENT_PYTHON_CHECKS_INCOMPLETE')
    forge=json.loads((OUT/'recipient-solidity.json').read_text());results={}
    for name,suite in forge.items():
        tests=suite['test_results']
        if not tests or any(t['status']!='Success' for t in tests.values()):raise ValueError('RECIPIENT_SOLIDITY_CHECKS_INCOMPLETE')
        results[name]=dict(passed=len(tests),matrix_specific=sum(n.startswith('testMatrix') for n in tests))
    if sum(r['matrix_specific'] for r in results.values())<10:raise ValueError('RECIPIENT_MATRIX_CHECKS_MISSING')
    modules=('recipient_profiles','recipient_wallet_client','address_transport','address_registration',
        'address_registration_client','address_mint','smp1_deposit','smp1_deposit_cli','contract_provenance',
        'bootstrap_withdrawal_fee','bootstrap','operations','participant_errors')
    paths=[f'signer-agent/signer_agent/{n}.py' for n in modules]
    tests=('recipient_profiles','address_transport','address_registration','address_watch','address_spv',
        'address_spv_watch','address_mint','address_mint_trust','address_mint_admission','bitcoin_input_keys',
        'smp1_deposit','contract_provenance','withdrawal_bootstrap','withdrawal_user','btc_fee_reserve',
        'withdrawal_fee','withdrawal_fee_spv','callback_admission','mint_admission','bitcoin_funding','zk_bootstrap')
    paths.extend(f'signer-agent/tests/test_{n}.py' for n in tests)
    paths+=['signer-agent/tests/test_recipient_profiles.py','signer-agent/tests/test_address_transport.py',
        'web3/test/RecipientMatrix.t.sol','web3/test/BitcoinAddressRegistry.t.sol','web3/foundry.toml',
        'ops/generate_abis.py','ops/generate_fee_runtime_hashes.py','ops/zkspv/recipient_evidence.py','ops/zkspv/report.py',
        'docs/ADDRESS_DEPOSIT_REGISTRY.md','docs/ADDRESS_REQUESTS_WATCH.md','docs/ADDRESS_REGISTRATION.md',
        'docs/BITCOIN_MINT_AND_TREASURY.md','docs/DEPOSIT_DERIVATION_INPUTS.md','docs/API.md','docs/CONFIGURATION.md',
        'docs/CONTRACT_SIZE_AND_DEPLOYMENT.md',
        'signer-agent/requirements.lock','frost-crypto/src/deposit_derivation.rs','frost-crypto/src/smp1_psbt.rs']
    for kind,folder in [('RecipientPolicy','federation'),('RecipientWalletV1','federation'),
            ('BitcoinAddressRegistry','federation'),('BitcoinDepositRegistry','federation'),
            ('BitcoinWithdrawalEscrow','federation'),('BitcoinBatchRegistry','federation'),
            ('MiningPoolV1','federation'),('PoolMpToken','tokens')]:
        paths.extend([f'web3/contracts/src/{folder}/{kind}.sol',f'signer-agent/contracts/{kind}.json'])
        files.append(ROOT/f'web3/contracts/out/{kind}.sol/{kind}.json')
    paths+=['web3/contracts/src/federation/BitcoinFeeCodeHashes.sol',
        'web3/lib/openzeppelin-contracts/package.json',
        'web3/lib/openzeppelin-contracts/contracts/utils/cryptography/ECDSA.sol',
        'web3/lib/openzeppelin-contracts/contracts/utils/Address.sol']
    report=dict(schema='satoshifi.recipient-matrix.v1',as_of='2026-11-01',
        status='CODE_IMPLEMENTED_LIVE_QUALIFICATION_PENDING',code_implemented=True,ready=False,production_ready=False,
        combined_e2e_performed=False,actual_core_acceptance_performed=False,actual_value_transfer_performed=False,
        fresh_proving_performed=False,independent_review_performed=False,
        local_solidity_wallet_mp_transfer_approve_direct_withdrawal=True,
        python_tests=counts,solidity_suites=results,solidity_tests=sum(r['passed'] for r in results.values()),
        inherited_solidity_tests_repeated=True,
        profiles=dict(ordinary='Current nonzero/no-code format and exact destination, without an ownership/future-key claim',
            wallet_v1='Direct immutable EOA-owner wallet from canonical policy factory; pinned runtime/owner/creation record, ERC-1271 and owner execute',
            advanced_contract='Explicit unverified reception with exact context/runtime/risk acknowledgement; outer runtime cannot qualify proxy/storage/control',
            unsupported=['Undeployed claimed wallet','EIP-7702 delegated recipient','Other claimed verified wallet/proxy profiles']),
        implemented=['Immutable choice hash in existing request_nonce preserves 382-byte intent and registration operation ABI',
            'Independent requester/approving participant validation and on-chain register/mint system-sink/profile checks',
            'Exact chain/policy/namespace/pool/token/requester/recipient binding and advanced risk confirmation',
            'Private draft and V3 encrypted choice opening until existing DKG registration-intent publication; exact public opening only afterward',
            'CREATE2 wallet factory and typed owner create/MP transfer/escrow approve/direct-withdraw commands in existing EOA lane',
            'SMP1 client recipient preview, separate profile/financial confirmations and saved-choice rechecks before signing/broadcast',
            'Changed current eligibility cannot reassign/delete historical binding, accepted watch or custody/signing/recovery'],
        trust_boundaries=['Solidity wallet, policy, registry, MP and escrow execute locally; threshold/DKG/SPV/qualification are explicit fixtures',
            'Python ABI/hash, HPKE, native derivation/PSBT and SQLite are real; RPC/provenance histories are synthetic fixtures',
            'Recipient signature is not a universal prerequisite for third-party reception; requester authorization remains separate',
            'Address on-chain gates do not silently change legacy SMP1/coinbase monetary consensus',
            'Wallet direct MP primary withdrawal is implemented; S redemption and fee-consent/subsidy via this wallet are not qualified'],
        compatibility=dict(mainnet_guest_unchanged=True,raw_intent_bytes=382,registration_operation_abi_unchanged=True,
            legacy_ordinary_nonces_preserved=True,legacy_smp1_coinbase_preserved=True,qualification_flags_unchanged=True),
        deferred=['Live Core/proof/FROST/consumer and failure/reorg/recovery contour',
            'Operational profile/verifier/old-epoch continuity qualification','Independent review'],
        source_sha256={name:sha(ROOT/name) for name in sorted(set(paths))},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in files})
    (OUT/'recipient-matrix-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python=counts,solidity=report['solidity_tests'],ready=False)))


if __name__=='__main__':main()
