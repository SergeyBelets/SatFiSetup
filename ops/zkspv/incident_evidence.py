"""Incident/status component evidence; synthetic providers are not live acceptance."""
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
    files=[]
    for n in ('incident-tests.xml','incident-tests.log','incident-solidity.json','incident-solidity.log',
            'incident-abi.log','incident-pins.log','incident-sizes.json','incident-encoded-sizes.json'):
        path=OUT/n;path.write_bytes((a.input_directory/n).read_bytes());files.append(path)
    suites=list(ET.parse(OUT/'incident-tests.xml').getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    if counts['tests']==0 or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('INCIDENT_PYTHON_CHECKS_INCOMPLETE')
    forge=json.loads((OUT/'incident-solidity.json').read_text());results={}
    for name,suite in forge.items():
        tests=suite['test_results']
        if not tests or any(t['status']!='Success' for t in tests.values()):raise ValueError('INCIDENT_SOLIDITY_CHECKS_INCOMPLETE')
        results[name]=dict(passed=len(tests),incident_specific=sum(n.startswith('testIncident') for n in tests))
    if sum(s['incident_specific'] for s in results.values())<5:raise ValueError('INCIDENT_SOLIDITY_CASES_MISSING')
    modules=('incident_transport','incident_cli','incident_observations','address_status','address_status_client',
        'address_transport','address_registration','address_registration_client','address_mint','participant_cli',
        'participant_errors','operation_status','ethereum_intents','coordinator','service','secure_state','crypto',
        'operations','bootstrap','bootstrap_withdrawal_fee','bitcoin_cli','contract_provenance')
    tests=('incident_status','operation_status','address_registration','address_transport','address_mint','address_mint_trust',
        'address_mint_admission','address_watch','bitcoin_input_keys','recipient_profiles','withdrawal_bootstrap','zk_bootstrap',
        'bootstrap_parallel','bootstrap_receipt_checks','bootstrap_read_finality','ethereum_mainnet','ethereum_signing_binding',
        'ethereum_rpc_budget','signing_service','nonce_recovery','dkg_protocol')
    paths=[f'signer-agent/signer_agent/{n}.py' for n in modules]+[f'signer-agent/tests/test_{n}.py' for n in tests]
    paths+=['ops/zkspv/incident_evidence.py','ops/zkspv/report.py','ops/generate_abis.py','web3/foundry.toml',
        'web3/test/BitcoinIncidentMessageBoard.t.sol','web3/test/BitcoinAddressRegistry.t.sol',
        'web3/test/fixtures/incident-status-v1.json','config/incident-service.example.json',
        'docs/ADDRESS_DEPOSIT_REGISTRY.md','docs/API.md','docs/CONFIGURATION.md',
        'frost-crypto/src/main.rs','frost-crypto/Cargo.toml','frost-crypto/Cargo.lock',
        'web3/contracts/src/core/BitcoinDepositDerivation.sol','web3/contracts/src/core/BitcoinWithdrawalFeeDerivation.sol']
    for kind in ('BitcoinIncidentMessageBoard','BitcoinAddressMessageBoard','BitcoinAddressRegistry','DKGAuthorization','FROSTCoordinator'):
        folder='' if kind=='FROSTCoordinator' else 'federation/'
        paths.extend([f'web3/contracts/src/{folder}{kind}.sol',f'signer-agent/contracts/{kind}.json'])
        files.append(ROOT/f'web3/contracts/out/{kind}.sol/{kind}.json')
    report=dict(schema='satoshifi.incident-status.v1',as_of='2026-11-02',
        status='CODE_IMPLEMENTED_LIVE_QUALIFICATION_PENDING',code_implemented=True,ready=False,production_ready=False,
        python_tests=counts,solidity_suites=results,solidity_tests=sum(s['passed'] for s in results.values()),
        inherited_solidity_cases_repeated=True,
        native_hpke_performed=True,native_evm_frost_adapter_performed=True,independent_libsecp_signature_check_performed=True,
        live_dkg_or_evm_quorum_performed=False,actual_core_acceptance_performed=False,
        actual_value_transfer_performed=False,fresh_proving_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        implemented=['Dedicated canonical incident board, domain and HPKE context; REPORT/FORWARD/ACK/STATUS with private salted evidence',
            'Original reporter/commitment proof retained across participant forwarding; explicit bounded same-evidence delivery retries',
            'Public typed setStatus hash independently reconstructed from exact scope/change/sequence/evidence and existing enrolled EVM_AUTH',
            'Mandatory private evidence, signer identity and explicit local review hash before existing FROST private work',
            'Confirmed/latest generations, status sequences, terminal/revocation and closed-issuance guards; resume service/qualification revalidation',
            'Canonical exact historical status receipt recovery, pending-finality distinction and stable shared EOA envelopes',
            'Explicit adoption/retry/reanchor; status observations and deduplicated reorg witnesses retained outside inbox backup in identity transaction lane',
            'Permissive signing fenced by lower/orphaned/terminal/closed observations; freshly reviewed restrictive decisions may reassert a hold',
            'Common opt-in bootstrap exports canonical incident_board; original nonce/spent/financial recovery journals remain intact'],
        trust_boundaries=['HPKE, native EVM share adapter, libsecp256k1 verification and SQLite are real; polynomial signing shares are fixed local test material',
            'Python canonical chain/provenance/DKG/quorum/Core observations are explicit fixtures, not actual live chain acceptance',
            'Solidity transport/status/authorization consumption execute locally; inherited chain DKG/quorum/SPV/qualification remain fixtures',
            'Report authenticity does not prove compromise or evidence truth; the existing EVM_AUTH quorum makes the risk decision',
            'Local observations neither change canonical status nor guarantee on-chain halt during provider/quorum outage',
            'Status revalidation never clears signer/financial pauses, recreates unavailable key shares or migrates key families/graphs'],
        compatibility=dict(mainnet_guest_unchanged=True,bitcoin_signing_and_frost_nonce_journals_unchanged=True,
            registration_and_mint_hashes_unchanged=True,existing_transport_wire_and_dkg_slots_unchanged=True,
            verifier_qualification_flags_unchanged=True),
        deferred=['Combined live Core/prover/Groth16/EVM/FROST/payment/consumer and failure/reorg/recovery contour',
            'Production independent derivation/service/purpose qualification and live deployment',
            'Old-epoch and cross-graph/key-family continuity','Independent review and operational/resource acceptance'],
        native_binary_sha256=sha(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto'),
        source_sha256={n:sha(ROOT/n) for n in sorted(set(paths))},artifact_sha256={str(p.relative_to(ROOT)):sha(p) for p in files})
    (OUT/'incident-status-report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(dict(status=report['status'],python=counts,solidity=report['solidity_tests'],ready=False)))


if __name__=='__main__':main()
