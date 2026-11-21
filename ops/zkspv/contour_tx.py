"""Read original nonce reservations and bind exact signed call envelopes for adapters."""
import json
from pathlib import Path
from types import SimpleNamespace

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_account._utils.legacy_transactions import Transaction
from hexbytes import HexBytes
from web3 import Web3

from signer_agent.bootstrap import artifact
from signer_agent.bootstrap_parallel import restore_signed
from signer_agent.onboarding import read_private
from signer_agent.operations import Journal


def original(path,prefix,step,identity):
    journal=Journal(path);key=prefix+':'+step['id'];entry=journal.put(key,step)
    if not entry['result'].get('raw_transaction'):
        lane=Journal(str(Path(identity).resolve().with_suffix('.evm-nonce.lock'))+'.sqlite')
        saved=restore_signed(lane,SimpleNamespace(journal=journal),key,step)
        if saved and saved.get('raw_transaction'):entry=dict(entry,result=dict(entry['result'],**saved))
    return entry


def exact(entry,step,chain_id,target,kind,identity,limits):
    if entry['request']!=step:raise ValueError('CONTOUR_CALL_ENVELOPE')
    r=entry['result'];raw=bytes.fromhex(r['raw_transaction'].removeprefix('0x'))
    account=Account.from_key(json.loads(read_private(identity))['private_key'])
    if Web3.keccak(raw).hex()!=r['tx_hash'] or Account.recover_transaction(raw)!=account.address:
        raise ValueError('CONTOUR_CALL_ENVELOPE')
    if raw[0] in (1,2):tx=TypedTransaction.from_bytes(HexBytes(raw)).as_dict();chain=tx['chainId']
    elif raw[0]>=0xc0:
        tx=Transaction.from_bytes(raw).as_dict();chain=(tx['v']-35)//2 if tx['v']>=35 else None
    else:raise ValueError('CONTOUR_CALL_ENVELOPE')
    from signer_agent.bootstrap import encode_args
    spec=next(x for x in artifact(kind)['abi'] if x.get('name')==step['method'] and x.get('type')=='function')
    fn=Web3().eth.contract(address=target,abi=artifact(kind)['abi'])
    data=getattr(fn.functions,step['method'])(*encode_args(spec['inputs'],step['args']))._encode_transaction_data()
    cost=tx['gas']*tx.get('maxFeePerGas',tx.get('gasPrice',0))+tx['value']
    if (chain!=int(chain_id) or bytes(tx['to'])!=bytes.fromhex(target[2:])
            or bytes(tx['data'])!=bytes.fromhex(data.removeprefix('0x')) or tx['value']!=0
            or tx['nonce']!=int(r['nonce']) or tx['gas']>int(limits['max_tx_gas'])
            or cost>int(limits['max_transaction_cost_wei'])):raise ValueError('CONTOUR_CALL_ENVELOPE')
    return r
