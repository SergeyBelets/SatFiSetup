"""Wallet-funded SMP1 deposits with exact quote and immutable signed recovery.

Core owns its wallet keys. This client never strips an SMP1 marker to accommodate
a signer, never automatically replaces a signed transaction, and never reports
a Bitcoin send as MP issuance. FROST journals are not involved in wallet funding.
"""
import base64
import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

from eth_abi import encode
from eth_utils import keccak
from web3 import Web3

from . import crypto
from .bootstrap import artifact
from .btc_fee_reserve import number, read, digest, ZERO
from .bitcoin_funding import deposit_commitment
from .bitcoin_rpc import BitcoinRPC, BitcoinRPCError, satoshis, observation_error
from .onboarding import read_private
from .operations import Journal

PROFILE = 'SMP1_PSBT_V0_NATIVE_SEGWIT_V1'


def psbt_hex(encoded):
    if not isinstance(encoded, str) or len(encoded) > 1400000:
        raise ValueError('SMP1_PSBT_ENVELOPE')
    try:
        return base64.b64decode(encoded, validate=True).hex()
    except ValueError:
        raise ValueError('SMP1_PSBT_ENVELOPE') from None


def psbt_base64(raw):
    return base64.b64encode(bytes.fromhex(raw)).decode('ascii')


def inspect(encoded, **options):
    result = crypto.call('bitcoin_smp1_psbt_v1', psbt=psbt_hex(encoded), **options)
    if result.get('profile') != PROFILE:
        raise ValueError('SMP1_PSBT_PROFILE')
    return result


def btc_amount(sats):
    return f'{sats//100000000}.{sats%100000000:08d}'


def load_config(path):
    c = json.loads(read_private(path))
    fields = {'version','chain_id','evm_rpc','evm_confirmations','bitcoin_network','bitcoin_policy',
              'bitcoin_rpc','bitcoin_cookie','wallet_name','mining_pool','directory','max_fee_sats','max_inputs'}
    if not isinstance(c,dict) or set(c) != fields|({'recipient_policy'} if c.get('version')=='2' else set()) or c['version'] not in ('1','2'):
        raise ValueError('SMP1_CONFIG')
    from .bitcoin_trust import graph_options
    from .bootstrap_policy import validate_confirmations
    graph_options(dict(version='2',bitcoin_network=c['bitcoin_network'],evm_confirmations=c['evm_confirmations'],
        **({'bitcoin_policy':c['bitcoin_policy']} if c['bitcoin_policy'] is not None else {})))
    validate_confirmations(c,'SMP1_')
    if (c['chain_id'] not in ('1','11155111','31337') or not Web3.is_checksum_address(c['mining_pool'])
            or c['mining_pool'] == ZERO or not isinstance(c['wallet_name'],str) or not c['wallet_name']
            or any(not isinstance(c[p],str) or not Path(c[p]).is_absolute() for p in ('directory','bitcoin_cookie'))):
        raise ValueError('SMP1_CONFIG')
    number(c['max_fee_sats'],1,2100000000000000);number(c['max_inputs'],1,256)
    if c['version']=='2' and (not Web3.is_checksum_address(c['recipient_policy']) or c['recipient_policy']==ZERO):raise ValueError('SMP1_CONFIG')
    return c


class Deposit:
    def __init__(self, config, *, w3=None, bitcoin=None):
        self.c=config
        self.w3=w3 or Web3(Web3.HTTPProvider(config['evm_rpc'],request_kwargs={'timeout':30}))
        self.bitcoin=bitcoin or BitcoinRPC(config['bitcoin_rpc'],config['bitcoin_cookie'],config['bitcoin_network'],config['bitcoin_policy'])
        self.journal=Journal(Path(config['directory'])/'smp1-deposits.sqlite')
        self.journal.put('wallet-policy',{k:v for k,v in config.items() if k not in ('evm_rpc','bitcoin_rpc','bitcoin_cookie','directory')})

    @contextmanager
    def lock(self):
        fd=os.open(str(self.journal.path)+'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX);yield
        finally:
            os.close(fd)

    def context(self):
        from .evm_network import read_block
        from .bitcoin_trust import checked_runtime
        from .contract_provenance import prove_creation
        from .mint_admission import configuration
        if str(self.w3.eth.chain_id) != self.c['chain_id']:
            raise ValueError('CHAIN_MISMATCH')
        b=read_block(self.w3,int(self.c['evm_confirmations']));n=b.number
        def node(address,kind):
            checked_runtime(self.w3,address,kind,n);prove_creation(self.w3,address,kind,None,n)
            return self.w3.eth.contract(address=address,abi=artifact(kind)['abi'])
        pool=node(self.c['mining_pool'],'MiningPoolV1')
        admission=configuration(self.w3,read(pool,n,'mintAdmission'),n,expected_pool=pool.address)
        common=node(read(pool,n,'deposits'),'BitcoinDepositRegistry')
        escrow=node(read(pool,n,'escrow'),'BitcoinWithdrawalEscrow')
        batches=node(read(escrow,n,'batches'),'BitcoinBatchRegistry')
        token_address=read(pool,n,'mpToken')
        checked_runtime(self.w3,token_address,'PoolMpToken',n)
        token=self.w3.eth.contract(address=token_address,abi=artifact('PoolMpToken')['abi'])
        # MiningPoolV1's proven constructor creates its immutable MP child at
        # CREATE nonce 1. Generic direct-CREATE provenance cannot prove this child.
        import rlp
        expected_token=Web3.to_checksum_address(keccak(rlp.encode([bytes.fromhex(pool.address[2:]),1]))[-20:])
        script=bytes(read(pool,n,'payoutScript'))
        if (token.address!=expected_token or len(script)!=34 or script[:2]!=b'\x51\x20' or read(token,n,'poolDAO')!=pool.address
                or read(common,n,'scriptPools',keccak(script))!=pool.address
                or bytes(read(common,n,'poolScripts',pool.address))!=keccak(script)
                or read(common,n,'spendRecorder')!=escrow.address or read(escrow,n,'depositRegistry')!=common.address
                or read(batches,n,'withdrawalEscrow')!=escrow.address
                or read(pool,n,'authorization')!=read(common,n,'authorization')
                or read(pool,n,'authorization')!=read(batches,n,'authorization')
                or read(common,n,'spv')!=read(escrow,n,'spv')):
            raise ValueError('SMP1_GRAPH')
        spv_address=read(common,n,'spv')
        spv=self.w3.eth.contract(address=spv_address,abi=artifact('RegtestHeaderRelay' if self.c['bitcoin_network']=='regtest' else 'MainnetSPVGuard')['abi'])
        relay=None
        if self.c['bitcoin_network']=='regtest':
            node(spv_address,'RegtestHeaderRelay')
        else:
            from .zk_spv import is_zk,inspect_binding
            from .bitcoin_cli import _checked_mainnet_relay
            if is_zk(self.c['bitcoin_policy']):
                from .zk_profiles import profile as zk_profile
                spv=self.w3.eth.contract(address=spv_address,abi=artifact(zk_profile(self.c['bitcoin_policy'])['adapter'])['abi'])
                relay,_,_,_=inspect_binding(self.w3,read(spv,n,'state'),self.c['bitcoin_policy'],n,adapter_address=spv_address)
            else:
                node(spv_address,'MainnetSPVGuard')
                relay=_checked_mainnet_relay(self.w3,read(spv,n,'relay'),self.c['bitcoin_policy'],block=b)
                if int(read(spv,n,'maxTipAgeSeconds'))!=int(self.c['bitcoin_policy']['max_tip_age_seconds']):
                    raise ValueError('SMP1_GRAPH')
        registry=None;cg='0'
        if read(common,n,'addressRegistry')!=ZERO:
            from .address_mint import configuration as address_configuration
            registry=address_configuration(self.w3,pool,n)['registry'];cg=str(read(registry,n,'custodyGeneration'))
        if read(pool,n,'mintProofVersion')!=(2 if registry else 1):
            raise ValueError('SMP1_GRAPH')
        identity=dict(pool=pool.address,token=token.address,common=common.address,escrow=escrow.address,
            batches=batches.address,admission=admission['board'].address,spv=spv.address,
            registry=registry.address if registry else ZERO,script=script.hex())
        self.stable(b)
        return dict(pool=pool,common=common,escrow=escrow,batches=batches,token=token,admission=admission,
            spv=spv,relay=relay,registry=registry,cg=cg,block=b,identity=identity)

    def stable(self,b):
        if self.w3.eth.get_block(b.number).hash!=b.hash:
            raise ValueError('SMP1_EVM_REORG')

    def wallet(self):
        self.bitcoin.check_network()
        from .bootstrap_withdrawal_fee import genesis
        if self.bitcoin.call('getblockhash',0)!=genesis(self.c['bitcoin_network'])[2:]:
            raise ValueError('BITCOIN_NETWORK_MISMATCH')
        info=self.bitcoin.call('getwalletinfo')
        version=self.bitcoin.call('getnetworkinfo')['version']
        if info.get('walletname')!=self.c['wallet_name'] or type(version) is not int or not 300000<=version<310000:
            raise ValueError('SMP1_WALLET_PROFILE')
        return info

    def recipient_policy(self,ctx):
        if ctx['registry']:
            expected=read(ctx['registry'],ctx['block'].number,'recipientPolicy')
            if self.c.get('recipient_policy',expected)!=expected:raise ValueError('RECIPIENT_POLICY_ORIGIN')
            return expected
        address=self.c.get('recipient_policy')
        if not address:raise ValueError('RECIPIENT_SELECTION_REQUIRED')
        return address

    def recipient_preview(self,recipient,mode):
        from .recipient_profiles import preview
        ctx=self.context();g=ctx['identity']
        q=preview(self.w3,self.recipient_policy(ctx),g['pool'],g['pool'],g['token'],ZERO,recipient,mode,ctx['block'].number,
            sinks=self.recipient_sinks(ctx))
        self.stable(ctx['block'])
        return dict(status='RECIPIENT_REVIEW',selection=q,confirmation_hash=digest(q),ready=False)

    def recipient_sinks(self,ctx):
        sinks=set(ctx['identity'].values());n=ctx['block'].number
        for key in ('authorization','coordinator'):
            if key in ctx['admission']:sinks.add(ctx['admission'][key].address)
        if ctx['registry']:
            sinks.add(read(ctx['registry'],n,'verifier'));sinks.add(read(ctx['registry'],n,'dkg'))
        return sinks

    def fence(self,ctx,recipient,*,expected_cg=None,recipient_choice=None,recipient_accepted=None):
        n=ctx['block'].number
        if (not Web3.is_checksum_address(recipient) or recipient==ZERO or recipient in ctx['identity'].values()):
            raise ValueError('SMP1_RECIPIENT_PROFILE')
        if recipient_choice is None:
            if self.w3.eth.get_code(recipient,block_identifier=n) or self.w3.eth.get_code(recipient,block_identifier='latest'):
                raise ValueError('SMP1_RECIPIENT_PROFILE')
        else:
            from .recipient_profiles import review_quote,require_not_sink
            q=recipient_choice;g=ctx['identity']
            if (q['policy']!=self.recipient_policy(ctx) or q['namespace']!=g['pool'] or q['pool']!=g['pool']
                    or q['token']!=g['token'] or q['requester']!=ZERO or q['recipient']!=recipient):
                raise ValueError('RECIPIENT_SELECTION_REQUIRED')
            if ctx['registry']:require_not_sink(ctx['registry'],n,g['pool'],g['token'],recipient)
            sinks=self.recipient_sinks(ctx)
            review_quote(self.w3,q,recipient_accepted,n,sinks=sinks)
            review_quote(self.w3,q,recipient_accepted,self.w3.eth.get_block('latest').number,sinks=sinks)
        if ctx['registry']:
            from .bitcoin_continuity import Catalog,sample
            c=dict(registry=ctx['registry'],common=ctx['common'],batch=ctx['batches'])
            Catalog(identity_file=self.c['identity_file']).authorize(self.w3,sample(self.w3,c,n),'BASE_MINT')
            cg=int(ctx['cg'])
            if expected_cg is not None and cg!=int(expected_cg):
                raise ValueError('SMP1_GENERATION_CHANGED')
            read(ctx['registry'],n,'requireCustody',cg);read(ctx['registry'],'latest','requireCustody',cg)
        if self.c['bitcoin_network']!='regtest':
            from .bitcoin_cli import _inspect_mainnet_relay
            _inspect_mainnet_relay(self.w3,self.bitcoin,ctx['relay'],self.c['bitcoin_policy'],self.c['evm_confirmations'])
            if not read(ctx['spv'],n,'isFresh'):raise ValueError('MAINNET_RELAY_STALE')
        self.stable(ctx['block'])

    def inputs(self,ctx,parsed):
        n=ctx['block'].number;seen=[]
        if len(parsed['inputs'])>int(self.c['max_inputs']):raise ValueError('SMP1_INPUT_LIMIT')
        for item in parsed['inputs']:
            txid,index=item['outpoint'].split(':');vout=int(index)
            row=self.bitcoin.call('gettxout',txid,vout,True)
            if (row is None or type(row.get('coinbase')) is not bool
                    or satoshis(row['value'])!=int(item['amount']) or row['scriptPubKey']['hex']!=item['script']
                    or int(row['confirmations'])<(101 if row.get('coinbase') else 1)):
                raise ValueError('SMP1_INPUT_UNAVAILABLE')
            key=keccak(encode(['bytes32','uint32'],[bytes.fromhex(txid)[::-1],vout]))
            script=keccak(bytes.fromhex(item['script']))
            display=keccak(encode(['bytes32','uint32'],[bytes.fromhex(txid),vout]))
            if (read(ctx['common'],n,'consumed',key) or read(ctx['common'],n,'walletOutput',key)
                    or read(ctx['common'],n,'custodyTransactions',bytes.fromhex(txid)[::-1])
                    or read(ctx['common'],n,'scriptPools',script)!=ZERO
                    or bytes(read(ctx['common'],n,'addressIntents',script))!=bytes(32)
                    or bytes(read(ctx['common'],n,'feeScripts',script))!=bytes(32)
                    or bytes(read(ctx['batches'],n,'reservations',display))!=bytes(32)):
                raise ValueError('SMP1_CUSTODY_SOURCE')
            seen.append(dict(txid=txid,vout=vout))
        self.stable(ctx['block'])
        return seen

    def validate_transaction(self,q,parsed):
        expected=deposit_commitment(q['chain_id'],q['graph']['pool'],q['vout'],[q['recipient']],[q['mp_units']])
        outputs=parsed['outputs']
        if (len(outputs) not in (2,3) or parsed['fee_sats']!=q['fee_sats'] or parsed['unsigned']!=q['unsigned']
                or parsed['txid']!=q['txid'] or parsed['inputs']!=q['inputs']):
            raise ValueError('SMP1_QUOTE_CHANGED')
        for out in outputs:
            if out['vout']==q['vout']:
                valid=out['script']==q['graph']['script'] and out['amount']==q['amount_sats']
            elif out['vout']==q['marker_vout']:
                valid=out['script']==expected['script'] and out['amount']=='0'
            else:
                valid=out['script']==q['change_script'] and out['amount']==q['change_sats']
            if not valid:raise ValueError('SMP1_QUOTE_CHANGED')

    def fee_reserve_status(self,ctx):
        n=ctx['block'].number;where=read(ctx['batches'],n,'feeReserve')
        if where==ZERO:return dict(status='NOT_CONFIGURED')
        from .bitcoin_trust import checked_runtime
        from .contract_provenance import prove_creation
        checked_runtime(self.w3,where,'BitcoinFeeReserve',n)
        prove_creation(self.w3,where,'BitcoinFeeReserve',None,n)
        reserve=self.w3.eth.contract(address=where,abi=artifact('BitcoinFeeReserve')['abi'])
        if read(reserve,n,'batches')!=ctx['batches'].address or read(reserve,n,'deposits')!=ctx['common'].address:
            raise ValueError('SMP1_GRAPH')
        return dict(status='ONCHAIN_CLAIMS_RECONCILIATION_UNKNOWN',reserve=where,
            free_sats=str(read(reserve,n,'freeSats')),locked_sats=str(read(reserve,n,'lockedSats')),
            promised_sats=str(read(reserve,n,'promisedSats')))

    def prepare(self,recipient,amount,fee_rate,*,max_fee,ttl='300',recipient_choice=None,recipient_accepted=None):
        with self.lock():
            ctx=self.context();self.wallet()
            self.fence(ctx,recipient,recipient_choice=recipient_choice,recipient_accepted=recipient_accepted)
            sats=number(amount,1,2100000000000000);rate=number(fee_rate,1,10000)
            cap=number(max_fee,1,int(self.c['max_fee_sats']))
            destination=crypto.call('bitcoin_script_address_v1',script=ctx['identity']['script'],network=self.c['bitcoin_network'])['address']
            change=self.bitcoin.call('getrawchangeaddress','bech32')
            change_script=crypto.call('bitcoin_address_script_v1',address=change,network=self.c['bitcoin_network'])['script']
            if self.bitcoin.call('getaddressinfo',change).get('ismine') is not True or change_script==ctx['identity']['script']:
                raise ValueError('SMP1_CHANGE')
            marker=deposit_commitment(self.c['chain_id'],ctx['pool'].address,'0',[recipient],[str(sats*10**10)])
            row=self.bitcoin.call('walletcreatefundedpsbt',[],[{destination:btc_amount(sats)},{'data':marker['data']}],0,
                dict(add_inputs=True,include_unsafe=False,minconf=1,changeAddress=change,changePosition=2,
                     lockUnspents=False,fee_rate=str(rate),subtractFeeFromOutputs=[],replaceable=False),True)
            parsed=inspect(row['psbt'])
            deposit=[o for o in parsed['outputs'] if o['script']==ctx['identity']['script'] and o['amount']==str(sats)]
            marks=[o for o in parsed['outputs'] if o['script']==marker['script'] and o['amount']=='0']
            if len(deposit)!=1 or len(marks)!=1 or len(parsed['outputs']) not in (2,3):
                raise ValueError('SMP1_QUOTE_CHANGED')
            vout=deposit[0]['vout'];marker_vout=marks[0]['vout']
            correct=deposit_commitment(self.c['chain_id'],ctx['pool'].address,vout,[recipient],[str(sats*10**10)])
            parsed=inspect(row['psbt'],replace_marker=dict(vout=marker_vout,script=correct['script']))
            remainder=[o for o in parsed['outputs'] if o['vout'] not in (vout,marker_vout)]
            if remainder and (remainder[0]['script']!=change_script or int(remainder[0]['amount'])<=0):
                raise ValueError('SMP1_CHANGE')
            fee=int(parsed['fee_sats'])
            if fee<=0 or fee>cap or satoshis(row['fee'])!=fee:
                raise ValueError('SMP1_FEE_CAP')
            self.inputs(ctx,parsed);self.fence(ctx,recipient,recipient_choice=recipient_choice,recipient_accepted=recipient_accepted)
            q=dict(version='1',profile=PROFILE,operation_id=secrets.token_hex(32),chain_id=self.c['chain_id'],
                bitcoin_network=self.c['bitcoin_network'],wallet_name=self.c['wallet_name'],graph=ctx['identity'],
                custody_generation=ctx['cg'],recipient=recipient,recipient_profile=recipient_choice['mode'] if recipient_choice else 'ORDINARY_ADDRESS_V1',
                amount_sats=str(sats),mp_units=str(sats*10**10),vout=vout,marker_vout=marker_vout,
                allocation_hash=correct['allocation_hash'],change_address=change,change_script=change_script,
                change_sats=remainder[0]['amount'] if remainder else '0',fee_sats=str(fee),fee_cap_sats=str(cap),
                fee_rate_sat_vb=str(rate),psbt=psbt_base64(parsed['psbt']),unsigned=parsed['unsigned'],txid=parsed['txid'],
                inputs=parsed['inputs'],created_at=str(int(time.time())),expires_at=str(int(time.time())+number(ttl,1,3600)),
                anchor=dict(number=str(ctx['block'].number),hash=ctx['block'].hash.hex()))
            if recipient_choice is not None:q['recipient_choice']=recipient_choice
            self.validate_transaction(q,parsed)
            self.journal.put('deposit:'+q['operation_id'],q)
            return dict(status='SMP1_QUOTE',quote=q,quote_hash=digest(q),ready=False,fee_reserve=self.fee_reserve_status(ctx),
                funding_profile=PROFILE,fallback='ADDRESS_FLOW_IF_SIGNER_UNSUPPORTED',bitcoin_address=destination,
                guarantees='BTC amount is fixed; wallet fee is separate; only MP is minted after proof/admission/finality')

    def entry(self,operation_id,accepted=None):
        if not isinstance(operation_id,str) or len(operation_id)!=64 or bytes.fromhex(operation_id).hex()!=operation_id:
            raise ValueError('SMP1_OPERATION')
        entry=self.journal.get('deposit:'+operation_id);q=entry['request']
        if accepted is not None and accepted!=digest(q):raise ValueError('SMP1_QUOTE_ACCEPTANCE')
        parsed=inspect(q['psbt']);self.validate_transaction(q,parsed)
        return entry,q,parsed

    def current(self,q,parsed,*,new_signature=False):
        ctx=self.context();self.wallet()
        if ctx['identity']!=q['graph'] or q['wallet_name']!=self.c['wallet_name']:
            raise ValueError('SMP1_GRAPH')
        if self.w3.eth.get_block(int(q['anchor']['number'])).hash.hex()!=q['anchor']['hash']:
            raise ValueError('SMP1_EVM_REORG')
        if new_signature and int(time.time())>=int(q['expires_at']):raise ValueError('SMP1_QUOTE_EXPIRED')
        extra=dict(recipient_choice=q['recipient_choice'],recipient_accepted=digest(q['recipient_choice'])) if 'recipient_choice' in q else {}
        self.fence(ctx,q['recipient'],expected_cg=q['custody_generation'],**extra);self.inputs(ctx,parsed)
        return ctx

    def reserve(self,entry,q,parsed):
        key=entry['id'];state=entry['status'];result=entry['result']
        if state in ('RESERVING','RELEASING'):
            raise ValueError('SMP1_LOCK_RECOVERY_REQUIRED')
        wanted=[dict(txid=i['outpoint'].split(':')[0],vout=int(i['outpoint'].split(':')[1])) for i in parsed['inputs']]
        locked={(x['txid'],x['vout']) for x in self.bitcoin.call('listlockunspent')}
        if state=='PREPARED':
            if any((x['txid'],x['vout']) in locked for x in wanted):raise ValueError('SMP1_FOREIGN_LOCK')
            self.journal.update(key,'RESERVING',dict(inputs=wanted))
            if self.bitcoin.call('lockunspent',False,wanted,True) is not True:
                raise ValueError('SMP1_LOCK_RECOVERY_REQUIRED')
            result=dict(inputs=wanted);self.journal.update(key,'RESERVED',result)
        elif not all((x['txid'],x['vout']) in locked for x in wanted):
            raise ValueError('SMP1_LOCK_RECOVERY_REQUIRED')
        return result

    def sign(self,operation_id,accepted):
        with self.lock():
            entry,q,parsed=self.entry(operation_id,accepted)
            if entry['status'] in ('SIGNED','BROADCASTING','BROADCAST'):
                return self.summary(entry,q)
            if entry['status']=='PARTIALLY_SIGNED':
                return self.finalize(entry['id'],q,entry['result'])
            if entry['status'] not in ('PREPARED','RESERVED'):
                raise ValueError('SMP1_SIGNING_RECOVERY_REQUIRED')
            self.current(q,parsed,new_signature=True)
            result=self.reserve(entry,q,parsed)
            self.journal.update(entry['id'],'SIGNING_REQUESTED',result)
            row=self.bitcoin.call('walletprocesspsbt',q['psbt'],True,'ALL',True,False)
            updated=inspect(row['psbt'],allow_signed=True);self.validate_transaction(q,updated)
            if not updated['has_signatures']:
                # A successful, checked unsigned reply is different from a lost
                # response: no signature was delivered by this signing attempt.
                self.journal.update(entry['id'],'RESERVED',result)
                return dict(status='SIGNER_UNSUPPORTED_USE_PSBT_OR_ADDRESS_FLOW',operation_id=operation_id,
                            commitment_retained=True,cancel_available=True,ready=False)
            result['signed_psbt']=row['psbt']
            self.journal.update(entry['id'],'PARTIALLY_SIGNED',result)
            return self.finalize(entry['id'],q,result)

    def finalize(self,key,q,result):
        row=self.bitcoin.call('finalizepsbt',result['signed_psbt'],True)
        if row.get('complete') is not True:
            return dict(status='EXTERNAL_SIGNER_OR_ADDRESS_FLOW_REQUIRED',operation_id=q['operation_id'],
                commitment_retained=True,ready=False)
        parsed=inspect(q['psbt'],raw=row['hex']);self.validate_transaction(q,parsed)
        result['raw']=parsed['raw'];result['vsize']=parsed['vsize']
        self.journal.update(key,'SIGNED',result)
        return self.summary(self.journal.get(key),q)

    def export(self,operation_id,accepted,destination):
        from .dkg import atomic_private
        with self.lock():
            entry,q,parsed=self.entry(operation_id,accepted)
            if entry['status'] not in ('PREPARED','RESERVED','EXPORTED','PARTIALLY_SIGNED','SIGNING_REQUESTED'):
                raise ValueError('SMP1_SIGNING_RECOVERY_REQUIRED')
            if entry['status'] in ('PREPARED','RESERVED'):
                self.current(q,parsed,new_signature=True)
                result=self.reserve(entry,q,parsed)
            else:
                result=entry['result']  # Read-only recovery of potentially signed bytes.
            # Treat exported bytes as potentially signed even if the exporter
            # crashes before writing the file. Never automatically unlock them.
            self.journal.update(entry['id'],'EXPORTED',result)
            atomic_private(destination,((result.get('signed_psbt') or q['psbt'])+'\n').encode())
            return dict(status='PSBT_EXPORTED',operation_id=operation_id,commitment_retained=True,ready=False)

    def import_signed(self,operation_id,accepted,encoded):
        with self.lock():
            entry,q,parsed=self.entry(operation_id,accepted)
            if entry['status'] not in ('EXPORTED','PARTIALLY_SIGNED','SIGNING_REQUESTED'):
                raise ValueError('SMP1_SIGNING_RECOVERY_REQUIRED')
            updated=inspect(encoded,allow_signed=True);self.validate_transaction(q,updated)
            if not updated['has_signatures']:raise ValueError('SMP1_SIGNATURE_REQUIRED')
            self.current(q,parsed)
            result=entry['result'];result['signed_psbt']=encoded
            self.journal.update(entry['id'],'PARTIALLY_SIGNED',result)
            return self.finalize(entry['id'],q,result)

    def broadcast(self,operation_id,accepted):
        with self.lock():
            entry,q,parsed=self.entry(operation_id,accepted)
            if entry['status'] not in ('SIGNED','BROADCASTING','BROADCAST'):
                raise ValueError('SMP1_SIGNATURE_REQUIRED')
            result=entry['result'];final=inspect(q['psbt'],raw=result['raw']);self.validate_transaction(q,final)
            self.wallet()
            # Recover a lost send result using the exact txid and raw envelope.
            try:
                observed=self.bitcoin.call('getrawtransaction',q['txid'],False)
            except BitcoinRPCError as error:
                if error.rpc_code!=-5:raise
            else:
                checked=inspect(q['psbt'],raw=observed);self.validate_transaction(q,checked)
                result['observed_wtxid']=checked['wtxid']
                self.journal.update(entry['id'],'BROADCAST',result)
                return self.summary(self.journal.get(entry['id']),q)
            self.current(q,parsed)
            response=self.bitcoin.call('testmempoolaccept',[result['raw']])
            if (len(response)!=1 or response[0].get('txid')!=q['txid'] or response[0].get('allowed') is not True
                    or satoshis(response[0]['fees']['base'])!=int(q['fee_sats']) or str(response[0]['vsize'])!=final['vsize']):
                raise ValueError('SMP1_MEMPOOL_REJECTED')
            self.journal.update(entry['id'],'BROADCASTING',result)
            if self.bitcoin.call('sendrawtransaction',result['raw'])!=q['txid']:
                raise ValueError('SMP1_BROADCAST_CONFLICT')
            self.journal.update(entry['id'],'BROADCAST',result)
            return self.summary(self.journal.get(entry['id']),q)

    def cancel(self,operation_id,accepted):
        with self.lock():
            entry,q,parsed=self.entry(operation_id,accepted)
            if entry['status']=='CANCELLED':return self.summary(entry,q)
            if entry['status'] not in ('PREPARED','RESERVED'):raise ValueError('SMP1_SIGNATURE_MAY_EXIST')
            if entry['status']=='RESERVED':
                self.wallet();result=self.reserve(entry,q,parsed)
                self.journal.update(entry['id'],'RELEASING',result)
                if self.bitcoin.call('lockunspent',True,result['inputs']) is not True:
                    raise ValueError('SMP1_LOCK_RECOVERY_REQUIRED')
            self.journal.update(entry['id'],'CANCELLED',entry['result'])
            return self.summary(self.journal.get(entry['id']),q)

    def summary(self,entry,q):
        return dict(status=entry['status'],operation_id=q['operation_id'],quote_hash=digest(q),txid=q['txid'],
            vout=q['vout'],recipient=q['recipient'],amount_sats=q['amount_sats'],mp_units=q['mp_units'],fee_sats=q['fee_sats'],
            mint_request=dict(chain_id=q['chain_id'],mining_pool=q['graph']['pool'],txid=q['txid'],vout=q['vout'],
                recipients=[q['recipient']],amounts=[q['mp_units']]),ready=False)

    def status(self,operation_id):
        entry,q,parsed=self.entry(operation_id)
        result=self.summary(entry,q);ctx=self.context();n=ctx['block'].number
        if ctx['identity']!=q['graph']:raise ValueError('SMP1_GRAPH')
        key=keccak(encode(['bytes32','uint32'],[bytes.fromhex(q['txid'])[::-1],int(q['vout'])]))
        expected=keccak(encode(['address[]','uint256[]'],[[q['recipient']],[int(q['mp_units'])]]))
        def minted(at):
            allocation=bytes(read(ctx['pool'],at,'mintedAllocations',key))
            if allocation not in (bytes(32),expected):raise ValueError('BTC_MINT_ALLOCATION_MISMATCH')
            return allocation==expected
        result['mint_status']='MP_MINT_FINAL' if minted(n) else 'EVM_FINALITY_PENDING' if minted('latest') else 'MINT_ADMISSION_REQUIRED'
        result['bitcoin_status']='NOT_OBSERVED';result['spv_status']='UNKNOWN'
        result['fee_reserve']=self.fee_reserve_status(ctx)
        try:
            self.bitcoin.check_network()
        except (BitcoinRPCError,ValueError,RuntimeError) as error:
            result['bitcoin_status']='UNKNOWN';result['observation']=observation_error(error)
            self.stable(ctx['block'])
            return result
        try:
            tx=self.bitcoin.call('getrawtransaction',q['txid'],True)
            final=inspect(q['psbt'],raw=tx['hex']);self.validate_transaction(q,final)
            depth=max(6,int(read(ctx['common'],n,'depth')))
            confirmations=max(0,int(tx.get('confirmations',0)))
            result.update(bitcoin_status='MATURITY_REACHED' if confirmations>=depth else 'CONFIRMATIONS_PENDING',
                          confirmations=str(confirmations),required_confirmations=str(depth))
            if confirmations and tx.get('blockhash'):
                header=self.bitcoin.call('getblockheader',tx['blockhash'],True)
                if self.bitcoin.call('getblockhash',header['height'])!=tx['blockhash']:raise ValueError('SMP1_BITCOIN_REORG')
                canonical,count=read(ctx['spv'],n,'getBlockStatus',bytes.fromhex(tx['blockhash'])[::-1])
                result['spv_status']='HEADER_COVERED_INCLUSION_PENDING' if canonical and int(count)>=depth else 'FRESH_OR_ROOT_REQUIRED'
            if result['mint_status']=='MINT_ADMISSION_REQUIRED':
                from .mint_admission import claim_id
                claim=read(ctx['admission']['board'],n,'getClaim',claim_id(ctx['pool'].address,bytes.fromhex(q['txid'])[::-1],int(q['vout'])))
                if int(claim[6]):result['mint_status']='MINT_AUTHORIZATION_OR_EXECUTION_PENDING';result['mint_session']=str(claim[6])
        except BitcoinRPCError as error:
            result['bitcoin_status']='NOT_OBSERVED' if error.rpc_code==-5 else 'UNKNOWN'
            result['observation']=observation_error(error)
        self.stable(ctx['block'])
        return result
