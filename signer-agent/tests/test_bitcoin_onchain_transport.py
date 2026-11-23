"""Adversarial received packets, never trusted merely because a wallet member posted them."""
import json
from types import SimpleNamespace
from web3.datastructures import AttributeDict
import pytest
from web3 import Web3
from signer_agent import crypto
from signer_agent.bitcoin_onchain import collect, packet_payload


@pytest.fixture
def transport():
    addresses = [Web3.to_checksum_address('0x' + ('%040x' % i)) for i in range(1, 6)]
    manifest = SimpleNamespace(chain_id=31337, threshold=2, participants=addresses[:3])
    wallet = '5120' + '11' * 32
    plan = dict(version='2', network='regtest', batch_registry=addresses[3],
                batch_id='aa'*32, signing_board=addresses[4], wallet_result='bb'*32,
                wallet_script=wallet, deadline='2000000000', fee='1000', max_fee='2000',
                inputs=[dict(outpoint='cc'*32+':0', amount='10000', script=wallet)],
                outputs=[dict(amount='9000', script='0014'+'dd'*20)],
                withdrawal_refs=['ee'*32], signers=['1','2'])
    packets = {}
    block = AttributeDict(dict(number=100, hash=b'a'*32))
    registry = SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_block=lambda _: block)))
    def packet(batch, index, kind, address):
        return SimpleNamespace(call=lambda **_: packets.get((index, kind, address), b''))
    board = SimpleNamespace(functions=SimpleNamespace(packet=packet))
    commitments = [crypto.call('nonce', share='00'*31+'01')['commitments'] for _ in range(2)]
    for i in range(2):
        packets[(0, 0, addresses[i])] = packet_payload(plan, 0, 'commitment', str(i+1), commitments[i], manifest)
    return SimpleNamespace(m=manifest, plan=plan, registry=registry, board=board,
                           packets=packets, commitments=commitments, addresses=addresses)


def test_missing_share_does_not_get_substituted_by_unselected_member(transport):
    t = transport
    t.packets[(0,1,t.addresses[2])] = b'not even a valid packet'
    c,s,missing_c,missing_s = collect(t.m,t.registry,t.board,t.plan)
    assert not missing_c and len(missing_s)==2 and s == [{}]
    assert c == [dict(zip(['1','2'], t.commitments))]


@pytest.mark.parametrize('mutation', ['sender','input','batch','plan','kind','extra','duplicate-key','nonce'])
def test_replayed_ambiguous_or_secret_packets_fail_closed(transport, mutation):
    t=transport
    key=(0,0,t.addresses[0]); value=json.loads(t.packets[key])
    if mutation=='sender': value['signer']='2'
    elif mutation=='input': value['input']='1'
    elif mutation=='batch': value['batch']='ff'*32
    elif mutation=='plan': value['plan_hash']='ff'*32
    elif mutation=='kind': value['kind']='share'
    elif mutation=='extra': value['nonces']='00'*140
    elif mutation=='nonce': value['value']=crypto.call('nonce',share='00'*31+'01')['nonces']
    raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
    if mutation=='duplicate-key': raw=raw.replace(b'"signer":"1"',b'"signer":"1","signer":"1"')
    t.packets[key]=raw
    with pytest.raises((ValueError,RuntimeError)):
        collect(t.m,t.registry,t.board,t.plan)


def test_identical_nonce_commitment_from_two_members_rejected(transport):
    t=transport
    t.packets[(0,0,t.addresses[1])] = packet_payload(t.plan,0,'commitment','2',t.commitments[0],t.m)
    with pytest.raises(ValueError,match='BTC_DUPLICATE_COMMITMENT'):
        collect(t.m,t.registry,t.board,t.plan)


def test_reorg_during_packet_read_is_rejected(transport):
    t=transport
    def get_block(number):
        return AttributeDict(dict(number=100, hash=(b'a' if number=='latest' else b'b')*32))
    t.registry.w3.eth.get_block=get_block
    with pytest.raises(ValueError,match='BTC_BOARD_SNAPSHOT_REORG'):
        collect(t.m,t.registry,t.board,t.plan)


@pytest.mark.parametrize('action', ['btc-commit','btc-sign','btc-broadcast','btc-settle'])
def test_signing_commands_reject_misleading_fee_override(action):
    # A flag that looks like a local fee ceiling must not be silently ignored.
    from signer_agent.participant_cli import main
    with pytest.raises(SystemExit) as error:
        main([action, '--max-fee', '1'])
    assert error.value.code == 2


@pytest.mark.parametrize('options', [[], ['--batch', 'aa' * 32],
    ['--signing-board', '0x' + '11' * 20, '--batch', 'aa' * 32, '--package', 'unused.json']])
def test_expired_signing_requires_explicit_onchain_ids(options, capsys):
    from signer_agent.participant_cli import main
    assert main(['btc-sign', '--resume-expired', *options]) == 2
    assert json.loads(capsys.readouterr().out)['code'] == 'BTC_EXPIRED_SIGNING_REQUIRES_IDS'
