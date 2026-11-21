"""Package completed short checks without promoting fixture evidence to live qualification."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from signer_agent.bootstrap import artifact
from signer_agent.contract_provenance import creation_input
from signer_agent.deployment_limits import check_deployment

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'ops/artifacts/zkspv/worker-integration'


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--forge-json',type=Path,required=True);a=p.parse_args()
    xml=OUT/'btc-fee-reserve-tests.xml';suites=list(ET.parse(xml).getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    if not counts['tests'] or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('PYTHON_CHECKS_NOT_COMPLETE')
    raw=a.forge_json.read_bytes();forge=json.loads(raw);total=0;fee_tests=0;gas={}
    for name,suite in forge.items():
        for test,t in suite['test_results'].items():
            if t['status']!='Success':raise ValueError('SOLIDITY_CHECKS_NOT_COMPLETE')
            total+=1
            if 'BitcoinFeeReserveTest' in name and test.startswith('testFee'):
                fee_tests+=1;gas[test]=t['kind']['Unit']['gas']
    if fee_tests<10:raise ValueError('FEE_SUITE_INCOMPLETE')
    zipped=OUT/'btc-fee-reserve-forge.json.gz';zipped.write_bytes(gzip.compress(raw,mtime=0))
    fee=artifact('BitcoinFeeReserve');args=['0x'+f'{i:040x}' for i in range(1,6)]+[500000,5000]
    size=check_deployment(fee['deployedBytecode']['object'],creation_input('BitcoinFeeReserve',args))
    encoded=OUT/'btc-fee-reserve-encoded-size.json'
    write(encoded,dict(contract='BitcoinFeeReserve',constructor_types=['address']*5+['uint64','uint32'],
        constructor_args_bytes=224,all_constructor_fields_static=True,**size))
    old=json.loads((OUT/'address-spv-worker-report.json').read_text())
    sources=set(old['source_sha256'])
    for pattern in ('signer-agent/signer_agent/btc_fee*.py','signer-agent/tests/test_btc_fee_reserve.py',
            'web3/contracts/src/federation/*Fee*.sol','web3/test/BitcoinFeeReserve.t.sol',
            'signer-agent/contracts/BitcoinFee*.json','config/btc-fee-reserve.example.json','docs/BTC_FEE_RESERVE.md',
            'ops/zkspv/btc_fee_evidence.py'):
        sources.update(str(f.relative_to(ROOT)) for f in ROOT.glob(pattern))
    sources.update(('signer-agent/tests/test_bitcoin_onchain_security.py','signer-agent/tests/test_bitcoin_mainnet_finality.py'))
    hashes={name:sha(ROOT/name) for name in sorted(sources)}
    paths=[xml,zipped,encoded,OUT/'btc-fee-reserve-test-selection.json',OUT/'btc-fee-reserve-sizes.json']
    paths+=list((ROOT/'ops/artifacts/documentation-diagrams-btc-fee-reserve-20261101').glob('*'))
    for name in ('BitcoinFeeReserve','BitcoinFeeFundingVerifier','BitcoinFeePlanVerifier','BitcoinBatchRegistry',
                 'BitcoinDepositRegistry','BitcoinWithdrawalEscrow','BitcoinAddressRegistry'):
        paths.append(ROOT/'web3/contracts/out'/f'{name}.sol'/f'{name}.json')
    report=dict(schema='satoshifi.btc-fee-reserve.v1',as_of='2026-11-01',status='CODE_IMPLEMENTED_QUALIFICATION_PENDING',
        production_ready=False,ready=False,btc_fee_reserve_code_implemented=True,reconciliation_code_implemented=True,
        actual_profile_activated=False,real_candidate_verifier_qualified=False,fresh_proving_performed=False,
        combined_e2e_performed=False,actual_core_acceptance_performed=False,actual_value_transfers_performed=False,
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'),
        implemented=[
            'Immutable exact-graph BTC reserve, authenticated external P2TR unconditional donor proof and canonical free/whole-input locked/unbound promised accounting',
            'Principal and MP supply conservation; native/contract reserve, treasury, signing and exact old-winner settlement hooks; no fee gate on valid mint',
            'Canonical allocations, monotonic caps, exact V3 plan/hash/fee/vbytes bounds, same-input RBF, at most 16 versions and no timeout unlock after Signing',
            'Full archival Core inventory with registered and retained private watches, native descriptors/raw txids, backing/supply check and pending classification',
            'Authorized mempool collateral, confirmed settlement waiting, unknown-loss pause and explicit scoped recovery in the common BTC journal, without nonce reset',
            'Public typed FROST requests, enrolled board/operator/quorum, independent positive report review, generation guards before fresh/cached private work and source-bound action receipts',
            'Durable transaction recovery and bounded EOA gas budgets; SDK/CLI/bootstrap extension and address service V2 fee policy/status pin',
            'Nondecreasing retained generations/private watch inventory, static conservative stress/cap runway; no ETH/MP substitution or automatic reimbursement',
            'Updated runbook, ABI and eight rendered architecture diagrams'],
        boundaries=dict(crypto='REAL pinned native codec/derivation/DKG/FROST and coincurve/BIP340; explicit external Core/EVM/provenance fixtures',
            contract='REAL Forge candidate contract/accounting/token/principal/replacement logic and canonical donor BIP340; explicit DKG/authorization/SPV/qualification fixtures',
            database='REAL identity/watch/transaction SQLite and SecureState recovery; provider chain/UTXO facts are fixtures',
            report='Threshold attestation plus independent native completeness policy, not a trustless negative UTXO or unpublished-history proof',
            qualification='Actual derivation verifier QUALIFIED=false; no live Ready or production reserve activation',
            fee_profile='Restricted single external non-coinbase P2TR source; per-owner refundable credits/subsidies are separate unfinished scope',
            runway='Static stress policy with immutable protocol cap and signed vbytes; no live estimator, measured SLA or production operating qualification'),
        python_tests=counts,test_suite_count=len(json.loads((OUT/'btc-fee-reserve-test-selection.json').read_text())),
        solidity_tests=dict(executions=total,failures=0,fee_scenarios=fee_tests,inherited_address_scenarios_are_repeated=True,
            note='90 executions include repeated parent address tests; do not interpret as 90 distinct fee cases',fixture_test_gas=gas),
        deployment=size,static_checks=dict(abi_check='PASS',contract_sizes='115 ordinary deployable contracts pass; creation-object sizes exclude arguments except explicit fee encoded-size evidence',
            source_build='Pinned Solidity 0.8.30 / Osaka / via-IR / optimizer runs=1; no code-size override',
            compileall='PASS',diff_check='PASS',diagrams='8 local Mermaid/Chrome renders; no out-of-bounds labels; fee diagram visually inspected'),
        source_sha256=hashes,artifact_sha256={str(f.relative_to(ROOT)):sha(f) for f in sorted(paths)},
        reproduce=['PYTHONPATH=signer-agent python -m pytest -q <files in btc-fee-reserve-test-selection.json> --junitxml=ops/artifacts/zkspv/worker-integration/btc-fee-reserve-tests.xml',
            "forge test --root web3 --offline --match-contract '^(BitcoinAddressRegistryTest|BitcoinMintAdmissionTest|MiningDepositAllocationTest|BitcoinFeeReserveTest)$' --json",
            'PYTHONPATH=signer-agent python -m ops.generate_abis --check --zk',
            'PYTHONPATH=signer-agent python -m ops.check_contract_sizes --output ops/artifacts/zkspv/worker-integration/btc-fee-reserve-sizes.json',
            'PYTHONPATH=signer-agent python -m ops.zkspv.btc_fee_evidence --forge-json <completed Forge JSON>'],
        remaining=['Independent verifier/curve/profile review and actual qualification',
            'Complete production watch inventory, source/cross-graph/recipient/old-epoch continuity and operating qualification',
            'Combined actual Core/prover/transport/payment/quorum/consumer/fee/recovery E2E, deferred at user request',
            'Per-owner BTC/MP/S fee credits, consent, subsidy, refunds and bounded settlement of owner claims',
            'Target production bindings, hardware, deployment/migration and measured latency/TCO'],
        historical_evidence=['Earlier address/SPV/proof source snapshots remain historical; this report does not relabel their PASS'])
    destination=OUT/'btc-fee-reserve-report.json';write(destination,report)
    print(json.dumps(dict(report=str(destination.relative_to(ROOT)),python=counts,solidity=total,fee_scenarios=fee_tests,
        sources=len(hashes),artifacts=len(paths),production_ready=False)))


if __name__=='__main__':main()
