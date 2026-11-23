"""Graph-scoped BTC reserve observation. Unknown/pending BTC never becomes free."""
import hashlib
import json
import os
import secrets
from pathlib import Path
import sqlite3
from urllib.parse import quote

from eth_abi import encode
from eth_utils import keccak
from web3 import Web3
from .evm_rpc import evm_web3
from .bootstrap import artifact
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .evm_network import read_block
from .onboarding import read_private
from .operations import Journal,canonical

ZERO='0x'+'00'*20

def read(c,n,method,*args):return getattr(c.functions,method)(*args).call(block_identifier=n)
def digest(v):return hashlib.sha256(canonical(v).encode()).hexdigest()
def outpoint(txid,vout):return keccak(encode(['bytes32','uint32'],[bytes.fromhex(txid)[::-1],int(vout)]))
def number(v,lo=0,hi=2**64-1):
    from .spv_automation import integer
    return integer(v,lo,hi)

def load_config(path):
    c=json.loads(read_private(path))
    fields={'version','chain_id','evm_rpc','evm_confirmations','bitcoin_network','bitcoin_policy','bitcoin_rpc','bitcoin_cookie',
        'reserve','watch_databases','identity_file','directory','max_inventory','report_seconds','low_water_sats','target_sats',
        'stress_sat_per_vbyte','service_vbytes','future_services','max_versions','max_tx_gas','max_transaction_cost_wei',
        'max_total_cost_wei','max_job_cost_wei','max_attempts_per_job'}
    if (not isinstance(c,dict)
            or set(c)-{'trust_remote_watch_filters'} != (fields|({'fee_watch_databases'} if c.get('version')=='2' else set()))
            or type(c.get('trust_remote_watch_filters',False)) is not bool
            or c['version'] not in ('1','2')):raise ValueError('BTC_FEE_CONFIG')
    from .bitcoin_trust import graph_options
    graph_options(dict(version='2',bitcoin_network=c['bitcoin_network'],evm_confirmations=c['evm_confirmations'],
        **({'bitcoin_policy':c['bitcoin_policy']} if c['bitcoin_policy'] is not None else {})))
    if c['chain_id'] not in ('1','11155111','31337') or not Web3.is_checksum_address(c['reserve']):raise ValueError('BTC_FEE_CONFIG')
    from .bootstrap_policy import validate_confirmations
    validate_confirmations(c,'BTC_FEE_')
    if not isinstance(c['watch_databases'],list) or not 1<=len(c['watch_databases'])<=32 or len(set(c['watch_databases']))!=len(c['watch_databases']):raise ValueError('BTC_FEE_WATCH_REQUIRED')
    for p in [c['identity_file'],c['directory'],c['bitcoin_cookie'],*c['watch_databases']]:
        if not isinstance(p,str) or not Path(p).is_absolute():raise ValueError('BTC_FEE_CONFIG')
    if c['version']=='2':
        paths=c['fee_watch_databases']
        if (not isinstance(paths,list) or not 1<=len(paths)<=32 or len(set(paths))!=len(paths)
                or set(paths)&set(c['watch_databases']) or any(not isinstance(p,str) or not Path(p).is_absolute() for p in paths)):
            raise ValueError('WITHDRAWAL_FEE_WATCH_REQUIRED')
    for k,lo,hi in [('max_inventory',1,65536),('report_seconds',30,3600),('low_water_sats',0,2100000000000000),
        ('target_sats',1,2100000000000000),('stress_sat_per_vbyte',1,1000000),('service_vbytes',100,100000),
        ('future_services',1,4096),('max_versions',1,16)]:number(c[k],lo,hi)
    if int(c['target_sats'])<int(c['low_water_sats']):raise ValueError('BTC_FEE_CONFIG')
    from .spv_automation import validate_budget
    validate_budget(c)
    return c

class Inspector:
    def __init__(self,c,w3=None,bitcoin=None):
        self.c=c;self.w3=w3 or evm_web3(c['evm_rpc'], timeout=30)
        if bitcoin is None:
            from .bitcoin_rpc import BitcoinRPC
            bitcoin=BitcoinRPC(c['bitcoin_rpc'],c['bitcoin_cookie'],c['bitcoin_network'],c['bitcoin_policy'])
        self.bitcoin=bitcoin
        # No watch backup may reset the fee authority/observation journal.
        path=Path(str(Path(c['identity_file']).resolve())+'.btc-fee.sqlite');existed=path.exists()
        lane=Journal(str(Path(c['identity_file']).resolve())+'.transactions.sqlite');self.lane=lane
        with lane.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS btc_fee_identity(token TEXT PRIMARY KEY)')
            saved=db.execute('SELECT token FROM btc_fee_identity').fetchall()
            if saved:
                if len(saved)!=1 or not existed:raise ValueError('BTC_FEE_JOURNAL_RECOVERY_REQUIRED')
                token=saved[0][0]
            else:
                if existed:raise ValueError('BTC_FEE_JOURNAL_RECOVERY_REQUIRED')
                token=secrets.token_hex(32);db.execute('INSERT INTO btc_fee_identity VALUES(?)',(token,))
        self.journal=Journal(path);self.journal.put('btc-fee-identity',dict(token=token))
        self.journal.put('fee-policy:'+c['reserve'],{k:v for k,v in c.items() if k not in ('evm_rpc','bitcoin_rpc','bitcoin_cookie','directory','trust_remote_watch_filters')})
    def context(self,n=None,*,require_bound=True):
        w3=self.w3;c=self.c
        if str(w3.eth.chain_id)!=c['chain_id']:raise ValueError('CHAIN_MISMATCH')
        b=read_block(w3,int(c['evm_confirmations'])) if n is None else w3.eth.get_block(n)
        def node(where,kind,args=None):
            checked_runtime(w3,where,kind,b.number);prove_creation(w3,where,kind,args,b.number)
            return w3.eth.contract(address=where,abi=artifact(kind)['abi'])
        fee=node(c['reserve'],'BitcoinFeeReserve');r=node(read(fee,b.number,'addresses'),'BitcoinAddressRegistry')
        module=node(read(fee,b.number,'userModule'),'BitcoinFeeReserveUser',[fee.address])
        if read(module,b.number,'reserve')!=fee.address:raise ValueError('BTC_FEE_GRAPH_MISMATCH')
        common=node(read(fee,b.number,'deposits'),'BitcoinDepositRegistry')
        wallet=b'\x51\x20'+bytes(read(r,b.number,'baseX'));pool_address=read(common,b.number,'scriptPools',keccak(wallet))
        pool=node(pool_address,'MiningPoolV1')
        from .address_mint import configuration
        ac=configuration(w3,pool,b.number)
        batch=ac['batch'];escrow=node(read(fee,b.number,'escrow'),'BitcoinWithdrawalEscrow')
        board=node(read(fee,b.number,'signingBoard'),'BitcoinSigningBoard')
        donor=node(read(fee,b.number,'donorVerifier'),'FROSTVerifier',[])
        funding=node(read(fee,b.number,'fundingVerifier'),'BitcoinFeeFundingVerifier',[])
        plan_verifier=node(read(fee,b.number,'planVerifier'),'BitcoinFeePlanVerifier',[])
        args=[r.address,board.address,donor.address,funding.address,plan_verifier.address,int(read(fee,b.number,'protocolFeeCap')),int(read(fee,b.number,'maxVbytes'))]
        prove_creation(w3,fee.address,'BitcoinFeeReserve',args,b.number)
        binding=read(batch,b.number,'feeReserve')
        if (ac['registry'].address!=r.address or ac['common'].address!=common.address or binding not in ((fee.address,) if require_bound else (ZERO,fee.address))
                or read(fee,b.number,'batches')!=batch.address or read(r,b.number,'escrow')!=escrow.address
                or read(fee,b.number,'authorization')!=read(pool,b.number,'authorization')
                or read(board,b.number,'batchRegistry')!=batch.address or read(board,b.number,'walletId')!=read(r,b.number,'walletId')
                or read(board,b.number,'dkg')!=read(r,b.number,'dkg') or bytes(read(fee,b.number,'walletScriptHash'))!=keccak(wallet)):
            raise ValueError('BTC_FEE_GRAPH_MISMATCH')
        # Independent scope preimage; constructor type/args remain independently checked above.
        expected=keccak(encode(['bytes32','uint256']+['address']*6+['bytes32','bytes32','address','bytes32','bytes32','uint64','bytes32','uint64','uint32'],[
            keccak(text='SATOHSI_BTC_FEE_RESERVE_V1'),w3.eth.chain_id,fee.address,r.address,common.address,batch.address,escrow.address,board.address,
            bytes(read(r,b.number,'graphId')),bytes(read(r,b.number,'bitcoinGenesis')),read(r,b.number,'dkg'),bytes(read(r,b.number,'walletId')),
            bytes(read(r,b.number,'walletResult')),int(read(r,b.number,'keyEpoch')),keccak(wallet),args[-2],args[-1]]))
        if bytes(read(fee,b.number,'scope'))!=expected:raise ValueError('BTC_FEE_GRAPH_MISMATCH')
        self.stable(b)
        ctx=dict(fee=fee,registry=r,common=common,batch=batch,escrow=escrow,board=board,pool=pool,address_context=ac,block=b,scope=expected.hex(),wallet=wallet)
        from .withdrawal_fee import configuration
        ctx['withdrawal_fees']=configuration(w3,ctx,b.number)
        return ctx
    def stable(self,b):
        if self.w3.eth.get_block(b.number).hash!=b.hash:raise ValueError('BTC_FEE_EVM_REORG')
    def _bounded(self,count):
        if count>int(self.c['max_inventory']):raise ValueError('BTC_FEE_INVENTORY_LIMIT')
    def watches(self,ctx,best):
        from .address_watch import coverage_at
        from .deposit_intent import DepositIntentV1
        scripts={ctx['wallet'].hex()};snapshots={};ids=set()
        for path in self.c['watch_databases']:
            p=Path(path)
            if not p.is_file() or p.is_symlink() or p.stat().st_mode&0o077 or p.stat().st_uid!=os.getuid():raise ValueError('BTC_FEE_WATCH_REQUIRED')
            db=sqlite3.connect('file:'+quote(str(p))+'?mode=ro',uri=True)
            try:
                db.execute("BEGIN")
                row=db.execute("SELECT body FROM address_meta WHERE id='scope'").fetchone()
                scope=json.loads(row[0])['scope'] if row else {}
                if scope.get('registry')!=ctx['registry'].address or scope.get('chain_id')!=self.c['chain_id'] or scope.get('bitcoin_genesis')!=bytes(read(ctx['registry'],ctx['block'].number,'bitcoinGenesis')).hex():
                    raise ValueError('BTC_FEE_WATCH_SCOPE')
                rows=db.execute('SELECT id,immutable,next_height FROM address_intents ORDER BY id').fetchall();self._bounded(len(rows))
                for identifier,encoded,next_height in rows:
                    immutable=json.loads(encoded);intent=DepositIntentV1.decode(bytes.fromhex(immutable['intent']))
                    d=intent.derive(b'\x02'+ctx['wallet'][2:])
                    if (d!=immutable['descriptor'] or intent.intent_id.hex()!=identifier
                            or Web3.to_checksum_address(intent.deposit_registry)!=ctx['registry'].address):raise ValueError('ADDRESS_DESCRIPTOR_CONFLICT')
                    if coverage_at(db,identifier,best,tip=next_height-1) is None:
                        raise ValueError('BTC_FEE_WATCH_INCOMPLETE')
                    scripts.add(d['script_pubkey']);ids.add(identifier)
                    entry=[identifier,digest(immutable),next_height,best]
                    if identifier in snapshots and snapshots[identifier]!=entry:raise ValueError('BTC_FEE_WATCH_CONFLICT')
                    snapshots[identifier]=entry
            finally:db.close()
        r=ctx['registry'];n=ctx['block'].number;count=int(read(r,n,'intentCount'));self._bounded(count)
        for i in range(count):
            identifier=bytes(read(r,n,'intentIds',i)).hex()
            if identifier not in ids:raise ValueError('BTC_FEE_WATCH_INCOMPLETE')
        with self.lane.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS fee_watch_ids(scope TEXT,id TEXT,PRIMARY KEY(scope,id))')
            known={x[0] for x in db.execute('SELECT id FROM fee_watch_ids WHERE scope=?',(ctx['scope'],))}
            if known-ids:raise ValueError('BTC_FEE_WATCH_RECOVERY_REQUIRED')
            db.executemany('INSERT OR IGNORE INTO fee_watch_ids VALUES(?,?)',[(ctx['scope'],key) for key in ids])
        self._bounded(len(scripts))
        entries=[snapshots[k] for k in sorted(snapshots)]
        if ctx.get('withdrawal_fees'):
            from .withdrawal_fee_watch import scripts as fee_scripts
            extra,fee_entries=fee_scripts(self,ctx,best);scripts.update(extra);entries+=fee_entries
        return sorted(scripts),entries
    def _utxo(self,display,vout,script=None,amount=None,*,mempool=True):
        from .bitcoin_rpc import satoshis
        row=self.bitcoin.call('gettxout',display,int(vout),mempool)
        if row is None:return None
        value=satoshis(row['value']);actual=row['scriptPubKey']['hex']
        if (script is not None and script!=actual) or (amount is not None and amount!=value):raise ValueError('BTC_FEE_UTXO_MISMATCH')
        if int(row['confirmations'])<1:raise ValueError('BTC_FEE_FUNDING_IMMATURE')
        return dict(outpoint=display+':'+str(vout),script=actual,satoshis=str(value),confirmations=str(row['confirmations']),coinbase=bool(row.get('coinbase',False)))
    def _known_pending(self,ctx,display,vout):
        n=ctx['block'].number;batch=ctx['batch'];fee=ctx['fee']
        key=keccak(encode(['bytes32','uint32'],[bytes.fromhex(display),int(vout)]))
        root=bytes(read(batch,n,'reservations',key))
        if root==bytes(32):return False
        count=int(read(fee,n,'familyCount',root));self._bounded(count);plans=[]
        for i in range(count):
            candidate=bytes(read(fee,n,'familyPlans',root,i));p=read(fee,n,'getPlan',candidate)
            if p[13] and not p[14]:plans.append((candidate,bytes(p[4])))
        if ctx.get('withdrawal_fees'):
            personal=ctx['withdrawal_fees']['payments'];count=int(read(personal,n,'versionCount',root));self._bounded(count)
            for index in range(count):
                candidate=bytes(read(personal,n,'versions',root,index))
                if read(personal,n,'bound',candidate) and not read(personal,n,'settled',candidate):
                    plans.append((candidate,bytes(read(personal,n,'verification',candidate)[1])))
        for candidate,wire_txid in plans:
            if read(batch,n,'batchState',candidate)!=2:continue
            txid=wire_txid[::-1].hex()
            try:tx=self.bitcoin.call('getrawtransaction',txid,True)
            except Exception as exc:
                from .bitcoin_rpc import BitcoinRPCError
                if isinstance(exc,BitcoinRPCError) and exc.rpc_code==-5:continue
                raise
            from .funding_envelope import inspect_funding
            decoded=inspect_funding(tx['hex'],txid)
            if any(v['outpoint']==display+':'+str(vout) for v in decoded['inputs']):
                if tx.get('blockhash'):
                    header=self.bitcoin.call('getblockheader',tx['blockhash'],True)
                    if self.bitcoin.call('getblockhash',header['height'])!=tx['blockhash']:continue
                else:
                    try:self.bitcoin.call('getmempoolentry',txid)
                    except Exception as exc:
                        from .bitcoin_rpc import BitcoinRPCError
                        if isinstance(exc,BitcoinRPCError) and exc.rpc_code==-5:continue
                        raise
                payment=read(ctx['escrow'],n,'payment',candidate)
                if bytes(payment[0])==wire_txid and not payment[2]:return dict(root=root.hex(),txid=txid,confirmed=bool(tx.get('blockhash')))
        return False
    def observed(self,ctx,display,vout,script=None,amount=None):
        row=self._utxo(display,vout,script,amount)
        if row is not None:return row,None
        pending=self._known_pending(ctx,display,vout)
        if pending and not pending['confirmed']:
            # Only the exact approved family can use a chain UTXO hidden by
            # mempool. Canonical whole-input locks still exclude it from available().
            row=self._utxo(display,vout,script,amount,mempool=False)
            if row is not None:
                row['inflight_root']=pending['root'];return row,None
        return None,pending
    def reconcile(self,*,full=True,state=None):
        """Only a complete canonical inventory is healthy. Failures never create free credit."""
        ctx=self.context();b=ctx['block'];n=b.number;fee=ctx['fee'];r=ctx['registry'];common=ctx['common'];core=self.bitcoin
        critical=[];waiting=[]
        try:
            cg=int(read(r,n,'custodyGeneration'));dg=int(read(r,n,'derivationGeneration'))
            with self.lane.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS fee_generations(scope TEXT PRIMARY KEY,cg TEXT,dg TEXT)')
                prior=db.execute('SELECT cg,dg FROM fee_generations WHERE scope=?',(ctx['scope'],)).fetchone()
                if prior and (cg<int(prior[0]) or dg<int(prior[1])):raise ValueError('BTC_FEE_GENERATION_ROLLBACK')
                db.execute('INSERT INTO fee_generations VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET cg=excluded.cg,dg=excluded.dg',
                    (ctx['scope'],str(cg),str(dg)))
            core.check_network()
            if core.call('getblockhash',0)!=bytes(read(r,n,'bitcoinGenesis')).hex():raise ValueError('BTC_ADDRESS_GENESIS_MISMATCH')
            info=core.call('getblockchaininfo')
            if info.get('initialblockdownload') or info.get('pruned'):raise ValueError('ADDRESS_ARCHIVAL_CORE_REQUIRED')
            best=core.call('getbestblockhash');height=int(info['blocks'])
            if core.call('getblockhash',height)!=best:raise ValueError('BTC_INPUT_CORE_CHANGED')
            scripts,watches=self.watches(ctx,best)
            accounting={};count=int(read(common,n,'backingCount'));self._bounded(count)
            for i in range(count):
                txid,vout,amount=read(common,n,'backingAt',i);display=bytes(txid)[::-1].hex();key=display+':'+str(vout)
                accounting[key]=dict(backing=int(amount),free=0,user=0)
            count=int(read(fee,n,'creditCount'));self._bounded(count)
            for i in range(count):
                c=read(fee,n,'creditAt',i);key=bytes(c[0])[::-1].hex()+':'+str(c[1])
                row=accounting.setdefault(key,dict(backing=0,free=0,user=0));row['free']=int(c[2])
            total_user=0
            if ctx.get('withdrawal_fees'):
                personal=ctx['withdrawal_fees']['credits'];count=int(read(personal,n,'claimCount'));self._bounded(count)
                for index in range(count):
                    _,claim=read(personal,n,'claimAt',index);wire,vout=read(common,n,'valueLocation',claim[1])
                    if keccak(encode(['bytes32','uint32'],[bytes(wire),int(vout)]))!=bytes(claim[1]):raise ValueError('WITHDRAWAL_FEE_VALUE_LOCATION')
                    key=bytes(wire)[::-1].hex()+':'+str(vout);row=accounting.setdefault(key,dict(backing=0,free=0,user=0));row['user']+=int(claim[2]);total_user+=int(claim[2])
            total_backing=int(read(common,n,'totalBackingSats'));total_free=int(read(fee,n,'freeSats'))
            if sum(x['backing'] for x in accounting.values())!=total_backing or sum(x['free'] for x in accounting.values())!=total_free:
                raise ValueError('BTC_FEE_ACCOUNTING_MISMATCH')
            token=self.w3.eth.contract(address=read(ctx['pool'],n,'mpToken'),abi=artifact('PoolMpToken')['abi'])
            if int(read(token,n,'totalSupply'))!=total_backing*10**10:critical.append('MP_SUPPLY_BACKING_MISMATCH')
            observed={}
            for key,amounts in accounting.items():
                display,vout=key.split(':');row,pending=self.observed(ctx,display,vout)
                if row is None:
                    if pending:waiting.append(key)
                    else:critical.append('MISSING:'+key)
                elif int(row['satoshis'])!=amounts['backing']+amounts['free']+amounts['user']:
                    critical.append('AMOUNT:'+key)
                elif amounts['free'] and int(row['confirmations'])<max(6,int(read(common,n,'depth'))):
                    raise ValueError('BTC_FEE_FUNDING_IMMATURE')
                elif amounts['user'] and int(row['confirmations'])<max(101 if row['coinbase'] else 6,int(read(common,n,'depth'))):
                    raise ValueError('WITHDRAWAL_FEE_FUNDING_IMMATURE')
                else:observed[key]=row
            if full:
                scan=core.call('scantxoutset','start',[{'desc':'raw('+s+')'} for s in scripts])
                if not scan.get('success') or scan.get('bestblock')!=best or int(scan.get('height',-1))!=height:raise ValueError('BTC_FEE_SCAN_INCOMPLETE')
                self._bounded(len(scan['unspents']))
                from .bitcoin_rpc import satoshis
                for item in scan['unspents']:
                    key=item['txid']+':'+str(item['vout'])
                    row,pending=self.observed(ctx,item['txid'],item['vout'],item['scriptPubKey'],satoshis(item['amount']))
                    if row is None:
                        if pending:waiting.append(key)
                        else:critical.append('MEMPOOL_SPEND:'+key)
                    else:
                        from .funding_envelope import inspect_funding
                        raw=core.call('getrawtransaction',item['txid'],True)
                        parsed=inspect_funding(raw['hex'],item['txid'])
                        output=parsed['outputs'][int(item['vout'])]
                        if output['script']!=row['script'] or int(output['amount'])!=int(row['satoshis']):raise ValueError('BTC_FEE_UTXO_MISMATCH')
                        observed[key]=row
            else:
                prior=self.inventory(ctx)
                if prior is None or prior['tip']!=best or prior['watches']!=watches:raise ValueError('BTC_FEE_RECONCILIATION_REQUIRED')
                for key,row in prior['utxos'].items():
                    if key not in observed:
                        display,vout=key.split(':');current,_=self.observed(ctx,display,vout,row['script'],int(row['satoshis']))
                        if current is None:raise ValueError('BTC_FEE_RECONCILIATION_REQUIRED')
                        observed[key]=current
            if core.call('getbestblockhash')!=best:raise ValueError('BTC_INPUT_CORE_CHANGED')
            self.stable(b)
            wallet_total=sum(int(x['satoshis']) for x in observed.values())
            pending=wallet_total-total_backing-total_free if not critical and not waiting else 0
            if pending<0:critical.append('PRINCIPAL_SHORTFALL');pending=0
            status='RECOVERY_REQUIRED' if critical else 'SETTLEMENT_REQUIRED' if waiting else 'HEALTHY'
            body=dict(scope=ctx['scope'],tip=best,height=str(height),watches=watches,utxos=observed,
                backing=str(total_backing),free=str(total_free),pending=str(pending),wallet=str(wallet_total),
                cg=str(read(r,n,'custodyGeneration')),dg=str(read(r,n,'derivationGeneration')),critical=sorted(set(critical)),waiting=sorted(set(waiting)))
            if ctx.get('withdrawal_fees'):
                body['personal_btc']=str(total_user);body['pending_unclassified']=str(pending-total_user) if status=='HEALTHY' else None
                token=read(ctx['pool'],n,'mpToken');personal=ctx['withdrawal_fees']['credits']
                body['locked_mp_fee_units']=str(read(personal,n,'lockedMPFees',token));body['locked_s_fee_units']=str(read(personal,n,'lockedSFees',token))
            # Unprivileged token locks/classification do not revoke another
            # admitted request: keep those diagnostics outside reserve authority.
            basis={k:v for k,v in body.items() if k not in ('personal_btc','pending_unclassified','locked_mp_fee_units','locked_s_fee_units')}
            evidence=digest(basis)
            self.journal.put('fee-evidence:'+evidence,basis)
            if full and status=='HEALTHY':
                # Monotonic journal entry API is used for immutable evidence; a
                # separate pointer selects the latest complete inventory.
                with self.journal.db() as db:
                    db.execute('CREATE TABLE IF NOT EXISTS fee_inventory(scope TEXT PRIMARY KEY,body TEXT NOT NULL)')
                    db.execute('INSERT INTO fee_inventory VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET body=excluded.body',(ctx['scope'],canonical(body)))
            result=dict(status=status,evidence=evidence,inventory=body,ready=False)
            if critical and state is not None:self.pause(state,ctx,result)
            return result
        except Exception as exc:
            from .bitcoin_rpc import observation_error
            return dict(status='OBSERVATION_UNAVAILABLE',**observation_error(exc),ready=False)
    def pause(self,state,ctx,result):
        key=self.recovery_key(ctx)
        marker=dict(kind='BTC_FEE_RESERVE',scope=ctx['scope'],evidence=result['evidence'],cg=result['inventory']['cg'],dg=result['inventory']['dg'])
        state.put('btc-fee-incident:'+ctx['scope']+':'+result['evidence'],marker)
        if not state.get(key):state.put(key,marker)
    def recovery_key(self,ctx):
        r=ctx['registry'];dkg=self.w3.eth.contract(address=read(r,ctx['block'].number,'dkg'),abi=artifact('DKGCoordinator')['abi'])
        wallet=read(dkg,ctx['block'].number,'getCompletedResult',bytes(read(r,ctx['block'].number,'walletId')))
        return 'btc-payments-recovery:'+bytes(wallet[0]).hex()
    def inventory(self,ctx):
        with self.journal.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS fee_inventory(scope TEXT PRIMARY KEY,body TEXT NOT NULL)')
            row=db.execute('SELECT body FROM fee_inventory WHERE scope=?',(ctx['scope'],)).fetchone()
            return json.loads(row[0]) if row else None
    def healthy(self,*,state=None,full=False,allow_recovery=False):
        result=self.reconcile(full=full,state=state)
        if result['status']!='HEALTHY':raise ValueError('BTC_FEE_'+result['status'])
        ctx=self.context();n=ctx['block'].number;r=read(ctx['fee'],n,'reconciliation');body=result['inventory']
        if state is not None and state.get(self.recovery_key(ctx)) and not allow_recovery:
            raise ValueError('BTC_FEE_RECONCILIATION_RECOVERY_REQUIRED')
        if (r[1]!=1 or int(r[4])<int(ctx['block'].timestamp) or str(r[0][1])!=body['cg'] or str(r[0][2])!=body['dg']
                or str(r[2])!=body['height'] or bytes(r[3]).hex()!=body['tip'] or bytes(r[9]).hex()!=result['evidence']
                or [str(x) for x in r[5:9]]!=[body[k] for k in ('wallet','backing','pending','free')]):
            raise ValueError('BTC_FEE_RECONCILIATION_REQUIRED')
        self.fence(ctx)
        return ctx,result
    def fence(self,ctx,*,derived=0):
        r=ctx['registry'];n=ctx['block'].number
        cg=int(read(r,n,'custodyGeneration'));dg=int(read(r,n,'derivationGeneration'))
        for at in (n,'latest'):
            if (int(read(r,at,'custodyGeneration'))!=cg or int(read(r,at,'derivationGeneration'))!=dg
                    or int(read(r,at,'custodyState')) not in (0,1)):
                raise ValueError('BTC_FEE_GENERATION_CHANGED')
            if derived:
                verifier=self.w3.eth.contract(address=read(r,at,'verifier'),abi=artifact('BitcoinDepositDerivation')['abi'])
                if (derived!=dg or read(r,at,'derivationState')!=0 or not read(verifier,at,'QUALIFIED')
                        or keccak(bytes(self.w3.eth.get_code(verifier.address,block_identifier=at)))!=bytes(read(r,at,'verifierRuntimeHash'))):
                    raise ValueError('BTC_FEE_DERIVATION_REQUIRED')
        self.stable(ctx['block'])
    def status(self,*,full=False,state=None):
        result=self.reconcile(full=full,state=state);ctx=self.context();f=ctx['fee'];n=ctx['block'].number
        free,locked,promised=[int(read(f,n,k)) for k in ('freeSats','lockedSats','promisedSats')]
        if free<locked+promised:raise ValueError('BTC_FEE_ACCOUNTING_MISMATCH')
        available=free-locked-promised
        cap=int(read(f,n,'protocolFeeCap'));vbytes=max(int(read(f,n,'maxVbytes')),int(self.c['service_vbytes']))
        unit=max(cap,int(self.c['stress_sat_per_vbyte'])*vbytes)
        runway=unit*int(self.c['future_services'])
        status=result['status']
        if status=='HEALTHY':
            try:self.healthy(state=state)
            except ValueError:status='RECONCILIATION_REQUIRED'
            else:status='RESERVE_LOW' if available<max(runway,int(self.c['low_water_sats'])) else 'FEE_CAP_PROFILE_REQUIRED' if unit>cap else 'SUFFICIENT_QUALIFICATION_REQUIRED'
        return dict(status=status,observation=result['status'],scope=ctx['scope'],free_sats=str(free),locked_sats=str(locked),
            promised_sats=str(promised),available_sats=str(available),runway_sats=str(runway),
            target_sats=str(max(int(self.c['target_sats']),runway)),protocol_fee_cap=str(cap),service_vbytes=str(vbytes),
            rate_source='STATIC_STRESS_POLICY',evidence=result.get('evidence'),ready=False)
    def donation(self,ctx,funding):
        """Explicit external donor consent; a memo/unknown base transfer is insufficient."""
        from .funding_envelope import inspect_funding
        from coincurve import PublicKeyXOnly
        raw,previous,vout,signature,block_hash,siblings,index=funding
        txid=hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()
        tx=self.bitcoin.call('getrawtransaction',txid,True);parsed=inspect_funding(tx['hex'],txid)
        if bytes.fromhex(parsed['stripped'])!=raw or len(parsed['inputs'])!=1 or parsed['coinbase']:
            raise ValueError('BTC_FEE_DONOR_PROFILE')
        source_id,source_vout=parsed['inputs'][0]['outpoint'].split(':')
        source=self.bitcoin.call('getrawtransaction',source_id,True);sp=inspect_funding(source['hex'],source_id)
        if bytes.fromhex(sp['stripped'])!=previous or sp['coinbase']:raise ValueError('BTC_FEE_DONOR_PROFILE')
        source_script=sp['outputs'][int(source_vout)]['script']
        scripts,_=self.watches(ctx,self.bitcoin.call('getbestblockhash'))
        if source_script in scripts or len(source_script)!=68 or not source_script.startswith('5120'):
            raise ValueError('BTC_FEE_PROTECTED_SOURCE')
        if any(o['script'].startswith('6a') for o in parsed['outputs']):raise ValueError('BTC_FEE_DEPOSIT_MARKER')
        if not 0<=vout<len(parsed['outputs']):raise ValueError('BTC_FEE_DONOR_PROFILE')
        output=parsed['outputs'][vout];amount=int(output['amount'])
        if output['script']!=ctx['wallet'].hex() or amount<=0:raise ValueError('BTC_FEE_DONOR_PROFILE')
        row=self._utxo(txid,vout,output['script'],amount)
        if row is None or int(row['confirmations'])<max(6,int(read(ctx['common'],ctx['block'].number,'depth'))):
            raise ValueError('BTC_FEE_FUNDING_IMMATURE')
        # The donation itself is an ordinary transfer. A coinbase source is
        # deliberately outside this restricted proof profile, never treated as 6-deep.
        message=bytes(read(ctx['fee'],ctx['block'].number,'donationHash',bytes.fromhex(txid)[::-1],vout,amount))
        if not PublicKeyXOnly(bytes.fromhex(source_script[4:])).verify(signature,message):raise ValueError('BTC_FEE_DONOR_CONSENT')
        header=self.bitcoin.call('getblockheader',tx['blockhash'],True)
        if (self.bitcoin.call('getblockhash',header['height'])!=tx['blockhash']
                or block_hash!=bytes.fromhex(tx['blockhash'])[::-1]):raise ValueError('BTC_FEE_FUNDING_REORG')
        fv=self.w3.eth.contract(address=read(ctx['fee'],ctx['block'].number,'fundingVerifier'),abi=artifact('BitcoinFeeFundingVerifier')['abi'])
        verified=read(fv,ctx['block'].number,'verify',ctx['fee'].address,funding)
        if bytes(verified[0])!=bytes.fromhex(txid)[::-1] or int(verified[1])!=amount:raise ValueError('BTC_FEE_DONOR_PROFILE')
        return dict(txid=txid,vout=str(vout),satoshis=str(amount),message=message.hex())


def configured(registry,reserve,*,bitcoin=None):
    path=getattr(registry,'fee_reserve_config',None)
    if not path:raise ValueError('BTC_FEE_CONFIG_REQUIRED')
    c=load_config(path)
    if c['reserve']!=reserve:raise ValueError('BTC_FEE_GRAPH_MISMATCH')
    return Inspector(c,registry.w3,bitcoin)


def guard(manifest,plan,registry,graph,*,bitcoin=None,state=None,reserved=True):
    """Canonical hook, including legacy routes and cached packets; never a new nonce lane."""
    batch=registry.w3.eth.contract(address=plan['batch_registry'],abi=artifact('BitcoinBatchRegistry')['abi'])
    reserve=read(batch,'latest','feeReserve')
    if reserve==ZERO:return
    if plan.get('version')=='4':
        from .withdrawal_fee import guard as personal_guard
        return personal_guard(manifest,plan,registry,graph,bitcoin=bitcoin,state=state,reserved=reserved)
    if plan.get('version')!='3' or graph.get('usage')!='BTC_INPUT_KEYS_V1':raise ValueError('BTC_FEE_ADDRESS_PROFILE_REQUIRED')
    inspector=configured(registry,reserve,bitcoin=bitcoin);ctx,result=inspector.healthy(state=state)
    from .bitcoin_input_keys import build
    from .bitcoin_onchain_plan import onchain_plan_hash
    built=build(plan);txid=hashlib.sha256(hashlib.sha256(bytes.fromhex(built['unsigned'])).digest()).digest()
    n=ctx['block'].number;p=read(ctx['fee'],n,'getPlan',bytes.fromhex(plan['batch_id']))
    if (ctx['batch'].address!=batch.address or ctx['registry'].address!=plan['address_registry']
            or bytes(p[3])!=onchain_plan_hash(manifest,plan) or bytes(p[4])!=txid
            or int(p[8])!=int(plan['fee']) or int(p[11])!=int(plan['custody_generation'])
            or int(p[12])!=int(plan['derivation_generation']) or int(plan['max_fee'])>int(read(ctx['fee'],n,'protocolFeeCap'))
            or p[14] or (reserved and not p[13])):raise ValueError('BTC_FEE_PREPARED_PLAN_REQUIRED')
    allocation=read(ctx['fee'],n,'allocations',p[0])
    if not allocation[3] or int(plan['max_fee'])>int(allocation[1]) or bytes(read(ctx['fee'],n,'winners',p[1]))!=bytes(32):
        raise ValueError('BTC_FEE_ALLOCATION_UNAVAILABLE')
    if int(read(ctx['registry'],n,'custodyGeneration'))!=int(plan['custody_generation']):raise ValueError('BTC_FEE_GENERATION_CHANGED')
    inspector.fence(ctx,derived=int(plan['derivation_generation']))
    if reserved:read(ctx['fee'],'latest','checkSigning',bytes.fromhex(plan['batch_id']))
    if bitcoin is not None and bitcoin.call('getbestblockhash')!=result['inventory']['tip']:raise ValueError('BTC_INPUT_CORE_CHANGED')
    return result


def recover(inspector,manifest,state):
    """Explicit recovery of this incident only, after a new authorized healthy report."""
    key='btc-payments-recovery:'+manifest.context.hex()
    with state.exclusive():
        marker=state.get(key)
        if not marker or marker.get('kind')!='BTC_FEE_RESERVE':raise ValueError('BTC_FEE_RECOVERY_SCOPE')
        result=inspector.reconcile(full=True,state=state)
        if result['status']!='HEALTHY':raise ValueError('BTC_FEE_'+result['status'])
        ctx,result=inspector.healthy(state=state,allow_recovery=True);body=result['inventory']
        r=ctx['registry'];n=ctx['block'].number
        dkg=inspector.w3.eth.contract(address=read(r,n,'dkg'),abi=artifact('DKGCoordinator')['abi'])
        completed=read(dkg,n,'getCompletedResult',bytes(read(r,n,'walletId')))
        if (bytes(completed[0])!=manifest.context or marker['scope']!=ctx['scope']
                or int(body['cg'])<int(marker['cg']) or int(body['dg'])<int(marker['dg'])
                or result['evidence']==marker['evidence'] or state.get(key)!=marker):raise ValueError('BTC_FEE_RECOVERY_SCOPE')
        state.put('btc-fee-recovered:'+ctx['scope']+':'+marker['evidence'],dict(incident=marker,reconciled=result['evidence']))
        state.put(key,{})
        return dict(status='BTC_FEE_RECOVERED',evidence=result['evidence'],ready=False)
