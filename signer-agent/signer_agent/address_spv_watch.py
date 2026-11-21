"""Bounded watch-to-SPV scheduling; no mint, fee-reserve or signer authority."""
from .bootstrap import artifact
from .address_mint import read


def graph(client,ctx,block):
    c=client.c;w3=client.w3
    if c['bitcoin_network'] not in ('mainnet','testnet4') or (c['bitcoin_policy'] or {}).get('version')!='2':
        raise ValueError('SPV_ADDRESS_MAINNET_ZK_REQUIRED')
    from .zk_spv import inspect_binding
    from .bitcoin_trust import checked_runtime
    from .zk_profiles import profile as zk_profile
    selected=zk_profile(c['bitcoin_policy'])
    adapter_address=read(ctx['common'],block.number,'spv')
    checked_runtime(w3,adapter_address,selected['adapter'],block.number)
    adapter=w3.eth.contract(address=adapter_address,abi=artifact(selected['adapter'])['abi'])
    state=read(adapter,block.number,'state')
    inspect_binding(w3,state,c['bitcoin_policy'],block.number,adapter_address=adapter_address)
    return dict(chain_id=c['chain_id'],bitcoin_network=c['bitcoin_network'],bitcoin_policy=c['bitcoin_policy'],evm_confirmations=c['evm_confirmations'],
        contracts=dict(relay=dict(address=state),spv_guard=dict(address=adapter_address),
            deposits=dict(address=ctx['common'].address),address_registry=dict(address=ctx['registry'].address)))


def sync(client,*,worker_config=None,transport_config=None,federation=None,recipient=None):
    if bool(worker_config)==bool(transport_config):raise ValueError('ZK_REQUEST_ROUTE_CONFLICT')
    from . import address_spv
    if transport_config:
        from .spv_transport import load_config
    else:
        from .zk_worker import load_config
    selected=load_config(transport_config or worker_config)
    allowed={pool:'MiningPoolV1' for pool in selected.get('address_deposits',{}).get('registries',{})}
    policy=dict(selected,allowed_consumers=allowed)
    with client.store.exclusive():
        b,_,ctx,_,_=client.context();enrolled=graph(client,ctx,b)
        if any(selected[k]!=enrolled[k] for k in ('chain_id','evm_confirmations','bitcoin_policy')) or selected['zk_state']!=enrolled['contracts']['relay']['address']:
            raise ValueError('SPV_ADDRESS_GRAPH_MISMATCH')
        from .spv_automation import identity
        route=dict(mode='transport' if transport_config else 'local',identity=identity(selected['identity_file']),
            directory=selected['directory'],state=selected['zk_state'],profile=selected.get('address_deposits'),
            federation=federation,recipient=recipient,board=selected.get('board'),factory=selected.get('factory'),
            confederation_id=selected.get('confederation_id'))
        old=client.store.get('spv_route')
        if old is not None and old!=route:raise ValueError('SPV_ADDRESS_ROUTE_CHANGED')
        client.store.put('spv_route',route)
        rows=[]
        for identifier,immutable,_ in client.store.intents():
            client.store._check_immutable(immutable)
            for output in client.store.unspent_outputs(identifier):
                rows.append((identifier,immutable,output))
        if not rows:return dict(status='NO_ADDRESS_SPV_WORK',processed=0,ready=False)
        cursor=client.store.get('spv_cursor') or 0;results=[]
        from .bitcoin_rpc import BitcoinRPC
        bitcoin=BitcoinRPC(client.c['bitcoin_rpc'],client.c['bitcoin_cookie'],client.c['bitcoin_policy']['network'],client.c['bitcoin_policy']) if transport_config else None
        for offset in range(min(len(rows),int(client.c['max_messages_per_poll']))):
            index=(cursor+offset)%len(rows);identifier,immutable,output=rows[index]
            client.store.put('spv_cursor',(index+1)%len(rows))
            txid,vout=output['outpoint'].split(':')
            need=dict(kind=address_spv.KIND,consumer=ctx['pool'].address,intent_id=identifier,txid=txid,vout=vout)
            address_spv.check_graph(policy,enrolled,need)
            key='spv_need:'+identifier+':'+output['outpoint']
            try:
                # Only canonical registered bytes may enter the paid worker.
                from .address_mint import binding
                registered=binding(ctx,bytes.fromhex(identifier))
                if registered['intent'].encode().hex()!=immutable['intent']:raise ValueError('ADDRESS_DESCRIPTOR_CONFLICT')
                client.stable(b)
                if transport_config:
                    from .zk_worker_transport import request_from_consumer
                    result=request_from_consumer(transport_config,enrolled,need,bitcoin,federation,recipient)
                else:
                    from .zk_worker import request_from_consumer
                    result=request_from_consumer(worker_config,enrolled,need)
            except ValueError as exc:
                from .participant_errors import participant_exception_code
                code=participant_exception_code(exc)
                if code=='ValueError':raise
                result=dict(status='BLOCKED',reason=code)
            record=dict(need=need,result=result,ready=False)
            client.store.put(key,record);results.append(record)
        return dict(status='ADDRESS_SPV_SYNCED',processed=len(results),needs=results,ready=False)
