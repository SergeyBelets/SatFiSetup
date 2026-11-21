"""Nine explicit EVM/Bitcoin combinations on isolated nodes, no public funds.

Every combination enrolls three local identities and completes all three actual
Rust DKG ceremonies over the actual encrypted on-chain mailbox. Public Bitcoin
Core observations use historical PoW fixtures; production network checks remain
unchanged. This does not simulate current public-network liveness or custody.
"""
import json
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account
from web3 import Web3
from web3.exceptions import ContractLogicError, Web3RPCError

from signer_agent.bootstrap import Bootstrap, artifact
from signer_agent.bitcoin_network import MAINNET_GENESIS, TESTNET4_GENESIS, relay_artifact, relay_constructor_args, inspect_relay, sync_relay
from signer_agent.bitcoin_trust import invitation_options
from signer_agent.dkg import Participant, Registry
from signer_agent.dkg_protocol import Manifest, PROTOCOL_ID, PURPOSES
from signer_agent.onboarding import ROLES, chain_main, fingerprint, validate_invite, validate_bundle
from signer_agent.onchain_transport import OnchainMailbox, assembled_profile, ceremony_bundle, invitation, transact
from signer_agent.secure_state import SecureState
from test_dkg_processes import authorize_ceremony, deploy, tx
from test_mainnet_bootstrap import HistoricalFixtureCore

ROOT=Path(__file__).resolve().parents[2]

class HistoricalPublicCore(HistoricalFixtureCore):
    def __init__(self,network):
        super().__init__()
        self.network=network
        self.first,self.checkpoint,self.genesis,self.core_chain=32246,32256,MAINNET_GENESIS,'main'
        if network=='testnet4':
            folder=ROOT/'web3/test/fixtures/testnet4'
            self.headers=(folder/'headers-2006-4050.bin').read_bytes()
            self.records=(folder/'expected-2006-4050.bin').read_bytes()
            self.first,self.checkpoint,self.height,self.genesis,self.core_chain=2006,2016,2029,TESTNET4_GENESIS,'testnet4'
        self.by_hash={self.record(height)['hash']:height for height in range(self.first,self.height+1)}
        self.policy=self.public_policy()
    def raw(self,height):
        first=getattr(self,'first',32246);offset=(height-first)*80
        return self.headers[offset:offset+80]
    def record(self,height):
        raw=self.raw(height);offset=(height-getattr(self,'first',32246))*68;entry=self.records[offset:offset+68]
        from signer_agent.bitcoin_network import header_hash
        return dict(hash=header_hash(raw)[::-1].hex(),height=height,confirmations=self.height-height+1,
            time=int.from_bytes(raw[68:72],'little'),bits=raw[72:76][::-1].hex(),chainwork=entry[32:64].hex())
    def call(self,method,*args):
        if method=='getblockhash' and args[0]==0:return self.genesis
        result=super().call(method,*args)
        if method=='getblockchaininfo':result['chain']=self.core_chain
        return result
    def public_policy(self):
        checkpoint=getattr(self,'checkpoint',32256)
        return dict(version='1',network=self.network,genesis_hash=getattr(self,'genesis',MAINNET_GENESIS),
            checkpoint_header=self.raw(checkpoint).hex(),checkpoint_height=str(checkpoint),
            checkpoint_chainwork=self.record(checkpoint)['chainwork'],
            checkpoint_predecessors=[self.raw(h).hex() for h in range(checkpoint-10,checkpoint)],
            max_reorg_depth='12',max_tip_age_seconds='86400',max_relay_lag_blocks='0',max_headers_per_sync='4')

@pytest.mark.parametrize('chain',[1,11155111,31337],ids=['ethereum','sepolia','local'])
@pytest.mark.parametrize('network',['mainnet','testnet4','regtest'])
def test_v6_independent_networks_three_real_dkg_and_exact_relay(tmp_path,monkeypatch,capsys,record_property,chain,network):
    core=HistoricalPublicCore(network) if network!='regtest' else None
    timestamp=core.record(core.height)['time']+1 if core else 1800000000
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    rpc=f'http://127.0.0.1:{port}';w3=Web3(Web3.HTTPProvider(rpc))
    log=(tmp_path/'anvil.log').open('w')
    process=subprocess.Popen([shutil.which('anvil'),'--host','127.0.0.1','--port',str(port),'--chain-id',str(chain),
        '--hardfork','osaka','--gas-limit','30000000','--max-persisted-states','100000',
        '--cache-path',str(tmp_path/'anvil-cache'),'--timestamp',str(timestamp),'--silent'],stdout=log,stderr=log)
    try:
        for _ in range(100):
            if w3.is_connected():break
            if process.poll() is not None:raise AssertionError((tmp_path/'anvil.log').read_text())
            time.sleep(.05)
        else:raise AssertionError('isolated matrix node not ready')
        original=Web3.HTTPProvider.make_request
        def finalized_send(provider,method,params):
            response=original(provider,method,params)
            if chain==1 and method in ('eth_sendRawTransaction','eth_sendTransaction') and 'result' in response:
                assert 'error' not in original(provider,'anvil_mine',['0x80','0x0'])
            return response
        monkeypatch.setattr(Web3.HTTPProvider,'make_request',finalized_send)
        if chain==1:
            for name,value in dict(SATOSHIFI_MAX_FEE_PER_GAS_WEI='3000000000',SATOSHIFI_MAX_PRIORITY_FEE_PER_GAS_WEI='1000000000',SATOSHIFI_MAX_TX_GAS=str(2**24)).items():monkeypatch.setenv(name,value)
            w3.provider.make_request('anvil_mine',['0x80','0x0'])
        if core:monkeypatch.setattr('signer_agent.bitcoin_network.time',SimpleNamespace(time=lambda:timestamp))
        accounts=[Account.from_key(i.to_bytes(32,'big')) for i in range(1,4)]
        identities=[]
        for i,account in enumerate(accounts):
            w3.provider.make_request('anvil_setBalance',[account.address,hex(10**23)])
            path=tmp_path/f'identity-{i}.json';path.write_text(json.dumps(dict(private_key=account.key.hex())));path.chmod(0o600);identities.append(path)
        backed=chain==1 or core is not None
        invite=dict(version='6',chain_id=str(chain),bitcoin_network=network,mode='backed' if backed else 'integration',
            deployment_id='matrix',operator=accounts[0].address,treasury=accounts[0].address,approval_deadline='4102444800',
            federation_id='matrix-fed',confederation_id='matrix-conf',participants=[a.address for a in accounts],threshold='3',epoch='1',
            sessions={role:Web3.keccak(text=str(tmp_path)+role).hex() for role in ROLES},protocol_id=PROTOCOL_ID.hex(),
            phase_duration='86400' if chain==1 else '3600',evm_confirmations='12' if chain==1 else '1',
            min_p2wpkh_sats='294',min_p2tr_sats='330',min_custodians='2',bitcoin_confirmations='6',wrap_fee_bps='100',unwrap_fee_bps='100',
            wrap_staker_bps='2500',deploy_staker_bps='5000',converter_numerator='1',converter_denominator='100',converter_funding='1000',
            dao_token_recipient=accounts[0].address,dao_use_reserve=False,integration_fee_exemption=chain!=1)
        if core:invite['bitcoin_policy']=core.policy
        if chain==1:
            # Constructors reject chain 1. Inject only this adversarial local
            # fixture to prove matching pins cannot bless pre-existing mock
            # runtime either, independently of the selected Bitcoin network.
            for index,(name,kind,field) in enumerate((('usd','DAOTestUSD','reward_token'),('feed','DAOMockFeed','btc_usd_feed'))):
                definition=artifact(kind)
                constructor=w3.eth.contract(abi=definition['abi'],bytecode=definition['bytecode']['object']).constructor()
                nonce_before=w3.eth.get_transaction_count(accounts[0].address)
                with pytest.raises((ContractLogicError,Web3RPCError),match='integration network only'):
                    constructor.estimate_gas({'from':accounts[0].address})
                target=Web3.to_checksum_address('0x'+format(0xcafe+index,'040x'))
                code=definition['deployedBytecode']['object']
                assert 'error' not in w3.provider.make_request('anvil_setCode',[target,code])
                assert 'error' not in w3.provider.make_request('anvil_mine',['0x80','0x0'])
                component=dict(address=target,runtime_hash='0x'+bytes(Web3.keccak(w3.eth.get_code(target))).hex())
                if name=='usd':component['decimals']='6'
                rejected=dict(deployment_id='mainnet-forbidden-'+name,chain_id='1',bitcoin_network=network,
                    mode='backed',evm_rpc=rpc,evm_confirmations='12',steps=[],
                    contracts={name:dict(address=target,artifact=kind)},**{field:component})
                if core:rejected['bitcoin_policy']=core.policy
                with pytest.raises(ValueError,match='^MAINNET_TEST_COMPONENT_FORBIDDEN$'):
                    Bootstrap(rejected,tmp_path/(name+'-forbidden.sqlite'))
                assert w3.eth.get_transaction_count(accounts[0].address)==nonce_before
        token=deploy(w3,'StageRewardToken' if chain==1 else 'DAOTestUSD')
        feed=deploy(w3,'TWAPRemediationFeed' if chain==1 else 'DAOMockFeed')
        if chain!=1:
            assert token.functions.decimals().call()==6 and feed.functions.decimals().call()==8
            tx(w3,token.functions.mint(accounts[0].address,1000))
            assert token.functions.balanceOf(accounts[0].address).call()==1000
        if backed:
            invite['reward_token']=dict(address=token.address,runtime_hash='0x'+bytes(Web3.keccak(w3.eth.get_code(token.address))).hex(),decimals=str(token.functions.decimals().call()))
            invite['btc_usd_feed']=dict(address=feed.address,runtime_hash='0x'+bytes(Web3.keccak(w3.eth.get_code(feed.address))).hex())
        if chain==1:invite['deployment_fee']=dict(amount='100',treasury=accounts[0].address)
        validate_invite(invite)
        invite_path=tmp_path/'operator-invitation.json';invite_path.write_text(json.dumps(invite))
        chain_main(SimpleNamespace(action='publish',rpc=rpc,invite=invite_path,identity_file=identities[0],state=tmp_path/'publish.sqlite'))
        published=json.loads(capsys.readouterr().out);assert published['status']=='VERIFIED';board_address=published['board']
        monkeypatch.setattr('signer_agent.onboarding.local_bitcoin',lambda args,found:core)
        for index,identity in enumerate(identities):
            chain_main(SimpleNamespace(action='join-chain',rpc=rpc,board=board_address,accept_fingerprint=fingerprint(invite),
                identity_file=identity,directory=tmp_path/f'friend-{index}',bitcoin_rpc='historical-fixture',bitcoin_cookie='fixture'))
            assert json.loads(capsys.readouterr().out)['status']=='ENROLLED_ONCHAIN'
        profile=assembled_profile(w3,board_address)
        assert profile['onboarding']['invitation']==invite
        dkg=deploy(w3,'DKGCoordinator');board,_=invitation(w3,board_address)
        transact(w3,identities[0],tmp_path/'bind.sqlite',board,'bindRegistry',[dkg.address])
        registry=Registry(rpc,dkg.address,accounts[0].key.hex());registry.identity_file=identities[0]
        registry.transaction_journal=str(tmp_path/'operator-tx.sqlite')
        registry.bitcoin_graph=dict(version='2',chain_id=str(chain),**invitation_options(invite))
        for role in ROLES:
            c=profile[role]
            authorize_ceremony(w3,dkg,bytes.fromhex(c['id'][2:]),accounts[0].address)
            registry.send('createCeremony',bytes.fromhex(c['id'][2:]),c['participants'],int(c['threshold']),
                [bytes.fromhex(k[2:]) for k in c['transport_keys']],int(c['epoch']),PURPOSES['BTC_SPEND' if role=='btc_ceremony' else 'EVM_AUTH'],PROTOCOL_ID)
        participants=[]
        for role in ROLES:
            bundle=ceremony_bundle(w3,board_address,role);manifest=Manifest.from_dict(bundle)
            for index,identity in enumerate(identities):
                directory=tmp_path/f'friend-{index}';state=SecureState(str(directory/(role+'.enc')),(directory/'password').read_text().strip())
                member=Registry(rpc,dkg.address,accounts[index].key.hex());member.identity_file=identity
                member.transaction_journal=str(directory/'transactions.sqlite');member.bitcoin_graph=registry.bitcoin_graph
                validate_bundle(bundle,state,member)
                mailbox=OnchainMailbox(member,board_address,identity,member.transaction_journal)
                participants.append((role,Participant(manifest,member,state,mailbox)))
        for _ in range(10):
            statuses=[participant.run_once()['status'] for _,participant in participants]
            if all(value=='ACTIVE' for value in statuses):break
        assert statuses==['ACTIVE']*9
        groups=[];packets=[];private_values=[]
        for role in ROLES:
            sid=bytes.fromhex(invite['sessions'][role]);completed=dkg.functions.getCompletedResult(sid).call()
            groups.append(bytes(completed[4]));assert completed[6]==3
            for i,account in enumerate(accounts):
                for j,recipient in enumerate(accounts):
                    if i!=j:
                        payload=bytes(board.functions.packet(sid,2,account.address,recipient.address).call())
                        assert set(json.loads(payload))=={'envelope','hash','signature'};packets.append(payload.decode())
        for _,participant in participants:
            local=participant.s.get(participant.key)
            private_values.append(local['result']['signing_share'])
        assert len(set(groups))==3 and len(packets)==18
        assert not any(value in '\n'.join(packets) for value in private_values)
        # Selected Bitcoin network alone controls relay provenance/consensus.
        steps=[dict(id='relay',kind='deploy',artifact=relay_artifact(network),args=Bootstrap.wire(relay_constructor_args(core.policy)) if core else [])]
        if core:steps.append(dict(id='spv_guard',kind='deploy',artifact='MainnetSPVGuard',args=['$relay','86400']))
        relay_profile=dict(deployment_id='matrix-relay',chain_id=str(chain),bitcoin_network=network,mode=invite['mode'],
            evm_rpc=rpc,evm_confirmations=invite['evm_confirmations'],steps=steps)
        if backed:
            relay_profile.update(reward_token=invite['reward_token'],btc_usd_feed=invite['btc_usd_feed'],contracts={
                'usd':dict(address=token.address,artifact='IERC20Metadata'),
                'feed':dict(address=feed.address,artifact='IChainlinkBTCUSD')})
        runner=Bootstrap(relay_profile,tmp_path/'relay.sqlite');assert runner.apply(identities[0])['status']=='VERIFIED'
        relay=runner.contract('relay');assert relay.functions.testHelpersEnabled().call() is False
        if core:
            def send_many(raws):
                tx(w3,relay.functions.addBlockHeaders([bytes.fromhex(value) for value in raws]));return dict(status='VERIFIED')
            for _ in range(10):
                result=sync_relay(core,relay,core.policy,lambda value:send_many([value]),send_headers=send_many,
                    evm_confirmations=int(invite['evm_confirmations']))
                if result['status']=='MAINNET_RELAY_READY':break
            assert result['status']=='MAINNET_RELAY_READY'
            assert inspect_relay(core,relay,core.policy)['network']==network
            assert runner.contract('spv_guard').functions.isFresh().call()
        record_property('network_matrix',json.dumps(dict(evm_chain=chain,bitcoin_network=network,invitation_version='6',
            actual_dkg_ceremonies=3,independent_group_keys=True,encrypted_onchain_envelopes=18,
            relay=relay_artifact(network),test_helpers=False,live_public_network=False,historical_core=bool(core),
            canonical_test_usd=chain!=1,canonical_mock_feed=chain!=1,
            mainnet_test_component_constructors_and_runtime_rejected=chain==1)))
    finally:
        process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()
