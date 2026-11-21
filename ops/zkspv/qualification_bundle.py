"""Collect reproducible candidate evidence without granting independent qualification."""
import argparse
import json
from pathlib import Path
from eth_utils import keccak
from .contour import ROOT,sha
from .acceptance_cases import source_snapshot


def collect(cases_path,host_path,output,receipt_rechecks=None):
    cases=json.loads(Path(cases_path).read_text());host=json.loads(Path(host_path).read_text())
    if cases['source_sha256']!=source_snapshot():raise ValueError('QUALIFICATION_SOURCE_CHANGED')
    for name,h in host['source_sha256'].items():
        if sha(ROOT/name)!=h:raise ValueError('QUALIFICATION_NATIVE_SOURCE_CHANGED:'+name)
    binary=Path(host['binary'])
    if sha(binary)!=host['binary_sha256']:raise ValueError('QUALIFICATION_NATIVE_BINARY_CHANGED')
    all_local=cases['local_program_rows_passed']==cases['local_program_rows']
    verifiers={};build={}
    for path in sorted((ROOT/'web3/contracts/out').glob('*/*.json')):
        a=json.loads(path.read_text());metadata=a.get('metadata',{})
        if isinstance(metadata,str):metadata=json.loads(metadata)
        targets=metadata.get('settings',{}).get('compilationTarget',{})
        for source,name in targets.items():
            if not source.startswith('contracts/src/'):continue
            creation=a.get('bytecode',{}).get('object','').removeprefix('0x');runtime=a.get('deployedBytecode',{}).get('object','').removeprefix('0x')
            if not creation:continue
            if len(runtime)//2>24576 or len(creation)//2>49152:raise ValueError('QUALIFICATION_BUILD_LIMIT')
            build[name]=dict(source=source,artifact_sha256=sha(path),runtime_bytes=len(runtime)//2,creation_bytes=len(creation)//2,
                runtime_template_keccak256='0x'+keccak(bytes.fromhex(runtime)).hex(),
                immutable_references=a.get('deployedBytecode',{}).get('immutableReferences',{}),metadata=metadata['settings'])
    for name,const in [('BitcoinDepositDerivation','QUALIFIED'),('BitcoinWithdrawalFeeDerivation','FEE_QUALIFIED')]:
        source=ROOT/'web3'/build[name]['source']
        import re
        if not re.search(r'bool\s+public\s+constant\s+'+const+r'\s*=\s*false\s*;',source.read_text()):raise ValueError('QUALIFICATION_GATE_CHANGED')
        verifiers[name]=dict(**build[name],source_sha256=sha(source),activation_gate=False)
    verifiers['BitcoinKeyFamilyRegistry']=dict(**build['BitcoinKeyFamilyRegistry'],independently_reviewed=False)
    from .historical_sources import audit
    old=ROOT/'rust/zkspv/deposit-integration'
    old_run=ROOT/json.loads((old/'evidence/current.json').read_text())['directory']
    historical=audit(json.loads((old_run/'preparation.json').read_text())['source_sha256'])
    reverified=[]
    if receipt_rechecks:
        for row in json.loads(Path(receipt_rechecks).read_text()):
            result=json.loads(row['stdout'])
            if row['exit_code']!=0 or result['status']!='VERIFIED' or not result['dev_mode_disabled'] or not result['wrong_image_rejected'] or not result['modified_journal_rejected']:
                raise ValueError('QUALIFICATION_RECEIPT_RECHECK_FAILED')
            reverified.append(dict(id=row['id'],image_id=result['image_id'],receipt_sha256=result['receipt_sha256'],
                journal_sha256=result['journal_sha256'],wrong_image_rejected=True,modified_journal_rejected=True,scope='Retained historical receipt; no fresh proof or current consumer acceptance inferred'))
    report=dict(schema='satoshifi.qualification-review-bundle.v1',
        status='LOCAL_EVIDENCE_COLLECTED_QUALIFICATION_PENDING' if all_local else 'LOCAL_EVIDENCE_INCOMPLETE',
        local_program_rows=cases['local_program_rows'],local_program_rows_passed=cases['local_program_rows_passed'],
        candidates=verifiers,production_build=build,source_sha256=cases['source_sha256'],
        evidence_sha256={str(Path(cases_path).resolve().relative_to(ROOT)):sha(Path(cases_path)),
            str(Path(host_path).resolve().relative_to(ROOT)):sha(Path(host_path))},
        native_host_binary_sha256=host['binary_sha256'],
        historical_deposit_sources=historical,retained_receipt_rechecks=reverified,fresh_proving_performed=False,
        independent_review=None,independent_review_performed=False,
        real_contour=dict(status='DEFERRED_BY_USER',reason='Insufficient device disk capacity; no Core/FROST/Groth16 monetary contour or new proving run'),
        selected_observation_policy=json.loads((ROOT/'ops/zkspv-observation-policy.json').read_text()),
        remaining_qualification=[
            'Current-profile real Core acceptance, threshold authorization, production-image proof/consumer and final settlement/refund records',
            'Complete upstream/correction and consensus-policy qualification beyond successful native witness preparation',
            'Independent derivation and fee-purpose review, recipient/signing/old-epoch operational acceptance',
            'Independent review of canonical family root, original quorum enrollment and SDK pin/recovery',
            'Measured resource/storage/availability and actual production configuration'],
        original_mainnet_guest_changed=False,global_onchain_halt_guaranteed=False,
        qualified=False,fee_qualified=False,ready=False,production_ready=False)
    with Path(output).open('x') as f:json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,required=True);p.add_argument('--host-build',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--receipt-rechecks',type=Path)
    a=p.parse_args();v=collect(a.cases,a.host_build,a.output,a.receipt_rechecks)
    print(json.dumps(dict(status=v['status'],production_contracts=len(v['production_build']),qualified=False,fee_qualified=False,production_ready=False)))


if __name__=='__main__':main()
