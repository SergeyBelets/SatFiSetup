"""Independent EVM/Bitcoin selections; historical public headers are never live readiness."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from signer_agent.bitcoin_network import (TESTNET4_GENESIS, MAINNET_GENESIS, validate_policy,
    check_core, relay_artifact, header_hash, sync_relay)
from signer_agent.bitcoin_trust import invitation_options, graph_options, transaction_profile
from signer_agent.onboarding import validate_invite, assemble, fingerprint
from test_onboarding_mainnet import mainnet_invitation, signed_enrollments
from test_bitcoin_network import Core, Relay, policy, raw, CHECKPOINT

FIXTURE=Path(__file__).resolve().parents[2]/'web3/test/fixtures/testnet4'
HEADERS=(FIXTURE/'headers-2006-4050.bin').read_bytes()
RECORDS=(FIXTURE/'expected-2006-4050.bin').read_bytes()

def public_testnet_policy():
    manifest=json.loads((FIXTURE/'manifest.json').read_text())
    return dict(version='1',network='testnet4',genesis_hash=TESTNET4_GENESIS,
        checkpoint_height='2016',checkpoint_header=HEADERS[800:880].hex(),
        checkpoint_chainwork=manifest['checkpoint_chainwork'],
        checkpoint_predecessors=[HEADERS[i*80:(i+1)*80].hex() for i in range(10)],
        max_reorg_depth='6',max_tip_age_seconds='86400',max_relay_lag_blocks='0',max_headers_per_sync='16')

def invitation(chain,network):
    invite=mainnet_invitation()
    invite.update(version='6',chain_id=chain,bitcoin_network=network,
        mode='backed' if chain=='1' or network!='regtest' else 'integration',
        phase_duration='86400' if chain=='1' else '3600',
        evm_confirmations='12' if chain=='1' else '1',integration_fee_exemption=chain!='1')
    if chain=='1': invite['deployment_fee']=dict(amount='100',treasury=invite['treasury'])
    if network=='testnet4':invite['bitcoin_policy']=public_testnet_policy()
    if network=='regtest':
        del invite['bitcoin_policy']
        if chain!='1':
            del invite['reward_token'];del invite['btc_usd_feed']
    return invite

@pytest.mark.parametrize('chain',['1','31337','11155111'])
@pytest.mark.parametrize('network',['mainnet','testnet4','regtest'])
def test_v6_commits_independent_networks_and_signed_enrollment(chain,network):
    invite=invitation(chain,network)
    assert validate_invite(invite)==invite
    profile=assemble(invite,signed_enrollments(invite),'http://local-only')
    assert profile['chain_id']==chain and profile['bitcoin_network']==network
    assert profile['mode']==invite['mode']
    options=invitation_options(invite)
    assert options['bitcoin_network']==network
    graph=dict(version='2',chain_id=chain,**options)
    assert transaction_profile(graph)['mode']==invite['mode']
    assert transaction_profile(graph)['evm_confirmations']==invite['evm_confirmations']
    assert graph_options(graph)==options
    changed=deepcopy(invite);changed['bitcoin_network']='mainnet' if network!='mainnet' else 'testnet4'
    assert fingerprint(changed)!=fingerprint(invite)
    with pytest.raises(ValueError):assemble(changed,signed_enrollments(invite))

@pytest.mark.parametrize('name,chain',[('mainnet','1'),('sepolia','11155111'),('local','31337')])
@pytest.mark.parametrize('network',['mainnet','testnet4','regtest'])
def test_invite_cli_selects_independent_network_names_and_default_finality(tmp_path,capsys,name,chain,network):
    from signer_agent.onboarding import main
    from test_onboarding_policy_v3 import invite_args
    invite=invitation(chain,network);output=tmp_path/'invitation.json'
    args=invite_args(invite,output);position=args.index('--chain-id');del args[position:position+2]
    args.extend(['--ethereum-network',name,'--bitcoin-network',network])
    if network!='regtest':
        policy_file=tmp_path/'policy.json';policy_file.write_text(json.dumps(invite['bitcoin_policy']))
        args.extend(['--bitcoin-policy',str(policy_file)])
    if invite['mode']=='backed':
        token=invite['reward_token'];feed=invite['btc_usd_feed']
        args.extend(['--reward-token',token['address'],'--reward-token-code-hash',token['runtime_hash'],
            '--reward-token-decimals',token['decimals'],'--btc-usd-feed',feed['address'],'--btc-usd-feed-code-hash',feed['runtime_hash']])
    if chain=='1':args.extend(['--deployment-fee-amount','100','--deployment-fee-treasury',invite['treasury']])
    assert main(args)==0
    actual=json.loads(output.read_text())
    assert actual['chain_id']==chain and actual['bitcoin_network']==network and actual['version']=='6'
    assert actual['evm_confirmations']==('12' if chain=='1' else '1')
    assert json.loads(capsys.readouterr().out)['status']=='INVITED'
    other='31337' if chain!='31337' else '1'
    with pytest.raises(ValueError,match='ETHEREUM_NETWORK_MISMATCH'):main(args+['--chain-id',other])

@pytest.mark.parametrize('chain',['1','31337','11155111'])
def test_public_bitcoin_cannot_downgrade_policy_by_selecting_evm_testnet(chain):
    for network in ('mainnet','testnet4'):
        invite=invitation(chain,network)
        invite['bitcoin_policy']['network']='testnet4' if network=='mainnet' else 'mainnet'
        with pytest.raises(ValueError,match='MAINNET_POLICY_NETWORK'):validate_invite(invite)
        invite=invitation(chain,network);del invite['bitcoin_policy']
        with pytest.raises(ValueError):validate_invite(invite)

@pytest.mark.parametrize('network',['mainnet','testnet4','regtest'])
def test_ethereum_mainnet_cannot_downgrade_finality_economics_with_bitcoin_selection(network):
    for mutate in (lambda i:i.update(evm_confirmations='1'),lambda i:i.update(integration_fee_exemption=True),
                   lambda i:i.pop('reward_token'),lambda i:i.pop('btc_usd_feed'),lambda i:i.pop('deployment_fee'),
                   lambda i:i.update(mode='integration')):
        invite=invitation('1',network);mutate(invite)
        with pytest.raises(ValueError):validate_invite(invite)

@pytest.mark.parametrize('chain',['1','31337','11155111'])
def test_regtest_cannot_smuggle_public_network_policy(chain):
    invite=invitation(chain,'regtest');invite['bitcoin_policy']=public_testnet_policy()
    with pytest.raises(ValueError):validate_invite(invite)
    with pytest.raises(ValueError,match='REGTEST_POLICY_FORBIDDEN'):
        graph_options(dict(version='2',bitcoin_network='regtest',bitcoin_policy=public_testnet_policy(),evm_confirmations='12'))

class PublicTestnetCore:
    network='testnet4'
    def __init__(self):
        self.policy=public_testnet_policy();self.height=2026;self.chain='testnet4';self.genesis=TESTNET4_GENESIS
        self.changes={};self.hashes={self.record(h)[:32][::-1].hex():h for h in range(2006,2027)}
    def raw(self,h):return HEADERS[(h-2006)*80:(h-2006+1)*80]
    def record(self,h):return RECORDS[(h-2006)*68:(h-2006+1)*68]
    @property
    def now(self):return int.from_bytes(self.raw(self.height)[68:72],'little')+1
    def call(self,method,*args):
        if method=='getblockchaininfo':return dict(dict(chain=self.chain,blocks=self.height,headers=self.height,
            bestblockhash=self.record(self.height)[:32][::-1].hex(),chainwork=self.record(self.height)[32:64].hex(),initialblockdownload=False,pruned=False),**self.changes)
        if method=='getblockhash':return self.genesis if args[0]==0 else self.record(args[0])[:32][::-1].hex()
        if method=='getindexinfo':return {'txindex':dict(synced=True,best_block_height=self.height)}
        if method=='getblockheader':
            h=self.hashes[args[0]]
            if args[1] is False:return self.raw(h).hex()
            return dict(hash=args[0],height=h,confirmations=self.height-h+1,time=int.from_bytes(self.raw(h)[68:72],'little'),bits=self.record(h)[64:].hex(),chainwork=self.record(h)[32:64].hex())
        raise AssertionError(method)

def test_testnet4_uses_real_pow_anchor_and_fully_validated_core_requirements():
    core=PublicTestnetCore()
    assert validate_policy(core.policy,'testnet4')==core.policy
    assert check_core(core,'testnet4',core.policy,now=core.now)['chain']=='testnet4'
    assert relay_artifact('testnet4')=='Testnet4HeaderRelay'
    with pytest.raises(ValueError,match='MAINNET_TIP_STALE'):check_core(core,'testnet4',core.policy)
    with pytest.raises(ValueError,match='MAINNET_POLICY_NETWORK'):check_core(core,'mainnet',core.policy,now=core.now)

@pytest.mark.parametrize('chain',['main','test','signet','regtest'])
def test_testnet4_rejects_wrong_core_network(chain):
    core=PublicTestnetCore();core.chain=chain
    with pytest.raises(ValueError,match='BITCOIN_NETWORK_MISMATCH'):check_core(core,'testnet4',core.policy,now=core.now)

@pytest.mark.parametrize('genesis',[MAINNET_GENESIS,'00'*32,'0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206'])
def test_testnet4_rejects_wrong_genesis_even_if_rpc_chain_label_matches(genesis):
    core=PublicTestnetCore();core.genesis=genesis
    with pytest.raises(ValueError,match='MAINNET_GENESIS_MISMATCH'):check_core(core,'testnet4',core.policy,now=core.now)

@pytest.mark.parametrize('change,error',[({'initialblockdownload':True},'MAINNET_CORE_NOT_SYNCED'),
    ({'headers':2027},'MAINNET_CORE_NOT_SYNCED'),({'pruned':True},'MAINNET_CORE_PRUNED')])
def test_public_testnet4_does_not_bypass_live_node_safety(change,error):
    core=PublicTestnetCore();core.changes=change
    with pytest.raises(ValueError,match=error):check_core(core,'testnet4',core.policy,now=core.now)

def test_relay_batch_stops_at_finality_barrier_when_bitcoin_tip_grows():
    core=Core(height=CHECKPOINT+20);core.height=CHECKPOINT+10;core.index['txindex']['best_block_height']=core.height
    relay=Relay(core);finalized=dict(relay.stored);original=relay.functions
    class Functions:
        def __getattr__(self,name):
            if name not in ('blocks','getMainchainHead','getMainchainHeight','height2hash'):return getattr(original,name)
            def fn(*args):
                class Call:
                    def call(self,block_identifier='latest'):
                        if name=='blocks':return (relay.stored if block_identifier=='latest' else finalized).get(args[0],(bytes(32),bytes(32),0,0,0,0))
                        if block_identifier=='latest':return getattr(original,name)(*args).call()
                        if name=='getMainchainHead':return header_hash(raw(CHECKPOINT))
                        if name=='getMainchainHeight':return CHECKPOINT
                        return header_hash(raw(CHECKPOINT)) if args[0]==CHECKPOINT else bytes(32)
                return Call()
            return fn
    relay.functions=Functions();batches=[]
    def send_many(raws):
        batches.append(list(raws))
        for h in range(CHECKPOINT+1,CHECKPOINT+3):relay.add(raw(h).hex())
        return dict(status='CONFIRMING')
    first=sync_relay(core,relay,policy(),lambda _:pytest.fail('single callback'),send_headers=send_many,now=core.now)
    assert first['reason']=='EVM_HEADER_PENDING' and len(batches)==1 and len(batches[0])==2
    core.height+=5;core.index['txindex']['best_block_height']=core.height
    second=sync_relay(core,relay,policy(),lambda _:pytest.fail('single callback'),send_headers=send_many,now=core.now)
    assert second==dict(status='IN_PROGRESS',submitted='0',reason='EVM_HEADER_PENDING')
    assert len(batches)==1

@pytest.mark.parametrize('status',['BROADCAST','CONFIRMING','VERIFIED'])
def test_relay_recovers_pending_nonce_even_when_competitor_completed_entire_tip(status):
    core=Core(height=CHECKPOINT+10);relay=Relay(core)
    for height in range(CHECKPOINT+1,core.height+1):relay.add(raw(height).hex())
    recovered=[]
    class Sender:
        def recover_pending(self):
            recovered.append(True)
            return dict(status=status)
        def __call__(self,headers):pytest.fail('No new headers should be signed')
    result=sync_relay(core,relay,policy(),lambda _:pytest.fail('single callback'),send_headers=Sender(),now=core.now)
    assert recovered==[True]
    if status=='VERIFIED':assert result['status']=='MAINNET_RELAY_READY' and result['submitted']=='0'
    else:assert result==dict(status='IN_PROGRESS',submitted='0',reason='EVM_HEADER_PENDING')

def test_policy64_bounds_fresh_atomic_chunks_to16_without_changing_policy():
    core=Core(height=CHECKPOINT+40);p=policy();p['max_headers_per_sync']='64'
    relay=Relay(core,p);batches=[]
    def send(headers):
        batches.append(headers)
        for header in headers:relay.add(header)
        return dict(status='VERIFIED')
    for expected_count in (16,16,8):
        result=sync_relay(core,relay,p,lambda _:pytest.fail('single callback'),send_headers=send,now=core.now)
        assert int(result['submitted'])==expected_count and len(batches[-1])==expected_count
    assert result['status']=='MAINNET_RELAY_READY'
    assert p['max_headers_per_sync']=='64'
    assert [header for batch in batches for header in batch]==[raw(h).hex() for h in range(CHECKPOINT+1,core.height+1)]
