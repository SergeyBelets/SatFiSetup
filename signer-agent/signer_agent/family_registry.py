"""Independent canonical project root. Invitations never install or replace this pin.

This is one EVM-chain trust domain, not a cross-chain key-migration protocol.
The immutable pin is retained in the original EOA/spent journals as well as the
operator-owned policy file. Restoring one older journal cannot authorize a reset.
"""
import json
from pathlib import Path
from web3 import Web3
from .bootstrap import artifact
from .operations import canonical

FIELDS={'version','chain_id','address','runtime_hash','initial_authority','dkg','coordinator'}


def validate_enrollment(p):
    if isinstance(p,dict) and p.get('version')=='create-1':
        expected='0x'+Web3.keccak(hexstr=artifact('BitcoinKeyFamilyRegistry')['bytecode']['object']).hex().removeprefix('0x')
        if set(p)!={'version','creation_code_hash'} or p['creation_code_hash']!=expected:raise ValueError('FAMILY_ROOT_POLICY')
        return p
    return validate(p)


def enrolled_pin(w3,r,enrollment,n):
    """Resolve only a previously enrolled creation policy, never install a pin."""
    validate_enrollment(enrollment)
    if enrollment['version']!='create-1':return enrollment
    root=r.functions.familyRegistry().call(block_identifier=n)
    auth=r.functions.authorization().call(block_identifier=n)
    p=dict(version='1',chain_id=str(w3.eth.chain_id),address=root,initial_authority=auth,
        runtime_hash='0x'+Web3.keccak(w3.eth.get_code(root,block_identifier=n)).hex().removeprefix('0x'),
        dkg=r.functions.dkg().call(block_identifier=n),coordinator=w3.eth.contract(address=auth,abi=artifact('DKGAuthorization')['abi']).functions.coordinator().call(block_identifier=n))
    inspect(w3,p,n);return p


def policy_for_intent(w3,intent,enrollment,n):
    validate_enrollment(enrollment)
    if enrollment['version']!='create-1':return enrollment
    if intent['artifact']=='BitcoinAddressRegistry':
        root_address=intent['hash_args'][0];graph=intent['target']
    else:
        root_address=intent['target'];graph=intent['hash_args'][0] if intent['hash_method']=='getEnrollOperationHash' else None
    if intent['artifact']=='BitcoinKeyFamilyRegistry':
        r=w3.eth.contract(address=root_address,abi=artifact('BitcoinKeyFamilyRegistry')['abi']);auth=r.functions.governance().call(block_identifier=n)
    elif graph:
        r=w3.eth.contract(address=graph,abi=artifact('BitcoinAddressRegistry')['abi']);auth=r.functions.authorization().call(block_identifier=n)
    else:
        r=w3.eth.contract(address=root_address,abi=artifact('BitcoinKeyFamilyRegistry')['abi']);auth=r.functions.governance().call(block_identifier=n)
    a=w3.eth.contract(address=auth,abi=artifact('DKGAuthorization')['abi'])
    return dict(version='1',chain_id=str(w3.eth.chain_id),address=root_address,initial_authority=auth,
        runtime_hash='0x'+Web3.keccak(w3.eth.get_code(root_address,block_identifier=n)).hex().removeprefix('0x'),
        dkg=a.functions.registry().call(block_identifier=n),coordinator=a.functions.coordinator().call(block_identifier=n))


def validate(p):
    if not isinstance(p,dict) or set(p)!=FIELDS or p['version']!='1':raise ValueError('FAMILY_ROOT_POLICY')
    if not isinstance(p['chain_id'],str) or not p['chain_id'].isdecimal() or str(int(p['chain_id']))!=p['chain_id'] or int(p['chain_id'])<1:
        raise ValueError('FAMILY_ROOT_POLICY')
    for k in ('address','initial_authority','dkg','coordinator'):
        if not Web3.is_checksum_address(p[k]) or int(p[k],16)==0:raise ValueError('FAMILY_ROOT_POLICY')
    h=p['runtime_hash']
    if not isinstance(h,str) or len(h)!=66 or not h.startswith('0x') or any(c not in '0123456789abcdef' for c in h[2:]) or int(h,16)==0:
        raise ValueError('FAMILY_ROOT_POLICY')
    return p


def inspect(w3,p,n):
    from .contract_provenance import prove_creation
    if type(n) is not int:n=w3.eth.get_block(n).number
    validate(p)
    if str(w3.eth.chain_id)!=p['chain_id']:raise ValueError('FAMILY_ROOT_CHAIN')
    code=bytes(w3.eth.get_code(p['address'],block_identifier=n))
    if not code or '0x'+Web3.keccak(code).hex().removeprefix('0x')!=p['runtime_hash']:raise ValueError('FAMILY_ROOT_CODE')
    prove_creation(w3,p['address'],'BitcoinKeyFamilyRegistry',[p['initial_authority']],n)
    root=w3.eth.contract(address=p['address'],abi=artifact('BitcoinKeyFamilyRegistry')['abi'])
    def get(k):return getattr(root.functions,k)().call(block_identifier=n)
    if get('chainId')!=int(p['chain_id']) or get('dkg')!=p['dkg'] or get('coordinator')!=p['coordinator']:
        raise ValueError('FAMILY_ROOT_IDENTITY')
    return root


def declared(r):
    # Real contracts always have an ABI. Pure component fixtures without one
    # model the legacy boundary explicitly; they cannot qualify a real graph.
    return any(x.get('name')=='familyRegistry' for x in getattr(r,'abi',[]))


def pin_path(identity):return Path(str(Path(identity).resolve())+'.bitcoin-family-root.json')


def load_pin(catalog,chain):
    values=[]
    if catalog.lane is not None:
        from .onboarding import read_private
        p=pin_path(catalog.identity_file)
        if p.exists():values.append(validate(json.loads(read_private(p))))
    with catalog.databases() as sources:
        for db in sources:
            row=db.execute('SELECT body FROM btc_family_root WHERE chain=?',(chain,)).fetchone()
            if row:values.append(validate(json.loads(row[0])))
    if not values:raise ValueError('FAMILY_ROOT_PIN_REQUIRED')
    if any(v!=values[0] for v in values) or values[0]['chain_id']!=chain:raise ValueError('FAMILY_ROOT_PIN_CHANGED')
    return values[0]


def retain(catalog,p):
    validate(p)
    with catalog.databases() as sources:
        for db in sources:
            old=db.execute('SELECT body FROM btc_family_root WHERE chain=?',(p['chain_id'],)).fetchone()
            if old and json.loads(old[0])!=p:raise ValueError('FAMILY_ROOT_PIN_CHANGED')
            db.execute('INSERT OR IGNORE INTO btc_family_root VALUES(?,?)',(p['chain_id'],canonical(p)))


def guard(catalog,w3,s,mode):
    if 'family_root' not in s['identity'] or mode in ('READ','IDENTITY'):return
    p=load_pin(catalog,s['identity']['chain_id']);r=s['identity']['registry']
    if p['address']!=s['identity']['family_root']:raise ValueError('FAMILY_ROOT_PIN_CHANGED')
    derived=mode in ('ISSUE','ADDRESS_MINT','DERIVED_SIGN')
    for anchor in ('anchor','latest_anchor'):
        n=int(s[anchor]['number']);root=inspect(w3,p,n)
        if bytes(root.functions.requireGraph(r).call(block_identifier=n)).hex()!=s['family']:raise ValueError('FAMILY_ROOT_GRAPH')
        root.functions.fence(r,derived,mode=='ISSUE').call(block_identifier=n)
    retain(catalog,p)


def activation(catalog,w3,old,s):
    """One-time original-graph enrollment, never a new accounting identity."""
    a=old['identity'];b=s['identity'];new=b.get('family_root')
    if not new or int(new,16)==0 or a.get('family_root') not in (None,'0x'+'0'*40):return False
    if {k:v for k,v in a.items() if k!='family_root'}!={k:v for k,v in b.items() if k!='family_root'}:return False
    p=load_pin(catalog,b['chain_id'])
    if p['address']!=new:raise ValueError('FAMILY_ROOT_PIN_CHANGED')
    for key in ('anchor','latest_anchor'):
        n=int(s[key]['number']);root=inspect(w3,p,n)
        if bytes(root.functions.requireGraph(b['registry']).call(block_identifier=n)).hex()!=s['family']:raise ValueError('FAMILY_ROOT_GRAPH')
    return True


def install(w3,catalog,p,n):
    """Explicit local operator action; does not follow an invitation or clear state."""
    inspect(w3,p,n)
    if catalog.identity_file:
        from .dkg import atomic_private
        path=pin_path(catalog.identity_file)
        if path.exists():
            from .onboarding import read_private
            if json.loads(read_private(path))!=p:raise ValueError('FAMILY_ROOT_PIN_CHANGED')
        # Retain first: a crash before file creation can only require recovery.
        retain(catalog,p)
        if not path.exists():atomic_private(str(path),(canonical(p)+'\n').encode())
    else:retain(catalog,p)
    return dict(status='FAMILY_ROOT_PINNED',chain_id=p['chain_id'],address=p['address'],ready=False)


def review(w3,intent,bundle,n,p):
    """Typed enrollment/binding review, repeated before every private boundary."""
    from .contract_provenance import prove_creation
    validate(p);root=inspect(w3,p,n)
    enrolled=bundle.get('onboarding',{}).get('invitation',{}).get('withdrawal_fee',{})
    declared_policy=enrolled.get('family_root')
    if enrolled.get('version')!='2':raise ValueError('FAMILY_ROOT_ENROLLMENT')
    validate_enrollment(declared_policy)
    if declared_policy['version']!='create-1' and declared_policy!=p:raise ValueError('FAMILY_ROOT_ENROLLMENT')
    pair=(intent.get('artifact'),intent.get('hash_method'))
    allowed={('BitcoinKeyFamilyRegistry','getEnrollOperationHash'),('BitcoinKeyFamilyRegistry','getRotateOperationHash'),('BitcoinAddressRegistry','getBindFamilyRegistryOperationHash')}
    if pair not in allowed or intent.get('role')!='conf_ceremony' or intent.get('chain_id')!=p['chain_id']:
        raise ValueError('FAMILY_ROOT_INTENT')
    args=intent.get('hash_args')
    if not isinstance(args,list) or len(args)!=1 or not Web3.is_checksum_address(args[0]):raise ValueError('FAMILY_ROOT_INTENT')
    rotate=pair[1]=='getRotateOperationHash'
    address=args[0] if pair[0]=='BitcoinKeyFamilyRegistry' else intent['target']
    if rotate:
        r=w3.eth.contract(address=address,abi=artifact('DKGAuthorization')['abi'])
        prove_creation(w3,address,'DKGAuthorization',None,n)
        if r.functions.registry().call(block_identifier=n)!=p['dkg'] or r.functions.coordinator().call(block_identifier=n)!=p['coordinator']:raise ValueError('FAMILY_ROOT_GRAPH')
    else:
        r=w3.eth.contract(address=address,abi=artifact('BitcoinAddressRegistry')['abi'])
        prove_creation(w3,address,'BitcoinAddressRegistry',None,n)
        graph_dkg=r.functions.dkg().call(block_identifier=n)
        prove_creation(w3,graph_dkg,'DKGCoordinator',[],n)
        graph_auth=r.functions.authorization().call(block_identifier=n)
        prove_creation(w3,graph_auth,'DKGAuthorization',None,n)
        from .bootstrap_withdrawal_fee import genesis
        network=bundle.get('onboarding',{}).get('invitation',{}).get('bitcoin_network')
        if network not in ('mainnet','testnet4','regtest') or bytes(r.functions.bitcoinGenesis().call(block_identifier=n)).hex()!=genesis(network)[2:]:raise ValueError('FAMILY_ROOT_GRAPH')
        # Binding under a create invitation must use that graph's original key.
        # Later enrollments of independent graphs are instead signed by the
        # pinned root governance, authenticated against its original DKG below.
        if (declared_policy['version']=='create-1' and pair[0]=='BitcoinAddressRegistry'
                and r.functions.authorization().call(block_identifier=n)!=p['initial_authority']):raise ValueError('FAMILY_ROOT_ENROLLMENT')
    if pair[0]=='BitcoinKeyFamilyRegistry':
        if intent['target']!=root.address:raise ValueError('FAMILY_ROOT_INTENT')
        target=root
        complete=bool(root.functions.receipts(bytes.fromhex(intent['operation_hash'].removeprefix('0x'))).call(block_identifier=n))
        if complete:
            op=bytes.fromhex(intent['operation_hash'].removeprefix('0x'))
            if root.functions.receiptTarget(op).call(block_identifier=n)!=address or root.functions.receiptKind(op).call(block_identifier=n)!=(2 if rotate else 1):
                raise ValueError('FAMILY_ROOT_HASH')
    else:
        if args!=[p['address']]:raise ValueError('FAMILY_ROOT_INTENT')
        root.functions.requireGraph(address).call(block_identifier=n);target=r
        complete=bytes(r.functions.familyBindReceipt().call(block_identifier=n)).hex()==intent['operation_hash'].removeprefix('0x')
        if complete and r.functions.familyRegistry().call(block_identifier=n)!=p['address']:raise ValueError('FAMILY_ROOT_GRAPH')
    # Completed exact receipts are inspectable after nonce/governance advances.
    if not complete and bytes(getattr(target.functions,pair[1])(*args).call(block_identifier=n)).hex()!=intent['operation_hash'].removeprefix('0x'):
        raise ValueError('FAMILY_ROOT_HASH')
    if not complete:
        from .dkg_protocol import Manifest
        m=Manifest.from_dict(bundle)
        authority=root.functions.governance().call(block_identifier=n) if pair[0]=='BitcoinKeyFamilyRegistry' else r.functions.authorization().call(block_identifier=n)
        auth=w3.eth.contract(address=authority,abi=artifact('DKGAuthorization')['abi'])
        prove_creation(w3,authority,'DKGAuthorization',None,n)
        if (m.purpose!='EVM_AUTH' or m.chain_id!=w3.eth.chain_id or m.registry!=auth.functions.registry().call(block_identifier=n)
                or m.session_id!=bytes(auth.functions.dkgId().call(block_identifier=n)) or intent['coordinator']!=auth.functions.coordinator().call(block_identifier=n)):
            raise ValueError('FAMILY_ROOT_ENROLLMENT')
        dkg=w3.eth.contract(address=m.registry,abi=artifact('DKGCoordinator')['abi']);completed=dkg.functions.getCompletedResult(m.session_id).call(block_identifier=n)
        if (bytes(completed[0])!=m.context or bytes(completed[1])!=bytes(auth.functions.resultHash().call(block_identifier=n))
                or bytes(completed[3])!=Web3.keccak(text='EVM_AUTH') or list(completed[5])!=list(m.participants) or int(completed[6])!=m.threshold):
            raise ValueError('FAMILY_ROOT_ENROLLMENT')
    return dict(complete=complete,registry=address)


def signing_guard(client,state,session):
    payload=getattr(client,'family_registry_context',None)
    if payload is None:
        if session.origin_contract=='0x'+'00'*20:return
        from .bitcoin_trust import checked_runtime
        n=(getattr(client,'_read_snapshot',None) or client.w3.eth.get_block('latest')).number
        try:checked_runtime(client.w3,session.origin_contract,'BitcoinKeyFamilyRegistry',n)
        except ValueError as e:
            if str(e)=='BTC_CONTRACT_BUILD_MISMATCH':return
            raise
        raise ValueError('FAMILY_ROOT_INTENT')
    from .bitcoin_continuity import Catalog
    intent,bundle=payload
    if (str(session.session_id)!=intent['session'] or session.origin_contract!=intent['target']
            or session.message_hash.hex().removeprefix('0x')!=intent['operation_hash'].removeprefix('0x')):raise ValueError('FAMILY_ROOT_INTENT')
    catalog=Catalog(identity_file=getattr(client,'continuity_identity_file',None),state=state)
    p=load_pin(catalog,str(client.w3.eth.chain_id))
    n=(getattr(client,'_read_snapshot',None) or client.w3.eth.get_block('latest')).number
    for number in (n,client.w3.eth.get_block('latest').number):
        result=review(client.w3,intent,bundle,number,p)
        if result['complete']:raise ValueError('FAMILY_ROOT_EXECUTED')
    retain(catalog,p)


def enrollment_plan(w3,p,bundle,registry,deadline,evm_rpc,confirmations):
    """Prepare a normal Bootstrap recipe for the ORIGINAL root authority.

    This never sends or approves. New graph authorities cannot substitute
    themselves for the root governance; both independent quorums retain their
    ordinary DKG/FROST authorization and original transaction journals.
    """
    from .dkg_protocol import Manifest
    from .onboarding import validate_invite
    from .evm_network import read_block
    m=Manifest.from_dict(bundle);invite=validate_invite(bundle['onboarding']['invitation'])
    b=read_block(w3,int(confirmations));root=inspect(w3,p,b.number)
    from .onchain_transport import invitation
    board,enrolled=invitation(w3,bundle.get('onchain_board'))
    if enrolled!=invite or board.functions.registry().call(block_identifier=b.number)!=m.registry or board.functions.operator().call(block_identifier=b.number)!=invite['operator']:
        raise ValueError('FAMILY_ROOT_ENROLLMENT')
    auth_address=root.functions.governance().call(block_identifier=b.number)
    auth=w3.eth.contract(address=auth_address,abi=artifact('DKGAuthorization')['abi'])
    if (m.purpose!='EVM_AUTH' or m.chain_id!=w3.eth.chain_id or m.registry!=p['dkg'] or bundle.get('role')!='conf_ceremony'
            or auth.functions.dkgId().call(block_identifier=b.number)!=m.session_id or not bundle.get('onchain_board')
            or invite.get('evm_confirmations')!=confirmations):raise ValueError('FAMILY_ROOT_ENROLLMENT')
    from .spv_transport import uint
    expires=uint(deadline,b.timestamp+1,b.timestamp+604800)
    if bytes(root.functions.registryFamily(registry).call(block_identifier=b.number))!=bytes(32):
        root.functions.requireGraph(registry).call(block_identifier=b.number)
        return dict(status='ALREADY_ENROLLED',registry=registry,root=p['address'],ready=False)
    op=bytes(root.functions.getEnrollOperationHash(registry).call(block_identifier=b.number)).hex()
    public=dict(chain_id=p['chain_id'],coordinator=p['coordinator'],role='conf_ceremony',target=p['address'],
        artifact='BitcoinKeyFamilyRegistry',hash_method='getEnrollOperationHash',hash_args=[registry],operation_hash=op,session='$family_session')
    # Validate canonical constructors, network and the enrolled root policy now;
    # original participants repeat it with the real session before any nonce.
    review(w3,dict(public,session='1'),bundle,b.number,p)
    profile=dict(deployment_id='family-enroll-'+hashlib_sha([p['address'],registry,op,expires]),chain_id=p['chain_id'],evm_rpc=evm_rpc,
        evm_confirmations=confirmations,mode='backed' if invite['bitcoin_network']!='regtest' or p['chain_id']=='1' else 'integration',
        bitcoin_network=invite['bitcoin_network'],contracts=dict(root=dict(address=p['address'],artifact='BitcoinKeyFamilyRegistry'),
            coordinator=dict(address=p['coordinator'],artifact='FROSTCoordinator'),authorization=dict(address=auth_address,artifact='DKGAuthorization'),
            board=dict(address=bundle['onchain_board'],artifact='DKGOnchainBoard')),steps=[])
    if invite.get('bitcoin_policy') is not None:profile['bitcoin_policy']=invite['bitcoin_policy']
    profile['steps']=[
        dict(id='create',kind='call',contract='coordinator',method='createSessionFromDKG',args=['0x'+m.session_id.hex(),'0x'+op,p['address'],str(expires)],
            outputs=[dict(name='family_session',contract='coordinator',event='SessionCreated',field='sessionId')]),
        dict(id='publish',kind='call',contract='board',method='publishIntent',args=['$family_session',{'$json_hex':public}]),
        dict(id='participants',kind='wait',contract='coordinator',method='getAuthorizationResult',args=['$family_session'],index=['2'],equals=True,pending_status='AWAITING_PARTICIPANTS'),
        dict(id='authorize',kind='call',contract='authorization',method='authorizeOperation',args=['$family_session','0x'+op,p['address']]),
        dict(id='enroll',kind='call',contract='root',method='enroll',args=[registry,'0x'+op],checks=[dict(contract='root',method='receipts',args=['0x'+op],equals=True)])]
    if w3.eth.get_block(b.number).hash!=b.hash:raise ValueError('CONTINUITY_REORG')
    return profile


def hashlib_sha(value):
    import hashlib
    return hashlib.sha256(canonical(value).encode()).hexdigest()
