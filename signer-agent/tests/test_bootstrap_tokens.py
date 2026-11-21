"""Names are agreed before enrollment, propagated to constructors and checked before signing."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
import pytest
from signer_agent import bootstrap_tokens as tokens
from signer_agent.bootstrap_plan import build
from signer_agent.onboarding import assemble, validate_invite, verify_profile, main
from test_bootstrap_system import full_invite, NETWORKS
from test_onboarding_mainnet import signed_enrollments
from test_onboarding_policy_v3 import invite_args


def names():
    return {role: dict(name='Проект ' + role, symbol='P' + str(i)) for i, role in enumerate(tokens.ROLES)}


@pytest.mark.parametrize('chain,network,backend', NETWORKS)
@pytest.mark.parametrize('mode', ['create', 'reuse'])
def test_names_reach_every_constructor_and_read_check_in_all_profiles(chain, network, backend, mode):
    invite=full_invite(chain,network,backend,mode);invite['token_metadata']=names()
    if mode=='reuse':invite['ulr']['token']=names()['governance']
    validate_invite(invite)
    profile=assemble(invite,signed_enrollments(invite));verify_profile(profile)
    steps={s['id']:s for s in build(profile)['steps']}
    assert steps['deploy_federation']['args'][1:]==list(names()['federation_share'].values())
    assert steps['deploy_confederation']['args'][1:]==list(names()['confederation_share'].values())
    assert steps['mining']['args'][-2:]==list(names()['mining'].values())
    if mode=='create':assert steps['dao_token']['args'][:2]==list(names()['governance'].values())
    else:
        assert 'dao_token' not in steps
        assert steps['shared_ulr_bindings']['checks'][0]['equals']==names()['governance']['name']
    for step,role in [('deploy_federation','federation_share'),('deploy_confederation','confederation_share'),('mining','mining')]:
        checks=steps[step]['checks']
        for key,value in names()[role].items():
            assert any(c['method']==key and c['equals']==value for c in checks)
    assert steps['fee_router']['args'][-2:]==['0','0']


@pytest.mark.parametrize('value', [None,{}, {'governance':dict(name='X',symbol='X')}, dict(extra={})])
def test_partial_or_unknown_roles_are_rejected(value):
    with pytest.raises(ValueError,match='TOKEN_METADATA_SCHEMA'):tokens.validate(value)


@pytest.mark.parametrize('field,value', [('name',''),('name',' X'),('name','X '),('name','X\nY'),
    ('name','X\u202eY'),('name','a'*129),('name','я'*65),('symbol',''),('symbol','x'*33),
    ('symbol','USD coin'),('symbol','РСТ'),('symbol',9)])
def test_invalid_metadata_fails_before_enrollment(field,value):
    p=full_invite();p['token_metadata']=names();p['token_metadata']['governance'][field]=value
    with pytest.raises(ValueError,match='TOKEN_METADATA_'):validate_invite(p)


def test_metadata_length_and_unicode_boundaries():
    assert tokens.token(dict(name='я'*64,symbol='X'*32))['name']=='я'*64
    assert tokens.token(dict(name='Project',symbol='X.v1-2_3'))['symbol']=='X.v1-2_3'


def test_signed_metadata_cannot_be_changed_removed_or_injected():
    invite=full_invite();invite['token_metadata']=names();entries=signed_enrollments(invite)
    profile=assemble(invite,entries)
    assert profile['token_metadata']==names()
    for field in ('name','symbol'):
        changed=deepcopy(invite);changed['token_metadata']['mining'][field]='FAKE'
        with pytest.raises(ValueError,match='ENROLLMENT_CONTEXT'):assemble(changed,entries)
    for action in ('remove','change'):
        changed=deepcopy(profile)
        if action=='remove':del changed['token_metadata']
        else:
            changed['token_metadata']=deepcopy(changed['token_metadata'])
            changed['token_metadata']['governance']['symbol']='FAKE'
        with pytest.raises(ValueError,match='ENROLLED_PROFILE_MISMATCH'):verify_profile(changed)
    old=full_invite();profile=assemble(old,signed_enrollments(old));profile['token_metadata']=names()
    with pytest.raises(ValueError,match='ENROLLED_PROFILE_MISMATCH'):verify_profile(profile)


def test_shared_token_branding_is_retained_without_a_second_file_and_cannot_conflict():
    p=full_invite(mode='reuse');p['ulr']['token']=names()['governance']
    assert tokens.resolve(p)['governance']==names()['governance']
    assert tokens.resolve(p)['mining']['symbol']=='IMP'
    p['token_metadata']=names();p['token_metadata']['governance']['symbol']='FAKE'
    with pytest.raises(ValueError,match='TOKEN_METADATA_SHARED_CONFLICT'):validate_invite(p)
    del p['token_metadata'];p['ulr']['token']=None
    with pytest.raises(ValueError,match='TOKEN_METADATA_SCHEMA'):validate_invite(p)


def test_old_defaults_and_legacy_overrides_remain_compatible_but_enrolled_override_is_forbidden():
    assert tokens.resolve(dict(ulr={}))['governance']==dict(name='ULR',symbol='ULR')
    assert tokens.resolve({})['governance']==dict(name='Custodian Governance',symbol='CGOV')
    assert tokens.resolve(dict(governance_name='Old',governance_symbol='OLD'))['governance']['symbol']=='OLD'
    p=full_invite();profile=assemble(p,signed_enrollments(p));profile['governance_name']='FAKE'
    with pytest.raises(ValueError,match='TOKEN_METADATA_OVERRIDE'):verify_profile(profile)


@pytest.mark.parametrize('source',['direct','system','both'])
def test_invitation_cli_commits_metadata_and_rejects_conflicting_files(tmp_path,source):
    p=full_invite('31337','regtest');out=tmp_path/'invite.json'
    named=tmp_path/'names.json';named.write_text(json.dumps(names()))
    args=invite_args(p,out)
    if source=='direct':
        policy=tmp_path/'ulr.json';policy.write_text(json.dumps(p['ulr']))
        args+=['--ulr-policy',str(policy)]
    else:
        policy=tmp_path/'system.json';policy.write_text(json.dumps(dict(version='1',ulr=p['ulr'],
            withdrawal_fee=p['withdrawal_fee'],token_metadata=names())))
        args+=['--system-policy',str(policy)]
    if source in ('direct','both'):args+=['--token-metadata',str(named)]
    assert main(args)==0
    got=json.loads(out.read_text());assert got['token_metadata']==names()
    if source=='both':
        bad=names();bad['mining']['symbol']='BAD';named.write_text(json.dumps(bad))
        with pytest.raises(ValueError,match='TOKEN_METADATA_CONFIG_CONFLICT'):main(args)


def test_system_profile_accepts_metadata_file(tmp_path):
    from signer_agent.bootstrap_system import main as generate
    files={key:tmp_path/(key+'.json') for key in ('ulr','report','service','names','custody')}
    values=dict(ulr=dict(version='1',mode='create',bitcoin_backend='relay'),report=dict(qualified=False),service=dict(version='1'),names=names(),
        custody=dict(version='1',mode='custodians',threshold='2',sources=[]))
    for key,path in files.items():path.write_text(json.dumps(values[key]))
    out=tmp_path/'system.json'
    assert generate(['--ulr-policy',str(files['ulr']),'--qualification-report',str(files['report']),
        '--bitcoin-custody-policy',str(files['custody']),
        '--service-policy',str(files['service']),'--token-metadata',str(files['names']),
        '--protocol-fee-cap-sats','100000','--max-vbytes','10000','--max-packet-bytes','1024','--output',str(out)])==0
    assert json.loads(out.read_text())['token_metadata']==names()


def contracts(monkeypatch):
    from signer_agent import bitcoin_trust
    data={};runtime=[]
    def contract(address,abi):
        class Functions:
            def __getattr__(self,key):
                return lambda *args:NS(call=lambda **kw:data[address][key])
        return NS(address=address,functions=Functions())
    monkeypatch.setattr(bitcoin_trust,'checked_runtime',lambda w,a,k,at:runtime.append((a,k,at)))
    w3=NS(eth=NS(contract=contract))
    return data,w3,runtime


@pytest.mark.parametrize('kind,role,child_kind', [('MiningPoolV1','mining','PoolMpToken'),
    ('SimpleMultiPool','federation_share','MultiPoolShareToken'),('ConfederationMultiPool','confederation_share','ConfederationToken')])
def test_pre_sign_review_rejects_wrong_onchain_metadata(monkeypatch,kind,role,child_kind):
    data,w3,runtime=contracts(monkeypatch)
    data['pool']={('mpToken' if kind=='MiningPoolV1' else 'shareToken'):'token'}
    data['token']=dict(names()[role],poolDAO='pool')
    invite=dict(token_metadata=names())
    intent=dict(artifact=kind,target='pool')
    bundle=dict(onboarding=dict(invitation=invite))
    tokens.review_intent(w3,intent,bundle,17)
    assert ('token',child_kind,17) in runtime
    data['token']['symbol']='FAKE'
    with pytest.raises(ValueError,match='TOKEN_METADATA_MISMATCH'):tokens.review_intent(w3,intent,bundle,17)


def test_guard_rechecks_metadata_at_private_and_cached_export_boundaries(monkeypatch):
    from signer_agent import evm_network
    invite=dict(token_metadata=names(),evm_confirmations='2')
    intent=dict(target='0xabc',operation_hash='0x'+'11'*32)
    bundle=dict(onboarding=dict(invitation=invite))
    client=NS(token_metadata_context=(intent,bundle),w3=NS(eth=NS(get_block=lambda n:NS(hash=b'h'))))
    session=NS(message_hash=bytes.fromhex('11'*32),origin_contract='0xABC')
    monkeypatch.setattr(evm_network,'read_block',lambda w,d:NS(number=17,hash=b'h'))
    calls=[]
    monkeypatch.setattr(tokens,'review_intent',lambda w,i,b,at:calls.append(at))
    tokens.guard_signing_session(client,session);assert calls==[17]
    session.message_hash=b'X'*32
    with pytest.raises(ValueError,match='TOKEN_METADATA_OPERATION_MISMATCH'):tokens.guard_signing_session(client,session)
    session.message_hash=bytes.fromhex('11'*32)
    def reject(*a):raise ValueError('TOKEN_METADATA_MISMATCH')
    monkeypatch.setattr(tokens,'review_intent',reject)
    with pytest.raises(ValueError,match='TOKEN_METADATA_MISMATCH'):tokens.guard_signing_session(client,session)
