"""Read-only independent Core inclusion check, before the local nodes stop."""
import argparse
from pathlib import Path
import shutil
from signer_agent.bitcoin_rpc import BitcoinRPC
from .state_trial import ROOT, read, write
from .wrap_local import sha

def verify(base):
    p=read(base/'preparation.json')
    if not p['bitcoin_rpc_url'].startswith('http://127.0.0.1:'):raise ValueError('Loopback required')
    btc=BitcoinRPC(p['bitcoin_rpc_url'],Path(p['working_directory'])/'regtest/.cookie')
    assert btc.call('getblockchaininfo')['chain']=='regtest'
    rows=read(base/'funding.json');txids=[r['txid'] for r in rows]
    assert len(txids)==2 and len(set(txids))==2
    proof=btc.call('gettxoutproof',txids,bytes.fromhex(rows[0]['block_hash'])[::-1].hex())
    assert set(btc.call('verifytxoutproof',proof))==set(txids)
    target=base/'core-merkleblock.bin';data=bytes.fromhex(proof)
    if target.exists():assert target.read_bytes()==data
    else:target.write_bytes(data)
    artifacts={};folder=base/'consumer-artifacts';folder.mkdir(exist_ok=True)
    for name in p['addresses']:
        if name in ['TestRegtestDepositState','ZkSpvAdapter','RiscZeroGroth16Verifier']:continue
        paths=list((ROOT/'web3/contracts/out').glob('*/'+name+'.json'));assert len(paths)==1
        target=folder/(name+'.json')
        if target.exists():assert sha(target)==sha(paths[0])
        else:shutil.copyfile(paths[0],target)
        artifacts[name]=sha(target)
    result=dict(status='PASS',method='Bitcoin Core gettxoutproof + verifytxoutproof',verified_txids=txids,
        proof_sha256=sha(base/'core-merkleblock.bin'),chain='regtest',headers=btc.call('getblockcount'),consumer_artifact_sha256=artifacts)
    target=base/'core-inclusion.json'
    if target.exists():assert read(target)==result
    else:write(target,result)
    print('Core independently verified both transaction inclusions; ordinary consumer artifacts saved')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    verify(p.parse_args().output.resolve())
