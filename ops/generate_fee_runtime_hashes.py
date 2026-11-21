"""Pin stateless fee verifier runtimes without embedding them in reserve initcode.

Build ordinary artifacts, run this generator, rebuild, then run --check. The
verifiers do not import this generated source, so the process has no hash cycle.
"""
import argparse
from pathlib import Path
from eth_utils import keccak
from signer_agent.bootstrap import artifact

def main():
    p=argparse.ArgumentParser();p.add_argument('--check',action='store_true');a=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    result='// SPDX-License-Identifier: MIT\npragma solidity ^0.8.30;\n// Generated from ordinary-build runtime artifacts.\nlibrary BitcoinFeeCodeHashes {\n'
    for const,name in [('DONOR','FROSTVerifier'),('FUNDING','BitcoinFeeFundingVerifier'),('PLAN','BitcoinFeePlanVerifier')]:
        compiled=artifact(name)
        if compiled['deployedBytecode'].get('immutableReferences'):raise ValueError('STATELESS_VERIFIER_REQUIRED')
        result+='    bytes32 internal constant '+const+'=0x'+keccak(bytes.fromhex(compiled['deployedBytecode']['object'].removeprefix('0x'))).hex()+';\n'
    result+='}\n';path=root/'web3/contracts/src/federation/BitcoinFeeCodeHashes.sol'
    if a.check:
        if path.read_text()!=result:raise ValueError('FEE_VERIFIER_RUNTIME_PIN_DRIFT')
    else:path.write_text(result)
if __name__=='__main__':main()
