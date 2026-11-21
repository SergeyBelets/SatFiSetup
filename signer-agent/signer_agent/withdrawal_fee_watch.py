"""Fee-purpose watch uses the existing native block codec/reorg journal, not a mint path."""
from .address_watch import Store,coverage_at,digest
from .operations import canonical
from .withdrawal_fee_intent import WithdrawalFeeIntentV1
from . import crypto
import json

class FeeWatch(Store):
    def accept(self,raw,base_group,network,route,evidence):
        intent=WithdrawalFeeIntentV1.decode(raw);desc=intent.derive(base_group);scope=self.get('scope')['scope']
        if (scope.get('purpose')!='WITHDRAWAL_FEE' or intent.parent.bitcoin_genesis.hex()!=scope['bitcoin_genesis']
                or intent.credits_scope.hex()!=scope['ledger_scope'] or network!=scope['bitcoin_network']
                or base_group.hex()!=scope['base_group']):raise ValueError('WITHDRAWAL_FEE_WATCH_SCOPE')
        address=crypto.call('bitcoin_script_address_v1',script=desc['script_pubkey'],network=network)['address']
        immutable=dict(intent=raw.hex(),descriptor=desc,network=network,address=address,start_height='0')
        key=intent.intent_id.hex();receipt=dict(intent_id=key,descriptor_hash=digest(immutable),status='DURABLE',ready=False)
        self.space(len(canonical(immutable))*3+8192)
        with self.journal.db() as db:
            db.execute('BEGIN IMMEDIATE');prior=db.execute('SELECT immutable FROM address_intents WHERE id=?',(key,)).fetchone()
            if prior and json.loads(prior[0])!=immutable:raise ValueError('WITHDRAWAL_FEE_WATCH_CONFLICT')
            if not prior and db.execute('SELECT COUNT(*) FROM address_intents').fetchone()[0]>=self.max_intents:raise ValueError('ADDRESS_WATCH_FULL')
            db.execute('INSERT OR IGNORE INTO address_intents(id,requester,nonce,immutable) VALUES(?,?,?,?)',
                (key,intent.parent.requester.hex(),intent.parent.request_nonce.hex(),canonical(immutable)))
            record=dict(intent_id=key,receipt=receipt,evidence=evidence)
            old=self._get(db,'fee-route:'+route)
            if old and old!=record:raise ValueError('WITHDRAWAL_FEE_WATCH_CONFLICT')
            self._put(db,'fee-route:'+route,record)
        return receipt
    def _check_immutable(self,immutable):
        intent=WithdrawalFeeIntentV1.decode(bytes.fromhex(immutable['intent']));scope=self.get('scope')['scope']
        if (scope.get('purpose')!='WITHDRAWAL_FEE' or intent.parent.bitcoin_genesis.hex()!=scope['bitcoin_genesis']
                or intent.credits_scope.hex()!=scope['ledger_scope']):raise ValueError('WITHDRAWAL_FEE_WATCH_CONFLICT')
        d=intent.derive(bytes.fromhex(scope['base_group']))
        address=crypto.call('bitcoin_script_address_v1',script=d['script_pubkey'],network=scope['bitcoin_network'])['address']
        if immutable!=dict(intent=intent.encode().hex(),descriptor=d,network=scope['bitcoin_network'],address=address,start_height='0'):
            raise ValueError('WITHDRAWAL_FEE_WATCH_CONFLICT')


def scope(inspector,context):
    fee=context['withdrawal_fees'];r=context['registry'];n=context['block'].number
    from .btc_fee_reserve import read
    return dict(purpose='WITHDRAWAL_FEE',chain_id=inspector.c['chain_id'],registry=fee['funding'].address,ledger=fee['credits'].address,
        ledger_scope=fee['scope'],bitcoin_genesis=bytes(read(r,n,'bitcoinGenesis')).hex(),bitcoin_network=inspector.c['bitcoin_network'],
        base_group='02'+bytes(read(r,n,'baseX')).hex())


def scripts(inspector,context,best):
    import json,os,sqlite3
    from pathlib import Path
    from urllib.parse import quote
    from .btc_fee_reserve import read
    c=inspector.c;fee=context.get('withdrawal_fees')
    if fee is None:return [],[]
    if c.get('version')!='2':raise ValueError('WITHDRAWAL_FEE_WATCH_REQUIRED')
    expected=scope(inspector,context);entries={};all_scripts=set();ids=set()
    for path in c['fee_watch_databases']:
        p=Path(path)
        if not p.is_file() or p.is_symlink() or p.stat().st_mode&0o077 or p.stat().st_uid!=os.getuid():raise ValueError('WITHDRAWAL_FEE_WATCH_REQUIRED')
        db=sqlite3.connect('file:'+quote(str(p))+'?mode=ro',uri=True)
        try:
            db.execute("BEGIN")
            bound=db.execute("SELECT body FROM address_meta WHERE id='scope'").fetchone()
            if not bound or json.loads(bound[0])['scope']!=expected:raise ValueError('WITHDRAWAL_FEE_WATCH_SCOPE')
            for identifier,encoded,next_height in db.execute('SELECT id,immutable,next_height FROM address_intents ORDER BY id'):
                value=json.loads(encoded);intent=WithdrawalFeeIntentV1.decode(bytes.fromhex(value['intent']))
                desc=intent.derive(bytes.fromhex(expected['base_group']))
                if intent.intent_id.hex()!=identifier or intent.credits_scope.hex()!=fee['scope'] or desc!=value['descriptor']:
                    raise ValueError('WITHDRAWAL_FEE_WATCH_CONFLICT')
                if coverage_at(db,identifier,best,tip=next_height-1) is None:
                    raise ValueError('WITHDRAWAL_FEE_WATCH_INCOMPLETE')
                entry=['WITHDRAWAL_FEE',identifier,digest(value),next_height,best]
                if identifier in entries and entries[identifier]!=entry:raise ValueError('WITHDRAWAL_FEE_WATCH_CONFLICT')
                entries[identifier]=entry;ids.add(identifier);all_scripts.add(desc['script_pubkey'])
        finally:db.close()
    count=int(read(fee['funding'],context['block'].number,'bindingCount'));inspector._bounded(count)
    for index in range(count):
        identifier=bytes(read(fee['funding'],context['block'].number,'bindingIds',index)).hex()
        if identifier not in ids:raise ValueError('WITHDRAWAL_FEE_WATCH_INCOMPLETE')
    with inspector.lane.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS fee_purpose_watch(scope TEXT,id TEXT,PRIMARY KEY(scope,id))')
        saved={x[0] for x in db.execute('SELECT id FROM fee_purpose_watch WHERE scope=?',(fee['scope'],))}
        if saved-ids:raise ValueError('WITHDRAWAL_FEE_WATCH_RECOVERY_REQUIRED')
        db.executemany('INSERT OR IGNORE INTO fee_purpose_watch VALUES(?,?)',[(fee['scope'],key) for key in ids])
    return sorted(all_scripts),[entries[key] for key in sorted(entries)]
