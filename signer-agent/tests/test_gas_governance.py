"""Typed financial consent, nonce races, revocation and pre-private-work guards."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from eth_abi import encode
from web3 import Web3

from signer_agent import gas_governance as gov


def address(value):
    return Web3.to_checksum_address(f"0x{value:040x}")


@pytest.fixture
def env(monkeypatch):
    view = SimpleNamespace(nonce=4, time=100, epoch=1, initialized=True, halted=False,
                           policy={"binding": "reviewed"}, checked=[], domain=b"d" * 32)
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337))
    block = SimpleNamespace(number=7, hash=b"b" * 32, timestamp=100)
    w3.eth.get_block = lambda _: block
    target = SimpleNamespace(address=address(1), w3=w3)
    auth = SimpleNamespace(address=address(2))
    coordinator = SimpleNamespace(address=address(3))
    manifest = SimpleNamespace(session_id=b"s" * 32)
    ctx = dict(target=target, auth=auth, coordinator=coordinator, block=block, manifest=manifest)
    bundle = {"onchain_board": address(9)}
    monkeypatch.setattr(gov, "context", lambda *args: ctx)
    monkeypatch.setattr(gov, "policy", lambda *args: deepcopy(view.policy))
    monkeypatch.setattr(gov, "_active", lambda *args: view.checked.append("active"))
    args = [[address(5), address(6), "1", "0x" + "07" * 32, "3000", "0x" + "08" * 32]]
    from eth_utils.abi import function_abi_to_4byte_selector
    selector = function_abi_to_4byte_selector(next(x for x in gov.artifact(gov.KIND)["abi"] if x.get("name") == "configure"))
    digest = Web3.keccak(encode(["address", "bytes4", "bytes32", "uint256"], [target.address, selector,
        Web3.keccak(encode(["bytes32", gov.CONFIG_TYPE], [view.domain, gov._arguments(gov.CONFIGURE, args)[0]])), 4]))
    info = [0] * 16
    info[0], info[3], info[4], info[7], info[8], info[9], info[10], info[15] = 12, digest, True, 200, True, gov.ZERO, 1, target.address

    def read(c, method, number, *values):
        if method == "hashOperation":
            return Web3.keccak(encode(["address", "bytes4", "bytes32", "uint256"], list(values)))
        if method == gov.CONFIGURE:
            return digest
        return {"configurationNonce": view.nonce, "domain": view.domain, "getSession": info,
                "sessionDKGId": manifest.session_id, "sessionDKGResult": b"r" * 32,
                "resultHash": b"r" * 32}[method]

    monkeypatch.setattr(gov, "read", read)
    intent = dict(chain_id="31337", role="conf_ceremony", coordinator=coordinator.address,
        target=target.address, artifact=gov.KIND, hash_method=gov.CONFIGURE, hash_args=args,
        operation_hash=Web3.to_hex(digest), configuration_nonce="4", deadline="200", gas_policy=deepcopy(view.policy), session="12")
    return SimpleNamespace(w3=w3, block=block, view=view, ctx=ctx, info=info, intent=intent, bundle=bundle)


def test_review_recomputes_bound_nonce_and_financial_arguments(env):
    ctx, args = gov.review(env.w3, env.intent, env.bundle, 7, session=12)
    assert ctx == env.ctx and args[0][4] == 3000
    assert env.view.checked == ["active"]


@pytest.mark.parametrize("field,value", [("configuration_nonce", "5"), ("deadline", "0200"),
    ("configuration_nonce", "-1"), ("operation_hash", "0x" + "44" * 32),
    ("role", "evm_ceremony"), ("chain_id", "1"), ("hash_method", "hashOperation")])
def test_invalid_typed_intent_rejected(env, field, value):
    env.intent[field] = value
    with pytest.raises(ValueError, match="GAS_GOVERNANCE_"):
        gov.review(env.w3, env.intent, env.bundle, 7, session=12)


@pytest.mark.parametrize("index,value", [(1, address(11)), (4, "3001"), (5, "0x" + "99" * 32)])
def test_financial_terms_cannot_diverge_from_operation(env, index, value):
    env.intent["hash_args"][0][index] = value
    with pytest.raises(ValueError, match="GAS_GOVERNANCE_HASH"):
        gov.review(env.w3, env.intent, env.bundle, 7)


def test_new_configuration_invalidates_old_unexecuted_operation(env):
    env.view.nonce += 1
    with pytest.raises(ValueError, match="GAS_CONFIGURATION_CHANGED"):
        gov.review(env.w3, env.intent, env.bundle, 7, session=12)
    # Historical hashing remains possible for exact executed-operation recovery.
    gov.review(env.w3, env.intent, env.bundle, 7, active=False, session=12)
    assert env.view.checked == []


def test_policy_display_cannot_be_replaced_without_rejection(env):
    env.intent["gas_policy"] = {"binding": "different"}
    with pytest.raises(ValueError, match="GAS_GOVERNANCE_POLICY_CHANGED"):
        gov.review(env.w3, env.intent, env.bundle, 7)


def test_original_session_deadline_cannot_be_extended(env):
    env.info[7] = 201
    with pytest.raises(ValueError, match="GAS_GOVERNANCE_SESSION"):
        gov.review(env.w3, env.intent, env.bundle, 7, session=12)


def test_open_session_expiry_blocks_new_private_work_but_finalized_can_execute(env):
    env.block.timestamp = 200
    with pytest.raises(ValueError, match="GAS_GOVERNANCE_DEADLINE"):
        gov.review(env.w3, env.intent, env.bundle, 7, session=12)
    env.info[10] = 2
    gov.review(env.w3, env.intent, env.bundle, 7, session=12)


def test_pause_does_not_require_available_state(monkeypatch):
    # Deliberately no state/factory fields: pause returns before all state probes.
    gov._active({}, gov.PAUSE, [])


def test_active_configuration_profile_cannot_be_enabled_on_mainnet():
    ctx = dict(target=SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(chain_id=1))), block=SimpleNamespace(number=1))
    with pytest.raises(ValueError, match="GAS_SPV_PROFILE_NOT_QUALIFIED"):
        gov._active(ctx, gov.CONFIGURE, [])


def test_generic_service_cannot_sign_gas_without_typed_review(monkeypatch):
    monkeypatch.setattr(gov, "checked_runtime", lambda *args: "verified")
    client = SimpleNamespace(w3=SimpleNamespace(eth=SimpleNamespace(get_block=lambda _: SimpleNamespace(number=1))))
    with pytest.raises(ValueError, match="GAS_TYPED_INTENT_REQUIRED"):
        gov.guard_signing_session(client, SimpleNamespace(origin_contract=address(1)))


def test_shared_guard_precedes_commit_and_cached_share_paths():
    from signer_agent.service import SigningService
    service = object.__new__(SigningService)
    touched = []
    def block(_):
        touched.append("gas guard")
        raise ValueError("GAS_CONFIGURATION_CHANGED")
    service.client = SimpleNamespace(chain_id=31337, guard_gas_session=block)
    with pytest.raises(ValueError, match="GAS_CONFIGURATION_CHANGED"):
        service._approve_locked(SimpleNamespace(state=1, purpose=1))
    assert touched == ["gas guard"]


@pytest.mark.parametrize("delegated", [True, False])
def test_gas_public_proposal_uses_custodian_permission_before_any_send(env, tmp_path, monkeypatch, delegated):
    import json
    from contextlib import nullcontext
    from signer_agent import bitcoin_onchain, onchain_transport, operation_status
    sender, original = address(80), address(81)
    board = SimpleNamespace(address=address(9), functions=SimpleNamespace(
        operator=lambda:SimpleNamespace(call=lambda **kw:original),
        canPublishIntent=lambda who:SimpleNamespace(call=lambda **kw:delegated and who == sender)))
    package=tmp_path/'gas.json';intent=deepcopy(env.intent);intent.pop('session');package.write_text(json.dumps(intent))
    registry=SimpleNamespace(w3=env.w3,account=SimpleNamespace(address=sender))
    monkeypatch.setattr(gov,'confirmed_block',lambda *args:env.block)
    monkeypatch.setattr(onchain_transport,'board_at',lambda *args:board)
    monkeypatch.setattr(operation_status,'executed_operation',lambda *args,**kw:False)
    env.ctx['coordinator'].events=SimpleNamespace(SessionCreated=lambda:SimpleNamespace(
        process_receipt=lambda *args,**kwargs:[dict(args=dict(sessionId=12))]))
    sent=[]
    def send(registry,target,kind,method,args,**kwargs):
        assert method in ('createSessionFromDKG','publishIntent')
        sent.append(method)
        return {}
    monkeypatch.setattr(bitcoin_onchain,'send',send)
    state=SimpleNamespace(exclusive=nullcontext)
    args=SimpleNamespace(action='gas-propose',board=board.address,package=package)
    if delegated:
        result=gov.management(args,dict(env.bundle,role='conf_ceremony'),registry,state)
        assert result['status']=='AWAITING_PARTICIPANTS' and sent==['createSessionFromDKG','publishIntent']
    else:
        with pytest.raises(ValueError,match='GAS_BOARD_OPERATOR_REQUIRED'):
            gov.management(args,dict(env.bundle,role='conf_ceremony'),registry,state)
        assert not sent
