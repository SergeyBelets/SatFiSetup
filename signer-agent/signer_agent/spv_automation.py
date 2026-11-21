"""Opt-in canonical service admission/quotes/payments under immutable budgets.

Encrypted quotes are hints. Only independently re-read registry/state/Core facts
and a private payer policy can authorize an automated value transfer.
"""
import hashlib
import json
from pathlib import Path
from eth_account import Account
from web3 import Web3
from .bootstrap import Bootstrap
from .gas_treasury import GasTreasuryClient
from .onboarding import read_private
from .operations import Journal,canonical


def integer(value,minimum=1,maximum=2**256-1):
    if not isinstance(value,str) or not value.isascii() or not value.isdecimal() or str(int(value))!=value or not minimum<=int(value)<=maximum:
        raise ValueError('SPV_AUTOMATION_INTEGER')
    return int(value)


def identity(path):return Account.from_key(json.loads(read_private(Path(path)))['private_key']).address


def validate_budget(p):
    for k in ('max_tx_gas','max_transaction_cost_wei','max_total_cost_wei','max_job_cost_wei','max_attempts_per_job'):
        integer(p[k],21000 if k=='max_tx_gas' else 1,32 if k=='max_attempts_per_job' else 16777216 if k=='max_tx_gas' else 2**256-1)


class Budget:
    def __init__(self,identity_file,scope,policy,job,*,guard=None):
        self.journal=Journal(str(Path(identity_file).resolve())+'.spv-service-budget.sqlite')
        self.scope,self.policy,self.job=scope,policy,job
        self.guard=guard
        self.journal.put('spv-budget:'+hashlib.sha256(scope.encode()).hexdigest(),policy)
        with self.journal.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS spv_costs(scope TEXT,id TEXT,job TEXT,cost TEXT,PRIMARY KEY(scope,id))')
    def reserve(self,operation,tx):
        if self.guard is not None:self.guard()
        p=self.policy
        if tx['gas']>int(p['max_tx_gas']):raise ValueError('SPV_AUTOMATION_GAS_LIMIT')
        cost=tx['gas']*tx.get('maxFeePerGas',tx.get('gasPrice'))+int(tx.get('value',0))
        if cost>int(p['max_transaction_cost_wei']):raise ValueError('SPV_AUTOMATION_TX_BUDGET')
        with self.journal.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows={r['id']:(r['job'],int(r['cost'])) for r in db.execute('SELECT * FROM spv_costs WHERE scope=?',(self.scope,))}
            if operation not in rows and sum(1 for j,_ in rows.values() if j==self.job)>=int(p['max_attempts_per_job']):raise ValueError('SPV_AUTOMATION_ATTEMPT_BUDGET')
            wanted=max(cost,rows.get(operation,('',0))[1])
            if sum(v for k,(_,v) in rows.items() if k!=operation)+wanted>int(p['max_total_cost_wei']):raise ValueError('SPV_AUTOMATION_TOTAL_BUDGET')
            if sum(v for k,(j,v) in rows.items() if k!=operation and j==self.job)+wanted>int(p['max_job_cost_wei']):raise ValueError('SPV_AUTOMATION_JOB_BUDGET')
            db.execute('INSERT INTO spv_costs VALUES(?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET cost=excluded.cost',(self.scope,operation,self.job,str(wanted)))


def funding_scope(client):return client.manifest['domain'].lower()


def quote(client,job,coverage):
    request=client.prepare_fund('0x'+job)
    return dict(schema='satoshifi.spv-service-quote.v1',request=request,coverage=coverage)


def observation(w3,policy,state_address,need,bitcoin,allowed):
    from .zk_worker import Worker
    from .zk_spv import inspect_binding
    from .bitcoin_network import check_core
    from .evm_network import read_block
    b=read_block(w3,int(policy['evm_confirmations']))
    state,adapter,_,_=inspect_binding(w3,state_address,policy['bitcoin_policy'],b.number)
    if state.functions.halted().call():raise ValueError('MAINNET_RELAY_HALTED')
    worker=Worker.__new__(Worker);worker.w3=w3;worker.bitcoin=bitcoin
    worker.c=dict(policy,allowed_consumers=allowed)
    bitcoin_policy = policy["bitcoin_policy"]
    core = check_core(bitcoin, bitcoin_policy["network"], bitcoin_policy)
    result=worker.observe_need(need,b,adapter,core)
    if result['status']=='NEEDS_STATE' and worker.aligned(state,b,core) and state.functions.isFresh().call(block_identifier=b.number):
        current=state.functions.getState().call(block_identifier=b.number)
        if result['height']+result['depth']-1<=current[3]:result['status']='COVERED_BY_STATE'
    worker.stable(b);return result,core


def validate_payment(c,p):
    if p==dict(enabled=False):return
    fields={'enabled','binding','identity_file','bitcoin_rpc','bitcoin_cookie','budget_scope','allowed_federations','allowed_consumers',
        'allowed_service_identities','treasury','treasury_epoch','roster_hash','tariff_version','tariff_hash','max_service_fee_wei',
        'max_tx_gas','max_transaction_cost_wei','max_total_cost_wei','max_job_cost_wei','max_attempts_per_job'}
    if not isinstance(p,dict) or set(p)!=fields or p['enabled'] is not True:raise ValueError('SPV_PAYMENT_POLICY')
    validate_budget(p);integer(p['max_service_fee_wei']);integer(p['treasury_epoch']);integer(p['tariff_version'])
    for name in ('budget_scope','roster_hash','tariff_hash'):
        if len(bytes.fromhex(p[name]))!=32:raise ValueError('SPV_PAYMENT_POLICY')
    for name in ('identity_file','bitcoin_cookie'):
        if not Path(p[name]).is_absolute():raise ValueError('SPV_PAYMENT_POLICY')
    if (not isinstance(p['allowed_federations'],list) or not 1<=len(p['allowed_federations'])<=256
        or any(not isinstance(f,str) or not 1<=len(f.encode())<=128 for f in p['allowed_federations'])
        or not isinstance(p['allowed_consumers'],dict) or not 1<=len(p['allowed_consumers'])<=4096
        or not isinstance(p['allowed_service_identities'],list) or not 1<=len(p['allowed_service_identities'])<=32
        or not Web3.is_checksum_address(p['treasury'])):raise ValueError('SPV_PAYMENT_POLICY')
    for address,kind in p['allowed_consumers'].items():
        if not Web3.is_checksum_address(address) or kind not in ('MiningPoolV1','BitcoinWithdrawalEscrow',
                'ConfederationBitcoinReserve','BitcoinWithdrawalEscrowV2'):raise ValueError('SPV_PAYMENT_POLICY')
    if any(not Web3.is_checksum_address(a) for a in p['allowed_service_identities']):raise ValueError('SPV_PAYMENT_POLICY')
    binding=p['binding']
    if any(binding.get(k)!=c[k] for k in ('chain_id','evm_confirmations','bitcoin_policy','factory','confederation_id')):raise ValueError('SPV_PAYMENT_BINDING')


def offer(client,need,federation,recipient):
    p=client.c.get('payment',{})
    if not p.get('enabled'):return None
    if need.get('kind')=='withdrawal_fee':
        from .withdrawal_fee_spv import scope
        scope(dict(client.c,allowed_consumers=p['allowed_consumers']),need)
    if need.get('kind')=='address_deposit':
        from .address_spv import scope
        scope(dict(client.c,allowed_consumers=p['allowed_consumers']),need)
    if (federation not in p['allowed_federations'] or need.get('consumer') not in p['allowed_consumers']
            or recipient not in p['allowed_service_identities']):raise ValueError('SPV_PAYMENT_SCOPE')
    payer=identity(p['identity_file'])
    if payer!=client.account.address:raise ValueError('SPV_PAYMENT_PAYER_IDENTITY')
    return dict(payer=payer,budget_scope=p['budget_scope'],treasury=p['treasury'],treasury_epoch=p['treasury_epoch'],
        roster_hash=p['roster_hash'],tariff_version=p['tariff_version'],tariff_hash=p['tariff_hash'],
        max_service_fee_wei=p['max_service_fee_wei'])


def payer_tick(transport,*,routes=None,recover_only=False,before_submit=None):
    p=transport.c.get('payment',{})
    if not p.get('enabled'):return dict(status='DISABLED')
    client=GasTreasuryClient(transport.w3,p['binding'])
    # Stable private journal is per payer identity, not a resettable transport directory.
    rows=transport.store.incoming('STATUS');processed=0
    check_block,_,_=transport.context()
    for message_id,record,state in rows:
        body=record.get('body',{})
        if routes is not None and record.get('header',{}).get('route') not in routes:continue
        if body.get('status')!='QUOTE' or state in ('QUARANTINED','ORPHANED'):continue
        if processed>=int(transport.c['max_messages_per_poll']):break
        key='payment:'+message_id
        saved=transport.store.get(key)
        if saved and saved.get('status') in ('STALE_QUOTE','REJECTED'):continue
        if check_block.timestamp<(transport.store.get('payment_next:'+message_id) or 0):continue
        processed+=1
        transport.store.put('payment_next:'+message_id,check_block.timestamp+int(transport.c['retry_backoff_seconds']))
        if (saved and saved.get('status')=='CONFIRMED' and client.status(saved['job'])['paid']
                and (before_submit is None or saved.get('receipt',{}).get('transaction_hash') and saved.get('receipt',{}).get('block_hash'))):continue
        if saved and saved.get('request'):
            budget=Budget(p['identity_file'],funding_scope(client)+':payer',p,saved['job'])
            recovered=client.submit(saved['request'],p['identity_file'],budget=budget,recover_only=True)
            if recovered['status']!='NOT_SIGNED':
                transport.store.put(key,dict(saved,status=recovered['status'],receipt=recovered));return recovered
            if recover_only or before_submit is not None:return recovered
        route=record['header']['route']
        originals=[json.loads(r[0]) for r in transport.store.db.execute('SELECT body FROM outgoing')]
        original=next((v for v in originals if v['header']['kind']=='REQUEST' and v['header']['route']==route),None)
        if not original:continue
        if record['header']['sender'] not in p['allowed_service_identities']:raise ValueError('SPV_PAYMENT_SERVICE_IDENTITY')
        from .spv_transport import hex32
        block,board,_=transport.context();packet=board.functions.packet(hex32(message_id)).call(block_identifier=block.number)
        if not bytes(packet[8]) or hashlib.sha256(bytes(packet[8])).hexdigest()!=record['ciphertext_sha256']:continue
        proposed=body.get('quote')
        if not isinstance(proposed,dict) or proposed.get('schema')!='satoshifi.spv-service-quote.v1' or not isinstance(proposed.get('request'),dict):
            transport.store.put(key,dict(status='REJECTED'));continue
        request=proposed['request'];job=request.get('job_id')
        try:hex32(job.removeprefix('0x'))
        except (ValueError,AttributeError):transport.store.put(key,dict(status='REJECTED'));continue
        if client.status(job)['paid']:
            transport.store.put(key,dict(status='CONFIRMED',job=job));continue
        if recover_only:continue
        try:current=client.prepare_fund(job)
        except ValueError as exc:
            if str(exc) not in ('GAS_PERMISSION_INACTIVE','GAS_PERMISSION_EXPIRED','GAS_JOB_ALREADY_PAID'):raise
            transport.store.put(key,dict(status='STALE_QUOTE',job=job));continue
        if current!=request:transport.store.put(key,dict(status='STALE_QUOTE',job=job));continue
        expected_offer=offer(transport,original['body']['need'],original['body']['federation_id'],original['header']['recipient'])
        terms=request['terms'];cfg=request['configuration']
        acknowledgments=[r for _,r,_ in transport.store.incoming('ACK') if r.get('header',{}).get('route')==route and r.get('body',{}).get('status')=='ACCEPTED']
        if not acknowledgments:continue
        ack=acknowledgments[0]
        from .spv_transport import message_key
        ack_key=message_key(route,'ACK','0')
        ack_packet=board.functions.packet(hex32(ack_key)).call(block_identifier=block.number)
        if not bytes(ack_packet[8]) or hashlib.sha256(bytes(ack_packet[8])).hexdigest()!=ack['ciphertext_sha256']:continue
        if (body.get('operation_id')!=original['body']['operation_id'] or body.get('request_id')!=original['request_id']
                or request.get('schema')!='satoshifi.spv-gas-request.v1' or request.get('action')!='fund'
                or request.get('registry')!=client.registry.address or request.get('domain','').lower()!=p['binding']['domain'].lower()
                or request.get('chain_id')!=transport.c['chain_id'] or request['payer']!=expected_offer['payer']
                or original['body'].get('payment_request')!=expected_offer
                or terms[1].removeprefix('0x')!=ack['body']['semantic_key']
                or terms[3]!=expected_offer['payer'] or terms[4].removeprefix('0x')!=p['budget_scope']
                or terms[2].removeprefix('0x')!=Web3.keccak(text=original['body']['federation_id']).hex()
                or int(terms[5])>int(original['body']['service_deadline']) or cfg[0]!=p['treasury']
                or cfg[2]!=p['tariff_version'] or cfg[3].removeprefix('0x')!=p['tariff_hash']
                or request['treasury_epoch']!=p['treasury_epoch'] or request['roster_hash'].removeprefix('0x')!=p['roster_hash']
                or int(request['value'])>int(p['max_service_fee_wei'])):raise ValueError('SPV_QUOTE_OUTSIDE_CONSENT')
        from .bitcoin_rpc import BitcoinRPC
        bitcoin=BitcoinRPC(p['bitcoin_rpc'],p['bitcoin_cookie'],transport.c['bitcoin_policy']['network'],transport.c['bitcoin_policy'])
        result,_=observation(transport.w3,transport.c,transport.c['zk_state'],original['body']['need'],bitcoin,p['allowed_consumers'])
        if result['status'] in ('READY','COMPLETED','COVERED_BY_STATE'):
            transport.store.put(key,dict(status='COVERED',job=job));continue
        if result['status']!='NEEDS_STATE':continue
        # The canonical registry prevents duplicate value transfers. Renewal does
        # not change the original job, payer, scope or deadline.
        if client.status(job)['paid']:
            transport.store.put(key,dict(status='CONFIRMED',job=job));continue
        current=client.prepare_fund(job)
        if current!=request:transport.store.put(key,dict(status='STALE_QUOTE',job=job));continue
        from .address_spv import guard
        from .withdrawal_fee_spv import guard as fee_guard
        budget=Budget(p['identity_file'],funding_scope(client)+':payer',p,job,
            guard=guard(transport.w3,transport.c,transport.c['zk_state'],original['body']['need'],bitcoin,p['allowed_consumers'])
                or fee_guard(transport.w3,transport.c,transport.c['zk_state'],original['body']['need'],bitcoin,p['allowed_consumers']))
        transport.store.put(key,dict(status='SENDING',job=job,request=request))
        # submit rechecks exact terms unless recovering the identical signed bytes.
        if before_submit is None:outcome=client.submit(request,p['identity_file'],budget=budget)
        else:outcome=client.submit(request,p['identity_file'],budget=budget,
            recover_only=bool(before_submit('fund',request,job)))
        transport.store.put(key,dict(status=outcome['status'],job=job,request=request,receipt=outcome))
        return outcome
    return dict(status='NOT_SIGNED' if recover_only else 'IDLE')
