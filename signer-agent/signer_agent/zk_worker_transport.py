"""Authenticated transport adapter for the executable worker; ACKs are hints."""
import hashlib
import json
from pathlib import Path
from .bootstrap import artifact
from .operations import canonical
from .spv_transport import Client, load_config, hex32, uint
from .zk_spv import checkpoint_descriptor
from .zk_profiles import profile as zk_profile
from .zk_worker_batching import register
from .zk_worker_service import semantic_key


def validate_request(client,record,now):
    body=record["body"]; h=record["header"]; route=record["route"];c=client.c
    expected={"version","domain","request_id","nonce","confederation_id","federation_id","chain_id","state","adapter",
        "bitcoin_network","checkpoint","program_image_id","journal_schema","reason","operation_id","need","created_at","deliver_by","service_deadline","known_state","payment_job_id"}
    if body.get('version')=='2':expected.add('payment_request')
    if set(body)!=expected or body.get('version') not in ('1','2'):raise ValueError("SPV_REQUEST_SCHEMA")
    b,board,domain=client.context()
    view=client.w3.eth.contract(address=board.functions.stateView().call(block_identifier=b.number),abi=artifact(zk_profile(c['bitcoin_policy'])["view"])["abi"])
    adapter=view.functions.adapter().call(block_identifier=b.number)
    constants=dict(version=body['version'],domain=domain,request_id=route["request_id"],confederation_id=c["confederation_id"],federation_id=route["federation"],
        chain_id=c["chain_id"],state=c["zk_state"],adapter=adapter,bitcoin_network='bitcoin-'+c['bitcoin_policy']['network'],checkpoint=checkpoint_descriptor(c["bitcoin_policy"]).hex(),
        program_image_id=zk_profile(c['bitcoin_policy'])['image'],journal_schema="SATOSHIFI_TRANSITION_V2")
    if any(body[k]!=v for k,v in constants.items()) or h["sender"]!=route["requester"] or h["recipient"]!=route["service"]:
        raise ValueError("SPV_REQUEST_SCOPE")
    hex32(body["nonce"]);hex32(body["operation_id"])
    if not isinstance(body["known_state"],dict) or set(body["known_state"])!={"commitment","version"}:raise ValueError("SPV_KNOWN_STATE")
    hex32(body["known_state"]["commitment"]);uint(body["known_state"]["version"])
    if body["payment_job_id"] is not None:hex32(body["payment_job_id"])
    if body['version']=='2':
        p=body['payment_request']
        if not isinstance(p,dict) or set(p)!={'payer','budget_scope','treasury','treasury_epoch','roster_hash','tariff_version','tariff_hash','max_service_fee_wei'}:raise ValueError('SPV_PAYMENT_OFFER')
        if p['payer']!=h['sender']:raise ValueError('SPV_PAYMENT_OFFER_IDENTITY')
        from .spv_transport import Web3
        if not Web3.is_checksum_address(p['treasury']):raise ValueError('SPV_PAYMENT_OFFER')
        for name in ('budget_scope','roster_hash','tariff_hash'):hex32(p[name])
        for name in ('treasury_epoch','tariff_version','max_service_fee_wei'):uint(p[name],1,2**256-1)
    need=body["need"]
    if not isinstance(need,dict) or body["operation_id"]!=hashlib.sha256(canonical(need).encode()).hexdigest():raise ValueError("SPV_OPERATION_BINDING")
    reason={"deposit":"DEPOSIT","address_deposit":"DEPOSIT","withdrawal_fee":"DEPOSIT","withdrawal":"WITHDRAWAL"}.get(need.get("kind"))
    if reason is None or body["reason"]!=reason:raise ValueError("SPV_REASON")
    created=uint(body["created_at"]);deliver=uint(body["deliver_by"]);deadline=uint(body["service_deadline"])
    if (deliver!=int(h["expires_at"]) or not created<=now or not created<deliver<=deadline
            or deadline-created>int(c["service_sla_seconds"])):raise ValueError("SPV_REQUEST_EXPIRED")
    for number in (b.number,"latest"):
        if not board.functions.requesterAllowed(route["federation"],route["requester"]).call(block_identifier=number):raise ValueError("SPV_REQUEST_ROLE_REVOKED")
        if not board.functions.serviceAllowed(route["service"]).call(block_identifier=number):raise ValueError("SPV_SERVICE_ROLE_REVOKED")
    client.stable(b);return need,created,deadline


class WorkerTransport:
    def __init__(self,worker):
        self.worker=worker;self.client=Client(load_config(worker.c["transport"]["config_file"]))
        try:
            c=self.client.c
            if any(c[k]!=worker.c[k] for k in ("chain_id","evm_confirmations","bitcoin_policy","zk_state")):
                raise ValueError("SPV_WORKER_TRANSPORT_SCOPE")
            if c.get('address_deposits')!=worker.c.get('address_deposits'):raise ValueError('SPV_ADDRESS_PROFILE_CHANGED')
            if c.get('withdrawal_fees')!=worker.c.get('withdrawal_fees'):raise ValueError('SPV_WITHDRAWAL_FEE_PROFILE_CHANGED')
            _,_,domain=self.client.context()
            old=worker.inbox.get("transport_domain")
            if old is not None and old!=domain:raise ValueError("SPV_WORKER_TRANSPORT_CHANGED")
            worker.inbox.put("transport_domain",domain)
        except BaseException:self.client.close();raise
    def close(self):self.client.close()
    def ingest(self,*,routes=None):
        w=self.worker;c=self.client;c.poll()
        pending=[r for r in c.store.incoming("REQUEST") if r[2]=="RECEIVED"
                 and (routes is None or r[1]['header']['route'] in routes)]
        cursor=c.store.get("work_cursor") or 0
        processed=0
        b,_,_=c.context()
        for offset in range(len(pending)):
            index=(cursor+offset)%len(pending);key,record,status=pending[index]
            retry=c.store.get("validation:"+key) or dict(next_at=0,attempts=0)
            if b.timestamp<retry["next_at"]:continue
            if processed>=int(c.c["max_messages_per_poll"]):break
            processed+=1;c.store.put("work_cursor",index+1)
            # A previous accepted obligation survives expiry, restart and reorg;
            # it is re-observed independently by the worker and never cancelled by transport.
            link=w.inbox.get("remote:"+record["header"]["route"])
            if link is None:
                b,board,_=c.context()
                packet=board.functions.packet(hex32(key)).call(block_identifier=b.number)
                if not bytes(packet[8]):c.store.mark(key,"ORPHANED");continue
                if hashlib.sha256(bytes(packet[8])).hexdigest()!=record.get("ciphertext_sha256"):
                    c.store.mark(key,"QUARANTINED");continue
                try:
                    need,created,deadline=validate_request(c,record,b.timestamp)
                    allowed=w.c["transport"]["federation_consumers"].get(record["route"]["federation"],[])
                    if need.get("consumer") not in allowed:raise ValueError("SPV_FEDERATION_CONSUMER")
                    # Request registers only the validated need; preserve the
                    # sender's original SLA instead of inventing a local one.
                    result=w.request(need,register_local=False)
                    local=result["local_need_id"]
                    register(w,local,created,deadline,record["header"]["route"])
                    link=dict(local_need_id=local,deadline=deadline,request_id=record["route"]["request_id"],
                        operation_id=record["body"]["operation_id"],federation=record["route"]["federation"],payment_job_id=record["body"]["payment_job_id"],payment_request=record['body'].get('payment_request'))
                    w.inbox.put("remote:"+record["header"]["route"],link)
                except Exception as exc:
                    reason=str(exc)
                    if not (type(exc) is ValueError and reason.startswith(("SPV_","ZK_NEED_","ZK_CONSUMER_","ZK_DEPOSIT_","ZK_WITHDRAWAL_"))):
                        attempts=retry["attempts"]+1
                        c.store.put("validation:"+key,dict(attempts=attempts,next_at=b.timestamp+int(c.c["retry_backoff_seconds"])*min(2**min(attempts,12),4096)))
                        if attempts>=int(c.c["max_publish_attempts"]):c.store.mark(key,"VALIDATION_REVIEW_REQUIRED")
                        continue
                    c.queue_reply(record["header"]["route"],"ACK",0,dict(version="1",request_id=record["route"]["request_id"],
                        operation_id=record["body"].get("operation_id"),status="REJECTED",reason=reason))
                    c.store.mark(key,"REJECTED");continue
            body=dict(version="1",request_id=link["request_id"],operation_id=link["operation_id"],status="ACCEPTED",
                local_need_id=link["local_need_id"],service_deadline=str(link["deadline"]),
                semantic_key=semantic_key(w,[link["local_need_id"]]).hex())
            c.queue_reply(record["header"]["route"],"ACK",0,body);c.store.mark(key,"HANDLED")
    def updates(self,result,*,routes=None):
        w=self.worker;c=self.client;statuses={k:s for k,_,s in w.inbox.needs()}
        b,board,_=c.context()
        for route,encoded in w.inbox.db.execute("SELECT key,body FROM meta WHERE key LIKE 'remote:%'"):
            link=json.loads(encoded);route=route[len("remote:"):];local=link["local_need_id"]
            if routes is not None and route not in routes:continue
            if link.get("payment_job_id") and not w.inbox.get("service:"+local) and w.c.get("service",{}).get("mode")!="confederation":
                from .zk_worker_service import attach
                try:attach(w,dict(service_job_id=link["payment_job_id"],need_ids=[local]))
                except Exception:w.inbox.put("payment_link_error:"+local,"SPV_CANONICAL_PAYMENT_LINK_PENDING")
            status=statuses.get(local,"QUEUED")
            if result.get("status") in ("HALTED","HALT_OBSERVED_UNCONFIRMED"):status="HALTED"
            elif result.get("status")=="BLOCKED" and status!="COMPLETED":status="OBSERVATION_UNAVAILABLE"
            group=w.inbox.get("need_group:"+local)
            body=dict(version="1",request_id=link["request_id"],operation_id=link["operation_id"],status=status,
                local_need_id=local,execution_group_id=group,service_deadline=str(link["deadline"]),
                result=result.get("status"),reason=result.get("reason"),missed=b.timestamp>link["deadline"],
                canonical_service_job_id=(w.inbox.get("service:"+local) or {}).get("service_job_id"))
            marker=c.store.get("status:"+route) or dict(sequence=0,digest=None,next_at=0)
            digest=hashlib.sha256(canonical(body).encode()).hexdigest()
            if digest==marker["digest"] or b.timestamp<marker["next_at"] or marker["sequence"]>=64:continue
            sequence=marker["sequence"]+1
            c.queue_reply(route,"STATUS",sequence,body)
            actual=c.store.outgoing("status:"+route+":"+str(sequence))[1]["body"]
            c.store.put("status:"+route,dict(sequence=sequence,digest=hashlib.sha256(canonical(actual).encode()).hexdigest(),next_at=b.timestamp+int(w.c["transport"]["status_interval_seconds"])))
        # Heartbeat notices use established private routes; no synthetic txid/request.
        for notice,encoded in w.inbox.db.execute("SELECT key,body FROM meta WHERE key LIKE 'heartbeat_notice:%'"):
            entry=json.loads(encoded)
            if entry.get("delivered"):continue
            for route,link_encoded in w.inbox.db.execute("SELECT key,body FROM meta WHERE key LIKE 'remote:%'"):
                route=route[len("remote:"):]
                if routes is not None and route not in routes:continue
                marker=c.store.get("notice:"+notice+":"+route)
                if marker:continue
                status=c.store.get("status:"+route) or dict(sequence=0,digest=None,next_at=0)
                if status["sequence"]>=64:continue
                seq=status["sequence"]+1
                body=dict(version="1",status="HEARTBEAT_NOTICE",reason=entry["reason"],deadline=str(entry["deadline"]),
                    heartbeat_job_id=entry["service_job_id"],request_id=json.loads(link_encoded)["request_id"])
                key=c.queue_reply(route,"STATUS",seq,body,notice=notice)
                c.store.put("notice:"+notice+":"+route,key)
                status["sequence"]=seq;c.store.put("status:"+route,status)
    def flush(self):
        result=self.client.transmit()
        # Delivery is established by canonical packet availability, not a send call.
        if result["status"]=="CONFIRMED":
            row=self.client.store.db.execute("SELECT body FROM outgoing WHERE id=?",(result["message_id"],)).fetchone()
            notice=json.loads(row[0]).get("notice") if row else None
            if notice:
                value=self.worker.inbox.get(notice)
                if value:
                    route=json.loads(row[0])["header"]["route"]
                    value.setdefault("deliveries",{})[route]=result["message_id"]
                    expected={k[len("remote:"):] for k,_ in self.worker.inbox.db.execute("SELECT key,body FROM meta WHERE key LIKE 'remote:%'")}
                    value["delivered"]=bool(expected) and expected<=set(value["deliveries"])
                    self.worker.inbox.put(notice,value)
        return result


def request_from_consumer(config_path,graph,request,bitcoin,federation,recipient):
    """Remote hints never authorize money: readiness is read from state + Core."""
    from .bootstrap import artifact
    from .zk_spv import inspect_binding,inspect_live
    from .bitcoin_network import header_hash
    from . import crypto
    client=Client(load_config(config_path))
    try:
        c=client.c
        if (any(c[k]!=graph[k] for k in ("chain_id","bitcoin_policy","evm_confirmations"))
                or c["zk_state"]!=graph["contracts"]["relay"]["address"]):raise ValueError("SPV_CONSUMER_SCOPE")
        if request.get('kind')=='address_deposit':
            from .address_spv import check_graph
            policy=dict(c,allowed_consumers={pool:'MiningPoolV1' for pool in c.get('address_deposits',{}).get('registries',{})})
            check_graph(policy,graph,request)
            from .spv_automation import observation
            observed,_=observation(client.w3,policy,c['zk_state'],request,bitcoin,policy['allowed_consumers'])
            if observed['status'] in ('READY','COMPLETED'):
                return dict(status=observed['status'],source='CANONICAL_STATE')
        if request.get('kind')=='withdrawal_fee':
            from .withdrawal_fee_spv import check_graph
            policy=dict(c,allowed_consumers={pool:'MiningPoolV1' for pool in c.get('withdrawal_fees',{}).get('registries',{})})
            check_graph(policy,graph,request)
            from .spv_automation import observation
            observed,_=observation(client.w3,policy,c['zk_state'],request,bitcoin,policy['allowed_consumers'])
            if observed['status'] in ('READY','COMPLETED'):return dict(status=observed['status'],source='CANONICAL_STATE')
        block,_,_=client.context()
        state,adapter,_,_=inspect_binding(client.w3,c["zk_state"],c["bitcoin_policy"],block.number,
            adapter_address=graph["contracts"]["spv_guard"]["address"])
        try:
            inspect_live(bitcoin,state,c["bitcoin_policy"],block_identifier=block.number)
            tx=bitcoin.call("getrawtransaction",request["txid"],True)
            if tx.get("blockhash") and request.get('kind') not in ('address_deposit','withdrawal_fee'):
                raw=bytes.fromhex(bitcoin.call("getblockheader",tx["blockhash"],False))
                kind="MiningPoolV1" if request["kind"]=="deposit" else "BitcoinWithdrawalEscrow"
                consumer=client.w3.eth.contract(address=request["consumer"],abi=artifact(kind)["abi"])
                if kind=="MiningPoolV1":
                    deposits=consumer.functions.deposits().call(block_identifier=block.number)
                    depth=client.w3.eth.contract(address=deposits,abi=artifact("BitcoinDepositRegistry")["abi"]).functions.depth().call(block_identifier=block.number)
                    if crypto.call("bitcoin_funding",raw=tx["hex"],txid=request["txid"])["coinbase"]:depth=max(depth,100)
                else:depth=consumer.functions.depth().call(block_identifier=block.number)
                canonical,count=adapter.functions.getBlockStatus(header_hash(raw)).call(block_identifier=block.number)
                client.stable(block)
                if canonical and count>=max(6,depth):return dict(status="READY",source="CANONICAL_STATE")
        except ValueError as exc:
            if str(exc) not in ("MAINNET_RELAY_STALE","ZK_CORE_STATE_MISMATCH","MAINNET_RELAY_AHEAD_OF_CORE"):raise
        key,_=client.send_request(request,federation,recipient)
        client.poll();client.process_replies()
        from .spv_automation import payer_tick
        payment=payer_tick(client)
        delivery=client.transmit()
        return dict(status="AWAITING_ZK_STATE",message_id=key,delivery_status=delivery["status"],payment_status=payment['status'])
    finally:client.close()
