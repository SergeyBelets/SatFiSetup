"""Durable address descriptors and bounded archival-Core rescan. No money journal writes."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from . import crypto
from .deposit_intent import DepositIntentV1
from .operations import Journal, canonical


MAX_SCAN_HEIGHT = 2**32 - 1
MAX_REORG = 100
WATCH_COVERAGE_VERSION = "3"
# Keep one request within a single Bitcoin Core filter-index lookup.
FILTER_PAGE_BLOCKS = 2_000
BLOCK_HASH = re.compile(r"[0-9a-f]{64}")
FILTER_SCRIPT = re.compile(r"(?!6a)(?:[0-9a-f]{2}){1,512}")


def digest(value): return hashlib.sha256(canonical(value).encode()).hexdigest()


def trusted_remote_filters(config, bitcoin):
    """Consent applies only to the endpoint in this service's private config."""
    return (config.get("trust_remote_watch_filters") is True
            and config.get("bitcoin_rpc") == getattr(bitcoin, "url", None))


def coverage_at(db, intent, best, *, tip=None):
    """Read the current coverage and its cursor/anchor in one SQLite snapshot."""
    row = db.execute("""
        SELECT i.next_height, c.body, s.hash
        FROM address_intents i
        JOIN address_meta c ON c.id='coverage:'||i.id
        JOIN address_scans s ON s.intent=i.id AND s.height=i.next_height-1
        JOIN address_meta v ON v.id='watch_coverage_version' AND v.body=?
        WHERE i.id=?
    """, (canonical(WATCH_COVERAGE_VERSION), intent)).fetchone()
    if row is None or row[2] != best:
        return None
    height = row[0] - 1
    coverage = json.loads(row[1])
    if (height >= 0 and (tip is None or height == tip)
            and coverage.get("status") == "WATCH_COVERED" and coverage.get("bestblock") == best
            and coverage.get("tip") == str(height)):
        return coverage
    return None


def filter_page(bitcoin, script, first, last, max_blocks):
    indexes = bitcoin.call("getindexinfo", "basic block filter index")
    index = indexes.get("basic block filter index") if isinstance(indexes, dict) else None
    if (not isinstance(index, dict) or index.get("synced") is not True
            or type(index.get("best_block_height")) is not int
            or index["best_block_height"] < last):
        raise ValueError("ADDRESS_FILTER_INDEX_REQUIRED")
    anchor = bitcoin.call("getblockhash", last)
    block = bitcoin.call("getblock", anchor, 1)
    if block["hash"] != anchor or block["height"] != last or not block["tx"]:
        raise ValueError("ADDRESS_CORE_CHANGED")
    txid = block["tx"][-1]
    transaction = bitcoin.call("getrawtransaction", txid, True, anchor)
    if transaction["txid"] != txid or transaction["blockhash"] != anchor:
        raise ValueError("ADDRESS_CORE_CHANGED")
    sentinel = next((output["scriptPubKey"]["hex"] for output in reversed(transaction["vout"])
                     if FILTER_SCRIPT.fullmatch(output["scriptPubKey"]["hex"])), None)
    if sentinel is None:
        # A page without a known filter element in its last block cannot be anchored.
        return None

    result = bitcoin.call("scanblocks", "start", [f"raw({script})", f"raw({sentinel})"], first, last, "basic")
    hashes = result.get("relevant_blocks") if isinstance(result, dict) else None
    # The sentinel must match on the requested branch. In particular, an empty
    # or incomplete filter-index lookup cannot move the scan cursor.
    if (not isinstance(hashes, list) or result.get("completed") is not True
            or type(result.get("from_height")) is not int or result["from_height"] != first
            or type(result.get("to_height")) is not int or result["to_height"] != last
            or len(hashes) > last - first + 1
            or any(not isinstance(value, str) or not BLOCK_HASH.fullmatch(value) for value in hashes)
            or len(set(hashes)) != len(hashes) or anchor not in hashes):
        raise ValueError("ADDRESS_FILTER_SCAN_INCOMPLETE")
    if len(hashes) > max_blocks:
        return None
    matches = {last: anchor}
    for value in hashes:
        if value == anchor:
            continue
        header = bitcoin.call("getblockheader", value, True)
        height = header["height"]
        if (header["hash"] != value or type(height) is not int or not first <= height < last
                or height in matches or bitcoin.call("getblockhash", height) != value):
            raise ValueError("ADDRESS_CORE_CHANGED")
        matches[height] = value
    return sorted(matches.items())


class Store:
    def __init__(self,path,scope,max_intents=256,max_outputs=4096,max_bytes=2**30):
        if not 1<=max_intents<=256 or not 1<=max_outputs<=4096:raise ValueError("ADDRESS_WATCH_LIMIT")
        if not 2**20<=max_bytes<=2**36:raise ValueError("ADDRESS_WATCH_LIMIT")
        self.journal=Journal(path);self.path=self.journal.path
        self.max_intents=max_intents;self.max_outputs=max_outputs;self.max_bytes=max_bytes
        with self.journal.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS address_meta(id TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS address_intents(id TEXT PRIMARY KEY,requester TEXT NOT NULL,nonce TEXT NOT NULL,
                    immutable TEXT NOT NULL,next_height INTEGER NOT NULL DEFAULT 0,UNIQUE(requester,nonce));
                CREATE TABLE IF NOT EXISTS address_scans(intent TEXT,height INTEGER,hash TEXT NOT NULL,parent TEXT NOT NULL,
                    PRIMARY KEY(intent,height));
                CREATE TABLE IF NOT EXISTS address_observations(id TEXT PRIMARY KEY,intent TEXT NOT NULL,kind TEXT NOT NULL,
                    outpoint TEXT NOT NULL,txid TEXT NOT NULL,height INTEGER NOT NULL,hash TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS address_current(intent TEXT,outpoint TEXT,body TEXT NOT NULL,
                    PRIMARY KEY(intent,outpoint));
                CREATE TABLE IF NOT EXISTS address_requests(route TEXT PRIMARY KEY,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS address_packets(id TEXT PRIMARY KEY,direction TEXT,body TEXT NOT NULL,state TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS address_observation_block ON address_observations(intent,height,hash);
                CREATE INDEX IF NOT EXISTS address_current_height ON address_current(intent,json_extract(body,'$.height'));
                CREATE INDEX IF NOT EXISTS address_unspent ON address_current(intent,outpoint)
                    WHERE json_extract(body,'$.spent_by') IS NULL;
            ''')
            bound=dict(scope=scope,max_intents=max_intents,max_outputs=max_outputs,max_bytes=max_bytes)
            db.execute("BEGIN IMMEDIATE")
            old=self._get(db,"scope")
            if old is not None and old!=bound:raise ValueError("ADDRESS_WATCH_SCOPE_CHANGED")
            if old is None and db.execute("SELECT 1 FROM address_intents LIMIT 1").fetchone():
                raise ValueError("ADDRESS_WATCH_SCOPE_CHANGED")
            self._put(db,"scope",bound)
            if old is None:
                self._put(db, "watch_coverage_version", WATCH_COVERAGE_VERSION)
    @contextmanager
    def exclusive(self):
        fd=os.open(str(self.path)+".lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError("ADDRESS_WATCH_BUSY") from None
            yield
        finally:os.close(fd)
    @staticmethod
    def _get(db,key):
        row=db.execute("SELECT body FROM address_meta WHERE id=?",(key,)).fetchone()
        return json.loads(row[0]) if row else None
    @staticmethod
    def _put(db,key,value):
        db.execute("INSERT INTO address_meta VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",(key,canonical(value)))
    def get(self,key):
        with self.journal.db() as db:return self._get(db,key)
    def coverage_at(self, intent, best, *, tip=None):
        with self.journal.db() as db:
            return coverage_at(db, intent, best, tip=tip)
    def space(self,additional=0):
        size=sum(p.stat().st_size for p in (self.path,Path(str(self.path)+"-wal")) if p.exists())
        if size+additional>self.max_bytes:raise ValueError("ADDRESS_WATCH_DISK_BUDGET")
    def put(self,key,value):
        with self.journal.db() as db:self._put(db,key,value)
    def intents(self):
        with self.journal.db() as db:return [(r[0],json.loads(r[1]),r[2]) for r in db.execute("SELECT id,immutable,next_height FROM address_intents ORDER BY rowid")]
    def accept(self,raw,base_group,network,route,evidence):
        """ACK is returned only after descriptor + genesis rescan plan commit together."""
        intent=DepositIntentV1.decode(raw);descriptor=intent.derive(base_group)
        scope=self.get("scope")["scope"]
        if (intent.bitcoin_genesis.hex()!=scope["bitcoin_genesis"] or network!=scope.get("bitcoin_network",network)
                or base_group.hex()!=scope.get("base_group",base_group.hex())):
            raise ValueError("ADDRESS_WATCH_SCOPE_CHANGED")
        address=crypto.call("bitcoin_script_address_v1",script=descriptor["script_pubkey"],network=network)["address"]
        immutable=dict(intent=raw.hex(),descriptor=descriptor,network=network,address=address,start_height="0")
        key=intent.intent_id.hex();requester=intent.requester.hex();nonce=intent.request_nonce.hex()
        accepted=dict(intent_id=key,descriptor_hash=digest(immutable),status="DURABLE",ready=False)
        self.space(len(canonical(immutable))*3+8192)
        with self.journal.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior=db.execute("SELECT immutable FROM address_intents WHERE id=?",(key,)).fetchone()
            if prior and json.loads(prior[0])!=immutable:raise ValueError("ADDRESS_DESCRIPTOR_CONFLICT")
            collision=db.execute("SELECT id FROM address_intents WHERE requester=? AND nonce=?",(requester,nonce)).fetchone()
            if collision and collision[0]!=key:raise ValueError("ADDRESS_REQUEST_NONCE_CONFLICT")
            if not prior and db.execute("SELECT COUNT(*) FROM address_intents").fetchone()[0]>=self.max_intents:
                raise ValueError("ADDRESS_WATCH_FULL")
            old=db.execute("SELECT body FROM address_requests WHERE route=?",(route,)).fetchone()
            record=dict(intent_id=key,receipt=accepted,evidence=evidence)
            if old and json.loads(old[0])!=record:raise ValueError("ADDRESS_REQUEST_CONFLICT")
            db.execute("INSERT OR IGNORE INTO address_intents(id,requester,nonce,immutable) VALUES(?,?,?,?)",(key,requester,nonce,canonical(immutable)))
            db.execute("INSERT OR IGNORE INTO address_requests VALUES(?,?)",(route,canonical(record)))
        return accepted
    def _check_immutable(self,immutable):
        intent=DepositIntentV1.decode(bytes.fromhex(immutable["intent"]))
        desc=intent.derive(bytes.fromhex(immutable["descriptor"]["base_group"]))
        address=crypto.call("bitcoin_script_address_v1",script=desc["script_pubkey"],network=immutable["network"])["address"]
        scope=self.get("scope")["scope"]
        if (immutable!=dict(intent=intent.encode().hex(),descriptor=desc,address=address,network=immutable["network"],start_height="0")
                or intent.bitcoin_genesis.hex()!=scope["bitcoin_genesis"]
                or immutable["network"]!=scope.get("bitcoin_network",immutable["network"])
                or desc["base_group"]!=scope.get("base_group",desc["base_group"])):raise ValueError("ADDRESS_DESCRIPTOR_CONFLICT")
    def _observe(self,db,intent,kind,outpoint,txid,height,block,body):
        record=dict(intent=intent,kind=kind,outpoint=outpoint,txid=txid,height=height,block=block,body=body)
        key=digest(record)
        db.execute("INSERT OR IGNORE INTO address_observations VALUES(?,?,?,?,?,?,?,?)",
            (key,intent,kind,outpoint,txid,height,block,canonical(body)))
    def _upgrade_coverage(self):
        """Rebuild legacy sparse projections whose filter source was unbound."""
        with self.journal.db() as db:
            db.execute("BEGIN IMMEDIATE")
            version = self._get(db, "watch_coverage_version")
            if version == WATCH_COVERAGE_VERSION:
                return
            if version not in (None, "2"):
                raise ValueError("ADDRESS_WATCH_COVERAGE_VERSION")
            if version is None:
                incomplete = db.execute("""
                    SELECT i.id FROM address_intents i
                    LEFT JOIN address_scans s ON s.intent=i.id
                    GROUP BY i.id
                    HAVING COUNT(s.height)!=i.next_height OR COALESCE(MIN(s.height),0)!=0
                        OR COALESCE(MAX(s.height),-1)!=i.next_height-1
                """).fetchall()
                for (intent,) in incomplete:
                    db.execute("DELETE FROM address_scans WHERE intent=?", (intent,))
                    db.execute("DELETE FROM address_current WHERE intent=?", (intent,))
                    db.execute("UPDATE address_intents SET next_height=0 WHERE id=?", (intent,))
            db.execute("DELETE FROM address_meta WHERE id LIKE 'coverage:%'")
            self._put(db, "watch_coverage_version", WATCH_COVERAGE_VERSION)

    def _trim_scans(self, intent, next_height):
        # Leave the full supported rewind window, including its oldest anchor.
        # The limit lets large existing journals release pages over several polls.
        with self.journal.db() as db:
            db.execute("""
                DELETE FROM address_scans WHERE rowid IN (
                    SELECT rowid FROM address_scans WHERE intent=? AND height<?
                    ORDER BY height LIMIT 4096
                )
            """, (intent, next_height-1-MAX_REORG))

    def _rewind(self,bitcoin,intent,next_height,max_reorg,tip,best):
        """Retain every observation; rewind only the canonical projection/cursor."""
        with self.journal.db() as db:
            db.execute("BEGIN IMMEDIATE")
            floor=max(0,next_height-1-max_reorg)
            height=min(next_height-1,tip)
            while height>=floor:
                row=db.execute("SELECT hash FROM address_scans WHERE intent=? AND height=?",(intent,height)).fetchone()
                if row and bitcoin.call("getblockhash",height)==row[0]:break
                height-=1
            if height<floor:raise ValueError("ADDRESS_RESCAN_RECOVERY_REQUIRED")
            if bitcoin.call("getbestblockhash")!=best:
                raise ValueError("ADDRESS_CORE_CHANGED")
            db.execute("DELETE FROM address_meta WHERE id=?", ("coverage:"+intent,))
            db.execute("""
                UPDATE address_current SET body=json_set(body,'$.spent_by',NULL)
                WHERE intent=? AND outpoint IN (
                    SELECT o.outpoint FROM address_scans s
                    JOIN address_observations o ON o.intent=s.intent AND o.height=s.height AND o.hash=s.hash
                    WHERE s.intent=? AND s.height>? AND o.kind='SPEND'
                )
            """, (intent,intent,height))
            db.execute("DELETE FROM address_current WHERE intent=? AND json_extract(body,'$.height')>?", (intent,height))
            db.execute("DELETE FROM address_scans WHERE intent=? AND height>?",(intent,height))
            db.execute("UPDATE address_intents SET next_height=? WHERE id=?",(height+1,intent))
            self._put(db,"reorg:"+intent,dict(last_common_height=height,previous_next=next_height))
            return height+1
    def _read_block(self, db, bitcoin, key, script, height, block):
        raw = bitcoin.call("getblock", block, 0)
        known = [r[0] for r in db.execute(
            "SELECT outpoint FROM address_current WHERE intent=? AND json_extract(body,'$.spent_by') IS NULL", (key,)
        )]
        parsed = crypto.call("bitcoin_watch_block_v1", raw=raw, hash=block, scripts=[script], outpoints=known)
        if bitcoin.call("getblockhash", height) != block:
            raise ValueError("ADDRESS_CORE_CHANGED")
        if parsed["hash"] != block:
            raise ValueError("ADDRESS_BLOCK_BINDING")
        return parsed

    def _record_block(self, db, key, height, block, parsed, *, contiguous):
        if contiguous and height:
            previous = db.execute("SELECT hash FROM address_scans WHERE intent=? AND height=?", (key, height-1)).fetchone()
            if not previous or previous[0] != parsed["parent"]:
                raise ValueError("ADDRESS_CORE_CHANGED")
        for output in parsed["outputs"]:
            body = dict(output, height=height, block=block, spent_by=None)
            self._observe(db, key, "OUTPUT", output["outpoint"], output["outpoint"].split(":")[0], height, block, output)
            db.execute("INSERT OR IGNORE INTO address_current VALUES(?,?,?)", (key, output["outpoint"], canonical(body)))
        for spend in parsed["spends"]:
            self._observe(db, key, "SPEND", spend["outpoint"], spend["txid"], height, block, spend)
            old = db.execute("SELECT body FROM address_current WHERE intent=? AND outpoint=?", (key, spend["outpoint"])).fetchone()
            if old:
                body = json.loads(old[0])
                body["spent_by"] = spend["txid"]
                db.execute("UPDATE address_current SET body=? WHERE intent=? AND outpoint=?", (canonical(body), key, spend["outpoint"]))
        if parsed["outputs"] and db.execute(
            "SELECT COUNT(*) FROM address_current WHERE json_extract(body,'$.spent_by') IS NULL"
        ).fetchone()[0] > self.max_outputs:
            raise ValueError("ADDRESS_WATCH_OUTPUT_LIMIT")
        db.execute("INSERT INTO address_scans VALUES(?,?,?,?)", (key, height, block, parsed["parent"]))

    def _scan_filtered(self, bitcoin, key, script, first, last, max_blocks):
        matches = filter_page(bitcoin, script, first, last, max_blocks)
        if matches is None:
            return None
        with self.journal.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT hash FROM address_scans WHERE intent=? AND height=?", (key, first-1)).fetchone()
            reserved_bytes = 0
            for height, block in matches:
                parsed = self._read_block(db, bitcoin, key, script, height, block)
                # Earlier writes in this transaction may still be in SQLite's
                # page cache and absent from the database/WAL sizes.
                reserved_bytes += len(canonical(parsed))*3+8192
                self.space(reserved_bytes)
                self._record_block(db, key, height, block, parsed, contiguous=False)
            if (not previous or bitcoin.call("getblockhash", first-1) != previous[0]
                    or bitcoin.call("getblockhash", last) != matches[-1][1]):
                raise ValueError("ADDRESS_CORE_CHANGED")
            db.execute("UPDATE address_intents SET next_height=? WHERE id=?", (last+1, key))
        return last+1

    def scan(self,bitcoin,*,max_blocks=16,max_reorg=MAX_REORG,start_height=None,allow_remote_filters=False):
        if not 1<=max_blocks<=256 or not 1<=max_reorg<=MAX_REORG:raise ValueError("ADDRESS_SCAN_LIMIT")
        with self.exclusive():
            configured = self.get("watch_start_height") if start_height is None else start_height
            first_full_block = 0 if configured is None else configured
            if type(first_full_block) is not int or not 0<=first_full_block<=MAX_SCAN_HEIGHT:
                raise ValueError("ADDRESS_SCAN_START_HEIGHT")
            if (first_full_block and getattr(bitcoin, "is_loopback", False) is not True
                    and not (allow_remote_filters is True and getattr(bitcoin, "is_https", False) is True)):
                raise ValueError("ADDRESS_FILTER_LOCAL_CORE_REQUIRED")
            bitcoin.check_network()
            scope=self.get("scope")["scope"]
            if bitcoin.call("getblockhash",0)!=scope["bitcoin_genesis"]:raise ValueError("BTC_ADDRESS_GENESIS_MISMATCH")
            info=bitcoin.call("getblockchaininfo")
            if info.get("initialblockdownload") or info.get("pruned"):
                raise ValueError("ADDRESS_ARCHIVAL_CORE_REQUIRED")
            tip=int(info["blocks"])
            if first_full_block>tip+1:raise ValueError("ADDRESS_SCAN_START_HEIGHT")
            best=bitcoin.call("getbestblockhash")
            if bitcoin.call("getblockhash",tip)!=best:raise ValueError("ADDRESS_CORE_CHANGED")
            if self.get("watch_coverage_version") != WATCH_COVERAGE_VERSION:
                self._upgrade_coverage()
            rows=self.intents()
            if not rows:
                if start_height is not None:
                    self.put("watch_start_height", start_height)
                return dict(status="IDLE",ready=False)
            round_robin=self.get("scan_cursor") or 0
            key,immutable,next_height=rows[round_robin%len(rows)]
            self._check_immutable(immutable)
            self.put("scan_cursor",(round_robin+1)%len(rows))
            if next_height:
                overlap = min(next_height - 1, tip)
                with self.journal.db() as db:
                    prev = db.execute("SELECT hash FROM address_scans WHERE intent=? AND height=?", (key, overlap)).fetchone()
                if tip < next_height - 1 and (prev is None or prev[0] == best):
                    if bitcoin.call("getbestblockhash") != best:
                        raise ValueError("ADDRESS_CORE_CHANGED")
                    return dict(status="WAITING_BITCOIN_RPC", intent_id=key, next_height=str(next_height),
                        tip=str(tip), bestblock=best, ready=False)
                if prev is None or bitcoin.call("getblockhash", overlap) != prev[0]:
                    next_height=self._rewind(bitcoin,key,next_height,max_reorg,tip,best)
            if next_height>MAX_REORG+1:
                self._trim_scans(key,next_height)
            if next_height<=tip:
                with self.journal.db() as db:
                    db.execute("DELETE FROM address_meta WHERE id=?", ("coverage:"+key,))
            script = immutable["descriptor"]["script_pubkey"]
            # Sparse historical pages stay outside the ordinary rewind window.
            last = min(first_full_block - 1, tip - max_reorg, next_height + FILTER_PAGE_BLOCKS - 1)
            advanced = self._scan_filtered(bitcoin, key, script, next_height, last, max_blocks) if 0 < next_height <= last else None
            if advanced is None:
                advanced = next_height
                end = 1 if next_height == 0 and last >= 1 else min(tip+1, next_height+max_blocks)
                for height in range(next_height, end):
                    if height > MAX_SCAN_HEIGHT:
                        raise ValueError("ADDRESS_SCAN_HISTORY_LIMIT")
                    block = bitcoin.call("getblockhash", height)
                    with self.journal.db() as db:
                        parsed = self._read_block(db, bitcoin, key, script, height, block)
                        self.space(len(canonical(parsed))*3+8192)
                        db.execute("BEGIN IMMEDIATE")
                        self._record_block(db, key, height, block, parsed, contiguous=True)
                        db.execute("UPDATE address_intents SET next_height=? WHERE id=?", (height+1,key))
                    advanced = height+1
            if bitcoin.call("getbestblockhash")!=best:raise ValueError("ADDRESS_CORE_CHANGED")
            result=dict(status="WATCH_COVERED" if advanced>tip else "RESCANNING",intent_id=key,
                next_height=str(advanced),tip=str(tip),bestblock=best,ready=False)
            with self.journal.db() as db:
                if result["status"]=="WATCH_COVERED":
                    anchor=db.execute("SELECT hash FROM address_scans WHERE intent=? AND height=?", (key,tip)).fetchone()
                    if advanced!=tip+1 or not anchor or anchor[0]!=best:
                        raise ValueError("ADDRESS_CORE_CHANGED")
                if start_height is not None:
                    self._put(db, "watch_start_height", start_height)
                self._put(db, "coverage:"+key, result)
            return result
    def outputs(self,intent):
        with self.journal.db() as db:return [json.loads(r[0]) for r in db.execute("SELECT body FROM address_current WHERE intent=? ORDER BY outpoint",(intent,))]
    def unspent_outputs(self, intent):
        with self.journal.db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM address_current WHERE intent=? AND json_extract(body,'$.spent_by') IS NULL ORDER BY outpoint", (intent,)
            )]
    def backup(self,destination):
        destination=Path(destination)
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        import sqlite3
        with self.exclusive(),self.journal.db() as source:
            target=sqlite3.connect(destination)
            try:source.backup(target)
            finally:target.close()
        with destination.open("rb") as f:os.fsync(f.fileno())
        with destination.open("rb") as f:checksum=hashlib.file_digest(f,"sha256").hexdigest()
        return dict(status="ADDRESS_BACKUP_SAVED",sha256=checksum)
