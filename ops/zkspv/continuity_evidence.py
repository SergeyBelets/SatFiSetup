"""Original-graph client evidence; unsupported migrations retain UNKNOWN/blocked gates."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'ops/artifacts/zkspv/worker-integration'


def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    artifacts=[]
    for name in ('continuity-tests.xml','continuity-tests.log'):
        dest=OUT/name;dest.write_bytes((a.input_directory/name).read_bytes());artifacts.append(dest)
    tree=ET.parse(OUT/'continuity-tests.xml');suites=list(tree.getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    focused=sum(1 for case in tree.getroot().iter('testcase') if case.get('classname','').endswith('test_bitcoin_continuity'))
    if counts['tests']==0 or focused<35 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('CONTINUITY_CHECKS_INCOMPLETE')
    modules=('bitcoin_continuity','continuity_cli','address_transport','address_registration','address_registration_client',
        'address_mint','bitcoin_trust','bitcoin_input_keys','bitcoin_worker','participant_cli','participant_errors',
        'mint_admission','smp1_deposit','service','secure_state','operations','bootstrap_withdrawal_fee',
        'epoch_service','incident_transport','withdrawal_fee_funding','withdrawal_fee_policy','btc_fee_policy','bitcoin_cli')
    paths=[f'signer-agent/signer_agent/{n}.py' for n in modules]
    tests=('bitcoin_continuity','epoch_service','incident_status','operation_status','address_registration','address_transport',
        'address_mint','address_mint_trust','address_mint_admission','address_watch','address_spv_watch','bitcoin_input_keys',
        'recipient_profiles','withdrawal_bootstrap','zk_bootstrap','bootstrap_parallel','bootstrap_receipt_checks','bootstrap_read_finality',
        'ethereum_mainnet','ethereum_signing_binding','ethereum_rpc_budget','signing_service','nonce_recovery','dkg_protocol',
        'btc_fee_reserve','withdrawal_fee','withdrawal_fee_spv','smp1_deposit','security_crypto_review','mint_admission')
    paths += [f'signer-agent/tests/test_{n}.py' for n in tests]
    paths += ['docs/BITCOIN_CONTINUITY.md','docs/README.md','docs/API.md','docs/CONFIGURATION.md','docs/BITCOIN_EPOCH_SERVICE.md',
        'docs/ADDRESS_DEPOSIT_REGISTRY.md','ops/zkspv/continuity_evidence.py','ops/zkspv/report.py',
        'web3/contracts/src/federation/BitcoinAddressRegistry.sol','web3/contracts/src/federation/BitcoinDepositRegistry.sol',
        'web3/contracts/src/federation/BitcoinWithdrawalEscrow.sol','web3/contracts/src/federation/BitcoinBatchRegistry.sol',
        'web3/contracts/src/core/BitcoinDepositDerivation.sol','web3/contracts/src/core/BitcoinWithdrawalFeeDerivation.sol',
        'frost-crypto/src/main.rs','frost-crypto/src/bitcoin.rs']
    report=dict(schema='satoshifi.original-graph-continuity.v1',as_of='2026-11-02',
        status='ORIGINAL_GRAPH_GUARDS_IMPLEMENTED_MIGRATION_UNSUPPORTED',code_implemented=True,profile='ORIGINAL_BTC_ACCOUNTING_GRAPH_V1',
        ready=False,production_ready=False,history_complete=False,cross_graph_asset_migration_supported=False,
        global_onchain_family_registry_implemented=False,python_tests=counts,continuity_specific_tests=focused,
        implemented=['Canonical original registry/common-consumed/backing/escrow/batch/SPV/authorization/DKG/profile identity retention',
            'Identity transaction and independent signer spent journals preserve terminal/generation/closed/reorg facts across state backup',
            'Observed canonical address/fee child keys retained as parent-family aliases, including detection after earlier enrollment',
            'Address request/registration/mint, SMP1 custody, BTC trust/private work and EVM FROST registration/mint/fee hooks',
            'Typed one-time address-registry bootstrap admission before EVM_AUTH nonce/share, including unbound nonce-zero phase',
            'Bounded canonical inventory and named request/payment/reservation/replacement/fee facts with explicit incomplete history',
            'Same-graph continuation only; verifier/registry/accounting/same-key resharing and live custody migration explicitly blocked',
            'Explicit same-identity observation reanchor preserves terminal restrictions, original nonce/spent data and financial holds'],
        boundaries=['Canonical chain/provenance/DKG/service qualification are explicit fixtures; native codec/derivation/curve and SQLite are real',
            'Existing monetary ABI, operation hash schemas, mainnet guest and Solidity contracts unchanged in this continuation',
            'Catalog scope is the participating operator/signer, not a global on-chain enumeration of every family/deployment',
            'Known canonical aliases do not identify all possible undisclosed public tweaks; independence of arbitrary unknown keys is not proved',
            'Non-enumerable consumed/walletOutput, requests, approvals, old signatures and private/orphaned ingress require additional evidence',
            'Original supported receipts do not become current issuance/mint/signing permission; no automatic live-asset transfer'],
        live_dkg_or_evm_quorum_performed=False,fresh_proving_performed=False,actual_core_acceptance_performed=False,
        actual_value_transfer_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        deferred=['Unified combined-contour runner and actual Core/FROST/proof/consumer/value/reorg/recovery acceptance',
            'Independent actual derivation/purpose/profile and global continuity-policy qualification',
            'Any proposed live cross-graph custody/accounting transition requires a separate qualified protocol and complete inventory',
            'Unknown intermediate history under specification 15.11 remains unresolved'],
        source_sha256={n:sha(ROOT/n) for n in sorted(set(paths))},artifact_sha256={str(p.relative_to(ROOT)):sha(p) for p in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    (OUT/'continuity-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,continuity_specific=focused,ready=False)))


if __name__=='__main__':main()
