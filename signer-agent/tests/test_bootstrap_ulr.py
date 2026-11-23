"""Shared issuance/reuse, signed backend selection and exact bootstrap ABI graph."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from web3 import Web3

from signer_agent.bootstrap import artifact, encode_args
from signer_agent.bootstrap_plan import build
from signer_agent.bootstrap_ulr import ROOTS, validate
from signer_agent import bootstrap_ulr as ulr
from signer_agent import ulr_treasury as treasury_policy
from signer_agent import contract_provenance
from signer_agent.onboarding import assemble, validate_invite, verify_profile, main
from test_onboarding_policy_v3 import invitation, direct_profile, address, invite_args
from test_onboarding_mainnet import mainnet_invitation, signed_enrollments
from test_zk_bootstrap import zk_invite
from test_ulr_treasury import env


def proposal(mode="create", backend="relay", backed=False):
    p = (zk_invite() if backend == "zk-state-v2" else mainnet_invitation()) if backed else invitation()
    p.update(version="6", evm_confirmations="1", wrap_staker_bps="0", deploy_staker_bps="0", dao_use_reserve=False,
             ulr=dict(version="1", mode=mode, bitcoin_backend=backend))
    p.pop("btc_usd_feed", None)
    if mode == "reuse":
        p["ulr"]["roots"] = {name: dict(address=address(60 + i), runtime_hash="0x" + f"{i+1:02x}" * 32)
                              for i, name in enumerate(ROOTS)}
        if backed:
            p["ulr"]["roots"]["usd"] = {k: p["reward_token"][k] for k in ("address", "runtime_hash")}
    return p


def profile(p, board=False):
    result = direct_profile(p)
    for name in ("ulr", "bitcoin_policy", "reward_token", "btc_usd_feed", "evm_confirmations", "deployment_fee"):
        if name in p:
            result[name] = deepcopy(p[name])
    if board:
        # An authentic enrolled bundle is tested below; graph metadata alone is
        # inspected here without generating/using participant keys.
        result = assemble(p, signed_enrollments(p))
        result["onchain_board"] = address(90)
    return result


@pytest.mark.parametrize("backed,backend", [(False, "relay"), (True, "relay"), (True, "zk-state-v2")])
@pytest.mark.parametrize("mode", ["create", "reuse"])
def test_one_shared_graph_and_gross_fee_route(mode, backed, backend):
    p = proposal(mode, backend, backed)
    validate_invite(p)
    plan = build(profile(p))
    steps = {s["id"]: s for s in plan["steps"]}
    assert not {"controller", "twap", "settlement", "register_dao", "feed"} & steps.keys()
    if mode == "create":
        issues = [s for s in plan["steps"] if s.get("artifact") == "CustodianDAOToken"]
        assert len(issues) == 1 and issues[0]["args"] == ["ULR", "ULR", p["dao_token_recipient"]]
        assert steps["staking"]["args"] == ["$dao_token", "$usd", "200000000000000000000000", "1"]
        assert steps["snapshots"]["args"] == ["$staking", "$fed_auth"]
    else:
        assert not any(s.get("artifact") in set(ROOTS.values()) | {"DAOTestUSD"} for s in plan["steps"])
        assert {k: plan["contracts"][k]["address"] for k in ROOTS} == {k: v["address"] for k, v in p["ulr"]["roots"].items()}
        assert steps["fund_ulr_converter"]["method"] == "transfer"
    assert steps["fee_router"]["args"] == ["$vault", "$ulr_treasury", "0", "0"]
    assert steps["ulr_treasury"]["args"] == ["$confederations", p["confederation_id"], "$snapshots", "1000000000", "9000"]
    assert steps["ulr_voting"]["args"] == ["$staking", "$validator"]
    assert steps["decisions"]["args"] == ["$ulr_voting", "$conf_auth"]
    for name in ("enroll_ulr_treasury", "bind_ulr_candidates"):
        assert steps[name + "_custodians"]["method"] == "meetsMinimumCustodians"
    assert steps["mint_admission"]["artifact"] == "BitcoinMintAdmission"
    if backed:
        assert steps["relay"]["artifact"] == ("ZkBitcoinStateV2" if backend == "zk-state-v2" else "MainnetHeaderRelay")


@pytest.mark.parametrize("mode", ["create", "reuse"])
def test_generated_approvals_include_signed_root_descriptor_and_correct_artifacts(mode):
    p = proposal(mode)
    steps = {s["id"]: s for s in build(profile(p, board=True))["steps"]}
    for name, kind in (("enroll_ulr_treasury", "ULRCustodianSnapshots"), ("bind_ulr_candidates", "CustodianValidator"),
                       ("fee_payer", "CustodianDAOFeeRouter")):
        intent = steps[name + "_publish_intent"]["args"][1]["$json_hex"]
        assert intent["artifact"] == kind
        assert intent["ulr_bootstrap"] == {k: "$" + k for k in (*ROOTS, "ulr_treasury")}
        assert intent["ulr_treasury"] == "$ulr_treasury"


def test_all_constructors_calls_checks_and_outputs_match_actual_abi():
    plan = build(profile(proposal(), board=True))
    kinds = {n: s["artifact"] for n, s in plan.get("contracts", {}).items()}
    kinds["onchain_board"] = "DKGOnchainBoard"
    def resolve(value):
        if isinstance(value, str) and value.startswith("$"):
            return address(99) if value[1:] in kinds else ("0x" + "11" * 32 if value.endswith(("_hash", "_script", "_group")) else "1")
        if isinstance(value, list):
            return [resolve(x) for x in value]
        return value
    for step in plan["steps"]:
        if step["kind"] == "deploy":
            kinds[step["id"]] = step["artifact"]
            ctor = next((a for a in artifact(step["artifact"])["abi"] if a["type"] == "constructor"), {"inputs": []})
            encode_args(ctor["inputs"], resolve(step["args"]))
        elif step["kind"] == "call":
            abi = next(a for a in artifact(kinds[step["contract"]])["abi"] if a.get("name") == step["method"])
            if step["method"] != "publishIntent":
                encode_args(abi["inputs"], resolve(step["args"]))
        for output in step.get("outputs", []):
            if "artifact" in output:
                kinds[output["name"]] = output["artifact"]
        for check in step.get("checks", []):
            assert any(a.get("name") == check["method"] for a in artifact(kinds[check["contract"]])["abi"])


def test_enrollments_and_profile_commit_every_ulr_input():
    p = proposal("reuse")
    entries = signed_enrollments(p)
    built = assemble(p, entries)
    assert verify_profile(built) == p
    for mutate in (lambda q: q.pop("ulr"), lambda q: q["ulr"].update(bitcoin_backend="zk-state-v2"),
                   lambda q: q["ulr"]["roots"]["staking"].update(address=address(88))):
        q = deepcopy(built)
        mutate(q)
        with pytest.raises(ValueError):
            verify_profile(q)
    q = deepcopy(p)
    q["ulr"]["roots"]["staking"]["runtime_hash"] = "0x" + "ff" * 32
    with pytest.raises(ValueError, match="ENROLLMENT_CONTEXT"):
        assemble(q, entries)
    legacy = invitation()
    legacy.update(version="6", evm_confirmations="1")
    built = assemble(legacy, signed_enrollments(legacy))
    built["ulr"] = p["ulr"]
    with pytest.raises(ValueError, match="ENROLLED_PROFILE_MISMATCH"):
        verify_profile(built)


@pytest.mark.parametrize("mutation,error", [
    (lambda p: p["ulr"].update(bitcoin_backend="zk-state-v2"), "BACKEND_MISMATCH"),
    (lambda p: p.update(wrap_staker_bps="1"), "LEGACY_SPLIT_FORBIDDEN"),
    (lambda p: p.update(dao_use_reserve=True), "LEGACY_SPLIT_FORBIDDEN"),
    (lambda p: p["ulr"].update(mode="mint"), "BOOTSTRAP_POLICY"),
    (lambda p: p["ulr"].update(unknown=True), "BOOTSTRAP_POLICY"),
    (lambda p: p.update(btc_usd_feed={}), "BTC_USD_FEED_UNUSED"),
])
def test_reject_ambiguous_or_legacy_allocation(mutation, error):
    p = proposal()
    mutation(p)
    with pytest.raises(ValueError, match=error):
        build(profile(p))


def test_cli_publishes_ulr_policy_without_btc_usd_oracle(tmp_path, capsys):
    p = proposal(backed=True)
    policy_file = tmp_path / "ulr.json"
    policy_file.write_text(json.dumps(p["ulr"]))
    out = tmp_path / "invite.json"
    args = invite_args(p, out) + ["--bitcoin-network", "mainnet", "--ulr-policy", str(policy_file)]
    btc = tmp_path / "btc.json"
    btc.write_text(json.dumps(p["bitcoin_policy"]))
    args += ["--bitcoin-policy", str(btc), "--reward-token", p["reward_token"]["address"],
             "--reward-token-code-hash", p["reward_token"]["runtime_hash"], "--reward-token-decimals", "6"]
    assert main(args) == 0
    generated = json.loads(out.read_text())
    assert generated["ulr"] == p["ulr"] and "btc_usd_feed" not in generated


@pytest.fixture
def enrolled_context(monkeypatch):
    """RPC and provenance fixtures; real signed invitations, no cryptographic bypass claims."""
    p = proposal()
    roots = {name: address(60 + i) for i, name in enumerate(ROOTS)}
    descriptor = dict(roots, ulr_treasury=address(70))
    objects = {value: SimpleNamespace(address=value) for value in (*roots.values(), address(70), address(71), address(72))}
    bundle = dict(onboarding=dict(invitation=p, enrollments=signed_enrollments(p)), registry=address(73))
    intent = dict(ulr_bootstrap=descriptor, ulr_treasury=address(70), artifact="ULRCustodianSnapshots",
                  hash_method="getEnrollTreasuryOperationHash")
    state = dict(sender=p["operator"], method="direct", dkg_id=bytes.fromhex(p["sessions"]["evm_ceremony"]),
                 factory_registry=bundle["registry"], token_name="ULR", supply=ulr.SUPPLY, stake_token=roots["dao_token"])
    proofs = []
    def proof(_, target, kind, args, at):
        proofs.append((target, kind, args, at))
        return dict(method=state["method"], sender=state["sender"])
    def read(obj, at, method, *args):
        return dict(authorization=address(72), registry=bundle["registry"], dkgId=state["dkg_id"], factory=address(71),
            confederationId=p["confederation_id"], dkgCoordinator=state["factory_registry"],
            totalSupply=state["supply"], name=state["token_name"], symbol="ULR", stakeToken=state["stake_token"],
            rewardToken=roots["usd"],decimals=6,CUSTODIAN_THRESHOLD=200000*10**18,minimumStakeAmount=1,MIN_CUSTODIAN_ULR=200000*10**18)[method]
    monkeypatch.setattr(treasury_policy, "contract", lambda w, k, a: objects[a])
    monkeypatch.setattr(treasury_policy, "read", read)
    monkeypatch.setattr(treasury_policy, "proven", lambda w, k, a, at, args=None: objects[a])
    monkeypatch.setattr(treasury_policy, "configuration", lambda *args: (objects[roots["snapshots"]], objects[roots["staking"]]))
    monkeypatch.setattr(contract_provenance, "prove_creation", proof)
    return SimpleNamespace(profile=p, roots=roots, objects=objects, bundle=bundle, intent=intent, state=state, proofs=proofs)


def test_bootstrap_pin_proves_exact_issue_and_own_factory(enrolled_context):
    e = enrolled_context
    pin = ulr.enrolled_pin(None, e.intent, e.bundle, 5)
    assert pin["staking"] == e.roots["staking"]
    assert pin["treasuries"] == [address(70)]
    assert any(kind == "CustodianDAOToken" and args == ["ULR", "ULR", e.profile["dao_token_recipient"]]
               for _, kind, args, _ in e.proofs)
    assert any(kind == "ULRConfederationTreasury" and args == [address(71), e.profile["confederation_id"], e.roots["snapshots"], 1000000000, 9000]
               for _, kind, args, _ in e.proofs)


@pytest.mark.parametrize("field,value,error", [
    ("sender", address(100), "ORIGIN"), ("method", "internal", "ORIGIN"),
    ("dkg_id", b"z" * 32, "SYSTEM_AUTHORITY"), ("factory_registry", address(100), "BOOTSTRAP_INTENT"),
    ("token_name", "FAKE", "ROOT_BINDING"), ("supply", ulr.SUPPLY + 1, "ROOT_BINDING"),
    ("stake_token", address(100), "ROOT_BINDING"),
])
def test_participant_refuses_substituted_origin_authority_or_common_roots(enrolled_context, field, value, error):
    e = enrolled_context
    e.state[field] = value
    with pytest.raises(ValueError, match=error):
        ulr.enrolled_pin(None, e.intent, e.bundle, 5)


def test_bootstrap_pin_rejects_unsigned_and_unrelated_operations(enrolled_context):
    e = enrolled_context
    e.intent["hash_method"] = "getPublishDayOperationHash"
    with pytest.raises(ValueError, match="BOOTSTRAP_INTENT"):
        ulr.enrolled_pin(None, e.intent, e.bundle, 5)
    e.bundle["onboarding"]["invitation"]["ulr"]["bitcoin_backend"] = "ZK"
    with pytest.raises(ValueError):
        ulr.enrolled_pin(None, e.intent, e.bundle, 5)


def test_reuse_rejects_a_changed_address_or_runtime_before_inspection(monkeypatch):
    p = proposal("reuse")
    roots = {name: spec["address"] for name, spec in p["ulr"]["roots"].items()}
    w3 = SimpleNamespace(eth=SimpleNamespace(get_code=lambda *args: b"different runtime"))
    with pytest.raises(ValueError, match="ROOT_CODE_HASH"):
        ulr.inspect_roots(w3, p, roots, 5)
    roots["dao_token"] = address(100)
    with pytest.raises(ValueError, match="ROOT_CODE_HASH"):
        ulr.inspect_roots(w3, p, roots, 5)


def test_pipeline_requires_canonical_pool_mp_and_enrolled_quote(monkeypatch):
    objects = {address(i): SimpleNamespace(address=address(i)) for i in range(1, 8)}
    conf = [None] * 18
    conf[7] = address(7)
    state = dict(mpToken=address(5), authorization=address(7))
    def read(obj, at, method, *args):
        return dict(converter=address(2), inputToken=address(5), poolDAO=address(6), rewardToken=address(4),
                    getConfederation=conf, **state)[method]
    calls = []
    monkeypatch.setattr(treasury_policy, "contract", lambda w, k, a: objects[a])
    monkeypatch.setattr(treasury_policy, "read", read)
    monkeypatch.setattr(treasury_policy, "proven", lambda w, k, a, at, args=None: calls.append((k, args)) or objects[a])
    from signer_agent import bitcoin_trust
    monkeypatch.setattr(bitcoin_trust, "checked_runtime", lambda *args: None)
    p = proposal()
    ulr.review_pipeline(None, address(1), address(3), objects[address(7)], "conf", dict(onboarding=dict(invitation=p)), 5)
    assert ("FundedFeeConverter", [address(5), address(4), 3, 9999]) in calls
    state["authorization"] = address(1)
    with pytest.raises(ValueError, match="MP_BINDING"):
        ulr.review_pipeline(None, address(1), address(3), objects[address(7)], "conf", dict(onboarding=dict(invitation=p)), 5)


@pytest.mark.parametrize("backend", ["relay", "zk-state-v2"])
@pytest.mark.parametrize("mode", ["create", "reuse"])
def test_ethereum_mainnet_keeps_project_fee_route_and_finality(backend, mode):
    p = proposal(mode, backend, backed=True)
    p.update(chain_id="1", evm_confirmations="12", phase_duration="86400", integration_fee_exemption=False,
             deployment_fee=dict(amount="100", treasury=address(48)))
    steps = {s["id"]: s for s in build(profile(p, board=True))["steps"]}
    assert steps["project_router"]["args"] == ["$project_vault", address(48), "0", "0"]
    assert "project_payer_confederations" in steps
    assert steps["fee_router"]["args"][1] == "$ulr_treasury"
    assert steps["fund_ulr_converter"]["verify_token_transfer"]["amount"] == p["converter_funding"]


@pytest.mark.parametrize("payer,enrolled,ready,previous_staking", [
    (True, True, True, False), (False, True, False, False), (True, False, False, False), (True, True, True, True),
])
def test_manifest_waits_for_connections_and_exports_reusable_roots(monkeypatch, payer, enrolled, ready, previous_staking):
    p = proposal()
    contracts = {name: dict(address=address(60 + i)) for i, name in enumerate(
        (*ROOTS, "ulr_treasury", "ulr_voting", "decisions", "fee_router", "fee_pipeline"))}
    objects = {name: SimpleNamespace(address=spec["address"]) for name, spec in contracts.items()}
    w3 = SimpleNamespace(eth=SimpleNamespace(get_block=lambda _: SimpleNamespace(number=5, hash=b"a" * 32),
                                             get_code=lambda a, at: b"runtime"))
    runner = SimpleNamespace(contracts=contracts, profile=p, w3=w3, read_number=lambda: "latest", contract=lambda n: objects[n])
    calls = []
    configured = (objects["snapshots"], objects["staking"])

    def inspect_roots(w, profile, roots, at, **options):
        assert options == ({"previous_staking": True} if previous_staking else {})
        calls.append(at)
        return (*configured, objects["dao_token"])

    def treasury_context(*args, **options):
        assert options == ({"configured": configured} if previous_staking else {})
        return (*configured, objects["ulr_treasury"], None, None, None)

    monkeypatch.setattr(ulr, "inspect_roots", inspect_roots)
    monkeypatch.setattr(treasury_policy, "treasury_context", treasury_context)
    monkeypatch.setattr(treasury_policy, "voting_context", treasury_context)
    def read(_, at, method, *args):
        calls.append(at)
        return dict(scope=b"s" * 32, treasuries=objects["ulr_treasury"].address if enrolled else address(100),
                    authorizedPayers=payer, treasury=objects["ulr_treasury"].address, wrapStakerBps=0, deployStakerBps=0)[method]
    monkeypatch.setattr(treasury_policy, "read", read)
    result = ulr.manifest(runner, previous_staking=previous_staking)
    assert result["ready"] == ready
    assert all(at == 5 for at in calls)
    if ready:
        assert result["pin"]["treasuries"] == [objects["ulr_treasury"].address]
        assert result["reuse_policy"]["roots"]["staking"]["address"] == objects["staking"].address
        p["ulr"] = result["reuse_policy"]
        validate(p)


def test_bootstrap_payer_review_requires_zero_shares_verified_constructors_and_exact_pin(env, monkeypatch):
    from eth_utils.abi import collapse_if_tuple
    router = SimpleNamespace(address=address(120), abi=artifact("CustodianDAOFeeRouter")["abi"])
    vault, pipeline = address(121), address(122)
    env.objects[router.address] = router
    env.objects[vault] = SimpleNamespace(address=vault)
    original = treasury_policy.read
    state = dict(share=0, payer=False)
    proofs = []
    def read(target, at, method, *args):
        if method == "getSetPayerOperationHash":
            abi = next(f for f in router.abi if f.get("name") == method)
            return Web3.keccak(env.w3.codec.encode([collapse_if_tuple(x) for x in abi["inputs"]], args))
        values = dict(incomeVault=vault, rewardToken=address(123), getCustodianDAO=["0x" + "00" * 20] * 7,
            treasury=env.treasury.address, wrapStakerBps=state["share"], deployStakerBps=0,
            authorizedPayers=state["payer"], operationTargets=router.address)
        return values[method] if method in values else original(target, at, method, *args)
    monkeypatch.setattr(treasury_policy, "read", read)
    monkeypatch.setattr(treasury_policy, "proven", lambda w, k, a, at, args=None: proofs.append((k, args)) or env.objects[a])
    monkeypatch.setattr(ulr, "enrolled_pin", lambda *args: env.pin)
    pipeline_calls = []
    monkeypatch.setattr(ulr, "review_pipeline", lambda *args: pipeline_calls.append(args))
    intent = treasury_policy.intent_for(env.w3, router, "CustodianDAOFeeRouter", "getSetPayerOperationHash",
        [pipeline, True], env.auth.address, "conf_ceremony", 5, ulr_bootstrap={}, ulr_treasury=env.treasury.address)
    assert not treasury_policy.review(env.w3, intent, env.bundle, 5, env.pin)["complete"]
    assert ("CustodianDAOFeeRouter", [vault, env.treasury.address, 0, 0]) in proofs
    assert pipeline_calls[0][1:3] == (pipeline, router.address)
    for mutation in (lambda i: i.pop("ulr_bootstrap"), lambda i: i.update(hash_args=[pipeline, False])):
        wrong = deepcopy(intent)
        mutation(wrong)
        with pytest.raises(ValueError, match="BOOTSTRAP_PAYER_POLICY"):
            treasury_policy.review(env.w3, wrong, env.bundle, 5, env.pin)
    state["share"] = 1
    with pytest.raises(ValueError, match="BOOTSTRAP_PAYER_POLICY"):
        treasury_policy.review(env.w3, intent, env.bundle, 5, env.pin)
    state["share"] = 0
    env.view.complete = True
    with pytest.raises(ValueError, match="ROUTER_RECEIPT"):
        treasury_policy.review(env.w3, intent, env.bundle, 5, env.pin)
    state["payer"] = True
    assert treasury_policy.review(env.w3, intent, env.bundle, 5, env.pin)["complete"]


def test_zk_fee_pipeline_cannot_skip_consumer_verifier_review(monkeypatch):
    """The ULR bootstrap branch must keep the ZK checks of the ordinary signer."""
    from signer_agent import zk_spv,bitcoin_trust
    objects={address(i):SimpleNamespace(address=address(i)) for i in range(1,8)}
    conf=[None]*18;conf[7]=address(7)
    def read(obj,at,method,*args):
        return dict(converter=address(2),inputToken=address(5),poolDAO=address(6),rewardToken=address(4),
            getConfederation=conf,mpToken=address(5),authorization=address(7))[method]
    monkeypatch.setattr(treasury_policy,'contract',lambda w,k,a:objects[a])
    monkeypatch.setattr(treasury_policy,'read',read)
    monkeypatch.setattr(treasury_policy,'proven',lambda w,k,a,at,args=None:objects[a])
    monkeypatch.setattr(bitcoin_trust,'checked_runtime',lambda *a:None)
    calls=[]
    def reject(w3,invite,pool,kind,intent,at):
        calls.append((pool.address,kind,at))
        raise ValueError('ZK_CHILD_ORIGIN_MISMATCH')
    monkeypatch.setattr(zk_spv,'review_consumer',reject)
    p=proposal(backend='zk-state-v2',backed=True)
    with pytest.raises(ValueError,match='ZK_CHILD_ORIGIN_MISMATCH'):
        ulr.review_pipeline(None,address(1),address(3),objects[address(7)],'conf',dict(onboarding=dict(invitation=p)),5)
    assert calls==[(address(6),'MiningPoolV1',5)]
