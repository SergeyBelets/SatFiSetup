"""Operator-controlled, durable funding checkpoint before bootstrap transactions."""
import re
from web3 import Web3
from .evm_network import read_block

TABLE='bootstrap_funding_pauses'


def minimum(value):
    if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]{0,77}',value) or int(value)>=2**256:
        raise ValueError('BOOTSTRAP_FUNDING_MINIMUM_REQUIRED')
    return value


def policy(profile, participant_minimum, operator_minimum):
    amount=minimum(participant_minimum)
    operator_amount=minimum(operator_minimum if operator_minimum is not None else amount)
    if int(operator_amount)<int(amount):raise ValueError('BOOTSTRAP_FUNDING_MINIMUM_REQUIRED')
    invite=profile.get('onboarding',{}).get('invitation',{})
    participants=invite.get('participants')
    if participants is None:
        participants=profile.get('evm_ceremony',{}).get('participants')
    operator=profile.get('operator')
    if (not isinstance(participants,list) or not participants or len(set(participants))!=len(participants)
            or operator not in participants or any(not Web3.is_checksum_address(a) for a in participants)):
        raise ValueError('BOOTSTRAP_FUNDING_ROSTER_REQUIRED')
    return dict(operator=operator,participants=participants,minimum_wei=amount,operator_minimum_wei=operator_amount)


def snapshot(bootstrap, terms):
    w3=bootstrap.w3
    block=read_block(w3,bootstrap.evm_confirmations)
    rows=[]
    for member in terms['participants']:
        confirmed=w3.eth.get_balance(member,block_identifier=block.number)
        pending=w3.eth.get_balance(member,block_identifier='pending')
        required=terms['operator_minimum_wei'] if member==terms['operator'] else terms['minimum_wei']
        rows.append(dict(address=member,confirmed_balance_wei=str(confirmed),pending_balance_wei=str(pending),
            minimum_wei=required,ready=min(confirmed,pending)>=int(required)))
    if w3.eth.get_block(block.number).hash!=block.hash:raise ValueError('EVM_SIGNING_SNAPSHOT_REORG')
    return dict(anchor=dict(number=str(block.number),hash=Web3.to_hex(block.hash)),participants=rows,
        ready=all(r['ready'] for r in rows),manual_release_required=True)


def checkpoint(bootstrap,account,*,pause=False,release=False,participant_minimum=None,operator_minimum=None,readonly=False):
    """Called under the bootstrap journal lock; funding never signs a transaction."""
    import json
    if pause and release or (participant_minimum is not None or operator_minimum is not None) and not pause:
        raise ValueError('BOOTSTRAP_FUNDING_OPTIONS')
    if readonly and (pause or release):raise ValueError('BOOTSTRAP_FUNDING_OPTIONS')
    with bootstrap.journal.db() as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()
        row=db.execute('SELECT terms,status FROM '+TABLE+' WHERE deployment=?',(bootstrap.prefix,)).fetchone() if exists else None
        if not row and not (pause or release):return None
        if pause or release:
            if account is None or account.address!=bootstrap.profile.get('operator'):
                raise ValueError('BOOTSTRAP_FUNDING_OPERATOR_REQUIRED')
        if pause:
            terms=policy(bootstrap.profile,participant_minimum,operator_minimum)
            if row and (json.loads(row['terms'])!=terms or row['status']!='PAUSED'):
                raise ValueError('BOOTSTRAP_FUNDING_POLICY_MISMATCH')
            if not row:
                # A pause cannot undo already signed work or stop a running DKG clock.
                entries=bootstrap.journal.list()
                step_ids={bootstrap.prefix+':'+s['id'] for s in bootstrap.profile['steps']}
                if any(e['id'] in step_ids and (e['result'].get('raw_transaction') or e['status']=='CONFIRMED') for e in entries):
                    raise ValueError('BOOTSTRAP_FUNDING_TOO_LATE')
                db.execute('CREATE TABLE IF NOT EXISTS '+TABLE+' (deployment TEXT PRIMARY KEY,terms TEXT NOT NULL,status TEXT NOT NULL)')
                db.execute('INSERT INTO '+TABLE+' VALUES (?,?,?)',(bootstrap.prefix,json.dumps(terms,sort_keys=True),'PAUSED'))
                row=dict(terms=json.dumps(terms),status='PAUSED')
        if row is None:raise ValueError('BOOTSTRAP_FUNDING_PAUSE_REQUIRED')
        if row['status']=='RELEASED':return None
        terms=json.loads(row['terms'])
        view=snapshot(bootstrap,terms)
        if release and view['ready']:
            db.execute('UPDATE '+TABLE+' SET status=? WHERE deployment=?',('RELEASED',bootstrap.prefix))
            return None
    manifest=bootstrap.manifest()
    if not readonly:bootstrap.journal.update(bootstrap.prefix,'PAUSED_FOR_FUNDING',manifest)
    return dict(status='PAUSED_FOR_FUNDING',funding=view,manifest=manifest)
