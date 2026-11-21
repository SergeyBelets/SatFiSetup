"""Real local Core funding -> regtest Groth16 -> ordinary adapter/registry/MP mint.

The only test contracts are the explicitly separate one-shot regtest state and
its guest. DKG, FROST authorization, deposit registry, escrow, pool and MP token
are ordinary repository artifacts. No injected verifier, code or monetary state.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from eth_abi import encode
from web3 import Web3
from .state_trial import ROOT, read, write, transact
from .wrap_local import sha
from .deposit_build import HERE, BUILD, NATIVE, CARGO, RUSTC, CARGO_CACHE

ANVIL=Path('/private/tmp/satohsi-tools/anvil')
BITCOIND=Path('/private/tmp/satohsi-tools/bitcoin-30.3/bin/bitcoind')

def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def public(value):
    if isinstance(value,bytes):return '0x'+value.hex()
    if isinstance(value,(list,tuple)):return [public(v) for v in value]
    if isinstance(value,dict):return {k:public(v) for k,v in value.items()}
    return value

def artifact(name):
    custom=HERE/'evidence/contracts'/(name+'.json')
    if custom.is_file():return read(custom)
    paths=list((ROOT/'web3/contracts/out').glob('*/'+name+'.json'))
    if len(paths)!=1:raise ValueError('Artifact not unique: '+name)
    return read(paths[0])

def runtime(w3,name,address):
    a=artifact(name)['deployedBytecode'];expected=bytearray.fromhex(a['object'].removeprefix('0x'))
    code=bytes(w3.eth.get_code(address));masked=bytearray(code)
    if len(masked)!=len(expected) or not code or len(code)>24576:raise ValueError('Runtime size: '+name)
    for refs in a.get('immutableReferences',{}).values():
        values=set()
        for r in refs:
            i,n=r['start'],r['length'];values.add(code[i:i+n]);masked[i:i+n]=bytes(n);expected[i:i+n]=bytes(n)
        if len(values)!=1:raise ValueError('Inconsistent immutables: '+name)
    if masked!=expected:raise ValueError('Ordinary runtime differs: '+name)
    return dict(address=address,runtime_bytes=len(code),runtime_sha256=hashlib.sha256(code).hexdigest(),ordinary_match=True)

def connect(prep):
    if not prep['rpc_url'].startswith('http://127.0.0.1:'):raise ValueError('Loopback required')
    w3=Web3(Web3.HTTPProvider(prep['rpc_url'],request_kwargs={'timeout':30}))
    assert w3.eth.chain_id==31337
    return w3

def contract(w3,name,address):return w3.eth.contract(address=address,abi=artifact(name)['abi'])

def verify_graph(w3,addresses):
    graph={name:runtime(w3,name,address) for name,address in addresses.items()}
    get=lambda name:contract(w3,name,addresses[name])
    state=get('TestRegtestDepositState');adapter=get('ZkSpvAdapter');deposits=get('BitcoinDepositRegistry')
    escrow=get('BitcoinWithdrawalEscrow');mining=get('MiningPoolV1');batches=get('BitcoinBatchRegistry')
    assert state.functions.adapter().call()==adapter.address and adapter.functions.state().call()==state.address
    assert state.functions.verifier().call()==addresses['RiscZeroGroth16Verifier']
    assert not adapter.functions.testHelpersEnabled().call()
    assert deposits.functions.spv().call()==escrow.functions.spv().call()==adapter.address
    assert deposits.functions.spendRecorder().call()==escrow.address
    assert escrow.functions.depositRegistry().call()==deposits.address
    assert mining.functions.deposits().call()==deposits.address and mining.functions.escrow().call()==escrow.address
    assert mining.functions.authorization().call()==deposits.functions.authorization().call()==batches.functions.authorization().call()==addresses['DKGAuthorization']
    assert escrow.functions.batches().call()==batches.address and batches.functions.withdrawalEscrow().call()==escrow.address
    assert mining.functions.mpToken().call()==addresses['PoolMpToken']
    assert deposits.functions.depth().call()==6
    payout=bytes(mining.functions.payoutScript().call())
    assert deposits.functions.poolScripts(mining.address).call()==Web3.keccak(payout)
    assert deposits.functions.scriptPools(Web3.keccak(payout)).call()==mining.address
    return graph

def helpers(rpc):
    # Existing real threshold/DKG harness. No mock signature implementation.
    os.environ['SATOHSI_TEST_RPC']=rpc
    sys.path.insert(0,str(ROOT/'tests/e2e'))
    import test_dkg_processes as dkg
    dkg.RPC=rpc
    from test_bitcoin_processes import authorize
    from lifecycle_helpers import committed_deposit,proof
    return dkg,authorize,committed_deposit,proof

def participant_from(directory,rpc):
    dkg,_,_,_=helpers(rpc);manifest=directory/'manifest.json'
    def participant(i,action,*extra):
        p=directory/str(i+1)
        return dkg.cli(action,'--manifest',manifest,'--rpc',rpc,'--identity-file',p/'identity.json',
            '--state',p/'state.enc','--password-file',p/'password','--mailbox',directory/'mailbox.sqlite',*extra)
    return participant

def stop_nodes(prep):
    results={}
    for name,binary in [('anvil',ANVIL),('bitcoin',BITCOIND)]:
        pid=prep[name+'_pid']
        p=subprocess.run(['ps','-p',str(pid),'-o','command='],text=True,capture_output=True)
        if p.returncode!=0:results[name]='already_stopped';continue
        if not p.stdout.startswith(str(binary)+' '):raise ValueError('PID ownership changed')
        os.kill(pid,signal.SIGTERM)
        for _ in range(100):
            p=subprocess.run(['ps','-p',str(pid),'-o','command='],text=True,capture_output=True)
            if p.returncode!=0:break
            time.sleep(.1)
        else:raise ValueError('Node shutdown incomplete')
        results[name]='stopped'
    return results

def source_snapshot(build):
    sources=dict(build['source_sha256'])
    for name,digest in sources.items():assert sha(ROOT/name)==digest,name
    paths=[HERE/'build-report.json',Path(__file__).resolve(),ROOT/'ops/zkspv/scenario_prove.py']
    for folder,pattern in [(ROOT/'web3/contracts/src','*.sol'),(ROOT/'signer-agent/signer_agent','*.py')]:paths.extend(folder.rglob(pattern))
    paths.extend(ROOT/'tests/e2e'/p for p in ['test_dkg_processes.py','test_bitcoin_processes.py','lifecycle_helpers.py'])
    paths.append(ROOT/'frost-crypto/target/debug/satohsi-frost-crypto')
    for path in paths:sources[str(path.relative_to(ROOT))]=sha(path)
    return sources

def prepare(out,work):
    build=read(HERE/'build-report.json');assert build['status']=='TEST_ONLY_GUEST_BUILD_PASS'
    assert sha(NATIVE/'release/prepare')==build['native_binary_sha256']
    sources=source_snapshot(build);out.mkdir(parents=True,exist_ok=False);work.mkdir(mode=0o700,exist_ok=False)
    ports=[free_port(),free_port()];rpc=f'http://127.0.0.1:{ports[0]}';bitcoin_rpc=f'http://127.0.0.1:{ports[1]}'
    processes=[];prep={}
    try:
        for name,cmd in [('anvil',[ANVIL,'--host','127.0.0.1','--port',str(ports[0]),'--chain-id','31337','--hardfork','osaka','--gas-limit','16777216','--silent']),
                         ('bitcoin',[BITCOIND,'-regtest','-datadir='+str(work),'-server','-listen=0','-connect=0','-dnsseed=0','-discover=0','-txindex=1',
                                     '-rpcbind=127.0.0.1','-rpcallowip=127.0.0.1','-rpcport='+str(ports[1]),'-fallbackfee=0.00002','-printtoconsole=0'])]:
            with (out/(name+'.log')).open('w') as log:p=subprocess.Popen(list(map(str,cmd)),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            processes.append(p);prep[name+'_pid']=p.pid
        prep.update(rpc_url=rpc,bitcoin_rpc_url=bitcoin_rpc,working_directory=str(work))
        write(out/'nodes.json',prep)
        from signer_agent.bitcoin_rpc import BitcoinRPC
        w3=Web3(Web3.HTTPProvider(rpc,request_kwargs={'timeout':30}))
        for _ in range(120):
            if any(p.poll() is not None for p in processes):raise RuntimeError('Local node exited')
            try:
                btc=BitcoinRPC(bitcoin_rpc,work/'regtest/.cookie')
                if w3.is_connected() and btc.call('getblockchaininfo')['chain']=='regtest':break
            except Exception:pass
            time.sleep(.25)
        else:raise RuntimeError('Local nodes unavailable')
        assert w3.eth.chain_id==31337
        btc.check_network();btc.call('createwallet','deposit-e2e')
        wallet=BitcoinRPC(bitcoin_rpc+'/wallet/deposit-e2e',work/'regtest/.cookie')
        reward=wallet.call('getnewaddress')
        # Keep each Core RPC below the ordinary client's bounded timeout.
        for start in range(0,2016,100):btc.call('generatetoaddress',min(100,2016-start),reward)
        assert btc.call('getblockcount')==2016
        initial=[bytes.fromhex(btc.call('getblockheader',btc.call('getblockhash',h),False)) for h in range(2006,2017)]
        cp=btc.call('getblockheader',btc.call('getblockhash',2016),True)
        checkpoint=(2016).to_bytes(8,'big')+bytes.fromhex(cp['chainwork'])+b''.join(initial)
        (out/'checkpoint.bin').write_bytes(checkpoint)
        dkg,authorize,committed_deposit,proof=helpers(rpc)
        addresses={};deployments={};receipts={}
        def deploy(name,*args):
            a=artifact(name);factory=w3.eth.contract(abi=a['abi'],bytecode=a['bytecode']['object'])
            init=factory.constructor(*args);record=transact(w3,init);r=w3.eth.get_transaction_receipt(record['transaction_hash'])
            assert bytes(w3.eth.get_transaction(r.transactionHash)['input'])==bytes.fromhex(init.data_in_transaction.removeprefix('0x'))
            addresses[name]=r.contractAddress;deployments[name]=dict(receipt=record,arguments=public(args),initcode_sha256=hashlib.sha256(bytes(w3.eth.get_transaction(r.transactionHash)['input'])).hexdigest())
            return contract(w3,name,r.contractAddress)
        registry=deploy('DKGCoordinator');verifier=deploy('FROSTVerifier');coordinator=deploy('FROSTCoordinator',verifier.address)
        receipts['bind_dkg']=transact(w3,coordinator.functions.bindDKGRegistry(registry.address))
        evm_dir=work/'evm';evm_dir.mkdir();btc_dir=work/'btc';btc_dir.mkdir()
        print('Running real EVM_AUTH and BTC_SPEND DKG ceremonies',flush=True)
        evm,participant,evm_public=dkg.ceremony(evm_dir,w3,registry,3,2,'EVM_AUTH')
        wallet_manifest,_,btc_public=dkg.ceremony(btc_dir,w3,registry,3,2,'BTC_SPEND')
        auth=deploy('DKGAuthorization',coordinator.address,evm.session_id,bytes.fromhex(evm_public['group'])[1:],list(evm.participants),2)
        state=deploy('TestRegtestDepositState',bytes.fromhex(build['program_image_id']),checkpoint)
        addresses['ZkSpvAdapter']=state.functions.adapter().call();addresses['RiscZeroGroth16Verifier']=state.functions.verifier().call()
        batches=deploy('BitcoinBatchRegistry',auth.address,registry.address,wallet_manifest.session_id)
        escrow=deploy('BitcoinWithdrawalEscrow',batches.address,addresses['ZkSpvAdapter'],6,294,330)
        deposits=deploy('BitcoinDepositRegistry',addresses['ZkSpvAdapter'],auth.address,6,escrow.address)
        def approve(c,name,helper,args):return authorize(w3,coordinator,auth,participant,work,c.address,name,helper,args)
        op=approve(batches,'BitcoinBatchRegistry','getSetWithdrawalEscrowOperationHash',[escrow.address])
        receipts['bind_escrow']=transact(w3,batches.functions.setWithdrawalEscrow(escrow.address,op))
        op=approve(escrow,'BitcoinWithdrawalEscrow','getSetDepositRegistryOperationHash',[deposits.address])
        receipts['bind_deposits']=transact(w3,escrow.functions.setDepositRegistry(deposits.address,op))
        mining=deploy('MiningPoolV1','TEST_ONLY_REGTEST_ZK_DEPOSIT',deposits.address,escrow.address,'Test Regtest Proof MP','TRMP')
        addresses['PoolMpToken']=mining.functions.mpToken().call()
        payout=bytes(mining.functions.payoutScript().call())
        assert payout.hex()=='5120'+btc_public['group'][2:]
        op=approve(deposits,'BitcoinDepositRegistry','getRegisterOperationHash',[mining.address,'0x'+payout.hex()])
        receipts['register_pool']=transact(w3,deposits.functions.register(mining.address,payout,op))
        graph=verify_graph(w3,addresses)
        descriptor=btc.call('getdescriptorinfo','rawtr('+btc_public['group'][2:]+')')['descriptor']
        destination=btc.call('deriveaddresses',descriptor)[0]
        funding=[];sats=1000000;amount=sats*10**10
        for recipient in w3.eth.accounts[:2]:
            commitment=bytes(mining.functions.getDepositData(0,[recipient],[amount]).call())
            txid=committed_deposit(wallet,destination,'0.01000000',commitment.hex())
            funding.append(dict(txid=txid,recipient=recipient,satoshis=sats,mp_amount=amount,vout=0))
        # Obtain real block headers and transaction Merkle paths from Core.
        btc.call('generatetoaddress',6,reward)
        for row in funding:
            raw,block,siblings,index=proof(btc,row['txid'])
            # The parser consumes legacy serialization. rust-bitcoin computes it
            # from this exact signed Core tx, including segwit witness stripping.
            from signer_agent import crypto
            decoded=crypto.call('bitcoin_funding',raw=raw.hex(),txid=row['txid'])
            row.update(raw_tx=raw.hex(),stripped_tx=decoded['stripped'],block_hash=block.hex(),siblings=[s.hex() for s in siblings],index=index)
        headers=[bytes.fromhex(btc.call('getblockheader',btc.call('getblockhash',h),False)) for h in range(2017,2023)]
        body=b'SATOSHIFI_REGTEST_DEPOSIT_V1'.ljust(32,b'\0')+(31337).to_bytes(32,'big')+bytes.fromhex(state.address[2:])+checkpoint+bytes([6])+b''.join(headers)
        (out/'state.input').write_bytes(len(body).to_bytes(4,'little')+body)
        subprocess.run([NATIVE/'release/prepare',out/'state.input',out],check=True)
        # Independently preserve Core's actual serialized blocks for later replay.
        for height in range(2017,2023):
            (out/f'block-{height}.bin').write_bytes(bytes.fromhex(btc.call('getblock',btc.call('getblockhash',height),0)))
        write(out/'funding.json',funding)
        mint=funding[0];args=['0x'+bytes.fromhex(mint['txid'])[::-1].hex(),0,[mint['recipient']],[amount]]
        op=approve(mining,'MiningPoolV1','getMintOperationHash',args)
        mint['operation']='0x'+op.hex();write(out/'funding.json',funding)
        assert not state.functions.isFresh().call() and state.functions.stateVersion().call()==0
        receipts['mint_before_proof_rejected']=transact(w3,mint_call(mining,mint),0)
        assert mining.functions.nonce().call()==0 and auth.functions.approvedOperations(op).call()
        assert deposits.functions.backingSats(outpoint(mint)).call()==0 and not deposits.functions.consumed(outpoint(mint)).call()
        write(out/'preparation.json',dict(prep,schema='satoshifi.regtest-deposit-preparation.v1',status='PREPARED',production_ready=False,
            public_transactions=0,network='regtest',source_sha256=sources,program=build['program'],program_sha256=build['program_sha256'],
            program_image_id=build['program_image_id'],input_sha256=sha(out/'state.input'),journal_sha256=sha(out/'state.journal'),
            checkpoint_sha256=sha(out/'checkpoint.bin'),addresses=addresses,deployments=deployments,graph=graph,checks=receipts,
            input_artifacts={p.name:sha(p) for p in out.iterdir() if p.is_file() and (p.suffix=='.bin' or p.name=='funding.json')},
            evm_manifest=evm.to_dict(),btc_manifest=wallet_manifest.to_dict(),evm_public=evm_public,btc_public=btc_public,
            tool_sha256=dict(anvil=sha(ANVIL),bitcoin_core=sha(BITCOIND)),scope='TEST_ONLY regtest linear guest/state; ordinary adapter and monetary contracts; real DKG/FROST'))
        print('PREPARED: real funding, native header verification, threshold approval; mint fails before proof',flush=True)
    except BaseException:
        for p in processes:
            if p.poll() is None:p.terminate()
        for p in processes:
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        raise

def outpoint(row):return Web3.keccak(encode(['bytes32','uint32'],[bytes.fromhex(row['txid'])[::-1],row['vout']]))

def mint_call(mining,row,**changes):
    r=dict(row,**changes)
    return mining.functions.mintFromProof(bytes.fromhex(r['stripped_tx']),r['vout'],bytes.fromhex(r['block_hash']),
        [bytes.fromhex(v) for v in r['siblings']],r['index'],[r['recipient']],[r['mp_amount']],bytes.fromhex(r['operation'].removeprefix('0x')))

def accept(out):
    prep=read(out/'preparation.json');w3=connect(prep);addresses=prep['addresses'];checks={};cleanup={}
    for p,digest in prep['source_sha256'].items():assert sha(ROOT/p)==digest,p
    for p,digest in prep['input_artifacts'].items():assert sha(out/p)==digest,p
    verified=read(out/'groth16/verified/receipt.json')
    assert verified['status']=='VERIFIED' and verified['receipt_kind']=='groth16' and verified['dev_mode_disabled']
    assert verified['image_id']==prep['program_image_id'] and verified['journal_sha256']==sha(out/'state.journal')
    assert verified['receipt_sha256']==sha(out/'groth16/receipt.bin')
    assert verify_graph(w3,addresses)==prep['graph']
    get=lambda name:contract(w3,name,addresses[name])
    state=get('TestRegtestDepositState');adapter=get('ZkSpvAdapter');mining=get('MiningPoolV1');deposits=get('BitcoinDepositRegistry')
    token=get('PoolMpToken');auth=get('DKGAuthorization');coordinator=get('FROSTCoordinator')
    funding=read(out/'funding.json');first,second=funding;journal=(out/'state.journal').read_bytes();seal=(out/'groth16/verified/seal.bin').read_bytes()
    try:
        tip_time=int.from_bytes(journal[320:352],'big')
        now=w3.eth.get_block('latest').timestamp
        w3.provider.make_request('evm_setNextBlockTimestamp',[max(now+1,tip_time+60)])
        bad=bytearray(journal);bad[96+31]^=1
        checks['wrong_state_rejected']=transact(w3,state.functions.submitTransition(bytes(bad),seal),0)
        bad=bytearray(journal);bad[384]^=1
        checks['modified_journal_rejected']=transact(w3,state.functions.submitTransition(bytes(bad),seal),0)
        assert state.functions.stateVersion().call()==0 and not state.functions.isFresh().call()
        checks['state_accepted']=transact(w3,state.functions.submitTransition(journal,seal))
        assert state.functions.stateVersion().call()==1 and state.functions.isFresh().call()
        checks['proof_replay_rejected']=transact(w3,state.functions.submitTransition(journal,seal),0)
        checks['mint_without_header_rejected']=transact(w3,mint_call(mining,first),0)
        block=(out/'block-2017.bin').read_bytes();raw=block[:80]
        path=(out/'history-2017.bin').read_bytes();siblings=[path[i:i+32] for i in range(0,len(path),32)]
        wrong=list(siblings);wrong[0]=bytes(32)
        checks['invalid_history_rejected']=transact(w3,adapter.functions.materializeHeader(2017,raw,wrong),0)
        checks['header_materialized']=transact(w3,adapter.functions.materializeHeader(2017,raw,siblings))
        assert adapter.functions.getBlockStatus(bytes.fromhex(first['block_hash'])).call()==[True,6]
        txid=bytes.fromhex(first['txid'])[::-1];merkle=[bytes.fromhex(s) for s in first['siblings']]
        assert adapter.functions.checkTxInclusion(bytes.fromhex(first['block_hash']),txid,merkle,first['index']).call()
        checks['wrong_merkle_rejected']=transact(w3,mint_call(mining,first,siblings=['00'*32]+first['siblings'][1:]),0)
        checks['wrong_index_rejected']=transact(w3,mint_call(mining,first,index=first['index']^(1<<len(first['siblings']))),0)
        checks['wrong_recipient_rejected']=transact(w3,mint_call(mining,first,recipient=w3.eth.accounts[3]),0)
        before=balances(token,deposits,mining,first)
        assert before==dict(balance=0,supply=0,backing=0,consumed=False,nonce=0,allocation='00'*32)
        checks['mint_accepted']=transact(w3,mint_call(mining,first))
        after=balances(token,deposits,mining,first)
        allocation=Web3.keccak(encode(['address[]','uint256[]'],[[first['recipient']],[first['mp_amount']]])).hex().removeprefix('0x')
        assert after==dict(balance=first['mp_amount'],supply=first['mp_amount'],backing=first['satoshis'],consumed=True,nonce=1,allocation=allocation)
        events=mining.events.Minted().process_receipt(w3.eth.get_transaction_receipt(checks['mint_accepted']['transaction_hash']))
        assert len(events)==1 and events[0]['args']['txid']==txid and events[0]['args']['satoshis']==first['satoshis']
        checks['mint_replay_rejected']=transact(w3,mint_call(mining,first),0)
        _,authorize,_,_=helpers(prep['rpc_url']);work=Path(prep['working_directory']);participant=participant_from(work/'evm',prep['rpc_url'])
        def approve(row):
            args=['0x'+bytes.fromhex(row['txid'])[::-1].hex(),row['vout'],[row['recipient']],[row['mp_amount']]]
            op=authorize(w3,coordinator,auth,participant,work,mining.address,'MiningPoolV1','getMintOperationHash',args)
            return dict(row,operation='0x'+op.hex())
        # Fresh authorization at the current nonce still cannot consume backing twice.
        again=approve(first)
        checks['reauthorized_outpoint_rejected']=transact(w3,mint_call(mining,again),0)
        assert balances(token,deposits,mining,first)==after
        second=approve(second)
        assert adapter.functions.checkTxInclusion(bytes.fromhex(second['block_hash']),bytes.fromhex(second['txid'])[::-1],
            [bytes.fromhex(s) for s in second['siblings']],second['index']).call()
        accepted_at=state.functions.lastUsefulAcceptedAt().call()
        w3.provider.make_request('evm_setNextBlockTimestamp',[accepted_at+3601]);w3.provider.make_request('evm_mine',[])
        assert not state.functions.isFresh().call()
        checks['stale_mint_rejected']=transact(w3,mint_call(mining,second),0)
        assert not deposits.functions.consumed(outpoint(second)).call() and deposits.functions.backingSats(outpoint(second)).call()==0
        assert token.functions.balanceOf(second['recipient']).call()==0
        assert auth.functions.approvedOperations(bytes.fromhex(second['operation'][2:])).call()
        assert balances(token,deposits,mining,first)==after
        report=dict(schema='satoshifi.regtest-deposit-acceptance.v1',status='LOCAL_REGTEST_ZK_DEPOSIT_PASS',production_ready=False,
            public_transactions=0,network='regtest',program_image_id=prep['program_image_id'],state=state.address,adapter=adapter.address,
            consumer=mining.address,registry=deposits.address,mp_token=token.address,journal_sha256=sha(out/'state.journal'),
            receipt_sha256=sha(out/'groth16/receipt.bin'),proof_kind='groth16',dev_mode=False,confirmations=6,
            bitcoin_txid=first['txid'],checks=checks,before=before,after=after,second_deposit_unconsumed=True,
            ordinary_graph=verify_graph(w3,addresses),mainnet_qualified=False,
            scope='Real Core regtest funding and PoW; real Groth16; TEST_ONLY linear state; ordinary adapter, monetary consumer, DKG and FROST')
    finally:
        cleanup=stop_nodes(prep);write(out/'cleanup.json',cleanup)
    write(out/'acceptance.json',dict(report,cleanup=cleanup))
    print(json.dumps(dict(status=report['status'],mint_gas=checks['mint_accepted']['gas_used'],satoshis=first['satoshis'])),flush=True)

def balances(token,deposits,mining,row):
    key=outpoint(row)
    return dict(balance=token.functions.balanceOf(row['recipient']).call(),supply=token.functions.totalSupply().call(),
        backing=deposits.functions.backingSats(key).call(),consumed=deposits.functions.consumed(key).call(),
        nonce=mining.functions.nonce().call(),allocation=bytes(mining.functions.mintedAllocations(key).call()).hex())

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=['prepare','accept','stop'])
    p.add_argument('--output',type=Path,required=True);p.add_argument('--work',type=Path)
    a=p.parse_args();out=a.output.resolve()
    if a.stage=='prepare':
        if a.work is None:p.error('--work required for prepare')
        prepare(out,a.work.resolve())
    elif a.stage=='accept':accept(out)
    else:write(out/'cleanup.json',stop_nodes(read(out/'nodes.json')))
