"""Status observations retained in the identity transaction lane, outside inbox backup."""
import json
from pathlib import Path
from .operations import Journal,canonical


def observe(client,ctx,scope,number,*,restrictive=False,recover=False):
    from .address_status import snapshot,read
    w3=client.w3;block=w3.eth.get_block(number);current=snapshot(ctx,block.number,scope)
    key=bytes(read(ctx['registry'],block.number,'custodyScope' if scope==0 else 'derivationScope')).hex()
    lane=Journal(str(Path(client.c['identity_file']).resolve())+'.transactions.sqlite')
    point=dict(current,number=str(block.number),hash=block.hash.hex(),scope=key)
    terminal=3 if scope==0 else 2
    with lane.db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS incident_observations(scope TEXT PRIMARY KEY,body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS incident_observation_history(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
        row=db.execute('SELECT body FROM incident_observations WHERE scope=?',(key,)).fetchone()
        old=json.loads(row[0]) if row else None
        lower=bool(old and (current['generation']<old['generation'] or current['sequence']<old['sequence']))
        orphan=bool(old and w3.eth.get_block(int(old['number'])).hash.hex()!=old['hash'])
        terminal_conflict=bool(old and old['terminal'] and current['state']!=terminal)
        closed_conflict=bool(old and old['issuance_closed'] and not current['issuance_closed'])
        stale=lower or orphan or terminal_conflict or closed_conflict
        if recover:
            if lower or terminal_conflict or closed_conflict:raise ValueError('INCIDENT_STATUS_RECOVERY_REQUIRED')
        elif stale and not restrictive:
            raise ValueError('INCIDENT_STATUS_RECOVERY_REQUIRED')
        # A fresh restrictive decision can reassert a hold after reorg. It
        # cannot erase the higher/terminal observation or license a resume.
        if old is None or recover or not stale and current['generation']>old['generation']:
            point['terminal']=current['state']==terminal or bool(old and old['terminal'])
            db.execute('INSERT INTO incident_observations VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET body=excluded.body',(key,canonical(point)))
        else:point=old
        # Retain the first anchor for each distinct checkpoint/reorg witness.
        # Repeated polling of an unchanged state does not grow the history.
        basis=dict(scope=key,observed=current,retained=point,lower_generation=lower,
            orphaned_anchor=orphan,terminal_conflict=terminal_conflict,closed_conflict=closed_conflict,recovered=recover)
        from .address_watch import digest
        witness=dict(basis,number=str(block.number),hash=block.hash.hex())
        db.execute('INSERT OR IGNORE INTO incident_observation_history VALUES(?,?)',(key+':'+digest(basis),canonical(witness)))
    client.stable(block)
    return dict(retained_generation=str(point['generation']),retained_terminal=point['terminal'],
        lower_generation=lower,orphaned_anchor=orphan,terminal_conflict=terminal_conflict,closed_conflict=closed_conflict)
