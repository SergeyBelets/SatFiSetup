"""Explicit network-to-program pins. A witness never selects its verifier."""
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[2]
TESTNET4_IMAGE='a6d885a56488897d9b181b5d224652ad67e020d3868060a3e5aae20a4ae82ab8'
MAINNET_IMAGE='a4c00c90dcc2600acd756df56ae4915e2180d0ebacc20c157382ec184fe0adea'


def profile(policy):
    network=policy.get('network') if isinstance(policy,dict) else None
    if network=='mainnet':
        return dict(network=network,image=MAINNET_IMAGE,state='ZkBitcoinStateV2',adapter='ZkSpvAdapter',view='ZkSPVServiceStateView',
            report=ROOT/'rust/zkspv/guest-integration/report.json',
            program=ROOT/'rust/zkspv/guest-integration/evidence/mainnet-transition.r0bf')
    if network!='testnet4':raise ValueError('ZK_PROFILE_UNSUPPORTED')
    report=ROOT/'rust/zkspv/testnet4-integration/report.json'
    if not report.is_file():raise ValueError('ZK_TESTNET4_GUEST_BUILD_REQUIRED')
    pin=json.loads(report.read_text())
    image=pin.get('program_image_id')
    if (pin.get('status')!='TESTNET4_GUEST_BUILD_PASS' or pin.get('bitcoin_network')!='testnet4'
            or not isinstance(image,str) or re.fullmatch('[0-9a-f]{64}',image) is None
            or image != TESTNET4_IMAGE):
        raise ValueError('ZK_TESTNET4_GUEST_PIN')
    return dict(network=network,image=image,state='ZkBitcoinTestnet4StateV2',adapter='ZkTestnet4SpvAdapter',view='ZkTestnet4SPVServiceStateView',
        report=report,program=ROOT/'rust/zkspv/testnet4-integration/evidence/testnet4-transition.r0bf')


def for_image(image):
    if image==MAINNET_IMAGE:return profile({'network':'mainnet'})
    p=profile({'network':'testnet4'})
    if image!=p['image']:raise ValueError('ZK_GUEST_IMAGE_DRIFT')
    return p


def journal_network(policy):
    return ('bitcoin-'+profile(policy)['network']).encode().ljust(32,b'\0')
