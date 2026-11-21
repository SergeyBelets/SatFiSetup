"""Verify a completed scenario receipt with the ordinary pinned EVM verifier.

This is conditional transition verification, not authentication of the starting
state's entire history or a production state update.
"""
import argparse
import json
import socket
import subprocess
import time
from pathlib import Path
from web3 import Web3
from .evm_receipt import checked_bundle
from .state_trial import artifact, runtime, transact, read, write, ANVIL, ROOT
from .wrap_local import sha


def verify(base):
    prep=read(base/'preparation.json');report=read(base/'groth16/report.json')
    assert report['status']=='VERIFIED' and report['source_unchanged']
    for name,digest in prep['source_sha256'].items():assert sha(ROOT/name)==digest,name
    for name,item in report['artifacts'].items():assert sha(base/'groth16'/name)==item['sha256'],name
    expected=dict(image_id=prep['program_image_id'],program_sha256=prep['program_sha256'],journal_sha256=prep['journal_sha256'])
    metadata=checked_bundle(base/'groth16/verified',expected)
    assert sha(base/'state.journal')==expected['journal_sha256']
    assert metadata['control_root']=='a54dc85ac99f851c92d7c96d7318af41dbe7c0194edfcc37eb4d422a998c1f56'
    bn254=bytes.fromhex(metadata['bn254_control_id_sdk_order'])[::-1]
    assert bn254.hex()=='04446e66d300eb7fb45c9726bb53c793dda407a62e9601618bb43c5c14657ac0'
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    with (base/'evm-anvil.log').open('w') as log:
        process=subprocess.Popen([str(ANVIL),'--host','127.0.0.1','--port',str(port),'--chain-id','31337',
            '--hardfork','osaka','--gas-limit','16777216','--silent'],stdout=log,stderr=subprocess.STDOUT)
        try:
            w3=Web3(Web3.HTTPProvider(f'http://127.0.0.1:{port}',request_kwargs={'timeout':20}))
            for _ in range(100):
                if process.poll() is not None:raise RuntimeError('Anvil exited')
                if w3.is_connected():break
                time.sleep(.05)
            else:raise RuntimeError('Anvil unavailable')
            assert w3.eth.chain_id==31337
            a=artifact('RiscZeroGroth16Verifier');factory=w3.eth.contract(abi=a['abi'],bytecode=a['bytecode']['object'])
            deployed=transact(w3,factory.constructor(bytes.fromhex(metadata['control_root']),bn254))
            address=w3.eth.get_transaction_receipt(deployed['transaction_hash']).contractAddress
            graph=runtime(w3,'RiscZeroGroth16Verifier',address);contract=w3.eth.contract(address=address,abi=a['abi'])
            assert bytes(contract.functions.SELECTOR().call()).hex()==metadata['groth16_selector']
            seal=(base/'groth16/verified/seal.bin').read_bytes();image=bytes.fromhex(expected['image_id']);journal=bytes.fromhex(expected['journal_sha256'])
            cases=[('accepted',seal,image,journal,1),('wrong_image',seal,bytes([image[0]^1])+image[1:],journal,0),
                ('wrong_journal',seal,image,bytes([journal[0]^1])+journal[1:],0),('modified_seal',seal[:-1]+bytes([seal[-1]^1]),image,journal,0)]
            checks={name:transact(w3,contract.functions.verify(s,i,j),status) for name,s,i,j,status in cases}
            result=dict(schema='satoshifi.scenario-evm.v1',status='LOCAL_SCENARIO_VERIFIER_PASS',scenario=prep['scenario'],
                receipt_sha256=metadata['receipt_sha256'],image_id=expected['image_id'],journal_sha256=expected['journal_sha256'],
                ordinary_verifier=graph,deployment=deployed,checks=checks,production_ready=False,public_transactions=0,
                state_transition_acceptance='NOT_PERFORMED',starting_state_scope=prep['starting_state_scope'],
                preparation_sha256=sha(base/'preparation.json'),groth16_report_sha256=sha(base/'groth16/report.json'))
            write(base/'evm.json',result);print(json.dumps(dict(status=result['status'],checks=checks)))
        finally:
            process.terminate();process.wait(timeout=10)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    verify(p.parse_args().output.resolve())
