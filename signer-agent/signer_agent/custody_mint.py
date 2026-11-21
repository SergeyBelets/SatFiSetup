"""SMP1/coinbase mint on V2 custody graphs, with original admission/history engines."""
from eth_abi import encode
from eth_utils import keccak

from .bootstrap import Bootstrap
from . import address_mint as address,mint_admission as admission,mint_admission_client as sdk
from .bitcoin_funding import _mint_complete,_mint_proof
from .bitcoin_trust import confirmed_block,check_bitcoin_graph


def complete(pool,contracts,args,block):
    if not _mint_complete(pool,args,block):return False
    key=keccak(encode(['bytes32','uint32'],args[:2]))
    if (bytes(pool.functions.mintedAddressIntents(key).call(block_identifier=block.number))!=bytes(32)
            or not contracts['deposits'].functions.consumed(key).call(block_identifier=block.number)):
        raise ValueError('BTC_MINT_COMPLETION_MODE_MISMATCH')
    return True


def dispatch(a,manifest,key,registry,state,graph,contracts,target,args,block):
    from .bitcoin_onchain_payment import _bitcoin,_auth_board
    from .bitcoin_onchain import send,snapshot_check
    from .bitcoin_continuity import trust_guard
    from .bitcoin_onchain_plan import decimal
    if complete(target,contracts,args,block):
        snapshot_check(registry.w3,block)
        return dict(status='BTC_FUNDING_EXECUTED',operation='mint',target=target.address)
    board_address=target.functions.mintAdmission().call(block_identifier=block.number)
    if getattr(a,'mint_admission',board_address) not in (None,board_address):raise ValueError('BTC_MINT_ADMISSION_GRAPH_MISMATCH')
    ctx=admission.configuration(registry.w3,board_address,block.number,expected_pool=target.address)
    board=ctx['board'];bitcoin=_bitcoin(a,graph);_,role=_auth_board(a,manifest,registry,graph)
    view=address.configuration(registry.w3,target,block.number);r=view['registry']
    if r.address!=graph['contracts']['address_registry']['address']:raise ValueError('BTC_ADDRESS_GRAPH_MISMATCH')
    generation=int(address.read(r,block.number,'custodyGeneration'))
    address.read(r,block.number,'requireCustody',generation);address.read(r,'latest','requireCustody',generation)
    trust_guard(manifest,registry,state,graph,mode='BASE_MINT')
    from .bitcoin_funding import consumer_readiness
    readiness=consumer_readiness(a,graph,bitcoin,target,args)
    if readiness and readiness['status']!='READY':return dict(status='AWAITING_ZK_STATE',spv=readiness)
    deadline=decimal(str(a.deadline),positive=True)
    reference=admission.claim_id(target.address,*args[:2])
    def review():
        current=confirmed_block(registry.w3,graph);view=address.configuration(registry.w3,target,current.number)
        r=view['registry']
        if r.address!=graph['contracts']['address_registry']['address']:raise ValueError('BTC_ADDRESS_GRAPH_MISMATCH')
        generation=int(address.read(r,current.number,'custodyGeneration'))
        address.read(r,current.number,'requireCustody',generation);address.read(r,'latest','requireCustody',generation)
        if int(address.read(r,'latest','custodyGeneration'))!=generation:raise ValueError('BTC_MINT_GENERATION_CHANGED')
        trust_guard(manifest,registry,state,graph,mode='BASE_MINT')
        check_bitcoin_graph(registry,graph,bitcoin)
        proof=_mint_proof(a,bitcoin,contracts,target,args,current)
        snapshot_check(registry.w3,current)
        return current,generation,proof
    block,cg,_=review()
    with state.exclusive():
        claim=address.read(board,block.number,'getClaim',reference)
        if claim[4] and (int(claim[4])!=deadline or int(address.read(board,block.number,'claimModes',reference))!=1
                or Bootstrap.wire(list(claim[:4]))!=Bootstrap.wire(args)):
            raise ValueError('BTC_MINT_ECONOMIC_INTENT_CHANGED')
        if a.action=='btc-mint-retry':
            if not claim[4]:raise ValueError('BTC_MINT_ADMISSION_CLAIM_REQUIRED')
            prior=admission.session_record(registry.w3,board.address,int(claim[6]),block.number,role=role,require_active=False)
            sdk.remember(state,registry,prior)
            if sdk._approved_current(prior):raise ValueError('BTC_MINT_OPERATION_ALREADY_APPROVED_EXECUTE_REQUIRED')
            if deadline<=registry.w3.eth.get_block('latest').timestamp:raise ValueError('BTC_MINT_ORIGINAL_DEADLINE_REQUIRED')
            receipt=send(registry,board.address,'BitcoinMintAdmission','retryGenerationMint',
                [reference,address.read(target,block.number,'nonce'),int(claim[6]),cg,0],graph=graph)
            block=registry.w3.eth.get_block(receipt['blockNumber']);claim=address.read(board,block.number,'getClaim',reference)
        elif not claim[4]:
            if a.action!='btc-mint-propose':raise ValueError('BTC_MINT_ADMISSION_CLAIM_REQUIRED')
            if deadline<=registry.w3.eth.get_block('latest').timestamp:raise ValueError('BTC_MINT_ORIGINAL_DEADLINE_REQUIRED')
            nonce=address.read(target,block.number,'nonce')
            digest=address.read(target,block.number,'getCustodyMintOperationHash',*args,cg)
            if address.read(ctx['authorization'],block.number,'isOperationApproved',digest,target.address):
                raise ValueError('BTC_MINT_OPERATION_ALREADY_APPROVED_LEGACY_RECOVERY_REQUIRED')
            if address.read(target,'latest','nonce')!=nonce:raise ValueError('BTC_MINT_STALE_CONTRACT_NONCE')
            receipt=send(registry,board.address,'BitcoinMintAdmission','openCustodyMint',
                [*args,cg,deadline,nonce],graph=graph)
            block=registry.w3.eth.get_block(receipt['blockNumber']);claim=address.read(board,block.number,'getClaim',reference)
        record=admission.session_record(registry.w3,board.address,int(claim[6]),block.number,role=role,
            require_active=a.action!='btc-mint-execute',signing=a.action!='btc-mint-execute')
        if (record.get('generation_mode')!=1 or record['deadline']!=deadline
                or Bootstrap.wire(record['arguments'])!=Bootstrap.wire([*args,cg])):
            raise ValueError('BTC_MINT_GENERATION_CHANGED')
        sdk.remember(state,registry,record);snapshot_check(registry.w3,block)
        if a.action!='btc-mint-execute':return sdk._result(record)
        record=sdk._execution_record(registry,board.address,int(claim[6]),role)
        current=confirmed_block(registry.w3,graph)
        if complete(target,contracts,args,current):return sdk._result(record,'BTC_FUNDING_EXECUTED')
        current,current_cg,_=review()
        if current_cg!=cg:raise ValueError('BTC_MINT_GENERATION_CHANGED')
        sdk._authorize(registry,graph,record)
        record=sdk._execution_record(registry,board.address,int(claim[6]),role)
        if not sdk._approved_current(record):raise ValueError('BTC_MINT_AUTHORIZATION_NOT_APPROVED')
        current,current_cg,proof=review()
        if current_cg!=cg:raise ValueError('BTC_MINT_GENERATION_CHANGED')
        from .zk_submission import options as proof_submission
        send(registry,target.address,'MiningPoolV1','mintFromProofV2',[*proof,cg,record['operation_hash']],
             graph=graph, **proof_submission(registry, graph, proof[2]))
    current=confirmed_block(registry.w3,graph)
    if not complete(target,contracts,args,current):raise ValueError('BTC_OPERATION_POSTCONDITION')
    snapshot_check(registry.w3,current)
    return sdk._result(record,'BTC_FUNDING_EXECUTED')
