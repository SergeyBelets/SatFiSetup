"""Real journals/curve identities; canonical chain, inventory and retention are explicit fixtures."""
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from signer_agent import bitcoin_continuity as continuity, continuity_handover as h, crypto
from signer_agent.operations import Journal, canonical
from signer_agent.secure_state import SecureState
from test_bitcoin_continuity import continuity as continuity_fixture
from test_address_transport import delivery


@pytest.fixture
def handover(continuity_fixture, monkeypatch):
    e = continuity_fixture; old = e.sample(); new = deepcopy(old)
    key = bytes.fromhex(crypto.call('public_share', share=(17).to_bytes(32,'big').hex())['public'])[1:]
    new['identity'] = dict(new['identity'], base_x=key.hex(), registry='0x'+'97'*20,
                           graph_id='ab'*32, wallet_id='cd'*32, wallet_result='ef'*32)
    new['family'] = continuity.family(bytes.fromhex(new['identity']['bitcoin_genesis']), key)
    ids = ['11'*32, '22'*32]; e.old = old; e.new = new; e.retained = False
    target_dir = e.tmp_path/'target'; target_dir.mkdir(mode=0o700)
    target = SimpleNamespace(c=dict(e.source.c, identity_file=str(target_dir/'identity.json')), w3=e.w3,
        context=lambda:(e.block(20), None, dict(e.ctx, target_fixture=True), None, None))
    e.clients = [e.source, target]
    def sample(w3, ctx, number): return deepcopy(e.new if ctx.get('target_fixture') else e.old)
    monkeypatch.setattr(continuity, 'sample', sample)
    def inventory(client, **kw):
        s = deepcopy(e.old if client is e.source else e.new)
        continuity.Catalog(identity_file=client.c['identity_file']).observe(client.w3, s)
        return dict(source=s, registered_bindings=[], fees=dict(status=e.fees, personal=None),
                    history_complete=False, activation_allowed=False, named_requests=kw.get('requests', []))
    monkeypatch.setattr(h, 'inventory', inventory); e.fees = 'OBSERVED'
    entries = [dict(wallet_context=ids[0], mode='SERVICING'), dict(wallet_context=ids[1], mode='ISSUING')]
    def snapshot(entry, client):
        i=(e.old if client is e.source else e.new)['identity']
        return dict(identity=dict(registry=i['registry'],chain_id=i['chain_id'],bitcoin_genesis=i['bitcoin_genesis'],
                    base_group='02'+i['base_x'],wallet_result=i['wallet_result'],key_epoch=i['key_epoch'],
                    graph_id=i['graph_id'],wallet_context=entry['wallet_context']),block=e.block(20),latest=e.block(20))
    fleet = SimpleNamespace(configuration=str(e.tmp_path/'fleet.json'), c=dict(fleet_id='33'*32, epochs=entries),
        lane=Journal(e.tmp_path/'fleet.sqlite'), exclusive=nullcontext,
        entry=lambda context:next(x for x in entries if x['wallet_context']==context),
        client=lambda entry:e.clients[ids.index(entry['wallet_context'])],
        snapshot=snapshot)
    monkeypatch.setattr('signer_agent.epoch_retention.review', lambda *args:dict(status='RETENTION_CHECKED' if e.retained else 'BLOCKED'))
    monkeypatch.setattr('signer_agent.epoch_service.load_config', lambda path: fleet.c)
    monkeypatch.setattr('signer_agent.epoch_service.Fleet', lambda *args, **kw: fleet)
    with fleet.lane.db() as db:
        db.execute('CREATE TABLE epoch_retention_current(fleet TEXT PRIMARY KEY,body TEXT)')
        db.execute('INSERT INTO epoch_retention_current VALUES(?,?)',
                   (fleet.c['fleet_id'], canonical(dict(request={}, packets=[]))))
    e.fleet = fleet; e.handover = h.Handover(fleet); e.ids = ids
    def drain():
        for states in e.old['states'].values(): states[0].update(state=1, generation=2, sequence=1, issuance_closed=True)
    e.drain = drain
    e.prepare = lambda:e.handover.prepare(*ids)
    e.review = lambda identifier:e.handover.review(identifier)
    return e


def test_prepare_is_restrictive_and_review_requires_draining_and_fresh_retention(handover):
    e=handover; first=e.prepare(); identifier=first['plan_id']
    assert not first['activation_allowed'] and not first['transfers_assets']
    blocked=e.review(identifier)
    assert set(blocked['reasons'])=={'CONTINUITY_HANDOVER_SOURCE_NOT_DRAINING','EPOCH_RETENTION_REQUIRED'}
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_REVIEW_REQUIRED'):
        continuity.Catalog(identity_file=e.clients[1].c['identity_file']).authorize(e.w3,e.new,'ISSUE')
    e.drain();e.retained=True; checked=e.review(identifier)
    assert checked['status']=='RETAINED_SERVICE_CHECKED'
    assert not checked['history_complete'] and not checked['global_key_independence_proved']
    assert not checked['activation_allowed'] and not checked['production_ready']
    continuity.Catalog(identity_file=e.clients[1].c['identity_file']).authorize(e.w3,e.new,'ISSUE')
    # Expiry/availability is a new current hold, never bypassed by yesterday's green report.
    e.retained=False
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_REVIEW_REQUIRED'):
        continuity.Catalog(identity_file=e.clients[1].c['identity_file']).authorize(e.w3,e.new,'ISSUE')


def test_exact_prepare_retry_keeps_original_inventory_and_plan(handover):
    e=handover;identifier=e.prepare()['plan_id']
    first=e.fleet.lane.get('continuity-handover-initial:'+identifier)['request']
    e.drain()
    assert e.prepare()['plan_id']==identifier
    assert e.fleet.lane.get('continuity-handover-initial:'+identifier)['request']==first


def test_same_parent_or_known_derived_child_is_not_an_independent_graph(handover):
    e=handover;e.new['identity']['base_x']=e.old['identity']['base_x'];e.new['family']=e.old['family']
    with pytest.raises(ValueError,match='DISTINCT_KEYS_REQUIRED'):e.prepare()
    e.new['identity']['base_x']=bytes.fromhex(crypto.call('public_share',share=(19).to_bytes(32,'big').hex())['public'])[1:].hex()
    key=bytes.fromhex(e.new['identity']['base_x']);e.new['family']=continuity.family(bytes.fromhex(e.old['identity']['bitcoin_genesis']),key)
    catalog=continuity.Catalog(identity_file=e.source.c['identity_file'])
    catalog.alias(e.w3,e.old,key,'canonical-derived-key-fixture')
    with pytest.raises(ValueError,match='CONTINUITY_DERIVED_KEY_REUSE'):e.prepare()


def test_target_mode_and_original_binding_cannot_be_rewritten(handover):
    e=handover;identifier=e.prepare()['plan_id'];e.drain();e.retained=True
    e.new['identity']['deposits']='0x'+'54'*20
    with pytest.raises(ValueError,match='CONTINUITY_GRAPH_CHANGE'):e.review(identifier)


def test_missing_partner_journal_requires_recovery_not_silent_reenrollment(handover):
    e=handover;identifier=e.prepare()['plan_id'];e.drain();e.retained=True
    catalog=continuity.Catalog(identity_file=e.source.c['identity_file'])
    with catalog.databases() as dbs:
        for db in dbs:db.execute('DELETE FROM btc_handover_bindings')
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_RECOVERY_REQUIRED'):e.review(identifier)
    # Explicit prepare repairs only the exact original restrictive enrollment.
    assert e.prepare()['plan_id']==identifier
    assert e.review(identifier)['status']=='RETAINED_SERVICE_CHECKED'


def test_incomplete_fee_observation_does_not_turn_into_empty_obligations(handover):
    e=handover;identifier=e.prepare()['plan_id'];e.drain();e.retained=True;e.fees='OBSERVATION_UNAVAILABLE'
    assert e.review(identifier)['reasons']==['CONTINUITY_HANDOVER_INVENTORY_UNAVAILABLE']


@pytest.mark.parametrize('scope,state',[('custody',3),('derivation',2)])
def test_source_incident_cannot_be_cleared_by_new_target_identity(handover,scope,state):
    e=handover;identifier=e.prepare()['plan_id'];e.drain();e.retained=True
    index=0 if scope=='custody' else 1
    for states in e.old['states'].values():states[index].update(state=state,generation=3,sequence=2)
    blocked=e.review(identifier)
    assert blocked['status']=='BLOCKED'
    assert ('CONTINUITY_HANDOVER_SOURCE_NOT_DRAINING' if index==0 else 'CONTINUITY_DERIVATION_RESTRICTED') in blocked['reasons']


def test_signer_enrollment_survives_older_encrypted_state_and_remains_restrictive(handover):
    e=handover;identifier=e.prepare()['plan_id'];state=SecureState(e.tmp_path/'participant'/'state','test password')
    with state._journal():pass
    old=state.path.read_bytes();e.handover.enroll_signer(identifier,state);state.path.write_bytes(old)
    state.put('untouched-private-journal-fixture',dict(status='consumed'))
    catalog=continuity.Catalog(state=state)
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_REVIEW_REQUIRED'):catalog.authorize(e.w3,e.new,'ISSUE')
    e.drain();e.retained=True
    catalog.authorize(e.w3,e.new,'ISSUE')
    assert state.get('untouched-private-journal-fixture')['status']=='consumed'
    # Existing money recovery/reading does not receive a new issuance prerequisite.
    e.retained=False;catalog.authorize(e.w3,e.new,'READ');catalog.authorize(e.w3,e.new,'ADDRESS_MINT')


def test_enrollment_cannot_be_replaced_by_another_plan_or_exported_json(handover):
    e=handover;identifier=e.prepare()['plan_id'];plan=e.handover.get(identifier)
    marker=e.handover.marker(plan);marker['plan_id']='ff'*32
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_CONFLICT'):
        h.bind(continuity.Catalog(identity_file=e.clients[1].c['identity_file']),marker)
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_NOT_FOUND'):e.handover.get('ff'*32)


def test_full_canonical_wallet_identity_is_bound_to_the_fleet_context(handover):
    e=handover;original=e.fleet.snapshot
    def changed(entry,client):
        result=original(entry,client);result['identity']['wallet_result']='ff'*32;return result
    e.fleet.snapshot=changed
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_CONFLICT'):e.prepare()


def test_generation_change_during_multi_graph_review_blocks_admission(handover,monkeypatch):
    e=handover;identifier=e.prepare()['plan_id'];e.drain();e.retained=True
    def changed(*args):
        for states in e.old['states'].values():states[0].update(generation=3,sequence=2)
        return dict(status='RETENTION_CHECKED')
    monkeypatch.setattr('signer_agent.epoch_retention.review',changed)
    assert 'CONTINUITY_HANDOVER_CHANGED' in e.review(identifier)['reasons']


def test_retired_target_cannot_reopen_by_relabeling_the_handover(handover):
    e=handover;e.fleet.c['epochs'][1]['mode']='SERVICING'
    with pytest.raises(ValueError,match='CONTINUITY_HANDOVER_MODES'):e.prepare()


def test_contour_adapter_requires_fresh_review_and_retains_first_exact_report(handover,monkeypatch):
    from ops.zkspv.contour_driver import main
    e=handover;identifier=e.prepare()['plan_id'];e.drain();e.retained=True
    root=e.tmp_path/'controller';root.mkdir(mode=0o700)
    def invoke(number,prior):
        invocation=root/str(number);invocation.mkdir(mode=0o700)
        binding=dict(version='1',run_id='11'*32,plan_sha256='22'*32,stage='continuity',capability='continuity',attempt_id='33'*32,
                     challenge=str(number)*64,action='probe',predecessors=[])
        context=invocation/'context.json';response=invocation/'response.json'
        context.write_text(json.dumps(dict(binding,directory=str(root),response_file=str(response),prior=prior)))
        monkeypatch.setenv('SATOHSI_CONTOUR_CONTEXT',str(context))
        main(['--fleet-config',e.fleet.configuration,'--plan-id',identifier])
        result=json.loads(response.read_text());assert result['binding']==binding;return result
    first=invoke(1,{})
    assert first['status']=='SATISFIED' and first['artifacts']
    assert invoke(2,first)['artifacts']==first['artifacts']
    e.retained=False;blocked=invoke(3,first)
    assert blocked['status']=='WAITING' and not blocked['artifacts']
