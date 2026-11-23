"""Address request HPKE transport. Durable delivery is never a Ready assertion."""
import argparse
import hashlib
import json
from pathlib import Path
import secrets

from eth_abi import encode
from eth_account import Account
from web3 import Web3
from .evm_rpc import evm_web3
from . import crypto
from .address_watch import Store, digest, trusted_remote_filters
from .bootstrap import Bootstrap, artifact
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .deposit_intent import DepositIntentV1
from .evm_network import read_block
from .onboarding import read_private
from .operations import canonical, Journal
from .spv_transport import hex32, uint, route_key, message_key, KINDS


def hpke_context(domain,header):
    data=canonical(["SATOHSI_ADDRESS_HPKE_V1",domain,header]).encode()
    return hashlib.sha256(b"ADDRESS-INFO\0"+data).hexdigest(),data.hex()


def seal(domain,header,body,public):
    raw=canonical(body).encode()
    if len(raw)>3900:raise ValueError("ADDRESS_MESSAGE_SIZE")
    info,aad=hpke_context(domain,header)
    r=crypto.call("seal",public=public,info=info,aad=aad,plaintext=raw.hex())
    return r["enc"]+r["ciphertext"]


def open_packet(domain,header,ciphertext,secret):
    if not 48<len(bytes.fromhex(ciphertext))<=4096:raise ValueError("ADDRESS_MESSAGE_SIZE")
    info,aad=hpke_context(domain,header)
    r=crypto.call("open",secret=secret,info=info,aad=aad,enc=ciphertext[:64],ciphertext=ciphertext[64:])
    raw=bytes.fromhex(r["plaintext"]);body=json.loads(raw)
    if not isinstance(body,dict) or len(raw)>3900 or canonical(body).encode()!=raw:
        raise ValueError("ADDRESS_MESSAGE_ENCODING")
    return body


def load_config(path):
    return validate_config(json.loads(read_private(path)))


def validate_config(c):
    fields={"version","chain_id","evm_confirmations","evm_rpc","bitcoin_network","bitcoin_policy",
        "registry","pool","board","identity_file","key_files","directory","bitcoin_rpc","bitcoin_cookie",
        "max_messages","max_messages_per_poll","max_intents","max_outputs","max_archive_bytes","max_blocks_per_poll","max_reorg",
        "ttl_seconds","max_publish_attempts","max_tx_gas","max_transaction_cost_wei","max_total_cost_wei"}
    if (set(c)-{"trust_remote_watch_filters"} != (fields|({'fee_reserve_config'} if c.get('version')=='2' else set()))
            or type(c.get("trust_remote_watch_filters",False)) is not bool
            or c["version"] not in ('1','2') or c["chain_id"] not in ("1","11155111","31337")):
        raise ValueError("ADDRESS_CONFIG")
    from .bootstrap_policy import validate_confirmations
    validate_confirmations(c,"ADDRESS_")
    from .bitcoin_trust import graph_options
    graph_options(dict(version="2",bitcoin_network=c["bitcoin_network"],evm_confirmations=c["evm_confirmations"],
        **({"bitcoin_policy":c["bitcoin_policy"]} if c["bitcoin_policy"] is not None else {})))
    for name in ("registry","pool","board"):
        if not Web3.is_checksum_address(c[name]) or int(c[name],16)==0:raise ValueError("ADDRESS_CONFIG")
    for name,lo,hi in (("max_messages",1,4096),("max_messages_per_poll",1,64),("max_intents",1,256),
        ("max_outputs",1,4096),("max_blocks_per_poll",1,256),("max_reorg",1,100),("ttl_seconds",60,86400),
        ("max_archive_bytes",2**20,2**36),
        ("max_publish_attempts",1,32),("max_tx_gas",21000,16777216),
        ("max_transaction_cost_wei",1,2**256-1),("max_total_cost_wei",1,2**256-1)):
        uint(c[name],lo,hi)
    if not isinstance(c["key_files"],list) or not 1<=len(c["key_files"])<=32:raise ValueError("ADDRESS_CONFIG")
    for p in [c["directory"],c["identity_file"],*([c["bitcoin_cookie"]] if c["bitcoin_cookie"] is not None else []),*c["key_files"]]:
        if not Path(p).is_absolute():raise ValueError("ADDRESS_ABSOLUTE_PATH")
    if c['version']=='2' and (not isinstance(c['fee_reserve_config'],str) or not Path(c['fee_reserve_config']).is_absolute()):raise ValueError('ADDRESS_CONFIG')
    return c


class Client:
    def seal_packet(self,*args):return seal(*args)
    def open_message(self,*args):return open_packet(*args)
    def __init__(self,c,w3=None):
        self.c=c;self.w3=w3 or evm_web3(c["evm_rpc"], timeout=30)
        self.account=Account.from_key(json.loads(read_private(c["identity_file"]))["private_key"])
        b,pool,ctx,board,domain=self.context()
        r=ctx["registry"]
        self.scope=dict(chain_id=c["chain_id"],registry=r.address,pool=pool.address,board=board.address,
            domain=domain,bitcoin_network=c["bitcoin_network"],bitcoin_genesis=self.read(r,b,"bitcoinGenesis").hex(),
            base_group="02"+bytes(self.read(r,b,"baseX")).hex(),wallet_result=self.read(r,b,"walletResult").hex(),
            key_epoch=str(self.read(r,b,"keyEpoch")))
        self.store=Store(Path(c["directory"])/"address.sqlite",dict(self.scope,identity=self.account.address),
            int(c["max_intents"]),int(c["max_outputs"]),int(c["max_archive_bytes"]))
        # Gas authorization state is deliberately outside watch backup/restore.
        cost_path=Path(str(Path(c["identity_file"]).resolve())+".address-costs.sqlite")
        existed=cost_path.exists()
        lane=Journal(str(Path(c["identity_file"]).resolve())+".transactions.sqlite")
        with lane.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS address_cost_binding(token TEXT PRIMARY KEY)")
            rows=db.execute("SELECT token FROM address_cost_binding").fetchall()
            if rows:
                if len(rows)!=1 or not existed:raise ValueError("ADDRESS_GAS_RECOVERY_REQUIRED")
                token=rows[0][0]
            else:
                if existed:raise ValueError("ADDRESS_GAS_RECOVERY_REQUIRED")
                token=secrets.token_hex(32);db.execute("INSERT INTO address_cost_binding VALUES(?)",(token,))
        self.costs=Journal(cost_path)
        with self.costs.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS address_cost_identity(token TEXT PRIMARY KEY)")
            rows=db.execute("SELECT token FROM address_cost_identity").fetchall()
            if existed and [r[0] for r in rows]!=[token]:raise ValueError("ADDRESS_GAS_RECOVERY_REQUIRED")
            if not existed:db.execute("INSERT INTO address_cost_identity VALUES(?)",(token,))
        self.cost_scope=digest([c["chain_id"],c["board"],self.account.address])
        if c.get('version')=='2':
            from .btc_fee_reserve import load_config
            fc=load_config(c['fee_reserve_config'])
            if any(fc[k]!=c[k] for k in ('chain_id','bitcoin_network','bitcoin_policy','evm_confirmations','identity_file')):raise ValueError('BTC_FEE_GRAPH_MISMATCH')
            self.costs.put('address-fee-policy:'+self.cost_scope,dict(reserve=fc['reserve'],
                policy=digest({k:v for k,v in fc.items() if k not in ('evm_rpc','bitcoin_rpc','bitcoin_cookie','directory','trust_remote_watch_filters')})))
        else:
            try:self.costs.get('address-fee-policy:'+self.cost_scope)
            except KeyError:pass
            else:raise ValueError('BTC_FEE_CONFIG_REQUIRED')
        with self.costs.db() as db:db.execute("CREATE TABLE IF NOT EXISTS address_costs(scope TEXT,id TEXT,cost TEXT,PRIMARY KEY(scope,id))")
        self.keys={}
        for path in c["key_files"]:
            k=json.loads(read_private(path))
            if set(k)!={"epoch","public","secret"}:raise ValueError("ADDRESS_KEY_SCHEMA")
            epoch=uint(k["epoch"],1);hex32(k["public"]);hex32(k["secret"])
            if epoch in self.keys:raise ValueError("ADDRESS_KEY_SCHEMA")
            self.keys[epoch]=k
    @staticmethod
    def read(node,block,method,*args):
        return getattr(node.functions,method)(*args).call(block_identifier=block.number)
    def stable(self,b):
        if self.w3.eth.get_block(b.number).hash!=b.hash:raise ValueError("ADDRESS_EVM_REORG")
    def context(self,*,verify_derivation=True):
        from .address_mint import configuration
        c=self.c
        if str(self.w3.eth.chain_id)!=c["chain_id"]:raise ValueError("CHAIN_MISMATCH")
        b=read_block(self.w3,int(c["evm_confirmations"]))
        pool=self.w3.eth.contract(address=c["pool"],abi=artifact("MiningPoolV1")["abi"])
        ctx=configuration(self.w3,pool,b.number,**({} if verify_derivation else {'verify_derivation':False}));r=ctx["registry"]
        if r.address!=c["registry"]:raise ValueError("ADDRESS_GRAPH_MISMATCH")
        checked_runtime(self.w3,c["board"],"BitcoinAddressMessageBoard",b.number)
        prove_creation(self.w3,c["board"],"BitcoinAddressMessageBoard",[r.address],b.number)
        board=self.w3.eth.contract(address=c["board"],abi=artifact("BitcoinAddressMessageBoard")["abi"])
        auth=self.read(pool,b,"authorization")
        domain=Web3.keccak(encode(["bytes32","uint256","address","address","bytes32","address"],
            [Web3.keccak(text="SATOHSI_ADDRESS_MESSAGES_V1"),int(c["chain_id"]),board.address,r.address,
             self.read(r,b,"graphId"),auth])).hex()
        if (self.read(board,b,"registry")!=r.address or self.read(board,b,"authorization")!=auth
                or self.read(board,b,"domain").hex()!=domain):raise ValueError("ADDRESS_GRAPH_MISMATCH")
        if c["bitcoin_policy"] is not None and self.read(r,b,"bitcoinGenesis").hex()!=c["bitcoin_policy"]["genesis_hash"]:
            raise ValueError("BTC_ADDRESS_GENESIS_MISMATCH")
        self.stable(b)
        from .bitcoin_continuity import Catalog,sample
        Catalog(identity_file=c['identity_file']).observe(self.w3,sample(self.w3,ctx,b.number))
        return b,pool,ctx,board,domain
    def _key(self,board,b,who,epoch=None):
        epoch=epoch or self.read(board,b,"currentEpoch",who)
        public=self.read(board,b,"transportKeys",who,epoch).hex()
        if epoch==0 or public=="00"*32:raise ValueError("ADDRESS_REGISTERED_KEY_REQUIRED")
        return epoch,public
    def _header(self,board,b,route,kind,seq,to):
        se,sp=self._key(board,b,self.account.address);re,rp=self._key(board,b,to)
        if se not in self.keys or self.keys[se]["public"]!=sp:raise ValueError("ADDRESS_LOCAL_KEY_REQUIRED")
        return dict(route=route,kind=kind,sequence=str(seq),sender=self.account.address,recipient=to,
            sender_epoch=str(se),recipient_epoch=str(re),expires_at=str(b.timestamp+int(self.c["ttl_seconds"]))),rp
    def _queue(self,packet,logical=None,result=None):
        h=packet["header"];key=message_key(h["route"],h["kind"],h["sequence"])
        self.store.space(len(canonical(packet))*3+8192)
        with self.store.journal.db() as db:
            old=db.execute("SELECT body FROM address_packets WHERE id=?",(key,)).fetchone()
            if old:
                if json.loads(old[0])!=packet:raise ValueError("ADDRESS_PACKET_CONFLICT")
                return key
            if db.execute("SELECT COUNT(*) FROM address_packets").fetchone()[0]>=int(self.c["max_messages"]):
                raise ValueError("ADDRESS_INBOX_FULL")
            db.execute("INSERT INTO address_packets VALUES(?,'OUT',?,'QUEUED')",(key,canonical(packet)))
            if logical is not None:self.store._put(db,logical,result)
        return key
    def make_intent(self,recipient,nonce=None,*,selection=None,accepted=None):
        from .bitcoin_continuity import client_guard
        client_guard(self)
        from .epoch_service import issuance_guard
        issuance_guard(self)
        if selection is not None:
            if nonce is not None or recipient not in (None,selection['recipient']):raise ValueError('RECIPIENT_SELECTION_REQUIRED')
            from .recipient_profiles import authorize_address
            nonce=authorize_address(self,selection,accepted);recipient=selection['recipient']
        b,pool,ctx,_,_=self.context();r=ctx["registry"]
        if not Web3.is_checksum_address(recipient) or int(recipient,16)==0:
            raise ValueError("ADDRESS_RECIPIENT_PROFILE")
        if recipient in {pool.address,r.address,self.read(pool,b,"mpToken"),ctx["common"].address,
                self.read(pool,b,"authorization"),self.read(pool,b,"escrow"),ctx["batch"].address}:
            raise ValueError("ADDRESS_RECIPIENT_PROFILE")
        def addr(value):return bytes.fromhex(value[2:])
        intent=DepositIntentV1(self.read(r,b,"bitcoinGenesis"),int(self.c["chain_id"]),addr(r.address),addr(pool.address),
            addr(self.read(pool,b,"mpToken")),self.read(r,b,"graphId"),addr(self.read(r,b,"dkg")),
            self.read(r,b,"walletId"),self.read(r,b,"walletResult"),int(self.read(r,b,"keyEpoch")),
            addr(self.account.address),addr(recipient),hex32(nonce or secrets.token_hex(32)),self.read(r,b,"servicePolicyHash"))
        self.validate_intent(intent,ctx,b,selection=selection)
        self.stable(b)
        if selection is not None:self.store.put('recipient-choice:'+intent.intent_id.hex(),selection)
        return intent
    def request(self,raw,service,delivery="0"):
        from .bitcoin_continuity import client_guard
        client_guard(self)
        from .epoch_service import issuance_guard
        issuance_guard(self)
        intent=DepositIntentV1.decode(raw);uint(delivery)
        if Web3.to_checksum_address(intent.requester)!=self.account.address:raise ValueError("ADDRESS_REQUESTER_MISMATCH")
        with self.store.exclusive():
            b,pool,ctx,board,domain=self.context()
            selection=self.store.get('recipient-choice:'+intent.intent_id.hex())
            self.validate_intent(intent,ctx,b,selection=selection)
            if service==self.account.address:raise ValueError("ADDRESS_SERVICE_ROLE")
            if not self.read(board,b,"serviceAllowed",service):raise ValueError("ADDRESS_SERVICE_ROLE")
            logical=digest([domain,intent.intent_id.hex(),self.account.address,service,delivery])
            old=self.store.get("request:"+logical)
            if old:return old
            from .address_registration import request_commitment
            salt=secrets.token_hex(32)
            request_id=request_commitment(domain,intent.intent_id.hex(),salt,self.account.address,service)
            route=route_key(request_id,self.account.address,service)
            h,pk=self._header(board,b,route,"REQUEST",0,service)
            body=dict(version="2",domain=domain,request_id=request_id,intent=raw.hex(),commitment_salt=salt,
                intent_id=intent.intent_id.hex(),created_at=str(b.timestamp),deliver_by=h["expires_at"])
            if selection is not None:body.update(version='3',recipient_choice=selection)
            packet=dict(header=h,body=body,ciphertext=self.seal_packet(domain,h,body,pk),request_id=request_id)
            key=message_key(route,"REQUEST","0")
            result=dict(status="QUEUED",message_id=key,route=route,intent_id=intent.intent_id.hex(),ready=False)
            self.stable(b);self._queue(packet,"request:"+logical,result);return result
    def validate_intent(self,intent,ctx,b,*,current_recipient=True,selection=None):
        r=ctx["registry"];pool=ctx["pool"]
        def addr(v):return Web3.to_checksum_address(v)
        if (intent.evm_chain_id!=self.w3.eth.chain_id or addr(intent.deposit_registry)!=r.address
                or addr(intent.pool)!=pool.address or addr(intent.mp_token)!=self.read(pool,b,"mpToken")
                or intent.bitcoin_genesis!=self.read(r,b,"bitcoinGenesis") or intent.custody_graph_id!=self.read(r,b,"graphId")
                or addr(intent.dkg_registry)!=self.read(r,b,"dkg") or intent.wallet_id!=self.read(r,b,"walletId")
                or intent.wallet_result!=self.read(r,b,"walletResult") or intent.key_epoch!=self.read(r,b,"keyEpoch")
                or intent.service_policy_hash!=self.read(r,b,"servicePolicyHash")):
            raise ValueError("ADDRESS_INTENT_SCOPE")
        recipient=addr(intent.recipient)
        if not current_recipient:return  # Recovery retains an existing exact binding.
        if recipient in {
                pool.address,r.address,self.read(pool,b,"mpToken"),ctx["common"].address,
                self.read(pool,b,"authorization"),self.read(pool,b,"escrow"),ctx["batch"].address}:
            raise ValueError("ADDRESS_RECIPIENT_PROFILE")
        from .recipient_profiles import describe_intent
        if selection is None and hasattr(self,'store'):selection=self.store.get('recipient-choice:'+intent.intent_id.hex())
        describe_intent(self.w3,ctx,intent,b.number,selection=selection)
    def poll(self):
        with self.store.exclusive():
            b,_,_,board,domain=self.context();count=self.read(board,b,"messageCount",self.account.address)
            cursor=self.store.get("inbox_cursor") or dict(index=0,number=b.number,hash=b.hash.hex())
            if cursor["index"]>count or cursor["number"]>b.number or self.w3.eth.get_block(cursor["number"]).hash.hex()!=cursor["hash"]:
                cursor["index"]=0
            end=min(count,cursor["index"]+int(self.c["max_messages_per_poll"]))
            for i in range(cursor["index"],end):
                key=self.read(board,b,"messageAt",self.account.address,i).hex();p=self.read(board,b,"packet",hex32(key))
                h=dict(route=p[0].hex(),kind={0:"REQUEST",1:"ACK",2:"STATUS"}[p[1]],sequence=str(p[2]),sender=p[3],recipient=p[4],
                    sender_epoch=str(p[5]),recipient_epoch=str(p[6]),expires_at=str(p[7]))
                if h["recipient"]!=self.account.address or key!=message_key(h["route"],h["kind"],h["sequence"]):raise ValueError("ADDRESS_PACKET_BINDING")
                epoch,public=self._key(board,b,self.account.address,int(h["recipient_epoch"]))
                if epoch not in self.keys or self.keys[epoch]["public"]!=public:raise ValueError("ADDRESS_LOCAL_KEY_REQUIRED")
                body=None;state="RECEIVED"
                try:body=self.open_message(domain,h,bytes(p[8]).hex(),self.keys[epoch]["secret"])
                except (ValueError,RuntimeError,KeyError):state="QUARANTINED"
                route=self.read(board,b,"route",hex32(h["route"]))
                if (route_key(route[2].hex(),route[0],route[1])!=h["route"]
                        or (h["kind"]=="REQUEST" and (h["sender"],h["recipient"])!=(route[0],route[1]))
                        or (h["kind"]!="REQUEST" and (h["sender"],h["recipient"])!=(route[1],route[0]))):
                    raise ValueError("ADDRESS_PACKET_BINDING")
                record=dict(header=h,body=body,request_id=route[2].hex(),ciphertext_hash=hashlib.sha256(bytes(p[8])).hexdigest())
                self.stable(b)
                self.store.space(len(canonical(record))*3+8192)
                with self.store.journal.db() as db:
                    prior=db.execute("SELECT body FROM address_packets WHERE id=?",(key,)).fetchone()
                    if prior and json.loads(prior[0])!=record:raise ValueError("ADDRESS_PACKET_CONFLICT")
                    if not prior and db.execute("SELECT COUNT(*) FROM address_packets").fetchone()[0]>=int(self.c["max_messages"]):raise ValueError("ADDRESS_INBOX_FULL")
                    db.execute("INSERT OR IGNORE INTO address_packets VALUES(?,'IN',?,?)",(key,canonical(record),state))
                    self.store._put(db,"inbox_cursor",dict(index=i+1,number=b.number,hash=b.hash.hex()))
            return end-cursor["index"]
    def service_once(self):
        self.poll()
        with self.store.exclusive():
            b,_,ctx,board,domain=self.context()
            if not self.read(board,b,"serviceAllowed",self.account.address):raise ValueError("ADDRESS_SERVICE_ROLE")
            with self.store.journal.db() as db:
                rows=db.execute("SELECT id,body FROM address_packets WHERE direction='IN' AND state='RECEIVED' AND json_extract(body,'$.header.kind')='REQUEST' ORDER BY rowid LIMIT ?",
                    (int(self.c["max_messages_per_poll"]),)).fetchall()
            for key,encoded in rows:
                record=json.loads(encoded)
                if record["header"]["kind"]!="REQUEST":continue
                try:self._accept(key,record,b,ctx,board,domain)
                except (ValueError,RuntimeError) as exc:
                    from .participant_errors import participant_exception_code
                    code=participant_exception_code(exc)
                    terminal={"ADDRESS_REQUEST_SCHEMA","ADDRESS_REQUEST_SCOPE","ADDRESS_REQUEST_EXPIRED",
                        "ADDRESS_INTENT_SCOPE","ADDRESS_RECIPIENT_PROFILE","ADDRESS_REQUEST_NONCE_CONFLICT","DEPOSIT_INTENT_ENCODING","CRYPTO_VALIDATION_FAILED",
                        "DEPOSIT_INTENT_BYTES","DEPOSIT_INTENT_CHAIN","RECIPIENT_SELECTION_REQUIRED","RECIPIENT_PROFILE",
                        "RECIPIENT_CODE_CHANGED","RECIPIENT_WALLET_PROFILE","RECIPIENT_ADVANCED_ACK","RECIPIENT_DELEGATED_UNSUPPORTED","RECIPIENT_SINK"}
                    if code not in terminal:raise
                    self.store.put("rejected:"+key,dict(reason=code))
                    with self.store.journal.db() as db:db.execute("UPDATE address_packets SET state='QUARANTINED' WHERE id=?",(key,))
                    continue
                with self.store.journal.db() as db:db.execute("UPDATE address_packets SET state='HANDLED' WHERE id=?",(key,))
            return dict(status="ADDRESS_REQUESTS_SAVED",ready=False)
    def _accept(self,key,record,b,ctx,board,domain):
        body=record["body"];h=record["header"]
        fields={"version","domain","request_id","intent","intent_id","created_at","deliver_by"}
        if body.get("version") in ("2","3"):fields.add("commitment_salt")
        if body.get('version')=='3':fields.add('recipient_choice')
        if set(body)!=fields or body.get("version") not in ("1","2","3"):raise ValueError("ADDRESS_REQUEST_SCHEMA")
        try:intent=DepositIntentV1.decode(bytes.fromhex(body["intent"]))
        except (TypeError,ValueError):raise ValueError("ADDRESS_REQUEST_SCHEMA") from None
        if (body["domain"]!=domain or body["request_id"]!=record["request_id"]
                or body["intent_id"]!=intent.intent_id.hex() or Web3.to_checksum_address(intent.requester)!=h["sender"]
                or body["deliver_by"]!=h["expires_at"]):raise ValueError("ADDRESS_REQUEST_SCOPE")
        if body["version"] in ("2","3"):
            from .address_registration import request_commitment
            try:commitment=request_commitment(domain,intent.intent_id.hex(),body["commitment_salt"],h["sender"],h["recipient"])
            except ValueError:raise ValueError("ADDRESS_REQUEST_SCOPE") from None
            if commitment!=record["request_id"]:raise ValueError("ADDRESS_REQUEST_SCOPE")
        try:created=uint(body["created_at"]);deliver=uint(body["deliver_by"])
        except ValueError:raise ValueError("ADDRESS_REQUEST_SCOPE") from None
        if not created<=b.timestamp or not created<deliver:raise ValueError("ADDRESS_REQUEST_SCOPE")
        current=self.read(board,b,"packet",hex32(key))
        if not bytes(current[8]) or hashlib.sha256(bytes(current[8])).hexdigest()!=record["ciphertext_hash"]:
            raise ValueError("ADDRESS_PACKET_ORPHANED")
        with self.store.journal.db() as db:old=db.execute("SELECT body FROM address_requests WHERE route=?",(h["route"],)).fetchone()
        if not old and deliver<=b.timestamp:raise ValueError("ADDRESS_REQUEST_EXPIRED")
        registered=bool(bytes(self.read(ctx['registry'],b,'getBinding',intent.intent_id)[0]))
        if not registered:
            from .bitcoin_continuity import client_guard
            client_guard(self)
            from .epoch_service import issuance_guard
            issuance_guard(self)
        selection=body.get('recipient_choice')
        self.validate_intent(intent,ctx,b,current_recipient=not registered,selection=selection)
        evidence=dict(message_id=key,ciphertext_hash=record["ciphertext_hash"])
        if body["version"] in ("2","3"):evidence["opening"]=body["commitment_salt"]
        if selection is not None and not registered:
            evidence['recipient_choice']=selection
            self.store.put('recipient-choice:'+intent.intent_id.hex(),selection)
        ack=self.store.accept(intent.encode(),b"\x02"+bytes(self.read(ctx["registry"],b,"baseX")),
            self.c["bitcoin_network"],h["route"],evidence)
        reply=dict(version="1",request_id=record["request_id"],intent_id=ack["intent_id"],
            status="DURABLE",descriptor_hash=ack["descriptor_hash"],ready=False)
        self.stable(b)
        # The independent durable descriptor commit precedes ACK encryption/outbox.
        self._reply(h["route"],"ACK",0,reply,b,board,domain)
    def _reply(self,route,kind,seq,body,b,board,domain):
        key=message_key(route,kind,str(seq))
        with self.store.journal.db() as db:
            old=db.execute("SELECT body FROM address_packets WHERE id=?",(key,)).fetchone()
            if old:
                prior=json.loads(old[0])["body"]
                if kind=="ACK" and prior!=body:raise ValueError("ADDRESS_PACKET_CONFLICT")
                if kind=="STATUS":self.store._put(db,"status:"+route,dict(sequence=seq,digest=digest(prior)))
                return key
        r=self.read(board,b,"route",hex32(route));h,pk=self._header(board,b,route,kind,seq,r[0])
        packet=dict(header=h,body=body,ciphertext=self.seal_packet(domain,h,body,pk))
        return self._queue(packet,"status:"+route,dict(sequence=seq,digest=digest(body))) if kind=="STATUS" else self._queue(packet)
    def status(self,intent_id,bitcoin=None):
        from .address_mint import binding
        entry=next((row for row in self.store.intents() if row[0]==intent_id),None)
        if entry is None:raise ValueError("ADDRESS_WATCH_NOT_FOUND")
        b,_,ctx,_,_=self.context();r=ctx["registry"]
        expected=DepositIntentV1.decode(bytes.fromhex(entry[1]["intent"])).derive(b"\x02"+bytes(self.read(r,b,"baseX")))
        if entry[1]["descriptor"]!=expected:raise ValueError("ADDRESS_DESCRIPTOR_CONFLICT")
        record=self.read(r,b,"getBinding",hex32(intent_id))
        registration="REGISTRATION_REQUIRED"
        if bytes(record[0]):
            checked=binding(ctx,hex32(intent_id))
            if checked["intent"].encode().hex()!=entry[1]["intent"]:raise ValueError("ADDRESS_DESCRIPTOR_CONFLICT")
            registration="REGISTERED"
        watch="OBSERVATION_UNAVAILABLE"
        if bitcoin is not None:
            bitcoin.check_network()
            if bitcoin.call("getblockhash",0)!=self.scope["bitcoin_genesis"]:raise ValueError("BTC_ADDRESS_GENESIS_MISMATCH")
            best=bitcoin.call("getbestblockhash")
            coverage=self.store.coverage_at(intent_id,best)
            watch="RESCANNING"
            if coverage and bitcoin.call("getblockhash",int(coverage["tip"]))==best:
                watch="WATCH_COVERED"
        cg=self.read(r,b,"custodyGeneration");dg=self.read(r,b,"derivationGeneration")
        fee=self.fee_status(bitcoin)
        from .recipient_profiles import describe_intent
        try:
            profile=describe_intent(self.w3,ctx,DepositIntentV1.decode(bytes.fromhex(entry[1]['intent'])),b.number,
                selection=self.store.get('recipient-choice:'+intent_id) if registration!='REGISTERED' else None)
            profile['current_eligibility']=True
        except ValueError as exc:
            from .participant_errors import participant_exception_code
            code=participant_exception_code(exc)
            if code not in ('ADDRESS_RECIPIENT_PROFILE','RECIPIENT_PROFILE','RECIPIENT_CODE_CHANGED',
                'RECIPIENT_WALLET_PROFILE','RECIPIENT_DELEGATED_UNSUPPORTED','RECIPIENT_SELECTION_REQUIRED','RECIPIENT_ADVANCED_ACK','RECIPIENT_SINK'):
                raise
            profile=dict(mode='REVIEW_REQUIRED',reason=code,current_eligibility=False)
        if self.read(r,b,"custodyState")!=0 or self.read(r,b,"issuanceClosed"):state="ISSUANCE_SUSPENDED"
        elif self.read(r,b,"derivationState")!=0:state="DERIVATION_QUALIFICATION_REQUIRED"
        elif not profile['current_eligibility']:state='RECIPIENT_REVIEW_REQUIRED'
        elif registration!="REGISTERED":state=registration
        elif watch!="WATCH_COVERED":state=watch
        else:state=fee['status']  # Qualification/operational gates still keep Ready disabled.
        self.stable(b)
        return dict(status=state,intent_id=intent_id,registration=registration,watch=watch,
            custody_generation=str(cg),derivation_generation=str(dg),fee_reserve=fee,recipient_profile=profile,ready=False)
    def fee_status(self,bitcoin=None,*,full=False):
        if self.c.get('version')!='2':return dict(status='FEE_SERVICE_PROFILE_REQUIRED',ready=False)
        from .btc_fee_reserve import Inspector,load_config
        inspector=Inspector(load_config(self.c['fee_reserve_config']),self.w3,bitcoin)
        fc=inspector.context()
        if fc['registry'].address!=self.c['registry'] or fc['pool'].address!=self.c['pool']:raise ValueError('BTC_FEE_GRAPH_MISMATCH')
        return inspector.status(full=full)
    def publish_status(self,bitcoin=None):
        with self.store.exclusive():
            b,_,_,board,domain=self.context()
            if not self.read(board,b,"serviceAllowed",self.account.address):raise ValueError("ADDRESS_SERVICE_ROLE")
            with self.store.journal.db() as db:rows=db.execute("SELECT route,body FROM address_requests ORDER BY rowid").fetchall()
            cursor=self.store.get("status_cursor") or 0
            for i in range(min(len(rows),int(self.c["max_messages_per_poll"]))):
                route,encoded=rows[(cursor+i)%len(rows)];accepted=json.loads(encoded)
                r=self.read(board,b,"route",hex32(route))
                if r[0]=="0x"+"00"*20:continue  # Watch obligations survive orphaned transport.
                observation=self.status(accepted["intent_id"],bitcoin)
                body=dict(version="1",request_id=r[2].hex(),intent_id=accepted["intent_id"],
                    status=observation["status"],observation=observation,
                    descriptor_hash=accepted["receipt"]["descriptor_hash"],ready=False)
                marker=self.store.get("status:"+route) or dict(sequence=0,digest=None)
                if marker["digest"]==digest(body) or marker["sequence"]>=64:continue
                seq=marker["sequence"]+1
                self._reply(route,"STATUS",seq,body,b,board,domain)
            if rows:self.store.put("status_cursor",(cursor+int(self.c["max_messages_per_poll"]))%len(rows))
            return dict(status="ADDRESS_STATUS_QUEUED",ready=False)
    def replies(self):
        """Canonical delivery projections only; a service cannot assert Ready here."""
        b,_,ctx,board,_=self.context()
        with self.store.journal.db() as db:
            rows=db.execute("SELECT id,body FROM address_packets WHERE direction='IN' ORDER BY rowid").fetchall()
            sent=[json.loads(r[0]) for r in db.execute("SELECT body FROM address_packets WHERE direction='OUT'")]
        originals={p["header"]["route"]:p for p in sent if p["header"]["kind"]=="REQUEST"}
        projections={}
        for key,encoded in rows:
            p=json.loads(encoded);h=p["header"];body=p["body"];original=originals.get(h["route"])
            if h["kind"]=="REQUEST" or original is None or not isinstance(body,dict):continue
            projection=projections.setdefault(h["route"],dict(status="AWAITING_ACK",acknowledged=False,ready=False,sequence=0))
            request_packet=self.read(board,b,"packet",hex32(message_key(h["route"],"REQUEST","0")))
            if bytes(request_packet[8]).hex()!=original["ciphertext"]:
                projection.update(status="DELIVERY_REVALIDATION_REQUIRED",acknowledged=False);continue
            current=self.read(board,b,"packet",hex32(key))
            if not bytes(current[8]) or hashlib.sha256(bytes(current[8])).hexdigest()!=p["ciphertext_hash"]:
                projection.update(status="DELIVERY_REVALIDATION_REQUIRED",acknowledged=False);continue
            intent=DepositIntentV1.decode(bytes.fromhex(original["body"]["intent"]))
            descriptor=intent.derive(b"\x02"+bytes(self.read(ctx["registry"],b,"baseX")))
            address=crypto.call("bitcoin_script_address_v1",script=descriptor["script_pubkey"],network=self.c["bitcoin_network"])["address"]
            expected=digest(dict(intent=intent.encode().hex(),descriptor=descriptor,network=self.c["bitcoin_network"],address=address,start_height="0"))
            allowed={"DURABLE","REGISTRATION_REQUIRED","REGISTERED","RESCANNING","WATCH_COVERED",
                "OBSERVATION_UNAVAILABLE","ISSUANCE_SUSPENDED","DERIVATION_QUALIFICATION_REQUIRED","FEE_SERVICE_PROFILE_REQUIRED",
                "RECONCILIATION_REQUIRED","RECOVERY_REQUIRED","SETTLEMENT_REQUIRED","RESERVE_LOW","FEE_CAP_PROFILE_REQUIRED","SUFFICIENT_QUALIFICATION_REQUIRED","RECIPIENT_REVIEW_REQUIRED"}
            if (body.get("version")!="1" or body.get("request_id")!=original["request_id"]
                    or body.get("intent_id")!=intent.intent_id.hex() or body.get("descriptor_hash")!=expected
                    or body.get("ready") is not False or body.get("status") not in allowed):
                projection["status"]="INVALID_REPLY";continue
            if h["kind"]=="ACK":projection["acknowledged"]=body["status"]=="DURABLE"
            elif int(h["sequence"])>projection["sequence"]:
                projection.update(status=body["status"],sequence=int(h["sequence"]))
        self.stable(b);return dict(status="ADDRESS_DELIVERY_STATUS",routes=projections,ready=False)
    def transmit(self,*,message_id=None):
        if message_id is not None:hex32(message_id)
        with self.store.exclusive():
            b,_,_,board,_=self.context()
            with self.store.journal.db() as db:
                rows=db.execute("SELECT id,body FROM address_packets WHERE direction='OUT'"+
                    (' AND id=?' if message_id is not None else '')+' ORDER BY rowid',
                    (message_id,) if message_id is not None else ()).fetchall()
            if message_id is not None and not rows:raise ValueError('ADDRESS_OUTGOING_REQUIRED')
            for key,encoded in rows:
                p=json.loads(encoded);h=p["header"];old=self.read(board,b,"packet",hex32(key))
                if bytes(old[8]):
                    if (bytes(old[8]).hex()!=p["ciphertext"] or old[3]!=h["sender"] or old[4]!=h["recipient"]
                            or old[5]!=int(h["sender_epoch"]) or old[6]!=int(h["recipient_epoch"]) or old[7]!=int(h["expires_at"])):
                        raise ValueError("ADDRESS_PACKET_CONFLICT")
                    self.stable(b)
                    with self.store.journal.db() as db:db.execute("UPDATE address_packets SET state='CONFIRMED' WHERE id=?",(key,))
                    continue
                count=self.store.get("attempt:"+key) or 0
                if count==0 and int(h["expires_at"])<=b.timestamp:
                    return dict(status="ADDRESS_DELIVERY_EXPIRED",message_id=key,ready=False)
                if count>=int(self.c["max_publish_attempts"]):continue
                if h["kind"]=="REQUEST":
                    method="request";args=["0x"+p["request_id"],h["recipient"],h["sender_epoch"],h["recipient_epoch"],h["expires_at"],"0x"+p["ciphertext"]]
                else:
                    method="reply";args=["0x"+h["route"],str(KINDS[h["kind"]]),h["sequence"],h["sender_epoch"],h["recipient_epoch"],h["expires_at"],"0x"+p["ciphertext"]]
                self.store.put("attempt:"+key,count+1)
                return self.send(key,method,args)
            return dict(status="IDLE",ready=False)

    def reserve_cost(self,key,tx):
        cost=tx['gas']*tx.get('maxFeePerGas',tx.get('gasPrice'))
        if tx['gas']>int(self.c['max_tx_gas']) or cost>int(self.c['max_transaction_cost_wei']):raise ValueError('ADDRESS_GAS_BUDGET')
        with self.costs.db() as db:
            db.execute('BEGIN IMMEDIATE')
            costs={r[0]:int(r[1]) for r in db.execute('SELECT id,cost FROM address_costs WHERE scope=?',(self.cost_scope,))}
            wanted=max(costs.get(key,0),cost)
            if (len(costs)>=4096 and key not in costs or sum(v for k,v in costs.items() if k!=key)+wanted>int(self.c['max_total_cost_wei'])):
                raise ValueError('ADDRESS_GAS_BUDGET')
            db.execute('INSERT INTO address_costs VALUES(?,?,?) ON CONFLICT(scope,id) DO UPDATE SET cost=excluded.cost',
                (self.cost_scope,key,str(wanted)))
    def send(self,key,method,args,*,target=None,kind="BitcoinAddressMessageBoard",receipt=False):
        client=self
        class Sender(Bootstrap):
            def build_transaction(self,step,account,nonce):
                tx=super().build_transaction(step,account,nonce)
                client.reserve_cost(key,tx)
                return tx
        c=self.c
        profile=dict(deployment_id="address-message-"+digest([self.scope,self.account.address,key]),
            chain_id=c["chain_id"],evm_confirmations=c["evm_confirmations"],evm_rpc=c["evm_rpc"],
            mode="backed" if c["chain_id"]=="1" or c["bitcoin_network"]!="regtest" else "integration",bitcoin_network=c["bitcoin_network"],
            contracts={"board":dict(address=target or c["board"],artifact=kind)},
            steps=[dict(id="send",kind="call",contract="board",method=method,args=args,value="0")])
        if c["bitcoin_policy"] is not None:profile["bitcoin_policy"]=c["bitcoin_policy"]
        controller=getattr(self,'transaction_controller',None)
        if controller is not None:return controller.submit_address(self,key,profile,receipt=receipt)
        runner=Sender(profile,str(Path(c["identity_file"]).resolve())+".transactions.sqlite")
        result=runner.apply_confirmed(c["identity_file"])
        if receipt:
            if result["status"]!="VERIFIED":raise ValueError("ADDRESS_TRANSACTION_UNCONFIRMED")
            saved=runner.journal.get(profile["deployment_id"]+":send")["result"]
            verified=runner.verify_receipt(saved)
            if verified is None:raise ValueError("ADDRESS_TRANSACTION_UNCONFIRMED")
            return verified
        return dict(status=result["status"],message_id=key,ready=False)

    def recover_binding(self,intent_id):
        from .address_mint import binding
        with self.store.exclusive():
            b,_,ctx,_,_=self.context()
            checked=binding(ctx,hex32(intent_id));self.stable(b)
            from .bitcoin_continuity import remember_binding
            remember_binding(self,ctx,hex32(intent_id),b.number)
            # Canonical public registry bytes, not an old server's coverage flag.
            result=self.store.accept(checked["intent"].encode(),b"\x02"+bytes(self.read(ctx["registry"],b,"baseX")),
                self.c["bitcoin_network"],"onchain:"+intent_id,dict(validation_provenance=checked["provenance"].hex()))
            return dict(result,status="ADDRESS_DESCRIPTOR_RECOVERED",ready=False)
    def recover_range(self,first,last):
        """Events locate public records; fresh canonical binding validation authenticates them."""
        b,pool,ctx,_,_=self.context()
        if type(first) is not int or type(last) is not int or not 0<=first<=last<=b.number or last-first>=64:
            raise ValueError("ADDRESS_RECOVERY_RANGE")
        logs=ctx["registry"].events.AddressRegistered().get_logs(from_block=first,to_block=last,
            argument_filters={"pool":pool.address})
        if len(logs)>int(self.c["max_intents"]):raise ValueError("ADDRESS_RECOVERY_RANGE")
        result=[]
        for log in logs:result.append(self.recover_binding(bytes(log["args"]["intentId"]).hex()))
        self.stable(b)
        return dict(status="ADDRESS_REGISTRY_RECOVERED",count=len(result),ready=False)


def main(argv=None):
    from .dkg import atomic_private
    from .cli import _protect_transport_logs
    _protect_transport_logs()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action",choices=("keygen","register-key","prepare","recipient-preview","wallet-create","wallet-transfer","wallet-approve","wallet-withdraw","request","poll","flush","serve-once","scan","status","replies","recover-binding","recover-range","backup",
        "registration-prepare","registration-propose","registration-execute","registration-status","registration-retry",
        "registration-adopt","registration-export","registration-import","spv-sync"))
    p.add_argument("--config");p.add_argument("--key-file");p.add_argument("--epoch")
    p.add_argument("--launch-config",type=Path,help="use the Bitcoin-watch start height for scan")
    p.add_argument("--recipient");p.add_argument("--request-nonce");p.add_argument("--intent-file")
    p.add_argument("--intent-id");p.add_argument("--service");p.add_argument("--delivery",default="0")
    p.add_argument("--output")
    p.add_argument('--recipient-mode',choices=('ordinary','wallet-v1','advanced-contract'))
    p.add_argument('--recipient-file');p.add_argument('--recipient-accept')
    p.add_argument('--wallet');p.add_argument('--wallet-owner');p.add_argument('--wallet-salt');p.add_argument('--amount-units');p.add_argument('--spender')
    p.add_argument('--bitcoin-destination');p.add_argument('--withdrawal-id')
    p.add_argument("--registration-board");p.add_argument("--registration-batch")
    p.add_argument("--registration-intent-id",action="append");p.add_argument("--deadline");p.add_argument("--session")
    p.add_argument("--batch-file")
    p.add_argument('--zk-worker-config');p.add_argument('--zk-transport-config')
    p.add_argument('--zk-request-federation');p.add_argument('--zk-service-recipient')
    p.add_argument('--epoch-service-config',help='Private enrolled old-epoch retention policy')
    p.add_argument("--from-block",type=int);p.add_argument("--to-block",type=int)
    a=p.parse_args(argv)
    if a.launch_config is not None and a.action!="scan":
        p.error("--launch-config requires scan")
    if a.action=="keygen":
        if not a.key_file or Path(a.key_file).exists():raise ValueError("ADDRESS_NEW_KEY_FILE_REQUIRED")
        uint(a.epoch,1);key=crypto.call("transport_key")
        atomic_private(a.key_file,(canonical(dict(epoch=a.epoch,public=key["public"],secret=key["secret"]))+"\n").encode())
        result=dict(status="ADDRESS_TRANSPORT_KEY_SAVED",public=key["public"],epoch=a.epoch)
    else:
        if not a.config:raise ValueError("ADDRESS_CONFIG")
        config=load_config(a.config)
        from .launch_configurator import watch_start_height
        start_height=watch_start_height(a.launch_config,config)
        client=Client(config)
        if a.epoch_service_config and a.action in ('prepare','request'):
            from .epoch_service import issuance_guard
            issuance_guard(client,a.epoch_service_config)
        if a.action=='spv-sync':result=dict(status='ADDRESS_SPV_REQUESTED',ready=False)
        elif a.action.startswith("registration-"):
            from .address_registration_client import Driver
            driver=Driver(client,a.registration_board)
            action=a.action.removeprefix("registration-")
            if action in ("prepare","import"):
                if a.deadline is None:raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
                exported=None
                if action=="import":
                    if not a.batch_file or Path(a.batch_file).stat().st_size>32768:
                        raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
                    exported=json.loads(read_private(a.batch_file))
                result=driver.prepare(a.registration_intent_id,a.deadline,exported=exported)
            elif action=="adopt":result=driver.adopt(a.session)
            elif action=="export":
                if not a.output or Path(a.output).exists():raise ValueError("OUTPUT_EXISTS")
                data=driver.export(a.registration_batch)
                atomic_private(a.output,(canonical(data)+"\n").encode())
                result=dict(status="REGISTRATION_BATCH_EXPORTED",batch_id=a.registration_batch,ready=False)
            elif action=="retry":result=driver.retry(a.registration_batch,a.deadline)
            else:result=getattr(driver,action)(a.registration_batch)
        elif a.action=="register-key":
            epoch=uint(a.epoch,1)
            if epoch not in client.keys:raise ValueError("ADDRESS_LOCAL_KEY_REQUIRED")
            public=client.keys[epoch]["public"]
            result=client.send("key-"+str(epoch)+"-"+public,"registerKey",[str(epoch),"0x"+public])
        elif a.action=='recipient-preview':
            from .recipient_profiles import address_preview
            if not a.output or Path(a.output).exists():raise ValueError('OUTPUT_EXISTS')
            result=address_preview(client,a.recipient,a.recipient_mode or 'ordinary')
            atomic_private(a.output,(canonical(result)+'\n').encode())
        elif a.action.startswith('wallet-'):
            from .recipient_wallet_client import action
            result=action(client,a)
        elif a.action=="prepare":
            if not a.intent_file or Path(a.intent_file).exists():raise ValueError("ADDRESS_NEW_INTENT_FILE_REQUIRED")
            choice=None
            if a.recipient_file:
                choice=json.loads(read_private(a.recipient_file))['selection']
            elif a.recipient_mode not in (None,'ordinary'):raise ValueError('RECIPIENT_CONFIRMATION_REQUIRED')
            intent=client.make_intent(a.recipient,a.request_nonce,selection=choice,accepted=a.recipient_accept)
            atomic_private(a.intent_file,(canonical(dict(intent=intent.encode().hex()))+"\n").encode())
            result=dict(status="ADDRESS_INTENT_DRAFTED",intent_id=intent.intent_id.hex(),ready=False,
                chain_id=str(intent.evm_chain_id),bitcoin_network=client.c["bitcoin_network"],
                pool=Web3.to_checksum_address(intent.pool),mp_token=Web3.to_checksum_address(intent.mp_token),
                requester=Web3.to_checksum_address(intent.requester),recipient=Web3.to_checksum_address(intent.recipient))
            from .recipient_profiles import describe_intent
            b,_,ctx,_,_=client.context()
            result['recipient_profile']=describe_intent(client.w3,ctx,intent,b.number,selection=choice)
        elif a.action=="request":
            data=json.loads(read_private(a.intent_file))
            if set(data)!={"intent"}:raise ValueError("ADDRESS_REQUEST_SCHEMA")
            if a.recipient_file:
                from .recipient_profiles import authorize_address
                choice=json.loads(read_private(a.recipient_file))['selection']
                authorize_address(client,choice,a.recipient_accept)
                decoded=DepositIntentV1.decode(bytes.fromhex(data['intent']));b,_,ctx,_,_=client.context()
                client.validate_intent(decoded,ctx,b,selection=choice)
                client.store.put('recipient-choice:'+decoded.intent_id.hex(),choice)
            result=client.request(bytes.fromhex(data["intent"]),a.service,a.delivery)
        elif a.action=="poll":result=dict(status="ADDRESS_MESSAGES_SAVED",count=client.poll(),ready=False)
        elif a.action=="flush":result=client.transmit()
        elif a.action=="serve-once":result=client.service_once()
        elif a.action=="replies":result=client.replies()
        elif a.action=="recover-binding":result=client.recover_binding(a.intent_id)
        elif a.action=="recover-range":result=client.recover_range(a.from_block,a.to_block)
        elif a.action=="backup":result=client.store.backup(a.output)
        else:
            c=client.c
            if not c["bitcoin_rpc"] or not c["bitcoin_cookie"]:raise ValueError("BITCOIN_RPC_REQUIRED")
            from .bitcoin_rpc import BitcoinRPC
            bitcoin=BitcoinRPC(c["bitcoin_rpc"],c["bitcoin_cookie"],c["bitcoin_network"],c["bitcoin_policy"])
            if a.action=="scan":
                result=client.store.scan(bitcoin,max_blocks=int(c["max_blocks_per_poll"]),max_reorg=int(c["max_reorg"]),
                    start_height=start_height,allow_remote_filters=trusted_remote_filters(c,bitcoin))
                if c.get('version')=='2':result=dict(result,fee_reserve=client.fee_status(bitcoin,full=True))
                client.publish_status(bitcoin)
            else:result=client.status(a.intent_id,bitcoin)
        if a.action=='serve-once' and client.c.get('version')=='2':
            result=dict(result,fee_reserve=client.fee_status(full=True))
        if a.action=='spv-sync' or (a.action in ('scan','serve-once') and (a.zk_worker_config or a.zk_transport_config)):
            from .address_spv_watch import sync
            result=dict(result,spv=sync(client,worker_config=a.zk_worker_config,transport_config=a.zk_transport_config,
                federation=a.zk_request_federation,recipient=a.zk_service_recipient))
    print(json.dumps(result))


if __name__=="__main__":
    try:main()
    except Exception as exc:
        from .participant_errors import participant_exception_code
        print(json.dumps(dict(status="ERROR",error=participant_exception_code(exc))))
        raise SystemExit(1) from None
