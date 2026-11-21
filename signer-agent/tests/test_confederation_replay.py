"""DAO participant retries preserve authority, typed arguments and new-operation checks."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_utils.abi import function_abi_to_4byte_selector
from hexbytes import HexBytes
from web3 import Web3

from signer_agent import bootstrap_confederation_dao as dao
from signer_agent import operation_status as status


@pytest.fixture
def membership(monkeypatch):
    addresses = [Web3.to_checksum_address(i.to_bytes(20, 'big')) for i in range(1, 8)]
    validator, staking, auth, token, registry, operator, member = addresses
    epoch, context, result = b'd' * 32, b'c' * 32, b'r' * 32
    invite = dict(ulr=dict(version='3', mode='create', candidate_threshold_wei='42'),
        sessions=dict(conf_ceremony=epoch.hex()), participants=[operator, member], operator=operator,
        dao_token_recipient=operator)
    bundle = dict(onboarding=dict(invitation=invite), role='conf_ceremony')
    manifest = SimpleNamespace(chain_id=1, purpose='EVM_AUTH', session_id=epoch, context=context,
                               registry=registry, participants=[member], threshold=1)
    calls = []
    env = SimpleNamespace(address=member, target=validator, auth=auth, consume=False,
                          is_member=False, eligible=True, calls=calls)
    values = dict(candidateStaking=staking, custodianValidator=validator, frostMultisig=auth,
        registry=registry, coordinator='coordinator', dkgId=epoch, getParticipants=[member],
        getCompletedResult=(context, result, 0, 0, b''), resultHash=result, stakeToken=token,
        totalSupply=dao.SUPPLY, CUSTODIAN_THRESHOLD=42, name='Gov', symbol='G')
    class Fields:
        def __getattr__(self, name):
            def invoke(*_args):
                def read(*, block_identifier):
                    assert block_identifier == 8
                    calls.append(name)
                    return dict(values, isCustodian=env.is_member, isCustodianCandidate=env.eligible)[name]
                return SimpleNamespace(call=read)
            return invoke
    eth = SimpleNamespace(chain_id=1, get_block=lambda _: SimpleNamespace(number=8, hash=b'b'),
                          contract=lambda *, address, abi: SimpleNamespace(address=address, functions=Fields()))
    w3 = SimpleNamespace(eth=eth)
    monkeypatch.setattr(dao, 'validate', lambda *args: None)
    monkeypatch.setattr('signer_agent.dkg_protocol.Manifest.from_dict', lambda b: manifest)
    monkeypatch.setattr('signer_agent.bootstrap.artifact', lambda k: {'abi': []})
    monkeypatch.setattr('signer_agent.bitcoin_trust.checked_runtime', lambda *_: None)
    monkeypatch.setattr('signer_agent.contract_provenance.prove_creation',
                        lambda *_: dict(method='direct', sender=operator))
    monkeypatch.setattr('signer_agent.bootstrap_tokens.resolve', lambda _: {'governance':dict(name='Gov',symbol='G')})
    def executed(_w3, target, kind, _intent, at, *, session, dao_authorization):
        assert (_w3, target.address, kind, at, session, dao_authorization) == (w3, validator, 'CustodianValidator', 8, 7, auth)
        calls.append('executed');return env.consume
    monkeypatch.setattr(status, 'executed_operation', executed)
    def review(method, **extra):
        intent = dict(artifact='CustodianValidator', target=validator, hash_method=method,
                      hash_args=[member], coordinator='coordinator', role='conf_ceremony', **extra)
        return dao.review_session(w3,intent,bundle,8,7)
    env.review = review
    return env


@pytest.mark.parametrize('helper,after,error', [
    ('getAddCustodianOperationHash',True,'CONFEDERATION_DAO_CANDIDATE'),
    ('getRemoveCustodianOperationHash',False,'CONFEDERATION_DAO_CUSTODIAN')])
def test_completed_membership_change_only_skips_mutable_admission(membership,helper,after,error):
    e=membership;e.consume=True;e.is_member=after;e.eligible=False
    assert e.review(helper)==((e.auth,e.target),True)
    assert 'executed' in e.calls and 'isCustodian' not in e.calls
    e.consume=False
    with pytest.raises(ValueError,match=error):e.review(helper)
    with pytest.raises(ValueError,match='POLICY_MISMATCH'):e.review(helper,session='8')


@pytest.mark.parametrize('method,other', [('addCustodian','removeCustodian'),('removeCustodian','addCustodian')])
def test_consumed_membership_receipt_binds_original_method_and_member(method,other):
    w3=Web3()
    abi=json.loads((Path(__file__).resolve().parents[1]/'contracts'/'CustodianValidator.json').read_text())
    addr=Web3.to_checksum_address('0x'+'22'*20)
    op=b'h'*32
    action=next(entry for entry in abi if entry.get('name')==method)
    receipt=Web3.keccak(w3.codec.encode(['bytes4','address'],[function_abi_to_4byte_selector(action),addr]))
    target=SimpleNamespace(abi=abi,functions=SimpleNamespace(operationReceipts=lambda k:
                 SimpleNamespace(call=lambda *,block_identifier:receipt if k==op and block_identifier==8 else b'')))
    def verify(name,member):
        helper='get'+name[0].upper()+name[1:]+'OperationHash'
        status._dao_receipt(w3,target,None,dict(hash_method=helper,hash_args=[member]),op,8,None)
    verify(method,addr)
    with pytest.raises(ValueError,match='ULR_CUSTODIAN_OPERATION_RECEIPT'):verify(other,addr)
    with pytest.raises(ValueError,match='ULR_CUSTODIAN_OPERATION_RECEIPT'):verify(method,'0x'+'44'*20)


def test_nft_replay_uses_reviewed_dao_authority():
    # No independently authenticated DAO graph, hence no terminal status.
    assert not status.executed_operation(None,None,'ConfederationNFTPass',{},8,session=7)
    functions=SimpleNamespace(income=lambda: SimpleNamespace(call=lambda **_: 'sale-income'),
                              authorization=lambda: SimpleNamespace(call=lambda **_:'unrelated'))
    target=SimpleNamespace(functions=functions)
    from unittest.mock import patch
    with patch.object(status,'artifact',return_value={'abi':[]}):
        w3=SimpleNamespace(eth=SimpleNamespace(contract=lambda **_:target))
        with pytest.raises(ValueError,match='CONFEDERATION_DAO_BINDING'):
            status.executed_operation(w3,target,'ConfederationNFTPass',{},8,session=7,dao_authorization='reviewed')


def test_tariff_replay_binds_canonical_direct_calldata(monkeypatch):
    op=b'z'*32
    target_addr=Web3.to_checksum_address('0x'+'11'*20)
    auth_addr=Web3.to_checksum_address('0x'+'22'*20)
    digest=SimpleNamespace(call=lambda **_:b'current tariff nonce changed')
    # This is a fixture ABI for typed helper arguments. The runtime obtains
    # the real ABI and canonical constructor from the original deployment.
    spec=dict(type='tuple',components=[dict(type='uint64'),dict(type='uint16'),dict(type='uint256')])
    helper='getApproveTariffOperationHash'
    target=SimpleNamespace(address=target_addr, abi=[dict(type='function',name=helper,inputs=[spec])],
        functions=SimpleNamespace(getApproveTariffOperationHash=lambda *args:digest),
        encode_abi=lambda method,args: (Web3().codec.encode(['(uint64,uint16,uint256)','bytes32'],args)).hex())
    block_hash,tx_hash=HexBytes(b'b'*32),HexBytes(b't'*32)
    block=SimpleNamespace(hash=block_hash,transactions=[tx_hash])
    actual=target.encode_abi('approveTariff', [[100,1,5],op])
    tx=dict(to=target_addr,input=actual,hash=tx_hash,blockHash=block_hash,blockNumber=5,transactionIndex=0)
    receipt=dict(status=1,transactionHash=tx_hash,blockHash=block_hash,blockNumber=5,transactionIndex=0)
    w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda n:block,get_transaction=lambda _:tx,
                                           get_transaction_receipt=lambda _:receipt))
    def logs(_w3,start,end,topics,address):
        assert (start,end,address)==(2,8,auth_addr)
        event=dict(address=auth_addr,transactionHash=tx_hash,blockHash=block_hash,blockNumber=5,
                   transactionIndex=0,logIndex=0,topics=[HexBytes(t) for t in topics])
        receipt['logs']=[event]
        yield [event]
    monkeypatch.setattr('signer_agent.evm_log_paging.log_pages',logs)
    def check(duration):
        status._dao_receipt(w3,target,SimpleNamespace(address=auth_addr),
                dict(hash_method=helper,hash_args=[[str(duration),'1','5']]),op,8,2)
    check(100)
    with pytest.raises(ValueError,match='OPERATION_PARAMETERS_MISMATCH'):check(101)
    receipt['status']=0
    with pytest.raises(ValueError,match='POLICY_MISMATCH'):check(100)
