"""Focused SMP1 preparation evidence, distinct from live Core/consumer acceptance."""
import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'ops/artifacts/zkspv/worker-integration'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    files=[]
    for name in ('smp1-tests.xml','smp1-tests.log','smp1-rust-tests.log','smp1-native-build.log','smp1-abi-check.log'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());files.append(target)
    suites=list(ET.parse(OUT/'smp1-tests.xml').getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    if not counts['tests'] or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('SMP1_TESTS_INCOMPLETE')
    rust=re.search(r'test result: ok\. (\d+) passed; 0 failed; (\d+) ignored',(OUT/'smp1-rust-tests.log').read_text())
    if rust is None:raise ValueError('SMP1_RUST_TESTS_INCOMPLETE')
    modules=('smp1_deposit','smp1_deposit_cli','bitcoin_cli','bitcoin_funding','bitcoin_rpc','bitcoin_network',
        'bitcoin_trust','mint_admission','mint_admission_client','address_mint','withdrawal_user','callback_admission',
        'contract_provenance','operations','participant_errors','bootstrap','bootstrap_create','bootstrap_withdrawal_fee','crypto')
    tests=('test_smp1_deposit','test_bitcoin_funding','test_funding_envelope','test_unminted_deposit_protection',
        'test_bitcoin_network','test_bitcoin_mainnet_rpc','test_independent_networks','test_withdrawal_user',
        'test_withdrawal_bootstrap','test_zk_bootstrap','test_callback_admission')
    paths=[f'signer-agent/signer_agent/{name}.py' for name in modules]+[f'signer-agent/tests/{name}.py' for name in tests]
    paths+=['frost-crypto/src/smp1_psbt.rs','frost-crypto/src/bitcoin.rs','frost-crypto/src/main.rs',
        'frost-crypto/Cargo.toml','frost-crypto/Cargo.lock','config/smp1-deposit.example.json',
        'docs/BITCOIN_MINT_AND_TREASURY.md','docs/API.md','docs/CONFIGURATION.md',
        'ops/zkspv/smp1_evidence.py','ops/zkspv/report.py']
    for name,folder in (('MiningPoolV1','federation'),('BitcoinDepositRegistry','federation'),
        ('BitcoinWithdrawalEscrow','federation'),('BitcoinBatchRegistry','federation'),('BitcoinMintAdmission','federation'),
        ('PoolMpToken','tokens')):
        paths.extend([f'web3/contracts/src/{folder}/{name}.sol',f'signer-agent/contracts/{name}.json'])
        files.append(ROOT/f'web3/contracts/out/{name}.sol/{name}.json')
    binary=ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'
    report=dict(schema='satoshifi.smp1-wallet-preparation.v1',as_of='2026-11-01',
        status='CODE_IMPLEMENTED_LIVE_ACCEPTANCE_PENDING',code_implemented=True,ready=False,production_ready=False,
        actual_wallet_rpc_performed=False,actual_core_acceptance_performed=False,actual_value_transfer_performed=False,
        fresh_proving_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        python_tests=counts,rust_tests=dict(passed=int(rust[1]),ignored=int(rust[2]),ignored_reason='existing public fixture generator'),
        native_binary_sha256=sha(binary),solidity_changes=False,
        profile=dict(name='SMP1_PSBT_V0_NATIVE_SEGWIT_V1',wallet_api='Bitcoin Core 30.x',psbt_version=0,
            inputs=['P2WPKH','P2TR key path'],default_max_inputs=256,contract_output_limit=32,
            native_psbt_envelope_bytes=1048576,full_final_transaction_bytes=400000,stripped_transaction_bytes=100000,
            wallet_outputs='one deposit, one zero-value SMP1, optional owner-wallet change',
            ordinary_mint_depth='max(6, configured depth)',coinbase_input_depth=101),
        implemented=['Automatic wallet PSBT funding and actual-vout SMP1 allocation/marker computation without caller ABI/crypto',
            'Exact amount, recipient, pool/token/network, fee cap and change review; local pinned rust-bitcoin parsing',
            'Core prevout/value/script and common custody/reservation source checks before signing and sending',
            'Durable persistent Core locks, explicit acknowledgment ambiguity, no foreign observed lock adoption',
            'Checked no-signature wallet replies can cancel; potentially signed/exported claims cannot timeout-unlock',
            'External PSBT export/import retains commitment and exact unsigned transaction; unsafe sighashes/changed intent rejected',
            'Final native envelope plus Core mempool fee/vsize checks; exact signed-envelope broadcast/recovery and no automatic replacement',
            'Separate Bitcoin/header-SPV/admission/EVM-finality status; exact existing mint consumer arguments and fee reserve claims',
            'Canonical constructor/CREATE-nonce proof for internal MP child; S-pool factory provenance via authenticated callback registry'],
        trust_boundaries=['Core owns wallet keys and is the trusted wallet/UTXO/signing API; no keys are exported',
            'Core lock API has no owner tags; unresolved RPC/durable-write windows stop for operator reconciliation',
            'Python EVM/Core boundaries are fixtures; native parser and SQLite are real; fixture signatures are not consensus acceptance',
            'Recipient profile checks format/network/current no-code/sinks, without universal EOA ownership or smart-wallet claims'],
        compatibility=dict(low_level_smp1_coinbase_preserved=True,general_funding_100k_parser_preserved=True,
            mainnet_guest_unchanged=True,frost_nonce_journals_unchanged=True,derivation_qualification_flags_unchanged=True),
        sources=[f'https://bitcoincore.org/en/doc/30.0.0/rpc/{group}/{method}/' for group,method in
            [('wallet','walletcreatefundedpsbt'),('wallet','walletprocesspsbt'),('wallet','lockunspent'),
             ('rawtransactions','finalizepsbt'),('rawtransactions','testmempoolaccept')]],
        deferred=['Live Core wallet/mempool/signature/transaction acceptance for this exact profile',
            'Full consumer/admission/FROST/proof/EVM flow and failure/reorg/recovery contour',
            'Actual hardware wallets, recipient matrix, old-epoch continuity and independent review'],
        source_sha256={name:sha(ROOT/name) for name in sorted(paths)},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in files})
    path=OUT/'smp1-wallet-preparation-report.json';path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python=counts,rust=report['rust_tests'],ready=False)))


if __name__=='__main__':main()
