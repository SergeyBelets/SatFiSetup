"""Real rust-bitcoin PSBT codec and SQLite; Core/EVM boundaries are fixtures."""
import base64
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
import hashlib
import json

import pytest
from eth_utils import keccak
from eth_abi import encode
from hexbytes import HexBytes
from web3.datastructures import AttributeDict

from signer_agent import smp1_deposit as m
from signer_agent.smp1_deposit import Deposit,inspect,psbt_base64
from signer_agent.bitcoin_rpc import BitcoinRPCError
from signer_agent.bitcoin_funding import deposit_commitment
from signer_agent.operations import Journal
from signer_agent.bootstrap_create import create_address
from test_address_mint import Functions,address


def compact(n):
    return bytes([n]) if n<253 else b'\xfd'+n.to_bytes(2,'little')


def psbt_fixture(outputs,*,value=200000,input_script=None,sequence=0xffffffff,sighash=None,duplicate=False):
    script=bytes.fromhex(input_script or ('5120'+'33'*32))
    prev=bytes.fromhex('11'*32)+bytes(4)
    txin=prev+b'\x00'+sequence.to_bytes(4,'little')
    output=lambda amount,s:amount.to_bytes(8,'little')+compact(len(s))+s
    tx=b'\x02\x00\x00\x00'+compact(2 if duplicate else 1)+txin*(2 if duplicate else 1)+compact(len(outputs))
    tx+=b''.join(output(a,bytes.fromhex(s)) for a,s in outputs)+bytes(4)
    witness=output(value,script)
    imap=b'\x01\x01'+compact(len(witness))+witness
    if sighash is not None:imap+=b'\x01\x03\x04'+sighash.to_bytes(4,'little')
    raw=b'psbt\xff\x01\x00'+compact(len(tx))+tx+b'\x00'+(imap+b'\x00')*(2 if duplicate else 1)+b'\x00'*len(outputs)
    return base64.b64encode(raw).decode(),tx


def signed_raw(unsigned,*,bad_hash=False):
    # One native Taproot input, key-path witness; validation of these fixture
    # signatures is deliberately the mocked Core boundary, not a PASS claim.
    sig=b'\x44'*64+(b'\x02' if bad_hash else b'\x01')
    return (unsigned[:4]+b'\x00\x01'+unsigned[4:-4]+b'\x01'+compact(len(sig))+sig+unsigned[-4:]).hex()


def signed_psbt(encoded):
    raw=base64.b64decode(encoded);pos=5
    def varint(at):
        if raw[at]<253:return raw[at],at+1
        assert raw[at]==253
        return int.from_bytes(raw[at+1:at+3],'little'),at+3
    # Skip the global map, then insert a Taproot key signature in the first input.
    for stage in range(2):
        while raw[pos]!=0:
            size,pos=varint(pos);pos+=size
            size,pos=varint(pos);pos+=size
        if stage==0:pos+=1
    entry=b'\x01\x13\x41'+b'\x44'*64+b'\x01'
    return base64.b64encode(raw[:pos]+entry+raw[pos:]).decode()


def test_native_psbt_infers_outputs_fees_and_rewrites_only_unsigned_marker():
    c=deposit_commitment('31337',address(1),'0',[address(2)],['1000000000000000'])
    encoded,tx=psbt_fixture([(0,c['script']),(100000,'5120'+'22'*32),(99000,'0014'+'55'*20)])
    before=inspect(encoded)
    assert before['fee_sats']=='1000' and before['unsigned']==tx.hex()
    revised=deposit_commitment('31337',address(1),'1',[address(2)],['1000000000000000'])
    after=inspect(encoded,replace_marker=dict(vout='0',script=revised['script']))
    assert after['outputs'][0]['script']==revised['script']
    assert after['fee_sats']==before['fee_sats'] and after['inputs']==before['inputs']
    assert after['txid']!=before['txid']
    final=inspect(psbt_base64(after['psbt']),raw=signed_raw(bytes.fromhex(after['unsigned'])))
    assert final['txid']==after['txid'] and final['wtxid']!=final['txid']


@pytest.mark.parametrize('kwargs',[dict(sequence=0xfffffffd),dict(input_script='76a914'+'11'*20+'88ac'),
    dict(sighash=2),dict(sighash=0x81),dict(duplicate=True)])
def test_native_rejects_unsupported_inputs_rbf_or_partial_sighash(kwargs):
    encoded,_=psbt_fixture([(199000,'5120'+'22'*32)],**kwargs)
    with pytest.raises(RuntimeError):inspect(encoded)


def test_native_rejects_trailing_data_changed_final_outputs_and_none_signature():
    encoded,tx=psbt_fixture([(199000,'5120'+'22'*32)])
    with pytest.raises(RuntimeError):inspect(base64.b64encode(base64.b64decode(encoded)+b'\x00').decode())
    with pytest.raises(RuntimeError):inspect(encoded,raw=signed_raw(tx,bad_hash=True))
    changed=bytearray(tx);changed[-5]^=1
    with pytest.raises(RuntimeError):inspect(encoded,raw=signed_raw(changed))


@pytest.fixture
def client(tmp_path,monkeypatch):
    pool=address(1);token=create_address(pool,1);recipient=address(2)
    block=AttributeDict(dict(number=7,hash=HexBytes('77'*32),timestamp=1000))
    deposit_script='5120'+'22'*32
    base=dict(pool=pool,token=token,common=address(3),escrow=address(4),batches=address(5),admission=address(6),
              spv=address(7),registry=m.ZERO,script=deposit_script)
    values=dict(minted=bytes(32),consumed=False,custody=False,reservation=bytes(32))
    def node(where,functions):return SimpleNamespace(address=where,functions=Functions(functions))
    common=node(base['common'],dict(depth=6,consumed=lambda *_:values['consumed'],walletOutput=lambda *_:False,
        custodyTransactions=lambda *_:values['custody'],scriptPools=lambda *_:m.ZERO,
        addressIntents=lambda *_:bytes(32),feeScripts=lambda *_:bytes(32)))
    ctx=dict(identity=base,cg='0',registry=None,block=block,common=common,
        pool=node(pool,dict(mintedAllocations=lambda *_:values['minted'])),
        batches=node(base['batches'],dict(reservations=lambda *_:values['reservation'],feeReserve=m.ZERO)),
        admission=dict(board=node(base['admission'],dict(getClaim=lambda *_:[0,0,0,0,0,0,0]))),
        spv=node(base['spv'],dict(getBlockStatus=lambda *_:[True,6])))
    class Core:
        def check_network(self):return {}
        def __init__(self):
            self.calls=[];self.locked=[];self.sent=None;self.sign_fail=False;self.reject=False;self.reorder=True;self.reply_loss=False;self.cannot_sign=False
        def call(self,method,*args):
            self.calls.append((method,args))
            if method=='getrawchangeaddress':return change_address
            if method=='getaddressinfo':return dict(ismine=True)
            if method=='walletcreatefundedpsbt':
                payload=args[1][1]['data'];marker='6a38'+payload
                outputs=[(0,marker),(100000,deposit_script),(99000,change_script)] if self.reorder else [(100000,deposit_script),(0,marker),(99000,change_script)]
                encoded,tx=psbt_fixture(outputs)
                return dict(psbt=encoded,fee=Decimal('0.00001000'),changepos=2)
            if method=='gettxout':return dict(value=Decimal('0.00200000'),scriptPubKey=dict(hex='5120'+'33'*32),confirmations=6,coinbase=False)
            if method=='listlockunspent':return self.locked
            if method=='lockunspent':
                self.locked=[] if args[0] else args[1]
                return True
            if method=='walletprocesspsbt':
                if self.sign_fail:raise BitcoinRPCError('BITCOIN_RPC_FAILED')
                return dict(psbt=args[0] if self.cannot_sign else signed_psbt(args[0]),complete=False)
            if method=='finalizepsbt':
                p=inspect(args[0],allow_signed=True);return dict(complete=True,hex=signed_raw(bytes.fromhex(p['unsigned'])))
            if method=='getrawtransaction':
                if self.sent is None:raise BitcoinRPCError('BITCOIN_RPC_REJECTED',-5)
                if args[1] is False:return self.sent
                return dict(hex=self.sent,confirmations=6,blockhash='88'*32)
            if method=='testmempoolaccept':
                p=inspect(quote()['quote']['psbt'],raw=args[0][0])
                return [dict(txid=p['txid'],allowed=not self.reject,fees=dict(base=Decimal('0.00001000')),vsize=int(p['vsize']))]
            if method=='sendrawtransaction':
                self.sent=args[0]
                if self.reply_loss:raise BitcoinRPCError('BITCOIN_RPC_FAILED')
                return quote()['quote']['txid']
            if method=='getblockheader':return dict(height=100)
            if method=='getblockhash':return '88'*32
            raise AssertionError(method)
    d=Deposit.__new__(Deposit)
    d.c=dict(chain_id='31337',bitcoin_network='regtest',wallet_name='funding',max_fee_sats='5000',max_inputs='16')
    d.w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda *_:block))
    d.journal=Journal(tmp_path/'wallet.sqlite');d.bitcoin=Core()
    d.context=lambda:ctx;d.wallet=lambda:{};d.fence=lambda *a,**kw:None
    change_script='0014'+'55'*20
    # Public address placeholder: this boundary is a fixture, not an address vector.
    change_address='bcrt1q24242424242424242424242424242424dl3p9y'
    # The actual decoder is tested separately; fixtures return this exact script
    # only for the change address and retain native PSBT parsing everywhere.
    original=m.crypto.call
    def call(op,**params):
        if op=='bitcoin_address_script_v1':return dict(address=change_address,script=change_script)
        return original(op,**params)
    monkeypatch.setattr(m.crypto,'call',call)
    saved={}
    def quote():
        if not saved:saved.update(d.prepare(recipient,'100000','2',max_fee='2000'))
        return saved
    return d,quote,values,ctx


def test_quote_computes_vout_commitment_fee_and_mp_without_signing_or_locking(client):
    d,quote,_,_=client;q=quote()['quote']
    assert q['vout']=='1' and q['marker_vout']=='0' and q['amount_sats']=='100000'
    assert q['mp_units']=='1000000000000000' and q['fee_sats']=='1000'
    marker=inspect(q['psbt'])['outputs'][0]
    assert marker['script']==deposit_commitment('31337',q['graph']['pool'],'1',[q['recipient']],[q['mp_units']])['script']
    assert not d.bitcoin.locked and not any(x[0]=='walletprocesspsbt' for x in d.bitcoin.calls)


def test_sign_and_lost_broadcast_reply_replay_same_transaction_without_double_send(client):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    assert d.sign(op,accepted)['status']=='SIGNED'
    assert d.sign(op,accepted)['status']=='SIGNED'
    assert sum(c[0]=='walletprocesspsbt' for c in d.bitcoin.calls)==1
    d.bitcoin.reply_loss=True
    with pytest.raises(BitcoinRPCError):d.broadcast(op,accepted)
    assert d.journal.get('deposit:'+op)['status']=='BROADCASTING'
    assert d.broadcast(op,accepted)['status']=='BROADCAST'
    assert sum(c[0]=='sendrawtransaction' for c in d.bitcoin.calls)==1
    status=d.status(op)
    assert status['bitcoin_status']=='MATURITY_REACHED' and status['mint_status']=='MINT_ADMISSION_REQUIRED'
    assert status['ready'] is False


def test_signing_timeout_never_unlocks_or_automatically_signs_again(client):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    d.bitcoin.sign_fail=True
    with pytest.raises(BitcoinRPCError):d.sign(op,accepted)
    assert d.journal.get('deposit:'+op)['status']=='SIGNING_REQUESTED'
    with pytest.raises(ValueError):d.sign(op,accepted)
    with pytest.raises(ValueError):d.cancel(op,accepted)
    assert d.bitcoin.locked and sum(c[0]=='walletprocesspsbt' for c in d.bitcoin.calls)==1


def test_quote_cannot_spend_custody_or_reserved_sources(client):
    d,quote,values,_=client
    values['custody']=True
    with pytest.raises(ValueError,match='SMP1_CUSTODY_SOURCE'):quote()
    assert not d.journal.list() and not d.bitcoin.locked


def test_export_keeps_marker_and_prevents_timeout_unlock(client,tmp_path):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    path=tmp_path/'wallet.psbt'
    assert d.export(op,accepted,path)['status']=='PSBT_EXPORTED'
    assert inspect(path.read_text().strip())['unsigned']==q['quote']['unsigned']
    with pytest.raises(ValueError,match='SIGNATURE_MAY_EXIST'):d.cancel(op,accepted)
    assert d.import_signed(op,accepted,signed_psbt(path.read_text().strip()))['status']=='SIGNED'


def test_foreign_lock_is_never_adopted_or_released(client):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    d.bitcoin.locked=[dict(txid='11'*32,vout=0)]
    with pytest.raises(ValueError,match='SMP1_FOREIGN_LOCK'):d.sign(op,accepted)
    d.cancel(op,accepted)
    assert d.bitcoin.locked and not any(c[0]=='lockunspent' for c in d.bitcoin.calls)


def test_uncertain_lock_acknowledgment_requires_recovery_without_unlock(client,monkeypatch):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    original=Journal.update
    def crash(j,key,status,result):
        if status=='RESERVED':raise SystemExit('after Core lock before fsync')
        return original(j,key,status,result)
    monkeypatch.setattr(Journal,'update',crash)
    with pytest.raises(SystemExit):d.sign(op,accepted)
    monkeypatch.setattr(Journal,'update',original)
    with pytest.raises(ValueError):d.sign(op,accepted)
    with pytest.raises(ValueError):d.cancel(op,accepted)
    assert d.journal.get('deposit:'+op)['status']=='RESERVING' and d.bitcoin.locked


def test_mempool_rejection_retains_signed_bytes_and_recipient_for_recovery(client):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    d.sign(op,accepted);before=d.journal.get('deposit:'+op)['result']['raw'];d.bitcoin.reject=True
    with pytest.raises(ValueError,match='MEMPOOL_REJECTED'):d.broadcast(op,accepted)
    assert d.journal.get('deposit:'+op)['result']['raw']==before and d.bitcoin.sent is None


def test_mp_issuance_is_independent_of_bitcoin_rpc_availability(client):
    d,quote,values,_=client;q=quote()['quote']
    values['minted']=keccak(encode(['address[]','uint256[]'],[[q['recipient']],[int(q['mp_units'])]]))
    def unavailable(*_):raise BitcoinRPCError('BITCOIN_RPC_FAILED')
    d.bitcoin.call=unavailable
    assert d.status(q['operation_id'])['mint_status']=='MP_MINT_FINAL'


def test_context_proves_mp_child_via_mining_constructor_not_direct_create(tmp_path,monkeypatch):
    from signer_agent import bitcoin_trust,contract_provenance,mint_admission,evm_network
    pool=address(30);token=create_address(pool,1);common=address(31);escrow=address(32);batch=address(33)
    auth=address(34);spv=address(35);admission=address(36);script=bytes.fromhex('5120'+'44'*32)
    block=AttributeDict(dict(number=10,hash=HexBytes('aa'*32)))
    def node(where,values):return SimpleNamespace(address=where,functions=Functions(values))
    nodes={pool:node(pool,dict(mintAdmission=admission,deposits=common,escrow=escrow,mpToken=token,
        payoutScript=script,authorization=auth,mintProofVersion=1)),
        token:node(token,dict(poolDAO=pool)),
        common:node(common,dict(scriptPools=lambda _:pool,poolScripts=lambda _:keccak(script),spendRecorder=escrow,
            authorization=auth,spv=spv,addressRegistry=m.ZERO)),
        escrow:node(escrow,dict(batches=batch,depositRegistry=common,spv=spv)),
        batch:node(batch,dict(withdrawalEscrow=escrow,authorization=auth)),spv:node(spv,{})}
    proofs=[]
    monkeypatch.setattr(bitcoin_trust,'checked_runtime',lambda *a:None)
    monkeypatch.setattr(contract_provenance,'prove_creation',lambda w,a,k,*args:proofs.append((a,k)))
    monkeypatch.setattr(evm_network,'read_block',lambda *a:block)
    monkeypatch.setattr(mint_admission,'configuration',lambda *a,**kw:dict(board=node(admission,{})))
    d=Deposit.__new__(Deposit);d.c=dict(chain_id='31337',evm_confirmations='1',bitcoin_network='regtest',mining_pool=pool)
    d.w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337,get_block=lambda _:block,contract=lambda address,abi:nodes[address]))
    assert d.context()['identity']['token']==token
    assert (pool,'MiningPoolV1') in proofs and not any(k=='PoolMpToken' for _,k in proofs)
    nodes[token].functions=Functions(dict(poolDAO=address(99)))
    with pytest.raises(ValueError,match='SMP1_GRAPH'):d.context()


def test_cli_produces_a_reviewable_quote_and_requires_acceptance(tmp_path,monkeypatch,capsys):
    from signer_agent import smp1_deposit_cli as cli,bitcoin_cli
    q=dict(operation_id='22'*32,chain_id='31337',bitcoin_network='regtest',recipient=address(2),recipient_profile='ORDINARY_ADDRESS_V1',
        amount_sats='100000',mp_units='1000000000000000',fee_sats='1000',fee_cap_sats='2000',fee_rate_sat_vb='2',change_address='change',change_sats='99000',graph=dict(pool=address(30),token=address(31)),
        vout='1',allocation_hash='11'*32,expires_at='9999999999',psbt='private-metadata')
    output=dict(status='SMP1_QUOTE',quote=q,quote_hash='aa'*32)
    d=SimpleNamespace(prepare=lambda *a,**kw:output)
    monkeypatch.setattr(cli,'load_config',lambda _:None);monkeypatch.setattr(cli,'Deposit',lambda _:d)
    path=tmp_path/'quote.json'
    args=['deposit','prepare','--config','config','--recipient',address(2),'--amount-sats','100000',
        '--fee-rate-sat-vb','2','--max-fee-sats','2000','--out',str(path)]
    assert bitcoin_cli.main(args)==0
    shown=json.loads(capsys.readouterr().out)
    assert 'quote' not in shown and 'private-metadata' not in json.dumps(shown)
    assert shown['quote_hash']=='aa'*32 and json.loads(path.read_text())==output
    assert path.stat().st_mode&0o077==0
    with pytest.raises(ValueError,match='SMP1_OUTPUT_EXISTS'):bitcoin_cli.main(args)


def test_bad_acceptance_and_expired_quote_never_sign_or_reserve(client,monkeypatch):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id']
    with pytest.raises(ValueError,match='SMP1_QUOTE_ACCEPTANCE'):d.sign(op,'00'*32)
    monkeypatch.setattr(m.time,'time',lambda:int(q['quote']['expires_at'])+1)
    with pytest.raises(ValueError,match='SMP1_QUOTE_EXPIRED'):d.sign(op,q['quote_hash'])
    assert not d.bitcoin.locked and not any(c[0]=='walletprocesspsbt' for c in d.bitcoin.calls)


def test_known_unsigned_signer_reply_allows_explicit_cancel_and_address_fallback(client):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];d.bitcoin.cannot_sign=True
    result=d.sign(op,q['quote_hash'])
    assert result['commitment_retained'] is True and result['cancel_available'] is True
    assert d.journal.get('deposit:'+op)['status']=='RESERVED'
    assert d.cancel(op,q['quote_hash'])['status']=='CANCELLED'
    assert not d.bitcoin.locked


def test_import_rejects_retargeted_outputs_before_finalization(client,tmp_path):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    d.export(op,accepted,tmp_path/'quote.psbt')
    marker=deposit_commitment('31337',q['quote']['graph']['pool'],'1',[address(88)],[q['quote']['mp_units']])
    changed=inspect(q['quote']['psbt'],replace_marker=dict(vout='0',script=marker['script']))
    with pytest.raises(ValueError,match='SMP1_QUOTE_CHANGED'):
        d.import_signed(op,accepted,signed_psbt(psbt_base64(changed['psbt'])))
    assert d.journal.get('deposit:'+op)['status']=='EXPORTED'
    assert not any(c[0]=='finalizepsbt' for c in d.bitcoin.calls)


def test_partial_signing_resumes_finalizer_without_another_wallet_signature(client,monkeypatch):
    d,quote,_,_=client;q=quote();op=q['quote']['operation_id'];accepted=q['quote_hash']
    original=d.bitcoin.call
    def pending(method,*args):
        if method=='finalizepsbt':return dict(complete=False,psbt=args[0])
        return original(method,*args)
    d.bitcoin.call=pending
    assert d.sign(op,accepted)['status']=='EXTERNAL_SIGNER_OR_ADDRESS_FLOW_REQUIRED'
    d.bitcoin.call=original
    assert d.sign(op,accepted)['status']=='SIGNED'
    assert sum(c[0]=='walletprocesspsbt' for c in d.bitcoin.calls)==1


@pytest.mark.parametrize('coinbase,confirmations,accepted',[(False,1,True),(True,100,False),(True,101,True)])
def test_sender_input_coinbase_maturity_is_distinct_from_ordinary_deposit_maturity(client,coinbase,confirmations,accepted):
    d,quote,_,_=client;original=d.bitcoin.call
    def call(method,*args):
        result=original(method,*args)
        if method=='gettxout':result.update(coinbase=coinbase,confirmations=confirmations)
        return result
    d.bitcoin.call=call
    if accepted:assert quote()['quote']['amount_sats']=='100000'
    else:
        with pytest.raises(ValueError,match='SMP1_INPUT_UNAVAILABLE'):quote()


def test_network_observation_failure_does_not_erase_final_mp_allocation(client):
    d,quote,values,_=client;q=quote()['quote']
    values['minted']=keccak(encode(['address[]','uint256[]'],[[q['recipient']],[int(q['mp_units'])]]))
    def wrong():raise ValueError('BITCOIN_NETWORK_MISMATCH')
    d.bitcoin.check_network=wrong
    result=d.status(q['operation_id'])
    assert result['mint_status']=='MP_MINT_FINAL' and result['bitcoin_status']=='UNKNOWN'
