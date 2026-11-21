"""Crash-resumable status decisions in the existing EVM_AUTH and transaction lanes."""
import json
from eth_utils import keccak
from web3.logs import DISCARD
from . import address_status as policy
from .address_registration_client import Driver as RegistrationDriver
from .bootstrap import Bootstrap
from .incident_transport import public_report
from .operations import canonical
from .spv_transport import hex32,uint
from .onchain_transport import can_publish_intent


def request_id(registry,report,scope,next_state,close):
    return keccak(b'SATOHSI_STATUS_REQUEST_V1\0'+canonical([registry,report,scope,next_state,close]).encode()).hex()


class Driver:
    __init__=RegistrationDriver.__init__
    context=RegistrationDriver.context
    def _key(self,identifier):hex32(identifier);return 'incident-operation:'+identifier
    def _get(self,identifier):
        r=self.client.store.get(self._key(identifier))
        if (r is None or r['registry']!=self.client.c['registry'] or r['pool']!=self.client.c['pool']
                or r['registration_board']!=self.board_address or r['incident_board']!=self.client.c['board']
                or request_id(r['registry'],r['report_id'],r['scope'],r['next_state'],r['close_issuance'])!=identifier):raise ValueError('INCIDENT_STATUS_REQUEST')
        return r
    def _attempt(self,ctx,report,next_state,close,deadline,number,ordinal=1):
        scope=int(report['scope']);s=policy.snapshot(ctx,number,scope)
        c=[scope,s['state'],s['generation'],s['sequence'],next_state,close,uint(str(deadline),1,2**64-1),
            [bytes.fromhex(report[k]) for k in ('incident_id','reason','evidence_hash','bitcoin_block','bitcoin_tx','evm_anchor')]]
        policy.pending(self.client.w3,ctx,c,number)
        return dict(ordinal=str(ordinal),change=Bootstrap.wire(c),session=None,intent=None,anchor=None)
    def _digest(self,record,ctx,authority,number):
        return policy.operation_hash(self.client.w3,ctx,authority['authorization'],policy.parse_change(record['attempts'][-1]['change']),number)
    def _complete(self,record,ctx,authority,number):
        c=policy.parse_change(record['attempts'][-1]['change']);op=self._digest(record,ctx,authority,number)
        receipt=int(policy.read(ctx['registry'],number,'statusReceipts',op))
        if receipt and receipt!=c[2]+1:raise ValueError('INCIDENT_STATUS_RECEIPT')
        return receipt
    def _progress(self,r,b,ctx,a):
        confirmed=self._complete(r,ctx,a,b.number)
        from .incident_observations import observe
        if confirmed:
            observe(self.client,ctx,r['scope'],b.number,restrictive=True)
            self.client.stable(b)
            return self.result(r,'STATUS_APPLIED_HISTORICAL',generation=str(confirmed),current=policy.snapshot(ctx,b.number,r['scope']))
        observed=self._complete(r,ctx,a,'latest')
        if observed:
            observe(self.client,ctx,r['scope'],'latest',restrictive=True)
            self.client.stable(b)
            return self.result(r,'STATUS_CONFIRMING',observed_generation=str(observed),generation='0',current=policy.snapshot(ctx,b.number,r['scope']))
        return None
    def _put(self,record,b):
        c=self.client;c.store.space(len(canonical(record))*3+8192)
        with c.store.journal.db() as db:
            key=self._key(record['request_id'])
            if c.store._get(db,key) is None and db.execute("SELECT COUNT(*) FROM address_meta WHERE id LIKE 'incident-operation:%'").fetchone()[0]>=int(c.c['max_intents']):
                raise ValueError('INCIDENT_HISTORY_LIMIT')
            c.stable(b);c.store._put(db,key,record)
    @staticmethod
    def result(record,status,**extra):
        a=record['attempts'][-1]
        return dict(status=status,operation_id=record['request_id'],report_id=record['report_id'],attempt=a['ordinal'],
            session=a['session'],intent=a['intent'],ready=False,**extra)
    def prepare(self,report_id,next_state,close,deadline):
        c=self.client
        with c.store.exclusive():
            b,ctx,a=self.context();report=c.report(report_id);scope=int(report['scope'])
            from .incident_transport import validate_report
            validate_report(report,c.w3,ctx,b.number)
            if next_state not in policy.STATES[scope] or type(close) is not bool:raise ValueError('INCIDENT_STATUS_INTENT')
            target=policy.STATES[scope].index(next_state)
            identifier=request_id(ctx['registry'].address,report_id,scope,target,close)
            old=c.store.get(self._key(identifier))
            if old is not None:
                old=self._get(identifier)
                if old['attempts'][-1]['change'][6]!=str(deadline):raise ValueError('INCIDENT_EXPLICIT_RETRY_REQUIRED')
                return self._progress(old,b,ctx,a) or self.result(old,'INCIDENT_STATUS_PREPARED')
            proof=c.proof(report_id,ctx,b.number)
            record=dict(version='1',request_id=identifier,registry=ctx['registry'].address,pool=ctx['pool'].address,
                registration_board=self.board_address,incident_board=c.c['board'],report_id=report_id,report=public_report(report),
                delivery=proof,scope=scope,next_state=target,close_issuance=close,
                attempts=[self._attempt(ctx,report,target,close,deadline,b.number)])
            self._put(record,b);return self.result(record,'INCIDENT_STATUS_PREPARED')
    def preview(self,report_id,next_state,close,deadline):
        """Freeze a reviewable exact transition before transport/session creation."""
        c=self.client
        with c.store.exclusive():
            b,ctx,a=self.context();report=c.report(report_id);scope=int(report['scope'])
            from .incident_transport import validate_report
            validate_report(report,c.w3,ctx,b.number)
            if next_state not in policy.STATES[scope] or type(close) is not bool:raise ValueError('INCIDENT_STATUS_INTENT')
            target=policy.STATES[scope].index(next_state)
            change=self._attempt(ctx,report,target,close,deadline,b.number)['change']
            decision=dict(version='1',report_id=report_id,
                operation_id=request_id(ctx['registry'].address,report_id,scope,target,close),change=change)
            from .address_watch import digest
            c.stable(b);return dict(status='INCIDENT_DECISION_REVIEW_REQUIRED',decision=decision,confirmation_hash=digest(decision),ready=False)
    def _wire(self,r,a,ctx,authority,session):
        change=policy.parse_change(a['change'])
        op=policy.operation_hash(self.client.w3,ctx,authority['authorization'],change,self.client.w3.eth.get_block('latest').number)
        return dict(chain_id=str(self.client.w3.eth.chain_id),coordinator=authority['coordinator'].address,role='conf_ceremony',
            target=ctx['registry'].address,artifact='BitcoinAddressRegistry',hash_method='getStatusOperationHash',
            hash_args=[a['change']],operation_hash=op.hex(),session=str(session),
            incident_status=dict(version='1',pool=r['pool'],registration_board=r['registration_board'],incident_board=r['incident_board'],
                report_id=r['report_id'],report=r['report'],delivery=r['delivery']))
    def _check_anchor(self,a):
        if a['anchor'] and self.client.w3.eth.get_block(int(a['anchor']['number'])).hash.hex()!=a['anchor']['hash']:raise ValueError('INCIDENT_EVM_REORG')
    def propose(self,identifier):
        c=self.client
        with c.store.exclusive():
            r=self._get(identifier);b,ctx,authority=self.context();a=r['attempts'][-1]
            progress=self._progress(r,b,ctx,authority)
            if progress:return progress
            change=policy.parse_change(a['change'])
            for n in (b.number,'latest'):policy.pending(c.w3,ctx,change,n)
            if not can_publish_intent(authority['board'],c.account.address,b.number):raise ValueError('INCIDENT_OPERATOR_REQUIRED')
            key='incident-'+identifier+':'+a['ordinal']
            if a['session'] is None:
                wire=self._wire(r,a,ctx,authority,1)
                receipt=c.send(key+':session','createSessionFromDKG',['0x'+authority['dkg_id'].hex(),'0x'+wire['operation_hash'],ctx['registry'].address,a['change'][6]],
                    target=authority['coordinator'].address,kind='FROSTCoordinator',receipt=True)
                events=authority['coordinator'].events.SessionCreated().process_receipt(receipt,errors=DISCARD)
                if len(events)!=1:raise ValueError('INCIDENT_STATUS_SESSION')
                sid=int(events[0]['args']['sessionId']);wire=self._wire(r,a,ctx,authority,sid)
                policy.review(c.w3,wire,None,receipt['blockNumber'],published=False,executable=False)
                a.update(session=str(sid),intent=wire,anchor=dict(number=str(receipt['blockNumber']),hash=receipt['blockHash'].hex()))
                self._put(r,b)
            self._check_anchor(a);policy.review(c.w3,a['intent'],None,'latest',published=False)
            encoded=canonical(a['intent']).encode()
            if len(encoded)>16384:raise ValueError('INCIDENT_STATUS_INTENT')
            prior=bytes(policy.read(authority['board'],'latest','intents',int(a['session'])))
            if prior and prior!=encoded:raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED')
            if not prior:c.send(key+':publish','publishIntent',[a['session'],'0x'+encoded.hex()],target=authority['board'].address,kind='DKGOnchainBoard')
            policy.review(c.w3,a['intent'],None,'latest')
            return self.result(r,'AWAITING_INCIDENT_REVIEW')
    def adopt(self,session):
        c=self.client
        with c.store.exclusive():
            b,ctx,authority=self.context();sid=uint(str(session),1,2**256-1)
            raw=bytes(policy.read(authority['board'],b.number,'intents',sid))
            try:wire=json.loads(raw)
            except (ValueError,TypeError):raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED') from None
            if canonical(wire).encode()!=raw:raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED')
            reviewed=policy.review(c.w3,wire,None,b.number,session=sid,executable=False)
            m=wire['incident_status'];change=reviewed['arguments']
            if m['pool']!=c.c['pool'] or m['incident_board']!=c.c['board'] or wire['target']!=c.c['registry']:raise ValueError('INCIDENT_REPORT_SCOPE')
            identifier=request_id(wire['target'],m['report_id'],change[0],change[4],change[5])
            old=c.store.get(self._key(identifier))
            if old is not None:
                if any(a['session']==str(sid) and a['intent']==wire for a in old['attempts']):return self.result(old,'INCIDENT_STATUS_ADOPTED')
                a=old['attempts'][-1]
                if a['session'] is not None or a['change']!=wire['hash_args'][0]:raise ValueError('INCIDENT_EXPLICIT_RETRY_REQUIRED')
                a.update(session=str(sid),intent=wire,anchor=dict(number=str(b.number),hash=b.hash.hex()));self._put(old,b)
                return self.result(old,'INCIDENT_STATUS_ADOPTED')
            r=dict(version='1',request_id=identifier,registry=wire['target'],pool=m['pool'],registration_board=self.board_address,
                incident_board=m['incident_board'],report_id=m['report_id'],report=m['report'],delivery=m['delivery'],
                scope=change[0],next_state=change[4],close_issuance=change[5],attempts=[dict(ordinal='1',change=wire['hash_args'][0],
                    session=str(sid),intent=wire,anchor=dict(number=str(b.number),hash=b.hash.hex()))])
            self._put(r,b);return self.result(r,'INCIDENT_STATUS_ADOPTED')
    def review(self,identifier):
        c=self.client
        with c.store.exclusive():
            r=self._get(identifier);wire=r['attempts'][-1]['intent']
            if wire is None:raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED')
            checked=policy.private_review(c,wire,None,'latest')
            if checked['complete']:return self.result(r,'STATUS_APPLIED_HISTORICAL',generation=str(checked['generation']))
            from .address_watch import digest
            b=checked['anchor'];change=checked['arguments']
            q=dict(version='1',operation_hash=wire['operation_hash'],report_id=r['report_id'],evidence_hash=r['report']['evidence_hash'],
                identity=c.account.address,scope='CUSTODY' if change[0]==0 else 'DERIVATION',expected_state=policy.STATES[change[0]][change[1]],
                next_state=policy.STATES[change[0]][change[4]],expected_generation=str(change[2]),sequence=str(change[3]),
                close_issuance=change[5],deadline=str(change[6]),service=checked['service'],observations=checked['observations'],anchor=dict(number=str(b.number),hash=b.hash.hex()))
            c.stable(b);c.store.put('incident-review:'+wire['operation_hash'],q)
            return self.result(r,'INCIDENT_LOCAL_REVIEW_REQUIRED',review=q,confirmation_hash=digest(q))
    def approve(self,identifier,accepted):
        from .address_watch import digest
        c=self.client
        with c.store.exclusive():
            r=self._get(identifier);wire=r['attempts'][-1]['intent']
            if wire is None:raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED')
            q=c.store.get('incident-review:'+wire['operation_hash'])
            if not q or accepted!=digest(q) or q['identity']!=c.account.address:raise ValueError('INCIDENT_CONFIRMATION_REQUIRED')
            if c.w3.eth.get_block(int(q['anchor']['number'])).hash.hex()!=q['anchor']['hash']:raise ValueError('INCIDENT_EVM_REORG')
            checked=policy.private_review(c,wire,None,'latest')
            if checked['complete']:return self.result(r,'STATUS_APPLIED_HISTORICAL')
            if c.account.address not in policy.read(checked['authority']['authorization'],'latest','getParticipants'):raise ValueError('INCIDENT_SIGNER_IDENTITY')
            c.stable(checked['anchor']);c.store.put('incident-approval:'+wire['operation_hash'],q)
            return self.result(r,'INCIDENT_LOCALLY_APPROVED')
    def execute(self,identifier):
        c=self.client
        with c.store.exclusive():
            r=self._get(identifier);b,ctx,a=self.context()
            progress=self._progress(r,b,ctx,a)
            if progress:return progress
            attempt=r['attempts'][-1];self._check_anchor(attempt);wire=attempt['intent']
            if wire is None:raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED')
            checked=policy.review(c.w3,wire,None,'latest');op=checked['operation_hash'];authority=checked['authority']
            key='incident-'+identifier+':'+attempt['ordinal'];auth=authority['authorization']
            if not policy.read(auth,'latest','isOperationApproved',op,ctx['registry'].address):
                result=policy.read(authority['coordinator'],'latest','getAuthorizationResult',int(attempt['session']))
                if bytes(result[1])!=op or result[4]!=ctx['registry'].address or not result[2] or result[6]<result[5]:raise ValueError('INCIDENT_STATUS_SESSION')
                try:c.send(key+':authorize','authorizeOperation',[attempt['session'],'0x'+op.hex(),ctx['registry'].address],target=auth.address,kind='DKGAuthorization',receipt=True)
                except ValueError as exc:
                    if str(exc)!='ADDRESS_TRANSACTION_UNCONFIRMED':raise
                    return self.result(r,'INCIDENT_STATUS_AUTHORIZING')
                b,ctx,a=self.context()
                progress=self._progress(r,b,ctx,a)
                if progress:return progress
            if not policy.read(auth,b.number,'isOperationApproved',op,ctx['registry'].address):return self.result(r,'INCIDENT_STATUS_AUTHORIZING')
            policy.review(c.w3,wire,None,'latest')
            try:c.send(key+':apply','setStatus',Bootstrap.wire([checked['arguments'],op]),target=ctx['registry'].address,kind='BitcoinAddressRegistry',receipt=True)
            except ValueError as exc:
                if str(exc)!='ADDRESS_TRANSACTION_UNCONFIRMED':raise
                b,ctx,a=self.context()
                return self._progress(r,b,ctx,a) or self.result(r,'INCIDENT_STATUS_SUBMITTED')
            b,ctx,a=self.context();generation=self._complete(r,ctx,a,b.number)
            if not generation:raise ValueError('INCIDENT_STATUS_RECEIPT')
            from .incident_observations import observe
            observe(c,ctx,r['scope'],b.number,restrictive=True)
            c.stable(b);return self.result(r,'STATUS_APPLIED',generation=str(generation),current=policy.snapshot(ctx,b.number,r['scope']))
    def status(self,identifier):
        r=self._get(identifier);b,ctx,a=self.context();progress=self._progress(r,b,ctx,a)
        self.client.stable(b)
        return progress or self.result(r,'INCIDENT_STATUS_PENDING',generation='0',current=policy.snapshot(ctx,b.number,r['scope']))
    def retry(self,identifier,deadline=None):
        c=self.client
        with c.store.exclusive():
            r=self._get(identifier);b,ctx,authority=self.context();a=r['attempts'][-1]
            progress=self._progress(r,b,ctx,authority)
            if progress:return progress
            old=policy.parse_change(a['change']);current=policy.snapshot(ctx,b.number,old[0])
            fenced=(current['generation']!=old[2] or current['sequence']!=old[3] or b.timestamp>old[6])
            if not fenced:
                if a['session'] is None:raise ValueError('INCIDENT_PENDING_ATTEMPT')
                prior=policy.review(c.w3,a['intent'],None,b.number,executable=False)
                if prior['state']!=3 or policy.read(authority['authorization'],b.number,'isOperationApproved',prior['operation_hash'],ctx['registry'].address):raise ValueError('INCIDENT_PENDING_ATTEMPT')
            if len(r['attempts'])>=8:raise ValueError('INCIDENT_ATTEMPT_LIMIT')
            r['attempts'].append(self._attempt(ctx,r['report'],r['next_state'],r['close_issuance'],deadline or old[6],b.number,len(r['attempts'])+1))
            self._put(r,b);return self.result(r,'INCIDENT_RETRY_PREPARED')
    def recover(self,identifier):
        c=self.client
        with c.store.exclusive():
            r=self._get(identifier);b,ctx,a=self.context();attempt=r['attempts'][-1]
            progress=self._progress(r,b,ctx,a)
            if progress:return progress
            if attempt['intent'] is None:raise ValueError('INCIDENT_PUBLIC_INTENT_REQUIRED')
            policy.review(c.w3,attempt['intent'],None,b.number,executable=False)
            from .incident_observations import observe
            observe(c,ctx,r['scope'],b.number,recover=True)
            from .address_watch import digest
            c.store.put('incident-orphan-evidence:'+identifier+':'+attempt['ordinal']+':'+digest(attempt['anchor']),dict(previous_anchor=attempt['anchor']))
            attempt['anchor']=dict(number=str(b.number),hash=b.hash.hex());self._put(r,b)
            # Old local approvals retain their anchors and cannot license new work.
            return self.result(r,'INCIDENT_REVALIDATION_REQUIRED')
