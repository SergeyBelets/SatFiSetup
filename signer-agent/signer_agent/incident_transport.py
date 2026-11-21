"""Private incident evidence in a dedicated on-chain inbox. Delivery is not authority."""
import hashlib
import json
import secrets
from pathlib import Path
from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
from . import crypto
from .address_transport import Client as AddressClient,validate_config
from .bootstrap import artifact
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .evm_network import read_block
from .onboarding import read_private
from .operations import canonical
from .spv_transport import hex32,uint,route_key,message_key

ZERO='0x'+'00'*20
PUBLIC=('version','chain_id','registry','graph_id','scope','scope_id','reporter','incident_id','reason',
        'evidence_hash','bitcoin_block','bitcoin_tx','evm_anchor')


def load_config(path):
    c=json.loads(read_private(path));recovery=c.pop('recovery_service_config',None)
    validate_config(c)
    if recovery is not None and (not isinstance(recovery,str) or not Path(recovery).is_absolute()):raise ValueError('INCIDENT_CONFIG')
    return dict(c,recovery_service_config=recovery)


def hpke_context(domain,header):
    raw=canonical(['SATOHSI_INCIDENT_HPKE_V1',domain,header]).encode()
    return hashlib.sha256(b'INCIDENT-INFO\0'+raw).hexdigest(),raw.hex()


def public_report(report):return {k:report[k] for k in PUBLIC}


def evidence_hash(report):
    data={k:v for k,v in report.items() if k!='evidence_hash'}
    return keccak(b'SATOHSI_INCIDENT_EVIDENCE_V1\0'+canonical(data).encode()).hex()


def report_id(report):return keccak(b'SATOHSI_INCIDENT_REPORT_V1\0'+canonical(public_report(report)).encode()).hex()


def delivery_id(identifier,sender,recipient,attempt='0'):
    n=uint(attempt,0,31)
    return identifier if n==0 else keccak(encode(['bytes32','bytes32','address','address','uint32'],[
        keccak(text='SATOHSI_INCIDENT_DELIVERY_V1'),hex32(identifier),sender,recipient,n])).hex()


def validate_report(report,w3,ctx,number,*,private=True):
    try:return _validate_report(report,w3,ctx,number,private=private)
    except (KeyError,TypeError,AttributeError):raise ValueError('INCIDENT_REPORT') from None
    except ValueError as exc:
        if str(exc) in ('SPV_BYTES32','SPV_INTEGER'):raise ValueError('INCIDENT_REPORT') from None
        raise


def _validate_report(report,w3,ctx,number,*,private=True):
    expected=set(PUBLIC)|({'salt','details'} if private else set())
    if not isinstance(report,dict) or set(report)!=expected or report['version']!='1':raise ValueError('INCIDENT_REPORT')
    r=ctx['registry'];read=lambda m,*a:getattr(r.functions,m)(*a).call(block_identifier=number)
    scope=uint(report['scope'],0,1)
    if (report['chain_id']!=str(w3.eth.chain_id) or report['registry']!=r.address
            or report['graph_id']!=bytes(read('graphId')).hex()
            or report['scope_id']!=bytes(read('custodyScope' if scope==0 else 'derivationScope')).hex()
            or not Web3.is_checksum_address(report['reporter']) or report['reporter']==ZERO):raise ValueError('INCIDENT_REPORT_SCOPE')
    for name in ('graph_id','scope_id','incident_id','reason','evidence_hash','bitcoin_block','bitcoin_tx','evm_anchor'):hex32(report[name])
    if any(report[n]=='00'*32 for n in ('incident_id','reason','evidence_hash')):raise ValueError('INCIDENT_REPORT')
    if private:
        hex32(report['salt']);d=report['details']
        if (not isinstance(d,dict) or set(d)!={'summary','references'} or not isinstance(d['summary'],str)
                or not 1<=len(d['summary'].encode())<=1024 or not isinstance(d['references'],list) or len(d['references'])>16):
            raise ValueError('INCIDENT_REPORT')
        for h in d['references']:hex32(h)
        if len(canonical(report).encode())>3300 or evidence_hash(report)!=report['evidence_hash']:raise ValueError('INCIDENT_EVIDENCE_MISMATCH')
    return report_id(report)


def delivery_proof(w3,ctx,board_address,report,message_id,number,*,attempt='0'):
    """Authenticate the reporter and exact public evidence commitment, not its truth."""
    checked_runtime(w3,board_address,'BitcoinIncidentMessageBoard',number)
    prove_creation(w3,board_address,'BitcoinIncidentMessageBoard',[ctx['registry'].address],number)
    board=w3.eth.contract(address=board_address,abi=artifact('BitcoinIncidentMessageBoard')['abi'])
    read=lambda m,*a:getattr(board.functions,m)(*a).call(block_identifier=number)
    identifier=validate_report(report,w3,ctx,number,private=False)
    packet=read('packet',hex32(message_id));route=read('route',bytes(packet[0]))
    if (read('registry')!=ctx['registry'].address or not bytes(packet[8]) or int(packet[1])!=0 or int(packet[2])!=0
            or packet[3]!=report['reporter'] or route[0]!=report['reporter'] or route[1]!=packet[4]
            or not read('serviceAllowed',route[1]) or bytes(route[2]).hex()!=delivery_id(identifier,route[0],route[1],attempt)
            or route_key(bytes(route[2]).hex(),route[0],route[1])!=bytes(packet[0]).hex()
            or message_key(bytes(packet[0]).hex(),'REQUEST','0')!=message_id):raise ValueError('INCIDENT_DELIVERY_REQUIRED')
    return board


class Client(AddressClient):
    def context(self):
        from .address_mint import configuration
        c=self.c
        if str(self.w3.eth.chain_id)!=c['chain_id']:raise ValueError('CHAIN_MISMATCH')
        b=read_block(self.w3,int(c['evm_confirmations']))
        pool=self.w3.eth.contract(address=c['pool'],abi=artifact('MiningPoolV1')['abi'])
        # Status restriction must not depend on a working derivation verifier.
        ctx=configuration(self.w3,pool,b.number,verify_derivation=False);r=ctx['registry']
        if r.address!=c['registry']:raise ValueError('INCIDENT_REPORT_SCOPE')
        checked_runtime(self.w3,c['board'],'BitcoinIncidentMessageBoard',b.number)
        prove_creation(self.w3,c['board'],'BitcoinIncidentMessageBoard',[r.address],b.number)
        board=self.w3.eth.contract(address=c['board'],abi=artifact('BitcoinIncidentMessageBoard')['abi'])
        auth=self.read(pool,b,'authorization')
        domain=keccak(encode(['bytes32','uint256','address','address','bytes32','address'],[
            keccak(text='SATOHSI_INCIDENT_MESSAGES_V1'),self.w3.eth.chain_id,board.address,r.address,self.read(r,b,'graphId'),auth])).hex()
        if (self.read(board,b,'registry')!=r.address or self.read(board,b,'authorization')!=auth
                or bytes(self.read(board,b,'incidentDomain')).hex()!=domain):raise ValueError('INCIDENT_BOARD')
        if c['bitcoin_policy'] is not None and bytes(self.read(r,b,'bitcoinGenesis')).hex()!=c['bitcoin_policy']['genesis_hash']:
            raise ValueError('BTC_ADDRESS_GENESIS_MISMATCH')
        self.stable(b)
        from .bitcoin_continuity import Catalog,sample
        Catalog(identity_file=c['identity_file']).observe(self.w3,sample(self.w3,ctx,b.number))
        return b,pool,ctx,board,domain
    def send(self,key,method,args,*,target=None,kind='BitcoinIncidentMessageBoard',receipt=False):
        return super().send(key,method,args,target=target,kind=kind,receipt=receipt)
    def seal_packet(self,domain,header,body,public):
        plain=canonical(body).encode()
        if len(plain)>3900:raise ValueError('INCIDENT_MESSAGE_SIZE')
        info,aad=hpke_context(domain,header)
        r=crypto.call('seal',public=public,info=info,aad=aad,plaintext=plain.hex())
        return r['enc']+r['ciphertext']
    def open_message(self,domain,header,ciphertext,secret):
        if not 48<len(bytes.fromhex(ciphertext))<=4096:raise ValueError('INCIDENT_MESSAGE_SIZE')
        info,aad=hpke_context(domain,header)
        r=crypto.call('open',secret=secret,info=info,aad=aad,enc=ciphertext[:64],ciphertext=ciphertext[64:])
        raw=bytes.fromhex(r['plaintext']);body=json.loads(raw)
        if not isinstance(body,dict) or len(raw)>3900 or canonical(body).encode()!=raw:raise ValueError('INCIDENT_REPORT')
        return body
    def _save_report(self,report,ctx,number):
        identifier=validate_report(report,self.w3,ctx,number)
        self.store.space(len(canonical(report))*3+8192)
        with self.store.journal.db() as db:
            old=self.store._get(db,'incident-report:'+identifier)
            if old is not None and old!=report:raise ValueError('INCIDENT_EVIDENCE_MISMATCH')
            if old is None and db.execute("SELECT COUNT(*) FROM address_meta WHERE id LIKE 'incident-report:%'").fetchone()[0]>=int(self.c['max_intents']):
                raise ValueError('INCIDENT_HISTORY_LIMIT')
            self.store._put(db,'incident-report:'+identifier,report)
        return identifier
    def prepare_report(self,scope,reason,details,*,incident_id=None,bitcoin_block=None,bitcoin_tx=None,evm_anchor=None):
        with self.store.exclusive():
            b,_,ctx,_,_=self.context();scope=str(uint(scope,0,1));hex32(reason)
            r=dict(version='1',chain_id=self.c['chain_id'],registry=ctx['registry'].address,
                graph_id=bytes(self.read(ctx['registry'],b,'graphId')).hex(),scope=scope,
                scope_id=bytes(self.read(ctx['registry'],b,'custodyScope' if scope=='0' else 'derivationScope')).hex(),
                reporter=self.account.address,incident_id=incident_id or secrets.token_hex(32),reason=reason,
                bitcoin_block=bitcoin_block or '00'*32,bitcoin_tx=bitcoin_tx or '00'*32,
                evm_anchor=evm_anchor or b.hash.hex(),salt=secrets.token_hex(32),details=details)
            r['evidence_hash']=evidence_hash(r);self.stable(b);self._save_report(r,ctx,b.number)
            return r
    def report(self,identifier):
        hex32(identifier);r=self.store.get('incident-report:'+identifier)
        if r is None:raise ValueError('INCIDENT_PRIVATE_REPORT_REQUIRED')
        return r
    def send_report(self,report,recipient,attempt='0'):
        with self.store.exclusive():
            b,_,ctx,board,domain=self.context();identifier=self._save_report(report,ctx,b.number)
            origin=None
            if report['reporter']!=self.account.address:
                if not self.read(board,b,'serviceAllowed',self.account.address):raise ValueError('INCIDENT_REPORTER')
                origin=self.proof(identifier,ctx,b.number)
            if not self.read(board,b,'serviceAllowed',recipient) or recipient==self.account.address:raise ValueError('INCIDENT_RECIPIENT')
            request_id=delivery_id(identifier,self.account.address,recipient,attempt)
            route=route_key(request_id,self.account.address,recipient);key=message_key(route,'REQUEST','0')
            old=self.store.get('incident-delivery:'+route)
            if old:return old
            h,pk=self._header(board,b,route,'REQUEST',0,recipient)
            body=dict(version='1',type='INCIDENT_REPORT',domain=domain,report=report)
            if attempt!='0':body['attempt']=attempt
            if origin is not None:body.update(type='INCIDENT_FORWARD',origin=origin)
            result=dict(status='INCIDENT_REPORT_QUEUED',report_id=identifier,message_id=key,route=route,ready=False)
            if attempt!='0':result['attempt']=attempt
            if origin is not None:result['origin']=origin
            self.stable(b);self._queue(dict(header=h,body=body,ciphertext=self.seal_packet(domain,h,body,pk),request_id=request_id),
                'incident-delivery:'+route,result)
            return result
    def receive_once(self,*,message_id=None):
        if message_id is not None:hex32(message_id)
        self.poll()
        with self.store.exclusive():
            b,_,ctx,board,domain=self.context()
            with self.store.journal.db() as db:
                rows=db.execute("SELECT id,body FROM address_packets WHERE direction='IN' AND state='RECEIVED'"+
                    (' AND id=?' if message_id is not None else '')+' ORDER BY rowid LIMIT ?',
                    ((message_id,) if message_id is not None else ())+(int(self.c['max_messages_per_poll']),)).fetchall()
            for key,encoded in rows:
                p=json.loads(encoded);h=p['header'];body=p['body']
                try:
                    current=self.read(board,b,'packet',hex32(key))
                    if not bytes(current[8]) or hashlib.sha256(bytes(current[8])).hexdigest()!=p['ciphertext_hash']:raise ValueError('INCIDENT_DELIVERY_REQUIRED')
                    if h['kind']=='REQUEST':
                        forwarded=body.get('type')=='INCIDENT_FORWARD'
                        if set(body)!=({'version','type','domain','report'}|({'origin'} if forwarded else set())|({'attempt'} if 'attempt' in body else set())) or body['version']!='1' or body['type'] not in ('INCIDENT_REPORT','INCIDENT_FORWARD') or body['domain']!=domain:
                            raise ValueError('INCIDENT_REPORT')
                        r=body['report'];identifier=validate_report(r,self.w3,ctx,b.number)
                        attempt=body.get('attempt','0')
                        if p['request_id']!=delivery_id(identifier,h['sender'],h['recipient'],attempt):raise ValueError('INCIDENT_REPORT')
                        proof=dict(message_id=key)
                        if attempt!='0':proof['attempt']=attempt
                        if forwarded:
                            if (not isinstance(body['origin'],dict) or set(body['origin']) not in ({'message_id'},{'message_id','attempt'})
                                    or not self.read(board,b,'serviceAllowed',h['sender'])):raise ValueError('INCIDENT_REPORT')
                            proof=body['origin']
                        delivery_proof(self.w3,ctx,board.address,public_report(r),proof['message_id'],b.number,attempt=proof.get('attempt','0'))
                        self._save_report(r,ctx,b.number)
                        # Evidence is durable before ACK; no status transition is inferred.
                        self.store.put('incident-proof:'+identifier,proof)
                        self._reply(h['route'],'ACK',0,dict(version='1',type='INCIDENT_ACK',report_id=identifier,evidence_hash=r['evidence_hash'],ready=False),b,board,domain)
                    else:
                        original=self.store.get('incident-delivery:'+h['route'])
                        if original is None:raise ValueError('INCIDENT_REPORT')
                        identifier=original['report_id'];r=self.report(identifier)
                        if (original is None or body.get('version')!='1' or body.get('report_id')!=identifier
                                or body.get('evidence_hash')!=r['evidence_hash'] or body.get('ready') is not False
                                or body.get('type')!=('INCIDENT_ACK' if h['kind']=='ACK' else 'INCIDENT_STATUS')):raise ValueError('INCIDENT_REPORT')
                        with self.store.journal.db() as db:
                            sent=db.execute("SELECT body FROM address_packets WHERE id=? AND direction='OUT'",(original['message_id'],)).fetchone()
                        prior=self.read(board,b,'packet',hex32(original['message_id']))
                        if not sent or bytes(prior[8]).hex()!=json.loads(sent[0])['ciphertext']:raise ValueError('INCIDENT_DELIVERY_REQUIRED')
                        origin=original.get('origin',dict(message_id=original['message_id']))
                        delivery_proof(self.w3,ctx,board.address,public_report(r),origin['message_id'],b.number,attempt=origin.get('attempt','0'))
                        self.store.put('incident-reply:'+key,dict(report_id=identifier,type=body['type'],ready=False))
                except (ValueError,RuntimeError,KeyError,TypeError) as exc:
                    from .participant_errors import participant_exception_code
                    code=participant_exception_code(exc)
                    if code not in ('INCIDENT_REPORT','INCIDENT_REPORT_SCOPE','INCIDENT_EVIDENCE_MISMATCH','INCIDENT_DELIVERY_REQUIRED','INCIDENT_PRIVATE_REPORT_REQUIRED','CRYPTO_VALIDATION_FAILED','SPV_INTEGER'):
                        raise
                    self.store.put('incident-rejected:'+key,dict(reason=code))
                    with self.store.journal.db() as db:db.execute("UPDATE address_packets SET state='QUARANTINED' WHERE id=?",(key,))
                    continue
                self.stable(b)
                with self.store.journal.db() as db:db.execute("UPDATE address_packets SET state='HANDLED' WHERE id=?",(key,))
            return dict(status='INCIDENT_MESSAGES_SAVED',ready=False)
    def proof(self,identifier,ctx,number):
        report=public_report(self.report(identifier));saved=self.store.get('incident-proof:'+identifier)
        candidates=[saved] if saved else []
        with self.store.journal.db() as db:
            for row, in db.execute("SELECT body FROM address_meta WHERE id LIKE 'incident-delivery:%' AND json_extract(body,'$.report_id')=?",(identifier,)):
                result=json.loads(row)
                candidates.append(result.get('origin',dict(message_id=result['message_id'],**({'attempt':result['attempt']} if 'attempt' in result else {}))))
        for p in candidates:
            try:delivery_proof(self.w3,ctx,self.c['board'],report,p['message_id'],number,attempt=p.get('attempt','0'))
            except ValueError as exc:
                if str(exc)=='INCIDENT_DELIVERY_REQUIRED':continue
                raise
            return p
        raise ValueError('INCIDENT_DELIVERY_REQUIRED')
    def notify(self,identifier,*,route=None):
        if route is not None:hex32(route)
        selected_route=route
        with self.store.exclusive():
            b,_,ctx,board,domain=self.context();r=self.report(identifier)
            with self.store.journal.db() as db:
                rows=db.execute("SELECT body FROM address_packets WHERE direction='IN' AND state='HANDLED' AND json_extract(body,'$.header.kind')='REQUEST'").fetchall()
            count=0
            for encoded, in rows:
                p=json.loads(encoded)
                if selected_route is not None and p['header']['route']!=selected_route:continue
                if report_id(p['body']['report'])!=identifier:continue
                route=p['header']['route'];marker=self.store.get('status:'+route) or dict(sequence=0,digest=None)
                from .address_status import snapshot
                body=dict(version='1',type='INCIDENT_STATUS',report_id=identifier,evidence_hash=r['evidence_hash'],
                    custody=snapshot(ctx,b.number,0),derivation=snapshot(ctx,b.number,1),ready=False)
                from .address_watch import digest
                if marker['sequence']>=64 or marker['digest']==digest(body):continue
                self._reply(route,'STATUS',marker['sequence']+1,body,b,board,domain);count+=1
            self.stable(b);return dict(status='INCIDENT_STATUS_QUEUED',count=count,ready=False)
