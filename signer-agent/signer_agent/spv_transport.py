"""SPV-only HPKE on-chain transport. Private plaintext never enters calldata.

Canonical contract state authenticates tx sender and role; HPKE Base provides
confidentiality using the existing Rust IPC, not a new cryptographic primitive.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from eth_abi import encode
from eth_account import Account
from web3 import Web3
from .evm_rpc import evm_web3
from . import crypto
from .bootstrap import Bootstrap, artifact
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .evm_network import read_block, verify_anchor
from .onboarding import read_private
from .operations import canonical
from .zk_service_binding import inspect_service_view
from .zk_spv import validate_confederation, checkpoint_descriptor
from .zk_profiles import profile as zk_profile
from .zk_worker_prover import write
from .spv_history import history_limit, require_history_space, sqlite_bytes

KINDS = {"REQUEST": 0, "ACK": 1, "STATUS": 2}
MAX_PLAIN = 12000


def hex32(v):
    if not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v): raise ValueError("SPV_BYTES32")
    return bytes.fromhex(v)


def uint(v, low=0, high=2**64-1):
    if not isinstance(v, str) or not re.fullmatch(r"0|[1-9][0-9]*", v) or not low <= int(v) <= high:
        raise ValueError("SPV_INTEGER")
    return int(v)


def route_key(request_id, requester, service):
    return Web3.keccak(encode(["bytes32", "address", "address"], [hex32(request_id), requester, service])).hex()


def message_key(route, kind, sequence):
    return Web3.keccak(encode(["bytes32", "uint8", "uint32"], [hex32(route), KINDS[kind], uint(sequence, 0, 64)])).hex()


def hpke_context(domain, header):
    raw = canonical(["SATOSHIFI_SPV_HPKE_V1", domain, header]).encode()
    return hashlib.sha256(b"SPV-INFO\0"+raw).hexdigest(), raw.hex()


def seal(domain, header, body, public):
    plain = canonical(body).encode()
    if len(plain) > MAX_PLAIN: raise ValueError("SPV_MESSAGE_TOO_LARGE")
    info, aad = hpke_context(domain, header)
    result = crypto.call("seal", public=public, info=info, aad=aad, plaintext=plain.hex())
    return result["enc"] + result["ciphertext"]


def open_packet(domain, header, ciphertext, secret):
    if not 48 < len(bytes.fromhex(ciphertext)) <= 16384: raise ValueError("SPV_MESSAGE_SIZE")
    info, aad = hpke_context(domain, header)
    result = crypto.call("open", secret=secret, info=info, aad=aad, enc=ciphertext[:64], ciphertext=ciphertext[64:])
    plain = bytes.fromhex(result["plaintext"])
    if len(plain) > MAX_PLAIN: raise ValueError("SPV_MESSAGE_TOO_LARGE")
    body = json.loads(plain)
    if not isinstance(body, dict) or canonical(body).encode() != plain: raise ValueError("SPV_BODY_ENCODING")
    return body


def load_config(path):
    c = json.loads(read_private(Path(path)))
    fields = {"version", "chain_id", "evm_confirmations", "evm_rpc", "bitcoin_policy", "zk_state", "factory",
        "confederation_id", "board", "identity_file", "key_files", "directory", "max_messages_per_poll",
        "max_pending_messages", "max_publish_attempts", "retry_backoff_seconds", "max_tx_gas",
        "max_transaction_cost_wei", "max_total_cost_wei", "request_ttl_seconds", "reply_ttl_seconds", "service_sla_seconds"}
    if c.get('version') in ('2','3','4'):fields.add('payment')
    if c.get('version')in ('3','4'):fields.add('address_deposits')
    if c.get("version")=="4":fields.add("withdrawal_fees")
    if not fields <= set(c) <= fields | {"history_max_bytes"} or c["version"] not in ("1","2","3","4") or c["chain_id"] not in ("1", "11155111", "31337"):
        raise ValueError("SPV_TRANSPORT_CONFIG")
    history_limit(c)
    from .bootstrap_policy import validate_confirmations
    validate_confirmations(c, "SPV_")
    from .bitcoin_network import validate_policy
    validate_policy(c["bitcoin_policy"]); validate_confederation(c["bitcoin_policy"], c["confederation_id"])
    for field in ("zk_state", "factory", "board"):
        if not Web3.is_checksum_address(c[field]) or int(c[field],16) == 0: raise ValueError("SPV_TRANSPORT_ADDRESS")
    for field, low, high in (("max_messages_per_poll",1,64),("max_pending_messages",1,4096),
        ("max_publish_attempts",1,32),("retry_backoff_seconds",1,3600),("max_tx_gas",21000,16777216),
        ("max_transaction_cost_wei",1,2**256-1),("max_total_cost_wei",1,2**256-1),
        ("request_ttl_seconds",60,86400),("reply_ttl_seconds",60,86400),("service_sla_seconds",60,604800)):
        uint(c[field],low,high)
    if not isinstance(c["key_files"],list) or not 1 <= len(c["key_files"]) <= 32: raise ValueError("SPV_KEY_FILES")
    for path in [c["directory"], c["identity_file"], *c["key_files"]]:
        if not Path(path).is_absolute(): raise ValueError("SPV_ABSOLUTE_PATH")
    if int(c["request_ttl_seconds"])>int(c["service_sla_seconds"]):raise ValueError("SPV_REQUEST_SLA")
    if c['version'] in ('2','3','4'):
        from .spv_automation import validate_payment
        validate_payment(c,c['payment'])
    if c['version']in ('3','4'):
        from .address_spv import validate_profile
        # A transport may use voluntary confederation funding, with payment disabled.
        validate_profile(c['address_deposits'],c['payment'].get('allowed_consumers'))
    if c["version"]=="4":
        from .withdrawal_fee_spv import validate_profile
        validate_profile(c["withdrawal_fees"],c["payment"].get("allowed_consumers"))
    return c


class Store:
    def __init__(self, c, address):
        self.history_limit = history_limit(c)
        self.path = Path(c["directory"]); self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077 or self.path.stat().st_uid != os.getuid():
            raise ValueError("SPV_PRIVATE_DIRECTORY")
        database = self.path / "transport.sqlite"
        fd = os.open(database, os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600); os.fchmod(fd,0o600); os.close(fd)
        self.db = sqlite3.connect(database, isolation_level=None, timeout=30)
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outgoing(id TEXT PRIMARY KEY, logical TEXT UNIQUE, body TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incoming(id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS costs(id TEXT PRIMARY KEY, cost TEXT NOT NULL, attempts INTEGER NOT NULL);
        ''')
        self.c = c
        scope = {k:c[k] for k in ("chain_id","evm_confirmations","bitcoin_policy","zk_state","factory","confederation_id","board")}
        scope["identity"] = address
        if c.get('version') in ('2','3','4'):scope['payment']=c['payment']
        old = self.get("scope")
        if old is not None and old != scope: raise ValueError("SPV_TRANSPORT_SCOPE_CHANGED")
        self.put("scope",scope)
        if self.get("packet-history-enabled") and self.history_limit is None:
            self.db.close()
            raise ValueError("SPV_HISTORY_POLICY_REQUIRED")
        if self.history_limit is not None:
            self.db.executescript("""
                CREATE INDEX IF NOT EXISTS spv_incoming_state ON incoming(state);
                CREATE INDEX IF NOT EXISTS spv_outgoing_state ON outgoing(state);
                CREATE INDEX IF NOT EXISTS spv_request_route ON outgoing(json_extract(body,'$.header.route'))
                    WHERE json_extract(body,'$.header.kind')='REQUEST';
            """)
            self.put("packet-history-enabled", True)
        personal=c.get("withdrawal_fees")
        if self.get("withdrawal_fees_config") not in (None,personal):raise ValueError("SPV_WITHDRAWAL_FEE_PROFILE_CHANGED")
        if personal is not None:self.put("withdrawal_fees_config",personal)
        profile=c.get('address_deposits')
        if self.get('address_deposits_config') not in (None,profile):raise ValueError('SPV_ADDRESS_PROFILE_CHANGED')
        if profile is not None:self.put('address_deposits_config',profile)
    def close(self): self.db.close()
    def get(self,k):
        row=self.db.execute("SELECT body FROM meta WHERE key=?",(k,)).fetchone()
        return json.loads(row[0]) if row else None
    def put(self,k,v): self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",(k,canonical(v)))
    def outgoing(self,logical):
        row=self.db.execute("SELECT id,body,state FROM outgoing WHERE logical=?",(logical,)).fetchone()
        return (row[0],json.loads(row[1]),row[2]) if row else None
    def queue(self,logical,packet):
        old=self.outgoing(logical)
        if old: return old[0]
        self.require_space()
        if self.active_count("outgoing") >= int(self.c["max_pending_messages"]):
            raise ValueError("SPV_OUTBOX_FULL")
        key=message_key(packet["header"]["route"],packet["header"]["kind"],packet["header"]["sequence"])
        self.db.execute("INSERT INTO outgoing VALUES(?,?,?,'QUEUED')",(key,logical,canonical(packet)))
        return key
    def active_count(self, direction):
        predicates = {"incoming": " WHERE state IN ('RECEIVED','VALIDATION_REVIEW_REQUIRED')",
                      "outgoing": " WHERE state!='CONFIRMED'"}
        predicate = predicates[direction]
        return self.db.execute("SELECT COUNT(*) FROM " + direction + (predicate if self.history_limit is not None else "")).fetchone()[0]

    def require_space(self):
        require_history_space(self.path, "transport.sqlite", self.history_limit)

    def history_status(self):
        if self.history_limit is None:
            return None
        return dict(admission_limit_bytes=self.history_limit, bytes=sqlite_bytes(self.path,"transport.sqlite"),
            active_incoming=self.active_count("incoming"), active_outgoing=self.active_count("outgoing"),
            replies_anchor=self.get("reply-history-anchor"), rechecking_replies=bool(self.get("reply-history-recheck")))

    def incoming(self,kind=None,*,received_only=False,limit=None):
        clauses, args = [], []
        if kind:
            clauses.append("json_extract(body,'$.header.kind')=?"); args.append(kind)
        elif received_only:
            clauses.append("json_extract(body,'$.header.kind') IN ('ACK','STATUS')")
        if received_only:
            clauses.append("state='RECEIVED'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT id,body,state FROM incoming" + where + " ORDER BY rowid"
        if limit is not None:
            query += " LIMIT ?"; args.append(limit)
        return [(key,json.loads(body),state) for key,body,state in self.db.execute(query,args)]

    def request_for_route(self, route):
        row = self.db.execute("SELECT body FROM outgoing WHERE json_extract(body,'$.header.kind')='REQUEST' "
                              "AND json_extract(body,'$.header.route')=?", (route,)).fetchone()
        return json.loads(row[0]) if row else None
    def mark(self,key,state): self.db.execute("UPDATE incoming SET state=? WHERE id=?",(state,key))
    @contextmanager
    def exclusive(self):
        fd=os.open(self.path/"transport.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"a") as f:
            try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError("SPV_TRANSPORT_BUSY") from None
            yield
    def reserve(self,key,cost):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows={r[0]:(int(r[1]),r[2]) for r in self.db.execute("SELECT * FROM costs")}
            prior,attempts=rows.get(key,(0,0)); wanted=max(prior,cost)
            if (wanted>int(self.c["max_transaction_cost_wei"]) or
                    sum(v[0] for k,v in rows.items() if k!=key)+wanted>int(self.c["max_total_cost_wei"])):
                raise ValueError("SPV_TRANSPORT_GAS_BUDGET")
            if attempts>=int(self.c["max_publish_attempts"]): raise ValueError("SPV_TRANSPORT_ATTEMPTS")
            self.db.execute("INSERT INTO costs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET cost=excluded.cost,attempts=excluded.attempts",
                (key,str(wanted),attempts+1)); self.db.commit()
        except BaseException: self.db.rollback(); raise


class Client:
    def __init__(self,c,w3=None):
        self.c=c; self.w3=w3 or evm_web3(c["evm_rpc"], timeout=30)
        self.account=Account.from_key(json.loads(read_private(Path(c["identity_file"])))["private_key"])
        self.store=Store(c,self.account.address)
        self.keys={}
        for path in c["key_files"]:
            k=json.loads(read_private(Path(path)))
            if set(k)!={"epoch","public","secret"}: raise ValueError("SPV_KEY_SCHEMA")
            epoch=uint(k["epoch"],1); hex32(k["public"]); hex32(k["secret"])
            if epoch in self.keys: raise ValueError("SPV_KEY_EPOCH_DUPLICATE")
            self.keys[epoch]=k
    def close(self): self.store.close()
    def stable(self,block):
        if self.w3.eth.get_block(block.number).hash!=block.hash: raise ValueError("SPV_EVM_REORG")
    def anchor_reorg(self, anchor, block):
        try:
            verify_anchor(self.w3, anchor["number"], anchor["hash"], safe=block)
        except ValueError as error:
            if str(error) == "EVM_REORG_RECOVERY_REQUIRED":
                self.stable(block)
                return True
            raise
        return False
    def context(self):
        if str(self.w3.eth.chain_id)!=self.c["chain_id"]: raise ValueError("CHAIN_MISMATCH")
        b=read_block(self.w3,int(self.c["evm_confirmations"]))
        board=self.w3.eth.contract(address=self.c["board"],abi=artifact("SPVMessageBoard")["abi"])
        checked_runtime(self.w3,board.address,"SPVMessageBoard",b.number)
        def call(c,n,*a): return getattr(c.functions,n)(*a).call(block_identifier=b.number)
        view_address=call(board,"stateView")
        prove_creation(self.w3,board.address,"SPVMessageBoard",[self.c["factory"],self.c["confederation_id"],view_address],b.number)
        checked_runtime(self.w3,self.c["factory"],"ConfederationFactory",b.number)
        prove_creation(self.w3,self.c["factory"],"ConfederationFactory",None,b.number)
        view=inspect_service_view(self.w3,view_address,self.c["bitcoin_policy"],b.number,state_address=self.c["zk_state"])
        domain=Web3.keccak(encode(["bytes32","uint256","address","address","string","address","address","address","bytes32"],
            [Web3.keccak(text="SATOSHIFI_SPV_MESSAGES_V1"),int(self.c["chain_id"]),board.address,self.c["factory"],
             self.c["confederation_id"],view_address,call(view,"state"),call(view,"adapter"),call(view,"policyHash")])).hex()
        if call(board,"domain").hex()!=domain or call(board,"confederationId")!=self.c["confederation_id"]:
            raise ValueError("SPV_DOMAIN_BINDING")
        self.stable(b)
        return b,board,domain
    def _key(self,board,block,address,epoch=None):
        epoch=epoch or board.functions.currentEpoch(address).call(block_identifier=block.number)
        key=board.functions.transportKeys(address,epoch).call(block_identifier=block.number).hex()
        if epoch==0 or key=="00"*32: raise ValueError("SPV_REGISTERED_KEY_REQUIRED")
        return epoch,key
    def _header(self,board,b,route,kind,seq,recipient,ttl):
        se,sp=self._key(board,b,self.account.address); re,rp=self._key(board,b,recipient)
        if se not in self.keys or self.keys[se]["public"]!=sp: raise ValueError("SPV_LOCAL_KEY_REQUIRED")
        return dict(route=route,kind=kind,sequence=str(seq),sender=self.account.address,recipient=recipient,
            sender_epoch=str(se),recipient_epoch=str(re),expires_at=str(b.timestamp+int(ttl))),rp
    def send_request(self,need,federation,recipient,revision="0",payment_job=None):
        if need.get("kind")=="withdrawal_fee":
            from .withdrawal_fee_spv import scope
            policy=dict(self.c,allowed_consumers={pool:"MiningPoolV1" for pool in self.c.get("withdrawal_fees",{}).get("registries",{})})
            scope(policy,need)
        if need.get('kind')=='address_deposit':
            from .address_spv import scope
            profile=dict(self.c,allowed_consumers={pool:'MiningPoolV1' for pool in self.c.get('address_deposits',{}).get('registries',{})})
            scope(profile,need)
        with self.store.exclusive():
            uint(revision)
            b,board,domain=self.context()
            if not isinstance(federation,str) or not 1<=len(federation.encode())<=128:raise ValueError("SPV_FEDERATION")
            if not Web3.is_checksum_address(recipient): raise ValueError("SPV_RECIPIENT")
            if payment_job is not None:hex32(payment_job)
            logical="request:"+hashlib.sha256(canonical([domain,federation,recipient,need,revision,payment_job]).encode()).hexdigest()
            old=self.store.outgoing(logical)
            if old: return old[0],old[1]
            if not board.functions.requesterAllowed(federation,self.account.address).call(block_identifier=b.number): raise ValueError("SPV_REQUEST_ROLE")
            if not board.functions.serviceAllowed(recipient).call(block_identifier=b.number): raise ValueError("SPV_SERVICE_ROLE")
            request_id=secrets.token_hex(32); route=route_key(request_id,self.account.address,recipient)
            header,pk=self._header(board,b,route,"REQUEST",0,recipient,self.c["request_ttl_seconds"])
            body=dict(version="1",domain=domain,request_id=request_id,nonce=secrets.token_hex(32),
                confederation_id=self.c["confederation_id"],federation_id=federation,chain_id=self.c["chain_id"],
                state=self.c["zk_state"],adapter=board.functions.stateView().call(block_identifier=b.number),
                bitcoin_network='bitcoin-'+self.c['bitcoin_policy']['network'],checkpoint=checkpoint_descriptor(self.c["bitcoin_policy"]).hex(),
                program_image_id=zk_profile(self.c['bitcoin_policy'])['image'],journal_schema="SATOSHIFI_TRANSITION_V2",
                reason="DEPOSIT" if need.get("kind") in ("deposit","address_deposit","withdrawal_fee") else "WITHDRAWAL",operation_id=hashlib.sha256(canonical(need).encode()).hexdigest(),
                need=need,created_at=str(b.timestamp),deliver_by=header["expires_at"],
                service_deadline=str(b.timestamp+int(self.c["service_sla_seconds"])),payment_job_id=payment_job)
            # Actual adapter, never the read-only payment view.
            view=self.w3.eth.contract(address=body["adapter"],abi=artifact(zk_profile(self.c['bitcoin_policy'])["view"])["abi"])
            body["adapter"]=view.functions.adapter().call(block_identifier=b.number)
            state=self.w3.eth.contract(address=self.c["zk_state"],abi=artifact(zk_profile(self.c["bitcoin_policy"])["state"])["abi"])
            body["known_state"]=dict(commitment=state.functions.getState().call(block_identifier=b.number)[7].hex(),
                version=str(state.functions.stateVersion().call(block_identifier=b.number)))
            from .spv_automation import offer
            proposed=offer(self,need,federation,recipient)
            if proposed is not None:body.update(version='2',payment_request=proposed)
            packet=dict(header=header,ciphertext=seal(domain,header,body,pk),body=body,request_id=request_id,federation=federation)
            self.stable(b); key=self.store.queue(logical,packet)
            return key,packet
    def queue_reply(self,route,kind,seq,body,*,notice=None):
        with self.store.exclusive():return self._queue_reply(route,kind,seq,body,notice=notice)
    def _queue_reply(self,route,kind,seq,body,*,notice=None):
        logical=kind.lower()+":"+route+":"+str(seq)
        old=self.store.outgoing(logical)
        if old: return old[0]
        b,board,domain=self.context(); info=board.functions.route(hex32(route)).call(block_identifier=b.number)
        if info[1]!=self.account.address: raise ValueError("SPV_REPLY_ROLE")
        header,pk=self._header(board,b,route,kind,seq,info[0],self.c["reply_ttl_seconds"])
        packet=dict(header=header,ciphertext=seal(domain,header,body,pk),body=body,notice=notice)
        self.stable(b)
        return self.store.queue(logical,packet)
    def transmit(self,*,message_id=None,recover_only=False):
        if message_id is not None:hex32(message_id)
        with self.store.exclusive():
            b,board,_=self.context()
            anchor=self.store.get("outgoing_anchor")
            if anchor and self.anchor_reorg(anchor, b):
                self.store.db.execute("UPDATE outgoing SET state='QUEUED' WHERE state='CONFIRMED'")
            self.store.put("outgoing_anchor",dict(number=b.number,hash=b.hash.hex()))
            rows=self.store.db.execute("SELECT id,body FROM outgoing WHERE state!='CONFIRMED'"+
                (" AND id=?" if message_id is not None else "")+" ORDER BY rowid",
                () if message_id is None else (message_id,))
            for key,encoded in rows:
                packet=json.loads(encoded); h=packet["header"]
                retry=self.store.get("retry:"+key) or dict(next_at=0,attempts=0)
                if b.timestamp<retry["next_at"]: continue
                old=board.functions.packet(hex32(key)).call(block_identifier=b.number)
                if bytes(old[8]):
                    if (bytes(old[8]).hex()!=packet["ciphertext"] or old[3]!=h["sender"] or old[4]!=h["recipient"]
                            or old[5]!=int(h["sender_epoch"]) or old[6]!=int(h["recipient_epoch"]) or old[7]!=int(h["expires_at"])):
                        raise ValueError("SPV_CANONICAL_PACKET_CONFLICT")
                    self.stable(b); self.store.db.execute("UPDATE outgoing SET state='CONFIRMED' WHERE id=?",(key,))
                    return dict(status="CONFIRMED",message_id=key)
                if retry["attempts"]>=int(self.c["max_publish_attempts"]): continue
                # Recover original signed bytes through Bootstrap, including expiry/reorg.
                if h["kind"]=="REQUEST":
                    method="request"; args=["0x"+packet["request_id"],packet["federation"],h["recipient"],h["sender_epoch"],h["recipient_epoch"],h["expires_at"],"0x"+packet["ciphertext"]]
                else:
                    method="reply"; args=["0x"+h["route"],str(KINDS[h["kind"]]),h["sequence"],h["sender_epoch"],h["recipient_epoch"],h["expires_at"],"0x"+packet["ciphertext"]]
                if recover_only:
                    result=self.send("message-"+key,method,args,recover_only=True)
                    if result['status']=='NOT_SIGNED':return dict(status='NOT_SIGNED',message_id=key)
                    self.store.put("retry:"+key,dict(attempts=retry["attempts"]+1,next_at=b.timestamp+int(self.c["retry_backoff_seconds"])*2**retry["attempts"]))
                else:
                    self.store.put("retry:"+key,dict(attempts=retry["attempts"]+1,next_at=b.timestamp+int(self.c["retry_backoff_seconds"])*2**retry["attempts"]))
                    result=self.send("message-"+key,method,args)
                return dict(status=result["status"],message_id=key)
            return dict(status="IDLE")
    def send(self,operation,method,args,*,recover_only=False):
        client=self
        class Sender(Bootstrap):
            def build_transaction(self,step,account,nonce):
                tx=super().build_transaction(step,account,nonce)
                if tx["gas"]>int(client.c["max_tx_gas"]): raise ValueError("SPV_TRANSPORT_GAS_LIMIT")
                client.store.reserve(operation,tx["gas"]*tx.get("maxFeePerGas",tx.get("gasPrice"))+int(tx.get("value",0)))
                return tx
        c=self.c; step=dict(id="send",kind="call",contract="board",method=method,args=args,value="0")
        namespace=hashlib.sha256(canonical([c["chain_id"],c["board"],self.account.address,operation]).encode()).hexdigest()
        profile=dict(deployment_id="spv-transport-"+namespace,mode="backed",bitcoin_network=c['bitcoin_policy']['network'],bitcoin_policy=c["bitcoin_policy"],
            chain_id=c["chain_id"],evm_confirmations=c["evm_confirmations"],evm_rpc=c["evm_rpc"],
            contracts={"board":dict(address=c["board"],artifact="SPVMessageBoard")},steps=[step])
        runner=Sender(profile,self.store.path/"transactions.sqlite")
        if recover_only:
            from .bootstrap_parallel import restore_signed
            from .operations import Journal
            identity=Path(c['identity_file']).resolve()
            key=profile['deployment_id']+':send';entry=runner.journal.put(key,step)
            lane=Journal(str(identity.with_suffix('.evm-nonce.lock'))+'.sqlite')
            if not entry['result'].get('raw_transaction') and not restore_signed(lane,runner,key,step):
                return dict(status='NOT_SIGNED')
        return runner.apply(c["identity_file"])
    def poll(self):
        with self.store.exclusive():
            b,board,domain=self.context()
            cursor=self.store.get("cursor") or dict(index=0,number=b.number,hash=b.hash.hex())
            rewind=self.anchor_reorg(cursor, b)
            count=board.functions.messageCount(self.account.address).call(block_identifier=b.number)
            if rewind:
                cursor["index"]=0  # Re-read canonical bytes; never delete obligations/outgoing ciphertext.
            elif cursor["index"]>count:
                raise ValueError("EVM_RPC_BEHIND")
            saved=[]
            end=min(count,cursor["index"]+int(self.c["max_messages_per_poll"]))
            for i in range(cursor["index"],end):
                key=board.functions.messageAt(self.account.address,i).call(block_identifier=b.number).hex()
                p=board.functions.packet(hex32(key)).call(block_identifier=b.number)
                h=dict(route=p[0].hex(),kind={0:"REQUEST",1:"ACK",2:"STATUS"}[p[1]],sequence=str(p[2]),sender=p[3],recipient=p[4],
                    sender_epoch=str(p[5]),recipient_epoch=str(p[6]),expires_at=str(p[7]))
                record=dict(header=h,number=b.number,block_hash=b.hash.hex(),ciphertext_sha256=hashlib.sha256(bytes(p[8])).hexdigest())
                epoch,pk=self._key(board,b,self.account.address,int(h["recipient_epoch"]))
                if epoch not in self.keys or self.keys[epoch]["public"]!=pk: raise ValueError("SPV_LOCAL_KEY_REQUIRED")
                try:
                    if h["recipient"]!=self.account.address or message_key(h["route"],h["kind"],h["sequence"])!=key: raise ValueError("SPV_PACKET_BINDING")
                    record["body"]=open_packet(domain,h,bytes(p[8]).hex(),self.keys[epoch]["secret"])
                    r=board.functions.route(hex32(h["route"])).call(block_identifier=b.number)
                    record["route"]=dict(requester=r[0],service=r[1],federation=r[2],request_id=r[3].hex())
                    state="RECEIVED"
                except (ValueError,RuntimeError,KeyError,json.JSONDecodeError):
                    record["reason"]="SPV_PACKET_UNREADABLE"; state="QUARANTINED"
                saved.append((key,record,state))
            self.stable(b); self.store.db.execute("BEGIN IMMEDIATE")
            try:
                if rewind:
                    self.store.db.execute("UPDATE incoming SET state='RECEIVED' WHERE state!='QUARANTINED' AND json_extract(body,'$.header.kind') IN ('ACK','STATUS')")
                for key,record,state in saved:
                    old=self.store.db.execute("SELECT state,body FROM incoming WHERE id=?",(key,)).fetchone()
                    if not old:
                        self.store.require_space()
                        if ((self.store.history_limit is None or state == 'RECEIVED')
                                and self.store.active_count("incoming") >= int(self.c["max_pending_messages"])):
                            raise ValueError("SPV_INBOX_FULL")
                    if old:
                        prior=json.loads(old[1])
                        if "body" in prior and (prior["body"]!=record.get("body") or prior["header"]!=record["header"]):
                            prior["reason"]="SPV_CONFLICTING_ORPHANED_MESSAGE"
                            self.store.db.execute("UPDATE incoming SET body=?,state='QUARANTINED' WHERE id=?",(canonical(prior),key))
                            continue
                    self.store.db.execute("INSERT INTO incoming VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,state=CASE WHEN incoming.state IN ('QUARANTINED','ORPHANED') THEN excluded.state ELSE incoming.state END",(key,canonical(record),state))
                self.store.put("cursor",dict(index=end,number=b.number,hash=b.hash.hex())); self.store.db.commit()
            except BaseException: self.store.db.rollback(); raise
            return len(saved)

    def process_replies(self):
        with self.store.exclusive():
            b,board,_=self.context()
            cursor = self.store.get("cursor")
            if cursor is not None and cursor["number"] > b.number:
                raise ValueError("EVM_RPC_BEHIND")
            history_enabled = self.store.history_limit is not None
            if history_enabled:
                self._recheck_replies(b)
            limit = int(self.c['max_messages_per_poll']) if history_enabled else None
            for key,record,state in self.store.incoming(received_only=history_enabled, limit=limit):
                h=record.get("header",{})
                if h.get("kind") not in ("ACK","STATUS"):continue
                body=record.get("body",{});original=self.store.request_for_route(h["route"])
                if state=="QUARANTINED":continue
                if not original:
                    if history_enabled:self.store.mark(key,"QUARANTINED")
                    continue
                current=board.functions.packet(hex32(key)).call(block_identifier=b.number)
                projection=self.store.get("reply:"+h["route"]) or dict(sequence=-1,acknowledged=False,status="AWAITING_ACK")
                if not bytes(current[8]):
                    if projection.get("ack_message_id")==key:
                        projection["acknowledged"]=False;self.store.put("reply:"+h["route"],projection)
                    if projection.get("message_id")==key:
                        projection["status"]="DELIVERY_REVALIDATION_REQUIRED"
                        self.store.put("reply:"+h["route"],projection)
                    self.store.mark(key,"ORPHANED");continue
                if record.get("ciphertext_sha256")!=hashlib.sha256(bytes(current[8])).hexdigest():
                    self.store.mark(key,"QUARANTINED");continue
                if state=="HANDLED":continue
                request=original["body"]
                valid=(body.get("version")=="1" and body.get("request_id")==original["request_id"]
                    and h["sender"]==original["header"]["recipient"] and h["recipient"]==self.account.address)
                notice=body.get("status")=="HEARTBEAT_NOTICE"
                if not notice:valid=valid and body.get("operation_id")==request["operation_id"]
                allowed={"ACCEPTED","REJECTED","QUEUED","READY","COMPLETED","NEEDS_STATE","WAITING_BITCOIN",
                    "WAITING_BITCOIN_REORG","AWAITING_SERVICE_PAYMENT","HALTED","HEARTBEAT_NOTICE","OBSERVATION_UNAVAILABLE","QUOTE"}
                valid=valid and body.get("status") in allowed
                if body.get("service_deadline") is not None:
                    valid=valid and body["service_deadline"]==request["service_deadline"]
                if not valid:self.store.mark(key,"QUARANTINED");continue
                if h["kind"]=="ACK":
                    projection["acknowledged"]=body["status"]=="ACCEPTED"
                    projection["ack_message_id"]=key
                    if projection["sequence"]==-1:projection.update(status=body["status"],message_id=key)
                elif int(h["sequence"])>projection["sequence"]:
                    projection["sequence"]=int(h["sequence"])
                    if notice:projection["heartbeat_notice"]=key
                    else:projection.update(status=body["status"],message_id=key)
                projection.update(number=b.number,block_hash=b.hash.hex(),
                    late=b.timestamp>int(h["expires_at"]))
                self.stable(b);self.store.put("reply:"+h["route"],projection);self.store.mark(key,"HANDLED")
            if history_enabled and self.store.get("reply-history-recheck"):
                if self.store.incoming(received_only=True, limit=1):
                    raise ValueError("SPV_REPLY_HISTORY_RECHECK_REQUIRED")
                self.stable(b)
                self.store.put("reply-history-recheck", False)

    def _recheck_replies(self, block):
        # Reply projections are derived from exact ciphertext records. Rebuild
        # them in bounded pages after confirmed canonical history changes.
        scope = self.store.get("reply-history-anchor")
        rewind = not scope or self.anchor_reorg(scope, block)
        self.stable(block)
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            if rewind:
                db.execute("DELETE FROM meta WHERE key GLOB 'reply:*'")
                db.execute("UPDATE incoming SET state='RECEIVED' WHERE state!='QUARANTINED' "
                           "AND json_extract(body,'$.header.kind') IN ('ACK','STATUS')")
                self.store.put("reply-history-recheck", True)
            self.store.put("reply-history-anchor", dict(number=block.number,hash=block.hash.hex()))
            db.commit()
        except BaseException:
            db.rollback()
            raise


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("action",choices=["keygen","register-key","request","poll","flush","status","delegate","rescan"])
    p.add_argument("--config"); p.add_argument("--file"); p.add_argument("--output"); p.add_argument("--epoch"); p.add_argument("--federation");p.add_argument("--recipient")
    p.add_argument("--service",action="store_true");p.add_argument("--revoke",action="store_true");p.add_argument("--revision");p.add_argument("--payment-job"); a=p.parse_args()
    if a.action=="keygen":
        epoch=uint(a.epoch,1); path=Path(a.output)
        if path.exists(): raise ValueError("SPV_KEY_FILE_EXISTS")
        key=crypto.call("transport_key");write(path,dict(epoch=str(epoch),**key));print(json.dumps(dict(status="CREATED",public=key["public"],epoch=str(epoch))));return
    c=Client(load_config(a.config))
    try:
        if a.action=="register-key":
            epoch=uint(a.epoch,1);k=c.keys[epoch]; c.context()
            result=c.send("key-"+str(epoch),"registerKey",[str(epoch),"0x"+k["public"]])
        elif a.action=="delegate":
            revision=uint(a.revision,1)
            c.context(); method="setServiceDelegate" if a.service else "setRequestDelegate"
            args=[a.recipient,not a.revoke] if a.service else [a.federation,a.recipient,not a.revoke]
            result=c.send("delegate-"+hashlib.sha256(canonical([method,args,revision]).encode()).hexdigest(),method,args)
        elif a.action=="request":
            key,_=c.send_request(json.loads(read_private(Path(a.file))),a.federation,a.recipient,a.revision or "0",a.payment_job)
            result=dict(status="QUEUED",message_id=key)
        elif a.action=="poll":
            count=c.poll();c.process_replies()
            from .spv_automation import payer_tick
            result=dict(status="POLLED",messages=count,payment=payer_tick(c))
        elif a.action=="rescan":
            with c.store.exclusive():
                c.store.put("cursor",None)
                c.store.db.execute("UPDATE incoming SET state='RECEIVED' WHERE state='VALIDATION_REVIEW_REQUIRED'")
            result=dict(status="RESCAN_SCHEDULED")
        elif a.action=="flush": result=c.transmit()
        else:
            c.process_replies()
            receipts=[dict(route=k[len("reply:"):],status=json.loads(v)["status"],acknowledged=json.loads(v)["acknowledged"]) for k,v in c.store.db.execute("SELECT key,body FROM meta WHERE key LIKE 'reply:%'")]
            result=dict(status="LOCAL",receipts=receipts,outgoing=dict(c.store.db.execute("SELECT state,COUNT(*) FROM outgoing GROUP BY state")),incoming=dict(c.store.db.execute("SELECT state,COUNT(*) FROM incoming GROUP BY state")),history=c.store.history_status())
        print(json.dumps(result))
    finally: c.close()

if __name__=="__main__":
    try: main()
    except Exception as e:
        code=str(e) if type(e) is ValueError and re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}",str(e)) else "SPV_TRANSPORT_UNAVAILABLE"
        print(json.dumps(dict(status="BLOCKED",reason=code)));raise SystemExit(2)
