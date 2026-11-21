"""Custodian EOA approval honors the enrolled depth before private signing."""
import json
from types import SimpleNamespace

import pytest
from eth_account import Account
from web3 import Web3

from signer_agent import custodian_cli
from signer_agent.bootstrap import artifact


@pytest.fixture
def approval(monkeypatch):
    addresses = [Web3.to_checksum_address('0x' + format(i, '040x')) for i in range(1, 7)]
    target, authorization, validator, coordinator, registry, treasury = addresses
    account = Account.from_key((1).to_bytes(32, 'big'))
    op, dkg_id = b'o' * 32, b'd' * 32
    fixture = SimpleNamespace(chain=11155111, depth=12, reads=[], private_reads=[], sent=[],
        changed=False, reorg_at=None, finalized=True, codes=[])
    anchor = SimpleNamespace(number=42, hash=b'a' * 32)
    intent = dict(operation_hash=op.hex(), chain_id=str(fixture.chain), artifact='FederationFeeManager',
        target=target, coordinator=coordinator, hash_method='getSetTreasuryOperationHash', hash_args=[treasury])
    fixture.intent = intent
    profile = dict(evm_confirmations=str(fixture.depth), mode='backed', bitcoin_network='testnet4',
        conf_ceremony=dict(id='0x' + dkg_id.hex()))
    fixture.profile = profile

    class Functions:
        def __getattr__(self, method):
            def function(*args):
                def call(*, block_identifier):
                    fixture.reads.append((method, block_identifier))
                    values = dict(intents=json.dumps(intent).encode(), getSetTreasuryOperationHash=op,
                        getEnrollTreasuryOperationHash=op,
                        custodianValidator=validator, frostMultisig=authorization, coordinator=coordinator,
                        dkgRegistry=registry, registry=registry, sessionDKGId=dkg_id,
                        getAuthorizationResult=(0, op, fixture.finalized or block_identifier == 'latest', 0, target, 3, 3),
                        getCustodians=[account.address], custodianSigned=False, executedOperations=False)
                    if method == fixture.reorg_at:
                        fixture.changed = True
                    return values[method]
                return SimpleNamespace(call=call)
            return function

    def contract(address, abi=None):
        return SimpleNamespace(address=address, abi=abi or [], functions=Functions())

    def get_code(address, *, block_identifier):
        fixture.codes.append(block_identifier)
        return b'confirmed-validator-runtime'

    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=fixture.chain,
        contract=contract, get_code=get_code,
        get_block=lambda number: SimpleNamespace(number=number, hash=b'b' * 32 if fixture.changed else anchor.hash)))
    fixture.w3 = w3
    class W3:
        HTTPProvider = staticmethod(lambda url: url)
        keccak = staticmethod(Web3.keccak)
        def __new__(cls, provider):
            return w3
    monkeypatch.setattr(custodian_cli, 'Web3', W3)
    monkeypatch.setattr(custodian_cli, 'board_at', lambda *args: contract('board'))
    monkeypatch.setattr(custodian_cli, 'assembled_profile', lambda *args: profile)
    def read_block(actual, depth):
        assert actual is w3 and depth == fixture.depth
        return anchor
    monkeypatch.setattr('signer_agent.evm_network.read_block', read_block)
    monkeypatch.setattr(custodian_cli, 'read_private', lambda path:
        fixture.private_reads.append(path) or json.dumps(dict(private_key='01'.rjust(64, '0'))))
    class Sender:
        def __init__(self, config, state):
            fixture.sent.append(config)
        def apply_confirmed(self, identity):
            return dict(status='VERIFIED')
    monkeypatch.setattr(custodian_cli, 'Bootstrap', Sender)
    fixture.args = SimpleNamespace(rpc='local-fixture', board='board', session=1,
        operation_hash=op.hex(), identity_file='private-fixture', state='journal-fixture')
    return fixture


@pytest.mark.parametrize('chain,depth', [(1, 12), (11155111, 12), (31337, 3)])
def test_every_custodian_policy_read_uses_one_confirmed_snapshot(approval, chain, depth):
    f = approval
    f.w3.eth.chain_id = chain
    f.depth = depth
    f.profile['evm_confirmations'] = str(depth)
    f.intent['chain_id'] = str(chain)
    assert custodian_cli.approve(f.args)['status'] == 'CUSTODIAN_APPROVED'
    assert f.reads and all(number == 42 for _, number in f.reads)
    assert f.codes == [42]
    assert len(f.private_reads) == len(f.sent) == 1
    assert f.sent[0]['evm_confirmations'] == str(depth)


def test_unconfirmed_frost_result_never_unlocks_custodian_private_key(approval):
    approval.finalized = False  # The fake would return True for unsafe latest reads.
    with pytest.raises(ValueError, match='FROST_APPROVAL_REQUIRED'):
        custodian_cli.approve(approval.args)
    assert not approval.private_reads and not approval.sent


def test_ulr_enrollment_uses_reviewed_confederation_and_same_confirmed_quorum(approval,monkeypatch):
    f=approval;calls=[]
    addresses=[Web3.to_checksum_address('0x'+format(i,'040x')) for i in range(1,7)]
    f.intent.update(artifact='ULRCustodianSnapshots',hash_method='getEnrollTreasuryOperationHash',
        hash_args=[addresses[4],'conf-A',addresses[5]])
    def context(w3,intent,board,at,pin):
        calls.append((w3,intent,board,at,pin))
        return addresses[1],addresses[2],dict(complete=False)
    monkeypatch.setattr(custodian_cli,'ulr_context',context)
    f.args.ulr_config='reviewed-ulr-pin.json'
    assert custodian_cli.approve(f.args)['status']=='CUSTODIAN_APPROVED'
    assert calls==[(f.w3,f.intent,'board',42,'reviewed-ulr-pin.json')]
    assert len(f.private_reads)==len(f.sent)==1
    f.finalized=False;f.private_reads.clear();f.sent.clear()
    with pytest.raises(ValueError,match='FROST_APPROVAL_REQUIRED'):custodian_cli.approve(f.args)
    assert not f.private_reads and not f.sent


def test_system_snapshot_cannot_request_an_unrelated_confederation_custodian_vote():
    with pytest.raises(ValueError,match='CUSTODIAN_TARGET_UNSUPPORTED'):
        custodian_cli.ulr_context(None,dict(artifact='ULRCustodianSnapshots',hash_method='getPublishDayOperationHash'),
            'board',42)


@pytest.mark.parametrize('at,private_reads', [('getAuthorizationResult', 0), ('custodianSigned', 1)])
def test_reorg_before_private_access_or_before_send_stops_custodian_approval(approval, at, private_reads):
    approval.reorg_at = at
    with pytest.raises(ValueError, match='EVM_SIGNING_SNAPSHOT_REORG'):
        custodian_cli.approve(approval.args)
    assert len(approval.private_reads) == private_reads
    assert not approval.sent
