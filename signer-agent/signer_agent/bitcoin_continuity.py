"""Original-graph-only continuity. A locator/catalog is never migration authority."""
import hashlib
import json
from pathlib import Path
from contextlib import contextmanager

from coincurve import PublicKey
from .bootstrap import artifact,Bootstrap
from .operations import Journal,canonical
from .address_mint import read

PROFILE='ORIGINAL_BTC_ACCOUNTING_GRAPH_V1'


def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def family(genesis,key):
    if len(genesis)!=32 or len(key)!=32:raise ValueError('CONTINUITY_KEY')
    try:PublicKey(b'\x02'+key)
    except ValueError:raise ValueError('CONTINUITY_KEY') from None
    return hashlib.sha256(b'SATOHSI_BTC_KEY_FAMILY_V1\0'+genesis+key).hexdigest()


def sample(w3,ctx,number,*,allow_unbound=False):
    """Call only with a freshly authenticated configuration/inspected signer graph."""
    from .address_status import snapshot
    r=ctx['registry'];common=ctx['common'];batch=ctx['batch'];b=w3.eth.get_block(number);latest=w3.eth.get_block('latest')
    get=lambda name:read(r,b.number,name)
    genesis=bytes(get('bitcoinGenesis'));base=bytes(get('baseX'))
    identity=dict(profile=PROFILE,chain_id=str(w3.eth.chain_id),registry=r.address,deposits=common.address,
        escrow=get('escrow'),batches=batch.address,authorization=get('authorization'),dkg=get('dkg'),
        spv=read(common,b.number,'spv'),bitcoin_genesis=genesis.hex(),base_x=base.hex(),
        graph_id=bytes(get('graphId')).hex(),wallet_id=bytes(get('walletId')).hex(),wallet_result=bytes(get('walletResult')).hex(),
        key_epoch=str(get('keyEpoch')),custody_scope=bytes(get('custodyScope')).hex(),derivation_scope=bytes(get('derivationScope')).hex(),
        derivation_profile=bytes(get('PROFILE')).hex(),verifier=get('verifier'),
        verifier_runtime=bytes(get('verifierRuntimeHash')).hex(),verifier_source=bytes(get('verifierSourceHash')).hex(),
        qualification=bytes(get('qualificationCommitment')).hex(),service_policy=bytes(get('servicePolicyHash')).hex())
    bound=read(common,b.number,'addressRegistry')
    admitted=bound==r.address or allow_unbound and int(bound,16)==0 and int(read(common,b.number,'nonce'))==0
    if (get('deposits')!=common.address or get('batches')!=batch.address or not admitted
            or read(common,b.number,'spendRecorder')!=identity['escrow']
            or read(common,b.number,'authorization')!=identity['authorization']):raise ValueError('CONTINUITY_GRAPH')
    states={name:[snapshot(ctx,at,scope) for scope in (0,1)] for name,at in [('confirmed',b.number),('latest',latest.number)]}
    from .family_registry import declared
    if declared(r):
        identity['family_root']=get('familyRegistry')
        if read(r,latest.number,'familyRegistry')!=identity['family_root']:raise ValueError('FAMILY_ROOT_UNSTABLE')
    result=dict(family=family(genesis,base),identity=identity,states=states,
        anchor=dict(number=str(b.number),hash=b.hash.hex()),latest_anchor=dict(number=str(latest.number),hash=latest.hash.hex()))
    stable(w3,result);return result


def stable(w3,s):
    for key in ('anchor','latest_anchor'):
        a=s[key]
        if w3.eth.get_block(int(a['number'])).hash.hex()!=a['hash']:raise ValueError('CONTINUITY_REORG')


class Catalog:
    """Retained facts in identity transaction and/or independent signer spent journals."""
    def __init__(self,*,identity_file=None,state=None):
        if not identity_file and state is None:raise ValueError('CONTINUITY_JOURNAL_REQUIRED')
        self.identity_file=identity_file
        self.lane=Journal(str(Path(identity_file).resolve())+'.transactions.sqlite') if identity_file else None
        self.state=state

    @contextmanager
    def databases(self):
        # Commit each independently: a crash can add a restriction, never erase the older copy.
        from contextlib import ExitStack
        with ExitStack() as stack:
            sources=[]
            if self.lane is not None:sources.append(stack.enter_context(self.lane.db()))
            if self.state is not None:sources.append(stack.enter_context(self.state._journal()))
            for db in sources:
                db.execute('CREATE TABLE IF NOT EXISTS btc_continuity(family TEXT PRIMARY KEY,body TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS btc_continuity_aliases(alias TEXT PRIMARY KEY,family TEXT,binding TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS btc_continuity_keys(key TEXT PRIMARY KEY,family TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS btc_continuity_child_keys(key TEXT PRIMARY KEY,family TEXT,binding TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS btc_continuity_history(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS btc_family_root(chain TEXT PRIMARY KEY,body TEXT NOT NULL)')
            yield sources

    def observe(self,w3,s,*,recover=False):
        stable(w3,s);reasons=set();f=s['family'];identity=s['identity'];states=s['states']
        # Determine activation before opening journal transactions (the pin is
        # retained independently in those same journals).
        from .family_registry import activation
        activations=set()
        with self.databases() as sources:
            previous=[json.loads(row[0]) for db in sources if (row:=db.execute('SELECT body FROM btc_continuity WHERE family=?',(f,)).fetchone())]
        for old in previous:
            if old['identity']!=identity and activation(self,w3,old,s):activations.add(digest(old['identity']))
        with self.databases() as sources:
            for db in sources:
                row=db.execute('SELECT body FROM btc_continuity WHERE family=?',(f,)).fetchone()
                old=json.loads(row[0]) if row else None
                if old and digest(old['identity']) in activations:old=dict(old,identity=identity)
                alias=db.execute('SELECT family FROM btc_continuity_aliases WHERE alias=?',(f,)).fetchone()
                local=set()
                if alias and alias[0]!=f:local.add('CONTINUITY_DERIVED_KEY_REUSE')
                key_owner=db.execute('SELECT family FROM btc_continuity_keys WHERE key=?',(identity['base_x'],)).fetchone()
                child=db.execute('SELECT family FROM btc_continuity_child_keys WHERE key=?',(identity['base_x'],)).fetchone()
                if key_owner and key_owner[0]!=f:local.add('CONTINUITY_NETWORK_KEY_REUSE')
                if child and child[0]!=f:local.add('CONTINUITY_DERIVED_KEY_REUSE')
                if old and old['identity']!=identity:local.add('CONTINUITY_GRAPH_CHANGE')
                if recover and local:raise ValueError(sorted(local)[0])
                retained=old
                if old is None or old['identity']==identity:
                    max_states=[]
                    for scope,terminal in ((0,3),(1,2)):
                        confirmed,latest=states['confirmed'][scope],states['latest'][scope]
                        previous=old['states'][scope] if old else None
                        for current in (confirmed,latest):
                            if previous and (current['generation']<previous['generation'] or current['sequence']<previous['sequence']):local.add('CONTINUITY_GENERATION_ROLLBACK')
                            if previous and previous['terminal'] and current['state']!=terminal:local.add('CONTINUITY_TERMINAL')
                            if previous and previous['issuance_closed'] and not current['issuance_closed']:local.add('CONTINUITY_ISSUANCE_CLOSED')
                        if (latest['generation']<confirmed['generation'] or latest['sequence']<confirmed['sequence']
                                or latest['generation']==confirmed['generation'] and latest!=confirmed):local.add('CONTINUITY_UNSTABLE_STATE')
                        current=max((confirmed,latest),key=lambda x:(x['generation'],x['sequence']))
                        state=dict(current,terminal=current['state']==terminal or bool(previous and previous['terminal']),
                            issuance_closed=current['issuance_closed'] or bool(previous and previous['issuance_closed']))
                        if previous and (state['generation']<previous['generation'] or state['sequence']<previous['sequence']):
                            state=dict(previous,terminal=state['terminal'],issuance_closed=state['issuance_closed'])
                        max_states.append(state)
                    orphan=False
                    if old:
                        try:orphan=w3.eth.get_block(int(old['anchor']['number'])).hash.hex()!=old['anchor']['hash']
                        except Exception:orphan=True
                        if orphan and not recover:local.add('CONTINUITY_REANCHOR_REQUIRED')
                    if recover and local:raise ValueError(sorted(local)[0])
                    # First anchor for an unchanged checkpoint survives ordinary polling.
                    anchor=s['latest_anchor'] if not old or recover or max_states!=old['states'] and not orphan else old['anchor']
                    retained=dict(identity=identity,states=max_states,anchor=anchor)
                    db.execute('INSERT INTO btc_continuity VALUES(?,?) ON CONFLICT(family) DO UPDATE SET body=excluded.body',(f,canonical(retained)))
                witness=dict(family=f,identity=identity,observed=states,retained=retained,reasons=sorted(local),recovered=recover)
                if not local:db.execute('INSERT OR IGNORE INTO btc_continuity_keys VALUES(?,?)',(identity['base_x'],f))
                db.execute('INSERT OR IGNORE INTO btc_continuity_history VALUES(?,?)',(digest(witness),canonical(witness)))
                reasons.update(local)
        stable(w3,s)
        return dict(status='CONTINUITY_REQUIRED' if reasons else 'ORIGINAL_GRAPH_RETAINED',reasons=sorted(reasons),
            family=f,graph_fingerprint=digest(identity),ready=False,production_ready=False)

    def authorize(self,w3,s,mode='IDENTITY'):
        if mode not in ('READ','IDENTITY','ISSUE','ADDRESS_MINT','BASE_MINT','DERIVED_SIGN','BASE_SIGN'):raise ValueError('CONTINUITY_MODE')
        from .family_registry import guard
        guard(self,w3,s,mode)
        result=self.observe(w3,s)
        reasons=[r for r in result['reasons'] if mode!='READ' or r=='CONTINUITY_GRAPH_CHANGE']
        if reasons:raise ValueError(reasons[0])
        for custody,derivation in s['states'].values():
            if mode not in ('READ','IDENTITY') and custody['state'] not in (0,1):raise ValueError('CONTINUITY_CUSTODY_RESTRICTED')
            if mode=='ISSUE' and (custody['state']!=0 or custody['issuance_closed']):raise ValueError('CONTINUITY_ISSUANCE_CLOSED')
            if mode in ('ISSUE','ADDRESS_MINT','DERIVED_SIGN') and derivation['state']!=0:raise ValueError('CONTINUITY_DERIVATION_RESTRICTED')
        if mode=='ISSUE':
            from .continuity_handover import issuance_guard
            issuance_guard(self,s)
        return result

    def alias(self,w3,s,output_key,binding):
        self.authorize(w3,s,'READ')
        alias=family(bytes.fromhex(s['identity']['bitcoin_genesis']),output_key);parent=s['family']
        if alias==parent:raise ValueError('CONTINUITY_ALIAS_CONFLICT')
        with self.databases() as sources:
            for db in sources:
                row=db.execute('SELECT family,binding FROM btc_continuity_aliases WHERE alias=?',(alias,)).fetchone()
                if row and tuple(row)!=(parent,binding):raise ValueError('CONTINUITY_ALIAS_CONFLICT')
                other=db.execute('SELECT family,binding FROM btc_continuity_child_keys WHERE key=?',(output_key.hex(),)).fetchone()
                if other and tuple(other)!=(parent,binding):raise ValueError('CONTINUITY_ALIAS_CONFLICT')
                db.execute('INSERT OR IGNORE INTO btc_continuity_aliases VALUES(?,?,?)',(alias,parent,binding))
                db.execute('INSERT OR IGNORE INTO btc_continuity_child_keys VALUES(?,?,?)',(output_key.hex(),parent,binding))
        return alias


def context_for_graph(w3,graph):
    names=(('registry','address_registry','BitcoinAddressRegistry'),('common','deposits','BitcoinDepositRegistry'),('batch','batches','BitcoinBatchRegistry'))
    return {key:w3.eth.contract(address=graph['contracts'][name]['address'],abi=artifact(kind)['abi']) for key,name,kind in names}


def trust_guard(manifest,registry,state,graph,*,mode='IDENTITY'):
    if graph.get('usage') not in ('MINT_ONLY_V1','BTC_INPUT_KEYS_V1'):return
    from .bitcoin_trust import confirmed_block
    b=confirmed_block(registry.w3,graph);ctx=context_for_graph(registry.w3,graph)
    s=sample(registry.w3,ctx,b.number)
    if (s['identity']['wallet_id']!=manifest.session_id.hex() or s['identity']['dkg']!=manifest.registry):raise ValueError('CONTINUITY_GRAPH')
    return Catalog(identity_file=getattr(registry,'identity_file',None),state=state).authorize(registry.w3,s,mode)


def client_guard(client,mode='IDENTITY',*,ctx=None,number=None):
    if ctx is None:
        b,_,ctx,_,_=client.context();number=b.number
    s=sample(client.w3,ctx,number)
    return Catalog(identity_file=client.c['identity_file']).authorize(client.w3,s,mode)


def signing_guard(manifest,registry,state,graph,plan):
    if state is None or graph.get('usage')!='BTC_INPUT_KEYS_V1':return
    # Actual sign_step requires this pin before reaching the private boundary.
    # A component fixture without a graph pin is not canonical admission.
    if state.get('btc-registry:'+manifest.context.hex()) is None:return
    mode='DERIVED_SIGN' if any(d['mode']!='BASE' for d in plan['input_keys']) else 'BASE_SIGN'
    return trust_guard(manifest,registry,state,graph,mode=mode)


def evm_guard(client,state,session):
    from .family_registry import signing_guard as family_signing_guard
    family_signing_guard(client,state,session)
    binding=getattr(client,'continuity_binding',None)
    if binding is not None:
        anchor=getattr(client,'_read_snapshot',None) or client.w3.eth.get_block('latest')
        ctx=binding_context(client.w3,binding,session,anchor.number)
        latest=client.w3.eth.get_block('latest')
        binding_context(client.w3,binding,session,latest.number)
        if client.w3.eth.get_block(latest.number).hash!=latest.hash:raise ValueError('CONTINUITY_REORG')
        s=sample(client.w3,ctx,anchor.number,allow_unbound=True)
        return Catalog(identity_file=getattr(client,'continuity_identity_file',None),state=state).authorize(client.w3,s)
    payload=getattr(client,'continuity_context',None)
    if payload is None:return
    ctx,mode,sid=payload
    if sid!=session.session_id:raise ValueError('CONTINUITY_SESSION')
    anchor=getattr(client,'_read_snapshot',None) or client.w3.eth.get_block('latest')
    s=sample(client.w3,ctx,anchor.number)
    return Catalog(identity_file=getattr(client,'continuity_identity_file',None),state=state).authorize(client.w3,s,mode)


def binding_context(w3,intent,session,number):
    """Bootstrap admission of a NEW one-time binding, before its EVM_AUTH nonce/share."""
    from .address_mint import registry_identity
    from .bitcoin_trust import checked_runtime
    from .contract_provenance import prove_creation
    args=intent.get('hash_args')
    operation=intent.get('operation_hash','')
    operation=operation.removeprefix('0x') if isinstance(operation,str) else ''
    if (intent.get('artifact')!='BitcoinDepositRegistry' or intent.get('hash_method')!='getSetAddressRegistryOperationHash'
            or not isinstance(args,list) or len(args)!=1 or intent.get('target')!=session.origin_contract
            or intent.get('session')!=str(session.session_id) or operation!=session.message_hash.hex()):raise ValueError('CONTINUITY_SESSION')
    def node(where,kind):
        checked_runtime(w3,where,kind,number);prove_creation(w3,where,kind,None,number)
        return w3.eth.contract(address=where,abi=artifact(kind)['abi'])
    common=node(intent['target'],'BitcoinDepositRegistry');auth=read(common,number,'authorization')
    node(auth,'DKGAuthorization');escrow=node(read(common,number,'spendRecorder'),'BitcoinWithdrawalEscrow')
    batch=node(read(escrow,number,'batches'),'BitcoinBatchRegistry');dkg=read(batch,number,'dkg')
    node(dkg,'DKGCoordinator')
    # Initial binding is a signing authorization, so the derivation verifier
    # needs canonical runtime and constructor provenance even before issuance.
    r,_,_,_=registry_identity(w3,args[0],common.address,auth,escrow.address,batch.address,dkg,number)
    if bytes(read(common,number,'getSetAddressRegistryOperationHash',r.address)).hex()!=operation:raise ValueError('CONTINUITY_SESSION')
    return dict(registry=r,common=common,batch=batch)


def remember_binding(client,ctx,identifier,number):
    from .address_mint import binding
    checked=binding(dict(ctx,number=number),identifier)
    s=sample(client.w3,ctx,number)
    return Catalog(identity_file=client.c['identity_file']).alias(client.w3,s,checked['script'][2:],identifier.hex())


def fee_inventory(client,ctx,s,catalog,maximum,max_bindings,requests):
    n=int(s['anchor']['number']);escrow=client.w3.eth.contract(address=s['identity']['escrow'],abi=artifact('BitcoinWithdrawalEscrow')['abi'])
    result=dict(status='OBSERVED',personal=None,reserve=None)
    root=read(escrow,n,'withdrawalFees')
    if int(root,16):
        from .withdrawal_fee import configuration,funding_binding
        fee=configuration(client.w3,dict(ctx,escrow=escrow),n,root=root)
        claims=fee['credits'];count=int(read(claims,n,'claimCount'))
        if count>maximum:raise ValueError('CONTINUITY_INVENTORY_LIMIT')
        values=[Bootstrap.wire(read(claims,n,'claimAt',i)) for i in range(count)]
        funding=fee['funding'];count=int(read(funding,n,'bindingCount'))
        if count>max_bindings:raise ValueError('CONTINUITY_INVENTORY_LIMIT')
        aliases=[]
        for index in range(count):
            identifier=bytes(read(funding,n,'bindingIds',index))
            intent,desc,provenance=funding_binding(dict(ctx,withdrawal_fees=fee),identifier,n)
            catalog.alias(client.w3,s,bytes.fromhex(desc['output_key']),identifier.hex())
            aliases.append(dict(intent_id=identifier.hex(),intent=intent.encode().hex(),validation_provenance=provenance.hex()))
        result['personal']=dict(credits=claims.address,claims=values,funding_bindings=aliases,
            known_budgets={k:Bootstrap.wire(read(claims,n,'getBudget',bytes.fromhex(k))) for k in requests})
    reserve=read(ctx['batch'],n,'feeReserve')
    if int(reserve,16):
        from .bitcoin_trust import checked_runtime
        from .contract_provenance import prove_creation
        checked_runtime(client.w3,reserve,'BitcoinFeeReserve',n);prove_creation(client.w3,reserve,'BitcoinFeeReserve',None,n)
        node=client.w3.eth.contract(address=reserve,abi=artifact('BitcoinFeeReserve')['abi'])
        if read(node,n,'addresses')!=ctx['registry'].address or read(node,n,'deposits')!=ctx['common'].address:raise ValueError('CONTINUITY_GRAPH')
        result['reserve']=dict(address=reserve,free_sats=str(read(node,n,'freeSats')),
            locked_sats=str(read(node,n,'lockedSats')),promised_sats=str(read(node,n,'promisedSats')),
            reconciliation=Bootstrap.wire(read(node,n,'reconciliation')))
    return result


def inventory(client,*,max_entries=4096,max_bindings=256,requests=(),batches=()):
    """Bounded canonical facts; non-enumerable storage is explicitly not an emptiness proof."""
    if (type(max_entries) is not int or not 1<=max_entries<=4096 or type(max_bindings) is not int or not 1<=max_bindings<=256
            or len(requests)>32 or len(batches)>16 or len(set(requests))!=len(requests) or len(set(batches))!=len(batches)):
        raise ValueError('CONTINUITY_INVENTORY_LIMIT')
    from .spv_transport import hex32
    for value in (*requests,*batches):hex32(value)
    b,_,ctx,_,_=client.context();n=b.number;s=sample(client.w3,ctx,n)
    catalog=Catalog(identity_file=client.c['identity_file']);observation=catalog.observe(client.w3,s)
    r=ctx['registry'];common=ctx['common'];batch=ctx['batch'];count=int(read(r,n,'intentCount'))
    if count>max_bindings:raise ValueError('CONTINUITY_INVENTORY_LIMIT')
    bindings=[]
    from .address_mint import configuration,binding
    for index in range(count):
        identifier=bytes(read(r,n,'intentIds',index));row=read(r,n,'getBinding',identifier)
        pool=client.w3.eth.contract(address=row[1],abi=artifact('MiningPoolV1')['abi'])
        c=configuration(client.w3,pool,n,verify_derivation=False)
        if c['registry'].address!=r.address or c['common'].address!=common.address:raise ValueError('CONTINUITY_GRAPH')
        checked=binding(c,identifier)
        catalog.alias(client.w3,s,checked['script'][2:],identifier.hex())
        bindings.append(dict(intent_id=identifier.hex(),intent=checked['intent'].encode().hex(),script=checked['script'].hex(),
            recipient=checked['recipient'],validation_provenance=checked['provenance'].hex()))
    count=int(read(common,n,'backingCount'))
    if count>max_entries:raise ValueError('CONTINUITY_INVENTORY_LIMIT')
    backing=[];total=0
    from eth_abi import encode
    from eth_utils import keccak
    for index in range(count):
        txid,vout,amount=read(common,n,'backingAt',index);txid=bytes(txid);amount=int(amount)
        if not 0<amount<=2100000000000000:raise ValueError('CONTINUITY_ACCOUNTING')
        key=keccak(encode(['bytes32','uint32'],[txid,int(vout)]));total+=amount
        backing.append(dict(txid=txid[::-1].hex(),vout=str(vout),satoshis=str(amount),
            script_hash=bytes(read(common,n,'backingScripts',key)).hex(),consumed=bool(read(common,n,'consumed',key)),
            wallet_output=bool(read(common,n,'walletOutput',key)),reservation=bytes(read(batch,n,'reservations',keccak(encode(['bytes32','uint32'],[txid[::-1],int(vout)])))).hex()))
    if total!=int(read(common,n,'totalBackingSats')):raise ValueError('CONTINUITY_ACCOUNTING')
    escrow=client.w3.eth.contract(address=s['identity']['escrow'],abi=artifact('BitcoinWithdrawalEscrow')['abi'])
    request_rows={k:Bootstrap.wire(read(escrow,n,'requests',hex32(k))) for k in requests}
    batch_rows={k:dict(batch=Bootstrap.wire(read(batch,n,'batches',hex32(k))),
        reservation_root=bytes(read(batch,n,'reservationRoot',hex32(k))).hex(),
        replacement_of=bytes(read(batch,n,'replacementOf',hex32(k))).hex(),
        versions=str(read(batch,n,'versionCount',hex32(k))),payment=Bootstrap.wire(read(escrow,n,'payment',hex32(k)))) for k in batches}
    try:fees=fee_inventory(client,ctx,s,catalog,max_entries,max_bindings,requests)
    except Exception as exc:
        from .participant_errors import participant_exception_code
        fees=dict(status='OBSERVATION_UNAVAILABLE',reason=participant_exception_code(exc))
    stable(client.w3,s)
    result=dict(schema='satoshifi.bitcoin-continuity-inventory.v1',profile=PROFILE,source=s,observation=observation,
        registered_bindings=bindings,backing=backing,total_backing_sats=str(total),known_requests=request_rows,known_batches=batch_rows,fees=fees,
        history_complete=False,activation_allowed=False,ready=False,production_ready=False,
        non_enumerable=['all consumed/walletOutput keys','all requests/batches and signed versions','all outstanding approvals','unregistered or orphaned/late ingress','external or unknown graphs'],
        meaning='Canonical bounded inventory; zero balances or omitted locator keys never prove an empty graph or authorize migration')
    catalog.lane.put('continuity-inventory:'+digest(result),result)
    return result
