"""Bounded registration workflow over the existing DKG board and EVM_AUTH quorum."""
import json
from web3.logs import DISCARD

from . import address_registration as policy
from .bootstrap import Bootstrap
from .deposit_intent import DepositIntentV1
from .operations import canonical
from .spv_transport import hex32,uint
from .onchain_transport import can_publish_intent


class Driver:
    def __init__(self,client,board_address):
        self.client=client;self.board_address=board_address
        if board_address is None:raise ValueError("ADDRESS_REGISTRATION_BOARD_REQUIRED")
        self.context()
    def context(self):
        c=self.client;b,_,ctx,_,_=c.context()
        a=policy.authority(c.w3,ctx,self.board_address,b.number)
        defaults=dict(bitcoin_network="regtest",evm_confirmations="1")
        selected=dict(bitcoin_network=c.c["bitcoin_network"],evm_confirmations=c.c["evm_confirmations"])
        if c.c["bitcoin_policy"] is not None:selected["bitcoin_policy"]=c.c["bitcoin_policy"]
        if {**defaults,**a["options"]}!={**defaults,**selected}:raise ValueError("ADDRESS_REGISTRATION_AUTHORITY")
        c.stable(b);return b,ctx,a
    def _key(self,identifier):
        hex32(identifier);return "registration:"+identifier
    def _get(self,identifier):
        record=self.client.store.get(self._key(identifier))
        if record is None:raise ValueError("ADDRESS_REGISTRATION_BATCH_REQUIRED")
        if (record["registry"]!=self.client.c["registry"] or record["pool"]!=self.client.c["pool"]
                or record["registration_board"]!=self.board_address):raise ValueError("ADDRESS_REGISTRATION_AUTHORITY")
        raws=[bytes.fromhex(x) for x in record["intents"]]
        if policy.batch_id(record["registry"],raws)!=identifier:raise ValueError("ADDRESS_REGISTRATION_BINDING_CONFLICT")
        return record,raws
    def _proof(self,intent_id,raw,ctx,number):
        c=self.client
        with c.store.journal.db() as db:
            rows=db.execute("SELECT body FROM address_requests WHERE json_extract(body,'$.intent_id')=? ORDER BY rowid DESC LIMIT 8",(intent_id,)).fetchall()
        for encoded, in rows:
            evidence=json.loads(encoded)["evidence"]
            if "opening" not in evidence or "message_id" not in evidence:continue
            proof=dict(board=evidence.get("board",c.c["board"]),message_id=evidence["message_id"],opening=evidence["opening"])
            if 'recipient_choice' in evidence:proof['recipient_choice']=evidence['recipient_choice']
            try:policy.request_proof(c.w3,ctx,raw,proof,number)
            except ValueError as exc:
                if str(exc)=="ADDRESS_REQUEST_ATTESTATION_REQUIRED":continue
                raise
            return proof
        raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
    def _attempt(self,ctx,deadline,number,ordinal=1):
        r=ctx["registry"];read=lambda n:policy.read(r,number,n)
        limit=uint(str(deadline),1)
        if limit>=2**64:raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
        return dict(custody=str(read("custodyGeneration")),derivation=str(read("derivationGeneration")),
            deadline=str(limit),sequence=str(read("registrationNonce")),ordinal=str(ordinal),session=None,intent=None,anchor=None)
    def _validate_pending(self,record,raws,ctx,number):
        from .bitcoin_continuity import client_guard
        client_guard(self.client,'ISSUE',ctx=ctx,number=number)
        a=record["attempts"][-1]
        policy.pending(self.client.w3,ctx,raws,record["requests"],int(a["custody"]),int(a["derivation"]),
            int(a["deadline"]),int(a["sequence"]),number)
    def prepare(self,identifiers,deadline,*,exported=None):
        c=self.client
        with c.store.exclusive():
            b,ctx,_=self.context()
            rows={key:immutable for key,immutable,_ in c.store.intents()}
            if exported is not None:
                if not isinstance(exported,dict) or set(exported)!={"version","registry","pool","registration_board","intents","requests"} or exported["version"]!="1":
                    raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
                if (exported["registry"]!=ctx["registry"].address or exported["pool"]!=ctx["pool"].address
                        or exported["registration_board"]!=self.board_address):raise ValueError("ADDRESS_REGISTRATION_AUTHORITY")
                if not isinstance(exported["intents"],list) or not 1<=len(exported["intents"])<=8:
                    raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
                raws=[bytes.fromhex(x) for x in exported["intents"]];proofs=exported["requests"]
            else:
                if identifiers is None:
                    identifiers=[key for key in rows if not bytes(policy.read(ctx["registry"],b.number,"getBinding",hex32(key))[0])][:8]
                if not identifiers:return dict(status="NO_REGISTRATION_WORK",ready=False)
                if any(identifier not in rows for identifier in identifiers):raise ValueError("ADDRESS_WATCH_NOT_FOUND")
                raws=[bytes.fromhex(rows[identifier]["intent"]) for identifier in identifiers]
                proofs=[{} if bytes(policy.read(ctx["registry"],b.number,"getBinding",hex32(identifier))[0])
                    else self._proof(identifier,raw,ctx,b.number) for identifier,raw in zip(identifiers,raws)]
            if not 1<=len(raws)<=8 or len({DepositIntentV1.decode(x).intent_id for x in raws})!=len(raws):
                raise ValueError("ADDRESS_REGISTRATION_DUPLICATE")
            if not isinstance(proofs,list) or len(proofs)!=len(raws):raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
            identifier=policy.batch_id(ctx["registry"].address,raws)
            old=c.store.get(self._key(identifier))
            if old is not None:
                if str(deadline)!=old["attempts"][-1]["deadline"]:raise ValueError("ADDRESS_REGISTRATION_EXPLICIT_RETRY_REQUIRED")
                self._get(identifier)
                return self.result(old,"REGISTERED" if policy.complete(c.w3,ctx,raws,b.number) else "REGISTRATION_PREPARED")
            record=dict(version="1",registry=ctx["registry"].address,pool=ctx["pool"].address,
                registration_board=self.board_address,batch_id=identifier,intents=[x.hex() for x in raws],requests=proofs,
                attempts=[self._attempt(ctx,deadline,b.number)])
            if not policy.complete(c.w3,ctx,raws,b.number):
                self._validate_pending(record,raws,ctx,b.number)
            # An imported public batch seeds its own watch without the other
            # service's transport key, journal or signing package.
            if exported is not None:
                for raw,proof in zip(raws,proofs):
                    c.store.accept(raw,b"\x02"+bytes(policy.read(ctx["registry"],b.number,"baseX")),c.c["bitcoin_network"],
                        "registration-import:"+identifier+":"+DepositIntentV1.decode(raw).intent_id.hex(),proof)
                    if 'recipient_choice' in proof:
                        c.store.put('recipient-choice:'+DepositIntentV1.decode(raw).intent_id.hex(),proof['recipient_choice'])
            with c.store.journal.db() as db:
                if db.execute("SELECT COUNT(*) FROM address_meta WHERE id LIKE 'registration:%'").fetchone()[0]>=int(c.c["max_intents"]):
                    raise ValueError("ADDRESS_REGISTRATION_HISTORY_LIMIT")
                c.stable(b);c.store._put(db,self._key(identifier),record)
            return self.result(record,"REGISTERED" if policy.complete(c.w3,ctx,raws,b.number) else "REGISTRATION_PREPARED")
    @staticmethod
    def result(record,status="REGISTRATION_PREPARED"):
        a=record["attempts"][-1]
        return dict(status=status,batch_id=record["batch_id"],attempt=a["ordinal"],session=a["session"],
            intent=a["intent"],deadline=a["deadline"],registration_sequence=a["sequence"],ready=False)
    def export(self,identifier):
        record,_=self._get(identifier)
        return {key:record[key] for key in ("version","registry","pool","registration_board","intents","requests")}
    def _wire(self,record,raws,a,ctx,authority,session):
        digest=policy.operation_hash(self.client.w3,ctx,authority["authorization"],raws,int(a["custody"]),
            int(a["derivation"]),int(a["deadline"]),int(a["sequence"]),self.client.w3.eth.get_block("latest").number)
        args=[raws,int(a["custody"]),int(a["derivation"]),int(a["deadline"]),int(a["sequence"])]
        return dict(chain_id=str(self.client.w3.eth.chain_id),coordinator=authority["coordinator"].address,role="conf_ceremony",
            target=ctx["registry"].address,artifact="BitcoinAddressRegistry",hash_method="getRegisterOperationHash",
            hash_args=Bootstrap.wire(args),operation_hash=digest.hex(),session=str(session),
            address_registration=dict(version="1",pool=ctx["pool"].address,registration_board=self.board_address,
                batch_id=record["batch_id"],attempt=a["ordinal"],requests=record["requests"]))
    def propose(self,identifier):
        c=self.client
        with c.store.exclusive():
            record,raws=self._get(identifier);b,ctx,authority=self.context();a=record["attempts"][-1]
            if policy.complete(c.w3,ctx,raws,b.number):return self.result(record,"REGISTERED")
            from .bitcoin_continuity import client_guard
            client_guard(c,'ISSUE',ctx=ctx,number=b.number)
            self._validate_pending(record,raws,ctx,b.number);self._validate_pending(record,raws,ctx,"latest")
            if not can_publish_intent(authority["board"],c.account.address,b.number):
                raise ValueError("ADDRESS_REGISTRATION_OPERATOR_REQUIRED")
            key="registration-"+identifier+":"+a["ordinal"]
            if a["session"] is None:
                public=self._wire(record,raws,a,ctx,authority,1)
                receipt=c.send(key+":session","createSessionFromDKG",["0x"+authority["dkg_id"].hex(),
                    "0x"+public["operation_hash"],ctx["registry"].address,a["deadline"]],
                    target=authority["coordinator"].address,kind="FROSTCoordinator",receipt=True)
                events=authority["coordinator"].events.SessionCreated().process_receipt(receipt,errors=DISCARD)
                if len(events)!=1:raise ValueError("ADDRESS_REGISTRATION_RECEIPT")
                sid=int(events[0]["args"]["sessionId"])
                public=self._wire(record,raws,a,ctx,authority,sid)
                policy.review(c.w3,public,None,receipt["blockNumber"],published=False,executable=False)
                a.update(session=str(sid),intent=public,anchor=dict(number=str(receipt["blockNumber"]),hash=receipt["blockHash"].hex()))
                c.store.put(self._key(identifier),record)  # Before board publication.
            self._check_anchor(a)
            policy.review(c.w3,a["intent"],None,"latest",published=False)
            encoded=canonical(a["intent"]).encode()
            if len(encoded)>32768:raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
            prior=bytes(policy.read(authority["board"],"latest","intents",int(a["session"])))
            if prior and prior!=encoded:raise ValueError("ADDRESS_REGISTRATION_HISTORY_CONFLICT")
            if not prior:c.send(key+":publish","publishIntent",[a["session"],"0x"+encoded.hex()],target=authority["board"].address,kind="DKGOnchainBoard")
            policy.review(c.w3,a["intent"],None,"latest")
            return self.result(record,"AWAITING_PARTICIPANTS")
    def _check_anchor(self,attempt):
        if attempt["anchor"] is not None and self.client.w3.eth.get_block(int(attempt["anchor"]["number"])).hash.hex()!=attempt["anchor"]["hash"]:
            raise ValueError("ADDRESS_REGISTRATION_RECOVERY_REQUIRED")
    def adopt(self,session):
        c=self.client
        with c.store.exclusive():
            b,ctx,authority=self.context()
            raw=bytes(policy.read(authority["board"],b.number,"intents",uint(str(session),1)))
            try:public=json.loads(raw)
            except (ValueError,TypeError):raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED") from None
            if canonical(public).encode()!=raw:raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
            reviewed=policy.review(c.w3,public,None,b.number,session=int(session),executable=False)
            if (reviewed["context"]["pool"].address!=ctx["pool"].address
                    or reviewed["context"]["registry"].address!=ctx["registry"].address):
                raise ValueError("ADDRESS_REGISTRATION_AUTHORITY")
            raws=reviewed["arguments"][0];identifier=public["address_registration"]["batch_id"]
            old=c.store.get(self._key(identifier))
            if old is not None:
                for a in old["attempts"]:
                    if a["session"]==str(session):
                        if a["intent"]!=public:raise ValueError("ADDRESS_REGISTRATION_HISTORY_CONFLICT")
                        return self.result(old,"REGISTRATION_ADOPTED")
                a=old["attempts"][-1]
                if a["session"] is None and self._wire(old,raws,a,ctx,authority,int(session))==public:
                    a.update(session=str(session),intent=public,anchor=dict(number=str(b.number),hash=b.hash.hex()))
                    c.stable(b);c.store.put(self._key(identifier),old)
                    return self.result(old,"REGISTRATION_ADOPTED")
                raise ValueError("ADDRESS_REGISTRATION_EXPLICIT_RETRY_REQUIRED")
            _,cg,dg,deadline,sequence=reviewed["arguments"]
            record=dict(version="1",registry=ctx["registry"].address,pool=ctx["pool"].address,registration_board=self.board_address,
                batch_id=identifier,intents=[x.hex() for x in raws],requests=public["address_registration"]["requests"],
                attempts=[dict(custody=str(cg),derivation=str(dg),deadline=str(deadline),sequence=str(sequence),session=str(session),
                    ordinal=public["address_registration"]["attempt"],
                    intent=public,anchor=dict(number=str(b.number),hash=b.hash.hex()))])
            with c.store.journal.db() as db:
                if db.execute("SELECT COUNT(*) FROM address_meta WHERE id LIKE 'registration:%'").fetchone()[0]>=int(c.c["max_intents"]):
                    raise ValueError("ADDRESS_REGISTRATION_HISTORY_LIMIT")
                c.stable(b);c.store._put(db,self._key(identifier),record)
            return self.result(record,"REGISTRATION_ADOPTED")
    def execute(self,identifier):
        from .bitcoin_continuity import client_guard
        c=self.client
        with c.store.exclusive():
            record,raws=self._get(identifier);b,ctx,_=self.context();a=record["attempts"][-1]
            # Canonical exact bindings survive expiry, spend and later incident status.
            if policy.complete(c.w3,ctx,raws,b.number):return self.result(record,"REGISTERED")
            if a["intent"] is None:raise ValueError("ADDRESS_REGISTRATION_SESSION_REQUIRED")
            self._check_anchor(a)
            reviewed=policy.review(c.w3,a["intent"],None,"latest")
            authority=reviewed["authority"];auth=authority["authorization"];op=reviewed["operation_hash"]
            key="registration-"+identifier+":"+a["ordinal"]
            if not policy.read(auth,"latest","isOperationApproved",op,ctx["registry"].address):
                result=policy.read(authority["coordinator"],"latest","getAuthorizationResult",int(a["session"]))
                if bytes(result[1])!=op or result[4]!=ctx["registry"].address:raise ValueError("ADDRESS_REGISTRATION_SESSION_MISMATCH")
                c.send(key+":authorize","authorizeOperation",[a["session"],"0x"+op.hex(),ctx["registry"].address],
                    target=auth.address,kind="DKGAuthorization")
            reviewed=policy.review(c.w3,a["intent"],None,"latest")
            from .recipient_profiles import open_registration_choices
            client_guard(c,'ISSUE',ctx=reviewed['context'],number=reviewed['anchor'].number)
            open_registration_choices(c,ctx,raws,record['requests'],c.w3.eth.get_block('latest').number)
            c.send(key+":register","registerBatch",Bootstrap.wire(reviewed["arguments"]+[op]),
                target=ctx["registry"].address,kind="BitcoinAddressRegistry")
            b,ctx,_=self.context()
            if not policy.complete(c.w3,ctx,raws,b.number):raise ValueError("ADDRESS_REGISTRATION_POSTCONDITION")
            c.stable(b);return self.result(record,"REGISTERED")
    def retry(self,identifier,deadline=None):
        c=self.client
        with c.store.exclusive():
            record,raws=self._get(identifier);b,ctx,authority=self.context();a=record["attempts"][-1]
            if policy.complete(c.w3,ctx,raws,b.number):return self.result(record,"REGISTERED")
            r=ctx["registry"];read=lambda method:policy.read(r,b.number,method)
            fenced=(int(a["sequence"])!=int(read("registrationNonce")) or int(a["custody"])<int(read("custodyGeneration"))
                or int(a["derivation"])<int(read("derivationGeneration")) or b.timestamp>int(a["deadline"]))
            if not fenced:
                if a["session"] is None:raise ValueError("ADDRESS_REGISTRATION_PENDING_ATTEMPT")
                previous=policy.review(c.w3,a["intent"],None,b.number,executable=False)
                if previous["state"]!=3 or policy.read(authority["authorization"],b.number,"isOperationApproved",previous["operation_hash"],r.address):
                    raise ValueError("ADDRESS_REGISTRATION_PENDING_ATTEMPT")
            if int(a["ordinal"])>=8:raise ValueError("ADDRESS_REGISTRATION_ATTEMPT_LIMIT")
            latest_deadline=a["deadline"] if deadline is None else str(deadline)
            new=self._attempt(ctx,latest_deadline,b.number,int(a["ordinal"])+1)
            candidate=dict(record,attempts=record["attempts"]+[new])
            self._validate_pending(candidate,raws,ctx,b.number);self._validate_pending(candidate,raws,ctx,"latest")
            c.stable(b);c.store.put(self._key(identifier),candidate)
            return self.result(candidate,"REGISTRATION_RETRY_PREPARED")
    def status(self,identifier):
        record,raws=self._get(identifier);b,ctx,_=self.context()
        return self.result(record,"REGISTERED" if policy.complete(self.client.w3,ctx,raws,b.number) else "REGISTRATION_PENDING")
