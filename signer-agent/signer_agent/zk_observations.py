"""Processed own-Core observations and a crash-safe detect→queue outbox (§15.11).

The hash chain proves integrity of retained local observations, not completeness
of Bitcoin history. A known deep fork is sticky even when another submitter has
already replaced the EVM anchor. No automatic reset, synthetic halt or rebasing.
"""
import hashlib
import json
import os
import fcntl
from contextlib import contextmanager
from pathlib import Path
from .operations import Journal,canonical
from .bitcoin_network import header_hash


def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()


def domain(c):return digest(['SATOHSI_PROCESSED_CORE_V1',c['chain_id'],c['zk_state'],c['bitcoin_policy']['genesis_hash']])


def require_policy(c):
    """A new directory/legacy profile cannot forget the original EOA enrollment."""
    from pathlib import Path
    path=Path(str(Path(c['identity_file']).resolve())+'.transactions.sqlite')
    if not path.exists():return
    with Journal(path).db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_observation_domains'").fetchone():return
        old=db.execute('SELECT policy FROM core_observation_domains WHERE domain=?',(domain(c),)).fetchone()
        if old and json.loads(old[0])!=c.get('observations'):raise ValueError('ZK_OBSERVATION_POLICY_CHANGED')


def validate(p):
    fields={'version','risk_policy','max_reorg_depth','max_branch_headers','max_events','max_bytes','maintenance_seconds'}
    if (not isinstance(p,dict) or set(p)!=fields or p['version']!='1' or p['max_reorg_depth']!='100'
            or p['risk_policy']!='LOCAL_FENCE_RETAIN_MAINNET_GUEST_V1'):raise ValueError('ZK_OBSERVATION_POLICY')
    for k,lo,hi in [('max_branch_headers',102,4096),('max_events',1024,1000000),('max_bytes',1048576,2**40),('maintenance_seconds',60,604800)]:
        v=p[k]
        if not isinstance(v,str) or not v.isascii() or not v.isdecimal() or str(int(v))!=v or not lo<=int(v)<=hi:raise ValueError('ZK_OBSERVATION_POLICY')
    return p


class Log:
    def __init__(self,box):
        self.box=box;self.db=box.db;self.p=validate(box.config['observations'])
        # Economic policy/queue grouping never changes the observation domain.
        self.domain=domain(box.config)
        self.lane=Journal(str(Path(box.config['identity_file']).resolve())+'.transactions.sqlite')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS core_observations(seq INTEGER PRIMARY KEY,hash TEXT UNIQUE,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS core_observation_outbox(id TEXT PRIMARY KEY,event INTEGER,body TEXT NOT NULL,status TEXT NOT NULL);''')
        with self.lane.db() as d:
            d.executescript('''CREATE TABLE IF NOT EXISTS core_observation_domains(domain TEXT PRIMARY KEY,policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS core_observation_backup(domain TEXT,seq INTEGER,hash TEXT,body TEXT NOT NULL,PRIMARY KEY(domain,seq));''')
            old=d.execute('SELECT policy FROM core_observation_domains WHERE domain=?',(self.domain,)).fetchone()
            if old and json.loads(old[0])!=self.p:raise ValueError('ZK_OBSERVATION_POLICY_CHANGED')
            d.execute('INSERT OR IGNORE INTO core_observation_domains VALUES(?,?)',(self.domain,canonical(self.p)))
        self.reconcile()

    @contextmanager
    def locked(self):
        if getattr(self,'_locked',False):yield;return
        fd=os.open(self.box.path/'observations.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('ZK_OBSERVATION_BUSY') from None
            self._locked=True
            local=self.db.execute('SELECT seq,hash FROM core_observations ORDER BY seq DESC LIMIT 1').fetchone()
            with self.lane.db() as db:
                backup=db.execute('SELECT seq,hash FROM core_observation_backup WHERE domain=? ORDER BY seq DESC LIMIT 1',(self.domain,)).fetchone()
            current=self.rows[-1][:2] if self.rows else None
            if local!=current or backup!=current:self.reconcile()
            yield
        finally:self._locked=False;os.close(fd)

    def _checked(self,rows):
        previous='0'*64;checked=[];size=0
        for seq,h,body in rows:
            value=json.loads(body);size+=len(body.encode())
            if seq!=len(checked)+1 or value.get('seq')!=seq or value.get('previous')!=previous or value.get('domain')!=self.domain or digest(value)!=h:
                raise ValueError('ZK_OBSERVATION_INTEGRITY')
            checked.append((seq,h,body));previous=h
        if len(checked)>int(self.p['max_events']) or size>int(self.p['max_bytes']):raise ValueError('ZK_OBSERVATION_LIMIT')
        return checked

    def reconcile(self):
        local=self._checked(list(self.db.execute('SELECT seq,hash,body FROM core_observations ORDER BY seq')))
        with self.lane.db() as d:
            backup=self._checked(list(d.execute('SELECT seq,hash,body FROM core_observation_backup WHERE domain=? ORDER BY seq',(self.domain,))))
            if local[:min(len(local),len(backup))]!=backup[:min(len(local),len(backup))]:raise ValueError('ZK_OBSERVATION_BACKUP_CONFLICT')
            rows=local if len(local)>=len(backup) else backup
            d.executemany('INSERT OR IGNORE INTO core_observation_backup VALUES(?,?,?,?)',[(self.domain,*r) for r in rows[len(backup):]])
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.executemany('INSERT OR IGNORE INTO core_observations VALUES(?,?,?)',rows[len(local):])
            for seq,h,raw in rows:
                e=json.loads(raw)
                if e['kind']=='NEED_DETECTED':
                    p=e['payload'];self.db.execute('INSERT OR IGNORE INTO core_observation_outbox VALUES(?,?,?,?)',(p['id'],seq,canonical(p),'PENDING'))
            self.db.commit()
        except BaseException:self.db.rollback();raise
        self.rows=rows;self.size=sum(len(r[2].encode()) for r in rows)

    def events(self,kind=None):
        return [(h,json.loads(body)) for _,h,body in self.rows if kind is None or json.loads(body)['kind']==kind]

    def append(self,kind,payload):
        seq=len(self.rows)+1;previous=self.rows[-1][1] if self.rows else '0'*64
        e=dict(seq=seq,previous=previous,domain=self.domain,kind=kind,payload=payload);h=digest(e);body=canonical(e)
        if seq>int(self.p['max_events']) or self.size+len(body.encode())>int(self.p['max_bytes']):raise ValueError('ZK_OBSERVATION_LIMIT')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            # Detect concurrent writers before inserting or changing any queue.
            last=self.db.execute('SELECT seq,hash FROM core_observations ORDER BY seq DESC LIMIT 1').fetchone()
            if last!=((seq-1,previous) if seq>1 else None):raise ValueError('ZK_OBSERVATION_CONCURRENT')
            self.db.execute('INSERT INTO core_observations VALUES(?,?,?)',(seq,h,body))
            if kind=='NEED_DETECTED':self.db.execute('INSERT OR IGNORE INTO core_observation_outbox VALUES(?,?,?,?)',(payload['id'],seq,canonical(payload),'PENDING'))
            self.db.commit()
        except BaseException:self.db.rollback();raise
        # Commit to the independent original EOA journal before authorizing work.
        # A failure here leaves a durable event that reconcile copies on retry.
        with self.lane.db() as d:
            d.execute('INSERT INTO core_observation_backup VALUES(?,?,?,?)',(self.domain,seq,h,body))
        self.rows.append((seq,h,body));self.size+=len(body.encode());return h

    def detect_need(self,request,status,created_at,deadline,local):
        with self.locked():return self._detect_need(request,status,created_at,deadline,local)

    def _detect_need(self,request,status,created_at,deadline,local):
        key=digest(['satoshifi.local-spv-need.v1',self.box.get('scope'),request])
        old=self.db.execute('SELECT body FROM core_observation_outbox WHERE id=?',(key,)).fetchone()
        if old:
            p=json.loads(old[0])
            if p['request']!=request:raise ValueError('ZK_OBSERVATION_OUTBOX_CONFLICT')
            # Retries do not move the original deadline or renew a payment.
            return key
        if self.db.execute("SELECT COUNT(*) FROM core_observation_outbox WHERE status='PENDING'").fetchone()[0]>=int(self.box.config['max_pending']):
            raise ValueError('ZK_WORKER_INBOX_FULL')
        # Private request floods cannot consume the security history reserve.
        if self.size>=3*int(self.p['max_bytes'])//4 or len(self.rows)>=3*int(self.p['max_events'])//4:raise ValueError('ZK_WORKER_INBOX_FULL')
        self.append('NEED_DETECTED',dict(id=key,request=request,status=status,created_at=created_at,deadline=deadline,local=local,queue_scope=self.box.get('scope')))
        return key

    def drain(self):
        with self.locked():return self._drain()

    def _drain(self):
        for key,raw in list(self.db.execute("SELECT o.id,o.body FROM core_observation_outbox o WHERE o.status='PENDING' OR NOT EXISTS(SELECT 1 FROM needs n WHERE n.id=o.id) ORDER BY o.event")):
            p=json.loads(raw);self.db.execute('BEGIN IMMEDIATE')
            try:
                if p['queue_scope']!=self.box.get('scope'):raise ValueError('ZK_OBSERVATION_OUTBOX_SCOPE')
                row=self.db.execute('SELECT body FROM needs WHERE id=?',(key,)).fetchone()
                if row and json.loads(row[0])!=p['request']:raise ValueError('ZK_OBSERVATION_OUTBOX_CONFLICT')
                if not row:
                    if self.db.execute('SELECT COUNT(*) FROM needs').fetchone()[0]>=int(self.box.config['max_pending']):raise ValueError('ZK_WORKER_INBOX_FULL')
                    self.db.execute('INSERT INTO needs VALUES(?,?,?)',(key,canonical(p['request']),p['status']))
                if p['local']:
                    k='obligation:'+key;v=self.box.get(k) or dict(created_at=p['created_at'],deadline=p['deadline'],origins={})
                    old=v['origins'].get('local')
                    if old is not None and old!=p['deadline']:raise ValueError('ZK_ORIGINAL_DEADLINE_CHANGED')
                    v['origins']['local']=p['deadline'];v['deadline']=min(v['deadline'],p['deadline']);self.box.put(k,v)
                self.db.execute("UPDATE core_observation_outbox SET status='QUEUED' WHERE id=?",(key,));self.db.commit()
            except BaseException:self.db.rollback();raise

    def fence(self):
        incidents=self.events('INCIDENT')
        if incidents:raise ValueError(incidents[0][1]['payload']['code'])
        if any(e['payload']['reorg_depth']>100 for _,e in self.events('CORE_PROCESSED')):raise ValueError('ZK_OBSERVATION_KNOWN_DEEP_REORG')
        done={e['payload']['detected'] for _,e in self.events('CORE_PROCESSED')}
        if any(h not in done for h,_ in self.events('CORE_DETECTED')):raise ValueError('ZK_OBSERVATION_WITNESS_REQUIRED')

    def process(self,bitcoin,detected):
        key,e=detected;p=e['payload'];height=p['height'];tip=p['tip']
        processed=self.events('CORE_PROCESSED');old=processed[-1][1]['payload'] if processed else None
        prior=old['headers'][:102] if old else [];known={x['hash']:x['height'] for x in prior}
        target=max(0,min(height-101,(old['height']-101) if old else height-101))
        chunks=[(h,e['payload']) for h,e in self.events('CORE_WITNESS_CHUNK') if e['payload']['detected']==key]
        tail=[];cursor=tip;at=height;common=None;previous=None
        for ordinal,(h,chunk) in enumerate(chunks):
            if chunk['index']!=ordinal or chunk['previous_chunk']!=previous:raise ValueError('ZK_OBSERVATION_INTEGRITY')
            for item in chunk['headers']:
                raw=bytes.fromhex(item['raw'])
                if (item['height']!=at or item['hash']!=cursor or len(raw)!=80 or header_hash(raw)[::-1].hex()!=cursor):raise ValueError('ZK_OBSERVATION_HEADER')
                if len(tail)<102:tail.append(item)
                if cursor in known:
                    if known[cursor]!=at:raise ValueError('ZK_OBSERVATION_HEADER')
                    common=at
                cursor=raw[4:36][::-1].hex();at-=1
            previous=h
        headers=[]
        while at>=target:
            if common is not None or len(headers)>=int(self.p['max_branch_headers']):break
            raw=bytes.fromhex(bitcoin.call('getblockheader',cursor,False))
            if len(raw)!=80 or header_hash(raw)[::-1].hex()!=cursor:raise ValueError('ZK_OBSERVATION_HEADER')
            item=dict(height=at,hash=cursor,raw=raw.hex());headers.append(item)
            if len(tail)<102:tail.append(item)
            if cursor in known:
                if known[cursor]!=at:raise ValueError('ZK_OBSERVATION_HEADER')
                common=at
            cursor=raw[4:36][::-1].hex();at-=1
        if headers:
            previous=self.append('CORE_WITNESS_CHUNK',dict(detected=key,index=len(chunks),previous_chunk=previous,headers=headers))
            chunks.append((previous,dict(headers=headers)))
        if common is None and at>=target:raise ValueError('ZK_OBSERVATION_WITNESS_REQUIRED')
        depth=old['height']-common if common is not None and old else 0 if not old else old['height']-min(x['height'] for x in prior)+1
        # An exhausted bounded walk is unknown, never proof of a deep fork.
        lower_bound=common is None and old is not None
        if lower_bound and at+1>min(x['height'] for x in prior):raise ValueError('ZK_OBSERVATION_WITNESS_REQUIRED')
        if common is not None:tail.extend(x for x in prior if x['height']<common)
        result=dict(detected=key,tip=tip,height=height,headers=tail[:102],witness_chunks=[h for h,_ in chunks],previous=old['detected'] if old else None,
            common_height=common,reorg_depth=depth,depth_is_lower_bound=lower_bound,evm=p['evm'])
        h=self.append('CORE_PROCESSED',result)
        if depth>100:self.append('INCIDENT',dict(code='ZK_OBSERVATION_KNOWN_DEEP_REORG',event=h,detected=key,depth=depth,lower_bound=lower_bound))
        return h

    def observe(self,bitcoin,core,evm):
        with self.locked():return self._observe(bitcoin,core,evm)

    def _observe(self,bitcoin,core,evm):
        # First retain detection, before any branch RPC or queue mutation.
        detected=self.events('CORE_DETECTED')
        if not detected or detected[-1][1]['payload']['tip']!=core['bestblockhash']:
            self.append('CORE_DETECTED',dict(tip=core['bestblockhash'],height=int(core['blocks']),chainwork=core['chainwork'],evm=evm))
        anchors=self.events('EVM_OBSERVED')
        if not anchors or anchors[-1][1]['payload']!=evm:self.append('EVM_OBSERVED',evm)
        done={e['payload']['detected'] for _,e in self.events('CORE_PROCESSED')}
        for item in self.events('CORE_DETECTED'):
            if item[0] not in done:self.process(bitcoin,item)
        if bitcoin.call('getbestblockhash')!=core['bestblockhash']:raise ValueError('MAINNET_CORE_REORG_DURING_CHECK')
        try:self.drain()
        except ValueError as exc:
            if str(exc)!='ZK_WORKER_INBOX_FULL':raise
            self.box.put('observation_outbox_status',dict(status='QUEUE_FULL',original_deadlines_retained=True))
        return self.rows[-1][1]

    def maintenance(self,block,current,aligned):
        self.fence()
        processed=self.events('CORE_PROCESSED')
        if not processed:return None
        h,e=processed[-1];p=e['payload']
        if aligned and int(current[3])>=p['height']:return None
        created=int(p['evm']['timestamp'])
        return dict(service_job_id='maintenance:observation:'+h,started_at=created,deadline=created+int(self.p['maintenance_seconds']),
            broadcast_not_before=0,due=created,observation=h)

    def backup(self,path):
        from pathlib import Path
        path=Path(path)
        if not path.is_absolute():raise ValueError('ZK_OBSERVATION_BACKUP_PATH')
        with self.locked():
            body=canonical(dict(version='1',domain=self.domain,policy=self.p,events=self.rows)).encode()
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as f:f.write(body);f.flush();os.fsync(f.fileno())
            fd=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            return dict(status='OBSERVATION_BACKUP_SAVED',sha256=hashlib.sha256(body).hexdigest(),events=len(self.rows),head=self.rows[-1][1] if self.rows else None)

    def restore(self,path,expected_sha256):
        from pathlib import Path
        import stat
        limit=2*int(self.p['max_bytes'])+1048576
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():raise ValueError('PRIVATE_FILE_PERMISSIONS')
            if info.st_size>limit:raise ValueError('ZK_OBSERVATION_LIMIT')
            body=source.read(limit+1)
        if len(body)>limit:raise ValueError('ZK_OBSERVATION_LIMIT')
        if hashlib.sha256(body).hexdigest()!=expected_sha256:raise ValueError('ZK_OBSERVATION_BACKUP_CONFLICT')
        value=json.loads(body)
        if (set(value)!={'version','domain','policy','events'} or value['version']!='1' or value['domain']!=self.domain or value['policy']!=self.p):
            raise ValueError('ZK_OBSERVATION_BACKUP_CONFLICT')
        rows=self._checked([tuple(row) for row in value['events']])
        with self.locked():
            if rows[:min(len(rows),len(self.rows))]!=self.rows[:min(len(rows),len(self.rows))]:raise ValueError('ZK_OBSERVATION_BACKUP_CONFLICT')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.executemany('INSERT INTO core_observations VALUES(?,?,?)',rows[len(self.rows):]);self.db.commit()
            except BaseException:self.db.rollback();raise
            self.reconcile();self.drain()
        return dict(status='OBSERVATION_HISTORY_RESTORED',events=len(self.rows),head=self.rows[-1][1] if self.rows else None,
            completeness_proven=False,ready=False)


def observer(worker):
    if not worker.c.get('observations'):return None
    if not hasattr(worker,'_observations'):worker._observations=Log(worker.inbox)
    return worker._observations


def observe(worker,block,state,core):
    log=observer(worker)
    if log is None:return None
    if core is None:return None  # Halt/receipt inspection remains possible.
    from .zk_worker import state_wire
    evm=dict(number=block.number,hash=block.hash.hex(),timestamp=block.timestamp,state=state_wire(state.functions.getState().call(block_identifier=block.number)))
    checkpoint=log.observe(worker.bitcoin,core,evm);worker.stable(block);return checkpoint
