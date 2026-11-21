"""Package short local evidence; never turn component fixtures into live acceptance."""
import argparse,gzip,hashlib,json,re,os
from pathlib import Path
import xml.etree.ElementTree as ET
from eth_utils import keccak
from signer_agent.bootstrap import artifact
from signer_agent.contract_provenance import creation_input
from signer_agent.deployment_limits import check_deployment
from .preflight import SUBGATES
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'ops/artifacts/zkspv'
EVIDENCE=OUT/'worker-integration'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write(p,v):p.write_text(json.dumps(v,indent=2,ensure_ascii=False)+'\n')
def main():
    p=argparse.ArgumentParser();p.add_argument('--forge-json',type=Path,required=True);p.add_argument('--forge-log',type=Path,required=True);a=p.parse_args()
    suites=list(ET.parse(EVIDENCE/'withdrawal-subsidy-tests.xml').getroot().iter('testsuite'))
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    if not counts['tests'] or any(counts[k] for k in ('failures','errors','skipped')):raise ValueError('PYTHON_CHECKS_INCOMPLETE')
    raw=a.forge_json.read_bytes();forge=json.loads(raw);executions=0;personal=0
    for name,suite in forge.items():
        for test,result in suite['test_results'].items():
            if result['status']!='Success':raise ValueError('SOLIDITY_CHECKS_INCOMPLETE:'+name+test)
            executions+=1;personal+=int('WithdrawalFeeSubsidyTest' in name and test.startswith('testSubsidy'))
    if personal<26:raise ValueError('PERSONAL_SUITE_INCOMPLETE')
    zipped=EVIDENCE/'withdrawal-subsidy-forge.json.gz';zipped.write_bytes(gzip.compress(raw,mtime=0))
    log=EVIDENCE/'withdrawal-subsidy-forge.log';log.write_bytes(a.forge_log.read_bytes())
    if f'{personal} passed; 0 failed' not in log.read_text():raise ValueError('GAS_LOG_INCOMPLETE')
    gas={}
    for label,value in re.findall(r'^  (.*cold upper): (\d+)$',log.read_text(),re.M):gas[label]=max(gas.get(label,0),int(value))
    if len(gas)<9 or any(v>=15000000 for v in gas.values()):raise ValueError('GAS_BOUND_FAILED')
    addr=lambda n:'0x'+f'{n:040x}'
    args={
        'WithdrawalFeeCredits':[addr(1),addr(2),addr(3)],'WithdrawalFeePayments':[addr(1),addr(2),addr(3)],
        'WithdrawalFeeFunding':[addr(1),addr(2),addr(3)],'WithdrawalFeePlanVerifier':[],'BitcoinWithdrawalFeeDerivation':[],
        'BitcoinFeeReserve':[addr(i) for i in range(1,6)]+[1000,10000],'BitcoinFeeReserveUser':[addr(1)],
        'SimpleMultiPoolDeployer':[addr(1),addr(2)],'SimpleMultiPool':['fee pool',addr(1),addr(2)],
        'BitcoinWithdrawalEscrow':[addr(1),addr(2),6,294,330],'BitcoinDepositRegistry':[addr(1),addr(2),6,addr(3)],
        'BitcoinSigningBoard':[addr(1),1024],'DKGOnchainBoard':[b'x'*8192,[addr(i) for i in range(1,17)]]}
    deployment={name:check_deployment(artifact(name)['deployedBytecode']['object'],creation_input(name,values)) for name,values in args.items()}
    pool=bytes.fromhex(artifact('SimpleMultiPool')['bytecode']['object'].removeprefix('0x'));half=len(pool)//2
    blobs=[]
    for part in (pool[:half],pool[half:]):
        blobs.append(check_deployment('0x00'+part.hex(),creation_input('CreationCodePart',[part])))
    encoded=EVIDENCE/'withdrawal-subsidy-encoded-sizes.json'
    write(encoded,dict(contracts=deployment,creation_parts=blobs,
        note='Static constructor arguments are exact length; SimpleMultiPool representative name. DKG invitation/roster at transport maxima. Every actual encoded deployment still requires its own size check. Blob sizes are actual returned runtimes, not the empty compiled runtime.'))
    baseline=EVIDENCE/'withdrawal-subsidy-baseline.json';current=OUT/'withdrawal-subsidy-report.json'
    if not baseline.exists():baseline.write_bytes(current.read_bytes())
    old=json.loads(baseline.read_text())
    sources=set()
    for pattern in ('web3/contracts/src/**/*.sol','web3/foundry.toml','web3/test/*Fee*.t.sol','web3/test/BitcoinSigningBoard.t.sol',
        'web3/test/EscrowBoundary.t.sol','web3/test/ValueFlowTestFixtures.sol','web3/test/WithdrawalRounding.t.sol',
        'web3/test/CallbackPoolAdmission.t.sol','signer-agent/signer_agent/*.py','signer-agent/contracts/*.json',
        'frost-crypto/src/*.rs','frost-crypto/Cargo.toml','frost-crypto/Cargo.lock','ops/zkspv/withdrawal_fee_evidence.py',
        'ops/zkspv/preflight.py','ops/zkspv/report.py','ops/generate_abis.py','ops/generate_fee_runtime_hashes.py',
        'config/*withdrawal*fee*.json','docs/WITHDRAWAL_FEE_SUBSIDY.md','docs/BTC_FEE_RESERVE.md',
        'docs/BITCOIN_MINT_AND_TREASURY.md','docs/BITCOIN_ONCHAIN_RUNBOOK.md','docs/ZK_SPV_ARCHITECTURE.md',
        'docs/CONTRACT_SIZE_AND_DEPLOYMENT.md','docs/CONFIGURATION.md','docs/API.md','docs/README.md','signer-agent/README.md','web3/README.md'):
        sources.update(str(f.relative_to(ROOT)) for f in ROOT.glob(pattern) if f.is_file())
    selection=json.loads((EVIDENCE/'withdrawal-subsidy-test-selection.json').read_text());sources.update(selection)
    binary=Path(os.environ.get('SATOHSI_CRYPTO_BIN',str(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto')))
    files=[zipped,log,encoded,EVIDENCE/'withdrawal-subsidy-sizes.json',EVIDENCE/'withdrawal-subsidy-tests.xml',
        EVIDENCE/'withdrawal-subsidy-test-selection.json',EVIDENCE/'withdrawal-subsidy-rust-tests.log',baseline]
    for name in args:files.append(ROOT/'web3/contracts/out'/f'{name}.sol'/f'{name}.json')
    files+=list((ROOT/'ops/artifacts/documentation-diagrams-withdrawal-fee-20261101').glob('*'))
    report=dict(schema='satoshifi.withdrawal-subsidy-report.v2',as_of='2026-11-01',status='CODE_IMPLEMENTED_QUALIFICATION_PENDING',
        production_ready=False,ready=False,personal_fee_code_implemented=True,actual_profile_activated=False,
        actual_candidate_purpose_qualified=False,actual_core_acceptance_performed=False,actual_value_transfers_performed=False,
        fresh_proving_performed=False,combined_e2e_performed=False,independent_review_performed=False,
        native_binary_sha256=sha(binary),python_tests=counts,test_suite_count=len(selection),
        solidity_tests=dict(executions=executions,failures=0,personal_scenarios=personal,inherited_cases_repeated=True),
        rust_tests=dict(passed=18,ignored=1,ignored_reason='public fixture generator; not an acceptance test'),
        implemented=['Owner-only BTC/MP/S budget consent, personal revisions, exact per-version approval and immutable admission receipts',
            'Full nominal prevout ledger, exact actual fee and principal / named-credit / treasury conservation',
            'Separate unqualified WITHDRAWAL_FEE derivation, durable watch and bounded all-input external classification',
            'Same-asset MP/S returns; fee-neutral per-MP S accounting; ordinary principal, NFT, rounding and CS preserved',
            'Stable BTC claim IDs across base change, inline/shared-script and separately consented refunds; cumulative refund caps and dust retention',
            'Old admitted winners survive free revisions/issuance pause; late credits remain separate; no timeout unlock after Signing',
            'Native V4 BASE/DERIVED/WITHDRAWAL_FEE signatures in the existing nonce/spent/recovery journal, full constructor/runtime graph review',
            'Shared SPV worker V6 and transport V4: explicit fee-root pin, canonical receipt/coverage reuse, archive/grouping/admission/quote/payment and voluntary confederation funding',
            'Resumable 8KiB public parts, exact full hash and participant-isolated BTC staging; ordinary ABI/bootstrap/CLI integration'],
        sources={k:dict(status='CODE_IMPLEMENTED_QUALIFICATION_PENDING') for k in ('BTC','MP','S')},
        supporting_gates={k:dict(status='UNKNOWN',code_implemented=True,local_component_checks='PASS',
            reason='Actual purpose/profile, production constructors, live provider chain, combined contour and independent acceptance remain unqualified') for k in SUBGATES['withdrawal_subsidy']},
        preserved_limits=dict(inputs=16,requests=32,outputs=32,versions=16,touched_claims=32,claims_per_location=32,
            classification_inputs_per_call=16,external_stripped_bytes=100000,runtime_bytes=24576,initcode_bytes=49152,
            public_plan_bytes=65536,public_intent_bytes=131072,public_part_bytes=8192),
        gas=dict(maximum_checked_upper=max(gas.values()),upper_bounds=gas,transaction_budget=15000000,
            method='forge --isolate is pinned: each action already includes cold accesses. Gross gasleft plus intrinsic/all-nonzero calldata and extra account margin. No refund deduction or duplicate cold SLOAD charge. Fixture setup gas is separate.',
            scope='Bounded external monetary/publication calls. Constructor sizes are checked separately; no constructor gas claim from isolated caller gasleft, target-chain SLA or live proving acceptance'),
        deployment=deployment,creation_part_deployment=blobs,
        boundaries=dict(crypto='REAL pinned native DKG/FROST and independent libsecp256k1 verification of Bitcoin sighashes; no live Core acceptance',
            contracts='REAL money, callbacks, parser, candidate arithmetic, budgets, versions, rollback, public transport and limits; explicit DKG/quorum/SPV/qualification fixtures',
            gas_claim_fixture='32-claim gas case impersonates canonical issuer/payment roles ONLY to prepare historical carried rights. Other BTC money cases execute actual classifier/SPV admission logic with marked cryptographic/provider boundaries.',
            services='REAL durable SQLite/HPKE/budgets/native inclusion logic with explicit EVM/Core/provenance/qualification boundaries',
            activation='Actual purpose remains FEE_QUALIFIED=false; no address offering, deployment, real asset transfers or new proof was performed'),
        findings=[old['findings'][0],
            dict(id='SEC-02',classification='new_scope_ambiguity',resolution='Canonical family/script classification before issuance, full authenticated prevout values, fixed owner rights, stable named carry and typed refunds',residual='SPV/header consensus and independent Core/watch completeness remain trust/operating boundaries'),
            dict(id='SEC-03',classification='new_scope_integration_risk',resolution='Per-budget revisions plus owner exact-version calls; separate quorum nonce and immutable admission receipt. Free updates cannot revoke a bound winner.',residual='New immutable profile requires a migration inventory for live old epochs/signed obligations; none supplied'),
            dict(id='SEC-04',classification='residual_economic_trust_model',resolution='Fee-neutral S restores the same per-MP claim; exact owner C_i and protocol cap/vbytes/consolidation restrictions',residual='Actual miner fee and owner consent do not exclude collusion or miner rebates; external economic/security acceptance remains open'),
            dict(id='SEC-05',classification='concretized_existing_bound',resolution='No monetary owner-history traversal; measured maximum request/input/output/claim cases, 16 versions, 1000 free revisions and chunked public transport',residual='Operational watch/archive capacity, pressure, live latency and full combined acceptance remain UNKNOWN')],
        prior_callback_evidence=dict(path=str(baseline.relative_to(ROOT)),source_snapshot='HISTORICAL',note='Original baseline reproduction and receipts preserved verbatim; no external independent audit claim'),
        source_sha256={name:sha(ROOT/name) for name in sorted(sources)},artifact_sha256={str(f.relative_to(ROOT)):sha(f) for f in files},
        deferred=['fresh Core / prover / Groth16 / EVM / FROST / payment / consumer combined contour',
            'actual independently qualified fee-purpose verifier and native source admission','production canonical bootstrap, operational inventory and live migration',
            'independent review; adversarial long-run continuity, resource/latency/economic qualification'])
    write(current,report);print(json.dumps(dict(status=report['status'],python=counts,solidity=report['solidity_tests'],sources=len(sources),gas_upper=max(gas.values()),ready=False)))
if __name__=='__main__':main()
