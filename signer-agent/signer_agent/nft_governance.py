"""Typed NFT governance on an independently pinned entity/fee-manager graph.

New collections are created by the reviewed manager, not an arbitrary issuer.
The same review runs at every private/retransmit signing boundary.
"""
import json
from pathlib import Path

from eth_utils.abi import function_abi_to_4byte_selector
from web3 import Web3

from .bootstrap import artifact, encode_args
from .dao_client import address
from .dkg_protocol import Manifest
from .operations import public_data
from .ulr_treasury import authority_context, contract, proven, read, number

KIND = 'FederationFeeManager'
CREATE = 'getCreateNFTModifierOperationHash'
MINT = 'getMintNFTOperationHash'
METHODS = {CREATE: 'createNFTModifier', MINT: 'mintNFT'}
BASIC = {'getSetFeeConfigOperationHash', 'getSetTreasuryOperationHash',
    'getSetNFTModifierOperationHash', 'getSetFeeDistributorOperationHash',
    'getSetWalletRegistryOperationHash', 'getSetProjectWalletShareBpsOperationHash',
    'getSetFeeCollectorOperationHash'}
ZERO = '0x' + '00' * 20


def validate_pin(pin):
    if (not isinstance(pin, dict) or set(pin) != {'version','chain_id','confirmations','board','role','factory','fee_manager'}
            or pin['version'] != '1' or pin['chain_id'] not in ('1','11155111','31337')
            or pin['role'] not in ('evm_ceremony','conf_ceremony')
            or not (12 if pin['chain_id']=='1' else 1) <= number(pin['confirmations']) <= 256):
        raise ValueError('NFT_PIN_SCHEMA')
    for key in ('board','factory','fee_manager'):
        if address(pin[key]) != pin[key] or pin[key] == ZERO:
            raise ValueError('NFT_PIN_ADDRESS')
    return pin


def load_pin(path):
    return validate_pin(public_data(json.loads(Path(path).read_text())))


def context(w3, bundle, pin, at, *, require_finalized=True):
    from .evm_network import read_block
    from .onchain_transport import _board_and_invitation
    validate_pin(pin)
    block = w3.eth.get_block(at)
    at = block.number
    if str(w3.eth.chain_id) != pin['chain_id']:
        raise ValueError('NFT_CHAIN_POLICY')
    if require_finalized and at > read_block(w3, int(pin['confirmations'])).number:
        raise ValueError('NFT_UNFINALIZED')
    invite = bundle.get('onboarding',{}).get('invitation',{})
    manifest = Manifest.from_dict(bundle)
    if (bundle.get('role') != pin['role'] or bundle.get('onchain_board') != pin['board']
            or manifest.purpose != 'EVM_AUTH' or manifest.chain_id != w3.eth.chain_id
            or number(pin['confirmations']) < int(invite.get('evm_confirmations','256'))):
        raise ValueError('NFT_ENROLLMENT_REQUIRED')
    board, accepted = _board_and_invitation(w3, pin['board'], block)
    if accepted != invite or read(board, at, 'registry') != manifest.registry:
        raise ValueError('NFT_ENROLLMENT_REQUIRED')
    kind = 'ConfederationFactory' if pin['role']=='conf_ceremony' else 'FederationFactory'
    factory = proven(w3, kind, pin['factory'], at)
    from .contract_provenance import prove_creation
    for target, target_kind in ((factory,kind),):
        proof = prove_creation(w3,target.address,target_kind,None,at)
        if proof.get('method') != 'direct' or proof.get('sender') != invite.get('operator'):
            raise ValueError('NFT_CREATION_ORIGIN')
    entity = read(factory, at, 'getConfederation' if pin['role']=='conf_ceremony' else 'getFederation',
                  invite['confederation_id' if pin['role']=='conf_ceremony' else 'federation_id'])
    auth_address, manager_address = (entity[7],entity[12]) if pin['role']=='conf_ceremony' else (entity[4],entity[7])
    validator_address = entity[8] if pin['role']=='conf_ceremony' else ZERO
    if manager_address != pin['fee_manager']:
        raise ValueError('NFT_MANAGER_BINDING')
    manager = proven(w3,KIND,pin['fee_manager'],at)
    proof = prove_creation(w3,manager.address,KIND,None,at)
    if proof.get('method') != 'direct' or proof.get('sender') != invite.get('operator'):
        raise ValueError('NFT_CREATION_ORIGIN')
    auth = authority_context(w3,auth_address,at)
    if (read(auth,at,'registry') != manifest.registry or bytes(read(auth,at,'dkgId')) != manifest.session_id
            or read(manager,at,'frostMultisig') != auth.address
            or read(manager,at,'custodianValidator') != validator_address
            or read(factory,at,'dkgCoordinator') != manifest.registry
            or read(factory,at,'frostCoordinator') != read(auth,at,'coordinator')):
        raise ValueError('NFT_AUTHORITY_BINDING')
    if validator_address != ZERO:
        from .bitcoin_trust import checked_runtime
        checked_runtime(w3,validator_address,'CustodianValidator',at)
        validator=contract(w3,'CustodianValidator',validator_address)
        if read(validator,at,'frostMultisig') != auth.address:
            raise ValueError('NFT_AUTHORITY_BINDING')
    if bytes(w3.eth.get_block(at).hash) != bytes(block.hash):
        raise ValueError('NFT_SNAPSHOT_REORG')
    return dict(manager=manager,authority=auth,validator=validator_address,board=board,block=block)


def arguments(helper, values):
    if helper not in METHODS or not isinstance(values,list):
        raise ValueError('NFT_INTENT_METHOD')
    abi = next(row for row in artifact(KIND)['abi'] if row.get('name') == helper)
    return encode_args(abi['inputs'],values)


def receipt_hash(helper, values, collection=None):
    method = METHODS[helper]
    abi = next(row for row in artifact(KIND)['abi'] if row.get('name') == method)
    selector = function_abi_to_4byte_selector(abi)
    if helper == CREATE:
        types, vals = ['bytes4','string','string','address'], [selector,*values,collection]
    else:
        types, vals = ['bytes4','address','address','uint256','uint256','uint256'], [selector,*values]
    from eth_abi import encode
    return Web3.keccak(encode(types,vals))


def review(w3, intent, bundle, at, pin, *, require_finalized=True):
    from .ethereum_intents import validate_signing_binding
    ctx = context(w3,bundle,pin,at,require_finalized=require_finalized)
    at = ctx['block'].number
    target, auth = ctx['manager'], ctx['authority']
    helper = intent.get('hash_method')
    if (intent.get('artifact') != KIND or intent.get('target') != target.address
            or intent.get('chain_id') != pin['chain_id'] or intent.get('role') != pin['role']):
        raise ValueError('NFT_INTENT_BINDING')
    validate_signing_binding(intent,bundle,read(auth,at,'coordinator'))
    values = arguments(helper,intent.get('hash_args'))
    raw = intent.get('operation_hash','')
    if not isinstance(raw,str) or len(raw.removeprefix('0x')) != 64:
        raise ValueError('NFT_OPERATION_HASH')
    op = bytes.fromhex(raw.removeprefix('0x'))
    complete = bool(read(auth,at,'executedOperations',op))
    if complete:
        collection = read(target,at,'nftCreatedCollections',op) if helper==CREATE else None
        if bytes(read(target,at,'nftOperationReceipts',op)) != receipt_hash(helper,values,collection):
            raise ValueError('NFT_OPERATION_RECEIPT')
    else:
        if helper == CREATE:
            if (read(target,at,'nftModifier') != ZERO or len(values) != 2
                    or any(not isinstance(v,str) or not 1 <= len(v.encode()) <= 128 for v in values)):
                raise ValueError('NFT_COLLECTION_CONFIGURATION')
        else:
            collection,to,expiry,volume,discount = values
            if (collection != read(target,at,'nftModifier') or collection==ZERO or to==ZERO
                    or expiry <= ctx['block'].timestamp or volume==0 or discount>10000
                    or not read(target,at,'managedNFTCollections',collection)):
                raise ValueError('NFT_MINT_CONFIGURATION')
            # This flag can only be written by canonical manager CREATE; it proves
            # the child's constructor/issuer path without trusting self-reported roles.
            nft = contract(w3,'FederationNFTModifier',collection)
            from .bitcoin_trust import checked_runtime
            checked_runtime(w3,collection,'FederationNFTModifier',at)
            for role in ('DEFAULT_ADMIN_ROLE','MINTER_ROLE','USAGE_ROLE'):
                if not read(nft,at,'hasRole',read(nft,at,role),target.address):
                    raise ValueError('NFT_ISSUER_BINDING')
        if bytes(read(target,at,helper,*values)) != op:
            raise ValueError('NFT_OPERATION_HASH')
    if bytes(w3.eth.get_block(at).hash) != bytes(ctx['block'].hash):
        raise ValueError('NFT_SNAPSHOT_REORG')
    return dict(ctx,complete=complete,arguments=values,operation_hash=op)


def guard_signing_session(client, session):
    from .bitcoin_trust import checked_runtime
    payload = getattr(client,'nft_intent_context',None)
    if session.origin_contract == ZERO:
        if payload is not None:raise ValueError('NFT_SESSION_BINDING')
        return
    anchor = getattr(client,'_read_snapshot',None) or client.w3.eth.get_block('latest')
    try:
        checked_runtime(client.w3,session.origin_contract,KIND,anchor.number)
    except ValueError as error:
        if payload is None and str(error)=='BTC_CONTRACT_BUILD_MISMATCH':return
        raise
    if payload is None:raise ValueError('NFT_TYPED_INTENT_REQUIRED')
    intent,bundle,pin = payload
    if (intent.get('artifact') != KIND or intent.get('operation_hash','').removeprefix('0x') != session.message_hash.hex()
            or intent.get('target','').lower() != session.origin_contract.lower()
            or intent.get('coordinator','').lower() != client.address.lower()):
        raise ValueError('NFT_SESSION_BINDING')
    from .onchain_transport import board_at
    for number_ in (anchor.number,'latest'):
        at = client.w3.eth.get_block(number_).number
        if bundle.get('onchain_board'):
            board = board_at(client.w3,bundle['onchain_board'],client.w3.eth.get_block(at))
            if json.loads(bytes(read(board,at,'intents',session.session_id))) != intent:
                raise ValueError('NFT_PUBLIC_INTENT_REQUIRED')
        helper = intent.get('hash_method')
        if helper in METHODS:
            if pin is None or not bundle.get('onchain_board'):raise ValueError('NFT_PIN_REQUIRED')
            if review(client.w3,intent,bundle,at,pin,require_finalized=number_!='latest')['complete']:
                raise ValueError('NFT_OPERATION_COMPLETE')
        else:
            if helper not in BASIC:raise ValueError('NFT_INTENT_METHOD')
            checked_runtime(client.w3,session.origin_contract,KIND,at)
            manager = contract(client.w3,KIND,session.origin_contract)
            abi = next(row for row in manager.abi if row.get('name')==helper)
            args = encode_args(abi['inputs'],intent['hash_args'])
            if bytes(read(manager,at,helper,*args)) != session.message_hash:
                raise ValueError('NFT_OPERATION_HASH')
    if bytes(client.w3.eth.get_block(anchor.number).hash) != bytes(anchor.hash):
        raise ValueError('NFT_SNAPSHOT_REORG')
    client.check_read_snapshot()
