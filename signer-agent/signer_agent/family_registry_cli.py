"""Inspect/pin an original Bitcoin root and prepare enrollment for a later graph."""
import json
from pathlib import Path
from web3 import Web3
from .evm_rpc import evm_web3
from .safe_errors import DiagnosticArgumentParser
from . import family_registry as policy
from .bootstrap import artifact
from .bitcoin_continuity import Catalog
from .contract_provenance import prove_creation
from .dkg_protocol import Manifest
from .evm_network import read_block
from .operations import canonical


def origin(w3, p, board_address):
    """Tie the root to the ORIGINAL board/operator/DKG, not a new invitation."""
    from .onchain_transport import ceremony_bundle
    bundle = ceremony_bundle(w3, board_address, 'conf_ceremony')
    invite=bundle['onboarding']['invitation'];m=Manifest.from_dict(bundle)
    at=read_block(w3,int(invite['evm_confirmations']))
    root=policy.inspect(w3,p,at.number)
    auth=w3.eth.contract(address=p['initial_authority'],abi=artifact('DKGAuthorization')['abi'])
    prove_creation(w3,auth.address,'DKGAuthorization',None,at.number)
    proof=prove_creation(w3,root.address,'BitcoinKeyFamilyRegistry',[auth.address],at.number)
    def get(name):return getattr(auth.functions,name)().call(block_identifier=at.number)
    if (m.purpose!='EVM_AUTH' or m.chain_id!=int(p['chain_id']) or m.registry!=p['dkg']
            or get('registry')!=p['dkg'] or get('coordinator')!=p['coordinator'] or bytes(get('dkgId'))!=m.session_id
            or list(get('getParticipants'))!=list(m.participants) or get('threshold')!=m.threshold
            or Web3.to_checksum_address(proof['sender'])!=invite['operator']):raise ValueError('FAMILY_ROOT_ORIGIN')
    dkg=w3.eth.contract(address=m.registry,abi=artifact('DKGCoordinator')['abi'])
    completed=dkg.functions.getCompletedResult(m.session_id).call(block_identifier=at.number)
    if (bytes(completed[0])!=m.context or bytes(completed[1])!=bytes(get('resultHash'))
            or bytes(completed[3])!=Web3.keccak(text='EVM_AUTH') or bytes(completed[4])!=bytes(get('groupPubkey'))
            or list(completed[5])!=list(m.participants) or int(completed[6])!=m.threshold):raise ValueError('FAMILY_ROOT_ORIGIN')
    if w3.eth.get_block(at.number).hash!=at.hash:raise ValueError('CONTINUITY_REORG')
    fingerprint=Web3.to_hex(Web3.keccak(text=canonical(dict(policy=p,original_board=board_address))))
    return at,bundle,fingerprint


def main(argv):
    parser=DiagnosticArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('inspect','pin','enroll-prepare'))
    parser.add_argument('--rpc',required=True)
    parser.add_argument('--board',required=True,help='Original root authority DKGOnchainBoard')
    parser.add_argument('--policy',required=True,help='Public root policy, checked against original board and constructors')
    parser.add_argument('--identity-file')
    parser.add_argument('--accept-fingerprint')
    parser.add_argument('--registry')
    parser.add_argument('--deadline')
    parser.add_argument('--output')
    args=parser.parse_args(argv)
    p=policy.validate(json.loads(Path(args.policy).read_text()))
    w3=evm_web3(args.rpc, timeout=30)
    board=Web3.to_checksum_address(args.board)
    at,bundle,fingerprint=origin(w3,p,board)
    result=dict(status='FAMILY_ROOT_REVIEWED',policy=p,original_board=board,fingerprint=fingerprint,ready=False)
    if args.action!='inspect':
        if not args.identity_file:raise ValueError('FAMILY_ROOT_IDENTITY_REQUIRED')
        catalog=Catalog(identity_file=args.identity_file)
        if args.action=='pin':
            if args.accept_fingerprint!=fingerprint:raise ValueError('FAMILY_ROOT_FINGERPRINT')
            result.update(policy.install(w3,catalog,p,at.number))
        else:
            if policy.load_pin(catalog,p['chain_id'])!=p:raise ValueError('FAMILY_ROOT_PIN_CHANGED')
            if not args.registry or not args.deadline or not args.output:raise ValueError('FAMILY_ROOT_ENROLLMENT_ARGS')
            result=policy.enrollment_plan(w3,p,bundle,Web3.to_checksum_address(args.registry),args.deadline,args.rpc,
                bundle['onboarding']['invitation']['evm_confirmations'])
    if args.output:
        # A prepared Bootstrap profile contains this participant's RPC URL.
        # Keep it private and never echo routing credentials in the summary.
        from .dkg import atomic_private
        atomic_private(args.output,(canonical(result)+'\n').encode())
    if args.action=='enroll-prepare' and 'deployment_id' in result:
        result=dict(status='FAMILY_ENROLLMENT_PREPARED',deployment_id=result['deployment_id'],
            output=str(Path(args.output).resolve()),root=p['address'],registry=args.registry,ready=False)
    print(json.dumps(result));return 0
