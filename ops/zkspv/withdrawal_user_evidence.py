"""Record focused continuation evidence without relabelling old runs as current."""
import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from signer_agent.bootstrap import artifact
from signer_agent.contract_provenance import creation_input
from signer_agent.deployment_limits import check_deployment

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'ops/artifacts/zkspv/worker-integration'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input-directory', type=Path, required=True)
    a = p.parse_args()
    mapping = {'withdrawal-bootstrap-tests.xml':'withdrawal-bootstrap-user-tests.xml',
        'withdrawal-bootstrap-tests.log':'withdrawal-bootstrap-user-tests.log',
        'withdrawal-bootstrap-rounding.json':'withdrawal-bootstrap-user-rounding.json',
        'withdrawal-bootstrap-subsidy.json':'withdrawal-bootstrap-user-subsidy.json',
        'withdrawal-bootstrap-rust.log':'withdrawal-bootstrap-user-rust.log',
        'withdrawal-bootstrap-sizes.json':'withdrawal-bootstrap-user-sizes.json'}
    files = []
    for source, target in mapping.items():
        path = OUT/target
        path.write_bytes((a.input_directory/source).read_bytes())
        files.append(path)
    suites = list(ET.parse(OUT/mapping['withdrawal-bootstrap-tests.xml']).getroot().iter('testsuite'))
    python = {key:sum(int(s.get(key, 0)) for s in suites) for key in ('tests','failures','errors','skipped')}
    if not python['tests'] or any(python[k] for k in ('failures','errors','skipped')):
        raise ValueError('FOCUSED_CHECKS_INCOMPLETE')
    solidity, new = 0, 0
    for kind in ('rounding','subsidy'):
        result = json.loads((OUT/f'withdrawal-bootstrap-user-{kind}.json').read_text())
        for suite in result.values():
            for name, test in suite['test_results'].items():
                if test['status'] != 'Success':
                    raise ValueError('SOLIDITY_CHECKS_INCOMPLETE')
                solidity += 1
                new += int(name.startswith('testQuotedWithdrawal'))
    rust = re.search(r'test result: ok\. (\d+) passed; 0 failed; (\d+) ignored',
                     (OUT/'withdrawal-bootstrap-user-rust.log').read_text())
    if new != 6 or rust is None:
        raise ValueError('CONTINUATION_CHECKS_INCOMPLETE')
    address = lambda n: '0x'+f'{n:040x}'
    constructors = {'WithdrawalFeePayments':[address(1),address(2),address(3)],
        'WithdrawalFeeFunding':[address(1),address(2),address(3)],'WithdrawalFeeCredits':[address(1),address(2),address(3)],
        'SimpleMultiPool':['owner pool',address(1),address(2)],'SimpleMultiPoolDeployer':[address(1),address(2)]}
    encoded = {name:check_deployment(artifact(name)['deployedBytecode']['object'], creation_input(name, args))
               for name, args in constructors.items()}
    pool = bytes.fromhex(artifact('SimpleMultiPool')['bytecode']['object'].removeprefix('0x'))
    parts = [pool[:len(pool)//2], pool[len(pool)//2:]]
    blobs = [check_deployment('0x00'+part.hex(), creation_input('CreationCodePart',[part])) for part in parts]
    paths = ['signer-agent/signer_agent/'+name+'.py' for name in (
        'bootstrap','bootstrap_plan','bootstrap_parallel','bootstrap_create','bootstrap_withdrawal_fee','operations','onboarding',
        'ethereum_intents','withdrawal_user','withdrawal_user_cli','withdrawal_fee_client','withdrawal_fee_cli','withdrawal_fee',
        'btc_fee_reserve','btc_fee_client','bitcoin_cli','participant_errors')]
    paths += ['signer-agent/tests/'+name+'.py' for name in ('test_withdrawal_user','test_withdrawal_bootstrap',
        'test_bootstrap_parallel','test_bootstrap_receipt_checks','test_bootstrap_read_finality','test_flow_cli','test_zk_bootstrap',
        'test_onboarding_policy_v3','test_withdrawal_fee','test_withdrawal_fee_spv')]
    paths += ['web3/contracts/src/demo/SimpleMultiPool.sol','web3/test/WithdrawalRounding.t.sol',
        'web3/test/WithdrawalFeeSubsidy.t.sol','web3/foundry.toml','frost-crypto/src/bitcoin.rs','frost-crypto/src/main.rs',
        'frost-crypto/Cargo.lock','ops/generate_abis.py','ops/zkspv/report.py','ops/zkspv/withdrawal_user_evidence.py',
        'config/withdrawal-fee-bootstrap.example.json','docs/WITHDRAWAL_FEE_SUBSIDY.md','docs/CONFIGURATION.md','docs/API.md',
        'docs/DEPOSIT_DERIVATION_INPUTS.md','docs/ADDRESS_SPV_WORKER.md']
    for name in ('SimpleMultiPool','SimpleMultiPoolDeployer','WithdrawalFeeCredits','WithdrawalFeeFunding','WithdrawalFeePayments',
                 'WithdrawalFeePlanVerifier','BitcoinWithdrawalFeeDerivation','BitcoinDepositDerivation','FederationNFTModifier'):
        paths.append(f'signer-agent/contracts/{name}.json')
        files.append(ROOT/f'web3/contracts/out/{name}.sol/{name}.json')
    report = dict(schema='satoshifi.withdrawal-bootstrap-user-report.v1', as_of='2026-11-01',
        status='CODE_IMPLEMENTED_QUALIFICATION_PENDING', bootstrap_user_code_implemented=True,
        ready=False, production_ready=False, combined_e2e_performed=False, actual_profile_activated=False,
        fresh_proving_performed=False, independent_review_performed=False, python_tests=python,
        solidity_tests=dict(executions=solidity, new_quote_guard_cases=new, failures=0, inherited_cases_included=True),
        rust_tests=dict(passed=int(rust[1]),ignored=int(rust[2]),ignored_reason='existing public fixture generator'),
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'),
        encoded_deployment_sizes=encoded, actual_blob_deployment_sizes=blobs,
        bootstrap=dict(opt_in_public_policy='withdrawal_fee.v1 in signed V6 invitation',
            create_group_transactions=3, raw_creation_gas_limit=8000000, padded_creation_gas_limit=10400000,
            nonce_recovery='All three EOA CREATE envelopes atomically reserved in shared identity lane before first broadcast',
            readiness='Bindings and route maps exported only after checked installation; qualification flags remain unchanged'),
        implemented=['Common bootstrap address registry before first base registration, signing board/reserve and reciprocal personal fee graph',
            'Public fee profile committed by every enrollment; appended or altered unenrolled policy rejected',
            'MP/S payout or total-budget quotes; exact fee/NFT inversion, per-MP balance and payout minimum checks',
            'Local rust-bitcoin destination checksum/network/profile parsing',
            'Durable same-request primary/consent recovery; exact-version review and approval without manual ABI JSON',
            'In-transaction exact ordinary S fee and P guard; reverting NFT volume use and cleared transient guard'],
        deferred=['Fresh full Core/proof/EVM/FROST/payment/consumer contour',
            'Actual independent derivation/purpose qualification and production activation',
            'Old-epoch continuity, recipient matrix and automated SMP1 preparation remain separate work'],
        scope='Real native crypto/parser and SQLite plus Solidity contract execution; Python EVM/Core transport boundaries are fixtures. No live value transfer or new proof.',
        source_sha256={name:sha(ROOT/name) for name in sorted(set(paths))},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in files})
    write(OUT/'withdrawal-bootstrap-user-report.json',report)
    print(json.dumps(dict(status=report['status'],python=python,solidity=solidity,rust=report['rust_tests'],ready=False)))


if __name__ == '__main__':
    main()
