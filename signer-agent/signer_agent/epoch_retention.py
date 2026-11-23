"""Participant-signed operational retention claims, never threshold spending authority."""
import secrets
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct
from . import crypto
from .address_watch import digest
from .operations import canonical
from .spv_transport import hex32,uint

DOMAIN='SATOHSI_BTC_EPOCH_RETENTION_V1'


def challenge(fleet,departing):
    from web3 import Web3
    if (not isinstance(departing,list) or len(departing)>16 or len(set(departing))!=len(departing)
            or any(not Web3.is_checksum_address(p) for p in departing)):raise ValueError('EPOCH_RETENTION_REQUEST')
    body=dict(version='1',fleet_id=fleet.c['fleet_id'],challenge=secrets.token_hex(32),
        departing=sorted(departing),wallet_contexts=sorted(e['wallet_context'] for e in fleet.c['epochs']))
    fleet.lane.put('epoch-review:'+body['challenge'],body)
    return body


def validate_challenge(fleet,body):
    from web3 import Web3
    if (not isinstance(body,dict) or set(body)!={'version','fleet_id','challenge','departing','wallet_contexts'}
            or body['version']!='1' or body['fleet_id']!=fleet.c['fleet_id']
            or body['wallet_contexts']!=sorted(e['wallet_context'] for e in fleet.c['epochs'])
            or not isinstance(body['departing'],list) or len(body['departing'])>16
            or any(not Web3.is_checksum_address(p) for p in body['departing'])
            or body['departing']!=sorted(set(body['departing']))):raise ValueError('EPOCH_RETENTION_REQUEST')
    if hex32(body['challenge'])==bytes(32):raise ValueError('EPOCH_RETENTION_REQUEST')


def message(body):return encode_defunct(primitive=(DOMAIN+'\0'+canonical(body)).encode())


def attest(fleet,request,manifest,registry,state,private_key,configuration):
    """Check the actual local scalar, pinned graph and independent journal; create no BTC nonce."""
    validate_challenge(fleet,request)
    e=fleet.entry(manifest.context.hex());review=fleet.service_review(e)
    s=review['snapshot'];account=Account.from_key(private_key)
    if manifest.to_dict()!=s['manifest'].to_dict() or account.address in request['departing']:raise ValueError('EPOCH_SIGNER_UNAVAILABLE')
    with state.exclusive():
        key=state.get(manifest.context.hex())
        if (not key or key.get('stage')!='active' or key.get('manifest')!=manifest.to_dict()
                or key.get('participant')!=account.address or type(key.get('index')) is not int
                or not 1<=key['index']<=len(manifest.participants)
                or manifest.participants[key['index']-1]!=account.address
                or key['result_hash']!=s['identity']['wallet_result']
                or key['result']['group']!=s['identity']['base_group']):raise ValueError('EPOCH_SIGNER_UNAVAILABLE')
        for field in ('key_package','public_package'):
            value=key['result'].get(field)
            if (not isinstance(value,str) or not 64<=len(value)<=131072 or len(value)%2
                    or any(ch not in '0123456789abcdef' for ch in value)):raise ValueError('EPOCH_SIGNER_UNAVAILABLE')
        # Validate/provision the existing independent journal, never delete or replace it.
        with state._journal():pass
        if state.get('btc-payments-recovery:'+manifest.context.hex()):raise ValueError('PAYMENTS_PAUSED_FOR_RECOVERY')
        from .bitcoin_trust import graph_network, require_registry
        from .secure_state import requires_independent_nonce_ledger
        pinned=require_registry(manifest,key,registry,state,s['identity']['batches'])
        graph=pinned['graph']
        if (graph.get('usage')!='BTC_INPUT_KEYS_V1' or graph['contracts']['address_registry']['address']!=s['identity']['registry']
                or graph['contracts']['batches']['address']!=s['identity']['batches']):raise ValueError('EPOCH_WALLET_BINDING')
        from .bitcoin_onchain import checked_board
        checked_board(manifest,key,registry,state,e['signing_board'])
        with crypto.require_pin(requires_independent_nonce_ledger(manifest.chain_id, graph_network(graph))):
            public=crypto.call('public_share',share=key['result']['signing_share'])['public']
        from .address_mint import read
        if public!=key['result']['public_share'] or bytes(read(s['dkg'],s['block'].number,'getPublicKeyShare',manifest.session_id,account.address)).hex()!=public:
            raise ValueError('EPOCH_SIGNER_UNAVAILABLE')
        issued=int(s['latest'].timestamp)
        body=dict(version='1',fleet_id=fleet.c['fleet_id'],challenge=request['challenge'],request_hash=digest(request),wallet_context=manifest.context.hex(),
            participant=account.address,service_fingerprint=review['service_fingerprint'],custody=review['custody'],derivation=review['derivation'],
            anchor=dict(number=str(s['block'].number),hash=s['block'].hash.hex()),issued_at=str(issued),
            expires_at=str(issued+int(fleet.c['attestation_ttl_seconds'])),public_share=public)
        packet=dict(body=body,signature=Account.sign_message(message(body),private_key).signature.hex())
        # Exact retries preserve the original evidence and do not renew its expiry implicitly.
        key_name='epoch-retention:'+request['challenge']+':'+manifest.context.hex()+':'+account.address
        saved=state.get(key_name)
        if saved:
            old=saved['body'];copy=dict(body,issued_at=old['issued_at'],expires_at=old['expires_at'],anchor=old['anchor'])
            if old!=copy:raise ValueError('EPOCH_RETENTION_CHANGED')
            packet=saved
        else:state.put(key_name,packet)
        marker=dict(fleet_id=fleet.c['fleet_id'],configuration=str(Path(configuration).resolve()))
        from .epoch_service import signer_enrollment
        signer_enrollment(state,manifest.context.hex(),binding=marker)
    return packet


def verify(fleet,request,packet,review):
    try:
        if not isinstance(packet,dict) or set(packet)!={'body','signature'}:raise ValueError('EPOCH_ATTESTATION')
        body=packet['body'];s=review['snapshot'];m=s['manifest']
        fields={'version','fleet_id','challenge','request_hash','wallet_context','participant','service_fingerprint','custody','derivation',
            'anchor','issued_at','expires_at','public_share'}
        if not isinstance(body,dict) or set(body)!=fields:raise ValueError('EPOCH_ATTESTATION')
        who=Account.recover_message(message(body),signature=packet['signature'])
        issued=uint(body['issued_at']);expires=uint(body['expires_at']);now=int(s['latest'].timestamp)
        if (body['version']!='1' or body['fleet_id']!=fleet.c['fleet_id'] or body['challenge']!=request['challenge'] or body['request_hash']!=digest(request)
                or body['wallet_context']!=m.context.hex() or who!=body['participant'] or who not in m.participants
                or who in request['departing'] or body['service_fingerprint']!=review['service_fingerprint']
                or body['custody']!=review['custody'] or body['derivation']!=review['derivation']
                or not issued<=now<expires or not 0<expires-issued<=int(fleet.c['attestation_ttl_seconds'])
                or not isinstance(body['anchor'],dict) or set(body['anchor'])!={'number','hash'}):raise ValueError('EPOCH_ATTESTATION')
        at=uint(body['anchor']['number'],0,s['block'].number);hex32(body['anchor']['hash'])
        if s['client'].w3.eth.get_block(at).hash.hex()!=body['anchor']['hash']:raise ValueError('EPOCH_ATTESTATION')
        from .address_mint import read
        if bytes(read(s['dkg'],s['block'].number,'getPublicKeyShare',m.session_id,who)).hex()!=body['public_share']:
            raise ValueError('EPOCH_ATTESTATION')
        return who
    except (KeyError,TypeError,ValueError,OverflowError):raise ValueError('EPOCH_ATTESTATION') from None


def review(fleet,request,packets):
    validate_challenge(fleet,request)
    try:saved=fleet.lane.get('epoch-review:'+request['challenge'])['request']
    except KeyError:raise ValueError('EPOCH_RETENTION_REQUEST') from None
    if saved!=request:raise ValueError('EPOCH_RETENTION_REQUEST')
    if not isinstance(packets,list) or len(packets)>512:raise ValueError('EPOCH_ATTESTATION')
    if any(not isinstance(p,dict) or not isinstance(p.get('body'),dict) or p['body'].get('wallet_context') not in request['wallet_contexts'] for p in packets):raise ValueError('EPOCH_ATTESTATION')
    results=[];funding={};reviewed={}
    for e in fleet.c['epochs']:
        context=e['wallet_context']
        try:
            current=fleet.service_review(e);s=current['snapshot'];m=s['manifest'];seen=set()
            for packet in packets:
                if packet.get('body',{}).get('wallet_context')!=context:continue
                who=verify(fleet,request,packet,current)
                if who in seen:raise ValueError('EPOCH_ATTESTATION')
                seen.add(who)
            checked=len(seen)>=m.threshold
            result=dict(wallet_context=context,status='RETENTION_CHECKED' if checked else 'THRESHOLD_UNAVAILABLE',
                threshold=str(m.threshold),retained=str(len(seen)),ready=False)
            reviewed[context]=current
            payer=(str(m.chain_id),s['client'].account.address)
            f=funding.setdefault(payer,dict(required=0,balance=int(current['balance']),contexts=[]))
            f['required']+=int(e['minimum_evm_balance_wei']);f['balance']=min(f['balance'],int(current['balance']));f['contexts'].append(context)
        except Exception as exc:
            from .participant_errors import participant_exception_code
            result=dict(wallet_context=context,status='BLOCKED',reason=participant_exception_code(exc),ready=False)
        results.append(result)
    for f in funding.values():
        if f['balance']<f['required']:
            for result in results:
                if result['wallet_context'] in f['contexts']:result.update(status='BLOCKED',reason='EPOCH_EVM_FUNDING_REQUIRED')
    # Recheck every earlier anchor after the last epoch/attestation; no multi-chain atomicity is implied.
    for current in reviewed.values():
        s=current['snapshot'];s['client'].stable(s['block']);s['client'].stable(s['latest'])
        from .address_status import snapshot
        now=s['client'].w3.eth.get_block('latest')
        if (snapshot(s['context'],now.number,0)!=current['custody'] or snapshot(s['context'],now.number,1)!=current['derivation']
                or any(int(p['body']['expires_at'])<=now.timestamp for p in packets if p['body']['wallet_context']==s['manifest'].context.hex())):
            for row in results:
                if row['wallet_context']==s['manifest'].context.hex():row.update(status='BLOCKED',reason='EPOCH_GENERATION_CHANGED')
    result=dict(status='RETENTION_CHECKED' if all(r['status']=='RETENTION_CHECKED' for r in results) else 'BLOCKED',
        challenge=request['challenge'],epochs=results,ready=False,production_ready=False,
        meaning='Fresh operational retention claims; not spending authorization, future availability or rotation execution')
    fleet.lane.put('epoch-retention-result:'+request['challenge']+':'+digest(result),result)
    if result['status']=='RETENTION_CHECKED':
        with fleet.lane.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS epoch_retention_current(fleet TEXT PRIMARY KEY,body TEXT)')
            db.execute('INSERT INTO epoch_retention_current VALUES(?,?) ON CONFLICT(fleet) DO UPDATE SET body=excluded.body',
                (fleet.c['fleet_id'],canonical(dict(request=request,packets=packets))))
    return result
