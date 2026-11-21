"""Real independent DKG/FROST processes create and mint a managed discount NFT.

Isolated Anvil only. No ready signatures/proofs or participant keys are delivered.
"""
import json, os, re, subprocess, sys
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

from test_full_bootstrap import exercise_bootstrap


@contextmanager
def advancing_blocks(w3):
    """The isolated test chain must progress for a two-confirmation local pin."""
    stop=Event();errors=[]
    def mine():
        while not stop.wait(0.5):
            try:
                result=w3.provider.make_request('evm_mine',[])
                if 'error' in result:raise RuntimeError(result['error'])
            except Exception as exc:
                errors.append(str(exc));break
    worker=Thread(target=mine,daemon=True);worker.start()
    try:yield
    finally:
        stop.set();worker.join(timeout=5)
    assert not errors,errors


def test_nft_create_mint_and_exact_retry_with_native_frost(tmp_path,record_property):
    first=exercise_bootstrap(tmp_path,record_property,
        custody=dict(version='1',mode='custodians',threshold='2',sources=[]),return_context=True)
    b,command=first['bootstrap'],first['command'];w3=b.w3
    with advancing_blocks(w3):
        pin=tmp_path/'nft.json';rpc=w3.provider.endpoint_uri
        initialized=command('nft','init','--rpc',rpc,'--board',first['board'],'--role','conf_ceremony',
            '--factory',b.contract('confederations').address,'--manager',b.contract('fee_manager').address,
            '--chain-id','31337','--confirmations','2','--output',pin)
        assert initialized['status']=='NFT_PIN_PREPARED'
        role='conf_ceremony';members=[i for i,a in enumerate(first['addresses']) if a in first['config'][role]['participants']]
        journal=tmp_path/'nft.sqlite';identities=first['identities']

        def operation(action,*args):
            intent=tmp_path/(action+'.json')
            prepared=command('nft',action+'-prepare','--rpc',rpc,'--config',pin,'--intent',intent,*args)
            assert prepared['status']=='NFT_OPERATION_PREPARED'
            proposed=command('nft','propose','--rpc',rpc,'--config',pin,'--intent',intent,
                '--deadline',str(w3.eth.get_block('latest').timestamp+3600),
                '--identity-file',identities[0],'--state',journal,'--send')
            assert proposed['status']=='AWAITING_PARTICIPANTS'
            session=proposed['session']
            for _ in range(3):
                for member in members:
                    result=first['participant'](role,member,'sign-evm','--session',session,'--nft-config',pin)
                    assert result['status'] in ('committed','signed','finalized'),result
            for member in members:
                approved=command('nft','custodian-approve','--rpc',rpc,'--config',pin,'--intent',intent,
                    '--identity-file',identities[member],'--state',tmp_path/f'nft-{member}.sqlite','--send',member=member)
                assert approved['status']=='VERIFIED'
            args=['execute','--rpc',rpc,'--config',pin,'--intent',intent,'--session',session,
                  '--identity-file',identities[0],'--state',journal,'--send']
            executed=command('nft',*args);assert executed['status']=='VERIFIED'
            assert command('nft',*args)['status']=='ALREADY_EXECUTED'
            return prepared,executed

        created,_=operation('create','--name','Pilot Fee Discounts','--symbol','PFD')
        status=command('nft','status','--rpc',rpc,'--config',pin)
        assert status['managed'] and status['collection']!='0x'+'00'*20
        before=b.contract('fee_manager').functions.operationNonce().call()
        operation('mint','--recipient',first['addresses'][1],
            '--expiration',str(w3.eth.get_block('latest').timestamp+86400),'--volume',str(1000*10**18),'--discount-bps','2500')
        assert b.contract('fee_manager').functions.operationNonce().call()==before+1
        from signer_agent.bootstrap import artifact
        nft=w3.eth.contract(address=status['collection'],abi=artifact('FederationNFTModifier')['abi'])
        assert nft.functions.ownerOf(0).call()==first['addresses'][1]
        assert nft.functions.balanceOf(first['addresses'][1]).call()==1
        quoted=command('nft','quote','--rpc',rpc,'--config',pin,'--owner',first['addresses'][1],
            '--token-id','0','--amount',str(100*10**18))
        assert quoted['quota_consumed'] is False
        assert int(quoted['fee_units'])==100*10**18*23//10000*7500//10000  # 23 bps, discounted by 25%.
        assert nft.functions.nftMetadata(0).call()[2]==0
        # Run the actual read-only smoke command delivered in the cold-start guide.
        guide=(Path(__file__).resolve().parents[2]/'docs/COLD_START.md').read_text()
        source=next(p for p in re.findall(r"python - <<'PY'\n(.*?)^PY\s*$",guide,re.M|re.S)
                    if 'nftMintedTokenIds' in p)
        def documented_check(intent):
            env=dict(os.environ,LOCAL=str(tmp_path),RPC=rpc,NFT_INTENT=str(intent))
            return subprocess.run([sys.executable,'-c',source],env=env,capture_output=True,text=True,timeout=120)
        checked=documented_check(tmp_path/'mint.json')
        assert checked.returncode==0,checked.stdout+checked.stderr
        issued=json.loads(checked.stdout)
        assert issued['token_id']=='0' and issued['owner']==first['addresses'][1] and issued['used_volume']=='0'
        good=json.loads((tmp_path/'mint.json').read_text())
        for field,value in [('operation_hash',created['operation_hash']),('chain_id','1'),
                            ('hash_method','getCreateNFTModifierOperationHash')]:
            bad=dict(good);bad[field]=value
            bad_path=tmp_path/'wrong-mint.json';bad_path.write_text(json.dumps(bad))
            rejected=documented_check(bad_path)
            assert rejected.returncode!=0,(field,rejected.stdout)
        bad=dict(good);bad['hash_args']=list(good['hash_args']);bad['hash_args'][-1]='1'
        bad_path.write_text(json.dumps(bad));assert documented_check(bad_path).returncode!=0
        record_property('nft_governance',json.dumps(dict(status='PASS',native_frost=True,
            managed_collection=True,create_mint=True,exact_retry=True,quote_consumes_nothing=True,
            enrolled_confirmations=1,local_confirmations=2,documented_metadata_smoke=True,
            wrong_operation_chain_helper_terms_rejected=True,
            federation_representative_excluded_from_custody_key=True,qualified=False)))
