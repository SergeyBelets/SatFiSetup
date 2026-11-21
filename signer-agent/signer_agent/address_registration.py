"""Typed address registration review. Request openings never contain transport/FROST secrets."""
from eth_abi import encode
from hashlib import sha256
from eth_utils import keccak
from eth_utils.abi import function_abi_to_4byte_selector
from web3 import Web3

from .bootstrap import artifact
from .deposit_intent import DepositIntentV1, PROFILE
from .bitcoin_trust import checked_runtime
from .contract_provenance import prove_creation
from .operations import canonical

ZERO="0x"+"00"*20


def read(node,number,method,*args):
    return getattr(node.functions,method)(*args).call(block_identifier=number)


def request_commitment(domain,intent_id,salt,requester,service):
    from .spv_transport import hex32
    return keccak(encode(["bytes32"]*4+["address","address"],[
        keccak(text="SATOHSI_ADDRESS_REQUEST_COMMITMENT_V2"),hex32(domain),hex32(intent_id),hex32(salt),requester,service])).hex()


def batch_id(registry,raws):
    return keccak(encode(["bytes32","address","bytes32[]"],[keccak(text="SATOHSI_ADDRESS_REGISTRATION_BATCH_V1"),
        registry,[sha256(raw).digest() for raw in raws]])).hex()


def node(w3,address,kind,number):
    checked_runtime(w3,address,kind,number)
    prove_creation(w3,address,kind,None,number)
    return w3.eth.contract(address=address,abi=artifact(kind)["abi"])


def authority(w3,ctx,board_address,number,bundle=None):
    """Use the existing enrolled confederation EVM_AUTH and public DKG board."""
    from .onchain_transport import invitation
    from .bitcoin_trust import invitation_options
    board,invite=invitation(w3,board_address)
    from .bootstrap_custody import role_inputs
    roster,threshold=role_inputs(invite,"conf_ceremony")
    r=ctx["registry"];auth=node(w3,read(ctx["pool"],number,"authorization"),"DKGAuthorization",number)
    coordinator=node(w3,read(auth,number,"coordinator"),"FROSTCoordinator",number)
    dkg=node(w3,read(r,number,"dkg"),"DKGCoordinator",number)
    verifier=node(w3,read(coordinator,number,"defaultVerifier"),"FROSTVerifier",number)
    prove_creation(w3,dkg.address,"DKGCoordinator",[],number)
    prove_creation(w3,verifier.address,"FROSTVerifier",[],number)
    identifier=bytes(read(auth,number,"dkgId"));result=bytes(read(auth,number,"resultHash"))
    completed=read(dkg,number,"getCompletedResult",identifier)
    if "bitcoin_custody" in invite and read(board,number,"intentCoordinator")!=coordinator.address:
        raise ValueError("ADDRESS_REGISTRATION_AUTHORITY")
    if (read(board,number,"registry")!=read(r,number,"dkg")
            or read(coordinator,number,"dkgRegistry")!=read(r,number,"dkg")
            or read(auth,number,"registry")!=read(r,number,"dkg")
            or invite["sessions"]["conf_ceremony"]!=identifier.hex()
            or invite["sessions"]["btc_ceremony"]!=bytes(read(r,number,"walletId")).hex()
            or list(roster)!=list(read(auth,number,"getParticipants"))
            or int(threshold)!=int(read(auth,number,"threshold"))
            or bytes(completed[1])!=result or bytes(completed[3])!=keccak(text="EVM_AUTH")
            or bytes(completed[4])!=bytes(read(auth,number,"groupPubkey"))
            or list(completed[5])!=list(roster) or int(completed[6])!=int(threshold)):
        raise ValueError("ADDRESS_REGISTRATION_AUTHORITY")
    if bundle is not None:
        from .dkg_protocol import Manifest
        m=Manifest.from_dict(bundle)
        if (bundle.get("onchain_board")!=board.address or bundle.get("role")!="conf_ceremony"
                or bundle.get("onboarding",{}).get("invitation")!=invite
                or m.chain_id!=w3.eth.chain_id or m.context!=bytes(completed[0])
                or m.purpose!="EVM_AUTH" or m.session_id!=identifier or m.registry!=read(r,number,"dkg")
                or list(m.participants)!=list(roster) or m.threshold!=int(threshold)):
            raise ValueError("ADDRESS_REGISTRATION_ENROLLMENT")
    return dict(board=board,invite=invite,options=invitation_options(invite),authorization=auth,
        coordinator=coordinator,dkg_id=identifier,result_hash=result)


def request_proof(w3,ctx,raw,proof,number):
    """The requester's canonical EVM send commits to these exact intent bytes."""
    from .spv_transport import hex32,route_key,message_key
    expected={"board","message_id","opening"}
    if isinstance(proof,dict) and 'recipient_choice' in proof:expected.add('recipient_choice')
    if not isinstance(proof,dict) or set(proof)!=expected:raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
    intent=DepositIntentV1.decode(raw)
    board=node(w3,proof["board"],"BitcoinAddressMessageBoard",number)
    prove_creation(w3,board.address,"BitcoinAddressMessageBoard",[ctx["registry"].address],number)
    r=ctx["registry"];auth=read(ctx["pool"],number,"authorization")
    domain=keccak(encode(["bytes32","uint256","address","address","bytes32","address"],[
        keccak(text="SATOHSI_ADDRESS_MESSAGES_V1"),w3.eth.chain_id,board.address,r.address,bytes(read(r,number,"graphId")),auth])).hex()
    if (read(board,number,"registry")!=r.address or read(board,number,"authorization")!=auth
            or bytes(read(board,number,"domain")).hex()!=domain):raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
    p=read(board,number,"packet",hex32(proof["message_id"]))
    if not bytes(p[8]) or int(p[1])!=0 or int(p[2])!=0:raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
    route=read(board,number,"route",bytes(p[0]))
    if (p[3]!=Web3.to_checksum_address(intent.requester) or p[3]!=route[0] or p[4]!=route[1]
            or not read(board,number,"serviceAllowed",p[4])
            or bytes(p[0]).hex()!=route_key(bytes(route[2]).hex(),p[3],p[4])
            or proof["message_id"]!=message_key(bytes(p[0]).hex(),"REQUEST","0")
            or bytes(route[2]).hex()!=request_commitment(domain,intent.intent_id.hex(),proof["opening"],p[3],p[4])):
        raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
    return True  # Delivery expiry does not cancel an authenticated immutable intent.


def complete(w3,ctx,raws,number):
    from .address_mint import binding
    r=ctx["registry"];done=True
    for raw in raws:
        identifier=DepositIntentV1.decode(raw).intent_id
        row=read(r,number,"getBinding",identifier)
        if not bytes(row[0]):done=False;continue
        b=binding(ctx,identifier)
        if b["intent"].encode()!=raw:raise ValueError("ADDRESS_REGISTRATION_BINDING_CONFLICT")
    return done


def fence(w3,ctx,cg,dg,deadline,sequence,number):
    r=ctx["registry"]
    block=w3.eth.get_block(number)
    if (int(read(r,number,"custodyGeneration"))!=cg or int(read(r,number,"derivationGeneration"))!=dg
            or int(read(r,number,"custodyState"))!=0 or int(read(r,number,"derivationState"))!=0
            or bool(read(r,number,"issuanceClosed")) or int(read(r,number,"registrationNonce"))!=sequence):
        raise ValueError("ADDRESS_REGISTRATION_FENCED")
    if int(block.timestamp)>deadline:raise ValueError("ADDRESS_REGISTRATION_EXPIRED")
    verifier=w3.eth.contract(address=read(r,number,"verifier"),abi=artifact("BitcoinDepositDerivation")["abi"])
    if (keccak(bytes(w3.eth.get_code(verifier.address,block_identifier=number)))!=bytes(read(r,number,"verifierRuntimeHash"))
            or bytes(read(verifier,number,"PROFILE"))!=keccak(text=PROFILE) or read(verifier,number,"QUALIFIED") is not True):
        raise ValueError("BTC_ADDRESS_VERIFIER_UNQUALIFIED")


def operation_hash(w3,ctx,auth,raws,cg,dg,deadline,sequence,number):
    r=ctx["registry"]
    params=keccak(encode(["bytes32","bytes32","bytes32","bytes[]","uint256","uint256","uint64"],[
        keccak(text="SATOHSI_ADDRESS_REGISTER_V1"),bytes(read(r,number,"graphId")),
        bytes(read(r,number,"derivationScope")),raws,cg,dg,deadline]))
    method=next(x for x in artifact("BitcoinAddressRegistry")["abi"] if x.get("name")=="registerBatch")
    return keccak(encode(["string","uint256","address","address","bytes4","bytes32","uint256"],[
        "SATOHSI_FROST_AUTHORIZATION_V1",w3.eth.chain_id,auth.address,r.address,
        function_abi_to_4byte_selector(method),params,sequence]))


def pending(w3,ctx,raws,proofs,cg,dg,deadline,sequence,number):
    fence(w3,ctx,cg,dg,deadline,sequence,number)
    if not isinstance(proofs,list) or len(proofs)!=len(raws):raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
    from .address_transport import Client
    public=object.__new__(Client);public.w3=w3
    block=w3.eth.get_block(number)
    for raw,proof in zip(raws,proofs):
        decoded=DepositIntentV1.decode(raw)
        if bytes(read(ctx["registry"],number,"getBinding",decoded.intent_id)[0]):continue
        public.validate_intent(decoded,ctx,block,selection=proof.get('recipient_choice'))
        decoded.derive(b"\x02"+bytes(read(ctx["registry"],number,"baseX")))
        request_proof(w3,ctx,raw,proof,number)


def review(w3,intent,bundle,number,*,session=None,executable=True,published=True):
    if isinstance(intent,dict) and intent.get('hash_method')=='getStatusOperationHash':
        from .address_status import review as status_review
        return status_review(w3,intent,bundle,number,session=session,executable=executable,published=published)
    from .address_mint import configuration
    from .bitcoin_onchain_plan import decimal,hex_value
    expected={"chain_id","coordinator","role","target","artifact","hash_method","hash_args","session","address_registration" ,"operation_hash"}
    if not isinstance(intent,dict) or set(intent)!=expected:raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
    meta=intent["address_registration"]
    if not isinstance(meta,dict) or set(meta)!={"version","pool","registration_board","batch_id","attempt","requests"} or meta["version"]!="1":
        raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
    args=intent["hash_args"]
    if not isinstance(args,list) or len(args)!=5 or not isinstance(args[0],list) or not 1<=len(args[0])<=8:
        raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
    if any(not isinstance(x,str) or not x.startswith("0x") for x in args[0]):
        raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
    if not isinstance(meta["requests"],list) or len(meta["requests"])!=len(args[0]):
        raise ValueError("ADDRESS_REQUEST_ATTESTATION_REQUIRED")
    raws=[hex_value(x[2:],382) for x in args[0]]
    identifiers=[DepositIntentV1.decode(raw).intent_id for raw in raws]
    if len(set(identifiers))!=len(raws):raise ValueError("ADDRESS_REGISTRATION_DUPLICATE")
    cg,dg=[decimal(x,2**256-1,positive=True) for x in args[1:3]]
    deadline=decimal(args[3],2**64-1,positive=True);sequence=decimal(args[4],2**256-1)
    if meta["batch_id"]!=batch_id(intent["target"],raws) or not 1<=decimal(meta["attempt"],8,positive=True)<=8:
        raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
    pool=w3.eth.contract(address=meta["pool"],abi=artifact("MiningPoolV1")["abi"])
    block=w3.eth.get_block(number);ctx=configuration(w3,pool,block.number);r=ctx["registry"]
    a=authority(w3,ctx,meta["registration_board"],block.number,bundle)
    digest=operation_hash(w3,ctx,a["authorization"],raws,cg,dg,deadline,sequence,block.number)
    if (intent["chain_id"]!=str(w3.eth.chain_id) or intent["role"]!="conf_ceremony"
            or intent["artifact"]!="BitcoinAddressRegistry" or intent["hash_method"]!="getRegisterOperationHash"
            or intent["target"]!=r.address or intent["coordinator"]!=a["coordinator"].address
            or intent["operation_hash"]!=digest.hex()
            or bytes(read(r,block.number,"getRegisterOperationHash",raws,cg,dg,deadline,sequence))!=digest):
        raise ValueError("ADDRESS_REGISTRATION_HASH_MISMATCH")
    sid=decimal(intent["session"],2**256-1,positive=True)
    if session is not None and int(session)!=sid:raise ValueError("ADDRESS_REGISTRATION_SESSION_MISMATCH")
    if published and bytes(read(a["board"],block.number,"intents",sid))!=canonical(intent).encode():
        raise ValueError("ADDRESS_REGISTRATION_PUBLIC_INTENT_REQUIRED")
    info=read(a["coordinator"],block.number,"getSession",sid)
    from .onchain_transport import can_publish_intent
    if (int(info[0])!=sid or not can_publish_intent(a["board"],info[1],block.number) or bytes(info[3])!=digest
            or not info[4] or not info[8] or info[9]!=ZERO or info[15]!=r.address
            or bytes(read(a["coordinator"],block.number,"sessionDKGId",sid))!=a["dkg_id"]
            or bytes(read(a["coordinator"],block.number,"sessionDKGResult",sid))!=a["result_hash"]):
        raise ValueError("ADDRESS_REGISTRATION_SESSION_MISMATCH")
    finished=complete(w3,ctx,raws,block.number)
    if executable and not finished:
        if int(info[10]) not in (1,2):raise ValueError("ADDRESS_REGISTRATION_SESSION_UNAVAILABLE")
        if int(info[10])==1 and int(info[7])!=deadline:raise ValueError("ADDRESS_REGISTRATION_DEADLINE_CHANGED")
        pending(w3,ctx,raws,meta["requests"],cg,dg,deadline,sequence,block.number)
    if w3.eth.get_block(block.number).hash!=block.hash:raise ValueError("ADDRESS_EVM_REORG")
    return dict(context=ctx,authority=a,arguments=[raws,cg,dg,deadline,sequence],operation_hash=digest,
        session=sid,state=int(info[10]),complete=finished,anchor=block)


def guard_signing_session(client,session):
    if session.origin_contract==ZERO:return
    anchor=getattr(client,"_read_snapshot",None) or client.w3.eth.get_block("latest")
    try:checked_runtime(client.w3,session.origin_contract,"BitcoinAddressRegistry",anchor.number)
    except ValueError as exc:
        if str(exc)=="BTC_CONTRACT_BUILD_MISMATCH":return
        raise
    family=getattr(client,'family_registry_context',None)
    if family is not None:
        intent,_=family
        if (intent.get('artifact')!='BitcoinAddressRegistry' or intent.get('hash_method')!='getBindFamilyRegistryOperationHash'
                or intent.get('target')!=session.origin_contract):raise ValueError('FAMILY_ROOT_INTENT')
        from .family_registry import signing_guard
        signing_guard(client,getattr(client,'family_registry_state',None),session)
        return
    incident=getattr(client,'incident_status_context',None)
    if incident is not None:
        from .address_status import guard_signing_session as guard_status
        guard_status(client,session,incident)
        return
    payload=getattr(client,"address_registration_context",None)
    if payload is None:raise ValueError("ADDRESS_REGISTRATION_INTENT_REQUIRED")
    intent,bundle=payload
    if intent.get("operation_hash")!=session.message_hash.hex():raise ValueError("ADDRESS_REGISTRATION_HASH_MISMATCH")
    for number in (anchor.number,"latest"):
        record=review(client.w3,intent,bundle,number,session=session.session_id)
        if record["complete"]:raise ValueError("ADDRESS_REGISTRATION_COMPLETE")
        if record["authority"]["coordinator"].address!=client.address:raise ValueError("ADDRESS_REGISTRATION_AUTHORITY")
    if client.w3.eth.get_block(anchor.number).hash!=anchor.hash:raise ValueError("ADDRESS_EVM_REORG")
    client.continuity_context=(record['context'],'ISSUE',session.session_id)
    client.check_read_snapshot()
