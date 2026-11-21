"""Managed discount collections and mint: typed review, explicit signatures, durable delivery."""
import hashlib
import json
from pathlib import Path

from web3 import Web3
from web3.logs import DISCARD

from .safe_errors import DiagnosticArgumentParser
from . import nft_governance as policy
from .dao_client import account_at, address
from .dkg_protocol import Manifest
from .evm_network import read_block
from .onchain_transport import ceremony_bundle
from .operations import canonical, public_data
from .ulr_treasury_cli import _executor, _write


def main(argv=None):
    p=DiagnosticArgumentParser(description=__doc__)
    p.add_argument('action',choices=['init','status','quote','create-prepare','mint-prepare','propose','custodian-approve','execute'])
    p.add_argument('--rpc',required=True);p.add_argument('--config');p.add_argument('--board')
    p.add_argument('--role',choices=['evm_ceremony','conf_ceremony']);p.add_argument('--factory');p.add_argument('--manager')
    p.add_argument('--chain-id',choices=['1','11155111','31337']);p.add_argument('--confirmations',default='1')
    p.add_argument('--output');p.add_argument('--intent');p.add_argument('--name');p.add_argument('--symbol')
    p.add_argument('--recipient');p.add_argument('--expiration');p.add_argument('--volume');p.add_argument('--discount-bps')
    p.add_argument('--owner');p.add_argument('--token-id');p.add_argument('--amount');p.add_argument('--wrap',action='store_true')
    p.add_argument('--deadline');p.add_argument('--session');p.add_argument('--sender');p.add_argument('--identity-file')
    p.add_argument('--state');p.add_argument('--id');p.add_argument('--send',action='store_true')
    a=p.parse_args(argv);w3=Web3(Web3.HTTPProvider(a.rpc))
    if a.action=='init':
        if not all((a.board,a.role,a.factory,a.manager,a.chain_id,a.output)):
            p.error('init requires --board --role --factory --manager --chain-id --output')
        pin=policy.validate_pin(dict(version='1',chain_id=a.chain_id,confirmations=a.confirmations,
            board=address(a.board),role=a.role,factory=address(a.factory),fee_manager=address(a.manager)))
    else:
        if not a.config:p.error('--config required')
        pin=policy.load_pin(a.config)
    at=read_block(w3,int(pin['confirmations']))
    bundle=ceremony_bundle(w3,pin['board'],pin['role'])
    ctx=policy.context(w3,bundle,pin,at.number)
    manager,auth=ctx['manager'],ctx['authority']
    read=lambda target,method,*args:policy.read(target,at.number,method,*args)
    if a.action=='init':
        _write(a.output,pin)
        print(json.dumps(dict(status='NFT_PIN_PREPARED',fingerprint=hashlib.sha256(canonical(pin).encode()).hexdigest(),pin=pin)))
        return 0
    if a.action in ('status','quote'):
        collection=read(manager,'nftModifier')
        if a.action=='quote':
            if not all(v is not None for v in (a.amount,a.token_id,a.owner)):p.error('quote requires --amount --token-id --owner')
            value=read(manager,'quoteFeeWithNFT',a.wrap,policy.number(a.amount),policy.number(a.token_id),address(a.owner))
            print(json.dumps(dict(status='NFT_FEE_QUOTE',fee_units=str(value),quota_consumed=False)))
        else:
            print(json.dumps(dict(status='NFT_MANAGER_STATUS',manager=manager.address,collection=collection,
                authority=auth.address,validator=ctx['validator'],operation_nonce=str(read(manager,'operationNonce')),
                managed=collection!=policy.ZERO and read(manager,'managedNFTCollections',collection))))
        return 0
    if a.action in ('create-prepare','mint-prepare'):
        if not a.intent:p.error('--intent required')
        if a.action=='create-prepare':
            if not a.name or not a.symbol:p.error('create-prepare requires --name --symbol')
            helper,values=policy.CREATE,[a.name,a.symbol]
        else:
            if not all(v is not None for v in (a.recipient,a.expiration,a.volume,a.discount_bps)):
                p.error('mint-prepare requires --recipient --expiration --volume --discount-bps')
            helper,values=policy.MINT,[read(manager,'nftModifier'),address(a.recipient),
                policy.number(a.expiration),policy.number(a.volume),policy.number(a.discount_bps)]
        from .ulr_treasury import intent_for
        intent=intent_for(w3,manager,policy.KIND,helper,values,auth.address,pin['role'],at.number)
        policy.review(w3,intent,bundle,at.number,pin)
        _write(a.intent,intent)
        print(json.dumps(dict(status='NFT_OPERATION_PREPARED',operation_hash=intent['operation_hash'],intent=intent)))
        return 0
    if not a.intent:p.error('--intent required')
    intent=public_data(json.loads(Path(a.intent).read_text()))
    reviewed=policy.review(w3,intent,bundle,at.number,pin)
    if reviewed['complete']:
        print(json.dumps(dict(status='ALREADY_EXECUTED',operation_hash=intent['operation_hash'])))
        return 0
    sender=account_at(a.identity_file).address if a.identity_file else address(a.sender) if a.sender else None
    if sender is None:p.error('--sender or --identity-file required')
    if a.send and (not a.identity_file or not a.state):p.error('--send requires own --identity-file and --state')
    executor=_executor(w3,dict(chain_id=pin['chain_id'],snapshots=manager.address,confirmations=pin['confirmations']),
                       at,manager,auth.address,policy.KIND)
    op=reviewed['operation_hash'];identifier=a.id or 'nft-'+op.hex()
    if a.action=='propose':
        if not a.deadline:p.error('--deadline required')
        deadline=policy.number(a.deadline)
        if deadline<=at.timestamp:raise ValueError('NFT_SESSION_DEADLINE')
        board=ctx['board']
        if sender!=read(board,'operator') and not read(board,'canPublishIntent',sender):
            raise ValueError('NFT_BOARD_PUBLISHER_REQUIRED')
        coordinator=policy.contract(w3,'FROSTCoordinator',read(auth,'coordinator'))
        for label,item,kind in (('coordinator',coordinator,'FROSTCoordinator'),('board',board,'DKGOnchainBoard')):
            executor.contracts[label]=item
            executor.config['contracts'][label]=dict(address=item.address,artifact=kind,
                runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(item.address,at.number))))
        if len(canonical(dict(intent,session=str(2**256-1))).encode())>32768:raise ValueError('NFT_INTENT_TOO_LARGE')
        result=executor.transact('coordinator','createSessionFromDKG',
            [Manifest.from_dict(bundle).session_id,op,manager.address,deadline],sender,a.identity_file,a.send,a.state,identifier+'-open')
        if a.send:
            receipt=w3.eth.get_transaction_receipt(result['tx_hash'])
            events=coordinator.events.SessionCreated().process_receipt(receipt,errors=DISCARD)
            if len(events)!=1:raise ValueError('NFT_SESSION_RECEIPT')
            sid=int(events[0]['args']['sessionId']);public=canonical(dict(intent,session=str(sid))).encode()
            executor.transact('board','publishIntent',[sid,public],sender,a.identity_file,True,a.state,identifier+'-publish')
            result=dict(status='AWAITING_PARTICIPANTS',session=str(sid),operation_hash=intent['operation_hash'])
        print(json.dumps(result));return 0
    if a.action=='custodian-approve':
        if ctx['validator']==policy.ZERO:raise ValueError('NFT_NO_CUSTODIAN_VALIDATOR')
        validator=policy.contract(w3,'CustodianValidator',ctx['validator'])
        if read(validator,'custodianSigned',op,sender):
            print(json.dumps(dict(status='ALREADY_APPROVED',operation_hash=intent['operation_hash'])));return 0
        executor.contracts['validator']=validator
        executor.config['contracts']['validator']=dict(address=validator.address,artifact='CustodianValidator',
            runtime_hash=Web3.to_hex(Web3.keccak(w3.eth.get_code(validator.address,at.number))))
        result=executor.transact('validator','registerCustodianSignature',[op],sender,a.identity_file,a.send,a.state,identifier+'-custodian')
        print(json.dumps(result));return 0
    if not read(auth,'isOperationApproved',op,manager.address):
        if a.session is None:raise ValueError('NFT_FINALIZED_SESSION_REQUIRED')
        if not a.send:
            print(json.dumps(dict(status='AUTHORIZATION_REQUIRED',operation_hash=intent['operation_hash'])));return 0
        executor.transact('authorization','authorizeOperation',[policy.number(a.session),op,manager.address],
            sender,a.identity_file,True,a.state,identifier+'-auth')
    result=executor.transact('target',policy.METHODS[intent['hash_method']],
        [*reviewed['arguments'],op],sender,a.identity_file,a.send,a.state,identifier+'-execute')
    print(json.dumps(result));return 0
