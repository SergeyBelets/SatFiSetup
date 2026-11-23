"""Bounded old-wallet service. Never rotates keys, authorizes spending or declares Ready."""
import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from web3 import Web3
from .address_watch import digest
from .address_transport import Client, load_config as address_config
from .bootstrap import artifact
from .onboarding import read_private
from .operations import Journal, canonical
from .spv_transport import hex32, uint


def load_config(path):
    c=json.loads(read_private(path))
    fields={'version','fleet_id','identity_file','directory','max_epochs_per_poll','max_bindings_per_poll',
        'max_archive_records','attestation_ttl_seconds','epochs'}
    if not isinstance(c,dict) or set(c)!=fields or c['version']!='1':raise ValueError('EPOCH_CONFIG')
    if hex32(c['fleet_id'])==bytes(32):raise ValueError('EPOCH_CONFIG')
    for k in ('identity_file','directory'):
        if not isinstance(c[k],str) or not Path(c[k]).is_absolute():raise ValueError('EPOCH_CONFIG')
    for k,lo,hi in [('max_epochs_per_poll',1,8),('max_bindings_per_poll',1,16),
        ('max_archive_records',1,65536),('attestation_ttl_seconds',60,3600)]:uint(c[k],lo,hi)
    if not isinstance(c['epochs'],list) or not 1<=len(c['epochs'])<=32:raise ValueError('EPOCH_CONFIG')
    ids=set();issuers=0
    for e in c['epochs']:
        if (not isinstance(e,dict) or set(e)!={'wallet_context','mode','address_config','signing_board','spv_route','minimum_evm_balance_wei'}
                or e['mode'] not in ('ISSUING','SERVICING') or not isinstance(e['address_config'],str)
                or not Path(e['address_config']).is_absolute()):raise ValueError('EPOCH_CONFIG')
        if hex32(e['wallet_context'])==bytes(32) or e['wallet_context'] in ids:raise ValueError('EPOCH_DUPLICATE')
        ids.add(e['wallet_context']);issuers+=e['mode']=='ISSUING'
        if not Web3.is_checksum_address(e['signing_board']) or int(e['signing_board'],16)==0:raise ValueError('EPOCH_CONFIG')
        uint(e['minimum_evm_balance_wei'],1,2**256-1)
        route=e['spv_route']
        if route is not None:
            if (not isinstance(route,dict) or set(route)!={'worker_config','transport_config','federation','recipient'}
                    or bool(route['worker_config'])==bool(route['transport_config'])):raise ValueError('EPOCH_CONFIG')
            for k in ('worker_config','transport_config'):
                if route[k] is not None and (not isinstance(route[k],str) or not Path(route[k]).is_absolute()):raise ValueError('EPOCH_CONFIG')
            for k in ('federation','recipient'):
                if route[k] is not None and not Web3.is_checksum_address(route[k]):raise ValueError('EPOCH_CONFIG')
    if issuers>1:raise ValueError('EPOCH_CONFIG')
    return c


class ObservationClient(Client):
    """Even revoked/broken derivation must not prevent watching existing descriptors."""
    def context(self):return super().context(verify_derivation=False)
    def make_intent(self,*args,**kw):raise ValueError('EPOCH_OBSERVATION_ONLY')
    def request(self,*args,**kw):raise ValueError('EPOCH_OBSERVATION_ONLY')
    def send(self,*args,**kw):raise ValueError('EPOCH_OBSERVATION_ONLY')


class Fleet:
    def __init__(self,c,*,client_factory=ObservationClient,configuration=None):
        self.c=c;self.client_factory=client_factory
        self.configuration=str(Path(configuration).resolve()) if configuration else None
        self.configs={};bindings={};directories=set()
        for e in c['epochs']:
            ac=address_config(e['address_config']);context=e['wallet_context'];self.configs[context]=ac
            if int(e['minimum_evm_balance_wei'])<int(ac['max_transaction_cost_wei']):raise ValueError('EPOCH_FUNDING_REQUIRED')
            directory=str(Path(ac['directory']).resolve())
            if directory in directories:raise ValueError('EPOCH_DUPLICATE')
            directories.add(directory)
            keys={}
            for path in ac['key_files']:
                k=json.loads(read_private(path))
                if not isinstance(k,dict) or set(k)!={'epoch','public','secret'}:raise ValueError('ADDRESS_KEY_SCHEMA')
                uint(k['epoch'],1);hex32(k['public']);hex32(k['secret'])
                if k['epoch'] in keys:raise ValueError('ADDRESS_KEY_SCHEMA')
                keys[k['epoch']]=k['public']
            policy={k:v for k,v in ac.items() if k not in ('evm_rpc','bitcoin_rpc','bitcoin_cookie','key_files','trust_remote_watch_filters')}
            bindings[context]=dict(entry={k:v for k,v in e.items() if k!='mode'},policy=policy,keys=keys,mode=e['mode'])
        self.path=Path(c['directory'])/'epochs.sqlite';existed=self.path.exists()
        self.lane=Journal(str(Path(c['identity_file']).resolve())+'.transactions.sqlite')
        self.journal=Journal(self.path)
        with self.exclusive(),self.lane.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS epoch_fleets(id TEXT PRIMARY KEY,token TEXT,directory TEXT)')
            row=db.execute('SELECT token,directory FROM epoch_fleets WHERE id=?',(c['fleet_id'],)).fetchone()
            if row:
                if not existed or row[1]!=str(self.path.resolve()):raise ValueError('EPOCH_JOURNAL_RECOVERY_REQUIRED')
                token=row[0]
            else:
                if existed:raise ValueError('EPOCH_JOURNAL_RECOVERY_REQUIRED')
                token=secrets.token_hex(32)
                db.execute('INSERT INTO epoch_fleets VALUES(?,?,?)',(c['fleet_id'],token,str(self.path.resolve())))
            db.execute('CREATE TABLE IF NOT EXISTS epoch_members(fleet TEXT,context TEXT,body TEXT,PRIMARY KEY(fleet,context))')
            old={r[0]:json.loads(r[1]) for r in db.execute('SELECT context,body FROM epoch_members WHERE fleet=?',(c['fleet_id'],))}
            selected={e['wallet_context']:e for e in c['epochs']}
            if old.keys()-selected.keys():raise ValueError('EPOCH_RETENTION_REQUIRED')
            for context,e in selected.items():
                # RPC locations are replaceable; identity, archive, policies and original keys are not.
                bound=bindings[context];policy=bound['policy'];keys=bound['keys']
                previous=old.get(context)
                if previous:
                    if (previous['entry']!=bound['entry'] or previous['policy']!=policy
                            or any(keys.get(k)!=v for k,v in previous['keys'].items())
                            or previous['mode']=='SERVICING' and e['mode']!='SERVICING'):
                        raise ValueError('EPOCH_BINDING_CHANGED')
            self.journal.put('epoch-fleet',dict(fleet_id=c['fleet_id'],token=token))
            for context,bound in bindings.items():
                db.execute('INSERT INTO epoch_members VALUES(?,?,?) ON CONFLICT(fleet,context) DO UPDATE SET body=excluded.body',
                    (c['fleet_id'],context,canonical(bound)))

    @contextmanager
    def exclusive(self):
        fd=os.open(str(self.path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('EPOCH_SERVICE_BUSY') from None
            yield
        finally:os.close(fd)

    def entry(self,context):
        for e in self.c['epochs']:
            if e['wallet_context']==context:return e
        raise ValueError('EPOCH_NOT_ENROLLED')

    def client(self,e):return self.client_factory(self.configs[e['wallet_context']])

    def snapshot(self,e,client):
        from .address_mint import read
        from .address_status import snapshot
        from .bitcoin_trust import checked_runtime
        from .contract_provenance import prove_creation
        from .dkg_protocol import Manifest
        b,pool,ctx,_,_=client.context();r=ctx['registry'];w3=client.w3;n=b.number
        dkg=w3.eth.contract(address=read(r,n,'dkg'),abi=artifact('DKGCoordinator')['abi'])
        checked_runtime(w3,dkg.address,'DKGCoordinator',n);prove_creation(w3,dkg.address,'DKGCoordinator',[],n)
        wallet_id=bytes(read(r,n,'walletId'));wallet=read(dkg,n,'getCompletedResult',wallet_id)
        ceremony=read(dkg,n,'getSessionInfo',wallet_id)
        participants=list(wallet[5]);transport=[bytes(read(dkg,n,'transportKeys',wallet_id,p)) for p in participants]
        m=Manifest(w3.eth.chain_id,dkg.address,wallet_id,int(read(r,n,'keyEpoch')),'BTC_SPEND',tuple(participants),tuple(transport),int(wallet[6]))
        if (m.context.hex()!=e['wallet_context'] or bytes(wallet[0])!=m.context
                or bytes(wallet[1])!=bytes(read(r,n,'walletResult')) or bytes(wallet[3])!=Web3.keccak(text='BTC_SPEND')
                or int(wallet[2])!=m.epoch or bytes(wallet[4])!=bytes(read(r,n,'baseX')) or not ceremony[9]
                or bytes(read(dkg,n,'PROTOCOL_V1'))!=m.protocol_id):raise ValueError('EPOCH_WALLET_BINDING')
        board=w3.eth.contract(address=e['signing_board'],abi=artifact('BitcoinSigningBoard')['abi'])
        checked_runtime(w3,board.address,'BitcoinSigningBoard',n)
        maximum=int(read(board,n,'maxPacketBytes'))
        prove_creation(w3,board.address,'BitcoinSigningBoard',[ctx['batch'].address,maximum],n)
        if (read(board,n,'batchRegistry')!=ctx['batch'].address or read(board,n,'dkg')!=dkg.address
                or bytes(read(board,n,'walletId'))!=wallet_id or bytes(read(board,n,'walletResult'))!=bytes(wallet[1])
                or not 512<=maximum<=4096):raise ValueError('EPOCH_BOARD_BINDING')
        latest=w3.eth.get_block('latest')
        states=[snapshot(ctx,k,scope) for k in (n,latest.number) for scope in (0,1)]
        from .incident_observations import observe
        # Restrictive observation preserves terminal/reorg witnesses while keeping discovery running.
        for k in (n,latest.number):
            for scope in (0,1):observe(client,ctx,scope,k,restrictive=True)
        client.stable(b);client.stable(latest)
        identity=dict(client.scope,wallet_context=m.context.hex(),signing_board=board.address,batches=ctx['batch'].address,
            participants=list(m.participants),threshold=str(m.threshold),graph_id=bytes(read(r,n,'graphId')).hex())
        self.journal.put('binding:'+m.context.hex(),identity)
        from .bitcoin_continuity import Catalog,sample
        Catalog(identity_file=client.c['identity_file']).observe(w3,sample(w3,ctx,n))
        with self.lane.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS epoch_key_families(family TEXT PRIMARY KEY,context TEXT)')
            family=digest([identity['bitcoin_genesis'],identity['base_group']])
            old=db.execute('SELECT context FROM epoch_key_families WHERE family=?',(family,)).fetchone()
            if old and old[0]!=m.context.hex():raise ValueError('EPOCH_KEY_FAMILY_UNSUPPORTED')
            db.execute('INSERT OR IGNORE INTO epoch_key_families VALUES(?,?)',(family,m.context.hex()))
        if self.configuration:
            enrollment=Journal(str(Path(client.c['identity_file']).resolve())+'.transactions.sqlite')
            key=digest([client.c['chain_id'],r.address,pool.address])
            enrollment.put('epoch-address:'+key,dict(configuration=self.configuration,fleet_id=self.c['fleet_id'],wallet_context=m.context.hex()))
        return dict(client=client,context=ctx,manifest=m,block=b,latest=latest,identity=identity,states=states,dkg=dkg)

    def retain_archive(self,e,store,purpose):
        """An old inbox backup cannot erase issued addresses or known reorg observations."""
        maximum=int(self.c['max_archive_records']);context=e['wallet_context']
        with store.exclusive(),store.journal.db() as source:
            descriptors=source.execute('SELECT id,immutable FROM address_intents LIMIT ?',(maximum+1,)).fetchall()
            observations=source.execute('SELECT id FROM address_observations LIMIT ?',(maximum+1,)).fetchall()
            if len(descriptors)>maximum or len(observations)>maximum:raise ValueError('EPOCH_ARCHIVE_LIMIT')
            records={'D:'+r[0]:digest(json.loads(r[1])) for r in descriptors}
            records.update({'O:'+r[0]:r[0] for r in observations})
            with self.lane.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS epoch_archive(fleet TEXT,context TEXT,purpose TEXT,id TEXT,digest TEXT,PRIMARY KEY(fleet,context,purpose,id))')
                previous={r[0]:r[1] for r in db.execute('SELECT id,digest FROM epoch_archive WHERE fleet=? AND context=? AND purpose=?',
                    (self.c['fleet_id'],context,purpose))}
                missing=previous.keys()-records.keys()
                if any(previous[k]!=v for k,v in records.items() if k in previous):raise ValueError('EPOCH_ARCHIVE_CONFLICT')
                for k,v in records.items():db.execute('INSERT OR IGNORE INTO epoch_archive VALUES(?,?,?,?,?)',(self.c['fleet_id'],context,purpose,k,v))
        return dict(status='ARCHIVE_RECOVERY_REQUIRED' if missing else 'ARCHIVE_RETAINED',missing=str(len(missing)),records=str(len(records)))

    def import_addresses(self,s):
        from .address_mint import read
        client=s['client'];r=s['context']['registry'];n=s['block'].number
        count=int(read(r,n,'intentCount'))
        if count>int(client.c['max_intents']):raise ValueError('EPOCH_INVENTORY_LIMIT')
        cursor=int(client.store.get('epoch-import-cursor') or 0);processed=0
        for offset in range(min(count,int(self.c['max_bindings_per_poll']))):
            index=(cursor+offset)%count;identifier=bytes(read(r,n,'intentIds',index))
            row=read(r,n,'getBinding',identifier)
            if row[1]==client.c['pool']:client.recover_binding(identifier.hex())
            processed+=1
            client.store.put('epoch-import-cursor',(index+1)%count)
        client.stable(s['block']);return dict(status='BINDINGS_OBSERVED',processed=str(processed),registered=str(count))

    def watch(self,e,client):
        from .bitcoin_rpc import BitcoinRPC
        from .address_watch import trusted_remote_filters
        c=client.c
        bitcoin=BitcoinRPC(c['bitcoin_rpc'],c['bitcoin_cookie'],c['bitcoin_network'],c['bitcoin_policy'])
        try:result=client.store.scan(bitcoin,max_blocks=int(c['max_blocks_per_poll']),max_reorg=int(c['max_reorg']),
            allow_remote_filters=trusted_remote_filters(c,bitcoin))
        except Exception as exc:
            from .participant_errors import participant_exception_code
            result=dict(status='OBSERVATION_UNAVAILABLE',reason=participant_exception_code(exc),ready=False)
        return bitcoin,result

    def fees(self,e,client,bitcoin):
        if client.c.get('version')!='2':raise ValueError('EPOCH_FUNDING_REQUIRED')
        from .btc_fee_reserve import Inspector,load_config
        from .withdrawal_fee_funding import Funding
        from .address_watch import trusted_remote_filters
        i=Inspector(load_config(client.c['fee_reserve_config']),client.w3,bitcoin);ctx=i.context()
        if ctx['registry'].address!=client.c['registry'] or ctx['pool'].address!=client.c['pool']:raise ValueError('EPOCH_WALLET_BINDING')
        expected=str(client.store.path.resolve())
        if expected not in [str(Path(p).resolve()) for p in i.c['watch_databases']]:raise ValueError('EPOCH_ARCHIVE_REQUIRED')
        archives=[]
        if ctx.get('withdrawal_fees'):
            if i.c.get('version')!='2':raise ValueError('WITHDRAWAL_FEE_WATCH_REQUIRED')
            f=Funding(i)
            for path in i.c['fee_watch_databases']:
                f.import_bindings(path,max_bindings=int(self.c['max_bindings_per_poll']))
                store=f.store(path)
                archive=self.retain_archive(e,store,'FEE:'+str(Path(path).resolve()))
                scan=store.scan(bitcoin,max_blocks=int(client.c['max_blocks_per_poll']),max_reorg=int(client.c['max_reorg']),
                    allow_remote_filters=trusted_remote_filters(i.c,bitcoin))
                after=self.retain_archive(e,store,'FEE:'+str(Path(path).resolve()))
                archives.append(dict(archive=after if archive['status']=='ARCHIVE_RETAINED' else archive,scan=scan))
        return dict(reserve=i.status(full=True),fee_watches=archives)

    def observe_fee_archives(self,e,client,bitcoin):
        """Existing refund/late-funding scripts remain observable when a verifier is quarantined."""
        if client.c.get('version')!='2':return dict(status='EPOCH_FUNDING_REQUIRED')
        from .btc_fee_reserve import load_config
        from .address_watch import trusted_remote_filters
        from .withdrawal_fee_watch import FeeWatch
        fc=load_config(client.c['fee_reserve_config']);results=[]
        if fc.get('version')!='2':return dict(status='NO_FEE_PURPOSE_ARCHIVE')
        for path in fc['fee_watch_databases']:
            def scan():
                p=Path(path)
                if not p.is_file() or p.is_symlink() or p.stat().st_uid!=os.getuid() or p.stat().st_mode&0o077:raise ValueError('EPOCH_ARCHIVE_REQUIRED')
                journal=Journal(p)
                with journal.db() as db:
                    row=db.execute("SELECT body FROM address_meta WHERE id='scope'").fetchone()
                if not row:raise ValueError('EPOCH_ARCHIVE_REQUIRED')
                saved=json.loads(row[0]);scope=saved['scope']
                if (scope.get('purpose')!='WITHDRAWAL_FEE' or any(scope.get(k)!=client.scope[k] for k in
                        ('chain_id','bitcoin_genesis','bitcoin_network','base_group'))):raise ValueError('WITHDRAWAL_FEE_WATCH_SCOPE')
                store=FeeWatch(p,scope,saved['max_intents'],saved['max_outputs'],saved['max_bytes'])
                before=self.retain_archive(e,store,'FEE:'+str(p.resolve()))
                observed=store.scan(bitcoin,max_blocks=int(client.c['max_blocks_per_poll']),max_reorg=int(client.c['max_reorg']),
                    allow_remote_filters=trusted_remote_filters(fc,bitcoin))
                after=self.retain_archive(e,store,'FEE:'+str(p.resolve()))
                return dict(watch=observed,archive=after if before['status']=='ARCHIVE_RETAINED' else before,authority='OBSERVATION_ONLY')
            result={};self._phase(result,'observation',scan);results.append(result)
        return dict(status='FEE_ARCHIVES_OBSERVED',archives=results,ready=False)

    def _phase(self,result,name,fn):
        try:result[name]=fn()
        except Exception as exc:
            from .participant_errors import participant_exception_code
            result[name]=dict(status='OBSERVATION_UNAVAILABLE',reason=participant_exception_code(exc))

    def run_epoch(self,e,*,schedule_spv=False):
        client=self.client(e);s=self.snapshot(e,client)
        result=dict(wallet_context=e['wallet_context'],mode=e['mode'],ready=False,production_ready=False,
            anchor=dict(number=str(s['block'].number),hash=s['block'].hash.hex()))
        self._phase(result,'archive',lambda:self.retain_archive(e,client.store,'ADDRESS'))
        self._phase(result,'bindings',lambda:self.import_addresses(s))
        self._phase(result,'transport',lambda:dict(status='RECEIVED',count=client.poll()))
        bitcoin=None
        try:bitcoin,result['watch']=self.watch(e,client)
        except Exception as exc:
            from .participant_errors import participant_exception_code
            result['watch']=dict(status='OBSERVATION_UNAVAILABLE',reason=participant_exception_code(exc))
        self._phase(result,'retained',lambda:self.retain_archive(e,client.store,'ADDRESS'))
        if bitcoin is not None:
            self._phase(result,'fee_observations',lambda:self.observe_fee_archives(e,client,bitcoin))
            self._phase(result,'funding',lambda:self.fees(e,client,bitcoin))
        if schedule_spv:
            def sync():
                if not e['spv_route']:raise ValueError('EPOCH_SPV_ROUTE_REQUIRED')
                from .address_spv_watch import sync
                route=e['spv_route']
                return sync(client,worker_config=route['worker_config'],transport_config=route['transport_config'],
                    federation=route['federation'],recipient=route['recipient'])
            self._phase(result,'spv',sync)
        with self.journal.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS epoch_runs(context TEXT PRIMARY KEY,body TEXT)')
            db.execute('INSERT INTO epoch_runs VALUES(?,?) ON CONFLICT(context) DO UPDATE SET body=excluded.body',(e['wallet_context'],canonical(result)))
        return result

    def run_once(self,*,schedule_spv=False):
        with self.exclusive():
            cursor=0
            with self.journal.db() as db:
                db.execute('CREATE TABLE IF NOT EXISTS epoch_cursor(id INTEGER PRIMARY KEY,value INTEGER)')
                row=db.execute('SELECT value FROM epoch_cursor WHERE id=1').fetchone()
                if row:cursor=row[0]
            results=[];entries=self.c['epochs']
            for offset in range(min(len(entries),int(self.c['max_epochs_per_poll']))):
                index=(cursor+offset)%len(entries);e=entries[index]
                try:result=self.run_epoch(e,schedule_spv=schedule_spv)
                except Exception as exc:
                    from .participant_errors import participant_exception_code
                    result=dict(wallet_context=e['wallet_context'],status='OBSERVATION_UNAVAILABLE',reason=participant_exception_code(exc),ready=False)
                results.append(result)
                with self.journal.db() as db:db.execute('INSERT INTO epoch_cursor VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',((index+1)%len(entries),))
            return dict(status='EPOCH_SERVICE_OBSERVED',epochs=results,ready=False,production_ready=False)

    def service_review(self,e,client=None):
        """Fresh read-only handover preflight. No cached green report grants service authority."""
        from .address_mint import read,binding
        from .incident_observations import observe
        client=client or self.client(e);s=self.snapshot(e,client);r=s['context']['registry'];n=s['block'].number
        from .bitcoin_continuity import client_guard
        client_guard(client,ctx=s['context'],number=n)
        _,_,_,transport,_=client.context()
        if not client.read(transport,s['block'],'serviceAllowed',client.account.address):raise ValueError('ADDRESS_SERVICE_ROLE')
        epoch,public=client._key(transport,s['block'],client.account.address)
        if epoch not in client.keys or client.keys[epoch]['public']!=public:raise ValueError('ADDRESS_LOCAL_KEY_REQUIRED')
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        if X25519PrivateKey.from_private_bytes(bytes.fromhex(client.keys[epoch]['secret'])).public_key().public_bytes_raw().hex()!=public:
            raise ValueError('ADDRESS_LOCAL_KEY_REQUIRED')
        for at in (n,s['latest'].number):
            for scope in (0,1):observe(client,s['context'],scope,at)
        custody,derivation,latest_custody,latest_derivation=s['states']
        if custody!=latest_custody or derivation!=latest_derivation:raise ValueError('EPOCH_GENERATION_CHANGED')
        if (custody['state'] not in (0,1) or derivation['state']!=0
                or e['mode']=='SERVICING' and (custody['state']!=1 or not custody['issuance_closed'])):
            raise ValueError('EPOCH_SERVICE_RESTRICTED')
        # Qualification remains explicit even when observation can tolerate broken verification code.
        from .address_mint import configuration
        configuration(client.w3,s['context']['pool'],n)
        verifier=client.w3.eth.contract(address=read(r,n,'verifier'),abi=artifact('BitcoinDepositDerivation')['abi'])
        if read(verifier,n,'QUALIFIED') is not True:raise ValueError('EPOCH_PROFILE_UNQUALIFIED')
        archive=self.retain_archive(e,client.store,'ADDRESS')
        if archive['status']!='ARCHIVE_RETAINED':raise ValueError('EPOCH_ARCHIVE_REQUIRED')
        from .bitcoin_rpc import BitcoinRPC
        c=client.c;core=BitcoinRPC(c['bitcoin_rpc'],c['bitcoin_cookie'],c['bitcoin_network'],c['bitcoin_policy'])
        core.check_network();info=core.call('getblockchaininfo');best=core.call('getbestblockhash')
        if info.get('pruned') or info.get('initialblockdownload'):raise ValueError('ADDRESS_ARCHIVAL_CORE_REQUIRED')
        if core.call('getblockhash',0)!=client.scope['bitcoin_genesis'] or core.call('getblockhash',int(info['blocks']))!=best:raise ValueError('ADDRESS_CORE_CHANGED')
        known={}
        for identifier,immutable,_ in client.store.intents():
            client.store._check_immutable(immutable)
            if client.store.coverage_at(identifier,best,tip=int(info['blocks'])) is None:raise ValueError('EPOCH_WATCH_INCOMPLETE')
            known[identifier]=immutable
        count=int(read(r,n,'intentCount'))
        if count>int(c['max_intents']):raise ValueError('EPOCH_INVENTORY_LIMIT')
        for index in range(count):
            identifier=bytes(read(r,n,'intentIds',index));row=read(r,n,'getBinding',identifier)
            if row[1]!=c['pool']:raise ValueError('EPOCH_POOL_COVERAGE_REQUIRED')
            checked=binding(s['context'],identifier)
            if identifier.hex() not in known or known[identifier.hex()]['intent']!=checked['intent'].encode().hex():raise ValueError('EPOCH_WATCH_INCOMPLETE')
        if len(known)!=count:raise ValueError('EPOCH_ORPHANED_DESCRIPTOR')
        if c.get('version')!='2':raise ValueError('EPOCH_FUNDING_REQUIRED')
        from .btc_fee_reserve import Inspector,load_config
        i=Inspector(load_config(c['fee_reserve_config']),client.w3,core);fc,financial=i.healthy(full=False)
        if fc['registry'].address!=r.address or fc['batch'].address!=s['context']['batch'].address:raise ValueError('EPOCH_WALLET_BINDING')
        reserve=i.status(full=False)
        if reserve['status']!='SUFFICIENT_QUALIFICATION_REQUIRED':raise ValueError('EPOCH_FUNDING_REQUIRED')
        # Inspect the exact fee archives here too; they cannot disappear behind a healthy old report.
        if fc.get('withdrawal_fees'):
            from .withdrawal_fee_funding import Funding
            f=Funding(i)
            for path in i.c.get('fee_watch_databases',[]):
                if self.retain_archive(e,f.store(path),'FEE:'+str(Path(path).resolve()))['status']!='ARCHIVE_RETAINED':raise ValueError('EPOCH_ARCHIVE_REQUIRED')
            v=fc['withdrawal_fees']['verifier']
            if read(v,n,'FEE_QUALIFIED') is not True:raise ValueError('EPOCH_PROFILE_UNQUALIFIED')
        balance=min(client.w3.eth.get_balance(client.account.address,block_identifier=at) for at in (n,'latest','pending'))
        if balance<int(e['minimum_evm_balance_wei']):raise ValueError('EPOCH_EVM_FUNDING_REQUIRED')
        # Unknown signed envelopes remain obligations even if their process crashed before broadcast.
        shared=Path(client.c['identity_file']).resolve().with_suffix('.evm-nonce.lock.sqlite')
        if shared.exists():
            from .bootstrap_recovery import protected_nonce_floor
            lane=Journal(shared)
            try:
                protected_nonce_floor(client.w3,lane,client.account,int(client.c['chain_id']))
            except Exception:raise ValueError('EPOCH_EVM_RECOVERY_REQUIRED') from None
            with lane.db() as db:
                pending=db.execute("SELECT request FROM operations WHERE status NOT IN ('CONFIRMED','RETIRED') AND json_extract(request,'$.chain_id')=? LIMIT ?",
                    (client.c['chain_id'],int(self.c['max_archive_records'])+1)).fetchall()
            if len(pending)>int(self.c['max_archive_records']):raise ValueError('EPOCH_EVM_RECOVERY_REQUIRED')
            for row in pending:
                # A lost global CONFIRMED write is recoverable from an exact final source receipt.
                try:
                    from eth_account import Account
                    saved=json.loads(row[0]);source=Path(saved['source_journal'])
                    if not source.is_file():raise ValueError('EPOCH_EVM_RECOVERY_REQUIRED')
                    record=Journal(source).get(saved['operation'])
                    if record['status']!='CONFIRMED':raise ValueError('EPOCH_EVM_RECOVERY_REQUIRED')
                    raw=bytes.fromhex(saved['raw_transaction'])
                    if (Web3.keccak(raw).hex()!=saved['tx_hash'] or Account.recover_transaction(raw)!=client.account.address):raise ValueError('EPOCH_EVM_RECOVERY_REQUIRED')
                    receipt=client.w3.eth.get_transaction_receipt(saved['tx_hash'])
                    if (receipt.status!=1 or receipt.blockNumber>n or receipt.blockHash.hex()!=record['result']['block_hash']
                            or client.w3.eth.get_block(receipt.blockNumber).hash!=receipt.blockHash):raise ValueError('EPOCH_EVM_RECOVERY_REQUIRED')
                except Exception:raise ValueError('EPOCH_EVM_RECOVERY_REQUIRED') from None
        if core.call('getbestblockhash')!=best:raise ValueError('ADDRESS_CORE_CHANGED')
        client.stable(s['block']);client.stable(s['latest'])
        return dict(snapshot=s,core_tip=best,archive=archive,reserve=reserve,financial_evidence=financial['evidence'],
            service_fingerprint=digest(s['identity']),custody=custody,derivation=derivation,balance=str(balance))


def signer_enrollment(state,context,*,binding=None):
    """An encrypted-state rollback must not erase the independently retained service guard."""
    marker=state.get('btc-epoch-service:'+context)
    if not marker and binding is None and not state.has_nonce_journal():return None
    with state._journal() as db:
        db.execute('CREATE TABLE IF NOT EXISTS epoch_service_binding(context TEXT PRIMARY KEY,body TEXT)')
        row=db.execute('SELECT body FROM epoch_service_binding WHERE context=?',(context,)).fetchone()
        retained=json.loads(row[0]) if row else None
        if marker and retained!=marker:raise ValueError('EPOCH_JOURNAL_RECOVERY_REQUIRED')
        if binding is not None:
            if (marker and marker!=binding or retained and retained!=binding):raise ValueError('EPOCH_BINDING_CHANGED')
            db.execute('INSERT OR IGNORE INTO epoch_service_binding VALUES(?,?)',(context,canonical(binding)))
            retained=binding
    if binding is not None:state.put('btc-epoch-service:'+context,binding)
    return retained


def signing_guard(manifest,registry,state,graph,plan=None):
    """An enrolled old epoch keeps the existing nonce/spent namespace and generation guards."""
    if state is None:return
    marker=signer_enrollment(state,manifest.context.hex())
    supplied=getattr(registry,'epoch_service_config',None)
    if not marker and not supplied:return
    filename=supplied or marker['configuration']
    fleet=Fleet(load_config(filename),configuration=filename);e=fleet.entry(manifest.context.hex())
    if marker and (marker['fleet_id']!=fleet.c['fleet_id'] or marker['configuration']!=str(Path(filename).resolve())):raise ValueError('EPOCH_BINDING_CHANGED')
    c=fleet.configs[e['wallet_context']]
    if (graph.get('usage')!='BTC_INPUT_KEYS_V1' or graph['contracts']['address_registry']['address']!=c['registry']
            or plan is not None and plan['signing_board']!=e['signing_board']):raise ValueError('EPOCH_WALLET_BINDING')
    review=fleet.service_review(e)
    if review['snapshot']['identity']['batches']!=graph['contracts']['batches']['address']:raise ValueError('EPOCH_WALLET_BINDING')
    if not marker:signer_enrollment(state,manifest.context.hex(),binding=dict(fleet_id=fleet.c['fleet_id'],configuration=str(Path(filename).resolve())))


def issuance_guard(client,configuration=None):
    """Once enrolled, dropping the CLI option cannot reopen a retired epoch."""
    lane=Journal(str(Path(client.c['identity_file']).resolve())+'.transactions.sqlite')
    key=digest([client.c['chain_id'],client.c['registry'],client.c['pool']])
    try:marker=lane.get('epoch-address:'+key)['request']
    except KeyError:marker=None
    if not marker and not configuration:return
    filename=configuration or marker['configuration'];fleet=Fleet(load_config(filename),configuration=filename)
    if marker and (marker['fleet_id']!=fleet.c['fleet_id'] or marker['configuration']!=str(Path(filename).resolve())):raise ValueError('EPOCH_BINDING_CHANGED')
    entries=[e for e in fleet.c['epochs'] if all(fleet.configs[e['wallet_context']][k]==client.c[k] for k in ('chain_id','registry','pool','identity_file','directory'))]
    if len(entries)!=1:raise ValueError('EPOCH_NOT_ENROLLED')
    e=entries[0];fleet.snapshot(e,client)
    if e['mode']!='ISSUING':raise ValueError('EPOCH_ISSUANCE_CLOSED')
    if any(old['mode']=='SERVICING' for old in fleet.c['epochs']):
        with fleet.lane.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS epoch_retention_current(fleet TEXT PRIMARY KEY,body TEXT)')
            row=db.execute('SELECT body FROM epoch_retention_current WHERE fleet=?',(fleet.c['fleet_id'],)).fetchone()
        if not row:raise ValueError('EPOCH_RETENTION_REQUIRED')
        saved=json.loads(row[0])
        from .epoch_retention import review
        if review(fleet,saved['request'],saved['packets'])['status']!='RETENTION_CHECKED':raise ValueError('EPOCH_RETENTION_REQUIRED')
