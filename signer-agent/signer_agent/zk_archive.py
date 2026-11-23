"""Permanent content-addressed admitted-header archive, separate from money journals.

Backup metadata is not trusted: a restore is promoted only after native replay
matches the actual confirmed EVM state. No archive operation rewinds EOA budgets.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from .operations import canonical
from .bitcoin_network import header_hash


def digest(data): return hashlib.sha256(data).hexdigest()


def private_dir(path):
    path=Path(path);path.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.is_symlink() or path.stat().st_mode&0o077 or path.stat().st_uid!=os.getuid():raise ValueError('ZK_ARCHIVE_PERMISSIONS')
    return path


class Ref:
    def __init__(self,store,commitment):self.store,self.commitment=store,commitment
    def __bool__(self):return self.store.version(self.commitment)['count']>0
    def __getitem__(self,index):
        if index!=-1:raise IndexError(index)
        v=self.store.version(self.commitment)
        if not v['count']:raise IndexError(index)
        return bytes(self.store.db.execute('SELECT data FROM chunks WHERE id=?',(v['chunk'],)).fetchone()[0])[-80:].hex()
    def contains(self,display_hash):
        row=self.store.db.execute('''WITH RECURSIVE lineage(id,chunk,parent) AS (
            SELECT id,chunk,parent FROM versions WHERE id=? UNION ALL
            SELECT v.id,v.chunk,v.parent FROM versions v JOIN lineage l ON v.id=l.parent)
            SELECT 1 FROM lineage l JOIN members m ON l.chunk=m.chunk WHERE m.hash=? LIMIT 1''',
            (self.commitment,display_hash)).fetchone()
        return bool(row)


class ArchiveStore:
    def __init__(self,path,scope,max_headers=200000,max_bytes=2**30):
        self.path=private_dir(path);self.max_headers=max_headers;self.max_bytes=max_bytes
        db=self.path/'archive.sqlite'
        fd=os.open(db,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);os.fchmod(fd,0o600);os.close(fd)
        self.db=sqlite3.connect(db,isolation_level=None,timeout=30)
        self.db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,data BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS members(chunk TEXT,hash TEXT,PRIMARY KEY(chunk,hash));
            CREATE INDEX IF NOT EXISTS member_hash ON members(hash);
            CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,parent TEXT,chunk TEXT,state TEXT NOT NULL,
                count INTEGER NOT NULL,chain TEXT NOT NULL,evidence TEXT NOT NULL);
        ''')
        encoded=canonical(scope);old=self.db.execute("SELECT body FROM meta WHERE key='scope'").fetchone()
        if old and old[0]!=encoded:self.db.close();raise ValueError('ZK_ARCHIVE_SCOPE')
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('scope',?)",(encoded,))
    def close(self):self.db.close()
    def version(self,key):
        row=self.db.execute('SELECT id,parent,chunk,state,count,chain,evidence FROM versions WHERE id=?',(key,)).fetchone()
        if not row:return None
        return dict(id=row[0],parent=row[1],chunk=row[2],state=json.loads(row[3]),count=row[4],chain=row[5],evidence=json.loads(row[6]))
    def ref(self,state):
        v=self.version(state[7])
        if v is None:
            if state[11]!='1':raise ValueError('ZK_ARCHIVE_RECOVERY_REQUIRED')
            self.db.execute('INSERT INTO versions VALUES(?,NULL,NULL,?,0,?,?)',
                (state[7],canonical(state),digest(b'SATOSHIFI_ARCHIVE_V1\0'+canonical(state).encode()),canonical(dict(initial=True))))
        elif v['state']!=state:raise ValueError('ZK_ARCHIVE_STATE_MISMATCH')
        return Ref(self,state[7])
    def check_promotion(self, before, after, delta):
        """Check the exact transition that would be admitted after publication."""
        parent=self.version(before[7])
        if not parent or parent['state']!=before:raise ValueError('ZK_ARCHIVE_PARENT_REQUIRED')
        if not delta or len(delta)>64:raise ValueError('ZK_ARCHIVE_DELTA')
        headers = [bytes.fromhex(header) for header in delta]
        if any(len(header) != 80 for header in headers):
            raise ValueError('ZK_ARCHIVE_DELTA')
        count=parent['count']+len(delta)
        if count>self.max_headers or count+1!=int(after[11]):raise ValueError('ZK_ARCHIVE_LIMIT_OR_COUNT')
        raw = b''.join(headers)
        chunk=digest(raw);chain=digest(bytes.fromhex(parent['chain'])+bytes.fromhex(chunk)+canonical(after).encode())
        old=self.version(after[7])
        if old:
            if old['state']!=after or old['chain']!=chain:raise ValueError('ZK_ARCHIVE_CONFLICT')
        size=self.db.execute('SELECT COALESCE(SUM(length(data)),0) FROM chunks').fetchone()[0]
        stored = self.db.execute('SELECT data FROM chunks WHERE id=?', (chunk,)).fetchone()
        if stored is not None and bytes(stored[0]) != raw:
            raise ValueError('ZK_ARCHIVE_CHUNK_CORRUPT')
        if old and stored is None:
            raise ValueError('ZK_ARCHIVE_CHUNK_MISSING')
        if not old and size + (len(raw) if stored is None else 0) > self.max_bytes:
            raise ValueError('ZK_ARCHIVE_DISK_BUDGET')
        return headers, raw, count, chunk, chain, old is not None

    def promote(self,before,after,delta,evidence):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            headers, raw, count, chunk, chain, known = self.check_promotion(before, after, delta)
            if not known:
                self.db.execute('INSERT OR IGNORE INTO chunks VALUES(?,?)',(chunk,raw))
                for header in headers:
                    self.db.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(chunk,header_hash(header)[::-1].hex()))
                self.db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)',(after[7],before[7],chunk,canonical(after),count,chain,canonical(evidence)))
            self.db.commit()
        except BaseException:self.db.rollback();raise
        return Ref(self,after[7])
    def materialize(self,ref):
        target=self.version(ref.commitment)
        if target is None:raise ValueError('ZK_ARCHIVE_RECOVERY_REQUIRED')
        path=self.path/'headers-current.bin'
        row=self.db.execute("SELECT body FROM meta WHERE key='stream'").fetchone()
        stream=json.loads(row[0]) if row else None
        if stream and path.is_file() and not path.is_symlink() and path.stat().st_size==stream['count']*80:
            if stream['id']==target['id'] and stream['chain']==target['chain']:
                return dict(path=str(path),count=target['count'],state=target['state'])
            if stream['id']==target['parent']:
                parent=self.version(target['parent'])
                chunk=self.db.execute('SELECT data FROM chunks WHERE id=?',(target['chunk'],)).fetchone()
                if parent and stream['chain']==parent['chain'] and chunk:
                    data=bytes(chunk[0])
                    if digest(data)!=target['chunk'] or len(data)%80 or not 0<len(data)<=5120:raise ValueError('ZK_ARCHIVE_CHUNK_CORRUPT')
                    with path.open('ab') as f:f.write(data);f.flush();os.fsync(f.fileno())
                    self.db.execute("INSERT INTO meta VALUES('stream',?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",(canonical(dict(id=target['id'],chain=target['chain'],count=target['count'])),))
                    return dict(path=str(path),count=target['count'],state=target['state'])
        # A stream is a cache. Reconstruction validates segment hashes; a cold
        # native session always replays it against the accepted EVM commitment.
        chain=[];v=target;seen=set()
        while v:
            if v['id'] in seen or len(seen)>self.max_headers+1:raise ValueError('ZK_ARCHIVE_CYCLE')
            seen.add(v['id']);chain.append(v);v=self.version(v['parent']) if v['parent'] else None
        previous=None;count=0;hasher=hashlib.sha256();temporary=path.with_suffix('.tmp')
        fd=os.open(temporary,os.O_CREAT|os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW,0o600)
        try:
            with os.fdopen(fd,'wb') as f:
                for item in reversed(chain):
                    if item['parent'] is None:
                        expected=digest(b'SATOSHIFI_ARCHIVE_V1\0'+canonical(item['state']).encode())
                        if item['chunk'] is not None or item['count']!=0 or item['state'][11]!='1':raise ValueError('ZK_ARCHIVE_INITIAL')
                    else:
                        if previous is None or previous['id']!=item['parent']:raise ValueError('ZK_ARCHIVE_LINEAGE')
                        row=self.db.execute('SELECT data FROM chunks WHERE id=?',(item['chunk'],)).fetchone()
                        if not row:raise ValueError('ZK_ARCHIVE_CHUNK_MISSING')
                        raw=bytes(row[0])
                        if not raw or len(raw)%80 or len(raw)>5120 or digest(raw)!=item['chunk']:raise ValueError('ZK_ARCHIVE_CHUNK_CORRUPT')
                        count+=len(raw)//80
                        expected=digest(bytes.fromhex(previous['chain'])+bytes.fromhex(item['chunk'])+canonical(item['state']).encode())
                        f.write(raw);hasher.update(raw)
                    if item['chain']!=expected or item['count']!=count or count+1!=int(item['state'][11]):raise ValueError('ZK_ARCHIVE_CHAIN_CORRUPT')
                    previous=item
                if count>self.max_headers or count*80>self.max_bytes:raise ValueError('ZK_ARCHIVE_LIMIT')
                f.flush();os.fsync(f.fileno())
            os.replace(temporary,path)
            fd=os.open(self.path,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:
            if temporary.exists():temporary.unlink()
        self.db.execute("INSERT INTO meta VALUES('stream',?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",(canonical(dict(id=target['id'],chain=target['chain'],count=count)),))
        return dict(path=str(path),count=count,state=target['state'])
    def backup(self,destination,*,max_file_bytes=None):
        destination=Path(destination)
        if destination.exists():raise ValueError('ZK_ARCHIVE_BACKUP_EXISTS')
        if max_file_bytes is None:
            max_file_bytes = self.max_bytes
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
        if pages * page_size > max_file_bytes:
            raise ValueError('ZK_ARCHIVE_BACKUP_LIMIT')
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        try:
            out=sqlite3.connect(destination)
            try:self.db.backup(out)
            finally:out.close()
            if destination.stat().st_size > max_file_bytes:
                raise ValueError('ZK_ARCHIVE_BACKUP_LIMIT')
            with destination.open('rb') as f:
                os.fsync(f.fileno())
                checksum=hashlib.file_digest(f,'sha256').hexdigest()
            fd=os.open(destination.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return dict(status='BACKUP_SAVED',sha256=checksum)
    def import_verified(self,other,state):
        # Caller MUST native-replay other against the current confirmed state.
        ref=other.ref_existing(state);other.materialize(ref)
        lineage=[];version=other.version(state[7])
        while version:
            lineage.append(version);version=other.version(version['parent']) if version['parent'] else None
        chunks={v['chunk'] for v in lineage if v['chunk']}
        size=self.db.execute('SELECT COALESCE(SUM(length(data)),0) FROM chunks').fetchone()[0]
        incoming=sum(len(other.db.execute('SELECT data FROM chunks WHERE id=?',(k,)).fetchone()[0]) for k in chunks if not self.db.execute('SELECT 1 FROM chunks WHERE id=?',(k,)).fetchone())
        if size+incoming>self.max_bytes:raise ValueError('ZK_ARCHIVE_DISK_BUDGET')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for chunk in chunks:
                row=other.db.execute('SELECT id,data FROM chunks WHERE id=?',(chunk,)).fetchone()
                if digest(bytes(row[1]))!=row[0]:raise ValueError('ZK_ARCHIVE_CHUNK_CORRUPT')
                self.db.execute('INSERT OR IGNORE INTO chunks VALUES(?,?)',row)
                chunk,data=row
                raw=bytes(data)
                if len(raw)%80 or not 0<len(raw)<=5120:raise ValueError('ZK_ARCHIVE_CHUNK_CORRUPT')
                for start in range(0,len(raw),80):
                    self.db.execute('INSERT OR IGNORE INTO members VALUES(?,?)',(chunk,header_hash(raw[start:start+80])[::-1].hex()))
            for v in reversed(lineage):
                row=(v['id'],v['parent'],v['chunk'],canonical(v['state']),v['count'],v['chain'],canonical(v['evidence']))
                old=self.version(row[0])
                if old and (old['state']!=json.loads(row[3]) or old['chain']!=row[5]):raise ValueError('ZK_ARCHIVE_CONFLICT')
                self.db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?,?)',row)
            self.db.commit()
        except BaseException:self.db.rollback();raise
    def ref_existing(self,state):
        v=self.version(state[7])
        if not v or v['state']!=state:raise ValueError('ZK_ARCHIVE_RESTORE_TARGET')
        return Ref(self,state[7])
