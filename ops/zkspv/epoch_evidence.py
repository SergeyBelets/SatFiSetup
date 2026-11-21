"""Source-bound old-epoch client evidence. Component checks never imply live acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'ops/artifacts/zkspv/worker-integration'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--input-directory',type=Path,required=True);a=p.parse_args()
    artifacts=[]
    for name in ('epoch-tests.xml','epoch-tests.log'):
        target=OUT/name;target.write_bytes((a.input_directory/name).read_bytes());artifacts.append(target)
    tree=ET.parse(OUT/'epoch-tests.xml');suites=list(tree.getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    focused=sum(1 for case in tree.getroot().iter('testcase') if case.get('classname','').endswith('test_epoch_service'))
    if counts['tests']==0 or focused<40 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('EPOCH_CHECKS_INCOMPLETE')
    modules=('epoch_service','epoch_retention','epoch_cli','address_transport','address_watch','address_mint','address_status',
        'incident_observations','bitcoin_cli','bitcoin_input_keys','bitcoin_worker','bitcoin_onchain','bitcoin_trust',
        'participant_cli','participant_errors','btc_fee_reserve','withdrawal_fee','withdrawal_fee_watch',
        'withdrawal_fee_funding','bootstrap','bootstrap_parallel','operations','secure_state','dkg_protocol','dkg','crypto')
    paths=[f'signer-agent/signer_agent/{n}.py' for n in modules]
    paths += [str(Path('signer-agent/tests')/('test_'+name+'.py')) for name in
        ('epoch_service','incident_status','operation_status','address_registration','address_transport','address_mint','address_mint_trust',
        'address_mint_admission','address_watch','address_spv_watch','bitcoin_input_keys','recipient_profiles','withdrawal_bootstrap',
        'zk_bootstrap','bootstrap_parallel','bootstrap_receipt_checks','bootstrap_read_finality','ethereum_mainnet',
        'ethereum_signing_binding','ethereum_rpc_budget','signing_service','nonce_recovery','dkg_protocol','btc_fee_reserve',
        'withdrawal_fee','withdrawal_fee_spv')]
    paths += ['config/bitcoin-epochs.example.json','docs/BITCOIN_EPOCH_SERVICE.md','docs/ADDRESS_DEPOSIT_REGISTRY.md',
        'docs/README.md','docs/API.md','docs/CONFIGURATION.md','docs/BITCOIN_ADDRESS_SIGNING.md','ops/zkspv/report.py','ops/zkspv/epoch_evidence.py',
        'frost-crypto/src/main.rs','frost-crypto/src/bitcoin.rs','web3/contracts/src/core/BitcoinDepositDerivation.sol',
        'web3/contracts/src/core/BitcoinWithdrawalFeeDerivation.sol','web3/contracts/src/federation/BitcoinAddressRegistry.sol',
        'web3/contracts/src/federation/BitcoinSigningBoard.sol','web3/contracts/src/federation/DKGCoordinator.sol']
    report=dict(schema='satoshifi.epoch-service.v1',as_of='2026-11-02',
        status='CODE_IMPLEMENTED_LIVE_QUALIFICATION_PENDING',code_implemented=True,ready=False,production_ready=False,
        python_tests=counts,epoch_specific_tests=focused,
        implemented=['Bounded durable multi-epoch observation with original wallet/address/signing-board/batch binding',
            'Independent membership, original transport epochs and archive observation witnesses; no silent removal or stale-backup erasure',
            'Original canonical address/fee binding recovery; late payments/refunds and watch under verifier quarantine',
            'Optional original SPV route; no default value transfer or threshold/nonce/authority change',
            'Fresh EOA-signed per-participant retention claims bound to challenge, exact wallet/roster/generations/anchor/expiry',
            'Local native scalar/public-share and existing pinned graph/board/spent-journal checks before attestation',
            'Original distinct threshold excluding departing members; canonical service coverage, qualification and shared funding envelopes',
            'Durable issuance and Bitcoin signer enrollment; dropping optional CLI argument does not bypass old-epoch service guards'],
        boundaries=['Chain, provenance, completed DKG, Core and qualification are explicit fixtures; native codec/share operations, EOA signatures and SQLite are real',
            'Participant retention claims are authenticated operational statements, not remote proof of possession, FROST spending authority or future uptime',
            'One owning pool per epoch; partial/multi-pool coverage is explicitly blocked',
            'Same-key re-enrollment is locally rejected, not a global canonical cross-graph/key-family solution',
            'Existing immutable legacy graphs and unavailable old shares/data cannot be repaired by configuration',
            'Confederation/operator-funded service allowed; actual BTC fee reserve and EVM funding checks still apply'],
        live_dkg_or_evm_quorum_performed=False,actual_core_acceptance_performed=False,fresh_proving_performed=False,
        actual_value_transfer_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        solidity_changed_in_this_continuation=False,native_rust_changed_in_this_continuation=False,
        deferred=['Combined live old-epoch late-funding/withdrawal/refund/FROST/Core/proof/consumer and crash/reorg/restore contour',
            'Production verifier/purpose/profile qualification, resource/funding acceptance and independent review',
            'Canonical cross-graph/key-family continuity and unified combined-contour runner',
            'Unobserved intermediate-history policy difference under specification 15.11'],
        source_sha256={n:sha(ROOT/n) for n in sorted(set(paths))},
        artifact_sha256={str(path.relative_to(ROOT)):sha(path) for path in artifacts},
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'))
    target=OUT/'epoch-service-report.json';target.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python_tests=counts,epoch_specific=focused,ready=False)))


if __name__=='__main__':main()
