"""Manual ULR distribution after mint/import and before ULR authorization sessions."""
import hashlib
import re
from .evm_network import read_block, verify_anchor
from .operations import canonical
from .ulr_treasury import MIN_ULR, SUPPLY

KIND='ulr_distribution'
STATUS='PAUSED_FOR_ULR_DISTRIBUTION'


def validate_release(bootstrap,account,release,accepted,readonly):
    if accepted is not None and not release or readonly and (release or accepted is not None):
        raise ValueError('ULR_DISTRIBUTION_OPTIONS')
    if not release:return
    if account is None or account.address!=bootstrap.profile.get('operator'):
        raise ValueError('ULR_DISTRIBUTION_OPERATOR_REQUIRED')
    if not isinstance(accepted,str) or not re.fullmatch(r'0x[0-9a-f]{64}',accepted):
        raise ValueError('ULR_DISTRIBUTION_FINGERPRINT_REQUIRED')
    steps=[s for s in bootstrap.profile['steps'] if s['kind']==KIND]
    if len(steps)!=1:raise ValueError('ULR_DISTRIBUTION_NOT_PAUSED')
    try:entry=bootstrap.journal.get(bootstrap.prefix+':'+steps[0]['id'])
    except KeyError:raise ValueError('ULR_DISTRIBUTION_NOT_PAUSED') from None
    if entry['status'] not in (STATUS,'CONFIRMED'):raise ValueError('ULR_DISTRIBUTION_NOT_PAUSED')


def snapshot(bootstrap,step):
    b=bootstrap;w3=b.w3
    block=read_block(w3,b.evm_confirmations)
    token=b.contract(step['token']);staking=b.contract(step['staking'])
    def read(c,at,name,*args):return getattr(c.functions,name)(*args).call(block_identifier=at)
    if (read(token,block.number,'totalSupply')!=SUPPLY or read(token,block.number,'decimals')!=18
            or read(staking,block.number,'stakeToken')!=token.address):
        raise ValueError('ULR_DISTRIBUTION_BINDING')
    threshold=read(staking,block.number,"CUSTODIAN_THRESHOLD")
    addresses=list(step['participants'])
    recipient=step.get('issue_recipient')
    if recipient and recipient not in addresses:addresses.append(recipient)
    confirmed=[];pending=[]
    for member in addresses:
        wallet=read(token,block.number,'balanceOf',member);stake=read(staking,block.number,'stakedBalance',member)
        confirmed.append(dict(address=member,wallet_ulr_wei=str(wallet),own_stake_ulr_wei=str(stake),
            total_ulr_wei=str(wallet+stake),at_least_candidate_threshold=wallet+stake>=threshold,at_least_200k_ulr=wallet+stake>=MIN_ULR))
        pending.append(dict(address=member,wallet_ulr_wei=str(read(token,'pending','balanceOf',member)),
            own_stake_ulr_wei=str(read(staking,'pending','stakedBalance',member))))
    state=dict(chain_id=str(w3.eth.chain_id),token=token.address,staking=staking.address,
        total_supply_ulr_wei=str(SUPPLY),candidate_threshold_wei=str(threshold),participants=confirmed)
    if 'token_metadata' in b.profile or 'token' in b.profile.get('ulr',{}):
        from .bootstrap_tokens import check
        state['token_metadata']=check(w3,token.address,'governance',b.profile,block.number)
    fingerprint='0x'+hashlib.sha256(canonical(state).encode()).hexdigest()
    stable=all(c['wallet_ulr_wei']==p['wallet_ulr_wei'] and c['own_stake_ulr_wei']==p['own_stake_ulr_wei'] for c,p in zip(confirmed,pending))
    if w3.eth.get_block(block.number).hash!=block.hash:raise ValueError('EVM_SIGNING_SNAPSHOT_REORG')
    return dict(state,fingerprint=fingerprint,transfers_confirmed=stable,pending=pending,
        anchor=dict(number=str(block.number),hash=block.hash.hex()),manual_confirmation_required=True)


def checkpoint(bootstrap,step,entry,account,*,release=False,accepted=None,readonly=False):
    key=entry['id'];saved=entry['result'];w3=bootstrap.w3
    if entry['status']=='RECOVERY_REQUIRED':raise ValueError('PRIOR_TRANSACTION_RECOVERY_REQUIRED')
    if entry['status']=='CONFIRMED':
        anchor=saved['read_anchor']
        safe=read_block(w3,bootstrap.evm_confirmations)
        verify_anchor(w3,int(anchor['number']),anchor['hash'],safe=safe)
        if release and accepted!=saved['distribution']['fingerprint']:
            raise ValueError('ULR_DISTRIBUTION_CHANGED')
        return None
    view=snapshot(bootstrap,step)
    result=dict(distribution=view)
    if not readonly:bootstrap.journal.update(key,STATUS,result)
    if release:
        if account is None or account.address!=bootstrap.profile.get('operator'):
            raise ValueError('ULR_DISTRIBUTION_OPERATOR_REQUIRED')
        if accepted!=view['fingerprint']:raise ValueError('ULR_DISTRIBUTION_CHANGED')
        if view['transfers_confirmed']:
            bootstrap.journal.update(key,'CONFIRMED',dict(result,read_anchor=view['anchor']))
            return None
    manifest=bootstrap.manifest()
    if not readonly:bootstrap.journal.update(bootstrap.prefix,STATUS,manifest)
    return dict(status=STATUS,step=step['id'],distribution=view,manifest=manifest)
