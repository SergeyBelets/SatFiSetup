"""Reproduce testnet4 copies of the frozen V2 EVM components.

Only network domains, component names/imports and checkpoint descriptor change.
Mainnet files are inputs and are never rewritten.
"""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'rust/zkspv/state-adapter/contracts'
NAMES = {'BitcoinCanonicalHistory':'BitcoinTestnet4CanonicalHistory',
    'BitcoinTransitionV2':'BitcoinTestnet4TransitionV2',
    'IZkBitcoinStateV2':'IZkBitcoinTestnet4StateV2',
    'BitcoinCheckpointV2':'BitcoinTestnet4CheckpointV2',
    'ZkSpvAdapter':'ZkTestnet4SpvAdapter', 'ZkBitcoinStateV2':'ZkBitcoinTestnet4StateV2'}


def generated():
    for old, new in NAMES.items():
        if old == 'IZkBitcoinStateV2':
            continue  # The interface is declared inside the adapter source.
        src = OUT / (old+'.sol')
        if not src.exists():
            src = ROOT / 'web3/contracts/src/core' / (old+'.sol')
        text = src.read_text()
        # Longest first avoids replacing the state name inside its interface.
        for name in sorted(NAMES, key=len, reverse=True):
            import re
            text = re.sub(r'\b'+name+r'\b',NAMES[name],text)
        text = text.replace('bitcoin-mainnet','bitcoin-testnet4').replace('mainnet-anchor','testnet4-anchor')
        text = text.replace('"satoshifi-core/BitcoinTestnet4','"./BitcoinTestnet4')
        text = text.replace('"satoshifi-core/IZkBitcoinTestnet4','"./IZkBitcoinTestnet4')
        if old == 'BitcoinTransitionV2':
            text = text.replace('"./BitcoinHeaderTime.sol"','"satoshifi-core/BitcoinHeaderTime.sol"')
        if old == 'BitcoinCheckpointV2':
            before = '(c,descriptor)=BitcoinCheckpoint.validate(raw,height,work,prior);'
            if text.count(before)!=1:
                raise ValueError('TESTNET4_CHECKPOINT_TEMPLATE_DRIFT')
            text = text.replace(before,'''(c,)=BitcoinCheckpoint.validate(raw,height,work,prior);
        bytes memory ancestors;
        for(uint256 i;i<prior.length;++i) ancestors=bytes.concat(ancestors,prior[i]);
        bytes[] memory binding=new bytes[](5);
        binding[0]=bytes("bitcoin-testnet4");binding[1]=raw;binding[2]=abi.encodePacked(height);
        binding[3]=abi.encodePacked(work);binding[4]=ancestors;
        descriptor=framedHash(bytes("testnet4-checkpoint-v1"),binding);''')
        yield OUT/(new+'.sol'), text


    view=(ROOT/'web3/contracts/src/federation/ZkSPVServiceStateView.sol').read_text()
    view=view.replace('"../core/BitcoinTransitionV2.sol"','"./BitcoinTestnet4TransitionV2.sol"')
    view=view.replace('BitcoinTransitionV2','BitcoinTestnet4TransitionV2')
    view=view.replace('ZkSPVServiceStateView','ZkTestnet4SPVServiceStateView')
    view=view.replace('IZkSPVServiceState','IZkTestnet4SPVServiceState').replace('IZkSPVServiceAdapter','IZkTestnet4SPVServiceAdapter')
    view=view.replace('MAINNET_IMAGE','TESTNET4_IMAGE').replace('immutable mainnet V2','immutable testnet4 V2')
    view=view.replace('a4c00c90dcc2600acd756df56ae4915e2180d0ebacc20c157382ec184fe0adea',
                      'a6d885a56488897d9b181b5d224652ad67e020d3868060a3e5aae20a4ae82ab8')
    yield OUT/'ZkTestnet4SPVServiceStateView.sol',view


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--check',action='store_true');a=p.parse_args()
    for path,text in generated():
        if a.check:
            if not path.exists() or path.read_text()!=text:raise ValueError('TESTNET4_CONTRACT_DRIFT:'+path.name)
        else:path.write_text(text)


if __name__=='__main__':main()
